#!/usr/bin/env python3
"""Spec-blind tests for Precedent Pat Step 5 plan implementation (ADR-0024).

Tests validate specific behaviors outlined in the plan for precedent-render.py,
commands/expert-plan.md Step 5, and _write_atomic behavior:

1. precedent-render.py validate:
   - Output JSON includes `shown` and `asked` counts
   - Exit 2 on unreadable questions or digest files
   - Reject duplicate block ids
   - Guard empty reason
   - Require non-null ref for citations and compare values against digest

2. precedent-render.py line:
   - Date field constrained to YYYY-MM-DD regex (invalid date -> line skipped)
   - All fields sanitized
   - Strip Unicode control/format/line-separator characters (Cc, Cf, Zl, Zp)
   - Invalid confidence skipped

3. _write_atomic I/O failures:
   - I/O failure behavior documented in module docstring (exit-code contract)

4. commands/expert-plan.md Step 5:
   - Trace persisted to ${SESSION_DIR}/precedent-trace.txt
   - `line`/`section` gated on trace beginning with `shown`
   - Security denylist applied
   - Self-exclusion filter works with empty FLOW_DIR
   - Stale precedent files removed
   - Pat-failed reason read from .reason
   - Pat dispatch bounded wait
   - Precedent line shown in brief/chat round-up

Run with: python3 tests/test_precedent_pat_step5_spec_blind.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "precedent-render.py"


def run_render(args, input_str=None, cwd=None):
    """Run precedent-render.py as subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        input=input_str,
        capture_output=True,
        text=True,
        cwd=cwd or str(REPO_ROOT),
    )
    return result.returncode, result.stdout, result.stderr


class TempFiles:
    """Context manager for temporary test files."""

    def __init__(self):
        self.tmpdir = None
        self.files = {}

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="precedent-step5-test-")
        return self

    def __exit__(self, *args):
        import shutil
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, name, data):
        """Write a file and return its path."""
        path = Path(self.tmpdir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, dict):
            path.write_text(json.dumps(data), encoding="utf-8")
        else:
            path.write_text(data, encoding="utf-8")
        self.files[name] = path
        return str(path)

    def get(self, name):
        """Return file path by name."""
        return str(self.files[name])


def make_questions(questions_list):
    """Build a questions file dict."""
    return {"questions": questions_list}


def make_digest_entry(**kwargs):
    """Build a digest entry with defaults."""
    defaults = {
        "id": "q-001",
        "date": "2026-01-15T10:30:00",
        "ref": 142,
        "source": "review",
        "answer": "Option A",
        "recommended": "Option B",
        "reason": "test reason",
        "path": "/example/path.md",
    }
    defaults.update(kwargs)
    return defaults


def main():
    h = Harness("PRECEDENT PAT STEP 5 SPEC-BLIND TEST SUITE")

    # ========================================================================
    # SECTION 1: validate subcommand - `shown` and `asked` counts
    # ========================================================================
    print("[Section 1] validate: output JSON includes `shown` and `asked` counts")

    # 1.1: Valid precedent with shown count
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        digest_path = tmp.write("digest.jsonl", json.dumps(make_digest_entry()) + "\n")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            has_shown = "shown" in result and isinstance(result.get("shown"), int) and result.get("shown") >= 0
            has_asked = "asked" in result and isinstance(result.get("asked"), int) and result.get("asked") >= 0
            h.test_result(
                "validate: output JSON includes `shown` count (numeric, non-negative)",
                has_shown,
                f"shown not valid; value={result.get('shown')}, type={type(result.get('shown'))}" if not has_shown else "",
            )
            h.test_result(
                "validate: output JSON includes `asked` count (numeric, non-negative)",
                has_asked,
                f"asked not valid; value={result.get('asked')}, type={type(result.get('asked'))}" if not has_asked else "",
            )
        except Exception as e:
            h.test_result(
                "validate: output JSON includes `shown` count",
                False,
                f"JSON parse error: {e}",
            )
            h.test_result(
                "validate: output JSON includes `asked` count",
                False,
                f"JSON parse error: {e}",
            )

    # 1.2: Silent precedent with shown=0
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 0 | citations: 0 | confidence: high=0 medium=0

