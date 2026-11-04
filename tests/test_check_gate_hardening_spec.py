#!/usr/bin/env python3
"""
Spec-driven tests for the #338 check-gate hardening - Section 6 only.

Covers flow-verify-receipt.py: an OK receipt whose CHECK lines contradict the resolver (opt-out source
together with a command) is rejected; when re-resolution fails, the underlying resolution
reason is reported.

Run with: python3 tests/test_check_gate_hardening_spec.py
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness
from test_flow_verify_receipt_check_gate import (
    make_repo, passing_lines, receipt_text, run_verify, write_opt_out, write_receipt,
)


@contextlib.contextmanager
def _env_unset(name):
    """Remove an environment variable for the duration of the block, then restore it."""
    saved = os.environ.pop(name, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[name] = saved


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


if __name__ == "__main__":
    h = Harness("CHECK GATE HARDENING SPEC TEST SUITE - SECTION 6")
    test_result = h.test_result

    print("[Section 6] flow-verify-receipt: contradicting CHECK lines and underlying resolution reason")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_opt_out(repo, "docs-only repo")
        contradict = (
            f"CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: opt-out\nCHECK_SHA: {head_sha}\n"
        )
        receipt = write_receipt(home, receipt_text(contradict))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "contradiction: opt-out source together with a command is rejected (exit 1, not ARTIFACTS: ok)",
            exit_code == 1 and "ARTIFACTS: ok" not in stdout
            and "check substituted: receipt ran 'just check', resolved 'opt-out'" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # Invalid checkOptOut and no command: re-resolution fails with the resolver's own reason.
        _write(Path(repo) / ".claude" / "project.yaml", "checkOptOut: true\n")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "re-resolution failure: the underlying 'checkOptOut must be a non-empty string' reason is reported",
            exit_code == 1 and "checkOptOut must be a non-empty string" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    print()
    h.summarize_and_exit()
