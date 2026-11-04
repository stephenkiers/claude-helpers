#!/usr/bin/env python3
"""SPLIT_BASIS and UNITS verification for scripts/flow-verify-receipt.py, through its CLI only.

A bare `SPLIT_BASIS: single` is not an accepted basis; the single basis must carry a non-empty
reason as `single(<reason>)`. `UNITS` must be an integer compared numerically against MIN_UNITS
(so 10 satisfies 9), and a non-integer or below-minimum UNITS is rejected. Each rule is paired with
an accepted control receipt so that a rejection for the wrong reason is caught.

Run with: python3 tests/test_flow_verify_receipt_split_basis.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT
from _inproc import run_python, RepoTemplate

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SINCE = "2026-10-13T00:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def git(repo, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _build_split_basis_repo(root):
    """Build base repo with initial and feature commits for template."""
    git_obj = git
    git_obj(str(root), "init", "-q")
    Path(f"{root}/initial.txt").write_text("initial\n")
    git_obj(str(root), "add", "initial.txt")
    git_obj(str(root), "commit", "-q", "-m", "initial")
    base_sha = git_obj(str(root), "rev-parse", "HEAD")
    Path(f"{root}/feature.txt").write_text("feature\n")
    git_obj(str(root), "add", "feature.txt")
    git_obj(str(root), "commit", "-q", "-m", "add feature")
    head_sha = git_obj(str(root), "rev-parse", "HEAD")
    return base_sha, head_sha


_SPLIT_BASIS_REPO = RepoTemplate(_build_split_basis_repo)


def make_repo(root):
    """Repo with a base commit and one feature commit. Returns (repo, base_sha, head_sha)."""
    repo_path = Path(root) / "repo"
    _SPLIT_BASIS_REPO.copy(repo_path)
    base_sha, head_sha = _SPLIT_BASIS_REPO.meta
    return str(repo_path), base_sha, head_sha


def write_cache(repo):
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))


def receipt(head_sha, split_line, min_units="1", units="1"):
    """An otherwise-complete OK implement receipt; only the split-basis fields vary."""
    return (
        "ROUND 1\n"
        "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
        f"CHECK_SHA: {head_sha}\n"
        f"{split_line}\n"
        f"MIN_UNITS: {min_units}\nPRIOR_SPLIT_BASIS: none\nUNITS: {units}\n"
        "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
    )


def verify(home, repo, base_sha, text):
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = home
    env["FLOW_VERIFY_GIT_TIMEOUT_SECS"] = "60"
    cmd = [str(VERIFIER), "--step", "implement", "--receipt", path, "--since", SINCE,
           "--repo", repo, "--branch", "HEAD", "--base", base_sha]
    result = run_python(cmd, env=env)
    return result.returncode, (result.stdout + result.stderr)


def verify_e2e(home, repo, base_sha, text):
    """End-to-end subprocess call for CLI coverage."""
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = home
    cmd = [sys.executable, str(VERIFIER), "--step", "implement", "--receipt", path, "--since", SINCE,
           "--repo", repo, "--branch", "HEAD", "--base", base_sha]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, (result.stdout + result.stderr)


def main():
    h = Harness("FLOW VERIFY RECEIPT SPLIT_BASIS AND UNITS")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base_sha, head_sha = make_repo(tmp)
        write_cache(repo)

        print("[SPLIT_BASIS] the single basis requires a reason")
        rc, out = verify_e2e(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: parallel"))
        h.test_result("control: SPLIT_BASIS: parallel is accepted", rc == 0 and "ARTIFACTS: ok" in out, f"rc={rc} out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: single(coupled files)"))
        h.test_result("SPLIT_BASIS: single(<reason>) is accepted", rc == 0 and "ARTIFACTS: ok" in out,
                      f"rc={rc} out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: single"))
        h.test_result("bare SPLIT_BASIS: single is rejected", rc != 0 and "ARTIFACTS: ok" not in out,
                      f"rc={rc} out={out[:300]!r}")
        h.test_result("bare single rejection names the single(<reason>) form",
                      "single(<reason>)" in out, f"out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: single()"))
        h.test_result("SPLIT_BASIS: single() with an empty reason is rejected",
                      rc != 0 and "ARTIFACTS: ok" not in out and "reason" in out, f"rc={rc} out={out[:300]!r}")

        print("[UNITS] integer, numerically compared against MIN_UNITS")
        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: parallel", min_units="9", units="10"))
        h.test_result("UNITS 10 satisfies MIN_UNITS 9 (numeric, not lexical, compare)",
                      rc == 0 and "ARTIFACTS: ok" in out, f"rc={rc} out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: parallel", min_units="2", units="1"))
        h.test_result("UNITS below MIN_UNITS is rejected",
                      rc != 0 and "cannot be less than" in out, f"rc={rc} out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: parallel", min_units="1", units="abc"))
        h.test_result("non-integer UNITS 'abc' is rejected as invalid",
                      rc != 0 and "must be integer" in out and "ARTIFACTS: ok" not in out, f"rc={rc} out={out[:300]!r}")

        rc, out = verify(tmp, repo, base_sha, receipt(head_sha, "SPLIT_BASIS: parallel", min_units="1", units="1.5"))
        h.test_result("fractional UNITS '1.5' is rejected as invalid",
                      rc != 0 and "must be integer" in out, f"rc={rc} out={out[:300]!r}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
