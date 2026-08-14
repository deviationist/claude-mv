#!/usr/bin/env zsh
# ---------------------------------------------------------------------------
# tools/generate-readme-svg.zsh — regenerate the README SVGs.
#
# Renders the REAL claude-mv into SVG terminal windows: builds a hermetic
# sandbox (a fake $HOME holding a folder to move and two seeded Claude profiles
# — projects/, session jsonl, config json, history.jsonl), runs the tool
# **unmodified** against it, and lays the captured output out on a terminal
# grid. The text in the images is therefore genuine output, not art. Only the
# window chrome around it — title bar, traffic lights, the `%` prompt line —
# is drawn. Nothing outside the sandbox is read or written: every path the tool
# touches is under $HOME, and $HOME is a tmpdir removed on exit.
#
# Sibling of claude-profile's, claude-usage's and claude-statusline's
# tools/generate-readme-svg.zsh, from which the grid/emitter core is borrowed;
# keep them roughly in sync. Three differences worth knowing:
#
#   * claude-mv has no porcelain — every line it prints is a human-facing
#     report — so all but one image is plain SGR capture, with no
#     screen-scraping and no stubbed dependency: --extract has two optional
#     helpers (ccfind, fzf), and its scene pins both to their absent form,
#     which needs no stub and renders the same on any machine. The exception is
#     the picker scene, which is RECONSTRUCTED by fzf_frame(): fzf draws with
#     terminal control sequences on a screen it takes over, so there is nothing
#     on stdout to capture. ccfind reconstructs its picker for the same reason.
#     Reconstruction can drift — if pick_with_fzf's invocation changes,
#     fzf_frame has to change with it.
#   * bold is rendered as font-weight, not just the bright palette. claude-mv
#     leans on bold to pick out the identifier in a line (`did rewrite cwd in
#     **3** session file(s)`), which the siblings' colour-only mapping would
#     silently flatten.
#   * the three emoji claude-mv prints need real cell widths and whole
#     grapheme clusters, neither of which the siblings' grid models — see
#     run_tspans() below.
#   * several screens contain an interactive prompt — the extract guide is
#     three of them on its own (folder, sessions, folder). The tool writes a
#     prompt without a trailing newline and a piped stdin is never echoed, so
#     the answer and the line break are missing from the capture; `answer()`
#     puts them back, and `answer1()` does it one prompt at a time for the
#     guide, whose two folder prompts read identically but are answered
#     separately. Those keystrokes are the only characters in these images
#     that claude-mv did not itself emit.
#
# The sandbox lives under a tmpdir, so its paths are long and machine-specific.
# They are rewritten for display only — /Users/demo, plus the same rewrite
# applied to Claude's encoded form of the path, so `projects/` dir names stay
# consistent with the paths beside them. Nothing else is touched.
#
# Usage:  zsh tools/generate-readme-svg.zsh
#           → assets/{move,profiles,conflict,restore,sessions,picker}-<hash>.svg,
#             older ones deleted, README <img> references rewritten (the random
#             hash busts GitHub's camo image cache). Commit all six files.
#         zsh tools/generate-readme-svg.zsh MOVE.svg PROFILES.svg CONFLICT.svg RESTORE.svg SESSIONS.svg
#           → fixed paths, README untouched (the picker scene is file-mode only
#             via the default path).
#
# Regenerate whenever the migration report, the conflict prompt, the session
# picker or the restore screen changes. Restore-point stamps are real timestamps, so they track the
# day you run it — fine for a demo.
# ---------------------------------------------------------------------------
emulate -L zsh
setopt extended_glob

here=${0:a:h}
root=${here:h}

tmp=$(cd "$(mktemp -d)" && pwd -P)   # physical: canonical() resolves symlinked
trap 'rm -rf "$tmp"' EXIT            # ancestors, so /var vs /private/var matters

fakehome="$tmp/home"
export HOME="$fakehome"
export USER=demo
export CLAUDE_MV_COLOR=always
unset CLAUDE_CONFIG_DIR NO_COLOR     # ambient values would leak in

cm="python3 $root/claude-mv.py"
DEMO='/Users/demo'                   # what the sandbox path is displayed as

# ---- hermetic sandbox ------------------------------------------------------
# Rebuilt from scratch before every scenario, so each image is an independent
# run rather than a continuation of the previous one's leftovers.

fakeuuid() { printf '%08x-51ab-4c7d-9f2e-%012x' $1 $(( $1 * 7919 )) }

