#!/usr/bin/env python3
"""
Spec-driven tests for scripts/flow-verify-receipt.py escalation argument handling.

Covers behaviours from feature #315 action plan:
- F4: `--expect-model` enforcement for escalated receipts
- F9: `--stop-source` must be readable before receipt-ok exit
- F17: Manual escalation (--escalation) should enforce once-per-step guard
- F20: `--escalation` arg validation: exclusive modes, required values, no opus model

Run with: python3 tests/test_flow_verify_receipt_escalation_args.py
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def since_timestamp():
    """ISO-8601 UTC an hour ago, so every fixture file is fresher than --since."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class EscalationFixture:
    """Minimal fixture with HOME, FLOW_DIR, review_dir, and receipt."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.flow = self.root / "flow"
        self.review_dir = self.home / ".claude" / "reviews" / "rv"
        self.receipt = self.flow / "steps" / "04-implement.md"

        self.home.mkdir(parents=True)
        self.review_dir.mkdir(parents=True)
        for sub in ("steps", "questions", "answers"):
            (self.flow / sub).mkdir(parents=True)

        # Required review files
        for name in ("claude-action-plan.md", "final-report.md", "findings.json"):
            (self.review_dir / name).write_text("x\n", encoding="utf-8")
        for reviewer in ("alice", "bob"):
            (self.review_dir / f"{reviewer}-pass1.md").write_text(
                f"{reviewer} review\n<!-- pass1-end -->\n", encoding="utf-8"
            )

    def write_receipt(self, body_lines, path=None):
        """Write an implement receipt with given lines."""
        target = Path(path) if path else self.receipt
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(body_lines) + "\n"
        target.write_text(body, encoding="utf-8")


def run_verifier(fx, step="implement", receipt=None, escalation_mode=False, **kwargs):
    """Run verifier with HOME pinned. Returns (exit_code, stdout, stderr)."""
    args = [sys.executable, str(VERIFIER), "--step", step]
    if receipt:
        args += ["--receipt", str(receipt)]
    else:
        args += ["--receipt", str(fx.receipt)]
    args += ["--since", since_timestamp(), "--flow-dir", str(fx.flow)]

    if escalation_mode:
        args.append("--escalation")
    for key, val in kwargs.items():
        if val is not None:
            arg_name = key.replace("_", "-")
            args.extend([f"--{arg_name}", str(val)])

    env = {**os.environ, "HOME": str(fx.home)}
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness("flow-verify-receipt.py: escalation args (#315 F4, F9, F17, F20)")

    # ========================
    # F20: --escalation arg contract and validation
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: FAILED",
                "FAILURE_CLASS: implementer-shortfall",
                "<!-- step-end -->",
            ]
        )
        # F20a: --escalation requires --stop-source and --escalations
        rc, out, err = run_verifier(
            fx, escalation_mode=True, failures=0
        )
        # Missing required arg is a verifier error (exit 2 with its own message).
        # On a tree without --escalation, argparse also exits 2, so the message is what pins #334.
        h.test_result(
            "F20a: --escalation without --stop-source fails",
            rc == 2 and "--escalation requires --stop-source" in err,
            f"exit code {rc}, expected 2 with the verifier message. stderr: {err[:200]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: FAILED",
                "FAILURE_CLASS: implementer-shortfall",
                "<!-- step-end -->",
            ]
        )
        # F20b: --escalation requires --escalations count
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="receipt", failures=0
        )
        h.test_result(
            "F20b: --escalation without --escalations fails",
            rc == 2 and "--escalation requires --escalations" in err,
            f"exit code {rc}, stderr: {err[:200]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # F20c: --escalation with all required args succeeds
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="receipt", escalations=0, failures=0
        )
        h.test_result(
            "F20c: --escalation with valid args succeeds (exit 0)",
            rc == 0,
            f"exit code {rc}, stderr: {err[:100]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # F20d: --stop-source must be one of {receipt, validation, audit}
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="invalid", escalations=0, failures=0
        )
        # argparse choices rejects the value; the message names the invalid choice.
        h.test_result(
            "F20d: --stop-source with invalid value fails",
            rc == 2 and "invalid choice: 'invalid'" in err,
            f"exit code {rc}, stderr: {err[:200]}",
        )

    # ========================
    # F4: --expect-model enforcement for escalated receipts
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "IMPLEMENTER_MODEL: haiku",
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # --expect-model is parsed and enforced in expect-model mode (not escalation mode).
        # A haiku receipt against an expected sonnet must hit the verifier's mismatch message;
        # on a tree without the flag, argparse rejects it before any of this runs.
        rc, out, err = run_verifier(fx, expect_model="sonnet")
        h.test_result(
            "F4a: --expect-model flag is accepted",
            rc == 1 and "implementer model mismatch (expected sonnet, receipt haiku)" in out,
            f"exit code {rc}, stdout: {out[:200]}, stderr: {err[:200]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # F4b: --expect-model accepts "sonnet" but not "opus" (per ADR-0004)
        # Test that we can pass expect-model
        rc, out, err = run_verifier(
            fx, expect_model="sonnet", escalation_mode=True, stop_source="receipt", escalations=0
        )
        h.test_result(
            "F4b: --expect-model='sonnet' is accepted",
            rc == 0,
            f"exit code {rc}",
        )

    # ========================
    # F9: --stop-source must be readable and used
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # F9a: In escalation mode with --stop-source, the verifier should use it
        # The output should reflect the escalation with the source
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="receipt", escalations=0, failures=0
        )
        # In escalation mode, should print ESCALATE line
        has_escalate_line = "ESCALATE" in out or "ESCALATE" in err
        h.test_result(
            "F9a: Escalation mode prints ESCALATE output",
            has_escalate_line,
            f"output: {(out + err)[:200]}",
        )

    # ========================
    # F17: Manual escalation guards (once-per-step)
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: FAILED",
                "FAILURE_CLASS: implementer-shortfall",
                "<!-- step-end -->",
            ]
        )
        # F17a: With --escalations > 0 the verifier must refuse to escalate again
        # (once-per-step guard: only escalate if escalations == 0)
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="receipt", escalations=1, failures=0
        )
        h.test_result(
            "F17a: --escalation with escalations=1 yields none escalations-spent (once-per-step)",
            rc == 0 and out.strip() == "ESCALATE: none escalations-spent",
            f"exit code {rc}, out {out!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = EscalationFixture(tmp)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        # F17b: With --escalations == 0 and --escalation flag, should succeed
        rc, out, err = run_verifier(
            fx, escalation_mode=True, stop_source="receipt", escalations=0, failures=0
        )
        h.test_result(
            "F17b: --escalation with escalations=0 is allowed (first escalation)",
            rc == 0,
            f"exit code {rc}",
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
