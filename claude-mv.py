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


def jsonl_first_cwd(project_dir: str) -> str | None:
    """Best-effort: find a top-level "cwd" value in any session jsonl."""
    try:
        names = sorted(n for n in os.listdir(project_dir) if n.endswith(".jsonl"))
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(project_dir, name), encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str):
                        return cwd
        except OSError:
            continue
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


# ── restore points ──────────────────────────────────────────────────────────

def create_restore_point(stamp: str, src: str, dst: str, mode: str,
                         plans: list[dict], moved: bool) -> str | None:
    """Copy every path the migration will touch into a restore-point dir.

    `moved` records whether this run also moves the folder — false for
    --already-moved, so a later --restore knows not to move it back.

    Returns the restore-point path, or None when there is nothing to save.
    """
    entries = []   # {"type": "dir"|"file", "original": path, "copy": rel}
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
    manifest = {"stamp": os.path.basename(rp), "src": src, "dst": dst,
                "mode": mode, "moved": moved, "entries": entries,
                "created": created}
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
    print(f"restore point {c(stamp, 'bold')} "
          f"{c('[' + m['mode'] + ']', 'dim')} — will undo "
          f"{'' if was_move else 'the history re-key '}"
          f"{c(src, 'cyan')} {c('→', 'dim')} {c(dst, 'cyan')}:")
    move_back = was_move and os.path.isdir(dst) and not os.path.exists(src)
    if move_back:
        print(f"  mv {c(dst, 'cyan')} {c('→', 'dim')} {c(src, 'cyan')}")
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

def new_tally() -> dict:
    """Counters the appliers add to, so the run can close with one line of
    totals rather than leaving the reader to add up the per-profile sections.
    Summed across profiles: a two-profile move reports both."""
    return {"dirs": 0, "sessions": 0, "keys": 0, "history": 0}


def summarize(t: dict) -> str:
    """The tally as a single phrase, dropping whatever is zero. Empty string
    when nothing at all was touched — callers then print no tally rather than
    a row of zeroes."""
    bits = []
    if t["dirs"]:
        bits.append(f"{t['dirs']} project dir(s)")
    if t["sessions"]:
        bits.append(f"{t['sessions']} session file(s)")
    if t["keys"]:
        bits.append(f"{t['keys']} config key(s)")
    if t["history"]:
        bits.append(f"{t['history']} history "
                    f"{'entry' if t['history'] == 1 else 'entries'}")
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


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="claude-mv",
        description="mv a directory and migrate Claude Code history with it")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="show what would happen without changing anything")
    ap.add_argument("--force", action="store_true",
                    help="skip the live-session guard / restore confirmation")
    ap.add_argument("--already-moved", action="store_true",
                    help="the folder was already renamed by something else: "
                         "move nothing, just re-key the history stranded on "
                         "<src-dir> onto <dst> (src must be gone, dst must "
                         "exist)")
    ap.add_argument("--on-conflict", choices=CONFLICT_MODES,
                    help="policy when destination history already exists "
                         "(default: ask on a tty, abort otherwise)")
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
    if not args.src or not args.dst:
        ap.error("src and dst are required (or use --restore)")
    if args.already_moved and args.on_conflict == "rename-only":
        ap.error("--on-conflict rename-only is meaningless with "
                 "--already-moved (there is no mv to do on its own) — "
                 "use abort to do nothing")

    src = canonical(args.src)
    dst = canonical(args.dst)
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
        # the fully physical path — resolve the last component too.
        dst = os.path.realpath(dst)
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

    profiles = [p for p in args.profile if os.path.isdir(p)]
    if not profiles:
        print(emsg("no existing --profile dirs given"), file=sys.stderr)
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

    # Overwrite keeps the restore point as the archive of the discarded
    # history — on by default, opt out with CLAUDE_MV_OVERWRITE_BACKUP=0.
    keep_backup = (os.environ.get("CLAUDE_MV_OVERWRITE_BACKUP") or "1") \
        .strip().lower() not in ("0", "false", "no", "off")
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
