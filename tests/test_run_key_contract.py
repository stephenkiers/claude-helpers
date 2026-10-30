#!/usr/bin/env python3
"""
Spec-blind test coverage for plan #346 (put-run-key-in-the-step-receipt).

This test suite is written from the plan specification alone, without reading the
implementation. It verifies the key behaviors and contracts described in the plan:

1. RUN_KEY_RE pattern is consistent between implement-ledger.py and flow-verify-receipt.py
2. Old marker-less receipts still work (grandfathered backward compatibility)
3. Verifier output includes RUN_KEY_SOURCE lines before ARTIFACTS: ok for implement/fix receipts
4. Receipt template has correct header order (CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT: 1)
5. Doc mentions correct render command with --repo, --flow-dir, --step flags
6. Telemetry includes ledger_lookup field for audits

Run with: python3 tests/test_run_key_contract.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

LEDGER_SCRIPT = REPO_ROOT / "scripts" / "implement-ledger.py"
VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
IMPL_CMD = REPO_ROOT / "commands" / "implement-with-haiku.md"
EXPERT_FLOW_CMD = REPO_ROOT / "commands" / "expert-flow.md"

# Load the modules to get the patterns
_ledger_spec = importlib.util.spec_from_file_location("implement_ledger", LEDGER_SCRIPT)
_ledger_module = importlib.util.module_from_spec(_ledger_spec)
_ledger_spec.loader.exec_module(_ledger_module)
LEDGER_RUN_KEY_RE = _ledger_module.RUN_KEY_RE

_verifier_spec = importlib.util.spec_from_file_location("flow_verify_receipt", VERIFIER_SCRIPT)
_verifier_module = importlib.util.module_from_spec(_verifier_spec)
_verifier_spec.loader.exec_module(_verifier_module)
VERIFIER_RUN_KEY_RE = _verifier_module.RUN_KEY_RE


def git(repo: str, *args: str) -> str:
    """Run a git command in a repo. Returns stdout stripped."""
    result = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_test_repo(root: str) -> tuple:
    """Create a git repo with an initial commit. Returns (repo, base_sha)."""
    repo = f"{root}/repo"
    Path(repo).mkdir(parents=True)
    git(repo, "init", "-q")
    Path(f"{repo}/file.txt").write_text("initial\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "initial")
    base_sha = git(repo, "rev-parse", "HEAD")
    return repo, base_sha


def setup_feature_repo(root: str) -> tuple:
    """A repo with a `just check` cache, a base commit and one feature commit. Returns (repo, base, head)."""
    repo, base_sha = make_test_repo(root)
    write_cache(repo)
    Path(f"{repo}/feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "add feature")
    return repo, base_sha, git(repo, "rev-parse", "HEAD")


def write_cache(repo: str, command: str = "just check") -> None:
    """Configure the repo's check command."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": command}}))


def run_verifier(receipt_path: str, repo: str, base_sha: str, home: str, step: str = "implement",
                 extra: tuple = ()) -> tuple:
    """Run the verifier CLI. Returns (returncode, stdout, stderr)."""
    cmd = [
        sys.executable, str(VERIFIER_SCRIPT),
        "--step", step,
        "--receipt", receipt_path,
        "--since", "2026-01-01T00:00:00Z",
        "--repo", repo,
        "--branch", "HEAD",
        "--base", base_sha,
        *extra,
    ]
    env = os.environ.copy()
    env["HOME"] = home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def write_receipt(home: str, text: str) -> str:
    """Write a receipt file. Returns the path."""
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    return path


