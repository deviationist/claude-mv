# claude-mv — mv a directory AND migrate its Claude Code history with it.
#
# Renaming a project folder normally orphans its Claude Code history: the
# ~/.claude/projects/<encoded-cwd> dir, the project entry in ~/.claude.json,
# and the history.jsonl prompt entries all stay keyed on the old path, so
# `claude --resume` in the new location finds nothing. claude-mv does the
# regular `mv` and re-keys all of it — including project dirs nested under
# the moved folder (monorepo subdirs with their own sessions) — across every
# configured profile dir.
#
# Usage:  claude-mv [-n|--dry-run] [--force] [--already-moved] <src-dir> <dst>
#         claude-mv --extract [--session <id>] <src-dir> <dst>
#
#   -n / --dry-run   show the full migration plan without touching anything
#   --force          proceed even if a live Claude session runs inside src
#                    (detected via <profile>/sessions/*.json + pid liveness)
#   --already-moved  the folder was renamed by something else (a plain mv, an
#                    editor, Claude itself) and its history is stranded on the
#                    old path: move nothing, just re-key the history onto the
#                    new path. src must be gone, dst must already exist.
#   --extract        pull individual SESSIONS out of src's history and re-key
#                    them onto dst, rather than moving a folder: for the
#                    project that was born mid-session in a parent directory
#                    (told Claude to mkdir and cd), leaving its history keyed
#                    on the parent. Pick from the sessions homed in src, and
#                    only those move — src keeps the rest. src defaults to the
#                    cwd; dst is asked for when not given, never guessed.
#
# Env (.env beside this script, gitignored — see .env.example):
#   CLAUDE_PROFILE_DIRS          profile dirs to migrate. Unset, claude-mv
#                                asks claude-profile when that is installed,
#                                and otherwise uses ~/.claude plus
#                                ~/.claude-personal when it exists.
#   CLAUDE_PROFILE_SCRIPT        (env, not .env) where claude-profile.py lives,
#                                when it is neither on PATH nor a sibling
#                                clone. Same override claude-usage honours.
#   CLAUDE_MV_CCFIND_SCRIPT      (env, not .env — like CLAUDE_PROFILE_SCRIPT,
#                                and authoritative the same way) where
#                                ccfind.zsh lives, when it is neither a loaded
#                                function nor a sibling clone. --extract uses
#                                ccfind to search transcripts; without it the
#                                sessions are listed straight off disk.
#   CLAUDE_MV_SOURCE             (env) ccfind|fs|auto — force the session
#                                source rather than detecting it
#   CLAUDE_MV_PICKER             (env) fzf|plain|auto — force the session picker
#   CLAUDE_MV_OVERWRITE_BACKUP   keep the restore point after a successful
#                                overwrite (default 1; 0 opts out)
#   CLAUDE_MV_RESTORE_ROOT       restore-point dir (default ~/.claude-mv/restore)
#
# Before touching anything, claude-mv snapshots every affected path into a
# restore point; `claude-mv --restore` lists them / rolls one back.
#
# Implementation lives in claude-mv.py (same split as sync-identities:
# python for the structured work, thin zsh wrapper for discoverability).

# Where this file lives, captured at SOURCE time. The repo root: it holds
# claude-mv.py, tests/ and the per-machine .env, and is the anchor for the
# sibling claude-profile clone below. Found regardless of where the repo is
# checked out.
typeset -g CLAUDE_MV_SELF_DIR="${${(%):-%x}:A:h}"

# ----------------------------------------------------------------------------
# Internal: invoke claude-profile, wherever it lives. Same three candidates in
# the same order as claude-usage's bridge, so no two tools in the family can
# disagree about which claude-profile they are talking to:
#   $CLAUDE_PROFILE_SCRIPT override → function/binary on PATH → sibling clone.
# `command -v` rather than a $commands lookup because claude-profile is a zsh
# FUNCTION in an interactive shell, not a binary. An explicit
# $CLAUDE_PROFILE_SCRIPT is authoritative: if the user says where it lives and
# it is not there, it is not installed, and the other candidates are not tried.
# Prints nothing and returns 1 when claude-profile isn't installed at all.
# ----------------------------------------------------------------------------
_claude_mv_profile_cmd() {
  if [[ -n ${CLAUDE_PROFILE_SCRIPT:-} ]]; then
    [[ -f $CLAUDE_PROFILE_SCRIPT ]] || return 1
    command python3 "$CLAUDE_PROFILE_SCRIPT" "$@"
    return $?
  fi
  if command -v claude-profile >/dev/null 2>&1; then
    claude-profile "$@"
    return $?
  fi
  local sibling="$CLAUDE_MV_SELF_DIR/../claude-profile/claude-profile.py"
  [[ -f $sibling ]] || return 1
  command python3 "$sibling" "$@"
}

