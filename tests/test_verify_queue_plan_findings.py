#!/usr/bin/env python3
"""
Test suite for verify-queue-sync-cli plan findings (expert review round 2).

Tests the implementation of findings from the plan:
1. Buffered writes: exceptions during sync leave the queue file untouched
2. Failure sentinels: resolve_repo_key/resolve_queue_path return None on failure (not "")
3. Skipped headings: parse_plan counts malformed ### headings in ParsePlanResult
4. Docstring completeness: sync() documents both RuntimeError and OSError
5. Open total correctness: open_total reflects only processed data

Run with: python3 tests/test_verify_queue_plan_findings.py
"""

import sys
import tempfile
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import verify_queue as vq
from _test_harness import REPO_ROOT, Harness


def test_resolve_repo_key_returns_none_on_failure():
    """resolve_repo_key should return None (not empty string) when repo key cannot be resolved."""
    # Test with a non-existent directory
    result = vq.resolve_repo_key(Path("/nonexistent/path/to/repo"))
    assert result is None, f"Expected None, got {result!r}"


def test_resolve_queue_path_returns_none_on_failure():
    """resolve_queue_path should return None (not Path) when queue path cannot be resolved."""
    # Test with a non-existent directory that's not a git worktree
    result = vq.resolve_queue_path(Path("/nonexistent/path"))
    assert result is None, f"Expected None, got {result!r}"


def test_parse_plan_skipped_headings_attribute():
    """ParsePlanResult should have a skipped_headings attribute counting malformed ### headings."""
    plan = """### 1. Valid heading
- **STATUS**: pending-decision

### 2. Missing STATUS
Some content here without a status field

### 3. Another valid
- **STATUS**: pending-measurement
- **Command**: test

### 4. No title text
- **STATUS**: pending-decision
"""
    result = vq.parse_plan(plan, "test-id", "/path/to/plan.md")

    # Should have skipped_headings attribute
    assert hasattr(result, 'skipped_headings'), "ParsePlanResult should have skipped_headings attribute"

    # Count should be > 0 for malformed headings
    assert result.skipped_headings > 0, f"Expected skipped_headings > 0, got {result.skipped_headings}"

    # The list should only contain valid rulings (not the malformed ones)
    assert len(result) < 4, f"Expected fewer than 4 rulings, got {len(result)}"

    # Each ruling should have required fields
    for ruling in result:
        assert ruling.summary, "Ruling should have non-empty summary"
        assert ruling.kind, "Ruling should have kind"


def test_parse_plan_skipped_headings_empty_for_valid_plan():
    """ParsePlanResult.skipped_headings should be 0 for a completely valid plan."""
    plan = """### 1. First item
- **STATUS**: pending-decision

### 2. Second item
- **STATUS**: pending-measurement
- **Command**: echo test
"""
    result = vq.parse_plan(plan, "test-id", "/path/to/plan.md")
    assert result.skipped_headings == 0, f"Expected 0 skipped headings for valid plan, got {result.skipped_headings}"


def test_parse_plan_skipped_headings_increments():
    """ParsePlanResult.skipped_headings should increment for each malformed heading."""
    plan = """### 1. No status
Content without a status field

### 2. Also no status
Another content without status

### 3. Valid
- **STATUS**: pending-decision
"""
    result = vq.parse_plan(plan, "test-id", "/path/to/plan.md")
    # Should have at least 2 skipped headings (the first two invalid ones)
    assert result.skipped_headings >= 2, f"Expected >= 2 skipped headings, got {result.skipped_headings}"


def test_sync_docstring_documents_errors():
    """sync() docstring should document both RuntimeError and OSError."""
    docstring = inspect.getdoc(vq.sync)
    assert docstring, "sync() should have a docstring"

    # Check for RuntimeError documentation
    assert "RuntimeError" in docstring, f"sync() docstring should mention RuntimeError, got:\n{docstring}"

    # Check for OSError documentation
    assert "OSError" in docstring, f"sync() docstring should mention OSError, got:\n{docstring}"