def check_block(head_sha: str) -> str:
    """The CHECK lines of a passing implement receipt for the repo cache's `just check`."""
    return ("CHECK: just check\n"
            "CHECK_EXIT: 0\n"
            "CHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + head_sha + "\n")


def marker_less_receipt(head_sha: str) -> str:
    """An OK implement receipt from before RECEIPT_CONTRACT existed (no RUN_KEY lines)."""
    return ("ROUND 1\n" + check_block(head_sha) +
            "OUTSTANDING: none\n"
            "Decision: OK\n"
            "<!-- step-end -->")


def keyed_receipt(head_sha: str, header_extra: str = "RECEIPT_CONTRACT: 1\nRUN_KEY: run-key-abc\n",
                  body_extra: str = "") -> str:
    """An OK implement receipt with the RECEIPT_CONTRACT and RUN_KEY header lines (#346)."""
    return ("ROUND 1\n" + check_block(head_sha) + header_extra +
            "OUTSTANDING: none\n"
            "Decision: OK\n"
            "<!-- step-end -->" + body_extra)


def make_plan_fixture(home: str) -> tuple:
    """A plan receipt that passes check_plan: plan file, session dir with a contributor-free roster."""
    flow_dir = Path(home) / ".claude" / "flows" / "repo" / "1-x-abc"
    (flow_dir / "steps").mkdir(parents=True)
    (flow_dir / "log.md").write_text("2026-10-20T10:00:00Z plan begin\n")
    plan_file = Path(home) / ".claude" / "plans" / "slug-20260101T000000-00001.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# plan\n")
    session = Path(home) / ".claude" / "plan-sessions" / "repo" / "slug-20260101T000000-00001"
    session.mkdir(parents=True)
    (session / "context.md").write_text("context\n")
    (session / "selected-experts.md").write_text("| Expert | Role |\n| --- | --- |\n| Auditor | audit |\n")
    (session / "plan.md").write_text("# plan\n")
    (session / "decisions.md").write_text("# decisions\n")
    receipt = flow_dir / "steps" / "plan.json"
    receipt.write_text(f"FINAL_PLAN_PATH: {plan_file}\nATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n")
    return flow_dir, receipt


