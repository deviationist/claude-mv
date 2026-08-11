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
#     report — so all three images are plain SGR capture. There is no
#     screen-scraping and no stubbed dependency; the tool needs neither.
#   * bold is rendered as font-weight, not just the bright palette. claude-mv
#     leans on bold to pick out the identifier in a line (`did rewrite cwd in
#     **3** session file(s)`), which the siblings' colour-only mapping would
#     silently flatten.
#   * the three emoji claude-mv prints need real cell widths and whole
#     grapheme clusters, neither of which the siblings' grid models — see
#     run_tspans() below.
#   * two screens end in an interactive prompt. The tool writes the prompt
#     without a trailing newline and a piped stdin is never echoed, so the
#     answer and the line break are missing from the capture; `answer()` puts
#     both back. Those keystrokes are the only characters in these images that
#     claude-mv did not itself emit.
#
# The sandbox lives under a tmpdir, so its paths are long and machine-specific.
# They are rewritten for display only — /Users/demo, plus the same rewrite
# applied to Claude's encoded form of the path, so `projects/` dir names stay
# consistent with the paths beside them. Nothing else is touched.
#
# Usage:  zsh tools/generate-readme-svg.zsh
#           → assets/{move,conflict,restore}-<hash>.svg, older ones deleted,
#             README <img> references rewritten (the random hash busts GitHub's
#             camo image cache). Commit all three files.
#         zsh tools/generate-readme-svg.zsh MOVE.svg CONFLICT.svg RESTORE.svg
#           → fixed paths, README untouched.
#
# Regenerate whenever the migration report, the conflict prompt or the restore
# screen changes. Restore-point stamps are real timestamps, so they track the
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

cmdline() { print -rn -- $'\e[2m%\e[0m '$'\e[1m'"$1"$'\e[0m' }

# ---- capture the real output ----------------------------------------------
# 1. the headline: a clean move across both profiles, nested project included.
seed
move_out=$(demoize "$(${=cm} $profiles "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1)")

# 2. the destination already has history: conflicts are listed, the policy is
#    asked for, and `c` (consolidate) is answered. Both profiles again, so the
#    drawn command line means the same machine as the image above it.
seed; seed_destination_history
conflict_out=$(CLAUDE_MV_FORCE_PROMPT=1 ${=cm} $profiles \
                 "$fakehome/code/lipsum" "$fakehome/code/foo" 2>&1 <<< 'c')
conflict_out=$(demoize "$(answer "$conflict_out" 'choice [a]: ' c)")

# 3. rolling one back, end to end. The move that produces the restore point is
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

[[ -n $move_out && -n $conflict_out && -n $restore_move && -n $restore_list \
   && -n $restore_run ]] || {
  print -u2 "generate-readme-svg: sandbox produced no output — aborting"; exit 1 }
[[ $move_out == *"done"* ]] || {
  print -u2 "generate-readme-svg: the move did not succeed — aborting"; exit 1 }

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
    integer i=0 y
    for line in "${_lines[@]}"; do
      y=$(( TH + PY + i * LH + FS ))
      [[ -n $line ]] && print -r -- "  <text x=\"$PX\" y=\"$y\" font-family=\"$FONT\" font-size=\"$FS\" xml:space=\"preserve\" fill=\"$FG\">$(render_ansi "$line")</text>"
      (( i++ ))
    done
    print -r -- "</svg>"
  } > "$out"
}

# ---- compose ---------------------------------------------------------------
typeset -a move_lines conflict_lines restore_lines
move_lines=("$(cmdline 'claude-mv ~/code/lipsum ~/code/foo')" '' "${(@f)move_out}")
conflict_lines=("$(cmdline 'claude-mv ~/code/lipsum ~/code/foo')" '' "${(@f)conflict_out}")
restore_lines=("$(cmdline 'claude-mv --on-conflict overwrite ~/code/lipsum ~/code/foo')" ''
               "${(@f)restore_move}" ''
               "$(cmdline 'claude-mv --restore')" '' "${(@f)restore_list}" ''
               "$(cmdline 'claude-mv --restore latest')" '' "${(@f)restore_run}")

# ---- write -----------------------------------------------------------------
MOVE_ARIA='claude-mv moving a folder: a restore point is taken, the folder is moved, then each Claude profile is re-keyed in turn — project dirs renamed, session files rewritten, config keys and history entries updated — closing with a green done line and a tally'
CONFLICT_ARIA='claude-mv finding history already at the destination: the conflicting project dir and config key are listed, four resolution policies are offered, consolidate is chosen, and the merge is reported per store across both profiles'
RESTORE_ARIA='an overwrite move keeping its restore point as the archive of the history it discarded, that point then listed by claude-mv --restore, and finally rolled back: the folder move-back and the number of dirs and files to restore are previewed, confirmed, and reported done'

if [[ -n ${1:-} ]]; then
  emit_svg move_lines     "$1" 'claude-mv' "$MOVE_ARIA";     print "wrote $1"
  [[ -n ${2:-} ]] && { emit_svg conflict_lines "$2" 'claude-mv' "$CONFLICT_ARIA"; print "wrote $2" }
  [[ -n ${3:-} ]] && { emit_svg restore_lines  "$3" 'claude-mv' "$RESTORE_ARIA";  print "wrote $3" }
else
  mkdir -p "$root/assets"
  local old
  for old in "$root"/assets/move-*.svg(N) "$root"/assets/conflict-*.svg(N) \
             "$root"/assets/restore-*.svg(N); do rm -f "$old"; done
  local hash; hash=$(xxd -l3 -p /dev/urandom)
  emit_svg move_lines     "$root/assets/move-${hash}.svg"     'claude-mv' "$MOVE_ARIA"
  emit_svg conflict_lines "$root/assets/conflict-${hash}.svg" 'claude-mv' "$CONFLICT_ARIA"
  emit_svg restore_lines  "$root/assets/restore-${hash}.svg"  'claude-mv' "$RESTORE_ARIA"
  sed -i.bak \
    -e "s|assets/move-[^)\"]*\.svg|assets/move-${hash}.svg|" \
    -e "s|assets/conflict-[^)\"]*\.svg|assets/conflict-${hash}.svg|" \
    -e "s|assets/restore-[^)\"]*\.svg|assets/restore-${hash}.svg|" \
    "$root/README.md" && rm -f "$root/README.md.bak"
  print "wrote assets/{move,conflict,restore}-${hash}.svg and updated README.md"
fi
