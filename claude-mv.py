#!/usr/bin/env python3
"""claude-mv — mv a directory and migrate its Claude Code history with it.

Invoked by the `claude-mv` zsh wrapper (claude-mv.zsh). Performs the actual
`mv` (unless --already-moved, see below), then for every Claude profile dir
passed via --profile:

  1. renames <profile>/projects/<encoded-old-cwd> to the new encoding — both
     the moved dir itself and any project dirs nested under it (e.g. a
     monorepo subdir with its own sessions)
  2. rewrites the top-level "cwd" field in the renamed projects' *.jsonl
     session files
  3. re-keys the "projects" map in the profile's config JSON
     (~/.claude.json for the default ~/.claude profile, else
     <profile>/.claude.json — the CLAUDE_CONFIG_DIR layout)
  4. rewrites the "project" field in <profile>/history.jsonl

Everything else under a profile (todos/, file-history/, shell-snapshots/,
session-env/, plans/, tasks/) is keyed by session id, not path, so it needs
no migration.

Claude Code encodes a project cwd by replacing every non-alphanumeric char
with "-" (/Users/me/.zsh → -Users-me--zsh). That encoding is ambiguous in
reverse (foo/bar and foo-bar collide), so nested project dirs are only
remapped when a session jsonl inside them confirms a real cwd under the
moved path.

Both arguments go through canonical() first (~, relative paths, trailing
slashes, symlinked ancestors), because that encoding is only a lookup key
if it is computed from the same physical path the kernel handed Claude as
its cwd — see canonical() for why the last component is left alone.

--already-moved reconciles a folder that was renamed by something else (a
plain `mv`, an editor, Claude itself) and left its history stranded on the
old path. No move is performed: src must be gone, dst must exist, and only
the history migration above runs. It is the same code path otherwise — the
old path can't be recovered from the encoded dir name (that encoding is
ambiguous), so it still has to be given explicitly. Sessions started in the
renamed folder before reconciling are the normal case, so the destination
usually already has its own (stub) history — that's the conflict path, and
`consolidate` is the mode that keeps both sides.

Destination history may already exist (e.g. the target path once hosted
Claude sessions of its own). All such conflicts are detected UP FRONT —
before the mv — and resolved by one policy, asked interactively on a tty
or supplied via --on-conflict:

  overwrite    destination history replaced by the moved project's; the
               discarded history survives in the kept restore point
  consolidate  merge — session files combined into one project dir, config
               entries field-merged (booleans OR, allowedTools unioned,
               counters maxed, empty/default fields filled from the source)
  rename-only  do the plain mv, leave all Claude history untouched
  abort        do nothing at all

--extract moves individual SESSIONS instead of a folder, for the project
that was born mid-session in a parent directory: you were in ~/code, told
Claude to make a folder and cd into it, and the whole conversation stayed
keyed on ~/code. Moving all of ~/code's history would be wrong — the other
sessions belong there — so this mode picks out the ones that don't. The
candidates are the sessions HOMED in src (whichever cwd they later wandered
to), listed via ccfind when it is installed and off the filesystem otherwise;
a picker chooses among them, or --session <id> names them outright. Only
their transcripts, sidecars and history entries move; src keeps everything
else, and no folder is touched. See the section above run_session_mode() for
what that does and does not rewrite.

Restore points: before any history migration, every path about to be
touched (affected project dirs, conflicting destination dirs, config JSON,
history.jsonl) is copied into $CLAUDE_MV_RESTORE_ROOT (default
~/.claude-mv/restore)/<stamp>/ together with a manifest. On success the
restore point is deleted — EXCEPT after an overwrite, where it is kept as
the archive of the discarded destination history. On mid-migration failure
it is kept and the error message names it. `--restore` lists restore
points; `--restore <stamp|latest>` rolls everything back (folder move
included, when there was one) after confirmation.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

CONFLICT_MODES = ("overwrite", "consolidate", "rename-only", "abort")
RESTORE_ROOT = os.environ.get("CLAUDE_MV_RESTORE_ROOT") or \
    os.path.expanduser("~/.claude-mv/restore")


# ── colour ──────────────────────────────────────────────────────────────────
# Everything claude-mv prints is a human-facing report — there is no porcelain
# for anything to parse — so colour is applied throughout, on both streams. It
# is a pure overlay: with colour off every line stays byte-identical to what it
# was before, which is what keeps the tests (which capture pipes, so colour is
# already off) reading plain text.
#
# Off when piped, honouring NO_COLOR; $CLAUDE_MV_COLOR=always|never forces it
# either way (`always` is what the README-SVG generator uses).
#
# The palette carries meaning, so keep it consistent when adding output:
#   cyan    a path, on-disk name or config key — the thing being acted on
#   bold    the identifier or count that makes the line worth reading
#   dim     provenance and asides (counts in parens, [profile] tags, arrows)
#   green   done / did / the safe choice    yellow  would / warning / prompt
#   red     an error, or the destructive choice
_SGR = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36"}


def color_enabled(stream=None) -> bool:
    mode = os.environ.get("CLAUDE_MV_COLOR", "auto")
    if mode == "always":
        return True
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    return (stream or sys.stdout).isatty()


def c(text: str, *styles: str, stream=None) -> str:
    """Wrap `text` in SGR styles when colour is on for `stream`.

    Callers must pad/align BEFORE colouring — an escape sequence counts
    toward str width but not toward what the terminal draws.
    """
    if not text or not styles or not color_enabled(stream):
        return text
    return "\033[" + ";".join(_SGR[s] for s in styles) + "m" + text + "\033[0m"


def emsg(msg: str) -> str:
    """`claude-mv: <msg>`, prefix coloured for stderr (where these all go)."""
    return c("claude-mv:", "red", "bold", stream=sys.stderr) + " " + msg


def wmsg(msg: str, stream=None) -> str:
    """`⚠️  <msg>`, sign coloured. The emoji renders double-width, so the
    plain form carries two trailing spaces — one here, one from the join."""
    return c("⚠️ ", "yellow", "bold", stream=stream) + " " + msg


def enc(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def canonical(path: str) -> str:
    """Absolute path with symlinked *ancestors* resolved, last component kept.

    Claude Code keys history on the process cwd, which the kernel reports
    physically — symlinks already resolved. So `/tmp/x` (macOS: a symlink to
    `/private/tmp/x`) is recorded as `/private/tmp/x`, and a path given
    through a symlinked ancestor has to be resolved the same way or enc()
    looks up a projects/ dir that was never there. Also handles ~ and
    relative paths, and drops trailing slashes.

    The last component is deliberately NOT resolved: if it is itself a
    symlink, `mv` renames the link, and the real folder — the one whose cwd
    the sessions recorded — does not move, so its history must stay put.
    """
    path = os.path.abspath(os.path.expanduser(path))
    parent, base = os.path.split(path)
    return os.path.join(os.path.realpath(parent), base) if base else path


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def remap(path: str, old: str, new: str) -> str:
    return new + path[len(old):]


def is_default(v) -> bool:
    return v in (None, False, 0, "", [], {})


def config_json_path(profile: str) -> str:
    default = os.path.join(os.path.expanduser("~"), ".claude")
    if os.path.realpath(profile) == os.path.realpath(default):
        return os.path.expanduser("~/.claude.json")
    return os.path.join(profile, ".claude.json")


def atomic_write(path: str, data: str) -> None:
    tmp = f"{path}.claude-mv-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def jsonl_first_cwd_of_file(path: str) -> str | None:
    """The first top-level "cwd" in one session file — its *starting* cwd.

    ccfind derives its `cwd` column the same way (first match in the file), so
    the two session sources agree on which sessions belong to a project dir.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str):
                    return cwd
    except OSError:
        return None
    return None


def jsonl_first_cwd(project_dir: str) -> str | None:
    """Best-effort: find a top-level "cwd" value in any session jsonl."""
    try:
        names = sorted(n for n in os.listdir(project_dir) if n.endswith(".jsonl"))
    except OSError:
        return None
    for name in names:
        cwd = jsonl_first_cwd_of_file(os.path.join(project_dir, name))
        if cwd is not None:
            return cwd
    return None


def find_project_dirs(projects_root: str, old: str) -> list[tuple[str, str]]:
    """[(dir_path, real_old_cwd)] for project dirs affected by the move."""
    if not os.path.isdir(projects_root):
        return []
    hits = []
    exact = enc(old)
    prefix = exact + "-"
    for name in sorted(os.listdir(projects_root)):
        d = os.path.join(projects_root, name)
        if not os.path.isdir(d):
            continue
        if name == exact:
            hits.append((d, old))
        elif name.startswith(prefix):
            # Could be a nested project (old/sub) or an unrelated sibling
            # (old-suffix) — only a session's recorded cwd can tell them apart.
            cwd = jsonl_first_cwd(d)
            if cwd and under(cwd, old):
                hits.append((d, cwd))
    return hits


# ── session discovery ───────────────────────────────────────────────────────
# --extract moves individual sessions rather than a whole folder, so it needs
# a candidate list: the sessions homed in src's project dir. Two interchangeable
# sources produce it, and they must agree — a soft dependency that quietly
# returns a different set depending on what is installed is worse than no
# dependency at all, so the suite pins them against each other.
#
#   ccfind      `ccfind --json -l -x -d <src>` — adds full-text search over the
#               transcript bodies and its own multi-profile resolution.
#   filesystem  a walk of <profile>/projects/<enc(src)>/*.jsonl. No search, but
#               it sweeps every profile claude-mv was given, so it is a faithful
#               substitute rather than a degraded one.
#
# $CLAUDE_MV_SOURCE=ccfind|fs|auto forces one (auto = ccfind when the wrapper
# found it). Tests need that: CI has no ccfind, so auto-detection alone would
# exercise one path twice and the other never.

# Openings that mean the machine was talking, not the person. str.startswith
# takes a tuple, so this is one comparison per candidate turn.
MACHINE_PREFIXES = ("<", "Caveat:", "[Request interrupted",
                    "Base directory for this skill:")


def turn_text(obj: dict) -> str:
    """What was said on one transcript line, as one clean line of text.

    "" when the line carries nothing a person would read — a summary record, a
    tool call, a turn whose blocks are all of some other kind.

    A turn's content is usually a list of typed blocks, and tool RESULTS come
    back as user turns too — on this machine 14049 of 14132 blocks in user
    turns are tool_result, against 81 text. Take the text blocks and nothing
    else, so a turn that was only the machine reporting back reads as empty.
    """
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    if not (isinstance(content, str) and content.strip()):
        return ""
    return " ".join(content.split())


def clip(text: str, limit: int) -> str:
    """`text` in at most `limit` characters, the ellipsis inside the budget."""
    return text[:limit - 1] + "…" if len(text) > limit else text


