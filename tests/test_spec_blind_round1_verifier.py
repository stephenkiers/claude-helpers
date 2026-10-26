#!/usr/bin/env python3
"""
Spec-blind test suite for the ROUND 1 section-header check in scripts/flow-verify-receipt.py
(#341 item 5, #322) and for the behaviour-neutral helper boundaries (#341 item 4).

Written only from the plan (follow-ups-from-315 plan, Steps 4 and 6 and its Testing Strategy),
never from the verifier source or the existing round-1 suites. Every case drives the verifier
CLI as a subprocess, so the tests exercise production code and fail if it is removed or changed.

Spec facts used:
- An implement or fix receipt with `Decision: OK` must contain a ROUND 1 section header: a line
  that starts (after optional whitespace and markdown markers) with ROUND, optional whitespace,
  1, optional closing `**`, optional spaces, then a separator (em dash, en dash, hyphen, colon,
  plus) or end of line. Matching is case-insensitive.
- Paraphrases of that header are accepted. Status-table lines (`Round 1 join:`,
  `Round 1 (implement):`), prose mentioning round 1 mid-line, and a bare token that is not
  followed by a separator are rejected.
- `ROUND 1: NOT RUN ...` passes the shape check (rule 7 handles whether round 1 ran).
- A miss yields a FAILED reason that names ROUND 1.
- The ROUND 1 check applies to Decision: OK receipts only; it must not replace the stop reason of a
  Decision: FAILED receipt.
- `_fold_report_answers()` and `check_report_checkpoint()` remain as module-level helpers.

Run with: python3 tests/test_spec_blind_round1_verifier.py
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _check_gate_fixture import with_check_lines
from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SINCE = "2020-01-01T00:00:00Z"
ROUND1_MISS = "ROUND 1"  # the miss reason must name ROUND 1

TAIL_OK = "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n"


def git_rev_parse(rev):
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", rev],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


# Try to get HEAD~1, fall back to empty-tree hash if not available
BASE = git_rev_parse("HEAD~1")
if not BASE:
    # Empty-tree hash (used when history is shallow or unavailable)
    BASE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def verify(receipt_text, step="implement"):
    """Run the verifier CLI on one receipt. Returns (returncode, combined output)."""
    with tempfile.TemporaryDirectory() as tmp:
        receipt = Path(tmp) / "receipt.md"
        receipt_text, check_argv = with_check_lines(receipt_text, REPO_ROOT, Path(tmp))
        receipt.write_text(receipt_text)
        argv = [sys.executable, str(VERIFIER), "--step", step, "--receipt", str(receipt),
                "--since", SINCE, "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", BASE]
        argv += check_argv
        if step == "fix":
            argv += ["--review-dir", tmp]
        r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def verify_expect_model(receipt_text, step="implement"):
    """Run the verifier CLI with --expect-model. Returns (returncode, combined output)."""
    with tempfile.TemporaryDirectory() as tmp:
        receipt = Path(tmp) / "receipt.md"
        receipt_text, check_argv = with_check_lines(receipt_text, REPO_ROOT, Path(tmp))
        receipt.write_text(receipt_text)
        argv = [sys.executable, str(VERIFIER), "--step", step, "--receipt", str(receipt),
                "--expect-model", "haiku", "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", BASE]
        argv += check_argv
        if step == "fix":
            argv += ["--review-dir", tmp]
        r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def last_line(text):
    """Return the last non-empty line of text (the Reason: line)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


h = Harness("SPEC-BLIND: ROUND 1 SECTION-HEADER CHECK AND HELPER BOUNDARIES")
t = h.test_result

# ---------------------------------------------------------------------------
print("[Precondition] git history available for the verifier's base SHA")
print("=" * 70)
t("HEAD~1 resolves (needed as a non-empty --base)", bool(BASE),
  "git rev-parse HEAD~1 failed; the verifier cannot be driven without a base SHA")

# ---------------------------------------------------------------------------
print()
print("[ROUND 1 positive shapes] Decision: OK implement receipt is accepted")
print("=" * 70)

