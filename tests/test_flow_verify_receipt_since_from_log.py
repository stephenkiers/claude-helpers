#!/usr/bin/env python3
"""
Black-box tests for scripts/flow-verify-receipt.py `--since-from-log`.

Driven only through the CLI (subprocess). Each case runs under a throwaway HOME; the verifier only
accepts a flow dir under $HOME/.claude/flows, so the fixtures are built there.

Covers:
1. Exit 2 when --since-from-log has no flow dir to read from (no --flow-dir, receipt not under steps/).
2. Exit 2 for an invalid flow dir: one that does not exist, and one outside ~/.claude/flows.
3. The Reason line carries the underlying error text (malformed and future begin timestamps).
4. Happy path: a valid, in-the-past begin line bounds the verifier without an --since-from-log error.
5. A truncated log tail with no matching begin line reports a Reason distinct from the plain
   'no begin or retry line' Reason.

Run with: python3 tests/test_flow_verify_receipt_since_from_log.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SHA = "0000000000000000000000000000000000000001"
PLAIN_NO_LINE_REASON = "Reason: no begin or retry line for implement"


def run_verifier(args, home, cwd):
    """Run flow-verify-receipt.py under the given HOME. Returns (returncode, combined output)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(VERIFIER)] + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def verifier_args(receipt, flow_dir=None):
    args = ["--step", "implement", "--receipt", str(receipt), "--since-from-log",
            "--repo", "/nonexistent-repo", "--branch", "feature/x", "--base", SHA]
    if flow_dir is not None:
        args += ["--flow-dir", str(flow_dir)]
    return args


def make_flow(home, name="1-x-abc"):
    """A flow dir under $HOME/.claude/flows/repo/, with a receipt at steps/implement.json."""
    flow = home / ".claude" / "flows" / "repo" / name
    (flow / "steps").mkdir(parents=True)
    receipt = flow / "steps" / "implement.json"
    receipt.write_text("{}")
    return flow, receipt


def reason_line(output):
    for line in output.splitlines():
        if line.startswith("Reason:"):
            return line
    return ""


def test_no_flow_dir_exits_2(h, tmp):
    home = Path(tmp) / "home"
    home.mkdir()
    receipt = home / "receipt.json"
    receipt.write_text("{}")
    rc, out = run_verifier(verifier_args(receipt), home, home)
    h.test_result("no flow dir: --since-from-log exits 2",
                  rc == 2, f"rc={rc} output={out.strip()}")
    h.test_result("no flow dir: message says a flow dir is needed",
                  "needs --flow-dir" in out, f"output={out.strip()}")


def test_invalid_flow_dir_exits_2(h, tmp):
    home = Path(tmp) / "home"
    home.mkdir()
    flow, receipt = make_flow(home)
    missing = home / ".claude" / "flows" / "repo" / "does-not-exist"
    rc, out = run_verifier(verifier_args(receipt, missing), home, home)
    h.test_result("invalid flow dir (missing): --since-from-log exits 2 and names the missing log.md",
                  rc == 2 and "--since-from-log: log.md is missing" in out, f"rc={rc} output={out.strip()}")

    outside = Path(tmp) / "outside-flows"
    outside.mkdir()
    (outside / "steps").mkdir()
    outside_receipt = outside / "steps" / "implement.json"
    outside_receipt.write_text("{}")
    rc, out = run_verifier(verifier_args(outside_receipt, outside), home, home)
    h.test_result("invalid flow dir (outside ~/.claude/flows): --since-from-log exits 2",
                  rc == 2 and "outside ~/.claude/flows" in out, f"rc={rc} output={out.strip()}")


def test_reason_carries_underlying_error(h, tmp):
    cases = (
        ("malformed timestamp", "2026-13-45T99:00:00Z implement begin\n",
         "Reason: malformed begin or retry line for implement (malformed implement begin or retry line in log.md)"),
        ("future timestamp", "2099-01-01T00:00:00Z implement begin\n",
         "Reason: malformed begin or retry line for implement (implement begin or retry timestamp is in the future)"),
    )
    for label, log_text, expected in cases:
        home = Path(tmp) / label.replace(" ", "-")
        home.mkdir()
        flow, receipt = make_flow(home)
        (flow / "log.md").write_text(log_text)
        rc, out = run_verifier(verifier_args(receipt, flow), home, home)
        reason = reason_line(out)
        h.test_result(f"reason carries the underlying error and exits 1 ({label})",
                      rc == 1 and reason == expected, f"rc={rc} reason={reason!r} output={out.strip()}")


def test_valid_begin_line_bounds_verifier(h, tmp):
    home = Path(tmp) / "home"
    home.mkdir()
    flow, receipt = make_flow(home)
    (flow / "log.md").write_text("2026-10-20T10:00:00Z implement begin\n")
    # A readable Decision is required before the artifacts check (a receipt without one is rejected).
    receipt.write_text("ROUND 1\nATTEMPT: 1\nDecision: OK\nOUTSTANDING: none\n<!-- step-end -->\n")
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    args = ["--step", "implement", "--receipt", str(receipt), "--since-from-log",
            "--flow-dir", str(flow), "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", head]
    rc, out = run_verifier(args, home, home)
    h.test_result("happy: valid begin line bounds the verifier and reaches the artifacts check",
                  "Reason: implement artifacts missing" in out and "since-from-log" not in out
                  and "no begin or retry" not in out and "Could not count commits" not in out,
                  f"rc={rc} output={out.strip()}")


def test_truncated_tail_reason_is_distinct(h, tmp):
    home = Path(tmp) / "home"
    home.mkdir()
    flow, receipt = make_flow(home)
    # A begin line well before the tail, then enough filler that the bounded tail read cannot see it.
    filler = "noise line padding for the tail read\n"
    (flow / "log.md").write_text("2026-10-20T10:00:00Z implement begin\n" + filler * (3 * 1024 * 1024 // len(filler)))
    _, out_big = run_verifier(verifier_args(receipt, flow), home, home)

    small = Path(tmp) / "small"
    small.mkdir()
    small_flow, small_receipt = make_flow(small)
    (small_flow / "log.md").write_text("some unrelated line\n")
    _, out_small = run_verifier(verifier_args(small_receipt, small_flow), small, small)

    expected_big = ("Reason: no begin or retry line for implement in the readable tail of log.md "
                    "(only the last 1048576 bytes of log.md were read; an older line may exist)")
    h.test_result("truncated tail: Reason is the exact tail-window reason",
                  reason_line(out_big) == expected_big, f"big={reason_line(out_big)!r}")
    h.test_result("truncated tail: plain no-begin-line Reason is the exact plain text",
                  reason_line(out_small) == PLAIN_NO_LINE_REASON, f"small={reason_line(out_small)!r}")


def main():
    h = Harness("flow-verify-receipt.py --since-from-log")
    cases = (
        test_no_flow_dir_exits_2,
        test_invalid_flow_dir_exits_2,
        test_reason_carries_underlying_error,
        test_valid_begin_line_bounds_verifier,
        test_truncated_tail_reason_is_distinct,
    )
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            case(h, tmp)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
