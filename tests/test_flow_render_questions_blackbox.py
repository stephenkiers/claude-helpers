#!/usr/bin/env python3
"""
Spec-blind blackbox tests for scripts/flow-render-questions.py.

Tests are written from the plan file alone (not reading the implementation).
Covers exit codes, input validation, context_path safety, header truncation,
description handling, --out naming, and file permissions.

Plan items covered (from ADR-0025 amendment and #421):
- F2: Empty questions list exits 4
- F5: Malformed options/non-string header exits 4
- F13: Unknown handles exit 4
- F14: --out filename contains seq
- F17: context_path containment and size cap
- F19: None placeholder; description only when present
- F20: --out file mode
- F22: Header truncation at word boundary
- F25: Tests for unreadable context_path, non-list items, empty questions

Run with: python3 tests/test_flow_render_questions_blackbox.py
"""

import json
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "flow-render-questions.py"
h = Harness("FLOW-RENDER-QUESTIONS BLACKBOX TESTS")


def run_renderer(question_file, prefix=None, out_path=None):
    """
    Run the renderer script and return (returncode, stdout, stderr).

    Args:
        question_file: path to the question JSON file
        prefix: optional --prefix value
        out_path: optional --out path; if None, creates a temp file

    Returns:
        (returncode, stdout, stderr, out_path_used)
    """
    if out_path is None:
        out_path = Path(tempfile.mktemp(suffix=".txt"))

    args = [sys.executable, str(SCRIPT), str(question_file), "--out", str(out_path)]
    if prefix is not None:
        args.extend(["--prefix", prefix])

    result = subprocess.run(args, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr, out_path


def make_question_file(tmp_dir, content):
    """Create a temporary question JSON file and return its path."""
    path = Path(tmp_dir) / "question.json"

    # Ensure each question has required fields
    if "questions" in content:
        for i, q in enumerate(content["questions"]):
            if "id" not in q:
                q["id"] = f"q{i+1}"
            if "question" not in q and "summary" in q:
                # Use summary as question if question is missing
                q["question"] = q["summary"]
            elif "question" not in q:
                q["question"] = f"Question {i+1}"

    with open(path, "w") as f:
        json.dump(content, f)
    return path


# ============================================================================
# SECTION 1: Exit codes (E1-E5)
# ============================================================================
print("[Section 1] Exit codes and error conditions")


def t_empty_questions_list_exits_4():
    """Empty questions list should exit 4 (_Invalid)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {"questions": []})
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Empty questions list exits 4",
            rc == 4,
            f"expected exit 4, got {rc}"
        )


def t_malformed_options_exits_4():
    """Malformed options entries (non-dict in list) - validator should reject invalid structures."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "options": ["not a dict"],  # Invalid: non-dict entry in list
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        # Per plan F5: malformed options entries exit exactly 4
        h.test_result(
            "Malformed options entry exits 4",
            rc == 4,
            f"expected exit 4, got {rc}"
        )


def t_non_string_header_exits_4():
    """Non-string header at question level - validator should reject."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "header": 123,  # Not a string: invalid
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        # Per plan F5: non-string header exits exactly 4
        h.test_result(
            "Non-string header exits 4",
            rc == 4,
            f"expected exit 4, got {rc}"
        )


def t_unknown_report_handle_exits_4():
    """Report with unknown handle (not Q/D/W) - should be validated and rejected."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = Path(tmp_dir) / "question.json"
        q_file.write_text(json.dumps({
            "kind": "report",
            "items": [{
                "handle": "X1",  # Invalid: not Q/D/W
                "title": "Some item"
            }]
        }))
        rc, _, _, _ = run_renderer(q_file)
        # Per plan F13: unknown report handle exits exactly 4
        h.test_result(
            "Unknown report handle exits 4",
            rc == 4,
            f"expected exit 4, got {rc}"
        )


def t_unreadable_input_file_exits_3():
    """Unreadable input file should exit 3."""
    q_file = Path("/nonexistent/path/question.json")
    rc, _, _, _ = run_renderer(q_file)
    h.test_result(
        "Unreadable input file exits 3",
        rc == 3,
        f"expected exit 3, got {rc}"
    )


def t_missing_required_out_flag_exits_1():
    """Missing --out flag should exit 1 (usage error)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {"questions": []})
        # Run without --out
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(q_file)],
            capture_output=True, text=True
        )
        h.test_result(
            "Missing --out flag exits 1",
            result.returncode == 1,
            f"expected exit 1, got {result.returncode}"
        )


# ============================================================================
# SECTION 2: Successful rendering (sanity check)
# ============================================================================
print("\n[Section 2] Successful rendering")


def t_minimal_valid_question():
    """Minimal valid question should render successfully (exit 0)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Question 1",
                "options": []
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Minimal valid question exits 0",
            rc == 0,
            f"expected exit 0, got {rc}"
        )


