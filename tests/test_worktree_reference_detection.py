#!/usr/bin/env python3
"""Structural test: the Project Detection bash block keeps the worktree-parent safety rules.

Run with: python3 tests/test_worktree_reference_detection.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

text = (REPO_ROOT / "prompts" / "worktree-reference.md").read_text()
section = text[text.index("## Project Detection"):text.index("## Stack Detection")]
block = re.search(r"```bash\n(.*?)```", section, re.S).group(1)
step3 = block[block.index("# 3."):block.index("# 4.")]

h = Harness("WORKTREE REFERENCE DETECTION STRUCTURE")
t = h.test_result

canon = step3.index('= "worktrees"')
t("canonical-layout rule precedes the candidate loop", canon < step3.index("read -r"))
t("block skips $HOME/.claude/ entries", '"$HOME/.claude/"*' in step3)
t("block skips entries nested under main", '"${MAIN_WORKTREE}/"*' in step3)
t("block skips type-prefixed parents", "feature|fix|chore" in step3)
t("block falls back to ${MAIN_WORKTREE}/worktrees", '"${MAIN_WORKTREE}/worktrees"' in step3)
t("block fails closed on unsafe parent", "unsafe WORKTREE_PARENT" in step3 and "exit 1" in step3)
t("block avoids bash arrays", "=(" not in step3 and "mapfile" not in step3)

# Behavioural: run step 3 under sh with a stubbed `git` and fake HOME.
def run_step3(main, entries, home):
    stub = "git() { printf '%s\\n' " + " ".join(f"'worktree {e}'" for e in [main] + entries) + "; }\n"
    script = stub + f"HOME='{home}'\nMAIN_WORKTREE='{main}'\n" + step3 + 'echo "$WORKTREE_PARENT"\n'
    with tempfile.TemporaryDirectory() as d:
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=d)

r = run_step3("/r/worktrees/main", ["/h/.claude/merge-queue-scratch/ab/scratch"], "/h")
t("bash: canonical layout wins over scratch", r.stdout.strip() == "/r/worktrees", r.stderr)
r = run_step3("/r/main", ["/h/.claude/merge-queue-scratch/ab/scratch", "/r/main/worktrees/1-x", "/r/sib/2-y"], "/h")
t("bash: scratch and nested entries skipped", r.stdout.strip() == "/r/sib", r.stderr)
r = run_step3("/r", [], "/h")
t("bash: plain clone fallback", r.stdout.strip() == "/r/worktrees", r.stderr)
r = run_step3("/h/.claude/x", [], "/h")
t("bash: fail-closed exits nonzero", r.returncode != 0 and "unsafe" in r.stderr, r.stderr)

h.summarize_and_exit()