def session_snippet(path: str, limit: int = 120) -> str:
    """First human turn in a transcript, as the label a picker shows.

    Best-effort by design: an unreadable or contentless session still deserves
    a row in the picker — it is the id that gets acted on, not the snippet.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "user":
                    continue
                text = turn_text(obj)
                if not text:
                    continue
                # Not every "user" turn was typed by one: Claude injects
                # caveats, slash-command expansions, skill preambles and
                # interruption markers as user messages, and a picker row
                # labelled "<local-command-caveat>" identifies nothing.
                #
                # Matched by exact prefix rather than anything looser — the
                # markers are bracketed, but so is a real prompt that opens
                # with a pasted image ("[Image #4] So these are the fields…"),
                # and dropping those would lose the very rows this list exists
                # to show.
                if text.startswith(MACHINE_PREFIXES):
                    continue
                return clip(text, limit)
    except OSError:
        pass
    return "(no prompt recorded)"


# ── full-text search ────────────────────────────────────────────────────────
# --search narrows the candidates to the sessions that actually mention
# something, which is the difference between "I know roughly when it was" and
# "I know what it was about".
#
# Matching mirrors ccfind exactly, because ccfind does the matching whenever it
# is installed: a **literal, case-insensitive substring** of a raw transcript
# line — not a regex, not a word-by-word AND, and it cannot span two lines.
# Matching the raw line rather than the decoded conversation is ccfind's choice
# and it is the right one here: a path, a filename, a tool result or an error
# message nobody ever typed is often exactly how a conversation is remembered.
#
# The excerpt, though, is ours in both cases — see excerpt(). A row has to read
# the same however the match was found, and ccfind's own snippet is a window on
# the raw JSON, which is the right answer for a search tool printing lines and
# the wrong one for a picker offering conversations.

def needle(query: str) -> str:
    """The query as it will be matched: words joined by one space, lowercased.

    ccfind joins its query words with a single space and folds case by
    default; this has to agree with it, or the same --search would mean two
    different things depending on what happens to be installed.
    """
    return " ".join(query.split()).lower()


def one_line(line: str) -> str:
    """A transcript line with its control characters made printable.

    Length-preserving on purpose: the caller has already found the match by
    offset in the raw line, so anything that shifted the text would move the
    window off it.
    """
    return "".join(ch if ch >= " " else " " for ch in line.rstrip("\n"))


def window(text: str, at: int, limit: int) -> str:
    """`limit` characters of `text` around the hit at `at`, marked when cut.

    A third of the budget goes to the left of the match: enough to see what
    the sentence was doing, while keeping the match itself on screen when a
    narrow terminal trims the row further.
    """
    start = max(0, at - limit // 3)
    end = start + limit
    return (("…" if start else "") + text[start:end].strip() +
            ("…" if end < len(text) else ""))


def excerpt(line: str, want: str, limit: int = 120) -> str:
    """The matching line, as the picker should show it.

    The line is a JSON record, so the hit can be in what was said or in the
    bookkeeping around it. When the turn's own text contains it, show that —
    it is the sentence a person would recognise. Otherwise fall back to the
    raw record, which is at least where the match actually is.
    """
    text = ""
    try:
        text = turn_text(json.loads(line))
    except ValueError:
        pass
    raw = one_line(line)
    for candidate in (text, raw):
        at = candidate.lower().find(want)
        if at >= 0:
            return window(candidate, at, limit)
    return window(raw, 0, limit)


def match_excerpt(path: str, want: str) -> str | None:
    """The first line of `path` mentioning `want`. None when none does.

    Stops at the first hit, so a session that matches early costs a few lines
    of reading rather than a whole transcript.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if want in line.lower():
                    return excerpt(line, want)
    except OSError:
        return None          # unreadable is not a match, it is not an answer
    return None


def search_rows(rows: list[dict], want: str) -> list[dict]:
    """The rows whose transcript mentions `want`, each carrying its excerpt."""
    hits = []
    for row in rows:
        found = match_excerpt(row["path"], want)
        if found is not None:
            row["match"] = found
            hits.append(row)
    return hits


def attach_excerpts(rows: list[dict], want: str) -> None:
    """Settle the excerpt each row shows for `want`.

    A row whose opening line already contains the match gets none: the picker
    would otherwise print the same words twice in two columns, and the opening
    line is the better half.

    Rows ccfind matched arrive with no excerpt at all, so they are read here.
    That read can come back empty — ccfind greps bytes in the machine's
    locale, we read decoded text, and a match it saw is not guaranteed to be
    one we can point at. The row stays, because ccfind is the one that was
    asked; it just shows nothing about where the match was rather than
    inventing it.
    """
    for row in rows:
        if want in row["snippet"].lower():
            row["match"] = ""
        elif not row.get("match"):
            row["match"] = match_excerpt(row["path"], want) or ""


def session_rows(profile: str, src: str,
                 recursive: bool = False) -> list[dict]:
    """Sessions homed in <profile>/projects/<enc(src)>/ — the filesystem source.

    "Homed in" is the right predicate, not "recorded cwd equals src": a session
    that started in src and cd'd elsewhere — precisely the case this feature
    exists for — keeps its file in src's project dir for its whole life. Its
    LATER cwd lines are somewhere else entirely, so anything matching on those
    would miss the one session the user is looking for.

    `recursive` widens that to src and every project dir below it, which is
    the scope for "I know what the conversation was about, not which folder I
    was standing in when it started". Same prefix-then-confirm walk the folder
    move uses, and for the same reason: enc() is lossy, so `<enc(src)>-thing`
    is only src's subdirectory when a session inside it says so.
    """
    root = os.path.join(profile, "projects")
    if recursive:
        dirs = find_project_dirs(root, src)
    else:
        d = os.path.join(root, enc(src))
        dirs = [(d, src)] if os.path.isdir(d) else []
    rows = []
    for d, home in dirs:
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(d, name)
            # enc() is lossy, so this dir can legitimately hold sessions of a
            # different real path (/a/b/c and /a/b-c encode alike, and Claude
            # files both here). Confirm against what the session recorded
            # before offering to move it.
            cwd = jsonl_first_cwd_of_file(path)
            if cwd is not None and not (under(cwd, src) if recursive
                                        else cwd == src):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            rows.append({"id": name[:-len(".jsonl")], "profile": profile,
                         "path": path, "mtime": mtime, "home": cwd or home,
                         "snippet": session_snippet(path)})
    return rows


def ccfind_command(args: list[str]) -> list[str] | None:
    """How to invoke ccfind, as resolved by the zsh wrapper.

    ccfind is a zsh *function* in an interactive shell, so there is often no
    file on PATH to exec — the wrapper resolves it (via $functions_source) to
    the script that defines it and passes that down, which we source in a
    throwaway zsh. CLAUDE_MV_CCFIND_BIN is the simpler case: a real executable.
    """
    src = os.environ.get("CLAUDE_MV_CCFIND_SOURCE")
    if src and os.path.isfile(src):
        # -f: skip the user's rc. Sourcing the script still auto-loads the .env
        # beside it, which is where that machine's ccfind profiles live.
        return ["zsh", "-fc", 'source "$1"; shift; ccfind "$@"', "_", src, *args]
    binary = os.environ.get("CLAUDE_MV_CCFIND_BIN")
    if binary:
        return [binary, *args]
    return None


def ccfind_rows(src: str, profiles: list[str], limit: int, query: str = "",
                recursive: bool = False) -> list[dict] | None:
    """Sessions homed in `src`, via ccfind. None = unusable, use the walk.

    Falling through rather than raising is the whole contract of a soft
    dependency: ccfind absent, too old, broken, or answering about a scope we
    did not ask for must all land on the filesystem source, never on an error
    and never on a silently different answer.

    A `query` is handed straight down: ccfind greps the transcripts with -F
    and reports the sessions that matched, which is the same question we would
    otherwise answer by reading every candidate ourselves. `-I` goes with it
    so that CCFIND_CASE on this machine cannot quietly decide what --search
    means; without it the fallback matcher and ccfind would disagree about
    case on some machines and not others.
    """
    scope = ["-d", src] if recursive else ["-x", "-d", src]
    # `--` ends ccfind's flag loop, so a query that opens with a dash is text
    # rather than an unknown option.
    text = ["--", query] if query else []
    cmd = ccfind_command(["--json", "-l", "-I", *scope, "-n", str(limit),
                          *text])
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return None

    # The compatibility handshake. A ccfind predating -x rejects the flag, but
    # one that accepted it and ignored it would answer about the whole SUBTREE
    # — for ~/code that is every sub-repo's sessions, offered up as if they
    # lived here. `scope_exact` is how the answer says which question it heard,
    # so it has to say back exactly what we asked: True when we sent -x, False
    # when we deliberately did not. Anything else, missing included, is an
    # answer to a different question.
    if doc.get("scope_exact") is not (not recursive):
        return None
    if query:
        # Same handshake, for the search. ccfind reads a leading positional as
        # a profile name when it matches one, so a query whose first word is
        # also a configured profile label would silently become a filter and
        # the answer would be every session in that profile. The echoed query
        # is how we know it heard text; `case_sensitive` is how we know -I
        # landed, because a query matched with the wrong case-folding is the
        # same kind of wrong answer.
        if doc.get("query") != query or doc.get("case_sensitive") is not False:
            return None

    known = {os.path.realpath(p): p for p in profiles}
    rows = []
    for hit in doc.get("results") or []:
        cfg = hit.get("config_dir")
        path = hit.get("path")
        sid = hit.get("id")
        if not (isinstance(cfg, str) and isinstance(path, str)
                and isinstance(sid, str)):
            continue
        profile = known.get(os.path.realpath(cfg))
        if profile is None:
            # ccfind resolves profiles independently of us (CCFIND_PROFILES,
            # its own claude-profile bridge), so it can see config dirs this
            # run was never given. Migrating into one would write to a profile
            # the user did not ask claude-mv to touch.
            continue
        # Confirm the hit really belongs to src — see session_rows() on why
        # the project dir alone cannot say. ccfind reports "?" when it could
        # not extract a cwd; that is "don't know", not "not ours", so read the
        # file ourselves rather than dropping a session on its silence.
        cwd = hit.get("cwd")
        if cwd in (None, "?"):
            cwd = jsonl_first_cwd_of_file(path)
        if cwd is not None and not (under(cwd, src) if recursive
                                    else cwd == src):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue          # ccfind saw it, we cannot — do not offer it
        # ccfind's snippet is a window on the raw JSON line that matched,
        # which is what a search tool printing lines should show and not what
        # a picker offering conversations should: with a query, the row's
        # identity stays the opening prompt and the match goes in its own
        # column. See excerpt().
        snippet = (("" if query else hit.get("snippet"))
                   or session_snippet(path))
        rows.append({"id": sid, "profile": profile, "path": path,
                     "mtime": mtime, "home": cwd or src, "snippet": snippet})
    if doc.get("truncated"):
        print(wmsg(f"ccfind returned {c(str(doc.get('shown')), 'bold')} of "
                   f"{c(str(doc.get('total')), 'bold')} sessions — raise "
                   f"{c('--limit', 'cyan')} to see the rest"),
              file=sys.stderr)
    return rows


def find_sessions(src: str, profiles: list[str], limit: int,
                  query: str = "",
                  recursive: bool = False) -> list[dict] | None:
    """The candidate list, newest first, from whichever source is available.

    None means the source could not answer at all — distinct from an empty
    list, which means it answered "nothing here". The caller reports them
    differently: one is a broken setup, the other is a mistyped path.

    With a `query`, whichever source answered has already narrowed the list to
    the sessions that mention it — ccfind by grepping, the walk by reading. The
    excerpt each row shows is attached here either way, and only for the rows
    that survive the cap, so a search costs a re-read of what it will print
    rather than of everything it looked at.
    """
    mode = (os.environ.get("CLAUDE_MV_SOURCE") or "auto").strip().lower()
    cap = limit if limit else 10_000      # 0 = uncapped (see run_session_mode)
    query = " ".join(query.split())       # the form ccfind echoes back
    want = needle(query)
    rows = None
    if mode in ("auto", "ccfind"):
        rows = ccfind_rows(src, profiles, cap, query, recursive)
        if rows is None and mode == "ccfind":
            print(emsg("CLAUDE_MV_SOURCE=ccfind, but ccfind could not answer "
                       "(not installed, too old for --json/-x/-I, answered a "
                       "different question, or failed) — unset it to walk the "
                       "filesystem instead"),
                  file=sys.stderr)
            return None
    if rows is None:
        rows = [row for p in profiles
                for row in session_rows(p, src, recursive)]
        if want:
            rows = search_rows(rows, want)
    # Newest first, id as the tie-break so the order — and so every test that
    # picks "the second row" — is stable rather than filesystem-dependent.
    rows.sort(key=lambda r: (-r["mtime"], r["id"]))
    if want and len(rows) > cap:
        # The plain list is capped quietly — --limit says what it does and the
        # newest N is a reasonable answer to "show me the sessions". A capped
        # SEARCH is different: the match you are looking for may be the one
        # that fell off, and nothing on screen would say so.
        print(wmsg(f"{c(str(len(rows)), 'bold')} sessions match — showing the "
                   f"newest {c(str(cap), 'bold')}; raise "
                   f"{c('--limit', 'cyan')} to see the rest"),
              file=sys.stderr)
    rows = rows[:cap]
    if want:
        attach_excerpts(rows, want)
    return rows


