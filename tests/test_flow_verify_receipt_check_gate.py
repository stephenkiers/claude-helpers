#!/usr/bin/env python3
"""
Check-gate fixtures for scripts/flow-verify-receipt.py (#338).

An OK implement/fix receipt must carry a full-check outcome that names the configured check
command, passed, and was run against the final HEAD. A receipt that claims a pass without those
CHECK lines is rejected; so is a substituted command, a failing exit, a stale or missing
CHECK_SHA, and an opt-out the resolver does not confirm.

Each rule is exercised with a passing fixture and a failing fixture so that the rule's
positive and negative behaviour are both pinned. Negative cases assert the rejection reason,
not only the exit code, so a rejection for the wrong reason fails the test.

Rule letters follow the verifier's comments in scripts/flow-verify-receipt.py `_check_gate_reason`:
(a) CHECK lines present, (b) command matches the resolver, (c) the check passed, (d) an opt-out is
resolver-confirmed, (e) an OK receipt with no command and no opt-out is rejected, (f) CHECK_SHA is
the final HEAD.

Run with: python3 tests/test_flow_verify_receipt_check_gate.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SINCE = "2026-10-13T00:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(root: str) -> tuple:
    """A git repo with an initial commit and one feature commit. Returns (repo, base_sha, head_sha)."""
    repo = f"{root}/repo"
    Path(repo).mkdir(parents=True)
    git(repo, "init", "-q")
    Path(f"{repo}/initial.txt").write_text("initial\n")
    git(repo, "add", "initial.txt")
    git(repo, "commit", "-q", "-m", "initial")
    base_sha = git(repo, "rev-parse", "HEAD")
    Path(f"{repo}/feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "add feature")
    head_sha = git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def write_cache(repo: str, command: str = "just check") -> None:
    """Configure commands.check in the repo's repo-cache so the resolver finds a command."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": command}}))


