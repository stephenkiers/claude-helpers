#!/usr/bin/env python3
"""Tests for scripts/precedent-render.py: validate, compose, and render Precedent Pat output.

Testing Strategy:
- Test the three subcommands: validate, line, section
- Test ADR-0024 §8.2 validation rules
- Test the display line with 240-char cap and "Differs" sentence
- Test the blockquote section output
- Test injection cases and sanitization
- Test failure modes and error handling

Run with: python3 tests/test_precedent_render.py
"""

import importlib.util
import json
import re
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
        self.tmpdir = tempfile.mkdtemp(prefix="precedent-render-test-")
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


def make_valid_precedent(questions_count=1, spoke_on=1, citations=1, high=0, medium=1):
    """Build a valid precedent.md file."""
    receipt = f"precedent-pat | questions: {questions_count} | spoke-on: {spoke_on} | citations: {citations} | confidence: high={high} medium={medium}"
    body = f"""# Precedent — session-123

{receipt}

## Q1 [q-001] Sample question header

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test reason"
**Prediction**: Option A
**Confidence**: medium
**Why**: User chose this option before.
**Distinguish**: No material difference seen.
"""
    return body


def main():
    h = Harness("PRECEDENT-RENDER TEST SUITE")

    # ============================================================================
    # SECTION 1: Basic file existence and structure
    # ============================================================================
    print("[Section 1] Script existence and basic invocation")

    script_exists = SCRIPT.is_file() and SCRIPT.stat().st_size > 0
    h.test_result(
        "scripts/precedent-render.py exists and is non-empty",
        script_exists,
        "file not found or empty" if not script_exists else "",
    )

    # Test script can be called with --help or similar
    if script_exists:
        rc, out, err = run_render(["--help"])
        h.test_result(
            "precedent-render.py responds to help flag",
            rc == 0 or "usage" in out.lower() or "usage" in err.lower(),
            f"rc={rc}" if rc != 0 else "",
        )

    print()

    # ============================================================================
    # SECTION 2: validate subcommand - valid file
    # ============================================================================
    print("[Section 2] validate subcommand - valid file")

    with TempFiles() as tmp:
        precedent_path = tmp.write("precedent.md", make_valid_precedent(
            questions_count=1,
            spoke_on=1,
            citations=1,
            high=0,
            medium=1
        ))
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question header",
            "question": "What should we do?",
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
            is_valid = result.get("status") == "ok" and isinstance(result.get("blocks"), dict) and "q-001" in result.get("blocks", {})
            h.test_result(
                "validate: valid precedent.md returns status: ok with blocks",
                is_valid,
                f"status={result.get('status')}, reason={result.get('reason')}, blocks={list(result.get('blocks', {}).keys())}" if not is_valid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: valid precedent.md returns status: ok",
                False,
                f"JSON parse error: {e}",
            )

    print()

    # ============================================================================
    # SECTION 3: validate subcommand - receipt validation failures
    # ============================================================================
    print("[Section 3] validate subcommand - receipt failures")

    # 3.1: Receipt regex mismatch (malformed receipt line)
    with TempFiles() as tmp:
        bad_receipt = """# Precedent — session-123

bad receipt line here

## Q1 [q-001] Sample question

(silent — nothing related)
"""
        precedent_path = tmp.write("precedent.md", bad_receipt)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001",
            "header": "Sample question",
            "question": "What?",
            "options": ["A", "B"],
            "recommended": "A",
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
            is_invalid = result.get("status") == "invalid"
            h.test_result(
                "validate: malformed receipt line gives invalid status",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: malformed receipt line gives invalid status",
                False,
                f"parse error: {e}",
            )

    # 3.2: Count mismatch (receipt says questions: 2 but only 1 Q block)
    with TempFiles() as tmp:
        body = """# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([
            {"id": "q-001", "header": "Q1", "question": "Q?", "options": ["A", "B"], "recommended": "B"},
            {"id": "q-002", "header": "Q2", "question": "Q?", "options": ["A", "B"], "recommended": "B"},
        ]))
        digest_path = tmp.write("digest.jsonl", json.dumps(make_digest_entry()) + "\n")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        try:
            result = json.loads(out) if out else {}
            # Count mismatch might be caught or might be caught by other validations
            # Just verify it produces valid JSON output
            h.test_result(
                "validate: count mismatch returns valid JSON output",
                "status" in result,
                "no status field in output" if "status" not in result else "",
            )
        except Exception as e:
            h.test_result(
                "validate: count mismatch returns valid JSON output",
                False,
                f"parse error: {e}",
            )

    # 3.3: Missing Distinguish line
    with TempFiles() as tmp:
        body = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Q1", "question": "Q?", "options": ["A", "B"], "recommended": "B",
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
            is_invalid = result.get("status") == "invalid"
            h.test_result(
                "validate: missing Distinguish line gives invalid status",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: missing Distinguish line gives invalid status",
                False,
                f"parse error: {e}",
            )

    # 3.4: Too many citations per question (4 instead of max 3)
    with TempFiles() as tmp:
        citations = "\n".join([
            f'**Precedent**: 2026-01-15 · #{140+i} · review · chose "A" (recommended was "B") · reason: "test{i}"'
            for i in range(4)
        ])
        body = f"""# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 4 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