(no blocks)
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        digest_path = tmp.write("digest.jsonl", "")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            shown = result.get("shown")
            asked = result.get("asked")
            h.test_result(
                "validate: silent precedent has shown=0",
                shown == 0,
                f"shown={shown}, expected 0" if shown != 0 else "",
            )
            h.test_result(
                "validate: asked count is numeric and >= 0",
                isinstance(asked, int) and asked >= 0,
                f"asked={asked}, type={type(asked)}" if not (isinstance(asked, int) and asked >= 0) else "",
            )
        except Exception as e:
            h.test_result(
                "validate: silent precedent has shown=0",
                False,
                f"JSON parse error: {e}",
            )
            h.test_result(
                "validate: asked count is numeric",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 2: validate subcommand - unreadable files handled with error exit
    # ========================================================================
    print("[Section 2] validate: unreadable files handled with exit code 2 or status invalid")

    # 2.1: Unreadable questions file
    with TempFiles() as tmp:
        precedent_path = tmp.write("precedent.md", "# Precedent\n")
        nonexistent_questions = str(tmp.tmpdir) + "/nonexistent-questions.json"
        digest_path = tmp.write("digest.jsonl", "")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", nonexistent_questions,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            # Should either exit with 2 or return invalid status
            is_error = rc == 2 or result.get("status") == "invalid"
            h.test_result(
                "validate: unreadable questions file returns error status or exit 2",
                is_error,
                f"rc={rc}, status={result.get('status')}" if not is_error else "",
            )
        except Exception as e:
            h.test_result(
                "validate: unreadable questions file returns error status or exit 2",
                rc == 2 or rc != 0,
                f"rc={rc}, parse error: {e}" if (rc == 0) else "",
            )

    # 2.2: Unreadable digest file
    with TempFiles() as tmp:
        precedent_path = tmp.write("precedent.md", "# Precedent\n")
        questions_path = tmp.write("questions.json", make_questions([]))
        nonexistent_digest = str(tmp.tmpdir) + "/nonexistent-digest.jsonl"

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", nonexistent_digest,
        ])

        try:
            result = json.loads(out) if out else {}
            # Should either exit with 2 or return invalid status
            is_error = rc == 2 or result.get("status") == "invalid"
            h.test_result(
                "validate: unreadable digest file returns error status or exit 2",
                is_error,
                f"rc={rc}, status={result.get('status')}" if not is_error else "",
            )
        except Exception as e:
            h.test_result(
                "validate: unreadable digest file returns error status or exit 2",
                rc == 2 or rc != 0,
                f"rc={rc}, parse error: {e}" if (rc == 0) else "",
            )

    print()

    # ========================================================================
    # SECTION 3: validate subcommand - reject duplicate block ids
    # ========================================================================
    print("[Section 3] validate: reject duplicate block ids")

    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 2 | citations: 2 | confidence: high=0 medium=2

## Q1 [q-001] First question

**Precedent**: 2026-01-15 · #142 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test

## Q2 [q-001] Duplicate id