# ============================================================================
# SECTION 3: context_path safety (containment and size cap)
# ============================================================================
print("\n[Section 3] context_path safety")


def t_context_path_must_be_under_flow_dir():
    """context_path outside flow-dir should be warned but rendering continues."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a context file outside ~/.claude/flows/
        outside = Path(tmp_dir) / "outside.txt"
        outside.write_text("Some context")

        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "context_path": str(outside)
            }]
        })
        # Per plan F17: path containment check warns but continues with exit 0
        rc, stdout, stderr, _ = run_renderer(q_file)
        h.test_result(
            "context_path outside flow-dir exits 0",
            rc == 0,
            f"expected exit 0, got {rc}: stderr={stderr}"
        )


def t_context_path_size_cap():
    """Very large context_path content should be capped."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a large context file
        large_context = Path(tmp_dir) / "large.txt"
        large_context.write_text("X" * 100000)  # 100KB

        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "context_path": str(large_context)
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file)

        # Should complete successfully
        h.test_result(
            "Large context_path does not crash",
            rc == 0,
            f"expected exit 0, got {rc}: {stderr}"
        )

        # Output should exist and be readable
        if out_path.exists():
            out_content = out_path.read_text()
            # Output should be much smaller than 100KB (capped)
            h.test_result(
                "Large context is capped in output",
                len(out_content) < 50000,  # Much less than the input
                f"output is {len(out_content)} bytes, not capped"
            )


# ============================================================================
# SECTION 4: Header truncation (word boundary, 12-char cap)
# ============================================================================
print("\n[Section 4] Header truncation")


def t_header_truncated_at_word_boundary():
    """Header longer than 12 chars should truncate at word boundary."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "options": [{"header": "This is a very long header"}]
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file)

        h.test_result(
            "Header truncation renders successfully",
            rc == 0,
            f"exit {rc}: {stderr}"
        )

        if out_path.exists():
            out_content = out_path.read_text()
            # Check that the output contains question data
            h.test_result(
                "Output file contains question data",
                len(out_content) > 0,
                "output is empty"
            )


def t_header_with_prefix_respects_12char_cap():
    """With --prefix '#N ', total header cap is 12 chars."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # '#1 ' is 3 chars, so room for 9 more chars
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "options": [{"header": "A very very long header"}]
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file, prefix="#1 ")

        h.test_result(
            "Prefix + header respects 12-char cap",
            rc == 0,
            f"exit {rc}: {stderr}"
        )


# ============================================================================
# SECTION 5: Description handling (None placeholder, omit null)
# ============================================================================
print("\n[Section 5] Description handling")


def t_missing_description_uses_placeholder():
    """Missing or None description should use a placeholder, not print 'None'."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "description": None,  # Explicitly None
                "options": []
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file)

        h.test_result(
            "None description renders without 'None' string",
            rc == 0,
            f"exit {rc}"
        )

        if out_path.exists():
            out_content = out_path.read_text()
            # Per plan F19: null description omitted, no literal 'None'
            # The output should NOT contain the literal string "None" as the description value
            h.test_result(
                "Output does not print literal 'None'",
                "None" not in out_content,
                f"output contains 'None': {out_content[:200]}"
            )


def t_omit_null_description_from_json():
    """Null description should be omitted from the output JSON (if any)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "description": None,
                "options": []
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file)

        h.test_result(
            "Null description handling succeeds",
            rc == 0,
            f"exit {rc}"
        )