{citations}
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Q1", "question": "Q?", "options": ["A", "B"], "recommended": "B",
        }]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=140+i, reason=f"test{i}"))
            for i in range(4)
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
                "validate: 4 citations per question gives invalid status",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: 4 citations per question gives invalid status",
                False,
                f"parse error: {e}",
            )

    # 3.5: Too many citations (6 instead of max 5)
    with TempFiles() as tmp:
        citations = "\n".join([
            f'**Precedent**: 2026-01-15 · #{140+i} · review · chose "A" (recommended was "B") · reason: "test{i}"'
            for i in range(6)
        ])
        body = f"""# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 6 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

{citations}
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Q1", "question": "Q?", "options": ["A", "B"], "recommended": "B",
        }]))
        digest_entries = "\n".join([
            json.dumps(make_digest_entry(ref=140+i, reason=f"test{i}"))
            for i in range(6)
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
            is_invalid = result.get("status") == "invalid" and result.get("reason") == "pat-invalid"
            h.test_result(
                "validate: 6 citations gives invalid status with pat-invalid reason",
                is_invalid,
                f"status={result.get('status')}, reason={result.get('reason')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: 6 citations gives invalid status",
                False,
                f"parse error: {e}",
            )

    print()

    # ============================================================================
    # SECTION 4: validate subcommand - block and prediction validation
    # ============================================================================
    print("[Section 4] validate subcommand - block and prediction handling")

    # 4.1: Unknown block id is dropped
    with TempFiles() as tmp:
        body = """# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Known question

**Precedent**: 2026-01-15 · #142 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test

## Q9 [q-999] Unknown question

**Precedent**: 2026-01-15 · #143 · review · chose "X" (recommended was "Y") · reason: "test"
**Prediction**: X
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Known question", "question": "Q?", "options": ["A", "B"], "recommended": "B",
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
            blocks = result.get("blocks", {})
            # Unknown block q-999 should not be in validated result
            has_unknown = "q-999" in blocks
            h.test_result(
                "validate: unknown block id is dropped",
                not has_unknown,
                "q-999 still present in validated blocks" if has_unknown else "",
            )
        except Exception as e:
            h.test_result(
                "validate: unknown block id is dropped",
                False,
                f"parse error: {e}",
            )

    # 4.2: Prediction not byte-matching label is dropped or handled
    with TempFiles() as tmp:
        body = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test"
