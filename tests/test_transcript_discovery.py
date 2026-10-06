#!/usr/bin/env python3
"""
Test suite for scripts/transcript_discovery.py.

Tests:
1. Valid origin resolution (valid transcript-origin.json)
2. Malformed origin file (not JSON, not a dict)
3. Origin validation failures (_SAFE_ID_RE rejection)
4. Path-scan fallback (0/1/N matches)
5. Array-nested field recursion in _contains_review_dir
6. Non-dict JSONL entries skipped, not crashed
7. OSError handling with stderr warning
8. List-nested field detection (fix #25)

Run with: python3 tests/run_all.py or directly with python3 tests/test_transcript_discovery.py
"""

import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

# Load transcript_discovery module
_spec = importlib.util.spec_from_file_location(
    "transcript_discovery",
    REPO_ROOT / "scripts" / "transcript_discovery.py"
)
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


class FakeHome:
    """Context manager that points HOME at a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        self._tmp.cleanup()


def test_valid_origin_resolution():
    """Test successful resolution from valid transcript-origin.json."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-abc123"
        review_dir.mkdir(parents=True)

        origin = {
            "session_id": "sess_12345",
            "project_dir": "my-project",
            "resolution": "env",
            "recorded_at": "2026-10-05T12:00:00+00:00"
        }
        origin_file = review_dir / "transcript-origin.json"
        origin_file.write_text(json.dumps(origin))

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is True
        assert result.get("session_id") == "sess_12345"
        assert result.get("project_dir") == "my-project"
        assert result.get("resolution") == "origin"


def test_malformed_origin_not_json():
    """Test handling of non-JSON origin file."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-xyz"
        review_dir.mkdir(parents=True)

        origin_file = review_dir / "transcript-origin.json"
        origin_file.write_text("{ invalid json }")

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "origin-unavailable"


def test_malformed_origin_not_dict():
    """Test handling of origin file that is not a dict."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-notdict"
        review_dir.mkdir(parents=True)

        origin_file = review_dir / "transcript-origin.json"
        origin_file.write_text(json.dumps(["array", "instead", "of", "dict"]))

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "origin-unavailable"


def test_safe_id_re_rejection_session_id():
    """Test _SAFE_ID_RE rejection of invalid session_id."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-badinv"
        review_dir.mkdir(parents=True)

        origin = {
            "session_id": "sess@12345!",  # Invalid: contains @ and !
            "project_dir": "my-project",
            "resolution": "env",
            "recorded_at": "2026-10-05T12:00:00+00:00"
        }
        origin_file = review_dir / "transcript-origin.json"
        origin_file.write_text(json.dumps(origin))

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "origin-unavailable"
        assert "failed validation" in result.get("reason", "")


def test_safe_id_re_rejection_project_dir():
    """Test _SAFE_ID_RE rejection of invalid project_dir."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-badproj"
        review_dir.mkdir(parents=True)

        origin = {
            "session_id": "sess_12345",
            "project_dir": "my/project",  # Invalid: contains /
            "resolution": "env",
            "recorded_at": "2026-10-05T12:00:00+00:00"
        }
        origin_file = review_dir / "transcript-origin.json"
        origin_file.write_text(json.dumps(origin))

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "origin-unavailable"


def test_path_scan_no_matches():
    """Test path-scan fallback with zero matches."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-nomatch"
        review_dir.mkdir(parents=True)

        # No session files exist
        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "origin-missing"


def test_path_scan_single_match():
    """Test path-scan fallback with exactly one match."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-singlematch"
        review_dir.mkdir(parents=True)

        # Create a project session file with a match
        # Structure: ~/.claude/projects/{project_dir}/{session_id}/subagents/*.jsonl
        projects_root = home / ".claude" / "projects" / "my-repo"
        session_dir = projects_root / "sess_abc123" / "subagents"
        session_dir.mkdir(parents=True)

        # Create a jsonl file with the review_dir name in it
        sessionl_file = session_dir / "sessionl.jsonl"
        entry = {
            "type": "message",
            "content": "review_dir_name=test-review-2026-10-05-singlematch"
        }
        sessionl_file.write_text(json.dumps(entry) + "\n")

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is True
        assert result.get("session_id") == "sess_abc123"
        assert result.get("project_dir") == "my-repo"
        assert result.get("resolution") == "path-scan"


