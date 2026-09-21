#!/usr/bin/env python3
"""
Test suite for reviewer-yield.py metrics and reporting.

Covers:
1. Token parsing from message.usage with de-duplication by message["id"]
2. Handling missing cache fields with defaults
3. Skipping entries with missing/non-dict message or usage
4. De-duplication: two entries sharing message["id"] are counted once
5. Bounded transcript discovery via transcript-origin.json
6. transcript-origin.json missing → zero tokens, unavailable status, stderr warning
7. Reviewer name resolution (slug + display name matching)
8. Size bucketing from changed-line counts
9. Findings.json parsing and verdict classification
10. Regime classification from timestamp
11. Report generation with proper exclusions and aggregation
12. JSON and Markdown outputs derived from same data structure
13. Script executable bit
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

# Import the module we're testing (load as a module from the scripts directory)
import importlib.util
spec = importlib.util.spec_from_file_location("reviewer_yield", REPO_ROOT / "scripts" / "reviewer-yield.py")
reviewer_yield = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer_yield)

harness = Harness("Reviewer Yield Tests")

# Test 1: Token sums are read correctly from message.usage across multiple entries
def test_token_parsing_multientry():
    """Token parsing sums across multiple assistant entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript = Path(tmpdir) / "test.jsonl"
        # Write test transcript with multiple entries
        transcript.write_text("""{"type": "assistant", "message": {"id": "msg-1", "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}}
{"type": "assistant", "message": {"id": "msg-2", "usage": {"input_tokens": 500, "output_tokens": 200, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 25}}}
""")

        tokens = reviewer_yield.parse_tokens_from_subagent(transcript)
        passed = (
            tokens["input_tokens"] == 1500 and
            tokens["output_tokens"] == 700 and
            tokens["cache_read_input_tokens"] == 150 and
            tokens["cache_creation_input_tokens"] == 75
        )
        harness.test_result("Token parsing: multiple entries summed correctly", passed)


# Test 2: Missing cache fields parse to 0 without raising
def test_token_parsing_missing_cache_fields():
    """Missing cache fields default to 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript = Path(tmpdir) / "test.jsonl"
        transcript.write_text("""{"type": "assistant", "message": {"id": "msg-1", "usage": {"input_tokens": 100, "output_tokens": 50}}}
""")

        tokens = reviewer_yield.parse_tokens_from_subagent(transcript)
        passed = (
            tokens["input_tokens"] == 100 and
            tokens["output_tokens"] == 50 and
            tokens["cache_read_input_tokens"] == 0 and
            tokens["cache_creation_input_tokens"] == 0
        )
        harness.test_result("Token parsing: missing cache fields default to 0", passed)


# Test 3: Entry missing message key or non-dict usage is skipped
def test_token_parsing_skip_malformed():
    """Entries with missing message or non-dict usage are skipped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript = Path(tmpdir) / "test.jsonl"
        transcript.write_text("""{"type": "assistant"}
{"type": "assistant", "message": {"id": "msg-1", "usage": "not-a-dict"}}
{"type": "assistant", "message": {"id": "msg-2", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
""")

        tokens = reviewer_yield.parse_tokens_from_subagent(transcript)
        passed = (
            tokens["input_tokens"] == 100 and
            tokens["output_tokens"] == 50
        )
        harness.test_result("Token parsing: skips malformed entries", passed)


# Test 4: De-duplication by message["id"]
def test_token_parsing_dedup_by_id():
    """Two entries sharing message["id"] are counted once."""
    with tempfile.TemporaryDirectory() as tmpdir:
        transcript = Path(tmpdir) / "test.jsonl"
        transcript.write_text("""{"type": "assistant", "message": {"id": "msg-1", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
{"type": "assistant", "message": {"id": "msg-1", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
{"type": "assistant", "message": {"id": "msg-2", "usage": {"input_tokens": 50, "output_tokens": 25, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
""")

        tokens = reviewer_yield.parse_tokens_from_subagent(transcript)
        # Should count msg-1 once (100+50) and msg-2 once (50+25)
        passed = (
            tokens["input_tokens"] == 150 and
            tokens["output_tokens"] == 75
        )
        harness.test_result("Token parsing: de-duplicates by message['id']", passed)


