#!/usr/bin/env python3
"""
Phase 4-Q stop paths of commands/merge-and-cleanup.md, executed (#359, plan Step 6).

flow-merge relies on a queue kickback, refusal, or unusable outcome ending the step with a
nonzero exit, a FAILED receipt Reason, and the worktree left intact (never reaching /cleanup).
This extracts the real Phase 4-Q bash block, stubs only `gh`, and drives it with fixture
result files.

Exit codes pinned: kickback 2, pushed_not_merged 2, refused 3, and a missing / unparsable /
unknown outcome 1 (its own case; it does NOT collapse into the refusal exit 3).

Run with: python3 tests/test_merge_queue_stop_paths.py
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

DOC = (REPO_ROOT / "commands" / "merge-and-cleanup.md").read_text()
# Unique PR number per process to avoid collision in parallel test runs
PR_NUM = f"{os.getpid()}"


def phase_4q_block():
    start = DOC.index("### Phase 4-Q")
    m = re.search(r"```bash\n(.*?)\n```", DOC[start:], re.DOTALL)
    assert m, "Phase 4-Q bash block not found"
    return m.group(1).replace("PR_NUM=<PR number from Phase 1>", f"PR_NUM={PR_NUM}")


def run_case(result, pr_state):
    """result: None (no file), a str (raw file body) or a dict. Returns (rc, out, wt_intact)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wt = tmp / "wt"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", str(wt)], check=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\nprintf '%s\\n' '{pr_state}'\n")
        gh.chmod(0o755)

        state = Path(f"/tmp/merge-and-cleanup.pr-{PR_NUM}")
        state.mkdir(exist_ok=True)
        started = time.time() - 60
        (state / "pr_num").write_text(PR_NUM)
        (state / "route").write_text("queue")
        (state / "wt").write_text(str(wt))
        (state / "queue_started_at").write_text(str(started))
        try:
            qdir = wt / ".git" / "merge-queue"
            qdir.mkdir()
            if result is not None:
                body = result if isinstance(result, str) else json.dumps({**result, "timestamp": time.time()})
                (qdir / f"result-{PR_NUM}.json").write_text(body)
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
            proc = subprocess.run(["bash", "-c", phase_4q_block()], capture_output=True, text=True,
                                  env=env, timeout=30)
            return proc.returncode, proc.stdout + proc.stderr, wt.exists()
        finally:
            shutil.rmtree(state, ignore_errors=True)


def main():
    h = Harness("MERGE QUEUE STOP PATHS (Phase 4-Q, #359)")

    cases = [
        ("kickback", {"outcome": "kickback", "reason": "gate failed"}, 2),
        ("pushed_not_merged", {"outcome": "pushed_not_merged", "reason": "push landed"}, 2),
        ("refused", {"outcome": "refused", "reason": "dirty base"}, 3),
        ("unknown outcome string", {"outcome": "something-new"}, 1),
        ("unparsable result file", "{not json", 1),
        ("missing result file", None, 1),
    ]
    for label, result, want in cases:
        rc, out, intact = run_case(result, "OPEN")
        h.test_result(f"{label}: exits {want}", rc == want, f"rc={rc}\n{out[-400:]}")
        h.test_result(f"{label}: /cleanup not reached on stop paths",
                      "invoking /cleanup" not in out, out[-400:])

    rc, out, _ = run_case({"outcome": "merged"}, "MERGED")
    h.test_result("merged: exits 0 and proceeds to /cleanup", rc == 0 and "invoking /cleanup" in out,
                  f"rc={rc}\n{out[-400:]}")

    # The receipt template documents each stop with a FAILED Reason.
    tail = DOC[DOC.index("`Decision: OK` only when the PR is merged"):]
    for needle in ("queue kickback (exit 2)", "queue refused (exit 3)",
                   "queue outcome unknown (exit 1)"):
        h.test_result(f"receipt Reason template names '{needle}'", needle in tail)
    h.test_result("doc states unknown outcome is its own case, not a refusal",
                  "its own case (exit 1, not a refusal)" in tail)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
