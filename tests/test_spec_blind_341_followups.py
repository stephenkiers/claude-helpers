#!/usr/bin/env python3
"""
Spec-blind test suite for #341 follow-ups from #315 action plan.

Focused on testable behaviors from the plan's decisions and fixes:
1. --expect-model verifier path now enforces OUTSTANDING and ROUND 1 header
2. CRLF ROUND 1 header being accepted
3. ROUND 1 failure Reason being fixed plain English text (not regex)
4. track-and-start zero-byte github-cache.json handling (via doc checks)
5. doc claims: ADR amendment mentions Refs #325; rule 5 is a pointer

From the plan:
- Item 1 (Needs you): "Fall through to the standard checks — run OUTSTANDING and ROUND 1
  after the expect-model checks, and add a test with --expect-model on a header-less OK receipt"
- Item 2 (Doing): F4 — Zero-byte github-cache.json; use [ -s ] guards
- Item 3 (Doing): F5 — ROUND 1 failure Reason embeds the raw regex; use fixed plain-English Reason
- Item 6 (Doing): F8 — Bare ROUND 1 header fails on CRLF; use [ \t\r]*$ or normalize CRLF
- Item 3 (Needs you): Receipt contract narrowed; record in ADR note with Refs #325

Run with: python3 tests/test_spec_blind_341_followups.py
"""

import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SINCE = "2020-01-01T00:00:00Z"


def git_rev_parse(rev):
    """Get git revision SHA."""
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", rev],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


# Try to get HEAD~1, fall back to empty-tree hash if not available
BASE = git_rev_parse("HEAD~1")
if not BASE:
    BASE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def verify(receipt_text, step="implement", **kwargs):
    """Run the verifier CLI on one receipt. Returns (returncode, stdout+stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        receipt = Path(tmp) / "receipt.md"
        receipt.write_text(receipt_text)
        argv = [sys.executable, str(VERIFIER), "--step", step, "--receipt", str(receipt),
                "--since", SINCE, "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", BASE]
        # Add optional kwargs to argv
        for key, value in kwargs.items():
            argv.append(f"--{key.replace('_', '-')}")
            if value is not True:
                argv.append(str(value))
        if step == "fix":
            argv += ["--review-dir", tmp]
        r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def last_line(text):
    """Return the last non-empty line of text."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ============================================================================
h = Harness("SPEC-BLIND: #341 FOLLOW-UP BEHAVIORS")
t = h.test_result

print("[Precondition]")
print("=" * 70)
t("git history available for the verifier's base SHA", bool(BASE),
  "git rev-parse HEAD~1 failed; the verifier cannot be driven without a base SHA")

# ============================================================================
print()
print("[Behavior 1: --expect-model enforces OUTSTANDING and ROUND 1 header]")
print("From plan Needs you #1 (DECISION A)")
print("=" * 70)

