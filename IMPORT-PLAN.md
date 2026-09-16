# Cross-host history transfer — plan and state

Working notes for the `--export` / `--import` pair. `--export` is built and
committed; `--import` is not. Written to be picked up cold, so it records the
decisions **with their reasons** — the point is not to re-litigate them.

Read `AGENTS.md`'s `--export` bullet first; this file is the part that is not
yet true.

## Where it stands

- `--export` — **done**, commit `2fb54cd`. 265 tests green (+19 for this).
  Check whether that commit is pushed before starting; it was not at the time
  of writing.
- `--import` — **not started.** The bundle format and the manifest are
  settled, so this is the mechanical half.
- The in-transcript provenance marker — **deferred to v2**, gated on an
  unrun experiment. See *Deferred*.

## Settled, with reasons (do not re-open without a new reason)

- **The transport is not claude-mv's job.** `claude-mv --export ~/p | ssh quim
  claude-mv --import ~/p`. Every guarantee this tool makes is local:
  `canonical()` resolves symlinked ancestors against the filesystem it runs
  on, profile resolution is the wrapper's job per machine, a restore point can
  only roll back writes on its own disk, and the live-session guard needs real
  pids. A network-aware `--already-moved` would reimplement all four badly,
  and would end the hermetic suite.
- **It is a fork, not a move.** The source keeps everything; the two copies
  diverge from that moment and never reconverge. There is deliberately **no
  cross-host `consolidate`** — `apply_dir_move`'s "keep both" branch would
  silently strand one side of two genuinely diverged transcripts sharing a
  session id.
- **What a project owns is Claude Code's answer.** `claude project purge
  --dry-run <path>` enumerates it. Wired in as a **conformance oracle only**
  (`TestFormatConformance.test_claude_agrees_with_our_model_of_project_state`)
  — its output is prose, so parsing it at runtime would be a fragile
  dependency. Reading it is what turned up `memory/` living inside
  `projects/<enc>/`, which would otherwise have been missed.
- **Store policy**, and the two places it disagrees with the vendor on
  purpose, because the destination is a different machine:
  - carried: `projects/<enc>/` (transcripts, `<id>/` sidecars, `memory/`),
    this project's `history.jsonl` entries, `tasks/` `todos/` `plans/`
  - refused: the config entry (trust, `allowedTools`, **and the project's MCP
    servers**, which name binaries on the source host), `file-history/`
    (pre-edit file contents as they were *there*, against a fresh checkout
    *here*), `shell-snapshots/` and `session-env/` (the vendor already
    excludes both)
- **Session-keyed stores match by id prefix, not filename** — they spell
  themselves differently (`<id>/`, `<id>.json`, `<id>-agent-<id>.json`) and a
  guessed convention would silently carry nothing.
- **With no `-o`, stdout is the tar and every human line goes to stderr.**
  That is the only reason the pipe works.
- **Provenance lives in the manifest and the import report** for v1 — free,
  and guaranteed to land.

## `--import`: the design

```
claude-mv --import <dst-dir> [-i FILE] [--on-conflict MODE] [-n] [--force]
```

1. **Read the bundle** — `-i FILE`, else stdin. **Extract to a temp dir
   first**, do not apply while streaming. This trades the streaming purity
   `--export` has for up-front conflict detection, and that is the right
   trade: "conflicts are detected before anything is written, so `abort`
   really means nothing happened" is a promise the whole tool rests on, and a
   sequential tar read cannot keep it.
2. **Validate the manifest** — `format == BUNDLE_FORMAT`, `version <=
   BUNDLE_VERSION`. A newer bundle than this claude-mv understands is an
   error, not a best effort.
3. **`canonical(dst)` on the receiving host** — this is the whole reason
   import runs there. Must already be a directory; `--import` moves nothing
   and creates no project folder.
4. **Remap** each `manifest.projects[].cwd` through
   `remap(cwd, manifest.source.path, dst)`, then `enc()` the result for the
   target dir name. Nested projects fall out of this for free.
5. **Rewrite `cwd` in the transcripts** — `rewrite_jsonl_field(path, "cwd",
   manifest.source.path, dst)`. Note this is the **opposite** of `--extract`'s
   deliberate non-action, and for a reason that must not get mis-copied:
   there, nothing moved on disk so the record would become a lie; here the
   project genuinely does live at `dst` on this machine.
6. **Conflicts** — target project dir or session id already present. Modes:
   `overwrite`, `consolidate`, `abort`. `rename-only` is meaningless (no mv);
   reject it the way `--already-moved` does. Ask on a tty, exit 2 when
   non-tty and unset.
7. **Restore point** — an ordinary local one via `write_restore_point`, with
   `kind: "import"`. Works normally because it is on this host's own disk.
8. **Live guard** — `check_live_sessions(profiles, [dst])`.
9. **Survey page** — reuse `confirm_session_move`'s shape. Must say: where the
   history came from (host, OS, path), that paths inside message content still
   describe that machine, and that no config entry is created so Claude will
   ask for trust on first run.
10. **Apply**, then tally. No config entry, ever.

## Open questions — decide these first, they shape step 4 and 10

1. **Which profile does import write to?** A bundle is profile-flattened; the
   receiving machine has its own profiles. Recommendation: the **first
   configured profile**, printed explicitly, overridable by passing a single
   `--profile`. Guessing silently is the failure mode to avoid.
2. **`history.jsonl` on re-import.** Lines are appended with `project`
   remapped. A second import of the same bundle would duplicate them. Needs a
   dedupe key — `sessionId` + `display` is the obvious candidate. Decide
   before writing step 10.
3. **Bundle size.** 387 MB across 44 project dirs in the profile this was
   written against; largest single project 80 MB. Fine over `wg-trunk0`, but
   `--import` should say what it is about to unpack.

## Deferred

- **The in-transcript provenance marker (v2).** Claude Code has a
  first-class record for it: `{"type":"system","subtype":"informational",...}`
  with `content`, `level`, and `uuid`/`parentUuid` threading (field set
  verified against a real record). Append at the **tail** — recency matters,
  since ~22,000 stale `/Users/…` paths sit above it in one transcript.
  Unresolved: **does an `informational` record reach the model on resume, or
  is it UI-only?** Not answerable from disk. Probe written and ready but
  **never run** — it needs a tty to execute, since writing a synthetic
  transcript into a live profile (correctly) trips the
  *Session Transcript Tampering* guard when an agent does it:

      scratchpad/probe-system-record.py   # regenerate if the scratchpad is gone

  It plants two nonces — one in a real user turn as a positive control, one
  in the `system` record — resumes with `--print` on Haiku, and reports which
  came back. Three-way result: both → use `informational`; control only →
  fall back to a synthetic user turn (Claude Code already injects those, which
  is why `MACHINE_PREFIXES` exists); neither → the probe is broken, not an
  answer.
- **Bundle integrity.** That same denial is design feedback: a transferred
  history is the least trustworthy input Claude Code can consume, and
  appending records to a transcript is a credible injection vector. If the
  marker ships, the bundle probably wants a checksum or signature, and import
  should refuse a bundle whose transcripts already carry claude-mv-attributed
  markers it did not write.
- **No cross-host reconciliation.** Two diverged forks stay diverged.

## Unrelated bugs found along the way

- **`TestResumePickerWithTmux.test_plain_mv_orphans_the_session` fails** under
  `CLAUDE_MV_LIVE_TEST=1`. Claude Code's trust dialog now defaults to
  **"No, exit"**, so `_picker_pane`'s belt-and-braces bare `Enter` quits
  Claude instead of accepting, and the assertion times out after 60s. The
  tool is fine — the positive test passes precisely because claude-mv
  migrates `hasTrustDialogAccepted` with the config key, so only the plain-mv
  control ever meets the dialog. Fix is in the fixture: send `Down` before
  `Enter`, or pre-trust both paths in that test (safe there, since no
  claude-mv run is watching for a conflict).
- **That failure also blocks the live-layer timing figure.** `README.md`'s
  `~20s` was left deliberately unchanged, because the only measurement
  available today (82s) is mostly that 60s timeout.
- **`tests/test_claude_mv.py`'s module docstring says "Seven layers"** and
  lists seven, while the README says eleven. Predates this work by several
  features — the session-sources, session-move and search layers were never
  added to it.
