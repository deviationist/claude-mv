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
#
#   -n / --dry-run   show the full migration plan without touching anything
#   --force          proceed even if a live Claude session runs inside src
#                    (detected via <profile>/sessions/*.json + pid liveness)
#   --already-moved  the folder was renamed by something else (a plain mv, an
#                    editor, Claude itself) and its history is stranded on the
#                    old path: move nothing, just re-key the history onto the
#                    new path. src must be gone, dst must already exist.
#
# Env (.env beside this script, gitignored — see .env.example):
#   CLAUDE_PROFILE_DIRS          profile dirs to migrate. Default: ~/.claude,
#                                plus ~/.claude-personal when it exists.
#   CLAUDE_MV_OVERWRITE_BACKUP   keep the restore point after a successful
#                                overwrite (default 1; 0 opts out)
#   CLAUDE_MV_RESTORE_ROOT       restore-point dir (default ~/.claude-mv/restore)
#
# Before touching anything, claude-mv snapshots every affected path into a
# restore point; `claude-mv --restore` lists them / rolls one back.
#
# Implementation lives in claude-mv.py (same split as sync-identities:
# python for the structured work, thin zsh wrapper for discoverability).

claude-mv() {
  emulate -L zsh
  # Resolve this function's defining file → the repo root, which holds
  # claude-mv.py, tests/ and the per-machine .env. Found regardless of where
  # the repo is checked out.
  local _dir="${${(%):-%x}:A:h}"
  local -a CLAUDE_PROFILE_DIRS
  local CLAUDE_MV_OVERWRITE_BACKUP CLAUDE_MV_RESTORE_ROOT
  source "${_dir}/.env" 2>/dev/null
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
  env "${envp[@]}" python3 "${_dir}/claude-mv.py" "${prof[@]}" "$@"
}