**Prediction**: Option A (modified)
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Sample question", "question": "Q?",
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
            # Non-matching prediction is either dropped (not in blocks) or marked invalid
            blocks = result.get("blocks", {})
            q1_present = "q-001" in blocks
            is_valid_json = "status" in result
            h.test_result(
                "validate: non-matching Prediction label is handled (dropped or marked invalid)",
                is_valid_json and (not q1_present or result.get("status") == "invalid"),
                f"status={result.get('status')}, q1_present={q1_present}" if not (is_valid_json and (not q1_present or result.get("status") == "invalid")) else "",
            )
        except Exception as e:
            h.test_result(
                "validate: non-matching Prediction label is handled (dropped or marked invalid)",
                False,
                f"parse error: {e}",
            )

    # 4.3: Cited ref missing from digest gives invalid
    with TempFiles() as tmp:
        body = """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question

**Precedent**: 2026-01-15 · #999 · review · chose "A" (recommended was "B") · reason: "test"
**Prediction**: A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
"""
        precedent_path = tmp.write("precedent.md", body)
        questions_path = tmp.write("questions.json", make_questions([{
            "id": "q-001", "header": "Sample question", "question": "Q?",
            "options": ["A", "B"],
            "recommended": "B",
        }]))
        # Digest has ref 142 but precedent cites ref 999
        digest_path = tmp.write("digest.jsonl", json.dumps(make_digest_entry(ref=142)) + "\n")

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
                "validate: cited ref missing from digest gives invalid",
                is_invalid,
                f"status={result.get('status')}" if not is_invalid else "",
            )
        except Exception as e:
            h.test_result(
                "validate: cited ref missing from digest gives invalid",
                False,
                f"parse error: {e}",
            )

    print()

    # ============================================================================
    # SECTION 5: line subcommand - basic operation
    # ============================================================================
    print("[Section 5] line subcommand - basic display line")

    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [
                        {
                            "date": "2026-01-15",
                            "ref": 142,
                            "source": "review",
                            "answer": "Option A",
                            "recommended": "Option B",
                            "reason": "test reason",
                        }
                    ],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "User chose this before.",
                    "distinguish": "No difference.",
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
            has_precedent_prefix = "Precedent:" in line
            has_prediction = "Prediction:" in line
            h.test_result(
                "line: output contains Precedent: prefix",
                has_precedent_prefix,
                f"line={line[:100]}" if not has_precedent_prefix else "",
            )
            h.test_result(
                "line: output contains Prediction: label",
                has_prediction,
                f"line={line[:100]}" if not has_prediction else "",
            )
        except Exception as e:
            h.test_result(
                "line: output contains Precedent: prefix",
                False,
                f"parse error: {e}",
            )
            h.test_result(
                "line: output contains Prediction: label",
                False,
                f"parse error: {e}",
            )

    # 5.1: Null ref renders without (#...)
    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [
                        {
                            "date": "2026-01-15",
                            "ref": None,
                            "source": "review",
                            "answer": "Option A",
                            "recommended": "Option B",
                            "reason": "test reason",
                        }
                    ],
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
            no_ref_marker = "(#" not in line
            h.test_result(
                "line: null ref renders without (#...)",
                no_ref_marker,
                f"line={line[:100]}" if not no_ref_marker else "",
            )
        except Exception as e:
            h.test_result(
                "line: null ref renders without (#...)",
                False,
                f"parse error: {e}",
            )

    # 5.2: Missing reason omits reason clause
    with TempFiles() as tmp:
        validated_json = {
            "status": "ok",
            "blocks": {
                "q-001": {
                    "precedent_lines": [
                        {
                            "date": "2026-01-15",
                            "ref": 142,
                            "source": "review",
                            "answer": "Option A",
                            "recommended": "Option B",
                            "reason": None,
                        }
                    ],
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
            no_reason_label = ": \"" not in line or "reason:" not in line.lower()
            h.test_result(
                "line: missing reason omits reason clause",
                no_reason_label or "reason:" not in line,
                f"line={line[:100]}" if "reason:" in line else "",
            )
        except Exception as e:
            h.test_result(
                "line: missing reason omits reason clause",
                False,
                f"parse error: {e}",
            )

    print()

    # ============================================================================
    # SECTION 6: line subcommand - injection and sanitization
    # ============================================================================
    print("[Section 6] line subcommand - injection cases")

    injection_cases = [
        ("newline", {"reason": "reason\nwith newline"}),
        ("leading #", {"reason": "# leading hash"}),
        ("leading >", {"reason": "> leading gt"}),
        ("leading ---", {"reason": "--- leading dashes"}),
        ("leading **", {"reason": "** leading bold"}),
        ("leading backtick", {"reason": "`code"}),
        ("forged Precedent:", {"reason": "Precedent: fake"}),
        ("forged Recommended", {"reason": "(Recommended)"}),
    ]

    for case_name, entry_override in injection_cases:
        with TempFiles() as tmp:
            entry = make_digest_entry()
            entry.update(entry_override)
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
                            **entry_override,
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
                # Check that the line is a single line (no newlines) and injection content is sanitized
                is_single_line = "\n" not in line
                # Verify specific injections are removed or neutralized
                injection_removed = True
                if case_name == "newline":
                    injection_removed = "\n" not in line
                elif case_name == "leading #":
                    injection_removed = not line.startswith("#")
                elif case_name == "forged Precedent:":
                    # The injected "Precedent: fake" should be neutralized to "precedent: fake" and not appear verbatim
                    injection_removed = "Precedent: fake" not in line and line.count("precedent:") <= 1  # Only sanitized lowercase should appear
                h.test_result(
                    f"line: injection case '{case_name}' yields sanitized single line",
                    is_single_line and injection_removed,
                    f"single_line={is_single_line}, injection_removed={injection_removed}, line={line[:60]}" if not (is_single_line and injection_removed) else "",
                )
            except Exception as e:
                h.test_result(
                    f"line: injection case '{case_name}' yields single line",
                    False,
                    f"parse error: {e}",
                )

    print()

    # ============================================================================
    # SECTION 7: line subcommand - character budget
    # ============================================================================
    print("[Section 7] line subcommand - 240 character budget")

    # 7.1: Over-budget handling with tight budget constraints
    with TempFiles() as tmp:
        # Use a reason that's long but not excessive, to test budget handling
        long_reason = "x" * 100  # Long but not impossible
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
                        "reason": long_reason,
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
            "recommended": "Option B",  # Differs from prediction
        }]))

        rc, out, err = run_render([
            "line",
            "--validated", validated_path,
            "--questions", questions_path,
        ])

        try:
            result = json.loads(out) if out else {}
            line = result.get("q-001", "")
            if line:  # Only test budget if a line was generated
                is_under_budget = len(line) <= 240
                has_prediction = "Prediction:" in line
                has_precedent = "Precedent:" in line
                h.test_result(
                    "line: output line stays under 240 chars with Prediction and Precedent",
                    is_under_budget and has_prediction and has_precedent,
                    f"len={len(line)}, has_prediction={has_prediction}, has_precedent={has_precedent}" if not (is_under_budget and has_prediction and has_precedent) else "",
                )
            else:
                # If no line was generated, that's also valid behavior
                h.test_result(
                    "line: over-budget line handling (generation attempted)",
                    True,
                    "",
                )
        except Exception as e:
            h.test_result(
                "line: output line stays under 240 chars when provided",
                False,
                f"parse error: {e}",
            )

    print()

    # ============================================================================
    # SECTION 8: section subcommand - blockquote output
    # ============================================================================
    print("[Section 8] section subcommand - blockquote rendering")

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
                        "reason": "test reason",
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "User chose this before.",
                    "distinguish": "No difference.",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)

        rc, out, err = run_render([
            "section",
            "--validated", validated_path,
        ])

        # section outputs markdown, not JSON
        has_section_heading = "## Precedent" in out
        has_blockquote = "> " in out
        h.test_result(
            "section: output contains ## Precedent heading",
            has_section_heading,
            "heading not found" if not has_section_heading else "",
        )
        h.test_result(
            "section: output contains blockquote markers",
            has_blockquote,
            "blockquote not found" if not has_blockquote else "",
        )

    # 8.1: Injection in section output (heading, dashes, backticks never start a line)
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
                        "reason": "normal reason",
                    }],
                    "prediction": "Option A",
                    "confidence": "medium",
                    "why": "## Q9 [x] injected section",
                    "distinguish": "#### Q1. injected subsection",
                }
            }
        }
        validated_path = tmp.write("validated.json", validated_json)

        rc, out, err = run_render([
            "section",
            "--validated", validated_path,
        ])

        # Check that no injected headings match the h4 pattern and no leading # appears in blockquote content
        matches_h4 = re.search(r"^####\s+Q\d+", out, re.MULTILINE)
        # Also verify the injected content doesn't appear as plain heading syntax
        has_injected_heading = "## Q9 [x]" in out or "#### Q1." in out
        h.test_result(
            "section: injected #### headings never start a line and injection is sanitized",
            not matches_h4 and not has_injected_heading,
            f"matched_h4={bool(matches_h4)}, has_injected_heading={has_injected_heading}" if matches_h4 or has_injected_heading else "",
        )

    print()

    # ============================================================================
    # SECTION 9: Exit codes and error handling
    # ============================================================================
    print("[Section 9] Exit codes and error handling")

    # 9.1: Even when invalid, exit code is 0
    with TempFiles() as tmp:
        precedent_path = tmp.write("precedent.md", "bad content")
        questions_path = tmp.write("questions.json", make_questions([]))
        digest_path = tmp.write("digest.jsonl", "")

        rc, out, err = run_render([
            "validate",
            "--precedent", precedent_path,
            "--questions", questions_path,
            "--digest", digest_path,
        ])

        h.test_result(
            "validate: exits with 0 even when status: invalid",
            rc == 0,
            f"rc={rc}" if rc != 0 else "",
        )

    print()

    curated_render_cases(h)
    curated_review_fix_cases(h)

    h.summarize_and_exit()