seed_project() {  # seed_project <profile-dir> <cwd> <n-files> <n-lines>
  local prof=$1 cwd=$2; integer nf=$3 nl=$4 i j
  local d="$prof/projects/${cwd//[^A-Za-z0-9]/-}" f id
  mkdir -p "$d"
  local role
  for (( i = 1; i <= nf; i++ )); do
    id=$(fakeuuid $(( i + ${#cwd} )))
    f="$d/$id.jsonl"
    : > "$f"
    for (( j = 1; j <= nl; j++ )); do
      (( j % 2 )) && role=user || role=assistant
      print -r -- "{\"parentUuid\":null,\"sessionId\":\"$id\",\"cwd\":\"$cwd\",\"version\":\"2.0.14\",\"type\":\"$role\",\"uuid\":\"$(fakeuuid $(( i * 100 + j )))\"}" >> "$f"
    done
  done
}

seed_profile() {  # seed_profile <profile-dir> <config-json> <cwd>... — the two
  local prof=$1 cfg=$2; shift 2  # non-project stores, keyed by absolute path
  local cwd msg
  mkdir -p "$prof"
  : > "$prof/history.jsonl"
  for cwd in "$@"; do
    for msg in 'add the retry wrapper' 'why is the encoder lossy?' \
               'rename the module' 'run the tests'; do
      print -r -- "{\"display\":\"$msg\",\"pastedContents\":{},\"project\":\"$cwd\"}" \
        >> "$prof/history.jsonl"
    done
  done
  {
    print -r -- '{'
    print -r -- '  "numStartups": 214,'
    print -r -- '  "projects": {'
    integer k=1
    for cwd in "$@"; do
      print -rn -- "    \"$cwd\": {\"allowedTools\": [\"Read\", \"Edit\"], \"history\": [], \"hasTrustDialogAccepted\": true, \"projectOnboardingSeenCount\": 3}"
      (( k < $# )) && print -r -- ',' || print -r -- ''
      (( k++ ))
    done
    print -r -- '  }'
    print -r -- '}'
  } > "$cfg"
}

# The shape almost every run has: one profile, one project, nothing nested.
# This is the hero image, so it deliberately shows the ordinary thing rather
# than the capable one.
seed_simple() {
  rm -rf "$fakehome"; mkdir -p "$fakehome/code/lipsum/src"
  seed_project "$fakehome/.claude" "$fakehome/code/lipsum" 3 16
  seed_profile "$fakehome/.claude" "$fakehome/.claude.json" "$fakehome/code/lipsum"
}

seed() {  # a folder to move, with a nested project of its own, in two profiles
  rm -rf "$fakehome"
  mkdir -p "$fakehome/code/lipsum/api" "$fakehome/code/lipsum/web"
  seed_project "$fakehome/.claude"          "$fakehome/code/lipsum"     2 14
  seed_project "$fakehome/.claude"          "$fakehome/code/lipsum/api" 1  9
  seed_project "$fakehome/.claude-personal" "$fakehome/code/lipsum"     1 11
  seed_profile "$fakehome/.claude"          "$fakehome/.claude.json" \
               "$fakehome/code/lipsum" "$fakehome/code/lipsum/api"
  seed_profile "$fakehome/.claude-personal" "$fakehome/.claude-personal/.claude.json" \
               "$fakehome/code/lipsum"
}

seed_session() {  # seed_session <profile> <cwd> <n> <prompt> <stamp> [later-cwd]
  local prof=$1 cwd=$2 prompt=$4 stamp=$5 later=${6:-}
  local id=$(fakeuuid $3) d="$prof/projects/${cwd//[^A-Za-z0-9]/-}"
  mkdir -p "$d"
  local f="$d/$id.jsonl"
  # Real prompt text, because --extract puts the opening line of each
  # conversation in the picker — four "(no prompt recorded)" rows would show
  # the layout and none of the point.
  print -r -- "{\"type\":\"user\",\"sessionId\":\"$id\",\"cwd\":\"$cwd\",\"message\":{\"role\":\"user\",\"content\":\"$prompt\"}}" > "$f"
  # The turn recorded after the conversation cd'd into the folder it had just
  # made. Only the picked session gets one; it is what makes this the shape
  # --extract exists for rather than an ordinary list.
  [[ -n $later ]] && \
    print -r -- "{\"type\":\"user\",\"sessionId\":\"$id\",\"cwd\":\"$later\",\"message\":{\"role\":\"user\",\"content\":\"now wire up the decoder\"}}" >> "$f"
  # Prompt-history entries carry the session that wrote them; re-keying just
  # this session's is the thing the report's last line counts. A conversation
  # that carried on in the new folder left more than one, so the count in the
  # image is a count of something.
  print -r -- "{\"display\":\"$prompt\",\"pastedContents\":{},\"project\":\"$cwd\",\"sessionId\":\"$id\"}" \
    >> "$prof/history.jsonl"
  [[ -n $later ]] && \
    print -r -- "{\"display\":\"now wire up the decoder\",\"pastedContents\":{},\"project\":\"$cwd\",\"sessionId\":\"$id\"}" \
      >> "$prof/history.jsonl"
  touch -t "$stamp" "$f"          # <stamp> orders the picker, newest first
}

# The shape --extract exists for: several sessions homed in ~/code, one of
# which had an idea, made a folder mid-conversation and kept working inside
# it. Its history is stranded on ~/code — but so is everyone else's, and
# theirs belongs there, which is why moving the whole folder's history is the
# wrong tool and picking one session is the right one.
seed_sessions() {
  rm -rf "$fakehome"
  mkdir -p "$fakehome/code/lipsum/src"
  local prof="$fakehome/.claude"
  # Config + a base history first: seed_profile truncates history.jsonl, so
  # the per-session entries have to be laid down after it.
  seed_profile "$prof" "$fakehome/.claude.json" "$fakehome/code"
  : > "$prof/history.jsonl"
  # Big seeds so fakeuuid's %08x reads like a session id rather than a
  # counter — these end up in the picker, where 00000004 would look fake.
  seed_session "$prof" "$fakehome/code" 3872015300 \
    'draft a tool that recovers images from the app cache' 202608141405 \
    "$fakehome/code/lipsum"
  seed_session "$prof" "$fakehome/code" 2843017391 \
    'which of these repos still target node 18?'          202608131152
  seed_session "$prof" "$fakehome/code" 3387281044 \
    'compare the two encoder branches'                    202608120931
  seed_session "$prof" "$fakehome/code" 1749306622 \
    'clean up the stale worktrees'                        202608110847
}

# fzf cannot be captured. It draws with terminal control sequences on a screen
# it takes over, so there is nothing on stdout to pipe — which is why ccfind
# reconstructs its picker rather than recording it, and why this does too.
# Everything here is drawn from what claude-mv actually passes fzf (the prompt,
# the header, --multi, and the rows it feeds in), so the layout is a claim
# about our own invocation, not a guess at fzf's.
#
# --reverse order: prompt, then the match count, then the header, then rows.
# The pointer sits on the current row and the marker on anything Tab has
# selected — the two glyphs that make it a MULTI-select picker, which is the
# part of this UI worth showing.
fzf_frame() {  # fzf_frame <out-array> <prompt> <header> <cur> <marked> <row>...
  local out=$1 prompt=$2 hdr=$3; integer cur=$4 marked=$5; shift 5
  local -a f
  integer n=$#
  f=("$(cmdline_dim "  $prompt")"
     "$(cmdline_dim "  $n/$n")"
     $'\e[2m  '"$hdr"$'\e[0m')
  integer k
  for (( k = 1; k <= n; k++ )); do
    # Two columns of chrome before the text, both always occupying their
    # width, so the rows line up whether or not a row is pointed at or marked.
    local ptr='  ' mark='  '
    (( k == cur )) && ptr=$'\e[31m> \e[0m'
    (( k == marked )) && mark=$'\e[32m> \e[0m'
    if (( k == cur )); then
      f+=("$ptr$mark"$'\e[1m'"${@[k]}"$'\e[0m')
    else
      f+=("$ptr$mark${@[k]}")
    fi
  done
  set -A $out "${f[@]}"
}
cmdline_dim() { print -rn -- $'\e[2m'"$1"$'\e[0m' }

# What makes the move a conflict: the target path already hosted sessions of
# its own — the everyday case for --already-moved, where you kept working in
# the renamed folder before reconciling.
seed_destination_history() {
  seed_project "$fakehome/.claude" "$fakehome/code/foo" 2 6
  seed_profile "$fakehome/.claude" "$fakehome/.claude.json" \
               "$fakehome/code/lipsum" "$fakehome/code/lipsum/api" \
               "$fakehome/code/foo"
}

profiles=(--profile "$fakehome/.claude" --profile "$fakehome/.claude-personal")

# Display-only: the sandbox lives in a tmpdir, so rewrite its path to something
# a reader recognises — both as a path and in Claude's encoded form, so the
# projects/ dir names stay consistent with the paths printed beside them.
fakehome_enc=${fakehome//[^A-Za-z0-9]/-}
DEMO_enc=${DEMO//[^A-Za-z0-9]/-}
demoize() {  # nested ${//} inside a ${//} is a syntax error, hence the two
  local t=$1  # precomputed *_enc above
  t=${t//$fakehome/$DEMO}
  t=${t//$fakehome_enc/$DEMO_enc}
  print -rn -- "$t"
}

# The tool writes an interactive prompt with no trailing newline, and a piped
# stdin is never echoed — so the capture has neither the typed answer nor the
# line break, and the next line runs straight into the prompt. Put both back.
answer() {  # answer <blob> <prompt> <typed>
  # NB: the pattern side of ${//} is a *pattern*, and both prompts carry
  # brackets ([a], [y/N]) that would read as character classes — (b) quotes
  # them, $~ makes that quoting count.
  # The replacement side is a plain string, so $'...' inside the ${//} would
  # land verbatim — build it first.
  local p=$'\e[1m'$2$'\e[0m' pq rep
  pq=${(b)p}
  rep=$p$'\e[1;32m'$3$'\e[0m'$'\n'
  print -rn -- "${1//$~pq/$rep}"
}

# answer(), but only the FIRST occurrence — the extract guide asks for a
# directory twice, and the two prompts are answered separately.
answer1() {  # answer1 <blob> <prompt> <typed>
  local p=$'\e[1m'$2$'\e[0m' pq rep
  pq=${(b)p}
  rep=$p$'\e[1;32m'$3$'\e[0m'$'\n'
  print -rn -- "${1/$~pq/$rep}"
}

cmdline() { print -rn -- $'\e[2m%\e[0m '$'\e[1m'"$1"$'\e[0m' }

# A command line, marked so emit_svg types it out a character at a time rather
# than printing it whole. \x01 cannot occur in captured output, so the marker
# needs no escaping anywhere else. The text after it is the bare command — the
# "% " prompt is drawn by the emitter, since a shell prints that before you
# start typing rather than as part of what you type.
CMD_MARK=$'\x01'
cmdline_typed() { print -rn -- "$CMD_MARK$1" }

# ---- capture the real output ----------------------------------------------
# 1. the happy path, and the hero: one folder, one profile, the four stores it
#    keys. Multi-profile and nested projects are capability, not the everyday
#    case, so they get their own image below rather than the first impression.
seed_simple
move_out=$(demoize "$(${=cm} --profile "$fakehome/.claude" \
                        "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1)")

# 2. the same move where there is more to carry: a second profile, and a
#    nested project of the moved folder's own with separate sessions.
seed
profiles_out=$(demoize "$(${=cm} $profiles "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1)")

# 3. the destination already has history: conflicts are listed, the policy is
#    asked for, and `c` (consolidate) is answered. Both profiles again, so the
#    drawn command line means the same machine as the image above it.
seed; seed_destination_history
conflict_out=$(CLAUDE_MV_FORCE_PROMPT=1 ${=cm} $profiles \
                 "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1 <<< 'c')
conflict_out=$(demoize "$(answer "$conflict_out" 'choice [a]: ' c)")

# 4. rolling one back, end to end. The move that produces the restore point is
#    shown rather than hidden: `overwrite` is the one mode that KEEPS its
#    restore point on success (it is the archive of the history it discarded),
#    which is both why the listing below reads [overwrite] and the only way
#    there is a point to list at all — every other mode cleans its own up.
#    --restore takes no --profile: it returns before profiles are resolved.
seed; seed_destination_history
restore_move=$(demoize "$(${=cm} $profiles --on-conflict overwrite \
                            "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1)")
restore_list=$(demoize "$(${=cm} --restore 2>&1)")
restore_run=$(CLAUDE_MV_FORCE_PROMPT=1 ${=cm} --restore latest 2>&1 <<< 'y')
restore_run=$(demoize "$(answer "$restore_run" 'restore? [y/N]: ' y)")

# 5. moving SESSIONS rather than a folder: the project that was born
#    mid-conversation in ~/code. The whole three-step guide — which folder,
#    which sessions, where to — since that flow IS the feature; the flags a
#    scripted run would use are in the README beside it.
#
#    Both helpers are pinned to their absent form (the no-fzf prompt, the
#    filesystem walk): it is what a machine without them gets, it needs no
#    stub, and it renders identically on a machine that has them.
#
#    Two bare Enters take the offered directories and `1` picks the session
#    that wandered — the paths are passed so the prompts have something to
#    offer, which is also how a real run seeds them.
seed_sessions
sessions_out=$(CLAUDE_MV_FORCE_PROMPT=1 CLAUDE_MV_PICKER=plain \
               CLAUDE_MV_SOURCE=fs ${=cm} --profile "$fakehome/.claude" \
                 --extract "$fakehome/code" "$fakehome/code/lipsum" 2>&1 \
                 <<< $'\n1\n\n')
sessions_out=$(answer1 "$sessions_out" 'directory: ' '')
sessions_out=$(answer  "$sessions_out" 'selection: ' 1)
sessions_out=$(demoize "$(answer1 "$sessions_out" 'directory: ' '')")

# 6. the same step with fzf installed, which is what most people get. The
#    scene above deliberately pins the no-fzf fallback so it stays hermetic and
#    shows the whole flow end to end; this one shows the picker itself, which
#    is the part a reader with fzf would otherwise never see in the README.
#
#    The session rows are lifted from the same seeded profile the scene above
#    used, so the two images agree about what is in ~/code.
typeset -a picker_lines
fzf_frame picker_lines 'session(s) to move > ' \
  'Tab marks · Enter confirms · Esc cancels' 1 3 \
  '2026-08-14 14:05  e6ca43c4  draft a tool that recovers images from the app cache' \
  '2026-08-13 11:52  a97500af  which of these repos still target node 18?' \
  '2026-08-12 09:31  c9e5ce94  compare the two encoder branches' \
  '2026-08-11 08:47  68444cfe  clean up the stale worktrees'
picker_lines=("$(cmdline_typed 'claude-mv --extract')" ''
              "$(demoize "$(print -rn -- $'\e[1msessions homed in \e[0m\e[36m'"$DEMO/code"$'\e[0m\e[2m (4 found)\e[0m')")"
              '' "${picker_lines[@]}")

[[ -n $move_out && -n $profiles_out && -n $conflict_out && -n $restore_move \
   && -n $restore_list && -n $restore_run && -n $sessions_out ]] || {
  print -u2 "generate-readme-svg: sandbox produced no output — aborting"; exit 1 }
[[ $move_out == *"done"* ]] || {
  print -u2 "generate-readme-svg: the move did not succeed — aborting"; exit 1 }
[[ $sessions_out == *"done"* ]] || {
  print -u2 "generate-readme-svg: the session move did not succeed — aborting"; exit 1 }

# ---- SVG ------------------------------------------------------------------
# Catppuccin Mocha chrome + the siblings' ANSI palette, so the four repos'
# images read as one set.
BG='#1e1e2e'  BAR='#181825'  FG='#cdd6f4'  DIMC='#9399b2'
DOT1='#f38ba8' DOT2='#f9e2af' DOT3='#a6e3a1'
typeset -a ANSI_N ANSI_B
ANSI_N=('#000000' '#b43c2a' '#00c200' '#c7c400' '#0225c7' '#ca30c7' '#00c5c7' '#c7c7c7')
ANSI_B=('#686868' '#dd7975' '#58e790' '#ece100' '#6871ff' '#ff77ff' '#60fdff' '#ffffff')
FONT="'Cascadia Code','Fira Code',SFMono-Regular,Consolas,Menlo,monospace"
integer FS=13 LH=20 TH=30 PX=20 PY=14 SLACK=24 MINCOLS=52
local -F REVEAL=1.8      # seconds any image may spend revealing its output
local -F TYPEMAX=2.2     # …and typing its command(s), however long they are
local -F ENTER=0.35      # the beat between the last keystroke and the output
local -F HOLD=4.0        # …then it stands finished this long before replaying

# Terminal grid: every character is pinned to its own cell, so a row occupies
# exactly (columns × cw) whichever font the renderer falls back to — which is
# what keeps the columns aligned in a browser that has none of these fonts.
typeset -a XCOL
local -F cw=7.85
integer k; local v
for (( k = 0; k <= 400; k++ )); do printf -v v '%.2f' $(( PX + k * cw )); XCOL[k+1]=$v; done
xesc() { local s=$1; s=${s//\&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; print -rn -- "$s" }

# Cell width is not code-point count. claude-mv prints three emoji, and a
# terminal draws each of them TWO columns wide — the tool's own padding already
# assumes that, so the grid has to agree or every line carrying one drifts a
# cell left of the lines above it. U+FE0F is the other half of the story: the
# variation selector that turns ⚠ into its colour form takes no cell of its
# own.
#
# Which is why an emoji cannot share a tspan with the text around it: an `x`
# list is positional, so the selector would be handed a coordinate no matter
# how the list is built — and a renderer given a per-glyph position for it
# splits the cluster and falls back to the flat monochrome glyph. Each emoji
# therefore gets a tspan to itself carrying a SINGLE x, so the cluster is
# placed once and shaped naturally, while plain text keeps the per-character
# list that pins the grid.
#
# Results come back through globals (RT, RW) rather than stdout: a $(…) here
# would be a subshell per run, and the second value would be lost.
typeset RT; integer RW
run_tspans() {  # run_tspans <start-col> <sgr-free-run> <tspan-attrs>
  local run=$2 attrs=$3 ch em seg="" xs=""
  integer start=$1 i n=0
  RT=""
  flush() {
    [[ -n $seg ]] || return
    RT+="<tspan x=\"${xs% }\"$attrs>$(xesc "$seg")</tspan>"
    seg=""; xs=""
  }
  for (( i = 1; i <= ${#run}; i++ )); do
    ch=$run[i]
    if [[ $ch == ($'\u2705'|$'\u274c'|$'\u26a0') ]]; then
      flush
      em=$ch
      [[ $run[i+1] == $'\ufe0f' ]] && { em+=$'\ufe0f'; (( i++ )) }
      RT+="<tspan x=\"$XCOL[start+n+1]\"$attrs>$em</tspan>"
      (( n += 2 ))
    else
      seg+=$ch; xs+="$XCOL[start+n+1] "; (( n++ ))
    fi
  done
  flush
  RW=$n
}

# Visible width of a row in cells, ignoring SGR — what the grid must size to.
vlen() {
  local t=${1//$'\e['[0-9;]#m/} ch; integer i n=0
  for (( i = 1; i <= ${#t}; i++ )); do
    ch=$t[i]
    [[ $ch == $'\ufe0f' ]] && continue
    if [[ $ch == ($'\u2705'|$'\u274c'|$'\u26a0') ]]; then (( n += 2 )); else (( n++ )); fi
  done
  print -rn -- $n
}

# One SGR line → <tspan> runs, carrying bold/dim + colour index across the line.
# Every run is pinned to its own columns so alignment survives font fallback.
# Bold maps to BOTH the bright palette entry and font-weight: claude-mv uses
# bold on its own (no colour) to pick out the number or name that makes a line
# worth reading, which a colour-only mapping would render as plain text.
render_ansi() {
  local s=$1 out="" pre tail params pcode attrs
  integer col=0 bold=0 dim=0
  local fill="" cidx=""
  local -a parts
  recompute() {
    if [[ -n $cidx ]]; then (( bold )) && fill=$ANSI_B[cidx+1] || fill=$ANSI_N[cidx+1]
    elif (( dim )); then fill=$DIMC
    else fill=""; fi
  }
  while [[ -n $s ]]; do
    pre=${s%%$'\e'*}
    if [[ -n $pre ]]; then
      attrs="${fill:+ fill=\"$fill\"}"
      (( bold )) && attrs+=' font-weight="700"'
      run_tspans $col "$pre" "$attrs"
      out+=$RT
      (( col += RW ))
    fi
    s=${s[$(( ${#pre} + 1 )),-1]}
    [[ -n $s ]] || break
    if [[ ${s[2]} == '[' ]]; then
      tail=${s#$'\e['}; params=${tail%%m*}
      s=${tail[$(( ${#params} + 2 )),-1]}
      parts=(${(s:;:)params}); (( ${#parts} )) || parts=(0)
      for pcode in $parts; do
        case $pcode in
          0)  bold=0; dim=0; cidx="" ;;
          1)  bold=1 ;;
          2)  dim=1 ;;
          <30-37>) cidx=$(( pcode - 30 )) ;;
          <90-97>) cidx=$(( pcode - 90 )); bold=1 ;;
          39) cidx="" ;;
        esac
      done
      recompute
    else
      s=${s[3,-1]}
    fi
  done
  print -rn -- "$out"
}

# emit_svg <lines-array-name> <out-file> <title> <aria>
# Every entry is a raw terminal line, SGR and all — claude-mv has no porcelain,
# so there is no second kind of row to model.
emit_svg() {
  local -a _lines=("${(@P)1}")
  local out=$2 title=$3 aria=$4 line
  integer maxcols=0 n
  for line in "${_lines[@]}"; do
    n=$(vlen "$line"); (( n > maxcols )) && maxcols=$n
  done
  (( maxcols < MINCOLS )) && maxcols=$MINCOLS
  integer W=$(( PX * 2 + maxcols * cw + 6 + SLACK ))
  integer H=$(( TH + PY + ${#_lines} * LH + PY ))
  {
    print -r -- "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"$W\" height=\"$H\" viewBox=\"0 0 $W $H\" role=\"img\" aria-label=\"$(xesc "$aria")\">"
    print -r -- "  <rect width=\"$W\" height=\"$H\" rx=\"10\" fill=\"$BG\"/>"
    print -r -- "  <rect width=\"$W\" height=\"$TH\" rx=\"10\" fill=\"$BAR\"/>"
    print -r -- "  <rect y=\"$(( TH - 6 ))\" width=\"$W\" height=\"6\" fill=\"$BAR\"/>"
    print -r -- "  <circle cx=\"18\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT1\"/><circle cx=\"36\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT2\"/><circle cx=\"54\" cy=\"$(( TH / 2 ))\" r=\"5.5\" fill=\"$DOT3\"/>"
    print -r -- "  <text x=\"$(( W / 2 ))\" y=\"$(( TH / 2 + 5 ))\" text-anchor=\"middle\" font-family=\"$FONT\" font-size=\"12\" fill=\"$DIMC\">$(xesc "$title")</text>"
    # ---- the reveal ---------------------------------------------------------
    # Lines fade in top to bottom, once, then stay. CSS rather than SMIL or
    # script: GitHub renders a README image through <img>, which runs
    # stylesheets and blocks scripts, so this is the only mechanism that
    # actually animates there.
    #
    # The step shrinks as a screen gets longer, capping every image at REVEAL
    # seconds — a reader who only wants the tally at the bottom should not be
    # made to wait proportionally to how much output the command happened to
    # produce. `both` holds each line hidden until its turn and visible after,
    # and there is no iteration count, so the finished frame is what the image
    # rests on rather than a loop restarting under the reader.
    #
    # Blank lines emit no element but still consume a slot, so the pauses in
    # the pacing are the blank lines in the real output.
    local -F step=$(( REVEAL / ${#_lines} ))
    (( step > 0.06 )) && step=0.06
    # It has to LOOP, and the reason is measurable rather than aesthetic: a
    # browser does not pause a CSS animation inside an offscreen <img>. A
    # run-once reveal on an image below the fold has therefore already
    # finished by the time anyone scrolls to it — verified, an image 3000px
    # down showed fully revealed the instant it came into view. Looping is the
    # only way a reader who did not land at the top ever sees it, and
    # "animate only when in view" is not on the table: that needs scroll
    # awareness, which means script, which <img> does not run.
    #
    # So: one shared cycle per image — the reveal, then a long hold on the
    # finished screen. The hold is what keeps a loop tolerable on a report
    # someone is reading; text stands complete for most of every cycle.
    #
    # Per-line @keyframes rather than one rule with per-line animation-delay,
    # because a delay applies to the FIRST iteration only — with `infinite`
    # the lines would all snap into sync on the second pass and the reveal
    # would never be seen again.
    #
    # step-end, not a fade: a terminal does not dissolve a line into being, it
    # prints it. (The same reasoning ccfind's frame timeline documents — there
    # a cross-fade ghosts one frame through another; here it would just make
    # text that never behaves like text.) Two stops per line, each holding
    # until the next flips it.
    # A running clock rather than index × step, because a command line does not
    # take one slot — it takes a keystroke per character. That is what gives
    # the sequence its shape: the command types, there is a beat for the Enter,
    # then its output arrives.
    integer nl=${#_lines} nchars=0 j
    local plain
    for (( i = 0; i < nl; i++ )); do
      [[ ${_lines[i+1]} == ${CMD_MARK}* ]] && (( nchars += ${#_lines[i+1]} - 1 ))
    done
    # Per-character time, shrunk so no single command spends longer than TYPEMAX
    # on its own. A demo that types at a realistic pace reads as slow, not as
    # authentic.
    local -F tstep=$(( nchars ? TYPEMAX / nchars : 0 ))
    (( tstep > 0.055 )) && tstep=0.055
    local -F clock=0 cycle=0 at
    # First pass: what time does each row land, and how long is the cycle?
    typeset -a AT
    for (( i = 0; i < nl; i++ )); do
      line=${_lines[i+1]}
      AT[i+1]=$clock
      if [[ $line == ${CMD_MARK}* ]]; then
        clock=$(( clock + (${#line} - 1) * tstep + ENTER ))
      else
        clock=$(( clock + step ))
      fi
    done
    cycle=$(( clock + HOLD ))

    print -r -- "  <style>"
    local -F pct
    for (( i = 0; i < nl; i++ )); do
      line=${_lines[i+1]}
      [[ -n $line ]] || continue                   # blanks draw nothing
      at=$AT[i+1]
      if [[ $line == ${CMD_MARK}* ]]; then
        plain=${line#$CMD_MARK}
        # The prompt is already on screen before anything is typed.
        pct=$(( at * 100.0 / cycle ))
        printf '    @keyframes cmvp%d { 0%%{opacity:0} %.3f%%{opacity:1} }\n' $i $pct
        printf '    #p%d { animation: cmvp%d %.2fs step-end infinite }\n' $i $i $cycle
        for (( j = 1; j <= ${#plain}; j++ )); do
          # character j appears on its own keystroke and stays…
          pct=$(( (at + j * tstep) * 100.0 / cycle ))
          printf '    @keyframes cmvc%d_%d { 0%%{opacity:0} %.3f%%{opacity:1} }\n' $i $j $pct
          printf '    #c%d_%d { animation: cmvc%d_%d %.2fs step-end infinite }\n' $i $j $i $j $cycle
        done
        for (( j = 0; j <= ${#plain}; j++ )); do
          # …while the block cursor is alive only for its own position, so it
          # walks along the line and is gone once Enter is pressed.
          local -F c0=$(( (at + j * tstep) * 100.0 / cycle ))
          local -F c1=$(( (at + (j + 1) * tstep) * 100.0 / cycle ))
          (( j == ${#plain} )) && c1=$(( (at + j * tstep + ENTER) * 100.0 / cycle ))
          printf '    @keyframes cmvk%d_%d { 0%%{opacity:0} %.3f%%{opacity:1} %.3f%%{opacity:0} }\n' \
                 $i $j $c0 $c1
          printf '    #k%d_%d { animation: cmvk%d_%d %.2fs step-end infinite }\n' $i $j $i $j $cycle
        done
        continue
      fi
      pct=$(( at * 100.0 / cycle ))
      # NB one stop per switch. Writing two at the same percentage does not
      # work — duplicates collapse to the last declaration.
      printf '    @keyframes cmv%d { 0%%{opacity:0} %.3f%%{opacity:1} }\n' $i $pct
      printf '    #l%d { animation: cmv%d %.2fs step-end infinite }\n' $i $i $cycle
    done
    # Motion is decoration here; the text is the content. Anyone who has asked
    # the OS for less of it gets the finished screen, permanently.
    #
    # A renderer that ignores <style> altogether needs nothing either: with no
    # animation applied these lines are simply opaque, which is the whole
    # screen — the state worth falling back to. (ccfind has to set opacity="0"
    # per frame for this, because its frames stack; a reveal does not.)
    print -r -- "    @media (prefers-reduced-motion: reduce) {"
    print -r -- "      text.l { animation: none !important; opacity: 1 }"
    print -r -- "    }"
    print -r -- "  </style>"
    integer y
    for (( i = 0; i < nl; i++ )); do
      line=${_lines[i+1]}
      y=$(( TH + PY + i * LH + FS ))
      [[ -n $line ]] || continue
      if [[ $line == ${CMD_MARK}* ]]; then
        plain=${line#$CMD_MARK}
        # The prompt, then one element per character, then one per cursor
        # position. Each is a single glyph pinned to its own column, so the
        # line assembles itself in place — no reflow, and none of the
        # duplicated whole-line copies a frame-per-keystroke would cost.
        print -r -- "  <text id=\"p$i\" class=\"l\" x=\"$PX\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" xml:space=\"preserve\" fill=\"$DIMC\">%</text>"
        for (( j = 1; j <= ${#plain}; j++ )); do
          print -r -- "  <text id=\"c${i}_$j\" class=\"l\" x=\"$XCOL[j+2]\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" font-weight=\"700\" xml:space=\"preserve\" fill=\"$FG\">$(xesc "$plain[j]")</text>"
        done
        for (( j = 0; j <= ${#plain}; j++ )); do
          print -r -- "  <text id=\"k${i}_$j\" class=\"l\" x=\"$XCOL[j+3]\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" xml:space=\"preserve\" fill=\"$FG\">█</text>"
        done
        continue
      fi
      print -r -- "  <text id=\"l$i\" class=\"l\" x=\"$PX\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" xml:space=\"preserve\" fill=\"$FG\">$(render_ansi "$line")</text>"
    done
    print -r -- "</svg>"
  } > "$out"
}

# ---- compose ---------------------------------------------------------------
typeset -a move_lines profiles_lines conflict_lines restore_lines sessions_lines
sessions_lines=("$(cmdline_typed 'claude-mv --extract ~/code ~/code/lipsum')" ''
                "${(@f)sessions_out}")
move_lines=("$(cmdline_typed 'claude-mv ~/code/lipsum ~/code/foo')" '' "${(@f)move_out}")
profiles_lines=("$(cmdline_typed 'claude-mv ~/code/lipsum ~/code/foo')" '' "${(@f)profiles_out}")
conflict_lines=("$(cmdline_typed 'claude-mv ~/code/lipsum ~/code/foo')" '' "${(@f)conflict_out}")
restore_lines=("$(cmdline_typed 'claude-mv --on-conflict overwrite ~/code/lipsum ~/code/foo')" ''
               "${(@f)restore_move}" ''
               "$(cmdline_typed 'claude-mv --restore')" '' "${(@f)restore_list}" ''
               "$(cmdline_typed 'claude-mv --restore latest')" '' "${(@f)restore_run}")

# ---- write -----------------------------------------------------------------
MOVE_ARIA='claude-mv moving a folder: a restore point is taken, the folder is moved, and its Claude profile is re-keyed — the project dir renamed, session files rewritten, the config key and history entries updated — closing with a green done line and a tally'
PROFILES_ARIA='the same move on a machine with two Claude profiles and a nested project under the moved folder: both profiles are re-keyed in turn, each reporting its own project dirs, session files, config keys and history entries'
CONFLICT_ARIA='claude-mv finding history already at the destination: the conflicting project dir and config key are listed, four resolution policies are offered, consolidate is chosen, and the merge is reported per store across both profiles'
RESTORE_ARIA='an overwrite move keeping its restore point as the archive of the history it discarded, that point then listed by claude-mv --restore, and finally rolled back: the folder move-back and the number of dirs and files to restore are previewed, confirmed, and reported done'
PICKER_ARIA='the same step on a machine with fzf installed: claude-mv --extract typed at a prompt, the four sessions homed in ~/code listed inside an fzf picker with its prompt, match count and key hints, a pointer on the first row and a Tab marker on a second — the multi-select that lets more than one conversation move at once'
SESSIONS_ARIA='claude-mv moving one session rather than a folder: the four conversations homed in ~/code are listed newest first with their opening prompts, one is picked by number, and only that session — its transcript and its own history entries — is re-homed onto the folder it created, leaving the others where they are'

if [[ -n ${1:-} ]]; then
  emit_svg move_lines     "$1" 'claude-mv' "$MOVE_ARIA";     print "wrote $1"
  [[ -n ${2:-} ]] && { emit_svg profiles_lines "$2" 'claude-mv' "$PROFILES_ARIA"; print "wrote $2" }
  [[ -n ${3:-} ]] && { emit_svg conflict_lines "$3" 'claude-mv' "$CONFLICT_ARIA"; print "wrote $3" }
  [[ -n ${4:-} ]] && { emit_svg restore_lines  "$4" 'claude-mv' "$RESTORE_ARIA";  print "wrote $4" }
  [[ -n ${5:-} ]] && { emit_svg sessions_lines "$5" 'claude-mv' "$SESSIONS_ARIA"; print "wrote $5" }
else
  mkdir -p "$root/assets"
  local old
  for old in "$root"/assets/move-*.svg(N) "$root"/assets/profiles-*.svg(N) \
             "$root"/assets/conflict-*.svg(N) "$root"/assets/restore-*.svg(N) \
             "$root"/assets/sessions-*.svg(N) "$root"/assets/picker-*.svg(N); do
    rm -f "$old"
  done
  local hash; hash=$(xxd -l3 -p /dev/urandom)
  emit_svg move_lines     "$root/assets/move-${hash}.svg"     'claude-mv' "$MOVE_ARIA"
  emit_svg profiles_lines "$root/assets/profiles-${hash}.svg" 'claude-mv' "$PROFILES_ARIA"
  emit_svg conflict_lines "$root/assets/conflict-${hash}.svg" 'claude-mv' "$CONFLICT_ARIA"
  emit_svg restore_lines  "$root/assets/restore-${hash}.svg"  'claude-mv' "$RESTORE_ARIA"
  emit_svg sessions_lines "$root/assets/sessions-${hash}.svg" 'claude-mv' "$SESSIONS_ARIA"
  emit_svg picker_lines   "$root/assets/picker-${hash}.svg"   'claude-mv' "$PICKER_ARIA"
  # `profiles` before `move`: the move pattern would otherwise also match the
  # tail of a profiles-*.svg reference and rewrite it to the wrong name.
  sed -i.bak \
    -e "s|assets/profiles-[^)\"]*\.svg|assets/profiles-${hash}.svg|" \
    -e "s|assets/move-[^)\"]*\.svg|assets/move-${hash}.svg|" \
    -e "s|assets/conflict-[^)\"]*\.svg|assets/conflict-${hash}.svg|" \
    -e "s|assets/restore-[^)\"]*\.svg|assets/restore-${hash}.svg|" \
    -e "s|assets/sessions-[^)\"]*\.svg|assets/sessions-${hash}.svg|" \
    -e "s|assets/picker-[^)\"]*\.svg|assets/picker-${hash}.svg|" \
    "$root/README.md" && rm -f "$root/README.md.bak"
  print "wrote assets/{move,profiles,conflict,restore,sessions,picker}-${hash}.svg and updated README.md"
fi