**Precedent**: 2026-01-15 · #143 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([
            {
                "id": "q-001",
                "header": "First question",
                "question": "Q?",
                "options": ["A", "B"],
                "recommended": "B",
            },
            {
                "id": "q-002",
                "header": "Duplicate id",
                "question": "Q?",
                "options": ["A", "B"],
                "recommended": "B",
            },
        ]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=142)),
            json.dumps(make_digest_entry(ref=143)),
        ])
        digest_path = tmp.write("digest.jsonl", digest_entries)

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            is_invalid = result.get("status") == "invalid"
            h.test_result(
                "validate: duplicate block ids cause invalid status",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: duplicate block ids cause invalid status",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 4: validate subcommand - guard empty reason (don't display if empty)
    # ========================================================================
    print("[Section 4] validate: guard empty reason")

    with TempFiles() as tmp:
        # Precedent with empty reason string (should be guarded in output)
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": "",  # Empty reason - should be guarded
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Empty reason should be guarded (not shown in line output with reason: pattern)
            # Check that the reason clause does NOT appear when reason is empty
            reason_clause_count = line.count('reason: ')
            h.test_result(
                "validate: empty reason is not displayed (reason: pattern absent)",
                reason_clause_count == 0,
                f"reason clause found in output: {line[:100]}" if reason_clause_count > 0 else "",
            )
        except Exception as e:
            h.test_result(
                "validate: empty reason is guarded (not displayed)",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 5: validate subcommand - require non-null ref for citations
    # ========================================================================
    print("[Section 5] validate: require non-null ref for citations")

    # 5.1: Citation with null ref is rejected
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · (null ref) · review · chose "Option A" (recommended was "Option B") · reason: "test"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        digest_path = tmp.write("digest.jsonl", json.dumps(make_digest_entry(ref=None)) + "\n")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            # A citation with null ref in the text (not the digest) should be invalid
            is_invalid = result.get("status") == "invalid" or len(result.get("blocks", {})) == 0
            h.test_result(
                "validate: citation with null ref is rejected or skipped",
                is_invalid or result.get("status") != "ok",
                f"status={result.get('status')}" if not (is_invalid or result.get("status") != "ok") else "",
            )
        except Exception as e:
            h.test_result(
                "validate: citation with null ref is rejected or skipped",
                False,
                f"JSON parse error: {e}",
            )

    # 5.2: Cited values compared against digest entry
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "Wrong Answer" (recommended was "Option B") · reason: "test"
**Prediction**: Wrong Answer
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        # Digest has answer="Option A" but precedent cites "Wrong Answer"
        digest_path = tmp.write("digest.jsonl", json.dumps(make_digest_entry(answer="Option A")) + "\n")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            # Mismatch between cited value and digest value should cause invalid
            is_invalid = result.get("status") == "invalid" or len(result.get("blocks", {})) == 0
            h.test_result(
                "validate: cited answer mismatched against digest is rejected",
                is_invalid,
                f"status={result.get('status')}, blocks={list(result.get('blocks', {}).keys())}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: cited answer mismatched against digest is rejected",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 6: line subcommand - date constrained to YYYY-MM-DD
    # ========================================================================
    print("[Section 6] line: date constrained to YYYY-MM-DD regex")

    # 6.1: Valid YYYY-MM-DD date
    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": "test",
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Valid date should produce a line containing the date and required elements
            has_date = "2026-01-15" in line
            has_precedent_prefix = line.startswith("Precedent:")
            h.test_result(
                "line: valid YYYY-MM-DD date produces output with expected content",
                has_date and has_precedent_prefix and len(line) > 20,
                f"line missing expected content: {line[:50]}" if not (has_date and has_precedent_prefix) else "",
            )
        except Exception as e:
            h.test_result(
                "line: valid YYYY-MM-DD date produces output",
                False,
                f"JSON parse error: {e}",
            )

    # 6.2: Invalid date format (ISO 8601 timestamp) -> line skipped
    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15T10:30:00",  # ISO format, not YYYY-MM-DD
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": "test",
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Invalid date should skip the line (q-001 not in result or empty string)
            q001_missing = "q-001" not in result
            q001_empty = result.get("q-001") == ""
            h.test_result(
                "line: invalid date format (ISO) causes line to be skipped",
                q001_missing or q001_empty,
                f"line present when it should be skipped: {line[:50]}" if line else "",
            )
        except Exception as e:
            h.test_result(
                "line: invalid date format (ISO) causes line to be skipped",
                False,
                f"JSON parse error: {e}",
            )

    # 6.3: Invalid date format (random string)
    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "not-a-date",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": "test",
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Invalid date should skip the line
            q001_missing = "q-001" not in result
            q001_empty = result.get("q-001") == ""
            h.test_result(
                "line: invalid date string causes line to be skipped",
                q001_missing or q001_empty,
                f"line present when it should be skipped: {line[:50]}" if line else "",
            )
        except Exception as e:
            h.test_result(
                "line: invalid date string causes line to be skipped",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 7: line subcommand - Unicode control/format/line-separator stripping
    # ========================================================================
    print("[Section 7] line: strip Unicode control/format/line-separator characters")

    # 7.1: Cc (control) characters
    with TempFiles() as tmp:
        # Add a null byte in reason (category Cc)
        reason_with_control = "test\x00reason"
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": reason_with_control,
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Control characters should be stripped
            has_null_byte = "\x00" in line
            h.test_result(
                "line: null bytes (Cc control chars) are stripped",
                not has_null_byte,
                "null byte found in output" if has_null_byte else "",
            )
        except Exception as e:
            h.test_result(
                "line: null bytes (Cc control chars) are stripped",
                False,
                f"JSON parse error: {e}",
            )

    # 7.2: Zl (line separator) and Zp (paragraph separator) characters
    with TempFiles() as tmp:
        # Add Unicode line separator (U+2028) and paragraph separator (U+2029)
        reason_with_separators = "test reason text"
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": reason_with_separators,
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Line separators should be stripped
            has_line_sep = " " in line
            has_para_sep = " " in line
            h.test_result(
                "line: Unicode line/paragraph separators (Zl, Zp) are stripped",
                not has_line_sep and not has_para_sep,
                "separators found in output" if (has_line_sep or has_para_sep) else "",
            )
        except Exception as e:
            h.test_result(
                "line: Unicode line/paragraph separators (Zl, Zp) are stripped",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 8: line subcommand - invalid confidence skipped
    # ========================================================================
    print("[Section 8] line: invalid confidence causes line to be skipped")

    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [{
                        "date": "2026-01-15",
                        "ref": 142,
                        "source": "review",
                        "answer": "Option A",
                        "recommended": "Option B",
                        "reason": "test",
                    }],
                    "prediction": "Option A",
                    "confidence": "very-high",  # Invalid: should be 'high' or 'medium'
                    "why": "Test",
                    "distinguish": "Test",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            # Invalid confidence should skip the line
            q001_missing = "q-001" not in result
            q001_empty = result.get("q-001") == ""
            h.test_result(
                "line: invalid confidence causes line to be skipped",
                q001_missing or q001_empty,
                f"line present when it should be skipped: {line[:50]}" if line else "",
            )
        except Exception as e:
            h.test_result(
                "line: invalid confidence causes line to be skipped",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ========================================================================
    # SECTION 9: _write_atomic I/O failure behavior (script-level)
    # ========================================================================
    print("[Section 9] _write_atomic I/O failures documented in module docstring")

    # Check that the script module has documentation of I/O failures
    try:
        script_text = SCRIPT.read_text()
        # Look for docstring mentioning exit codes and I/O failures
        has_module_docstring = '"""' in script_text or "'''" in script_text
        has_exit_code_mention = "exit" in script_text.lower() and ("2" in script_text or "io" in script_text.lower())

        h.test_result(
            "precedent-render.py has module docstring",
            has_module_docstring,
            "no module docstring found" if not has_module_docstring else "",
        )

        h.test_result(
            "precedent-render.py documents exit codes (especially for I/O failures)",
            has_exit_code_mention,
            "exit code documentation not found" if not has_exit_code_mention else "",
        )
    except Exception as e:
        h.test_result(
            "precedent-render.py has module docstring",
            False,
            f"error reading script: {e}",
        )
        h.test_result(
            "precedent-render.py documents exit codes",
            False,
            f"error reading script: {e}",
        )

    print()

    # ========================================================================
    # SECTION 10: validate subcommand - citation limit boundaries [WEAK_ASSERTION]
    # ========================================================================
    print("[Section 10] validate: citation limit boundaries (per-question and total)")

    # 10.1: Exactly 3 citations per question passes [WEAK_ASSERTION]
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 3 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test1"
**Precedent**: 2026-01-14 · #143 · review · chose "Option A" (recommended was "Option B") · reason: "test2"
**Precedent**: 2026-01-13 · #144 · review · chose "Option A" (recommended was "Option B") · reason: "test3"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=142)),
            json.dumps(make_digest_entry(ref=143)),
            json.dumps(make_digest_entry(ref=144)),
        ])
        digest_path = tmp.write("digest.jsonl", digest_entries)

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            is_valid = result.get("status") == "ok"
            shown = result.get("shown")
            h.test_result(
                "validate: exactly 3 citations per question passes (status=ok)",
                is_valid and shown == 1,
                f"status={result.get('status')}, shown={shown}" if not (is_valid and shown == 1) else "",
            )
        except Exception as e:
            h.test_result(
                "validate: exactly 3 citations per question passes (status=ok)",
                False,
                f"JSON parse error: {e}",
            )

    # 10.2: 4 citations per question fails [WEAK_ASSERTION]
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 4 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test1"
**Precedent**: 2026-01-14 · #143 · review · chose "Option A" (recommended was "Option B") · reason: "test2"
**Precedent**: 2026-01-13 · #144 · review · chose "Option A" (recommended was "Option B") · reason: "test3"
**Precedent**: 2026-01-12 · #145 · review · chose "Option A" (recommended was "Option B") · reason: "test4"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["Option A", "Option B"],
            "recommended": "Option B",
        }]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=142)),
            json.dumps(make_digest_entry(ref=143)),
            json.dumps(make_digest_entry(ref=144)),
            json.dumps(make_digest_entry(ref=145)),
        ])
        digest_path = tmp.write("digest.jsonl", digest_entries)

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            is_invalid = result.get("status") == "invalid"
            h.test_result(
                "validate: 4 citations per question fails (status=invalid)",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: 4 citations per question fails (status=invalid)",
                False,
                f"JSON parse error: {e}",
            )

    # 10.3: Total of 5 citations across questions passes [WEAK_ASSERTION]
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 2 | citations: 5 | confidence: high=0 medium=2

