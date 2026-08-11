# claude-mv — agent guide

`claude-mv <src-dir> <dst>` — `mv` a project folder **and** re-key its Claude
Code history so `claude --resume` still finds the sessions at the new path.

- Migrates four stores per profile: the `projects/<encoded-cwd>/` dir, the
  `cwd` field in its session `*.jsonl`, the `projects` map keys in the config
  JSON (`~/.claude.json` for the default profile, else
  `<profile>/.claude.json`), and the `project` field in `history.jsonl`.
  Everything else under a profile is keyed by session id, so it needs nothing.
- **Nested projects follow** (a monorepo subdir with its own sessions). The
  cwd encoding (`[^A-Za-z0-9]` → `-`) is lossy, so a nested dir is only
  re-keyed when a session inside it confirms a real cwd under the moved path;
  an unrelated sibling that merely encodes alike is left alone.
- **Paths**: `~`, relative paths, trailing slashes and symlinked *ancestors*
  are all resolved to the physical path Claude records. The final component is
  **not** resolved — if `src` is a symlink, `mv` renames the link and the real
  folder never moves, so its history stays. Never assume the string the user
  typed is the key; it goes through `canonical()` first.
- **`--already-moved`** reconciles a folder renamed by something else: move
  nothing, re-key the history stranded on the old path. `src` must be gone,
  `dst` must exist. The old path can't be derived from the encoded dir name —
  it must be given.
- **Conflicts** (destination already has history) are detected *before* the
  `mv` and resolved by one policy: `--on-conflict
  {overwrite,consolidate,rename-only,abort}`, asked interactively on a tty,
  exit 2 when non-tty and unset. `abort` guarantees nothing happened.
  `rename-only` is rejected with `--already-moved` (no `mv` to do on its own).
- **Restore points** in `$CLAUDE_MV_RESTORE_ROOT` (default
  `~/.claude-mv/restore`) capture every affected path before any change;
  removed on success, kept after `overwrite` and on failure.
  `claude-mv --restore [<stamp>|latest]` lists / rolls back, folder move
  included.
- **Guards**: refuses when a live Claude session runs in the affected path
  (`--force` overrides); warns instead of reporting success when nothing is
  keyed on `src`.
- Exit codes: `0` ok · `1` refused/aborted · `2` conflict needing a policy ·
  `3` failed mid-migration (restore point kept, named in the error).
- **Every line printed is a human-facing report** — nothing parses claude-mv's
  output, so it is coloured throughout, on both streams, and the run closes
  with a tally of the stores it touched. Colour is a pure overlay: off when
  piped, honouring `NO_COLOR`, forced by `CLAUDE_MV_COLOR=always|never`, and
  with it off every line is byte-identical to the uncoloured original — which
  is what lets the tests keep asserting on plain substrings.

## Layout

- `claude-mv.zsh` — thin wrapper: resolves profile dirs, sources the
  gitignored `.env` beside it, execs the python. Profile dirs come from
  `CLAUDE_PROFILE_DIRS` in `.env` if set, else **claude-profile** when that is
  installed (`_claude_mv_profile_cmd` locates it exactly as claude-usage's
  bridge does — `$CLAUDE_PROFILE_SCRIPT`, function/binary on `PATH`, sibling
  clone — and asks the side-effect-free `list` porcelain), else `~/.claude`
  plus `~/.claude-personal`. The bridge is soft: every failure path falls
  through, and nothing else in the repo knows claude-profile exists.
- `claude-mv.py` — all the logic; stdlib only, no deps.
- `tests/test_claude_mv.py` — seven layers (unit, e2e, multi-profile, zsh
  wrapper, conformance against the real `~/.claude`, live against the `claude`
  binary, resume UI under tmux). The last two opt in with
  `CLAUDE_MV_LIVE_TEST=1`; they need no auth and spend no tokens. The wrapper
  layer needs zsh — CI installs it on Linux and runs `zsh --version` *without*
  a `|| true`, so a runner image that drops zsh fails the build instead of
  quietly skipping the layer.
- `tools/generate-readme-svg.zsh` → `assets/*.svg` — the README images. Runs
  the tool unmodified against a throwaway `$HOME` and converts the ANSI to an
  SVG terminal grid, so the text in them is real output. Sibling of the same
  script in claude-profile / claude-usage / claude-statusline; keep the four
  roughly in sync.

## Working on this

- Run `python3 tests/test_claude_mv.py` — hermetic, ~1s. Never point a test at
  the real `~/.claude` for anything but reading.
- The conformance layer is the early-warning system for Claude Code changing
  its on-disk format. If it starts failing, the format moved — fix the tool,
  not the test.
- When changing path handling, add the case to `TestPathForms`: every spelling
  of one folder must migrate identically.
- New output goes through `c()` / `emsg()` / `wmsg()`, never a raw escape, and
  keeps the palette's meaning: cyan = the path/name/key being acted on, bold =
  the identifier or count worth reading, dim = asides, green = did/safe,
  yellow = would/warning, red = error/destructive. Pad and align *before*
  colouring — an escape counts toward `len()` but not toward what is drawn.
- Anything that changes the report, the conflict prompt or the restore screen
  means rerunning `tools/generate-readme-svg.zsh` and committing the new SVGs
  with the README.
