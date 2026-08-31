#!/usr/bin/env bash

# Resolve CLAUDE_HELPERS_DIR from the symlink location of this script.
#
# This script lives at <repo>/scripts/resolve-claude-helpers-dir.sh. When invoked from
# ~/.claude/scripts/resolve-claude-helpers-dir.sh (a symlink installed by /setup-local),
# readlink -f resolves to the real <repo>/scripts/resolve-claude-helpers-dir.sh path.
# Two dirname calls from there yield the <repo> root, which is the installed canonical
# checkout that /setup-local last symlinked — conventionally main, not any worktree.
# That's intentional: commands that source this run the installed CLI by design, not
# an in-progress feature-branch copy.

RESOLVE_SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
RESOLVE_EXIT=$?

if [ $RESOLVE_EXIT -ne 0 ] || [ -z "$RESOLVE_SCRIPT_PATH" ]; then
  echo "ERROR: could not resolve ~/.claude/scripts/resolve-claude-helpers-dir.sh — run /setup-local to (re)install claude-helpers symlinks" >&2
  return 1
fi

export CLAUDE_HELPERS_DIR="$(dirname "$(dirname "$RESOLVE_SCRIPT_PATH")")"
