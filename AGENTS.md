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
- **`--extract`** moves individual *sessions* instead of a folder — for the
  project born mid-session in a parent dir (you were in `~/code`, told Claude
  to `mkdir` and `cd`, and the whole conversation stayed keyed on `~/code`).
  Moving all of `~/code`'s history would be wrong, so this picks out the ones
  that don't belong. Candidates are the sessions **homed in** `src` — the
  project dir a session was *born* in, whatever cwd it later wandered to,
  which is exactly the session a "latest cwd" match would miss. What moves:
  `<id>.jsonl`, its `<id>/` sidecar (subagents + tool results — the one
  session-keyed store living *inside* a project dir), and that session's
  `history.jsonl` entries, selected by `sessionId`. What deliberately does
  **not**: the transcript's `cwd` lines (nothing moved on disk, so rewriting
  them would falsify the record — and since dst is normally *inside* src, a
  prefix remap would hit the lines already naming dst a second time), and the
  config `projects` map (the source project still exists; fabricating a
  destination entry would transplant its trust flag and `allowedTools` onto a
  path the user never approved). The `under(dst, src)` guard is skipped here —
  moving into a subdirectory is the whole point. Conflicts are per-session and
  resolve with `--on-conflict {overwrite,skip,abort}`, default `skip`. A
  destination counts as occupied if **either** half is there — the `<id>.jsonl`
  or the `<id>/` sidecar. A stray sidecar alone is rare but real (an
  interrupted run, a half-deleted session), and treating it as a clean move
  would land the transcript and *then* fail renaming the sidecar onto it,
  stopping halfway — breaking the up-front-detection promise — besides handing
  one conversation another's subagent transcripts.
- **`--extract` is a three-step guide**: which folder (a directory browser
  starting at the cwd, each row annotated with its session count), which
  sessions, then where to — in that order, because the destination is only
  decidable once you know what you are moving. Positionals **seed** the two
  folder steps rather than skipping them; a lone positional is always `dst`,
  since `src` defaults to the cwd and `dst` has no default. `--no-browse`
  drops both folder steps and then requires both paths — it does *not* mean
  non-interactive, which is why it is not called that: the session picker
  still runs, and `--session` is what silences that. **`dst` is never
  inferred** — not from the cwd, the source, or the session.
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
  The wrapper also resolves **ccfind** for `--extract`, same three-candidate
  shape but one extra trick: ccfind is a zsh *function*, so `command -v` finds
  it while the python still can't call it — `$functions_source[ccfind]` traces
  it back to the file to source. Order: `$CLAUDE_MV_CCFIND_SCRIPT` (env, not
  `.env`, and authoritative) → loaded function → sibling clone. Handed down as
  `CLAUDE_MV_CCFIND_SOURCE`.
- `claude-mv.py` — all the logic; stdlib only, no deps.
- **Two soft dependencies, both only for `--extract`, neither required.**
  *ccfind* lists the candidate sessions (`--json -l -x -d <src>`) and adds
  full-text search over transcripts; without it the same list comes off the
  filesystem, still across every profile. *fzf* drives both pickers — the
  session multi-select and the directory browser; without it they become a
  numbered prompt and a readline path prompt with tab completion. Both are
  invoked through `fzf_layout()`: **`--reverse` and a `--height` sized to the
  list**. Without `--height` fzf takes the alternate screen, so the command and
  everything above it vanish for the duration — a lot of screen to borrow for
  picking one row — and its default layout builds upward against every other
  line this tool prints. A line count rather than `~N%`: the auto-size form
  needs a newer fzf, and an unknown flag exits non-zero, which `pick_with_fzf`
  cannot tell apart from "cancelled". Mirrors ccfind's `--reverse --height=80%`;
  the family should not disagree about which way its pickers run. In `auto`,
  fzf is only launched when there is a tty (`fzf_wanted`): it draws a
  full-screen UI and reads the keyboard, so starting it on a pipe hangs rather
  than fails, which is exactly what the suite hit. Every failure path falls
  through — except
  a `CLAUDE_MV_SOURCE=ccfind` that can't be honoured, which fails loudly
  because being asked for a specific source and quietly using another is worse
  than stopping. **`scope_exact` in ccfind's JSON is the compatibility
  handshake**: a ccfind that took `-x` and ignored it would answer about the
  whole *subtree*, so anything but a definite `true` means fall back.
- `tests/test_claude_mv.py` — nine layers (unit, e2e, multi-profile, session
  sources, session move + picker, zsh wrapper, conformance against the real
  `~/.claude`, live against the `claude` binary, resume UI under tmux). The
  last two opt in with `CLAUDE_MV_LIVE_TEST=1`; they need no auth and spend no
  tokens. The wrapper layer needs zsh — CI installs it on Linux and runs `zsh
  --version` *without* a `|| true`, so a runner image that drops zsh fails the
  build instead of quietly skipping the layer.
  **`TestSessionSourcesAgree` is the one that keeps the soft dep honest**: it
  runs the *real* ccfind and the filesystem walk over one fixture and demands
  identical id sets, because a source that disagrees makes the tool behave
  differently per machine and no per-source test can see it. It skips without
  a ccfind checkout — CI included — so treat it as a local guard, not a
  verified-everywhere claim.