# Test 5: Bounded transcript discovery with transcript-origin.json
def test_bounded_transcript_discovery():
    """Discovery reads only the session dir named by transcript-origin.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        review_dir = Path(tmpdir) / "review"
        review_dir.mkdir()

        # Write transcript-origin.json
        origin_data = {
            "schema_version": 1,
            "cwd": "/test",
            "project_dir": "test-project",
            "session_id": "correct-session",
            "resolution": "env",
            "recorded_at": "2026-09-17T12:00:00Z"
        }
        (review_dir / "transcript-origin.json").write_text(json.dumps(origin_data))

        # Create projects directory structure
        projects_dir = Path(tmpdir) / ".claude" / "projects" / "test-project"
        correct_session = projects_dir / "correct-session" / "subagents"
        correct_session.mkdir(parents=True, exist_ok=True)

        # Create decoy session with matching transcript
        decoy_session = projects_dir / "decoy-session" / "subagents"
        decoy_session.mkdir(parents=True, exist_ok=True)

        # Write matching transcript in correct session
        correct_transcript = correct_session / "uncle-bob.jsonl"
        correct_transcript.write_text('{"type": "_fixture_meta"}\n{"type": "assistant", "message": {"id": "msg-1", "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "' + str(review_dir / "uncle-bob-pass1.md") + '"}}], "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}\n')

        # Write decoy transcript in decoy session (should NOT be read)
        decoy_transcript = decoy_session / "uncle-bob.jsonl"
        decoy_transcript.write_text('{"type": "_fixture_meta"}\n{"type": "assistant", "message": {"id": "msg-1", "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "' + str(review_dir / "uncle-bob-pass1.md") + '"}}], "usage": {"input_tokens": 9999, "output_tokens": 9999, "cache_read_input_tokens": 9999, "cache_creation_input_tokens": 9999}}}\n')

        # Temporarily set HOME to tmpdir
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir

            result = reviewer_yield.find_subagent_files_by_reviewer(str(review_dir), ["uncle-bob"])

            # Check that it found the correct session's transcript, not the decoy
            passed = (
                "uncle-bob" in result and
                len(result["uncle-bob"]) == 1 and
                result["uncle-bob"][0] == correct_transcript
            )
            harness.test_result("Discovery: reads only named session dir, not decoy siblings", passed)
        finally:
            if old_home:
                os.environ["HOME"] = old_home


# Test 6: Missing transcript-origin.json yields zero tokens, unavailable status, warning
def test_missing_transcript_origin():
    """Missing transcript-origin.json → unavailable tokens_status and warning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        review_dir = Path(tmpdir) / "review"
        review_dir.mkdir()

        # Create a minimal review structure (no transcript-origin.json)
        (review_dir / "final-report.md").write_text("# Report\n")

        # process_review_dir should return unavailable status
        # Capture stderr to verify warning
        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()

        try:
            repo_key, rows, tokens_status = reviewer_yield.process_review_dir(str(review_dir))
            stderr_output = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr

        passed = (
            tokens_status == "unavailable" and
            "Warning" in stderr_output
        )
        harness.test_result("Missing transcript-origin.json: yields unavailable status + warning", passed)


# Test 7: Reviewer name matching (slug and display name)
def test_reviewer_name_matching():
    """count_reviewer_mentions matches both slug and display name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        report_file = Path(tmpdir) / "report.md"

        # Write report mentioning "Uncle Bob" (display name)
        report_file.write_text("""# Final Report