def write_opt_out(repo: str, reason: str = "docs-only repo") -> None:
    """Opt the repo out of the implement/fix check gate via project.yaml checkOptOut."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "project.yaml").write_text(f'checkOptOut: "{reason}"\n')


def passing_lines(head_sha: str, command: str = "just check", source: str = "repo-cache") -> str:
    return f"CHECK: {command}\nCHECK_EXIT: 0\nCHECK_SOURCE: {source}\nCHECK_SHA: {head_sha}\n"


def receipt_text(check_block: str) -> str:
    return "ROUND 1\n" + check_block + "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"


def run_verify(receipt_path: str, repo: str, base_sha: str, home: str, main_worktree=None) -> tuple:
    cmd = [
        sys.executable, str(VERIFIER),
        "--step", "implement",
        "--receipt", receipt_path,
        "--since", SINCE,
        "--repo", repo,
        "--branch", "HEAD",
        "--base", base_sha,
    ]
    if main_worktree is not None:
        cmd.extend(["--main-worktree", main_worktree])
    env = os.environ.copy()
    env["HOME"] = home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def write_receipt(home: str, text: str) -> str:
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    return path


if __name__ == "__main__":
    h = Harness("FLOW VERIFY RECEIPT CHECK GATE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Rule (a): an OK receipt must carry the full-check outcome (#293 reproduction)")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        # #293: the receipt claims a build/type-check pass in prose and carries no CHECK lines.
        receipt = write_receipt(home, "ROUND 1\nBuild/type-check: pass (ruff, doc tests)\n"
                                      "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->")
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "#293 repro: prose 'pass' with no CHECK lines is rejected (exit 1)",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )
        test_result(
            "#293 repro: rejection reason is 'check outcome missing from receipt'",
            "check outcome missing from receipt" in stdout,
            f"stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (a) positive: an OK receipt with a complete passing CHECK block passes",
            exit_code == 0 and "ARTIFACTS: ok" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 2] Rule (b): the CHECK command must be the one the repo configures")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha, command="ruff check .", source="repo-cache")))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (b) negative: a substituted command (ruff for 'just check') is rejected",
            exit_code == 1 and "check substituted: receipt ran 'ruff check .', resolved 'just check'" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha, command="just check", source="repo-cache")))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (b) positive: the configured command ('just check') is accepted",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 3] Rule (c): the CHECK run must have passed")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        failing = passing_lines(head_sha).replace("CHECK_EXIT: 0", "CHECK_EXIT: 1")
        receipt = write_receipt(home, receipt_text(failing))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (c) negative: CHECK_EXIT: 1 is rejected with 'check did not pass'",
            exit_code == 1 and "check did not pass: CHECK_EXIT '1'" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (c) positive: CHECK_EXIT: 0 passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 4] Rule (f): CHECK_SHA must name the final HEAD")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        # The check was run on the parent commit, not the final HEAD.
        receipt = write_receipt(home, receipt_text(passing_lines(base_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (f) negative: CHECK_SHA naming the parent (not HEAD) is rejected with 'check ran on'",
            exit_code == 1 and f"check ran on {base_sha}, step ends at {head_sha}" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        no_sha = "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
        receipt = write_receipt(home, receipt_text(no_sha))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (f) negative: a CHECK block with no CHECK_SHA is rejected with 'check ran on none'",
            exit_code == 1 and "check ran on none" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (f) positive: CHECK_SHA equal to the final HEAD passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 5] Rule (d): an opt-out is accepted only when the resolver confirms it")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_opt_out(repo, "docs-only repo")
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) positive: CHECK: opted-out is accepted when project.yaml checkOptOut is set",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # No checkOptOut and no command configured: an opt-out claim is not confirmed by the resolver.
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) negative: CHECK: opted-out with no resolver opt-out is rejected with 'did not report an opt-out'",
            exit_code == 1 and "did not report an opt-out" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # A command is configured, so the resolver reports a command, not an opt-out: the opt-out
        # claim contradicts the resolver and is rejected.
        write_cache(repo, "just check")
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) negative: CHECK: opted-out with a configured command is rejected (resolver source 'repo-cache')",
            exit_code == 1 and "did not report an opt-out (resolver source 'repo-cache')" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 6] Rule (e): an OK receipt with no command and no opt-out is rejected")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # Nothing configured anywhere: the resolver has no command and no opt-out.
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (e) negative: a passing CHECK block with no configured command is rejected with 'check not configured'",
            exit_code == 1 and "check not configured" in stdout and "tried" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (e) positive: a configured command with a passing CHECK block passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 7] --main-worktree: the command may come from the main worktree's cache")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        main = f"{home}/main"
        Path(main).mkdir(parents=True)
        write_cache(main, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home, main_worktree=main)
        test_result(
            "--main-worktree positive: command from the main cache, worktree has none, passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    print()
    print("[Section 8] --check-worktree, CHECK_SOURCE and the --expect-model branch")

    def run_with(receipt_path, repo, base_sha, home, extra):
        cmd = [sys.executable, str(VERIFIER), "--step", "implement", "--receipt", receipt_path,
               "--since", SINCE, "--repo", repo, "--branch", "HEAD", "--base", base_sha] + extra
        env = os.environ.copy()
        env["HOME"] = home
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return r.returncode, r.stdout.strip()

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        step_wt = f"{home}/step-wt"
        Path(step_wt).mkdir()
        write_opt_out(step_wt, "docs-only repo")  # only the step's own worktree opts out
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        rc, out = run_with(receipt, repo, base_sha, home, [])
        test_result("--check-worktree: without it an opt-out only in the step worktree is rejected",
                    rc == 1, f"rc={rc} out={out[:200]!r}")
        rc, out = run_with(receipt, repo, base_sha, home, ["--check-worktree", step_wt])
        test_result("--check-worktree: the step worktree's opt-out is honoured",
                    rc == 0, f"rc={rc} out={out[:200]!r}")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        wrong_source = passing_lines(head_sha, source="justfile")
        receipt = write_receipt(home, receipt_text(wrong_source))
        rc, out = run_with(receipt, repo, base_sha, home, [])
        test_result("CHECK_SOURCE: a source differing from the resolver's is rejected with 'check source mismatch'",
                    rc == 1 and "check source mismatch: receipt CHECK_SOURCE 'justfile'" in out,
                    f"rc={rc} out={out[:200]!r}")
        # An opt-out receipt must name the opt-out source.
        write_opt_out(repo, "docs-only repo")
        (Path(repo) / ".claude" / "repo-cache.json").unlink()
        bad_opt = "CHECK: opted-out\nCHECK_SOURCE: repo-cache\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(bad_opt))
        rc, out = run_with(receipt, repo, base_sha, home, [])
        test_result("CHECK_SOURCE: an opt-out receipt with a non-opt-out source is rejected with 'check source mismatch'",
                    rc == 1 and "check source mismatch: opt-out receipt names CHECK_SOURCE 'repo-cache'" in out,
                    f"rc={rc} out={out[:200]!r}")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        no_check = "IMPLEMENTER_MODEL: haiku\nROUND 1\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        receipt = write_receipt(home, no_check)
        rc, out = run_with(receipt, repo, base_sha, home, ["--expect-model", "haiku"])
        test_result("--expect-model: an OK receipt with no CHECK lines is rejected",
                    rc == 1 and "check outcome missing" in out, f"rc={rc} out={out[:200]!r}")
        with_check = "IMPLEMENTER_MODEL: haiku\n" + receipt_text(passing_lines(head_sha))
        receipt = write_receipt(home, with_check)
        rc, out = run_with(receipt, repo, base_sha, home, ["--expect-model", "haiku"])
        test_result("--expect-model: an OK receipt with valid CHECK lines passes",
                    rc == 0, f"rc={rc} out={out[:200]!r}")

    print()
    print("[Section 9] Zero-commit OK fix receipt: the gate applies to every OK receipt (#338, decision A)")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        review_dir = Path(home) / ".claude" / "reviews" / "rv"
        review_dir.mkdir(parents=True)
        # No "Doing it" items, so the zero-commit carve-out accepts the step.
        (review_dir / "claude-action-plan.md").write_text("# Action plan\n\nNo items to do.\n")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        # --base equal to HEAD: the fix step made no commits, yet the check ran on HEAD.
        cmd = [sys.executable, str(VERIFIER), "--step", "fix", "--receipt", receipt,
               "--since", SINCE, "--repo", repo, "--branch", "HEAD", "--base", head_sha,
               "--review-dir", str(review_dir)]
        env = os.environ.copy()
        env["HOME"] = home
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        test_result("zero-commit fix: an OK receipt with valid CHECK lines on HEAD passes the gate",
                    r.returncode == 0 and "ARTIFACTS: ok" in r.stdout,
                    f"rc={r.returncode} stdout={r.stdout.strip()[:300]!r} stderr={r.stderr.strip()[:200]!r}")

    print()
    h.summarize_and_exit()