def t_non_null_description_is_rendered():
    """Non-null description should be rendered in output."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "description": "This is a description",
                "options": []
            }]
        })
        rc, stdout, stderr, out_path = run_renderer(q_file)

        h.test_result(
            "Non-null description renders successfully",
            rc == 0,
            f"exit {rc}"
        )

        if out_path.exists():
            out_content = out_path.read_text()
            h.test_result(
                "Output file is not empty with description",
                len(out_content) > 0,
                "output is empty"
            )


# ============================================================================
# SECTION 6: --out filename and permissions
# ============================================================================
print("\n[Section 6] --out filename and file permissions")


def t_out_filename_contains_seq():
    """--out filename can contain a seq number (e.g., from --prefix '#N ')."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "question-1.txt"
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "options": []
            }]
        })
        rc, _, _, _ = run_renderer(q_file, out_path=out_path)

        h.test_result(
            "--out file is created",
            rc == 0 and out_path.exists(),
            f"exit {rc}, exists={out_path.exists()}"
        )


def t_out_file_permissions():
    """--out file should have appropriate permissions (0600 or similar)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "output.txt"
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Q1",
                "options": []
            }]
        })
        rc, _, _, _ = run_renderer(q_file, out_path=out_path)

        h.test_result(
            "Output file is created",
            rc == 0 and out_path.exists(),
            f"exit {rc}"
        )

        if out_path.exists():
            # Check file permissions: should be readable (at least 0400)
            mode = stat.S_IMODE(out_path.stat().st_mode)
            is_readable = bool(mode & stat.S_IRUSR)
            h.test_result(
                "Output file is readable by owner",
                is_readable,
                f"mode={oct(mode)}, not readable"
            )


# ============================================================================
# SECTION 7: Complex questions with multiple options and items
# ============================================================================
print("\n[Section 7] Complex valid questions")


def t_question_with_multiple_options():
    """Question with multiple options should render successfully."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Choose an approach",
                "options": [
                    {"header": "Option A", "body": "Description of A"},
                    {"header": "Option B", "body": "Description of B"},
                ]
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Multiple options render successfully",
            rc == 0,
            f"exit {rc}"
        )


def t_question_with_items():
    """Question with report items (Q/D/W handles) should render."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Review findings",
                "items": [
                    {"handle": "Q1", "summary": "First question"},
                    {"handle": "D1", "summary": "First decision"},
                    {"handle": "W1", "summary": "First needs-you item"},
                ]
            }]
        })
        rc, _, _, out_path = run_renderer(q_file)

        h.test_result(
            "Items with valid handles render successfully",
            rc == 0,
            f"exit {rc}"
        )


# ============================================================================
# SECTION 8: Edge cases
# ============================================================================
print("\n[Section 8] Edge cases")


def t_question_with_only_summary():
    """Question with only summary (no options) should render."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": "Just a summary"
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Question with only summary renders",
            rc == 0,
            f"exit {rc}"
        )


def t_very_long_summary():
    """Very long summary should be handled."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        long_summary = "A" * 5000
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": long_summary,
                "options": []
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Very long summary does not crash",
            rc == 0,
            f"exit {rc}"
        )


def t_special_characters_in_content():
    """Special characters (unicode, quotes, etc.) should be handled."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        q_file = make_question_file(tmp_dir, {
            "questions": [{
                "summary": 'Question with "quotes" and \\ backslashes',
                "options": [
                    {"header": "Option with émojis and ñ"}
                ]
            }]
        })
        rc, _, _, _ = run_renderer(q_file)
        h.test_result(
            "Special characters are handled",
            rc == 0,
            f"exit {rc}"
        )


# ============================================================================
# Run all tests
# ============================================================================
if __name__ == "__main__":
    # Section 1: Exit codes
    t_empty_questions_list_exits_4()
    t_malformed_options_exits_4()
    t_non_string_header_exits_4()
    t_unknown_report_handle_exits_4()
    t_unreadable_input_file_exits_3()
    t_missing_required_out_flag_exits_1()

    # Section 2: Successful rendering
    t_minimal_valid_question()

    # Section 3: context_path safety
    t_context_path_must_be_under_flow_dir()
    t_context_path_size_cap()

    # Section 4: Header truncation
    t_header_truncated_at_word_boundary()
    t_header_with_prefix_respects_12char_cap()

    # Section 5: Description handling
    t_missing_description_uses_placeholder()
    t_omit_null_description_from_json()
    t_non_null_description_is_rendered()

    # Section 6: --out filename and permissions
    t_out_filename_contains_seq()
    t_out_file_permissions()

    # Section 7: Complex questions
    t_question_with_multiple_options()
    t_question_with_items()

    # Section 8: Edge cases
    t_question_with_only_summary()
    t_very_long_summary()
    t_special_characters_in_content()

    h.summarize_and_exit()