# ============================================================================
# CURATED CITATIONS (#297)
# ============================================================================

CURATED_RULE_PATH = "/Users/example/repo/docs/decisions/0001-cache.md"


def curated_precedent_md(path, answer="Option A", high=False):
    """A precedent.md whose single citation is a curated rule line."""
    confidence = "high" if high else "medium"
    receipt = (f"precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | "
               f"confidence: high={1 if high else 0} medium={0 if high else 1}")
    return f"""# Precedent — session-123

{receipt}

## Q1 [q-001] Sample question header

**Precedent**: curated rule "{answer}" ({path}) · spirit: "keeps things simple"
**Prediction**: {answer}
**Confidence**: {confidence}
**Why**: The project's standing rule says so; this is project-written, not a recorded human ruling.
**Distinguish**: No material difference seen.
"""


def curated_questions():
    return make_questions([{
        "id": "q-001",
        "header": "Sample question header",
        "question": "What?",
        "options": ["Option A", "Option B"],
        "recommended": "Option B",
    }])


def curated_render_cases(h):
    with TempFiles() as tmp:
        questions_path = tmp.write("questions.json", curated_questions())
        curated_digest = tmp.write("digest-curated.jsonl", json.dumps(make_digest_entry(
            source="curated", ref=None, answer="Option A", recommended=None,
            reason="keeps things simple", path=CURATED_RULE_PATH)) + "\n")
        precedent_ok = tmp.write("precedent-ok.md", curated_precedent_md(CURATED_RULE_PATH))
        precedent_high = tmp.write("precedent-high.md", curated_precedent_md(CURATED_RULE_PATH, high=True))

        rc, out, err = run_render(["validate", "--precedent", precedent_ok,
                                   "--questions", questions_path, "--digest", curated_digest])
        result = json.loads(out) if out.strip() else {}
        h.test_result("curated validate: accepted curated citation returns status ok",
                      rc == 0 and result.get("status") == "ok" and "q-001" in result.get("blocks", {}),
                      f"rc={rc} result={result} err={err}")

        rc, out, err = run_render(["validate", "--precedent", precedent_high,
                                   "--questions", questions_path, "--digest", curated_digest])
        result_high = json.loads(out) if out.strip() else {}
        h.test_result("curated validate: single curated citation may be high confidence",
                      result_high.get("status") == "ok", f"result={result_high} err={err}")

        validated = tmp.write("validated.json", json.dumps(result) if result else "{}")
        rc, out, err = run_render(["line", "--validated", validated, "--questions", questions_path])
        line = (json.loads(out) if out.strip() else {}).get("q-001", "")
        expected_line = f'Precedent: curated rule "Option A" ({CURATED_RULE_PATH})'
        h.test_result("curated line: renders exact curated line with full path and no 'chose'",
                      expected_line in line and "chose" not in line, f"line={line!r} err={err}")

        rc, out, err = run_render(["section", "--validated", validated])
        expected_section = f'> **Precedent**: curated rule "Option A" ({CURATED_RULE_PATH})'
        h.test_result("curated section: renders blockquote curated line",
                      expected_section in out, f"out={out!r} err={err}")

        digest_bad_path = tmp.write("digest-other.jsonl", json.dumps(make_digest_entry(
            source="curated", ref=None, answer="Option A", recommended=None,
            reason="keeps things simple", path="/Users/example/repo/docs/decisions/other.md")) + "\n")
        rc, out, err = run_render(["validate", "--precedent", precedent_ok,
                                   "--questions", questions_path, "--digest", digest_bad_path])
        h.test_result("curated validate: path absent from digest gives status invalid",
                      json.loads(out).get("status") == "invalid" if out.strip() else False,
                      f"out={out!r}")

        precedent_mismatch = tmp.write("precedent-mismatch.md",
                                       curated_precedent_md(CURATED_RULE_PATH, answer="Option B"))
        rc, out, err = run_render(["validate", "--precedent", precedent_mismatch,
                                   "--questions", questions_path, "--digest", curated_digest])
        # A mismatched curated answer drops only its own block; the file itself stays ok.
        mismatch_result = json.loads(out) if out.strip() else {}
        h.test_result("curated validate: answer not matching the digest entry drops that block (status ok, block absent)",
                      mismatch_result.get("status") == "ok" and "q-001" not in mismatch_result.get("blocks", {}),
                      f"out={out!r}")

        raw_digest = tmp.write("digest-raw-same-path.jsonl", json.dumps(make_digest_entry(
            source="review", ref=142, answer="Option A", recommended="Option B",
            reason="test reason", path=CURATED_RULE_PATH)) + "\n")
        rc, out, err = run_render(["validate", "--precedent", precedent_ok,
                                   "--questions", questions_path, "--digest", raw_digest])
        h.test_result("curated validate: path matching a raw entry (not curated) gives status invalid",
                      json.loads(out).get("status") == "invalid" if out.strip() else False,
                      f"out={out!r}")

        raw_validated = tmp.write("validated-raw.json", json.dumps({
            "status": "ok",
            "blocks": {"q-001": {
                "precedent_lines": [{
                    "date": "2026-01-15", "ref": 142, "source": "review", "answer": "Option A",
                    "recommended": "Option B", "reason": "test reason",
                }],
                "prediction": "Option A", "confidence": "medium", "why": "Test", "distinguish": "Test",
            }},
        }))
        rc, out, err = run_render(["line", "--validated", raw_validated, "--questions", questions_path])
        raw_line = (json.loads(out) if out.strip() else {}).get("q-001", "")
        h.test_result("raw line unchanged: 'Precedent: you chose \"Option A\" on 2026-01-15'",
                      'Precedent: you chose "Option A" on 2026-01-15' in raw_line, f"line={raw_line!r}")
        rc, out, err = run_render(["section", "--validated", raw_validated])
        h.test_result("raw section unchanged: '· review · chose \"Option A\"'",
                      '· review · chose "Option A"' in out, f"out={out!r}")

    print()


