#!/usr/bin/env python3
"""Structural test: worktree-parent detection has ONE implementation (the Python), and the
Project Detection bash block in prompts/worktree-reference.md only calls it.

Run with: python3 tests/test_worktree_reference_detection.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

DOC = REPO_ROOT / "prompts" / "worktree-reference.md"
text = DOC.read_text()
section = text[text.index("## Project Detection"):text.index("## Stack Detection")]
block = re.search(r"```bash\n(.*?)```", section, re.S).group(1)
step3 = block[block.index("# 3."):block.index("# 4.")]

h = Harness("WORKTREE REFERENCE DETECTION STRUCTURE")
t = h.test_result

t("block calls the detect-parent CLI",
  "scripts.workflow.cli worktree detect-parent" in step3)
t("block fails closed when the CLI fails", "exit 1" in step3)
t("block does not re-implement the heuristic (no candidate loop / type-prefix case)",
  "read -r" not in step3 and "feature|fix|chore" not in step3 and "CANDIDATE" not in step3)
t("block keeps no worktree-list parsing in step 3", "git worktree list" not in step3)
t("no other doc restates the type-prefix case pattern",
  not any("feature|fix|chore" in p.read_text()
          for p in list((REPO_ROOT / "commands").glob("*.md")) + list((REPO_ROOT / "prompts").glob("*.md"))))

# Behavioural: the CLI the doc calls prints the sibling parent and fails closed.
with tempfile.TemporaryDirectory() as d:
    root = Path(d).resolve()
    main = root / "repo" / "worktrees" / "main"
    main.mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(root / "home"),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(cmd, cwd=main, env=env, check=True, capture_output=True)
    run = lambda cwd: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "scripts.workflow.cli", "worktree", "detect-parent", "--cwd", str(cwd)],
        cwd=REPO_ROOT, env={**env, "PYTHONPATH": str(REPO_ROOT)}, capture_output=True, text=True)
    r = run(main)
    t("CLI: canonical layout prints the worktrees dir", r.returncode == 0 and r.stdout.strip() == str(main.parent), r.stderr)
    r = run(root / "not-a-repo")
    t("CLI: git failure exits 1 with ERROR on stderr and empty stdout",
      r.returncode == 1 and r.stdout == "" and r.stderr.startswith("ERROR:"), r.stderr)

h.summarize_and_exit()