def rewrite_jsonl_field(path: str, field: str, old: str, new: str,
                        dry_run: bool) -> int:
    """Rewrite top-level `field` values under `old` in a jsonl file.

    Only lines whose field actually changes are re-serialized; everything
    else is passed through byte-identical. Returns changed-line count.
    """
    changed = 0
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.rstrip("\n")
            try:
                obj = json.loads(stripped)
            except ValueError:
                out.append(line)
                continue
            val = obj.get(field)
            if isinstance(val, str) and under(val, old):
                obj[field] = remap(val, old, new)
                out.append(json.dumps(obj, ensure_ascii=False,
                                      separators=(",", ":")) + "\n")
                changed += 1
            else:
                out.append(line)
    if changed and not dry_run:
        atomic_write(path, "".join(out))
    return changed


def check_live_sessions(profiles: list[str], roots: list[str]) -> list[str]:
    """Live Claude processes whose cwd is inside any of `roots`.

    Normally that's just the dir being moved. With --already-moved the
    destination is checked too: the folder is already renamed, so a session
    running there right now is writing to the very project dir the migration
    is about to merge into.
    """
    live = []
    for profile in profiles:
        sess_dir = os.path.join(profile, "sessions")
        if not os.path.isdir(sess_dir):
            continue
        for name in os.listdir(sess_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(sess_dir, name), encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            cwd, pid = obj.get("cwd"), obj.get("pid")
            if not (isinstance(cwd, str) and pid
                    and any(under(cwd, r) for r in roots)):
                continue
            try:
                os.kill(int(pid), 0)
            except (OSError, ValueError):
                continue  # stale record, process gone
            live.append(f"pid {pid} in {cwd} ({profile})")
    return live


def merge_entries(src: dict, dst: dict) -> dict:
    """Consolidate two config project entries; dst (destination) wins for
    populated scalars, src fills gaps/defaults, booleans OR, lists union,
    counters max."""
    out = dict(dst)
    for k, v in src.items():
        cur = out.get(k)
        if isinstance(v, bool) and isinstance(cur, bool):
            out[k] = cur or v
        elif isinstance(v, list) and isinstance(cur, list):
            out[k] = cur + [x for x in v if x not in cur]
        elif k == "projectOnboardingSeenCount" and \
                isinstance(v, int) and isinstance(cur, int):
            out[k] = max(cur, v)
        elif k not in out or is_default(cur):
            out[k] = v
    return out


def build_plan(profile: str, old: str, new: str) -> dict:
    """Everything this profile needs, split into clean vs conflicting."""
    plan = {"profile": profile, "dir_moves": [], "dir_conflicts": [],
            "key_moves": [], "key_conflicts": [], "cfg": None, "hist": None}

    for d, real_old in find_project_dirs(os.path.join(profile, "projects"), old):
        target = os.path.join(profile, "projects", enc(remap(real_old, old, new)))
        bucket = "dir_conflicts" if os.path.exists(target) else "dir_moves"
        plan[bucket].append((d, target))

    cfg = config_json_path(profile)
    if os.path.isfile(cfg):
        plan["cfg"] = cfg
        with open(cfg, encoding="utf-8") as f:
            projects = json.load(f).get("projects")
        if isinstance(projects, dict):
            for key in sorted(projects):
                if not under(key, old):
                    continue
                new_key = remap(key, old, new)
                bucket = "key_conflicts" if new_key in projects else "key_moves"
                plan[bucket].append((key, new_key))

    hist = os.path.join(profile, "history.jsonl")
    if os.path.isfile(hist):
        plan["hist"] = hist
    return plan


def stranded_tally(plans: list[dict], src: str, dst: str) -> dict:
    """What is still keyed on `src`, counted in the report's own counters.

    The same numbers the run would close with, gathered before it starts —
    including the history entries, counted by asking the rewriter for a dry
    run rather than by a second reading of the same rule.
    """
    t = new_tally()
    for plan in plans:
        for d, _ in plan["dir_moves"] + plan["dir_conflicts"]:
            t["dirs"] += 1
            try:
                t["sessions"] += sum(1 for n in os.listdir(d)
                                     if n.endswith(".jsonl"))
            except OSError:
                pass
        t["keys"] += len(plan["key_moves"]) + len(plan["key_conflicts"])
        if plan["hist"]:
            t["history"] += rewrite_jsonl_field(plan["hist"], "project",
                                                src, dst, dry_run=True)
    return t


def moved_folder_at(src: str, dst: str) -> str:
    """Where the folder that used to be `src` is now, given `dst`.

    `mv old new` and `mv old somewhere/` are the same command with different
    intent, and once `old` is gone only the disk can say which one happened.
    If there is a folder of src's name sitting inside dst, that is the
    mv-into-a-directory reading and the one a plain `mv` would have produced;
    otherwise dst is the new name.
    """
    inside = os.path.join(dst, os.path.basename(src))
    return os.path.realpath(inside if os.path.isdir(inside) else dst)


def offer_reconcile(src: str, dst: str, profiles: list[str], args) -> bool:
    """`src` is gone and `dst` is here: offer to re-key what was left behind.

    The folder move and --already-moved are one migration with different
    amounts of it already done, and which one applies is a fact about the disk
    rather than a decision the user should have to make twice. So when the
    disk says the move already happened, say what is stranded and ask —
    rather than refusing and naming a flag to retype the command with.

    Only when something IS stranded. A src that never had history is a
    mistyped path, and offering to migrate nothing would dress a typo up as a
    plan. True to carry on as --already-moved; False when the caller should
    give up, having said why.
    """
    err = sys.stderr
    plans = [build_plan(p, src, dst) for p in profiles]
    if not any(p["dir_moves"] or p["dir_conflicts"] or p["key_moves"]
               or p["key_conflicts"] for p in plans):
        print(emsg(f"src is not a directory: "
                   f"{c(src, 'cyan', stream=err)}\n  "
                   f"{c(dst, 'cyan', stream=err)} is there, but no Claude "
                   f"history is keyed on {c(src, 'cyan', stream=err)} — "
                   f"nothing to re-key"), file=err)
        return False

    stranded = summarize(stranded_tally(plans, src, dst))
    print(wmsg(c(f"{src} is not there, but {dst} is — "
                 f"the folder looks moved already", "bold")))
    print(c("   still keyed on the old path: ", "dim") + c(stranded, "bold"))
    print(c("   claude-mv can finish the job: move nothing, re-key that "
            "history onto", "dim") + " " + c(dst, "cyan"))

    if args.dry_run:
        print(c("   (dry run — previewing what --already-moved would do)",
                "dim"))
        return True
    if args.force:
        return True
    if not can_prompt():
        # Not a refusal to work, a refusal to guess: on a pipe there is nobody
        # to ask, and re-keying history onto a path nobody confirmed is the
        # one thing this tool will not do quietly.
        # Repeats the finding rather than pointing at it: the report above
        # went to stdout, and a run redirected into a log is one where stderr
        # is the only half anybody reads.
        print(emsg(f"{c(src, 'cyan', stream=err)} is gone, "
                   f"{c(dst, 'cyan', stream=err)} is here, and "
                   f"{c(stranded, 'bold', stream=err)} are still keyed on the "
                   f"old path — confirmation needed.\n  Rerun from a tty, or "
                   f"say it outright:  claude-mv --already-moved "
                   f"{c(src, 'cyan', stream=err)} "
                   f"{c(dst, 'cyan', stream=err)}"), file=err)
        return False
    try:
        if input(c(f"re-key it onto {dst}? [y/N]: ", "bold")) \
                .strip().lower() in ("y", "yes"):
            return True
    except EOFError:
        pass
    print(c("cancelled — nothing was changed", "yellow"))
    return False


def print_conflicts(plans: list[dict]) -> None:
    print("\n" + wmsg(c("destination Claude history already exists:", "bold")))
    for plan in plans:
        for d, target in plan["dir_conflicts"]:
            n = len([x for x in os.listdir(target) if x.endswith(".jsonl")])
            print(f"  {c('projects/' + os.path.basename(target), 'yellow')} "
                  f"{c(f'({n} session file(s))', 'dim')}  "
                  f"{c('[' + plan['profile'] + ']', 'dim')}")
        for _, new_key in plan["key_conflicts"]:
            print(f"  config key {c(new_key, 'yellow')}  "
                  f"{c('[' + plan['cfg'] + ']', 'dim')}")


def can_prompt() -> bool:
    return sys.stdin.isatty() or bool(os.environ.get("CLAUDE_MV_FORCE_PROMPT"))


def ask_conflict_mode(already_moved: bool = False) -> str | None:
    """Interactive [o/c/r/a] prompt; None when stdin isn't a tty.

    `rename-only` is dropped with --already-moved: there is no mv to do on
    its own, so "leave the history untouched" is just abort.
    """
    if not can_prompt():
        return None
    print("\n" + c("How should the conflicting history be handled?", "bold"))
    print(f"  {c('[o]', 'bold', 'red')} overwrite   — replace it with the "
          f"moved project's history")
    print(" " * 20 + c("(discarded history survives in the kept restore "
                       "point)", "dim"))
    print(f"  {c('[c]', 'bold', 'green')} consolidate — merge: session files "
          f"combined, config entries merged")
    if not already_moved:
        print(f"  {c('[r]', 'bold', 'yellow')} rename only — do the plain mv, "
              f"leave Claude history untouched")
    print(f"  {c('[a]', 'dim')} abort       — do nothing")
    choices = {"o": "overwrite", "c": "consolidate", "a": "abort",
               "": "abort"}
    if not already_moved:
        choices["r"] = "rename-only"
    while True:
        try:
            ans = input(c("choice [a]: ", "bold")).strip().lower()
        except EOFError:
            return "abort"
        if ans in choices:
            return choices[ans]
        print(c(f"  ? '{ans}' — pick "
                f"{'o, c or a' if already_moved else 'o, c, r or a'}", "yellow"))


# ── the session picker ──────────────────────────────────────────────────────
# A resolver, nothing more: it turns the candidate list into the same list of
# ids `--session` takes, and everything downstream is identical either way.
# That is what keeps the mode scriptable, and what lets the tests drive the
# engine without going near a picker at all.
#
# fzf is soft, like it is in ccfind. The numbered fallback is not a consolation
# prize — it is the path the suite exercises, since a pipe can drive it and CI
# has no fzf.

def session_label(row: dict, home_width: int = 0) -> str:
    """One picker row: when it ran, its id, where it lives, how it opened.

    The "where" column appears only in a recursive scope, where the rows come
    from more than one folder and picking one without seeing which folder it
    is pulled out of would be picking blind. A flat list has no such column
    and reads exactly as it always did.

    With a search, the opening line still leads — it is what identifies the
    conversation — and the matching line follows it, when the match is not
    already visible in the opening line.
    """
    cells = [time.strftime("%Y-%m-%d %H:%M", time.localtime(row["mtime"])),
             row["id"][:8]]
    if home_width:
        cells.append((row.get("where") or "").ljust(home_width))
    cells.append(row["snippet"])
    label = "  ".join(cells)
    return label + "   ↦ " + row["match"] if row.get("match") else label


def where_col(home: str, src: str) -> str:
    """A session's home folder as a picker column: relative to the root.

    "./" for the root itself and "./sub/" for anything below it, so the column
    reads as a tree rather than as three repetitions of the same long prefix.
    Anything else — a session whose recorded cwd could not be read, so its
    home is only known to the directory it was filed in — keeps its absolute
    path rather than being drawn as something it is not.
    """
    if home == src or not home:
        return "./"
    if under(home, src):
        return "./" + os.path.relpath(home, src) + "/"
    return home


def session_labels(rows: list[dict]) -> list[str]:
    """Every picker row, aligned against each other.

    Both pickers go through this, so the fzf list and the numbered list stay
    the same list — the columns line up in one because they line up in both.
    """
    home_width = max((len(r.get("where") or "") for r in rows), default=0)
    return [session_label(r, home_width) for r in rows]


def fzf_layout(nrows: int) -> list[str]:
    """Make fzf behave like part of this command's output, not a takeover.

    Without --height fzf switches to the alternate screen: the command you just
    typed and everything above it disappear for the duration and come back
    after, which for picking one row out of a handful is a lot of screen to
    borrow. Sizing it to the list keeps the picker inline, under the report
    that introduced it. A line count rather than a percentage, because the
    right size here is "as tall as the list", and because the `~` auto-size
    form needs a newer fzf than the plain integer does — and an unknown flag
    would exit non-zero, which this code cannot tell apart from "cancelled".

    --reverse for the same reason: every other line claude-mv prints reads
    downward, and fzf's default layout builds upward from the bottom.

    Matches ccfind's `--reverse --height=80%` in spirit; the family should not
    disagree about which way its pickers run.
    """
    # rows + prompt + count + header, capped so a long list still leaves the
    # context above it on screen.
    return ["--reverse", f"--height={min(nrows + 3, 20)}"]


def pick_with_fzf(rows: list[dict]) -> list[dict] | None:
    """Multi-select through fzf. None when fzf can't be used at all."""
    fzf = shutil.which("fzf")
    if not fzf:
        return None
    # Index-prefixed so the selection maps back to a row exactly, rather than
    # by matching the label text back — snippets can repeat, ids cannot.
    menu = "\n".join(f"{i}\t{label}"
                     for i, label in enumerate(session_labels(rows)))
    try:
        r = subprocess.run(
            [fzf, "--multi", "--with-nth=2..", "--delimiter=\t",
             *fzf_layout(len(rows)),
             "--prompt=session(s) to move > ",
             "--header=Tab marks · Enter confirms · Esc cancels"],
            input=menu, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:          # 1 = no match, 130 = Esc/^C
        return []
    picked = []
    for line in r.stdout.splitlines():
        try:
            picked.append(rows[int(line.split("\t", 1)[0])])
        except (ValueError, IndexError):
            continue
    return picked


def parse_selection(answer: str, n: int) -> list[int] | None:
    """`1,3`, `2-4`, `all` → zero-based indices. None when it doesn't parse.

    Deliberately strict: a selection that is half-understood would move the
    wrong session's history, and there is always another prompt to be had.
    """
    answer = answer.strip().lower()
    if not answer:
        return None
    if answer == "all":
        return list(range(n))
    out = []
    for part in answer.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            lo, _, hi = part.partition("-")
            if not (lo.isdigit() and hi.isdigit()):
                return None
            lo, hi = int(lo), int(hi)
            if not (1 <= lo <= hi <= n):
                return None
            out.extend(range(lo - 1, hi))
        else:
            if not part.isdigit() or not 1 <= int(part) <= n:
                return None
            out.append(int(part) - 1)
    # dedupe, keep the order typed
    return list(dict.fromkeys(out)) or None


def pick_numbered(rows: list[dict]) -> list[dict]:
    """The no-fzf path: a numbered list and one prompt."""
    print("\n" + c("sessions available to move:", "bold"))
    width = max(len(str(len(rows))), 2)
    for i, label in enumerate(session_labels(rows), 1):
        print(f"  {c(str(i).rjust(width), 'bold')}  {label}")
    print(c("  pick one or more: 1 · 1,3 · 2-4 · all · empty to cancel", "dim"))
    while True:
        try:
            answer = input(c("selection: ", "bold"))
        except EOFError:
            return []
        if not answer.strip():
            return []
        picked = parse_selection(answer, len(rows))
        if picked is not None:
            return [rows[i] for i in picked]
        print(c(f"  ? '{answer.strip()}' — pick numbers between 1 and "
                f"{len(rows)}", "yellow"))


def fzf_wanted(mode: str) -> bool:
    """Whether to reach for fzf at all.

    fzf draws a full-screen UI and reads the keyboard, so launching it with no
    terminal attached leaves it waiting on input that can never arrive — a
    hang, not an error. In `auto` it therefore needs a tty; `fzf` forces it
    anyway, which is what lets a stubbed fzf be tested through a pipe.
    """
    return mode == "fzf" or (mode != "plain" and can_prompt())


def pick_sessions(rows: list[dict]) -> list[dict]:
    """Choose from the candidates, however this machine is equipped."""
    mode = (os.environ.get("CLAUDE_MV_PICKER") or "auto").strip().lower()
    if fzf_wanted(mode):
        picked = pick_with_fzf(rows)
        if picked is not None:
            return picked
        if mode == "fzf":
            print(emsg("CLAUDE_MV_PICKER=fzf, but fzf is not installed"),
                  file=sys.stderr)
            return []
    if not can_prompt():
        print(emsg("no way to choose a session — stdin is not a tty and fzf "
                   "is not installed; pass --session <id> instead"),
              file=sys.stderr)
        return []
    return pick_numbered(rows)


# ── the folder selector ─────────────────────────────────────────────────────
# --extract asks for two directories, and both are easy to get subtly wrong by
# typing: the source is the folder a conversation was BORN in (not where it
# ended up), and the destination is a real folder that already exists. So both
# are offered as a pick rather than a spelling, starting from somewhere
# sensible — the cwd for the source, the given path for the destination.
#
# Each row carries how many sessions that directory has, which is what turns a
# guess into a choice: on the source it shows where the history actually is,
# and on the destination it warns that something is already there.

def session_count(profiles: list[str], path: str,
                  recursive: bool = False) -> int:
    """Sessions homed in `path`, across every profile.

    Cheap enough to run per row: enc() is a pure string transform, so this is
    one isdir() and one listdir() per profile, no scanning.

    A recursive run counts the tree instead, because that is the number that
    row is offering. Costlier — one listdir of the projects root per profile,
    plus a read of the dirs whose names share the prefix — and still bounded
    by the number of project dirs, not by the size of any transcript.

    Both are counts of files, not of confirmed sessions, so an encoded dir
    that mixes a nested project with an unrelated folder of the same encoding
    is counted whole and the row can read one or two high. Deliberate: this
    runs per row of a directory browser, the number is a signpost for "the
    history is over here", and the next screen lists the sessions themselves.
    """
    n = 0
    for profile in profiles:
        root = os.path.join(profile, "projects")
        dirs = ([d for d, _ in find_project_dirs(root, path)] if recursive
                else [os.path.join(root, enc(path))])
        for d in dirs:
            try:
                n += sum(1 for x in os.listdir(d) if x.endswith(".jsonl"))
            except OSError:
                continue
    return n


def dir_rows(current: str, profiles: list[str],
             recursive: bool = False) -> list[tuple[str, str]]:
    """(payload, display) for the navigator at `current`."""
    def note(path):
        n = session_count(profiles, path, recursive)
        return f"  ({n} session{'' if n == 1 else 's'})" if n else ""

    rows = [(current, f"·  use this directory{note(current)}")]
    parent = os.path.dirname(current)
    if parent and parent != current:
        rows.append((parent, f"↑  {parent}"))
    try:
        names = sorted(n for n in os.listdir(current)
                       if os.path.isdir(os.path.join(current, n))
                       and n not in SKIP_DIRS)
    except OSError:
        names = []
    for name in names:
        full = os.path.join(current, name)
        rows.append((full, f"   {name}/{note(full)}"))
    return rows


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".next", "dist", "build", ".DS_Store"}


def pick_dir_with_fzf(start: str, title: str, profiles: list[str],
                      recursive: bool = False) -> str | None:
    """Navigate to a directory. None when fzf is unusable, "" when cancelled."""
    fzf = shutil.which("fzf")
    if not fzf:
        return None
    current = start
    while True:
        rows = dir_rows(current, profiles, recursive)
        menu = "\n".join(f"{i}\t{d}" for i, (_, d) in enumerate(rows))
        try:
            r = subprocess.run(
                [fzf, "--with-nth=2..", "--delimiter=\t",
                 *fzf_layout(len(rows)),
                 f"--prompt={os.path.basename(current) or '/'} > ",
                 f"--header={title} — {current}"],
                input=menu, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:          # Esc / ^C
            return ""
        try:
            chosen = rows[int(r.stdout.split("\t", 1)[0])][0]
        except (ValueError, IndexError):
            return ""
        if chosen == current:          # the "use this directory" row
            return current
        current = chosen


def make_dir_completer():
    """A readline completer offering directories only.

    Split out of the prompt so it can be tested: readline drives it from a
    terminal, which a piped suite has none of, and tab completion is the whole
    reason the no-fzf prompt is usable rather than a bare path to type.
    """
    def complete(text, state):
        path = os.path.expanduser(text)
        base = path if path.endswith(os.sep) else os.path.dirname(path)
        frag = "" if path.endswith(os.sep) else os.path.basename(path)
        try:
            names = sorted(n for n in os.listdir(base or ".")
                           if n.startswith(frag)
                           and os.path.isdir(os.path.join(base or ".", n)))
        except OSError:
            return None
        hits = [os.path.join(base, n) + os.sep for n in names]
        return hits[state] if state < len(hits) else None
    return complete


def readline_dir_prompt(start: str, title: str) -> str:
    """Type a path, with tab completion and `start` as the default.

    readline is stdlib but not guaranteed present, and macOS ships the libedit
    build, which spells its binding differently — hence both bindings and the
    quiet give-up. Without it this is still a plain prompt that works, just
    without tab completing.
    """
    try:
        import readline
    except ImportError:
        readline = None
    if readline is not None:
        readline.set_completer(make_dir_completer())
        readline.set_completer_delims(" \t\n")
        for binding in ("bind ^I rl_complete", "tab: complete"):
            try:
                readline.parse_and_bind(binding)
            except Exception:  # noqa: BLE001 — binding syntax varies by build
                pass
    print("\n" + c(title, "bold"))
    print(c(f"  Tab completes · Enter accepts {start}", "dim"))
    while True:
        try:
            answer = input(c("directory: ", "bold")).strip()
        except EOFError:
            return ""
        path = canonical(answer) if answer else start
        if os.path.isdir(path):
            return path
        print(c(f"  ? {path} is not a directory", "yellow"))


def pick_dir(start: str, title: str, profiles: list[str],
             recursive: bool = False) -> str:
    """Choose a directory, however this machine is equipped. "" = cancelled."""
    mode = (os.environ.get("CLAUDE_MV_PICKER") or "auto").strip().lower()
    if fzf_wanted(mode):
        picked = pick_dir_with_fzf(start, title, profiles, recursive)
        if picked is not None:
            return picked
        if mode == "fzf":
            print(emsg("CLAUDE_MV_PICKER=fzf, but fzf is not installed"),
                  file=sys.stderr)
            return ""
    if not can_prompt():
        print(emsg("nothing to choose a directory with — stdin is not a tty "
                   "and fzf is not installed; pass the paths as arguments "
                   "with --no-browse"), file=sys.stderr)
        return ""
    return readline_dir_prompt(start, title)


# ── restore points ──────────────────────────────────────────────────────────

def create_restore_point(stamp: str, src: str, dst: str, mode: str,
                         plans: list[dict], moved: bool) -> str | None:
    """Copy every path the migration will touch into a restore-point dir.

    `moved` records whether this run also moves the folder — false for
    --already-moved, so a later --restore knows not to move it back.

    Returns the restore-point path, or None when there is nothing to save.
    """
    created = []   # brand-new paths the migration creates (for restore rm)
    to_save = []   # (path, is_dir) collected first so an empty run makes no dir

    for plan in plans:
        for d, target in plan["dir_moves"]:
            to_save.append((d, True))
            created.append(target)
        for d, target in plan["dir_conflicts"]:
            to_save.append((d, True))
            to_save.append((target, True))
        if plan["cfg"] and (plan["key_moves"] or plan["key_conflicts"]):
            to_save.append((plan["cfg"], False))
        if plan["hist"]:
            to_save.append((plan["hist"], False))
    return write_restore_point(stamp, to_save, created,
                               {"src": src, "dst": dst, "mode": mode,
                                "moved": moved})


def write_restore_point(stamp: str, to_save: list, created: list,
                        extra: dict) -> str | None:
    """Snapshot `to_save` under a fresh stamp dir and write its manifest.

    Shared by the folder move and the session move, which differ only in what
    they collect: the manifest is the contract --restore reads back, so both
    shapes must be written by the same code or one of them drifts untested.
    """
    entries = []   # {"type": "dir"|"file", "original": path, "copy": rel}
    if not to_save:
        return None

    rp = os.path.join(RESTORE_ROOT, stamp)
    n = 2
    while os.path.exists(rp):  # same-second rerun
        rp = os.path.join(RESTORE_ROOT, f"{stamp}-{n}")
        n += 1
    for i, (path, is_dir) in enumerate(to_save):
        rel = os.path.join("files", str(i))
        copy = os.path.join(rp, rel)
        if is_dir:
            shutil.copytree(path, copy, symlinks=True)
        else:
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copy2(path, copy)
        entries.append({"type": "dir" if is_dir else "file",
                        "original": path, "copy": rel})
    manifest = {"stamp": os.path.basename(rp), "entries": entries,
                "created": created, **extra}
    with open(os.path.join(rp, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return rp


def list_restore_points() -> list[str]:
    if not os.path.isdir(RESTORE_ROOT):
        return []
    return sorted(n for n in os.listdir(RESTORE_ROOT)
                  if os.path.isfile(os.path.join(RESTORE_ROOT, n, "manifest.json")))


def cmd_restore(arg: str, force: bool) -> int:
    stamps = list_restore_points()
    if not stamps:
        print(emsg("no restore points"), file=sys.stderr)
        return 1

    if arg == "list":
        print(f"restore points in {c(RESTORE_ROOT, 'cyan')}:")
        for stamp in stamps:
            with open(os.path.join(RESTORE_ROOT, stamp, "manifest.json"),
                      encoding="utf-8") as f:
                m = json.load(f)
            print(f"  {c(stamp, 'bold')}  {c('[' + m['mode'] + ']', 'dim')}"
                  f"  {c(m['src'], 'cyan')} {c('→', 'dim')} "
                  f"{c(m['dst'], 'cyan')}")
        print(c("restore one with: claude-mv --restore <stamp|latest>", "dim"))
        return 0

    stamp = stamps[-1] if arg == "latest" else arg
    rp = os.path.join(RESTORE_ROOT, stamp)
    manifest_path = os.path.join(rp, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(emsg(f"no restore point '{stamp}' "
                   f"(see claude-mv --restore)"), file=sys.stderr)
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)
    src, dst = m["src"], m["dst"]
    originals = {e["original"] for e in m["entries"]}

    # Pre-`moved` restore points always came from a real move.
    was_move = m.get("moved", True)
    sessions = m.get("sessions") or []
    if m.get("kind") == "sessions":
        undoing = (f"the re-homing of {c(str(len(sessions)), 'bold')} "
                   f"session{'' if len(sessions) == 1 else 's'} ")
    else:
        undoing = "" if was_move else "the history re-key "
    print(f"restore point {c(stamp, 'bold')} "
          f"{c('[' + m['mode'] + ']', 'dim')} — will undo {undoing}"
          f"{c(src, 'cyan')} {c('→', 'dim')} {c(dst, 'cyan')}:")
    move_back = was_move and os.path.isdir(dst) and not os.path.exists(src)
    if move_back:
        print(f"  mv {c(dst, 'cyan')} {c('→', 'dim')} {c(src, 'cyan')}")
    elif m.get("kind") == "sessions":
        print(c(f"  (--extract run: no folder was moved, "
                f"{dst} stays put)", "dim"))
    elif not was_move:
        print(c(f"  (--already-moved run: no folder move to undo, "
                f"{dst} stays put)", "dim"))
    elif os.path.exists(src):
        print("  " + wmsg(f"{c(src, 'cyan')} already exists — folder move-back "
                          f"will be skipped"))
    else:
        print("  " + wmsg(f"{c(dst, 'cyan')} not found — folder move-back "
                          f"will be skipped"))
    # NB: `p`, not `c` — `c` is the colour helper, and shadowing it here would
    # break every coloured line below.
    doomed = [p for p in m["created"] if p not in originals and os.path.exists(p)]
    for p in doomed:
        print(f"  remove {c(p, 'cyan')}")
    n_dirs = sum(1 for e in m["entries"] if e["type"] == "dir")
    n_files = len(m["entries"]) - n_dirs
    print(f"  restore {c(str(n_dirs), 'bold')} project dir(s) + "
          f"{c(str(n_files), 'bold')} file(s) to their pre-move state")

    if not force:
        if not can_prompt():
            print(emsg("confirmation needed — rerun with --force or "
                       "from a tty"), file=sys.stderr)
            return 2
        try:
            if input(c("restore? [y/N]: ", "bold")).strip().lower() \
                    not in ("y", "yes"):
                print(c("aborted — nothing was changed", "yellow"))
                return 1
        except EOFError:
            return 1

    if move_back:
        try:
            os.rename(dst, src)
        except OSError:
            shutil.move(dst, src)
    for p in doomed:
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    for e in m["entries"]:
        copy = os.path.join(rp, e["copy"])
        if e["type"] == "dir":
            if os.path.isdir(e["original"]):
                shutil.rmtree(e["original"])
            shutil.copytree(copy, e["original"], symlinks=True)
        else:
            shutil.copy2(copy, e["original"] + ".claude-mv-tmp")
            os.replace(e["original"] + ".claude-mv-tmp", e["original"])
    shutil.rmtree(rp)
    print(c("✅ restored", "green", "bold") +
          f" — state is back to before the move; restore point "
          f"{c(stamp, 'bold')} removed")
    return 0


# ── migration ───────────────────────────────────────────────────────────────

def overwrite_backup_wanted() -> bool:
    """Whether a successful overwrite keeps its restore point.

    On by default — it is the only archive of the history the overwrite threw
    away, and so the only thing that makes that mode undoable. Opt out with
    CLAUDE_MV_OVERWRITE_BACKUP=0.
    """
    return (os.environ.get("CLAUDE_MV_OVERWRITE_BACKUP") or "1") \
        .strip().lower() not in ("0", "false", "no", "off")


def new_tally() -> dict:
    """Counters the appliers add to, so the run can close with one line of
    totals rather than leaving the reader to add up the per-profile sections.
    Summed across profiles: a two-profile move reports both."""
    return {"dirs": 0, "sessions": 0, "sidecars": 0, "keys": 0, "history": 0}


def summarize(t: dict) -> str:
    """The tally as a single phrase, dropping whatever is zero. Empty string
    when nothing at all was touched — callers then print no tally rather than
    a row of zeroes.

    Pluralised properly rather than with the terse "(s)" the report rows use.
    Those rows are a repeated column of counts; this is one sentence a reader
    actually reads, and a single-project move — much the commonest case —
    would otherwise open with "1 project dir(s)".
    """
    def n(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    bits = []
    if t["dirs"]:
        bits.append(n(t["dirs"], "project dir", "project dirs"))
    if t["sessions"]:
        bits.append(n(t["sessions"], "session file", "session files"))
    # .get: the folder path's tally predates this counter, and a caller that
    # builds a tally dict by hand should not have to know about a store its
    # own code path can never touch.
    if t.get("sidecars"):
        bits.append(n(t["sidecars"], "sidecar dir", "sidecar dirs"))
    if t["keys"]:
        bits.append(n(t["keys"], "config key", "config keys"))
    if t["history"]:
        bits.append(n(t["history"], "history entry", "history entries"))
    return " · ".join(bits)


def apply_dir_move(d: str, target: str, old: str, new: str,
                   mode: str, dry_run: bool, tally: dict) -> None:
    tag = c("would", "yellow") if dry_run else c("did", "green")
    conflict = os.path.exists(target)

    if conflict and mode == "overwrite":
        print(f"  {tag} discard "
              f"{c('projects/' + os.path.basename(target), 'red')} "
              f"{c('(copy kept in restore point)', 'dim')}")
        if not dry_run:
            shutil.rmtree(target)
        conflict = False

    if not conflict:
        print(f"  {tag} rename "
              f"{c('projects/' + os.path.basename(d), 'cyan')}")
        print(f"          {c('→', 'dim')} "
              f"{c('projects/' + os.path.basename(target), 'cyan', 'bold')}")
        if not dry_run:
            os.rename(d, target)
    else:  # consolidate
        print(f"  {tag} merge "
              f"{c('projects/' + os.path.basename(d), 'cyan')}")
        print(f"          into "
              f"{c('projects/' + os.path.basename(target), 'cyan', 'bold')}")
        if not dry_run:
            for name in sorted(os.listdir(d)):
                s, t = os.path.join(d, name), os.path.join(target, name)
                if os.path.exists(t):
                    print("  " + wmsg(f"keep both: {name} exists in "
                                      f"destination — source copy left in "
                                      f"place", stream=sys.stderr),
                          file=sys.stderr)
                    continue
                os.rename(s, t)
            try:
                os.rmdir(d)
            except OSError:
                print("  " + wmsg(f"{os.path.basename(d)} not empty after "
                                  f"merge — left in place",
                                  stream=sys.stderr), file=sys.stderr)

    live_dir = d if dry_run else target
    n_files = n_lines = 0
    if os.path.isdir(live_dir):
        for name in sorted(os.listdir(live_dir)):
            if not name.endswith(".jsonl"):
                continue
            n = rewrite_jsonl_field(os.path.join(live_dir, name), "cwd",
                                    old, new, dry_run)
            if n:
                n_files += 1
                n_lines += n
    if n_files:
        print(f"  {tag} rewrite cwd in {c(str(n_files), 'bold')} session "
              f"file(s) {c(f'({n_lines} lines)', 'dim')}")
    tally["dirs"] += 1
    tally["sessions"] += n_files


def apply_plan(plan: dict, old: str, new: str, mode: str,
               dry_run: bool, tally: dict) -> None:
    tag = c("would", "yellow") if dry_run else c("did", "green")
    print("\n" + c("──", "dim") + " " + c("profile", "dim") + " " +
          c(plan["profile"], "bold"))

    for d, target in plan["dir_moves"] + plan["dir_conflicts"]:
        apply_dir_move(d, target, old, new, mode, dry_run, tally)

    cfg = plan["cfg"]
    if cfg and (plan["key_moves"] or plan["key_conflicts"]):
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        projects = data.get("projects", {})
        for key, new_key in plan["key_moves"]:
            print(f"  {tag} re-key {c(os.path.basename(cfg), 'bold')}: "
                  f"{c(key, 'dim')} {c('→', 'dim')} {c(new_key, 'cyan')}")
            if not dry_run and key in projects and new_key not in projects:
                projects[new_key] = projects.pop(key)
        for key, new_key in plan["key_conflicts"]:
            verb = "replace" if mode == "overwrite" else "merge into"
            note = f"({'from' if mode == 'overwrite' else 'with'} {key})"
            print(f"  {tag} {verb} config key {c(new_key, 'cyan')} "
                  f"{c(note, 'dim')}")
            if not dry_run and key in projects:
                src = projects.pop(key)
                if mode == "overwrite":
                    projects[new_key] = src
                else:
                    projects[new_key] = merge_entries(src, projects.get(new_key, {}))
        tally["keys"] += len(plan["key_moves"]) + len(plan["key_conflicts"])
        if not dry_run:
            atomic_write(cfg, json.dumps(data, ensure_ascii=False, indent=2))

    if plan["hist"]:
        n = rewrite_jsonl_field(plan["hist"], "project", old, new, dry_run)
        if n:
            print(f"  {tag} rewrite project in "
                  f"{c('history.jsonl', 'bold')} "
                  f"{c(f'({n} entries)', 'dim')}")
            tally["history"] += n


# ── session migration ───────────────────────────────────────────────────────
# Moving sessions is a different operation from moving a folder, not a
# narrower one, and the stores behave differently enough that it gets its own
# plan/apply pair rather than a flag threaded through the folder path:
#
#   projects/<enc>/   source dir STAYS; <id>.jsonl and its <id>/ sidecar move
#                     into projects/<enc(dst)>/, which is created if needed
#   session cwd       NOT rewritten — see rehome_session()
#   config projects   NOT touched — the source project still exists, and
#                     fabricating a destination entry would transplant its
#                     trust flag and allowedTools onto a path the user never
#                     approved. Claude writes the entry on first run there.
#   history.jsonl     only the entries carrying a moved sessionId
#
# Everything else under a profile (todos/, file-history/, session-env/,
# plans/, tasks/) is keyed by session id alone, so it follows for free — the
# sidecar dir is the one session-keyed store that lives INSIDE the project
# dir and therefore has to be carried by hand.

SESSION_CONFLICT_MODES = ("overwrite", "skip", "abort")


def build_session_plan(profile: str, rows: list[dict], dst: str) -> dict:
    """What this profile has to do for the sessions chosen from it."""
    target_dir = os.path.join(profile, "projects", enc(dst))
    plan = {"profile": profile, "target_dir": target_dir, "moves": [],
            "conflicts": [], "cfg": config_json_path(profile), "hist": None,
            "ids": [r["id"] for r in rows]}
    for row in rows:
        sidecar = row["path"][:-len(".jsonl")]
        item = {"id": row["id"], "jsonl": row["path"],
                "sidecar": sidecar if os.path.isdir(sidecar) else None,
                "target": os.path.join(target_dir, row["id"] + ".jsonl"),
                "target_sidecar": os.path.join(target_dir, row["id"])}
        # Either half of the destination counts as occupied. A leftover
        # sidecar with no transcript beside it is odd but real (an interrupted
        # run, a half-deleted session), and treating it as a clean move would
        # break the promise the whole up-front detection makes: the transcript
        # would land, then the sidecar rename would fail onto the existing
        # directory, stopping halfway. It would also silently attach one
        # conversation's subagent transcripts to another's.
        bucket = ("conflicts"
                  if os.path.exists(item["target"])
                  or os.path.isdir(item["target_sidecar"])
                  else "moves")
        plan[bucket].append(item)
    hist = os.path.join(profile, "history.jsonl")
    if os.path.isfile(hist):
        plan["hist"] = hist
    return plan


def rehome_session(item: dict, target_dir: str, mode: str, dry_run: bool,
                   tally: dict) -> None:
    """Relocate one session's transcript and sidecar into the target dir.

    The recorded `cwd` lines are deliberately left as they are. Nothing moved
    on disk — the session really did start where it says — so rewriting them
    would falsify the record, exactly as the tool already declines to rewrite
    paths inside message content. It would also corrupt this shape outright:
    a session is normally re-homed INTO a subdirectory of where it started,
    so a prefix remap of old→new would hit the lines already naming the
    destination a second time (…/recovery → …/recovery/recovery).
    """
    tag = c("would", "yellow") if dry_run else c("did", "green")
    short = c(item["id"][:8], "bold")

    if os.path.exists(item["target"]) or os.path.isdir(item["target_sidecar"]):
        if mode != "overwrite":
            print(f"  {tag} skip {short} "
                  f"{c('(already present at the destination)', 'dim')}")
            return
        print(f"  {tag} replace {short} "
              f"{c('(copy kept in restore point)', 'red')}")
        if not dry_run:
            # Both halves go, so the replacement cannot inherit the previous
            # occupant's subagent transcripts.
            if os.path.exists(item["target"]):
                os.remove(item["target"])
            if os.path.isdir(item["target_sidecar"]):
                shutil.rmtree(item["target_sidecar"])
    else:
        print(f"  {tag} re-home {short} {c('→', 'dim')} "
              f"{c('projects/' + os.path.basename(target_dir), 'cyan', 'bold')}")

    if not dry_run:
        os.makedirs(target_dir, exist_ok=True)
        os.rename(item["jsonl"], item["target"])
    tally["sessions"] += 1

    if item["sidecar"]:
        print(f"       {c('+', 'dim')} sidecar "
              f"{c(os.path.basename(item['sidecar']) + '/', 'cyan')} "
              f"{c('(subagents, tool results)', 'dim')}")
        if not dry_run:
            os.rename(item["sidecar"], item["target_sidecar"])
        tally["sidecars"] = tally.get("sidecars", 0) + 1


def rewrite_history_sessions(path: str, ids: set, dst: str,
                             dry_run: bool) -> int:
    """Re-key history entries belonging to the moved sessions.

    Selected by sessionId rather than by path: the whole point is that the
    source project keeps its OTHER sessions' entries, which a prefix rewrite
    of `project` could not express.
    """
    changed = 0
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line.rstrip("\n"))
            except ValueError:
                out.append(line)
                continue
            if obj.get("sessionId") in ids and obj.get("project") != dst:
                obj["project"] = dst
                out.append(json.dumps(obj, ensure_ascii=False,
                                      separators=(",", ":")) + "\n")
                changed += 1
            else:
                out.append(line)
    if changed and not dry_run:
        atomic_write(path, "".join(out))
    return changed


def apply_session_plan(plan: dict, dst: str, mode: str, dry_run: bool,
                       tally: dict) -> None:
    tag = c("would", "yellow") if dry_run else c("did", "green")
    print("\n" + c("──", "dim") + " " + c("profile", "dim") + " " +
          c(plan["profile"], "bold"))

    for item in plan["moves"] + plan["conflicts"]:
        rehome_session(item, plan["target_dir"], mode, dry_run, tally)

    if plan["hist"]:
        moved_ids = {i["id"] for i in plan["moves"]}
        if mode == "overwrite":
            moved_ids |= {i["id"] for i in plan["conflicts"]}
        n = rewrite_history_sessions(plan["hist"], moved_ids, dst, dry_run)
        if n:
            print(f"  {tag} re-key {c(str(n), 'bold')} entr"
                  f"{'y' if n == 1 else 'ies'} in "
                  f"{c('history.jsonl', 'bold')} {c('→', 'dim')} "
                  f"{c(dst, 'cyan')}")
            tally["history"] += n


def create_session_restore_point(stamp: str, src: str, dst: str, mode: str,
                                 plans: list[dict]) -> str | None:
    to_save, created = [], []
    for plan in plans:
        for item in plan["moves"] + plan["conflicts"]:
            to_save.append((item["jsonl"], False))
            if item["sidecar"]:
                to_save.append((item["sidecar"], True))
            # Both halves of the destination are listed as created, so a
            # restore removes the relocated copy instead of leaving the
            # session sitting in two project dirs at once.
            created.append(item["target"])
            if item["sidecar"]:
                created.append(item["target_sidecar"])
        for item in plan["conflicts"]:
            # Whatever the destination already had, which an overwrite would
            # delete. Either half can be there on its own, so both are tested
            # rather than assumed. Saving them also puts them in `originals`,
            # which is what stops the restore from removing what it just put
            # back.
            if os.path.exists(item["target"]):
                to_save.append((item["target"], False))
            if os.path.isdir(item["target_sidecar"]):
                to_save.append((item["target_sidecar"], True))
        if plan["hist"]:
            to_save.append((plan["hist"], False))
    ids = sorted({i for plan in plans for i in plan["ids"]})
    return write_restore_point(stamp, to_save, created,
                               {"src": src, "dst": dst, "mode": mode,
                                "moved": False, "kind": "sessions",
                                "sessions": ids})


def check_live_ids(profiles: list[str], ids: set) -> list[str]:
    """Live Claude processes running one of the sessions about to be moved.

    Sharper than the folder guard's cwd test: the folder is not moving here,
    so what matters is whether a session's own transcript is being appended
    to while we relocate it.
    """
    live = []
    for profile in profiles:
        sess_dir = os.path.join(profile, "sessions")
        if not os.path.isdir(sess_dir):
            continue
        for name in sorted(os.listdir(sess_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(sess_dir, name), encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            sid, pid = obj.get("sessionId"), obj.get("pid")
            if not (sid in ids and pid):
                continue
            try:
                os.kill(int(pid), 0)
            except (OSError, ValueError):
                continue  # stale record, process gone
            live.append(f"pid {pid} running session {sid[:8]} ({profile})")
    return live


def resolve_extract_paths(args, profiles: list[str]):
    """Settle src and dst for --extract, asking where it is allowed to.

    Three steps, in the order a person works: which folder holds the history,
    which sessions, and where they should go. The first and last are folder
    pickers seeded with a sensible starting point — the cwd for the source,
    whatever was passed for the destination.

    Positional arguments are the seeds, not a bypass: passing a path starts
    its picker there rather than skipping it, so the guide stays one flow with
    fewer keystrokes rather than two different ones. --no-browse is the
    bypass, and then both paths are required, because the destination is the
    one thing this mode will not guess.

    Returns (src, dst), or (None, None) when the run was cancelled or refused.
    """
    src, dst = args.src, args.dst
    # One positional can only be the destination: src has a default and dst
    # never does, so `claude-mv --extract ~/code/newproj` is unambiguous.
    if dst is None and src is not None:
        src, dst = None, src

    if args.no_browse:
        # `not dst` is belt to the swap's braces: after it, a set src implies
        # a set dst, so this is currently equivalent to `if not src`. Kept
        # because it states the requirement rather than a consequence of the
        # line above — if the swap ever changes, this still says what it means.
        if not src or not dst:
            print(emsg("--no-browse needs both paths given: "
                       "claude-mv --extract --no-browse <src> <dst>"),
                  file=sys.stderr)
            return None, None
        return canonical(src), canonical(dst)

    src = pick_dir(canonical(src) if src else os.path.realpath(os.getcwd()),
                   "Which folder to search under?" if args.recursive
                   else "Which folder holds the sessions?",
                   profiles, args.recursive)
    if not src:
        print(c("cancelled — nothing was changed", "yellow"))
        return None, None
    return src, dst          # dst is settled after the sessions are chosen


def settle_destination(args, src: str, dst: str | None,
                       profiles: list[str]) -> str | None:
    """The last step of the guide: where the chosen sessions should land.

    Asked AFTER the sessions are picked, because that is the order the
    decision is actually made in — you know which conversation you are moving
    before you know where it belongs. A dst given on the command line seeds
    the picker rather than skipping it, so Enter confirms it.
    """
    if not args.no_browse:
        start = canonical(dst) if dst else src
        if not os.path.isdir(start):
            start = src          # a dst that does not exist yet cannot be a
        dst = pick_dir(start, "Where should they go?", profiles)  # start point
        if not dst:
            print(c("cancelled — nothing was changed", "yellow"))
            return None
    else:
        dst = canonical(dst)

    dst = os.path.realpath(dst) if os.path.isdir(dst) else dst
    if not os.path.isdir(dst):
        print(emsg(f"destination is not a directory: "
                   f"{c(dst, 'cyan', stream=sys.stderr)}\n  --extract re-keys "
                   f"history onto a folder that already exists; it moves "
                   f"nothing itself"), file=sys.stderr)
        return None
    if src == dst:
        print(emsg("source and destination are the same path — nothing to "
                   "re-key"), file=sys.stderr)
        return None
    return dst


BORN_HINT = ("a session started elsewhere and cd'd in is homed where it "
             "STARTED — try that path")


def explain_empty(src: str, profiles: list[str], query: str,
                  recursive: bool) -> str:
    """Why the candidate list came back empty, in the terms it was asked in.

    Three different mistakes end up here — nothing in this folder, nothing
    matching what you asked for, and everything one folder further down — and
    one message can only describe the first. Asking the question the other two
    ways costs another pass over a tree we have just established is small, and
    turns a dead end into the next thing to try.
    """
    err = sys.stderr
    scope = (("under " if recursive else "in ")
             + c(src, "cyan", stream=err))
    homed = find_sessions(src, profiles, 0, recursive=recursive) or []
    # Would a wider scope have answered? Only worth asking when we were not
    # already at the widest.
    wider = ([] if recursive
             else find_sessions(src, profiles, 0, query, True) or [])
    if query and homed:
        msg = emsg(f"none of the {c(str(len(homed)), 'bold', stream=err)} "
                   f"sessions homed {scope} mention "
                   f"{c(query, 'bold', stream=err)}")
    elif query:
        msg = emsg(f"no sessions are homed {scope}, so there is nothing to "
                   f"search")
    else:
        msg = emsg(f"no sessions are homed {scope}")
    if wider:
        n = len(wider)
        found = (f"{n} session{'' if n == 1 else 's'} below it"
                 + (f" {'mentions' if n == 1 else 'mention'} it" if query
                    else f" {'is' if n == 1 else 'are'} homed there"))
        return (msg + "\n  " + c(f"{found} — add ", "yellow", stream=err) +
                c("--recursive", "cyan", stream=err) +
                c(f" to include {'it' if n == 1 else 'them'}", "yellow",
                  stream=err))
    return msg + "\n  " + c(f"({BORN_HINT})", "dim", stream=err)


def confirm_session_move(src: str, dst: str, chosen: list[dict],
                         plans: list[dict], mode: str,
                         recursive: bool) -> bool:
    """The survey page: the whole decision restated, then one question.

    The guide asks three questions and then acts, and the thing being acted on
    — which conversation, out of which folder, into which other one — is
    exactly the thing that is easy to get one row wrong, especially now that a
    search can put rows from four different folders in one list. So the last
    screen before anything is written says it all back.

    Only where there is someone to answer: a run that could not have been
    asked a question is a run nobody is watching, and --force means the
    watching is over.
    """
    conflicted = {i["id"] for plan in plans for i in plan["conflicts"]}
    fate = "replaced" if mode == "overwrite" else "kept, this one skipped"
    print("\n" + c("about to re-home ", "bold") + c(str(len(chosen)), "bold") +
          f" session{'' if len(chosen) == 1 else 's'}:")
    print(c("  from  ", "dim") + c(src, "cyan") +
          c(" and below" if recursive else "", "dim"))
    print(c("  to    ", "dim") + c(dst, "cyan"))
    for row, label in zip(chosen, session_labels(chosen)):
        note = ("  " + c(f"(already at the destination — {fate})", "yellow")
                if row["id"] in conflicted else "")
        print("    " + label + note)
    print(c("  no folder is moved, and the transcripts keep the cwd they "
            "recorded", "dim"))
    print(c("  the destination's trust and tool permissions are left as "
            "Claude finds them", "dim"))
    try:
        return input(c("proceed? [y/N]: ", "bold")).strip().lower() in ("y",
                                                                       "yes")
    except EOFError:
        return False


def run_session_mode(args, src: str, dst: str | None,
                     profiles: list[str]) -> int:
    """--extract: move chosen sessions' history from `src` to `dst`."""
    query = " ".join((args.search or "").split())
    # --limit exists to keep the picker readable. Naming ids outright is not
    # the picker, and silently not finding a session because it sorted below
    # an arbitrary cutoff would be the worst kind of no-op.
    rows = find_sessions(src, profiles, 0 if args.session else args.limit,
                         query, args.recursive)
    if rows is None:
        return 1                      # the source already said why
    if not rows:
        print(explain_empty(src, profiles, query, args.recursive),
              file=sys.stderr)
        return 1
    if args.recursive:
        # One list, several home folders: the row has to say which, or the
        # pick is blind. Flat runs get no column at all.
        for row in rows:
            row["where"] = where_col(row.get("home") or src, src)

    if args.session:
        chosen, missing, ambiguous = [], [], []
        for want in args.session:
            hits = [r for r in rows if r["id"] == want
                    or r["id"].startswith(want)]
            if not hits:
                missing.append(want)
            elif len(hits) > 1:
                # Two ids sharing a prefix is unlikely and entirely possible.
                # Guessing which was meant would re-home the wrong
                # conversation, so say so and let the user be specific.
                ambiguous.append((want, [h["id"] for h in hits]))
            else:
                chosen.append(hits[0])
        if missing:
            print(emsg(f"not homed in {c(src, 'cyan', stream=sys.stderr)}: "
                       f"{', '.join(sorted(missing))}"), file=sys.stderr)
            return 1
        if ambiguous:
            for want, hits in ambiguous:
                print(emsg(f"{c(want, 'bold', stream=sys.stderr)} matches "
                           f"{len(hits)} sessions:"), file=sys.stderr)
                for hit in hits:
                    print(f"  {c(hit, 'yellow', stream=sys.stderr)}",
                          file=sys.stderr)
            return 1
        # dedupe: two spellings of one session select it once
        chosen = list({r["id"]: r for r in chosen}.values())
    else:
        print(c("sessions homed " + ("under " if args.recursive else "in "),
                "bold") + c(src, "cyan") +
              (c(" mentioning ", "bold") + c(query, "bold") if query else "") +
              c(f" ({len(rows)} found)", "dim"))
        chosen = pick_sessions(rows)
        if not chosen:
            print(c("nothing selected — nothing was changed", "yellow"))
            return 1

    # Step three: now that the sessions are known, where they go.
    dst = settle_destination(args, src, dst, profiles)
    if dst is None:
        return 1

    ids = {r["id"] for r in chosen}
    live = check_live_ids(profiles, ids)
    if live and not args.force:
        print(emsg("live Claude session(s) among the ones selected — close "
                   "them or use --force:"), file=sys.stderr)
        for entry in live:
            print(f"  {c(entry, 'yellow', stream=sys.stderr)}",
                  file=sys.stderr)
        return 1

    by_profile = {}
    for row in chosen:
        by_profile.setdefault(row["profile"], []).append(row)
    plans = [build_session_plan(p, rs, dst) for p, rs in by_profile.items()]

    mode = args.on_conflict or "skip"
    if any(plan["conflicts"] for plan in plans):
        print("\n" + wmsg(c("already present at the destination:", "bold")))
        for plan in plans:
            for item in plan["conflicts"]:
                print(f"  {c(item['id'][:8], 'yellow')}  "
                      f"{c('[' + plan['profile'] + ']', 'dim')}")
        if args.on_conflict is None:
            print(c(f"  keeping the destination's copy "
                    f"(--on-conflict overwrite to replace it)", "dim"))

    if not (args.dry_run or args.force) and can_prompt():
        if not confirm_session_move(src, dst, chosen, plans, mode,
                                    args.recursive):
            print(c("cancelled — nothing was changed", "yellow"))
            return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    rp = None
    if args.dry_run:
        print(f"would create restore point "
              f"{c(os.path.join(RESTORE_ROOT, stamp), 'dim')}")
    else:
        rp = create_session_restore_point(stamp, src, dst, mode, plans)
        if rp:
            print(c("restore point:", "dim") + " " + c(rp, "dim"))

    print(c("would re-home" if args.dry_run else "re-homing", "bold") + " " +
          c(str(len(chosen)), "bold") +
          f" session{'' if len(chosen) == 1 else 's'} "
          f"{c(src, 'cyan')} {c('→', 'dim')} {c(dst, 'cyan')} " +
          c("(no folder is moved)", "dim"))

    tally = new_tally()
    try:
        for plan in plans:
            apply_session_plan(plan, dst, mode, args.dry_run, tally)
    except Exception as e:  # noqa: BLE001 — anything mid-migration
        print("\n" + c("❌", "red", "bold", stream=sys.stderr) + " " +
              emsg(f"migration failed midway: {e}"), file=sys.stderr)
        if rp:
            print(f"   roll everything back with:  claude-mv --restore "
                  f"{c(os.path.basename(rp), 'bold', stream=sys.stderr)}",
                  file=sys.stderr)
        return 3

    summary = summarize(tally)
    if args.dry_run:
        print("\n" + (c("would migrate", "bold") + " " + summary + "\n"
                      if summary else "") +
              c("(dry run — nothing was changed)", "dim"))
        return 0

    # A destination Claude has never run in has no config entry yet. Saying so
    # is worth a line: the sessions resume fine without one, but the silence
    # would otherwise look like a store the tool forgot.
    cfg_note = ""
    for plan in plans:
        if not os.path.isfile(plan["cfg"]):
            continue
        try:
            with open(plan["cfg"], encoding="utf-8") as f:
                projects = json.load(f).get("projects") or {}
        except (OSError, ValueError):
            continue
        if dst not in projects:
            cfg_note = (c("   no config entry for the destination yet — "
                          "Claude writes one on first run there\n"
                          "   (trust and tool permissions are deliberately "
                          "not copied across)\n", "dim"))
            break

    if rp:
        if (mode == "overwrite" and any(p["conflicts"] for p in plans)
                and overwrite_backup_wanted()):
            print("\n" + c("✅ done", "green", "bold") +
                  (f" — {summary}" if summary else "") + "\n" + cfg_note +
                  c("   replaced destination sessions are kept in the restore "
                    "point:", "dim") + f"\n   {c(rp, 'cyan')}\n" +
                  c("   undo everything:  ", "dim") +
                  f"claude-mv --restore {os.path.basename(rp)}\n" +
                  c("   discard for good: ", "dim") + f"rm -rf {rp}")
            return 0
        shutil.rmtree(rp)
    print("\n" + c("✅ done", "green", "bold") +
          (f" — {summary}" if summary else "") + "\n" + cfg_note +
          c(f"   `claude --resume` in {dst} will now find "
            f"{'it' if len(chosen) == 1 else 'them'} "
            f"(restore point cleaned up)", "dim"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="claude-mv",
        description="mv a directory and migrate Claude Code history with it")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="show what would happen without changing anything")
    ap.add_argument("--force", action="store_true",
                    help="skip the live-session guard, the --extract survey "
                         "page and the restore confirmation")
    ap.add_argument("--already-moved", action="store_true",
                    help="the folder was already renamed by something else: "
                         "move nothing, just re-key the history stranded on "
                         "<src-dir> onto <dst> (src must be gone, dst must "
                         "exist)")
    ap.add_argument("--on-conflict",
                    choices=sorted(set(CONFLICT_MODES) |
                                   set(SESSION_CONFLICT_MODES)),
                    help="policy when destination history already exists "
                         "(default: ask on a tty, abort otherwise; with "
                         "--extract: overwrite/skip/abort, default skip)")
    ap.add_argument("--no-browse", action="store_true",
                    help="with --extract: don't browse for the folders — take "
                         "<src> and <dst> as arguments instead (both then "
                         "required). Choosing the sessions is unaffected; add "
                         "--session to skip that too")
    ap.add_argument("--extract", action="store_true",
                    help="move individual SESSIONS out of <src>'s history "
                         "onto <dst> instead of moving a folder — for when a "
                         "project was born mid-session in a parent directory")
    ap.add_argument("--session", action="append", default=[], metavar="ID",
                    help="session id to move (repeatable); skips the picker. "
                         "An 8-character prefix is enough")
    ap.add_argument("--search", metavar="TEXT",
                    help="with --extract: offer only the sessions whose "
                         "transcript mentions TEXT — the whole conversation "
                         "is searched, not just its opening line. A literal, "
                         "case-insensitive substring")
    ap.add_argument("-R", "--recursive", action="store_true",
                    help="with --extract: consider the sessions homed in "
                         "<src> AND in every folder below it (default: that "
                         "one folder only)")
    ap.add_argument("--limit", type=int, default=50, metavar="N",
                    help="how many sessions to offer (default 50)")
    ap.add_argument("--restore", nargs="?", const="list", metavar="STAMP",
                    help="list restore points, or roll one back "
                         "(--restore <stamp|latest>)")
    ap.add_argument("--profile", action="append", default=[],
                    help="Claude profile dir to migrate (repeatable)")
    ap.add_argument("src", nargs="?",
                    help="directory to move (with --already-moved: the path "
                         "it used to live at)")
    ap.add_argument("dst", nargs="?", help="destination (mv semantics)")
    args = ap.parse_args()

    if args.restore is not None:
        return cmd_restore(args.restore, args.force)
    if args.no_browse and not args.extract:
        ap.error("--no-browse only applies to --extract; the folder move "
                 "browses for nothing to begin with")
    # --extract fills its own paths in (cwd, then a picker), so it is exempt
    # from the requirement the folder move has.
    if not args.extract and (not args.src or not args.dst):
        ap.error("src and dst are required (or use --restore)")
    if args.already_moved and args.on_conflict == "rename-only":
        ap.error("--on-conflict rename-only is meaningless with "
                 "--already-moved (there is no mv to do on its own) — "
                 "use abort to do nothing")
    if args.extract and args.already_moved:
        ap.error("--extract and --already-moved are different operations: "
                 "one moves chosen sessions out of a folder's history, the "
                 "other re-keys a whole folder's history after a rename")
    if args.extract and args.on_conflict not in (None, *SESSION_CONFLICT_MODES):
        ap.error(f"--on-conflict {args.on_conflict} does not apply to "
                 f"--extract (individual session files either exist at the "
                 f"destination or do not) — use "
                 f"{{{','.join(SESSION_CONFLICT_MODES)}}}")
    if args.session and not args.extract:
        ap.error("--session <id> selects which sessions to move, so it needs "
                 "--extract")
    if args.search is not None and not args.extract:
        ap.error("--search narrows which sessions are offered, so it needs "
                 "--extract; the folder move takes a folder's whole history")
    if args.recursive and not args.extract:
        ap.error("--recursive widens which sessions are offered, so it needs "
                 "--extract; a folder move already takes the projects nested "
                 "inside the folder with it")
    if args.search is not None and not args.search.strip():
        ap.error("--search needs something to look for")
    if args.search is not None and args.session:
        ap.error("--search and --session are two ways to say which sessions: "
                 "one filters the list, the other names ids outright and "
                 "skips it")

    if args.extract:
        # Paths are settled inside the mode — the source picker needs the
        # profiles to annotate its rows, and the destination is not asked for
        # until the sessions are chosen. Note the under(dst, src) guard the
        # folder move applies is deliberately absent: re-homing a session INTO
        # a subdirectory of where it started is the whole point here.
        profiles = [p for p in args.profile if os.path.isdir(p)]
        if not profiles:
            print(emsg("no existing --profile dirs given"), file=sys.stderr)
            return 1
        src, dst = resolve_extract_paths(args, profiles)
        if src is None:
            return 1
        return run_session_mode(args, src, dst, profiles)

    src = canonical(args.src)
    dst = canonical(args.dst)

    # Resolved before the paths are judged, because whether a missing src is
    # an error or a job half done is a question only the profiles can answer.
    profiles = [p for p in args.profile if os.path.isdir(p)]
    if not profiles:
        print(emsg("no existing --profile dirs given"), file=sys.stderr)
        return 1

    # The folder is gone and the destination is here: the move already
    # happened somewhere else, and what is left is the half claude-mv can
    # still do. Offer it rather than refuse and name a flag to retype with.
    if not args.already_moved and not os.path.isdir(src) \
            and os.path.isdir(dst):
        dst = moved_folder_at(src, dst)
        if not offer_reconcile(src, dst, profiles, args):
            return 1
        args.already_moved = True

    if args.already_moved:
        # Reconcile-only: the move already happened elsewhere. Both ends are
        # inverted vs. a real move — the old path must be gone, the new one
        # must be there — which also makes a mistyped argument loud.
        if os.path.exists(src):
            print(emsg(f"--already-moved, but the old path still exists: "
                       f"{c(src, 'cyan', stream=sys.stderr)}\n  drop the flag "
                       f"to move it, or pass the path the folder was moved "
                       f"*from*"), file=sys.stderr)
            return 1
        if not os.path.isdir(dst):
            print(emsg(f"--already-moved, but the new path is not a "
                       f"directory: {c(dst, 'cyan', stream=sys.stderr)}"),
                  file=sys.stderr)
            return 1
        # The folder is already living here, so sessions started in it record
        # the fully physical path — resolve the last component too. A dst the
        # offer above settled is already physical, so this is a no-op there;
        # a dst the user typed with the flag is taken as typed, mv-into
        # guesswork included, because with the flag they said which it is.
        dst = os.path.realpath(dst)
        # Unreachable by construction, and kept as the statement of intent:
        # dst has to exist to get here and src has to be gone, so the two
        # cannot be equal — identical paths are already refused above, by the
        # "old path still exists" check. --extract has the same rule and there
        # it IS reachable (nothing has to be missing), which is where it earns
        # its test.
        if src == dst:
            print(emsg("src and dst are the same path — nothing to "
                       "re-key"), file=sys.stderr)
            return 1
    else:
        if not os.path.isdir(src):
            print(emsg(f"src is not a directory: "
                       f"{c(src, 'cyan', stream=sys.stderr)}\n  if the folder "
                       f"was already renamed, re-key its history with: "
                       f"claude-mv --already-moved {args.src} {args.dst}"),
                  file=sys.stderr)
            return 1
        if os.path.isdir(dst):
            # mv-into-dir: the folder lands inside an existing directory, so
            # that directory's own symlinks resolve (unlike a dst that is the
            # new *name*, which doesn't exist yet).
            dst = os.path.join(os.path.realpath(dst), os.path.basename(src))
        if os.path.exists(dst):
            print(emsg(f"destination exists: "
                       f"{c(dst, 'cyan', stream=sys.stderr)}"),
                  file=sys.stderr)
            return 1
        if under(dst, src):
            print(emsg(f"cannot move {c(src, 'cyan', stream=sys.stderr)} "
                       f"into itself"), file=sys.stderr)
            return 1
        if not os.path.isdir(os.path.dirname(dst)):
            print(emsg(f"no such directory: "
                       f"{c(os.path.dirname(dst), 'cyan', stream=sys.stderr)}"),
                  file=sys.stderr)
            return 1

    # With --already-moved the destination is live already, so a session
    # running there is writing to a project dir this run may merge into.
    live = check_live_sessions(profiles, [src, dst] if args.already_moved
                               else [src])
    if live and not args.force:
        print(emsg("live Claude session(s) in the affected path(s) — "
                   "close them or use --force:"), file=sys.stderr)
        for entry in live:
            print(f"  {c(entry, 'yellow', stream=sys.stderr)}",
                  file=sys.stderr)
        return 1

    # Pre-flight everything — conflicts are resolved BEFORE the mv so that
    # abort really means "nothing happened".
    plans = [build_plan(p, src, dst) for p in profiles]
    has_conflicts = any(p["dir_conflicts"] or p["key_conflicts"] for p in plans)
    nothing_keyed = not any(p["dir_moves"] or p["dir_conflicts"] or
                            p["key_moves"] or p["key_conflicts"] for p in plans)
    if nothing_keyed:
        if args.already_moved:
            print(emsg(f"no Claude history keyed on "
                       f"{c(src, 'cyan', stream=sys.stderr)} — nothing to "
                       f"re-key\n  (only history.jsonl prompt entries, if "
                       f"any, would be touched; check the old path)"),
                  file=sys.stderr)
            return 1
        # A plain mv of a folder Claude never ran in is perfectly legitimate,
        # so this is a warning, not an error — but it is also exactly what a
        # mistyped or unresolvable src looks like, and staying silent about
        # it is how a move "succeeds" having migrated nothing.
        print(wmsg(c(f"no Claude project history is keyed on {src}",
                     "bold")) + "\n" +
              c("   the mv still happens; only history.jsonl prompt entries "
                "(if any) get re-keyed", "dim"))
        if os.path.islink(src):
            print(c(f"   note: {src} is a symlink — mv renames the link, so "
                    f"the real folder\n         its sessions were recorded in "
                    f"is not moving", "dim"))
        else:
            print(c("   if you expected sessions here, check the path — "
                    "Claude records the\n         symlink-resolved one", "dim"))
    mode = args.on_conflict
    if has_conflicts:
        print_conflicts(plans)
        if args.dry_run and not mode:
            print(c("  (dry run: pass --on-conflict or run for real to be "
                    "asked)", "dim"))
            mode = "consolidate"  # preview the least destructive resolution
            print(c("  previewing --on-conflict consolidate", "dim") + "\n")
        elif not mode:
            mode = ask_conflict_mode(args.already_moved)
            if mode is None:
                modes = [m for m in CONFLICT_MODES
                         if not (args.already_moved and m == "rename-only")]
                print(emsg(f"conflicts and stdin is not a tty — pass "
                           f"--on-conflict {{{','.join(modes)}}}"),
                      file=sys.stderr)
                return 2
        if mode == "abort":
            print(c("aborted — nothing was changed", "yellow"))
            return 1
    else:
        mode = mode or "consolidate"  # irrelevant: nothing conflicts

    stamp = time.strftime("%Y%m%d-%H%M%S")
    migrating = mode != "rename-only"

    rp = None
    if migrating:
        if args.dry_run:
            print(f"would create restore point "
                  f"{c(os.path.join(RESTORE_ROOT, stamp), 'dim')}")
        else:
            rp = create_restore_point(stamp, src, dst, mode, plans,
                                      moved=not args.already_moved)
            if rp:
                print(c("restore point:", "dim") + " " + c(rp, "dim"))

    if args.already_moved:
        print(c('would re-key' if args.dry_run else 're-keying', "bold") +
              f" history {c(src, 'cyan')} {c('→', 'dim')} {c(dst, 'cyan')} " +
              c("(folder already moved — not touching it)", "dim"))
    else:
        print(c('would move' if args.dry_run else 'moving', "bold") +
              f" {c(src, 'cyan')} {c('→', 'dim')} {c(dst, 'cyan')}")
        if not args.dry_run:
            try:
                os.rename(src, dst)
            except OSError:
                try:
                    shutil.move(src, dst)
                except OSError as e:
                    if rp:
                        shutil.rmtree(rp)  # nothing migrated — don't keep it
                    print(emsg(f"mv failed, nothing changed: {e}"),
                          file=sys.stderr)
                    return 1

    if not migrating:
        print(c("rename only", "bold") +
              c(" — Claude history left untouched (still keyed on the old "
                "path)", "dim"))
        return 0

    tally = new_tally()
    try:
        for plan in plans:
            apply_plan(plan, src, dst, mode, args.dry_run, tally)
    except Exception as e:  # noqa: BLE001 — anything mid-migration
        print("\n" + c("❌", "red", "bold", stream=sys.stderr) + " " +
              emsg(f"migration failed midway: {e}"), file=sys.stderr)
        if rp:
            print(f"   roll everything back with:  claude-mv --restore "
                  f"{c(os.path.basename(rp), 'bold', stream=sys.stderr)}",
                  file=sys.stderr)
        return 3

    summary = summarize(tally)
    if args.dry_run:
        if summary:
            print("\n" + c("would migrate", "bold") + " " + summary)
            print(c("(dry run — nothing was changed)", "dim"))
        else:
            print("\n" + c("(dry run — nothing was changed)", "dim"))
        return 0

    keep_backup = overwrite_backup_wanted()
    if rp:
        if has_conflicts and mode == "overwrite" and keep_backup:
            # One command per line: joined with a separator this ran past
            # 120 columns and wrapped mid-path on any normal terminal, which
            # is a poor way to present two commands meant to be copied.
            print("\n" + c("✅ done", "green", "bold") +
                  (f" — {summary}" if summary else "") + "\n" +
                  c("   discarded destination history is kept in the restore "
                    "point:", "dim") + f"\n   {c(rp, 'cyan')}\n" +
                  c("   undo everything:  ", "dim") +
                  f"claude-mv --restore {os.path.basename(rp)}\n" +
                  c("   discard for good: ", "dim") + f"rm -rf {rp}")
            return 0
        shutil.rmtree(rp)
    print("\n" + c("✅ done", "green", "bold") +
          (f" — {summary}" if summary else "") + "\n" +
          c("   `claude --resume` in the new location will find the old "
            "sessions (restore point cleaned up)", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
