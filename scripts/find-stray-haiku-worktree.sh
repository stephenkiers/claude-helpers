#!/usr/bin/env bash

# Find stray haiku worktrees for a given branch.
#
# Usage: find-stray-haiku-worktree.sh <branch-name>
#
# Prints matching worktree paths to stdout (one per line).
# Returns 0 if worktree(s) found, 1 if none found or branch is empty.

BRANCH="$1"

if [[ -z "$BRANCH" ]]; then
  echo "Error: branch name required" >&2
  exit 1
fi

git worktree list --porcelain 2>/dev/null | awk '/^worktree / {print $2}' | while read -r wt; do
  bn=$(git -C "$wt" branch --show-current 2>/dev/null || true)
  case "$bn" in
    "${BRANCH}-haiku-"*) echo "$wt" ;;
  esac
done