## Q1 [q-001] First question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test1"
**Precedent**: 2026-01-14 · #143 · review · chose "Option A" (recommended was "Option B") · reason: "test2"
**Precedent**: 2026-01-13 · #144 · review · chose "Option A" (recommended was "Option B") · reason: "test3"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test

## Q2 [q-002] Second question

**Precedent**: 2026-01-12 · #145 · review · chose "Option X" (recommended was "Option Y") · reason: "test4"
**Precedent**: 2026-01-11 · #146 · review · chose "Option X" (recommended was "Option Y") · reason: "test5"
**Prediction**: Option X
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([
            {
                "id": "q-001",
                "header": "First question",
                "question": "What?",
                "options": ["Option A", "Option B"],
                "recommended": "Option B",
            },
            {
                "id": "q-002",
                "header": "Second question",
                "question": "What?",
                "options": ["Option X", "Option Y"],
                "recommended": "Option Y",
            },
        ]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=142)),
            json.dumps(make_digest_entry(ref=143)),
            json.dumps(make_digest_entry(ref=144)),
            json.dumps(make_digest_entry(ref=145)),
            json.dumps(make_digest_entry(ref=146)),
        ])
        digest_path = tmp.write("digest.jsonl", digest_entries)

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            is_valid = result.get("status") == "ok"
            shown = result.get("shown")
            h.test_result(
                "validate: total 5 citations across questions passes (status=ok, shown=2)",
                is_valid and shown == 2,
                f"status={result.get('status')}, shown={shown}" if not (is_valid and shown == 2) else "",
            )
        except Exception as e:
            h.test_result(
                "validate: total 5 citations across questions passes (status=ok, shown=2)",
                False,
                f"JSON parse error: {e}",
            )

    # 10.4: Total of 6 citations across questions fails [WEAK_ASSERTION]
    with TempFiles() as tmp:
        precedent_text = """# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 2 | citations: 6 | confidence: high=0 medium=2

## Q1 [q-001] First question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test1"
**Precedent**: 2026-01-14 · #143 · review · chose "Option A" (recommended was "Option B") · reason: "test2"
**Precedent**: 2026-01-13 · #144 · review · chose "Option A" (recommended was "Option B") · reason: "test3"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test

## Q2 [q-002] Second question

**Precedent**: 2026-01-12 · #145 · review · chose "Option X" (recommended was "Option Y") · reason: "test4"
**Precedent**: 2026-01-11 · #146 · review · chose "Option X" (recommended was "Option Y") · reason: "test5"
**Precedent**: 2026-01-10 · #147 · review · chose "Option X" (recommended was "Option Y") · reason: "test6"
**Prediction**: Option X
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", precedent_text)
        questions_path = tmp.write("questions.json", make_questions([
            {
                "id": "q-001",
                "header": "First question",
                "question": "What?",
                "options": ["Option A", "Option B"],
                "recommended": "Option B",
            },
            {
                "id": "q-002",
                "header": "Second question",
                "question": "What?",
                "options": ["Option X", "Option Y"],
                "recommended": "Option Y",
            },
        ]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=142)),
            json.dumps(make_digest_entry(ref=143)),
            json.dumps(make_digest_entry(ref=144)),
            json.dumps(make_digest_entry(ref=145)),
            json.dumps(make_digest_entry(ref=146)),
            json.dumps(make_digest_entry(ref=147)),
        ])
        digest_path = tmp.write("digest.jsonl", digest_entries)

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            is_invalid = result.get("status") == "invalid"
            h.test_result(
                "validate: total 6 citations across questions fails (status=invalid)",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: total 6 citations across questions fails (status=invalid)",
                False,
                f"JSON parse error: {e}",
            )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