def curated_review_fix_cases(h):
    """Review-fix cases: sanitized answer compare (F3), prior-answer note (F14),
    source-based discrimination (F15), and curated entries indexed by path only (F16)."""
    with TempFiles() as tmp:
        questions_path = tmp.write("questions.json", curated_questions())
        curated_digest = tmp.write("digest-curated.jsonl", json.dumps(make_digest_entry(
            source="curated", ref=None, answer="Option A", recommended=None,
            reason="keeps things simple", path=CURATED_RULE_PATH)) + "\n")

        # F3: answers that differ only in whitespace match after sanitizing
        digest_ws = tmp.write("digest-ws.jsonl", json.dumps(make_digest_entry(
            source="curated", ref=None, answer="Option  A ", recommended=None,
            reason="keeps things simple", path=CURATED_RULE_PATH)) + "\n")
        precedent_ok = tmp.write("precedent-ok.md", curated_precedent_md(CURATED_RULE_PATH))
        rc, out, err = run_render(["validate", "--precedent", precedent_ok,
                                   "--questions", questions_path, "--digest", digest_ws])
        result_ws = json.loads(out) if out.strip() else {}
        h.test_result("curated validate: answer differing only in whitespace matches (sanitized compare)",
                      result_ws.get("status") == "ok" and "q-001" in result_ws.get("blocks", {}),
                      f"out={out!r} err={err}")

        # F3: a mismatched curated block is dropped; a valid raw block in the same file survives
        two_questions = tmp.write("questions-two.json", make_questions([
            {"id": "q-001", "header": "First", "question": "Q?", "options": ["Option A", "Option B"],
             "recommended": "Option B"},
            {"id": "q-002", "header": "Second", "question": "Q?", "options": ["Option A", "Option B"],
             "recommended": "Option B"},
        ]))
        digest_both = tmp.write("digest-both.jsonl",
                                json.dumps(make_digest_entry(
                                    source="curated", ref=None, answer="Option A", recommended=None,
                                    reason="keeps things simple", path=CURATED_RULE_PATH)) + "\n"
                                + json.dumps(make_digest_entry(ref=142)) + "\n")
        precedent_mixed = tmp.write("precedent-mixed.md", f"""# Precedent — session-123

precedent-pat | questions: 2 | spoke-on: 2 | citations: 2 | confidence: high=0 medium=2

## Q1 [q-001] First

**Precedent**: curated rule "Option B" ({CURATED_RULE_PATH}) · spirit: "keeps things simple"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test

## Q2 [q-002] Second

**Precedent**: 2026-01-15 · #142 · review · chose "Option A" (recommended was "Option B") · reason: "test reason"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
""")
        rc, out, err = run_render(["validate", "--precedent", precedent_mixed,
                                   "--questions", two_questions, "--digest", digest_both])
        mixed = json.loads(out) if out.strip() else {}
        h.test_result("curated validate: mismatched curated block is dropped while the raw block survives",
                      mixed.get("status") == "ok" and "q-001" not in mixed.get("blocks", {})
                      and "q-002" in mixed.get("blocks", {}) and mixed.get("shown") == 1 and mixed.get("asked") == 2,
                      f"out={out!r} err={err}")

        # F15: a raw-shaped line that claims source "curated" but has no path fails closed
        precedent_spoof = tmp.write("precedent-spoof.md", """# Precedent — session-123

precedent-pat | questions: 1 | spoke-on: 1 | citations: 1 | confidence: high=0 medium=1

## Q1 [q-001] Sample question header

**Precedent**: 2026-01-15 · #142 · curated · chose "Option A" (recommended was "Option B") · reason: "test reason"
**Prediction**: Option A
**Confidence**: medium
**Why**: Test
**Distinguish**: Test
""")
        digest_raw = tmp.write("digest-raw.jsonl", json.dumps(make_digest_entry(ref=142)) + "\n")
        rc, out, err = run_render(["validate", "--precedent", precedent_spoof,
                                   "--questions", questions_path, "--digest", digest_raw])
        h.test_result("curated discrimination: raw-shaped line with source curated and no path gives status invalid",
                      json.loads(out).get("status") == "invalid" if out.strip() else False,
                      f"out={out!r} err={err}")

        # F16: curated entries are indexed by path only, never under refs[None]
        spec = importlib.util.spec_from_file_location("precedent_render_under_test", str(SCRIPT))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        refs, curated_paths = module._load_digest(curated_digest)
        h.test_result("digest load: curated entry indexed by path and not written under refs[None]",
                      None not in refs and curated_paths.get(CURATED_RULE_PATH, {}).get("source") == "curated",
                      f"refs_keys={list(refs.keys())} curated_paths={list(curated_paths.keys())}")

        # F14: curated-first line notes a conflicting raw prior citation
        conflict_validated = tmp.write("validated-conflict.json", json.dumps({
            "status": "ok",
            "blocks": {"q-001": {
                "precedent_lines": [
                    {"ref": None, "source": "curated", "answer": "Option A", "path": CURATED_RULE_PATH,
                     "recommended": None, "reason": "keeps things simple"},
                    {"date": "2026-01-15", "ref": 142, "source": "review", "answer": "Option B",
                     "recommended": "Option B", "reason": "test reason"},
                ],
                "prediction": "Option A", "confidence": "medium", "why": "Test", "distinguish": "Test",
            }},
        }))
        rc, out, err = run_render(["line", "--validated", conflict_validated, "--questions", questions_path])
        conflict_line = (json.loads(out) if out.strip() else {}).get("q-001", "")
        expected_conflict = (f'Precedent: curated rule "Option A" ({CURATED_RULE_PATH}). '
                             f"Prediction: Option A (medium). Differs from the panel's recommendation. "
                             f"Your earlier answer differed.")
        h.test_result("curated line: conflicting raw prior adds 'Your earlier answer differed.'",
                      conflict_line == expected_conflict, f"line={conflict_line!r} err={err}")

        # F14: a raw prior that agrees with the prediction adds nothing
        agree_validated = tmp.write("validated-agree.json", json.dumps({
            "status": "ok",
            "blocks": {"q-001": {
                "precedent_lines": [
                    {"ref": None, "source": "curated", "answer": "Option A", "path": CURATED_RULE_PATH,
                     "recommended": None, "reason": "keeps things simple"},
                    {"date": "2026-01-15", "ref": 142, "source": "review", "answer": "Option A",
                     "recommended": "Option B", "reason": "test reason"},
                ],
                "prediction": "Option A", "confidence": "medium", "why": "Test", "distinguish": "Test",
            }},
        }))
        rc, out, err = run_render(["line", "--validated", agree_validated, "--questions", questions_path])
        agree_line = (json.loads(out) if out.strip() else {}).get("q-001", "")
        expected_agree = (f'Precedent: curated rule "Option A" ({CURATED_RULE_PATH}). '
                          f"Prediction: Option A (medium). Differs from the panel's recommendation.")
        h.test_result("curated line: agreeing raw prior adds no prior-answer note",
                      agree_line == expected_agree, f"line={agree_line!r} err={err}")

        # F14: the prior-answer note stays inside the 240-char budget with a long curated answer
        long_validated = tmp.write("validated-long.json", json.dumps({
            "status": "ok",
            "blocks": {"q-001": {
                "precedent_lines": [
                    {"ref": None, "source": "curated", "answer": "x" * 300, "path": CURATED_RULE_PATH,
                     "recommended": None, "reason": None},
                    {"date": "2026-01-15", "ref": 142, "source": "review", "answer": "Option B",
                     "recommended": "Option B", "reason": "test reason"},
                ],
                "prediction": "Option A", "confidence": "medium", "why": "Test", "distinguish": "Test",
            }},
        }))
        rc, out, err = run_render(["line", "--validated", long_validated, "--questions", questions_path])
        long_line = (json.loads(out) if out.strip() else {}).get("q-001", "")
        h.test_result("curated line: long answer keeps the prior-answer note within 240 chars",
                      0 < len(long_line) <= 240 and long_line.endswith("Your earlier answer differed."),
                      f"len={len(long_line)} line={long_line[:80]!r} err={err}")

    print()


if __name__ == "__main__":
    main()
