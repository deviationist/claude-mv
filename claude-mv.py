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
    print("\n⚠️  destination Claude history already exists:")
    for plan in plans:
        for d, target in plan["dir_conflicts"]:
            n = len([x for x in os.listdir(target) if x.endswith(".jsonl")])
            print(f"  projects/{os.path.basename(target)} "
                  f"({n} session file(s))  [{plan['profile']}]")
        for _, new_key in plan["key_conflicts"]:
            print(f"  config key {new_key}  [{plan['cfg']}]")


def can_prompt() -> bool:
    return sys.stdin.isatty() or bool(os.environ.get("CLAUDE_MV_FORCE_PROMPT"))


def ask_conflict_mode(already_moved: bool = False) -> str | None:
    """Interactive [o/c/r/a] prompt; None when stdin isn't a tty.

    `rename-only` is dropped with --already-moved: there is no mv to do on
    its own, so "leave the history untouched" is just abort.
    """
    if not can_prompt():
        return None
    print("""
How should the conflicting history be handled?
  [o] overwrite   — replace it with the moved project's history
                    (discarded history survives in the kept restore point)
  [c] consolidate — merge: session files combined, config entries merged""")
    if not already_moved:
        print("  [r] rename only — do the plain mv, leave Claude history "
              "untouched")
    print("  [a] abort       — do nothing")
    choices = {"o": "overwrite", "c": "consolidate", "a": "abort",
               "": "abort"}
    if not already_moved:
        choices["r"] = "rename-only"
    while True:
        try:
            ans = input("choice [a]: ").strip().lower()
        except EOFError:
            return "abort"
        if ans in choices:
            return choices[ans]
        print(f"  ? '{ans}' — pick {'o, c or a' if already_moved else 'o, c, r or a'}")


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
        print("claude-mv: no restore points", file=sys.stderr)
        return 1

    if arg == "list":
        print(f"restore points in {RESTORE_ROOT}:")
        for stamp in stamps:
            with open(os.path.join(RESTORE_ROOT, stamp, "manifest.json"),
                      encoding="utf-8") as f:
                m = json.load(f)
            print(f"  {stamp}  [{m['mode']}]  {m['src']} → {m['dst']}")
        print("restore one with: claude-mv --restore <stamp|latest>")
        return 0

    stamp = stamps[-1] if arg == "latest" else arg
    rp = os.path.join(RESTORE_ROOT, stamp)
    manifest_path = os.path.join(rp, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"claude-mv: no restore point '{stamp}' "
              f"(see claude-mv --restore)", file=sys.stderr)
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)
    src, dst = m["src"], m["dst"]
    originals = {e["original"] for e in m["entries"]}

    # Pre-`moved` restore points always came from a real move.
    was_move = m.get("moved", True)
    print(f"restore point {stamp} [{m['mode']}] — will undo "
          f"{'' if was_move else 'the history re-key '}{src} → {dst}:")
    move_back = was_move and os.path.isdir(dst) and not os.path.exists(src)
    if move_back:
        print(f"  mv {dst} → {src}")
    elif not was_move:
        print(f"  (--already-moved run: no folder move to undo, "
              f"{dst} stays put)")
    elif os.path.exists(src):
        print(f"  ⚠️  {src} already exists — folder move-back will be skipped")
    else:
        print(f"  ⚠️  {dst} not found — folder move-back will be skipped")
    doomed = [c for c in m["created"] if c not in originals and os.path.exists(c)]
    for c in doomed:
        print(f"  remove {c}")
    n_dirs = sum(1 for e in m["entries"] if e["type"] == "dir")
    n_files = len(m["entries"]) - n_dirs
    print(f"  restore {n_dirs} project dir(s) + {n_files} file(s) to their "
          f"pre-move state")

    if not force:
        if not can_prompt():
            print("claude-mv: confirmation needed — rerun with --force or "
                  "from a tty", file=sys.stderr)
            return 2
        try:
            if input("restore? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("aborted — nothing was changed")
                return 1
        except EOFError:
            return 1

    if move_back:
        try:
            os.rename(dst, src)
        except OSError:
            shutil.move(dst, src)
    for c in doomed:
        shutil.rmtree(c) if os.path.isdir(c) else os.remove(c)
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
    print(f"✅ restored — state is back to before the move; restore point "
          f"{stamp} removed")
    return 0


# ── migration ───────────────────────────────────────────────────────────────

def apply_dir_move(d: str, target: str, old: str, new: str,
                   mode: str, dry_run: bool) -> None:
    tag = "would" if dry_run else "did"
    conflict = os.path.exists(target)

    if conflict and mode == "overwrite":
        print(f"  {tag} discard projects/{os.path.basename(target)} "
              f"(copy kept in restore point)")
        if not dry_run:
            shutil.rmtree(target)
        conflict = False

    if not conflict:
        print(f"  {tag} rename projects/{os.path.basename(d)}")
        print(f"          → projects/{os.path.basename(target)}")
        if not dry_run:
            os.rename(d, target)
    else:  # consolidate
        print(f"  {tag} merge projects/{os.path.basename(d)}")
        print(f"          into projects/{os.path.basename(target)}")
        if not dry_run:
            for name in sorted(os.listdir(d)):
                s, t = os.path.join(d, name), os.path.join(target, name)
                if os.path.exists(t):
                    print(f"  ⚠️  keep both: {name} exists in destination — "
                          f"source copy left in place", file=sys.stderr)
                    continue
                os.rename(s, t)
            try:
                os.rmdir(d)
            except OSError:
                print(f"  ⚠️  {os.path.basename(d)} not empty after merge — "
                      f"left in place", file=sys.stderr)

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
        print(f"  {tag} rewrite cwd in {n_files} session file(s) "
              f"({n_lines} lines)")


def apply_plan(plan: dict, old: str, new: str, mode: str,
               dry_run: bool) -> None:
    tag = "would" if dry_run else "did"
    print(f"\n── profile {plan['profile']}")

    for d, target in plan["dir_moves"] + plan["dir_conflicts"]:
        apply_dir_move(d, target, old, new, mode, dry_run)

    cfg = plan["cfg"]
    if cfg and (plan["key_moves"] or plan["key_conflicts"]):
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        projects = data.get("projects", {})
        for key, new_key in plan["key_moves"]:
            print(f"  {tag} re-key {os.path.basename(cfg)}: {key} → {new_key}")
            if not dry_run and key in projects and new_key not in projects:
                projects[new_key] = projects.pop(key)
        for key, new_key in plan["key_conflicts"]:
            verb = "replace" if mode == "overwrite" else "merge into"
            print(f"  {tag} {verb} config key {new_key} "
                  f"({'from' if mode == 'overwrite' else 'with'} {key})")
            if not dry_run and key in projects:
                src = projects.pop(key)
                if mode == "overwrite":
                    projects[new_key] = src
                else:
                    projects[new_key] = merge_entries(src, projects.get(new_key, {}))
        if not dry_run:
            atomic_write(cfg, json.dumps(data, ensure_ascii=False, indent=2))

    if plan["hist"]:
        n = rewrite_jsonl_field(plan["hist"], "project", old, new, dry_run)
        if n:
            print(f"  {tag} rewrite project in history.jsonl ({n} entries)")


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
            print(f"claude-mv: --already-moved, but the old path still "
                  f"exists: {src}\n  drop the flag to move it, or pass the "
                  f"path the folder was moved *from*", file=sys.stderr)
            return 1
        if not os.path.isdir(dst):
            print(f"claude-mv: --already-moved, but the new path is not a "
                  f"directory: {dst}", file=sys.stderr)
            return 1
        # The folder is already living here, so sessions started in it record
        # the fully physical path — resolve the last component too.
        dst = os.path.realpath(dst)
        if src == dst:
            print("claude-mv: src and dst are the same path — nothing to "
                  "re-key", file=sys.stderr)
            return 1
    else:
        if not os.path.isdir(src):
            print(f"claude-mv: src is not a directory: {src}\n  if the folder "
                  f"was already renamed, re-key its history with: claude-mv "
                  f"--already-moved {args.src} {args.dst}", file=sys.stderr)
            return 1
        if os.path.isdir(dst):
            # mv-into-dir: the folder lands inside an existing directory, so
            # that directory's own symlinks resolve (unlike a dst that is the
            # new *name*, which doesn't exist yet).
            dst = os.path.join(os.path.realpath(dst), os.path.basename(src))
        if os.path.exists(dst):
            print(f"claude-mv: destination exists: {dst}", file=sys.stderr)
            return 1
        if under(dst, src):
            print(f"claude-mv: cannot move {src} into itself", file=sys.stderr)
            return 1
        if not os.path.isdir(os.path.dirname(dst)):
            print(f"claude-mv: no such directory: {os.path.dirname(dst)}",
                  file=sys.stderr)
            return 1

    profiles = [p for p in args.profile if os.path.isdir(p)]
    if not profiles:
        print("claude-mv: no existing --profile dirs given", file=sys.stderr)
        return 1

    # With --already-moved the destination is live already, so a session
    # running there is writing to a project dir this run may merge into.
    live = check_live_sessions(profiles, [src, dst] if args.already_moved
                               else [src])
    if live and not args.force:
        print("claude-mv: live Claude session(s) in the affected path(s) — "
              "close them or use --force:", file=sys.stderr)
        for entry in live:
            print(f"  {entry}", file=sys.stderr)
        return 1

    # Pre-flight everything — conflicts are resolved BEFORE the mv so that
    # abort really means "nothing happened".
    plans = [build_plan(p, src, dst) for p in profiles]
    has_conflicts = any(p["dir_conflicts"] or p["key_conflicts"] for p in plans)
    nothing_keyed = not any(p["dir_moves"] or p["dir_conflicts"] or
                            p["key_moves"] or p["key_conflicts"] for p in plans)
    if nothing_keyed:
        if args.already_moved:
            print(f"claude-mv: no Claude history keyed on {src} — nothing to "
                  f"re-key\n  (only history.jsonl prompt entries, if any, "
                  f"would be touched; check the old path)", file=sys.stderr)
            return 1
        # A plain mv of a folder Claude never ran in is perfectly legitimate,
        # so this is a warning, not an error — but it is also exactly what a
        # mistyped or unresolvable src looks like, and staying silent about
        # it is how a move "succeeds" having migrated nothing.
        print(f"⚠️  no Claude project history is keyed on {src}\n"
              f"   the mv still happens; only history.jsonl prompt entries "
              f"(if any) get re-keyed")
        if os.path.islink(src):
            print(f"   note: {src} is a symlink — mv renames the link, so the "
                  f"real folder\n         its sessions were recorded in is "
                  f"not moving")
        else:
            print(f"   if you expected sessions here, check the path — Claude "
                  f"records the\n         symlink-resolved one")
    mode = args.on_conflict
    if has_conflicts:
        print_conflicts(plans)
        if args.dry_run and not mode:
            print("  (dry run: pass --on-conflict or run for real to be asked)")
            mode = "consolidate"  # preview the least destructive resolution
            print("  previewing --on-conflict consolidate\n")
        elif not mode:
            mode = ask_conflict_mode(args.already_moved)
            if mode is None:
                modes = [m for m in CONFLICT_MODES
                         if not (args.already_moved and m == "rename-only")]
                print(f"claude-mv: conflicts and stdin is not a tty — pass "
                      f"--on-conflict {{{','.join(modes)}}}", file=sys.stderr)
                return 2
        if mode == "abort":
            print("aborted — nothing was changed")
            return 1
    else:
        mode = mode or "consolidate"  # irrelevant: nothing conflicts

    stamp = time.strftime("%Y%m%d-%H%M%S")
    migrating = mode != "rename-only"

    rp = None
    if migrating:
        if args.dry_run:
            print(f"would create restore point {os.path.join(RESTORE_ROOT, stamp)}")
        else:
            rp = create_restore_point(stamp, src, dst, mode, plans,
                                      moved=not args.already_moved)
            if rp:
                print(f"restore point: {rp}")

    if args.already_moved:
        print(f"{'would re-key' if args.dry_run else 're-keying'} history "
              f"{src} → {dst} (folder already moved — not touching it)")
    else:
        print(f"{'would move' if args.dry_run else 'moving'} {src} → {dst}")
        if not args.dry_run:
            try:
                os.rename(src, dst)
            except OSError:
                try:
                    shutil.move(src, dst)
                except OSError as e:
                    if rp:
                        shutil.rmtree(rp)  # nothing migrated — don't keep it
                    print(f"claude-mv: mv failed, nothing changed: {e}",
                          file=sys.stderr)
                    return 1

    if not migrating:
        print("rename only — Claude history left untouched (still keyed on "
              "the old path)")
        return 0

    try:
        for plan in plans:
            apply_plan(plan, src, dst, mode, args.dry_run)
    except Exception as e:  # noqa: BLE001 — anything mid-migration
        print(f"\n❌ claude-mv: migration failed midway: {e}", file=sys.stderr)
        if rp:
            print(f"   roll everything back with:  claude-mv --restore "
                  f"{os.path.basename(rp)}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("\n(dry run — nothing was changed)")
        return 0

    # Overwrite keeps the restore point as the archive of the discarded
    # history — on by default, opt out with CLAUDE_MV_OVERWRITE_BACKUP=0.
    keep_backup = (os.environ.get("CLAUDE_MV_OVERWRITE_BACKUP") or "1") \
        .strip().lower() not in ("0", "false", "no", "off")
    if rp:
        if has_conflicts and mode == "overwrite" and keep_backup:
            print(f"\n✅ done — discarded destination history is kept in the "
                  f"restore point:\n   {rp}\n   undo everything: claude-mv "
                  f"--restore {os.path.basename(rp)}  ·  discard for good: "
                  f"rm -rf {rp}")
            return 0
        shutil.rmtree(rp)
    print("\n✅ done — `claude --resume` in the new location will find "
          "the old sessions (restore point cleaned up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