def test_path_scan_ambiguous_multiple_matches():
    """Test path-scan fallback with multiple matches (ambiguous)."""
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-review-2026-10-05-ambig"
        review_dir.mkdir(parents=True)

        # Create multiple project session files with matches
        # Structure: ~/.claude/projects/{project_dir}/{session_id}/subagents/*.jsonl
        projects_root = home / ".claude" / "projects"

        for repo_name, sess_id in [("repo1", "sess_111"), ("repo2", "sess_222")]:
            session_dir = projects_root / repo_name / sess_id / "subagents"
            session_dir.mkdir(parents=True)
            sessionl_file = session_dir / "sessionl.jsonl"
            entry = {"content": "test-review-2026-10-05-ambig"}
            sessionl_file.write_text(json.dumps(entry) + "\n")

        result = td.resolve_session(str(review_dir))
        assert result.get("resolved") is False
        assert result.get("resolution") == "ambiguous-path-scan"


def test_contains_review_dir_string_value():
    """Test _contains_review_dir finds review_dir_name in string values."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        entry = {"path": "/home/user/.claude/reviews/test-review-2026-10-05"}
        jsonl_file.write_text(json.dumps(entry) + "\n")

        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_dict_nested():
    """Test _contains_review_dir finds review_dir_name in nested dict."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        entry = {"data": {"nested_path": "/path/to/test-review-2026-10-05"}}
        jsonl_file.write_text(json.dumps(entry) + "\n")

        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_list_nested():
    """Test _contains_review_dir finds review_dir_name in list values (fix #25)."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        entry = {"paths": ["path1", "path2", "/home/.claude/reviews/test-review-2026-10-05"]}
        jsonl_file.write_text(json.dumps(entry) + "\n")

        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_list_dict_nested():
    """Test _contains_review_dir finds review_dir_name in list of dicts (fix #25)."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        entry = {
            "items": [
                {"name": "item1"},
                {"path": "/home/.claude/reviews/test-review-2026-10-05"}
            ]
        }
        jsonl_file.write_text(json.dumps(entry) + "\n")

        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_non_dict_skipped():
    """Test _contains_review_dir skips non-dict JSONL entries (fix #8)."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        # Write multiple lines: array, string, dict
        lines = [
            json.dumps(["array", "entry"]),
            json.dumps("string entry"),
            json.dumps({"path": "test-review-2026-10-05"})
        ]
        jsonl_file.write_text("\n".join(lines) + "\n")

        # Should find the dict entry, not crash on non-dict entries
        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_malformed_json_skipped():
    """Test _contains_review_dir skips malformed JSON lines (fix #11)."""
    with FakeHome() as home:
        jsonl_file = home / "test.jsonl"
        lines = [
            "{ invalid json",
            json.dumps({"path": "test-review-2026-10-05"})
        ]
        jsonl_file.write_text("\n".join(lines) + "\n")

        # Should find the valid dict entry, skipping the malformed line
        assert td._contains_review_dir(jsonl_file, "test-review-2026-10-05")


def test_contains_review_dir_oserror_logged():
    """Test _contains_review_dir logs OSError to stderr (fix #11)."""
    with FakeHome() as home:
        # Capture stderr
        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            jsonl_file = home / "test.jsonl"
            # Make file unreadable by changing permissions
            jsonl_file.write_text("test")
            os.chmod(str(jsonl_file), 0o000)

            result = td._contains_review_dir(jsonl_file, "test-review")

            stderr_output = sys.stderr.getvalue()
            # Should log a warning, not raise
            assert "Warning: OSError" in stderr_output
            assert result is False
        finally:
            # Restore permissions and stderr
            os.chmod(str(jsonl_file), 0o644)
            sys.stderr = old_stderr


def main():
    h = Harness("TRANSCRIPT_DISCOVERY TEST SUITE")
    t = h.test_result

    print("[Unit Tests] transcript_discovery.py")

    # Test 1: Valid origin
    try:
        test_valid_origin_resolution()
        t("valid origin resolution", True)
    except AssertionError as e:
        t("valid origin resolution", False, str(e))

    # Test 2: Malformed JSON origin
    try:
        test_malformed_origin_not_json()
        t("malformed origin (not JSON)", True)
    except AssertionError as e:
        t("malformed origin (not JSON)", False, str(e))

    # Test 3: Origin not a dict
    try:
        test_malformed_origin_not_dict()
        t("malformed origin (not dict)", True)
    except AssertionError as e:
        t("malformed origin (not dict)", False, str(e))

    # Test 4: SAFE_ID_RE rejection (session_id)
    try:
        test_safe_id_re_rejection_session_id()
        t("SAFE_ID_RE rejection (session_id)", True)
    except AssertionError as e:
        t("SAFE_ID_RE rejection (session_id)", False, str(e))

    # Test 5: SAFE_ID_RE rejection (project_dir)
    try:
        test_safe_id_re_rejection_project_dir()
        t("SAFE_ID_RE rejection (project_dir)", True)
    except AssertionError as e:
        t("SAFE_ID_RE rejection (project_dir)", False, str(e))

    # Test 6: Path-scan no matches
    try:
        test_path_scan_no_matches()
        t("path-scan fallback (0 matches)", True)
    except AssertionError as e:
        t("path-scan fallback (0 matches)", False, str(e))

    # Test 7: Path-scan single match
    try:
        test_path_scan_single_match()
        t("path-scan fallback (1 match)", True)
    except AssertionError as e:
        t("path-scan fallback (1 match)", False, str(e))

    # Test 8: Path-scan ambiguous
    try:
        test_path_scan_ambiguous_multiple_matches()
        t("path-scan fallback (N matches, ambiguous)", True)
    except AssertionError as e:
        t("path-scan fallback (N matches, ambiguous)", False, str(e))

    # Test 9: _contains_review_dir - string value
    try:
        test_contains_review_dir_string_value()
        t("_contains_review_dir (string value)", True)
    except AssertionError as e:
        t("_contains_review_dir (string value)", False, str(e))

    # Test 10: _contains_review_dir - dict nested
    try:
        test_contains_review_dir_dict_nested()
        t("_contains_review_dir (dict nested)", True)
    except AssertionError as e:
        t("_contains_review_dir (dict nested)", False, str(e))

    # Test 11: _contains_review_dir - list nested (fix #25)
    try:
        test_contains_review_dir_list_nested()
        t("_contains_review_dir (list nested, fix #25)", True)
    except AssertionError as e:
        t("_contains_review_dir (list nested, fix #25)", False, str(e))

    # Test 12: _contains_review_dir - list of dicts (fix #25)
    try:
        test_contains_review_dir_list_dict_nested()
        t("_contains_review_dir (list of dicts, fix #25)", True)
    except AssertionError as e:
        t("_contains_review_dir (list of dicts, fix #25)", False, str(e))

    # Test 13: Non-dict entries skipped (fix #8)
    try:
        test_contains_review_dir_non_dict_skipped()
        t("_contains_review_dir (non-dict entries skipped, fix #8)", True)
    except AssertionError as e:
        t("_contains_review_dir (non-dict entries skipped, fix #8)", False, str(e))

    # Test 14: Malformed JSON skipped (fix #11)
    try:
        test_contains_review_dir_malformed_json_skipped()
        t("_contains_review_dir (malformed JSON skipped, fix #11)", True)
    except AssertionError as e:
        t("_contains_review_dir (malformed JSON skipped, fix #11)", False, str(e))

    # Test 15: OSError logged to stderr (fix #11)
    try:
        test_contains_review_dir_oserror_logged()
        t("_contains_review_dir (OSError logged, fix #11)", True)
    except AssertionError as e:
        t("_contains_review_dir (OSError logged, fix #11)", False, str(e))

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