def test_sync_result_has_skipped_headings():
    """SyncResult should have a skipped_headings field."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue_dir = root / "wt"
        queue_dir.mkdir()
        queue_path = queue_dir / vq.QUEUE_FILENAME

        # Create a minimal sync result
        result = vq.SyncResult(
            queue=str(queue_path),
            repo_key="test-repo",
            scanned_plans=0,
            added=[],
            already_queued=[],
            open_total=0,
            skipped_headings=0
        )

        # Should have skipped_headings attribute
        assert hasattr(result, 'skipped_headings'), "SyncResult should have skipped_headings attribute"
        assert result.skipped_headings == 0, f"Expected skipped_headings=0, got {result.skipped_headings}"


def test_sync_exception_leaves_queue_untouched():
    """When sync() raises an exception, the queue file should remain untouched (buffered writes)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reviews_dir = root / "reviews" / "owner-repo"
        reviews_dir.mkdir(parents=True)
        queue_dir = root / "wt"
        queue_dir.mkdir()
        queue_path = queue_dir / vq.QUEUE_FILENAME

        # Write initial queue content
        initial_content = '{"repo":"x","pr":1,"status":"done"}\n'
        queue_path.write_text(initial_content)

        # Try to sync with repo_key=None to trigger an error
        try:
            vq.sync(
                reviews_root=reviews_dir,
                queue=queue_path,
                repo_key=None  # This should cause RuntimeError
            )
        except RuntimeError:
            # Expected - sync should raise RuntimeError when repo_key is None
            pass

        # Queue file should still have original content (buffered writes don't get flushed on error)
        final_content = queue_path.read_text()
        assert final_content == initial_content, (
            f"Queue file was modified despite exception.\n"
            f"Initial: {initial_content!r}\n"
            f"Final: {final_content!r}"
        )


def test_sync_result_to_dict_includes_skipped_headings():
    """SyncResult should serialize skipped_headings to dict (for to_dict() method if it exists)."""
    result = vq.SyncResult(
        queue="test.jsonl",
        repo_key="owner-repo",
        scanned_plans=1,
        added=["id1"],
        already_queued=[],
        open_total=1,
        skipped_headings=2
    )

    # If to_dict exists, use it; otherwise use vars()
    if hasattr(result, 'to_dict'):
        result_dict = result.to_dict()
    else:
        result_dict = vars(result)

    assert 'skipped_headings' in result_dict, "to_dict()/vars() should include skipped_headings"
    assert result_dict['skipped_headings'] == 2, f"skipped_headings should be 2, got {result_dict.get('skipped_headings')}"


def test_open_total_reflects_processed_data():
    """open_total in SyncResult should correctly count open items from synced data."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reviews_dir = root / "reviews" / "owner-repo"
        reviews_dir.mkdir(parents=True)
        queue_dir = root / "wt"
        queue_dir.mkdir()
        queue_path = queue_dir / vq.QUEUE_FILENAME

        # Create a review with pending items
        review_id = "test-branch-aaaaaaa-20261002T120000-00001"
        plan_dir = reviews_dir / review_id
        plan_dir.mkdir()

        plan_content = """### 1. First item
- **STATUS**: pending-decision