**Reviewer: Uncle Bob** found several issues.
Uncle Bob also noted...
""")

        display_names = {"uncle-bob": "Uncle Bob"}

        # Count mentions by slug should find matches (via display name)
        count = reviewer_yield.count_reviewer_mentions(report_file, "uncle-bob", display_names)

        passed = count >= 2  # At least 2 mentions of "Uncle Bob"
        harness.test_result("Reviewer name matching: finds display name", passed)


# Test 8: Size bucketing
def test_size_bucketing():
    """Size classification from changed-line counts."""
    bucket_config = {
        "config_version": 1,
        "buckets": [
            {"name": "xs", "max_changed_lines": 49},
            {"name": "s", "max_changed_lines": 199},
            {"name": "m", "max_changed_lines": 799},
            {"name": "l", "max_changed_lines": None}
        ]
    }

    test_cases = [
        (25, "xs"),
        (100, "s"),
        (500, "m"),
        (1000, "l"),
    ]

    all_passed = True
    for changed_lines, expected_bucket in test_cases:
        result = reviewer_yield.classify_size_bucket(changed_lines, bucket_config)
        if result != expected_bucket:
            all_passed = False

    harness.test_result("Size bucketing: classifies correctly", all_passed)


# Test 9: Regime classification from timestamp
def test_regime_classification():
    """Regime classification based on timestamp."""
    test_cases = [
        (datetime(2026, 7, 1), "pre-router"),
        (datetime(2026, 7, 11), "pre-router"),  # boundary (before 2026-07-12)
        (datetime(2026, 7, 12), "judgment-router"),  # start of judgment-router regime
        (datetime(2026, 8, 1), "judgment-router"),
        (datetime(2026, 9, 15), "judgment-router"),  # end of judgment-router
        (datetime(2026, 9, 16), "post-148-sam-gated"),  # start of post-148-sam-gated
    ]

    all_passed = True
    for ts, expected_regime in test_cases:
        result = reviewer_yield.classify_regime(ts)
        if result != expected_regime:
            all_passed = False
            # Debug output
            print(f"FAILED: {ts} -> expected {expected_regime}, got {result}", file=sys.stderr)

    harness.test_result("Regime classification: classifies by timestamp", all_passed)


# Test 10: Script executable bit
def test_script_executable():
    """scripts/reviewer-yield.py has executable bit set."""
    script_path = REPO_ROOT / "scripts" / "reviewer-yield.py"
    passed = os.access(script_path, os.X_OK)
    harness.test_result("Script executable: chmod +x verified", passed)


# Test 11: Findings.json parsing
def test_findings_json_parsing():
    """Findings.json reading for verdict classification."""
    with tempfile.TemporaryDirectory() as tmpdir:
        findings_file = Path(tmpdir) / "findings.json"
        findings_data = {
            "schema_version": 1,
            "findings": [
                {"id": "F1", "severity": "Critical", "raised_by": "uncle-bob", "supported_by": [], "verdict": "CONFIRMED"},
                {"id": "F2", "severity": "High", "raised_by": "security-sage", "supported_by": ["uncle-bob"], "verdict": "CONFIRMED"},
                {"id": "F3", "severity": "Medium", "raised_by": "tara-typesafe", "supported_by": [], "verdict": "REJECTED"},
            ]
        }
        findings_file.write_text(json.dumps(findings_data))

        result = reviewer_yield.read_findings_json(Path(tmpdir))

        passed = (
            result is not None and
            len(result.get("findings", [])) == 3 and
            result["findings"][0]["verdict"] == "CONFIRMED"
        )
        harness.test_result("Findings.json: parses and preserves structure", passed)


# Test 12: Report generation with JSON and Markdown consistency
def test_report_json_markdown_consistency():
    """JSON and Markdown outputs are derived from same data structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        review_dir = Path(tmpdir) / ".claude" / "reviews" / "test-repo" / "feature-20260917T120000-00001"
        review_dir.mkdir(parents=True)

        # Create minimal review structure
        (review_dir / "final-report.md").write_text("# Report\n")
        (review_dir / "uncle-bob-pass1.md").write_text("# Uncle Bob\n")

        # Create transcript-origin.json
        origin_data = {
            "schema_version": 1,
            "cwd": "/test",
            "project_dir": "test-project",
            "session_id": "test-session",
            "resolution": "env",
            "recorded_at": "2026-09-17T12:00:00Z"
        }
        (review_dir / "transcript-origin.json").write_text(json.dumps(origin_data))

        (review_dir / "findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [{
                "id": "f1", "severity": "Critical", "raised_by": "uncle-bob",
                "supported_by": [], "verdict": "CONFIRMED",
            }],
        }))

        # Isolate HOME so compute_report_data reads only this fixture
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = tmpdir

            bucket_config = reviewer_yield.load_bucket_config()
            report_data = reviewer_yield.compute_report_data("test-repo", bucket_config)
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                os.environ.pop("HOME", None)

        # Render both formats
        json_output = reviewer_yield.render_report_json(report_data)
        markdown_output = reviewer_yield.render_report_markdown(report_data)

        passed = (
            bool(json_output) and
            bool(markdown_output) and
            "methodology" in json_output and
            "Methodology" in markdown_output and
            report_data.get("solo_findings_per_reviewer") == {"uncle-bob": 1} and
            "uncle-bob: 1 solo findings" in markdown_output and
            json.loads(json_output).get("n_included_runs") == 1
        )
        harness.test_result("Report generation: JSON and Markdown both generated", passed)


# Run all tests
test_token_parsing_multientry()
test_token_parsing_missing_cache_fields()
test_token_parsing_skip_malformed()
test_token_parsing_dedup_by_id()
test_bounded_transcript_discovery()
test_missing_transcript_origin()
test_reviewer_name_matching()
test_size_bucketing()
test_regime_classification()
test_script_executable()
test_findings_json_parsing()
test_report_json_markdown_consistency()

harness.summarize_and_exit()