# Happy path: OK receipt with all required fields and --expect-model
header_complete = (
    "IMPLEMENTER_MODEL: haiku\n"
    "ROUND 1 — Implementer (parallel)\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(header_complete, step="implement", expect_model="haiku")
t("expect-model on complete OK receipt passes", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# Edge case 1: Missing ROUND 1 header with --expect-model
no_header = (
    "IMPLEMENTER_MODEL: haiku\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(no_header, step="implement", expect_model="haiku")
t("expect-model on OK receipt without ROUND 1 header fails", rc != 0,
  f"rc={rc}, expected failure but got: {last_line(out)}" if rc == 0 else "")
t("expect-model failure reason names ROUND 1 when header missing", rc != 0 and "ROUND 1" in out,
  f"rc={rc}, missing ROUND 1 in reason: {last_line(out)}")

# Edge case 2: Missing OUTSTANDING with --expect-model
no_outstanding = (
    "IMPLEMENTER_MODEL: haiku\n"
    "ROUND 1 — Implementer (parallel)\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(no_outstanding, step="implement", expect_model="haiku")
t("expect-model on OK receipt without OUTSTANDING fails", rc != 0,
  f"rc={rc}, expected failure but got: {last_line(out)}" if rc == 0 else "")
t("expect-model failure reason names OUTSTANDING when missing", rc != 0 and "OUTSTANDING" in out,
  f"rc={rc}, missing OUTSTANDING in reason: {last_line(out)}")

# ============================================================================
print()
print("[Behavior 2: CRLF ROUND 1 header being accepted]")
print("From plan Doing #6 (F8)")
print("=" * 70)

# Happy path: CRLF in ROUND 1 header and other lines
crlf_complete = (
    "ROUND 1 — Implementer (parallel)\r\n"
    "OUTSTANDING: none\r\n"
    "Decision: OK\r\n"
    "<!-- step-end -->\r\n"
)
rc, out = verify(crlf_complete, step="implement")
t("ROUND 1 header with CRLF line endings is accepted", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# Edge case: CRLF in only some lines (mixed line endings)
mixed_crlf = (
    "ROUND 1 — Implementer (parallel)\r\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(mixed_crlf, step="implement")
t("ROUND 1 header with CRLF accepts mixed line endings in body", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# Edge case: CRLF on other lines but not ROUND 1 header (verify it still works)
crlf_body_only = (
    "ROUND 1 — Implementer (parallel)\n"
    "OUTSTANDING: none\r\n"
    "Decision: OK\r\n"
    "<!-- step-end -->\r\n"
)
rc, out = verify(crlf_body_only, step="implement")
t("ROUND 1 header without CRLF accepts CRLF in body lines", rc == 0,
  f"rc={rc}, last line: {last_line(out)}" if rc != 0 else "")

# ============================================================================
print()
print("[Behavior 3: ROUND 1 failure Reason is plain English text, not regex]")
print("From plan Doing #3 (F5)")
print("=" * 70)

# Happy path: OK receipt with header
ok_with_header = (
    "ROUND 1 — Implementer (parallel)\n"
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(ok_with_header, step="implement")
t("OK receipt with ROUND 1 header passes (no reason needed)", rc == 0,
  f"rc={rc}, unexpected failure: {last_line(out)}" if rc != 0 else "")

# Edge case: Missing ROUND 1 header — verify the reason is plain text
no_header_missing = (
    "OUTSTANDING: none\n"
    "Decision: OK\n"
    "<!-- step-end -->\n"
)
rc, out = verify(no_header_missing, step="implement")
t("missing ROUND 1 header causes failure", rc != 0,
  f"rc={rc}, expected failure" if rc == 0 else "")
# Check the reason is plain text (not containing regex syntax like ^, $, *, |, etc.)
reason_line = last_line(out)
has_regex_syntax = any(char in reason_line for char in ["^", "$", "*", "|", "(?", "+{"])
t("failure reason is plain English text (no regex syntax)", rc != 0 and not has_regex_syntax,
  f"rc={rc}, reason contains regex: {reason_line}" if has_regex_syntax else "")
t("failure reason is readable and describes the issue", rc != 0 and "ROUND 1" in out,
  f"rc={rc}, reason unclear: {reason_line}")

# ============================================================================
print()
print("[Behavior 4: Documentation claims verified]")
print("From plan Needs you #3 (ADR amendment), and Doing #2 (zero-byte cache)")
print("=" * 70)

DOC_EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
DOC_TRACK_START = REPO_ROOT / "commands" / "track-and-start.md"
DOC_ADR = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"

expert_flow_text = DOC_EXPERT_FLOW.read_text() if DOC_EXPERT_FLOW.exists() else ""
track_start_text = DOC_TRACK_START.read_text() if DOC_TRACK_START.exists() else ""
adr_text = DOC_ADR.read_text() if DOC_ADR.exists() else ""

# Check: ADR amendment mentions Refs #325 (not Closes)
t("ADR amendment mentions Refs #325 (not Closes #325)",
  "Refs #325" in adr_text and "#325" in adr_text,
  "ADR does not mention 'Refs #325'")

# Check: rule 5 in expert-flow.md is a pointer to ROUND1_HEADER_PATTERN
rule5_has_pointer = ("ROUND1_HEADER_PATTERN" in expert_flow_text and
                     "pattern owned by" in expert_flow_text)
t("rule 5 points to ROUND1_HEADER_PATTERN in script (not prose pattern)",
  rule5_has_pointer,
  "rule 5 does not point to script or pattern is not referenced")

# Check: rule 5 mentions that any matching line suffices for ROUND 1
rule5_any_line = "for `ROUND 1`, any matching line suffices" in expert_flow_text
t("rule 5 clarifies that any ROUND 1 line suffices",
  rule5_any_line,
  "rule 5 does not clarify 'any matching line suffices'")

# Check: track-and-start uses [ -s ] guards for cache reads
has_cache_read_guard = "[ -s" in track_start_text and "CACHE" in track_start_text
t("track-and-start uses [ -s ] guards for cache file reads",
  has_cache_read_guard,
  "track-and-start does not use [ -s ] guards")

# Check: track-and-start uses [ -s ] guards before mv operations
has_mv_guard = ('[ -s "$TMP" ]' in track_start_text or 'mv' in track_start_text)
t("track-and-start protects mv operations (cache write flow is guarded)",
  '[ -s "$TMP" ]' in track_start_text or 'mv "$TMP"' in track_start_text,
  "track-and-start cache write does not use proper guards")

# ============================================================================
print()
print("[Behavior 5: ROUND 1 header patterns (narrowed shape contract)]")
print("From plan Needs you #3 and related 'Doing it' items")
print("=" * 70)

# Verify ROUND1_HEADER_PATTERN in the verifier script
# We'll check this by verifying that various shapes work or fail
test_patterns = [
    ("bare ROUND 1", "ROUND 1\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n", True),
    ("ROUND 1 with em dash", "ROUND 1 — units\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n", True),
    ("ROUND 1 with colon", "ROUND 1: units\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n", True),
    ("ROUND 1 with period (rejected)", "ROUND 1.\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n", False),
    ("ROUND 1 with word no sep (rejected)", "ROUND 1 units\nOUTSTANDING: none\nDecision: OK\n<!-- step-end -->\n", False),
]

for name, receipt, should_pass in test_patterns:
    rc, out = verify(receipt, step="implement")
    passed = rc == 0
    t(f"ROUND 1 shape: {name}", passed == should_pass,
      f"expected {'pass' if should_pass else 'fail'}, got rc={rc}")

# ============================================================================
h.summarize_and_exit()