# ----------------------------------------------------------------------------
# Internal: the machine's Claude config dirs according to claude-profile, one
# per line; nothing at all when it is not installed or cannot answer.
#
# Where that tool is present it is the registry of config dirs, so a profile
# added there is migrated here with no second edit — the drift that maintaining
# two lists invites is the whole reason to ask. It stays strictly optional:
# every failure path falls through to the built-in default, and setting
# CLAUDE_PROFILE_DIRS bypasses it outright.
#
# `list` is documented porcelain — name<TAB>dir<TAB>active, dir tilde-contracted
# — and side-effect free: claude-profile's wrapper dispatches it straight to
# the python, and its only post-command hook is gated to account/toggle/rotate.
# ----------------------------------------------------------------------------
_claude_mv_profile_dirs() {
  local -a rows
  rows=(${(f)"$(_claude_mv_profile_cmd list 2>/dev/null)"}) || return 0
  (( ${#rows[@]} )) || return 0
  # column 2 of each row, with `list`'s tilde-contraction undone
  print -rl -- "${(@)${(@)${(@)rows#*$'\t'}%%$'\t'*}/#\~/$HOME}"
}

# ----------------------------------------------------------------------------
# Internal: where ccfind lives, as a path the python can source. Prints nothing
# and returns 1 when ccfind isn't installed.
#
# --extract uses ccfind to search transcript bodies (see the python's
# find_sessions). The awkward part is that ccfind is a zsh FUNCTION in an
# interactive shell, so there is usually nothing on PATH to exec and the python
# cannot call it at all — but zsh records where a function was defined in
# $functions_source, which hands us the script to source. Same candidate order
# and same soft contract as the claude-profile bridge above: an explicit
# override is authoritative, every other failure falls through, and with no
# ccfind at all the python walks the filesystem instead.
# ----------------------------------------------------------------------------
_claude_mv_ccfind_source() {
  if [[ -n ${CLAUDE_MV_CCFIND_SCRIPT:-} ]]; then
    [[ -f $CLAUDE_MV_CCFIND_SCRIPT ]] || return 1
    print -r -- "$CLAUDE_MV_CCFIND_SCRIPT"
    return 0
  fi
  # A function we can see: ask zsh which file defined it.
  if (( ${+functions[ccfind]} )) && [[ -f ${functions_source[ccfind]:-} ]]; then
    print -r -- "${functions_source[ccfind]}"
    return 0
  fi
  local sibling="$CLAUDE_MV_SELF_DIR/../ccfind/ccfind.zsh"
  [[ -f $sibling ]] || return 1
  print -r -- "${sibling:A}"
}

claude-mv() {
  emulate -L zsh
  local _dir="$CLAUDE_MV_SELF_DIR"
  local -a CLAUDE_PROFILE_DIRS
  local CLAUDE_MV_OVERWRITE_BACKUP CLAUDE_MV_RESTORE_ROOT
  source "${_dir}/.env" 2>/dev/null
  # Profile dirs, most specific source first:
  #   1. CLAUDE_PROFILE_DIRS from .env, always wins. (.env and not the
  #      environment: it is an array, which zsh cannot export, and the `local
  #      -a` above deliberately blanks any ambient value so nothing leaks
  #      either way.)
  #   2. claude-profile, when installed — see _claude_mv_profile_dirs
  #   3. ~/.claude, plus ~/.claude-personal when it exists
  if (( ! ${#CLAUDE_PROFILE_DIRS[@]} )); then
    CLAUDE_PROFILE_DIRS=($(_claude_mv_profile_dirs))
  fi
  if (( ! ${#CLAUDE_PROFILE_DIRS[@]} )); then
    CLAUDE_PROFILE_DIRS=("$HOME/.claude")
    [[ -d "$HOME/.claude-personal" ]] && CLAUDE_PROFILE_DIRS+=("$HOME/.claude-personal")
  fi

  local -a prof
  local p
  for p in "${CLAUDE_PROFILE_DIRS[@]}"; do
    [[ -d "$p" ]] && prof+=(--profile "$p")
  done
  if (( ! ${#prof[@]} )); then
    print -u2 "claude-mv: none of the configured profile dirs exist (${CLAUDE_PROFILE_DIRS[*]})"
    return 1
  fi

  local -a envp
  [[ -n "$CLAUDE_MV_OVERWRITE_BACKUP" ]] && envp+=("CLAUDE_MV_OVERWRITE_BACKUP=$CLAUDE_MV_OVERWRITE_BACKUP")
  [[ -n "$CLAUDE_MV_RESTORE_ROOT" ]] && envp+=("CLAUDE_MV_RESTORE_ROOT=$CLAUDE_MV_RESTORE_ROOT")
  # Resolved here, not in the python: only this shell can see whether ccfind
  # is a function, and only zsh knows where that function came from.
  local _ccfind
  if _ccfind=$(_claude_mv_ccfind_source); then
    envp+=("CLAUDE_MV_CCFIND_SOURCE=$_ccfind")
  fi
  env "${envp[@]}" python3 "${_dir}/claude-mv.py" "${prof[@]}" "$@"
}