def run_plan(receipt: Path, flow_dir: Path, home: str) -> tuple:
    """Run the plan verifier with --since-from-log against flow_dir. Returns (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["HOME"] = home
    cmd = [sys.executable, str(VERIFIER_SCRIPT), "--step", "plan", "--receipt", str(receipt),
           "--since-from-log", "--flow-dir", str(flow_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def main():
    h = Harness("PLAN #346 SPEC-BLIND COVERAGE TEST SUITE")

    print("[Section 1] RUN_KEY_RE pattern consistency")

    # Test 1.1: Patterns are equal (as required by the plan)
    h.test_result(
        "RUN_KEY_RE patterns are identical between scripts",
        LEDGER_RUN_KEY_RE.pattern == VERIFIER_RUN_KEY_RE.pattern,
        f"ledger={LEDGER_RUN_KEY_RE.pattern!r} vs verifier={VERIFIER_RUN_KEY_RE.pattern!r}",
    )

    # Test 1.2: Validate the pattern against known valid/invalid keys
    # Pattern: ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ means: starts with alphanumeric,
    # followed by 0-199 chars that are alphanumeric/./underscore/dash
    valid_keys = ["my-run", "run-123", "run_key", "R", "a.b", "a-b_c.d", "test-", "x.y-z"]
    invalid_keys = ["-invalid", "too" + "x" * 200, ""]  # starts with dash, too long, empty

    for key in valid_keys:
        h.test_result(
            f"RUN_KEY_RE matches valid key: {key!r}",
            bool(LEDGER_RUN_KEY_RE.match(key)),
            f"pattern={LEDGER_RUN_KEY_RE.pattern!r}",
        )

    for key in invalid_keys:
        h.test_result(
            f"RUN_KEY_RE rejects invalid key: {key[:30]!r}...",
            not bool(LEDGER_RUN_KEY_RE.match(key)),
            f"pattern={LEDGER_RUN_KEY_RE.pattern!r}",
        )

    print()
    print("[Section 2] Receipt template structure (Step 3)")

    impl_text = IMPL_CMD.read_text()
    # Test that receipt template has correct header order
    h.test_result(
        "implement-with-haiku receipt template contains CHECK_SHA",
        "CHECK_SHA:" in impl_text,
        "CHECK_SHA not found in template",
    )
    h.test_result(
        "implement-with-haiku receipt template contains RECEIPT_CONTRACT: 1",
        "RECEIPT_CONTRACT: 1" in impl_text,
        "RECEIPT_CONTRACT: 1 not found in template",
    )

    # Verify header order: CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT -> OUTSTANDING
    header_order = re.search(
        r'CHECK_SHA:.*?\nRUN_KEY:.*?\nRECEIPT_CONTRACT: 1\nOUTSTANDING:',
        impl_text,
        re.DOTALL,
    )
    h.test_result(
        "receipt template header order is correct (CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT: 1 -> OUTSTANDING)",
        header_order is not None,
        "header order not found or incorrect",
    )

    print()
    print("[Section 3] Expert-flow command structure (Step 4)")

    flow_text = EXPERT_FLOW_CMD.read_text()

    # Anchor the render check to the `render --run-key` line itself: the flags must sit on that command line,
    # not merely appear somewhere in the document.
    render_lines = [ln for ln in flow_text.splitlines() if "render --run-key" in ln]
    h.test_result(
        'expert-flow "render --run-key" line names --repo, --flow-dir and --step',
        any(all(flag in ln for flag in ("--repo", "--flow-dir", "--step")) for ln in render_lines),
        f"render --run-key lines={render_lines!r}",
    )

    # Test fallback path documentation
    h.test_result(
        'expert-flow mentions "LEDGER LOOKUP: locate-fallback"',
        'LEDGER LOOKUP: locate-fallback' in flow_text,
        "fallback path not documented",
    )

    # Test telemetry field
    h.test_result(
        "expert-flow telemetry includes --arg ledger_lookup",
        '--arg ledger_lookup' in flow_text,
        "ledger_lookup telemetry field not found",
    )

    # Test that audits.jsonl uses the field
    h.test_result(
        "expert-flow telemetry passes ledger_lookup to audits.jsonl",
        'ledger_lookup:' in flow_text and 'audits.jsonl' in flow_text,
        "ledger_lookup not wired into audits.jsonl",
    )

    print()
    print("[Section 4] Backward compatibility (marker-less receipts)")

    # An old receipt without RECEIPT_CONTRACT or RUN_KEY still passes, through the locate-fallback.
    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = setup_feature_repo(home)
        path = write_receipt(home, marker_less_receipt(head_sha))
        rc, stdout, stderr = run_verifier(path, repo, base_sha, home, step="implement")
        lines = stdout.splitlines()
        h.test_result(
            "old marker-less implement receipt passes (exit 0) and ends with ARTIFACTS: ok",
            rc == 0 and lines[-1:] == ["ARTIFACTS: ok"],
            f"rc={rc} stdout={stdout[:200]!r}",
        )
        h.test_result(
            "old marker-less implement receipt prints RUN_KEY_SOURCE: locate-fallback before ARTIFACTS: ok",
            len(lines) >= 2 and lines[-2].startswith("RUN_KEY_SOURCE: locate-fallback"),
            f"stdout={stdout[:200]!r}",
        )

    print()
    print("[Section 5] OK receipt source lines and rejections (Step 2)")

    # A well-formed RUN_KEY prints RUN_KEY_SOURCE: receipt and RUN_KEY before ARTIFACTS: ok, on both paths.
    for label, extra, prefix in (
        ("standard", (), ""),
        ("expect-model", ("--expect-model", "haiku"), "IMPLEMENTER_MODEL: haiku\n"),
    ):
        with tempfile.TemporaryDirectory() as home:
            repo, base_sha, head_sha = setup_feature_repo(home)
            path = write_receipt(home, prefix + keyed_receipt(head_sha))
            rc, stdout, stderr = run_verifier(path, repo, base_sha, home, step="implement", extra=extra)
            h.test_result(
                f"{label}: OK receipt with RECEIPT_CONTRACT and RUN_KEY prints source and key before ARTIFACTS: ok",
                rc == 0 and stdout.splitlines()[-3:] == [
                    "RUN_KEY_SOURCE: receipt", "RUN_KEY: run-key-abc", "ARTIFACTS: ok"],
                f"rc={rc} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
            )

    # Invalid header shapes exit 1 with their Reason.
    rejections = (
        ("unknown RECEIPT_CONTRACT", keyed_receipt, dict(header_extra="RECEIPT_CONTRACT: 2\nRUN_KEY: run-key-abc\n"),
         "Reason: unknown RECEIPT_CONTRACT '2'"),
        ("duplicated RUN_KEY", keyed_receipt,
         dict(header_extra="RECEIPT_CONTRACT: 1\nRUN_KEY: run-key-abc\nRUN_KEY: run-key-abc\n"),
         "Reason: receipt RUN_KEY duplicated"),
        ("RUN_KEY below the header", keyed_receipt,
         dict(header_extra="RECEIPT_CONTRACT: 1\n", body_extra="\nRUN_KEY: run-key-abc\n"),
         "Reason: receipt RUN_KEY outside the header block"),
    )
    for label, build, kwargs, reason in rejections:
        with tempfile.TemporaryDirectory() as home:
            repo, base_sha, head_sha = setup_feature_repo(home)
            path = write_receipt(home, build(head_sha, **kwargs))
            rc, stdout, stderr = run_verifier(path, repo, base_sha, home, step="implement")
            h.test_result(
                f"rejection: {label} exits 1 with its Reason and no ARTIFACTS: ok",
                rc == 1 and reason in stdout.splitlines() and "ARTIFACTS: ok" not in stdout,
                f"rc={rc} stdout={stdout[:300]!r}",
            )

    print()
    print("[Section 6] Plan/review receipt handling")

    # A passing plan receipt must exit 0 before the negative check means anything.
    with tempfile.TemporaryDirectory() as home:
        flow_dir, receipt = make_plan_fixture(home)
        rc, stdout, stderr = run_plan(receipt, flow_dir, home)
        h.test_result(
            "passing plan receipt exits 0 with ARTIFACTS: ok",
            rc == 0 and "ARTIFACTS: ok" in stdout.splitlines(),
            f"rc={rc} stdout={stdout[:200]!r} stderr={stderr[:200]!r}",
        )
        h.test_result(
            "verifier output for plan receipt does NOT include RUN_KEY_SOURCE",
            "RUN_KEY_SOURCE:" not in stdout,
            f"stdout={stdout[:200]!r}",
        )

    print()
    print("[Section 7] Rule 8 command documentation")

    # Test that rule 8 uses --since-from-log
    h.test_result(
        'expert-flow mentions "--since-from-log" in rule 8',
        re.search(r'rule 8.*--since-from-log', flow_text, re.IGNORECASE | re.DOTALL) is not None,
        "rule 8 --since-from-log not found",
    )

    # Test that receipt template includes RUN_KEY and RECEIPT_CONTRACT
    h.test_result(
        "receipt template includes RUN_KEY: placeholder",
        "<RUN_KEY>" in impl_text,
        "<RUN_KEY> placeholder not found",
    )

    print()
    print("[Section 8] Verifier exit codes and reasons")

    # Test that verifier mentions the new exit reasons
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY duplicated' exit reason",
        "receipt RUN_KEY duplicated" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY outside the header block' exit reason",
        "receipt RUN_KEY outside the header block" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY malformed' exit reason",
        "receipt RUN_KEY malformed" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'unknown RECEIPT_CONTRACT' exit reason",
        "unknown RECEIPT_CONTRACT" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt missing RUN_KEY (required by RECEIPT_CONTRACT: 1)' exit reason",
        "receipt missing RUN_KEY (required by RECEIPT_CONTRACT: 1)" in flow_text,
        "exit reason not documented",
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