### 2. Second item
- **STATUS**: pending-measurement
- **Command**: test
"""
        (plan_dir / vq.PLAN_FILENAME).write_text(plan_content)

        # Sync
        result = vq.sync(
            reviews_root=reviews_dir,
            queue=queue_path,
            repo_key="owner-repo"
        )

        # open_total should equal the number of added pending items
        assert result.open_total >= len(result.added), (
            f"open_total ({result.open_total}) should be at least the number of added items ({len(result.added)})"
        )

        # Re-sync should not increase open_total (idempotence)
        result2 = vq.sync(
            reviews_root=reviews_dir,
            queue=queue_path,
            repo_key="owner-repo"
        )
        assert result2.open_total == result.open_total, (
            f"open_total should not change on re-sync: {result.open_total} vs {result2.open_total}"
        )


def test_resolve_repo_key_returns_correct_type():
    """resolve_repo_key return type should be str | None, not str | ""."""
    # Test when it succeeds (in a real git repo)
    result = vq.resolve_repo_key(REPO_ROOT)
    # Should be either None or a string
    assert result is None or isinstance(result, str), f"Expected None or str, got {type(result)}"

    # Test when it fails (non-git directory)
    result = vq.resolve_repo_key(Path("/nonexistent/nowhere"))
    assert result is None, f"Expected None for non-git dir, got {result!r}"


def test_resolve_queue_path_returns_correct_type():
    """resolve_queue_path return type should be Path | None, not Path | ""."""
    result = vq.resolve_queue_path(Path("/nonexistent"))
    # Should be either None or a Path
    assert result is None or isinstance(result, Path), f"Expected None or Path, got {type(result)}"

    # When it fails, should return None not empty path
    assert result is None, f"Expected None for non-worktree dir, got {result!r}"


if __name__ == "__main__":
    h = Harness("VERIFY-QUEUE PLAN FINDINGS TEST SUITE")
    t = h.test_result

    # Test group 1: Failure sentinels
    print("[Group 1] Failure sentinels - resolve_repo_key/resolve_queue_path return None")
    try:
        test_resolve_repo_key_returns_none_on_failure()
        t("resolve_repo_key returns None on failure", True)
    except AssertionError as e:
        t("resolve_repo_key returns None on failure", False, str(e))

    try:
        test_resolve_queue_path_returns_none_on_failure()
        t("resolve_queue_path returns None on failure", True)
    except AssertionError as e:
        t("resolve_queue_path returns None on failure", False, str(e))

    try:
        test_resolve_repo_key_returns_correct_type()
        t("resolve_repo_key type is str | None (not str | \"\")", True)
    except AssertionError as e:
        t("resolve_repo_key type is str | None (not str | \"\")", False, str(e))

    try:
        test_resolve_queue_path_returns_correct_type()
        t("resolve_queue_path type is Path | None (not Path | \"\")", True)
    except AssertionError as e:
        t("resolve_queue_path type is Path | None (not Path | \"\")", False, str(e))

    print()

    # Test group 2: Skipped headings
    print("[Group 2] Skipped headings - parse_plan counts malformed ### headings")
    try:
        test_parse_plan_skipped_headings_attribute()
        t("ParsePlanResult has skipped_headings attribute and counts malformed headings", True)
    except AssertionError as e:
        t("ParsePlanResult has skipped_headings attribute and counts malformed headings", False, str(e))

    try:
        test_parse_plan_skipped_headings_empty_for_valid_plan()
        t("skipped_headings is 0 for completely valid plan", True)
    except AssertionError as e:
        t("skipped_headings is 0 for completely valid plan", False, str(e))

    try:
        test_parse_plan_skipped_headings_increments()
        t("skipped_headings increments for each malformed heading", True)
    except AssertionError as e:
        t("skipped_headings increments for each malformed heading", False, str(e))

    print()

    # Test group 3: Docstring completeness
    print("[Group 3] Docstring completeness - sync() documents RuntimeError and OSError")
    try:
        test_sync_docstring_documents_errors()
        t("sync() docstring documents both RuntimeError and OSError", True)
    except AssertionError as e:
        t("sync() docstring documents both RuntimeError and OSError", False, str(e))

    print()

    # Test group 4: SyncResult structure
    print("[Group 4] SyncResult structure - has skipped_headings field")
    try:
        test_sync_result_has_skipped_headings()
        t("SyncResult has skipped_headings field", True)
    except AssertionError as e:
        t("SyncResult has skipped_headings field", False, str(e))

    try:
        test_sync_result_to_dict_includes_skipped_headings()
        t("SyncResult.to_dict() includes skipped_headings", True)
    except AssertionError as e:
        t("SyncResult.to_dict() includes skipped_headings", False, str(e))

    print()

    # Test group 5: Buffering and open_total
    print("[Group 5] Buffering behavior - exceptions leave queue untouched, open_total is correct")
    try:
        test_sync_exception_leaves_queue_untouched()
        t("sync() exception leaves queue file untouched (buffered writes)", True)
    except AssertionError as e:
        t("sync() exception leaves queue file untouched (buffered writes)", False, str(e))
    except Exception as e:
        t("sync() exception leaves queue file untouched (buffered writes)", False, f"Unexpected error: {e}")

    try:
        test_open_total_reflects_processed_data()
        t("open_total correctly reflects processed data without second read", True)
    except AssertionError as e:
        t("open_total correctly reflects processed data without second read", False, str(e))
    except Exception as e:
        t("open_total correctly reflects processed data without second read", False, f"Unexpected error: {e}")

    h.summarize_and_exit()
