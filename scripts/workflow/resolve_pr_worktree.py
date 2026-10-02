#!/usr/bin/env python3
"""
Resolve PR worktree path from git worktree list porcelain output.

Given a PR branch name (e.g., "feature/1234"), search git worktree list --porcelain
output to find the worktree path that has that branch checked out.

Invoked by queued-merge.md Phase 2 and Phase 3 as a shared utility.

Usage:
    PR_WORKTREE=$(PR_HEAD="feature/1234" WORKTREE_LIST="$(git worktree list --porcelain)" python3 scripts/workflow/resolve_pr_worktree.py)
    if [ -z "$PR_WORKTREE" ]; then
      echo "ERROR: worktree not found for PR_HEAD"
      exit 1
    fi
"""

import os
import sys
from typing import Dict, Optional


def main() -> None:
    pr_head = os.environ.get("PR_HEAD", "")
    worktree_list = os.environ.get("WORKTREE_LIST", "")

    if not pr_head:
        print("ERROR: PR_HEAD environment variable not set", file=sys.stderr)
        sys.exit(1)

    lines = worktree_list.strip().split("\n")

    current_record: Dict[str, str] = {}
    found: Optional[str] = None
    for line in lines:
        if not line.strip():
            # End of record
            if current_record and current_record.get("branch") == f"refs/heads/{pr_head}":
                found = current_record["worktree"]
                break
            current_record = {}
        else:
            parts = line.split(None, 1)
            if len(parts) >= 2:
                if parts[0] == "worktree":
                    current_record["worktree"] = parts[1]
                elif parts[0] == "branch":
                    current_record["branch"] = parts[1]

    # Check final record, in case the porcelain output doesn't end with a blank line
    if found is None and current_record and current_record.get("branch") == f"refs/heads/{pr_head}":
        found = current_record["worktree"]

    if found:
        print(found)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