- `tools/generate-readme-svg.zsh` → `assets/*.svg` — the README images. Runs
  the tool unmodified against a throwaway `$HOME` and converts the ANSI to an
  SVG terminal grid, so the text in them is real output. Sibling of the same
  script in claude-profile / claude-usage / claude-statusline; keep the four
  roughly in sync. **All five animate as a terminal session**: the command
  types itself a character at a time with a block cursor walking after it, a
  beat for the Enter, then the output arrives line by line; multi-command
  scenes (restore has three) interleave type→run→print. Then it holds and
  loops. All CSS `@keyframes` — `<img>` on GitHub runs stylesheets and blocks
  scripts, so CSS is the only thing that works there. `step-end`, not a fade (a
  terminal prints a character, it does not dissolve one into being — the same
  reasoning ccfind's frame timeline documents). Unlike ccfind's stacked frames
  this needs no `opacity="0"` fallback: a renderer ignoring the stylesheet
  shows everything, which is the state worth falling back to. The pacing is
  invented — a single captured run has no timing — and the README says so.
  **fzf cannot be captured** — it draws with terminal control sequences on a
  screen it takes over, so there is nothing on stdout to pipe. The picker scene
  is therefore *reconstructed* by `fzf_frame()` from what claude-mv actually
  passes fzf (prompt, header, `--multi`, and the rows it feeds in), the same
  approach ccfind takes and for the same reason. Reconstruction means it can
  drift from reality: if the invocation in `pick_with_fzf` changes, that
  function has to change with it.
  **Five things are load-bearing; changing any one silently breaks it:**
  (1) it must **loop** — a browser does not pause a CSS animation in an
  offscreen `<img>`, so a run-once reveal on an image below the fold is
  finished before anyone scrolls to it (measured: an image 3000px down was
  fully revealed the instant it came into view). Start-on-scroll is not
  available; that needs script. The cycle is held to 7–9s so the wait after
  scrolling is bounded. (2) **per-element `@keyframes`, never one rule with
  per-element `animation-delay`** — a delay applies to the first iteration
  only, so with `infinite` everything would snap into sync on the second pass
  and the sequence would never be seen again. (3) **one stop per switch** —
  duplicate percentages collapse to the last declaration. (4) the
  reduced-motion rule needs **`!important`**: the per-element `#l<i>`/`#c<i>_<j>`
  selectors outrank `text.l`. (5) typing is **one element per character** plus
  one per cursor position, not a whole-line copy per keystroke — the frame-per-
  keystroke shape ccfind uses would multiply a 58-character command by every
  line it sits on. Timing comes from a running clock (`AT[]`), not index ×
  step, because a command consumes a keystroke per character rather than one
  slot.

## Working on this

- Run `python3 tests/test_claude_mv.py` — hermetic, ~8s. Never point a test at
  the real `~/.claude` for anything but reading.
- **The suite is checked by mutation, not just by passing.** Breaking one
  decision at a time (drop a guard, invert a sort, widen a filter) must turn
  it red; a change that survives is a line the tests only watch. Coverage sits
  at ~95%, and the rest is mostly `except OSError` and cross-device `move`
  fallbacks. When adding a rule here, ask what single edit would defeat it and
  make sure some test names that.
- Two known-equivalent shapes, so nobody chases them: `--already-moved`'s
  `src == dst` (unreachable — dst must exist and src must be gone) and
  `--extract`'s `not dst` half of the `--no-browse` check (after the
  lone-positional swap, a set src implies a set dst). Both are kept as
  statements of intent and both say so in place.
- The conformance layer is the early-warning system for Claude Code changing
  its on-disk format. If it starts failing, the format moved — fix the tool,
  not the test.
- When changing path handling, add the case to `TestPathForms`: every spelling
  of one folder must migrate identically.
- **Fixtures write compact JSON** (`jsonl()` in the test file), because that is
  what Claude writes and because ccfind reads the cwd out with a regex that
  assumes no space after the colon. A prettier fixture is invisible to it, and
  the cross-source test silently stops proving anything.
- Touching `--extract`? The three deliberate non-actions — no `cwd` rewrite,
  no config entry, no folder move — are load-bearing, each with a test naming
  the reason. If one starts looking like an oversight, read the test before
  "fixing" it.
- New output goes through `c()` / `emsg()` / `wmsg()`, never a raw escape. A
  test parses the call sites and fails on a style name `_SGR` doesn't define —
  `c()` indexes it directly, so a typo is a `KeyError` on a terminal that the
  piped suite would never reach. Keep the palette's meaning: cyan = the path/name/key being acted on, bold =
  the identifier or count worth reading, dim = asides, green = did/safe,
  yellow = would/warning, red = error/destructive. Pad and align *before*
  colouring — an escape counts toward `len()` but not toward what is drawn.
- Anything that changes the report, the conflict prompt or the restore screen
  means rerunning `tools/generate-readme-svg.zsh` and committing the new SVGs
  with the README.