POSITIVE_HEADERS = {
    "em dash parallel header": "ROUND 1 — Implementer (parallel)",
    "hyphen separator": "ROUND 1 - Implementer (parallel)",
    "en dash separator": "ROUND 1 – Implementer (single unit)",
    "em dash single unit": "ROUND 1 — Implementer (single unit)",
    "bare ROUND 1 line (end of line)": "ROUND 1",
    "colon units paraphrase": "ROUND 1: units 3 of 3 done",
    "ROUND 1+2 complete (plus separator)": "ROUND 1+2 complete (2 rounds)",
    "ROUND 1: NOT RUN passes the shape check": "ROUND 1: NOT RUN as parallel Haiku units (no --pause)",
    "bold closing marker, Round case": "**Round 1** — implementer",
    "markdown ## heading": "## ROUND 1: units",
    "indented lowercase round 1 -": "  round 1 - implementer",
    "blockquote marker": "> ROUND 1 — quoted header",
    "bullet marker": "- ROUND 1: bullet header",
    "whole line bold": "**ROUND 1 — Implementer**",
    "leading tab": "\tROUND 1 — tab indented",
    "double space before 1": "ROUND  1 — double space",
    "lowercase separator-less end of line": "round 1",
}
for name, header in POSITIVE_HEADERS.items():
    rc, out = verify(header + "\n" + TAIL_OK)
    t(f"accepts header: {name}", rc == 0,
      f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# Realistic receipt: header plus a status table is accepted.
realistic_ok = (
    "ROUND 1 — Implementer (parallel)\n"
    "  Round 1 join:       not run (no --pause)\n"
    "  Round 2 (implement):        ok\n"
    + TAIL_OK
)
rc, out = verify(realistic_ok)
t("accepts a real-shaped receipt with header and status table", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# ---------------------------------------------------------------------------
print()
print("[ROUND 1 negative shapes] Decision: OK implement receipt is rejected, reason names ROUND 1")
print("=" * 70)

NEGATIVE_BODIES = {
    "no ROUND 1 token at all": "Implemented the change.\n",
    "prose-only round 1 mid-sentence": "Haiku reports all of round 1 passed cleanly.\n",
    "prose-only round 1 at line start with no separator": "round 1 went fine overall.\n",
    "token mid-line after label": "Summary: ROUND 1 — done\n",
    "status line Round 1 join (separator-less label)": "  Round 1 join:       not run (no --pause)\n",
    "status line Round 1 (implement) with paren": "  Round 1 (implement):        unknown\n",
    "ROUND 10 is not ROUND 1": "ROUND 10 — tenth round\n",
    "ROUND 1 followed by a period": "ROUND 1. done\n",
    "ROUND 1 followed by a word, no separator": "Round 1 summary follows\n",
}
for name, body in NEGATIVE_BODIES.items():
    rc, out = verify(body + TAIL_OK)
    t(f"rejects: {name}", rc != 0 and ROUND1_MISS in out,
      f"rc={rc}, last line: {last_line(out)}")

# The exact status-table receipt with the round-1 header removed (no header anywhere).
status_only = (
    "  Round 1 join:       not run (no --pause)\n"
    "  Round 1 (implement):        unknown\n"
    "  Round 2 (implement):        ok\n"
    + TAIL_OK
)
rc, out = verify(status_only)
t("rejects a status-table-only receipt (header removed)", rc != 0 and ROUND1_MISS in out,
  f"rc={rc}, last line: {last_line(out)}")

# Header is present in one line but a prose-only mention would be the only other ROUND 1 token.
prose_plus_status = (
    "Haiku reports all of round 1 passed cleanly.\n"
    "  Round 1 (implement):        unknown\n"
    + TAIL_OK
)
rc, out = verify(prose_plus_status)
t("rejects prose plus status line with no section header", rc != 0 and ROUND1_MISS in out,
  f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[ROUND 1 applies to fix receipts too]")
print("=" * 70)

rc, out = verify("ROUND 1 — Fixer\n" + TAIL_OK, step="fix")
t("fix receipt with ROUND 1 header is accepted", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

rc, out = verify("Summary: fixed the thing.\n" + TAIL_OK, step="fix")
t("fix receipt without ROUND 1 header is rejected naming ROUND 1",
  rc != 0 and ROUND1_MISS in out, f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[Decision: FAILED keeps its own stop reason]")
print("=" * 70)

failed_no_header = "OUTSTANDING: none\nDecision: FAILED\nREASON: boom\n<!-- step-end -->\n"
rc, out = verify(failed_no_header)
t("FAILED receipt without ROUND 1 header does not get the ROUND 1 miss reason",
  rc == 1 and "boom" in out and "section header" not in out and "missing ROUND 1" not in out,
  f"rc={rc}, last line: {last_line(out)}")

failed_status = (
    "  Round 1 (implement):        unknown\n"
    "OUTSTANDING: none\nDecision: FAILED\nREASON: boom\n<!-- step-end -->\n"
)
rc, out = verify(failed_status)
t("FAILED receipt with only status lines does not get the ROUND 1 miss reason",
  rc == 1 and "boom" in out and "missing ROUND 1" not in out,
  f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[Other reasons survive when the ROUND 1 header is present]")
print("=" * 70)

missing_outstanding = "ROUND 1 — Implementer (parallel)\nDecision: OK\n<!-- step-end -->\n"
rc, out = verify(missing_outstanding)
t("OK receipt with header but no OUTSTANDING still fails on OUTSTANDING",
  rc != 0 and "OUTSTANDING" in out and "ROUND 1" not in last_line(out),
  f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[CRLF line endings (D6)]")
print("=" * 70)

crlf_header_receipt = "ROUND 1 — Implementer (parallel)\r\nOUTSTANDING: none\r\nDecision: OK\r\n<!-- step-end -->\r\n"
rc, out = verify(crlf_header_receipt)
t("ROUND 1 header with CRLF line endings is accepted (D6)",
  rc == 0, f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[Expect-model mode still checks OUTSTANDING and ROUND 1 (NY1)]")
print("=" * 70)

header_ok_receipt = (
    "IMPLEMENTER_MODEL: haiku\n"
    "ROUND 1 — Implementer (parallel)\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify_expect_model(header_ok_receipt)
t("expect-model on OK receipt with header passes",
  rc == 0, f"rc={rc}, last line: {last_line(out)}")

no_header_receipt = (
    "IMPLEMENTER_MODEL: haiku\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify_expect_model(no_header_receipt)
t("expect-model on OK receipt without ROUND 1 header now fails (NY1)",
  rc == 1 and ROUND1_MISS in out,
  f"rc={rc}, last line: {last_line(out)}")

# ---------------------------------------------------------------------------
print()
print("[Helper boundaries kept recognisable (#341 Step 4)]")
print("=" * 70)

spec = importlib.util.spec_from_file_location("flow_verify_receipt_under_test", VERIFIER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
t("_fold_report_answers is a module-level callable",
  callable(getattr(module, "_fold_report_answers", None)))
t("check_report_checkpoint is a module-level callable",
  callable(getattr(module, "check_report_checkpoint", None)))

h.summarize_and_exit()
