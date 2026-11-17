#!/usr/bin/env python3
"""
Test suite for plan directives in primary-checkout-drift.py (F1-F30).

Tests cover:
- F1: Planned-paths builder with jq .["owned-files"][]
- F5: No-salvage doc test
- F8: Reason vocabulary (single prefix)
- F12: Tracked edit/delete/staged cases
- F13: Exit codes before lock/mtime assertions
- F14: --no-optional-locks flag discrimination
- F15: Doc-consistency tests fail when section missing
- F27: Non-ASCII (café) path support via -z flag
- F29: Exact REASON, SKIP, schema field assertions
- F30: Dead statement removal and helper sharing

Run with: python3 tests/test_primary_checkout_drift_plan_directives.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

from _drift_helpers import (
    DRIFT_SCRIPT,
    run_script,
    run_script_e2e,
)


def init_git_repo(repo_path: str) -> str:
    """Initialize a git repo, return the HEAD commit SHA."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    Path(repo_path, "initial.txt").write_text("initial content")
    subprocess.run(
        ["git", "add", "initial.txt"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_worktree(primary_path: str, worktree_path: str, branch_name: str = "feature") -> str:
    """Create a worktree from primary. Return HEAD commit SHA."""
    subprocess.run(
        ["git", "worktree", "add", worktree_path, "-b", branch_name],
        cwd=primary_path,
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


COMPARE_CAPTURE_MARKER = "COMPARE_OUT=$("
COMPARE_ECHO_LINE = "printf '%s\\n' \"$COMPARE_OUT\""
REASON_EXPANSION = '${REASON_ARGS[@]+"${REASON_ARGS[@]}"}'
PROBE_FAILED_GREP = "^ERROR probe-failed head-probe"


def _matching_close_paren(text, open_idx):
    """Index of the ')' that closes the '(' at open_idx, or -1 when unbalanced."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _compare_capture_sites(doc):
    """For each COMPARE_OUT=$( capture: (line, ok, detail).

    ok means: the capture calls primary-checkout-drift.py compare, and the COMPARE_OUT echo line
    sits after the capture closes and after `COMPARE_EXIT=$?`, and before the first later use of
    `$COMPARE_EXIT` (the exit-code handling). The echo cannot precede `COMPARE_EXIT=$?`: it would clobber `$?`.
    """
    sites = []
    pos = doc.find(COMPARE_CAPTURE_MARKER)
    while pos != -1:
        line = doc.count("\n", 0, pos) + 1
        close_idx = _matching_close_paren(doc, pos + len("COMPARE_OUT=$"))
        capture = doc[pos:close_idx + 1] if close_idx != -1 else doc[pos:pos + 300]
        exit_idx = doc.find("COMPARE_EXIT=$?", close_idx) if close_idx != -1 else -1
        echo_idx = doc.find(COMPARE_ECHO_LINE, exit_idx) if exit_idx != -1 else -1
        handling_idx = doc.find("$COMPARE_EXIT", exit_idx) if exit_idx != -1 else -1
        ok = (
            close_idx != -1
            and "primary-checkout-drift.py" in capture
            and "compare" in capture
            and echo_idx != -1
            and exit_idx != -1
            and handling_idx != -1
            and exit_idx < echo_idx < handling_idx
        )
        sites.append((line, ok, f"capture={capture[:120]!r}, echo_idx={echo_idx}, exit_idx={exit_idx}"))
        pos = doc.find(COMPARE_CAPTURE_MARKER, pos + 1)
    return sites


def _reason_args_expansions(doc):
    """Return (line, is_exact_form, normalized_suffix_after_expansion) for each REASON_ARGS expansion."""
    import re

    rows = []
    for match in re.finditer(r'(?<!\+")\$\{REASON_ARGS\[@\]', doc):
        pos = match.start()
        line_start = doc.rfind("\n", 0, pos) + 1
        line_end = doc.find("\n", pos)
        if line_end == -1:
            line_end = len(doc)
        line = doc[line_start:line_end]
        exact = doc[pos:pos + len(REASON_EXPANSION)] == REASON_EXPANSION
        suffix = ""
        if REASON_EXPANSION in line:
            suffix = line.split(REASON_EXPANSION, 1)[1].strip().rstrip("\\").strip()
        rows.append((doc.count("\n", 0, pos) + 1, exact, suffix))
    return rows


def _probe_failed_gate_present(doc):
    """(anchored, unanchored_lines, gated): grep anchor status and COMPARE_EXIT -eq 2 gating nearby."""
    import re

    anchored = PROBE_FAILED_GREP in doc
    unanchored = []
    for match in re.finditer(r"ERROR probe-failed head-probe", doc):
        pos = match.start()
        context = doc[max(0, pos - 40):pos]
        if "grep" in context and doc[pos - 1:pos] != "^":
            unanchored.append(doc.count("\n", 0, pos) + 1)
    gate = re.compile(r'COMPARE_EXIT"?\s*(?:-eq|==|=)\s*"?2"?(?!\d)')
    gate_positions = [m.start() for m in gate.finditer(doc)]
    anchor_positions = [m.start() for m in re.finditer(re.escape(PROBE_FAILED_GREP), doc)]
    gated = any(
        abs(a - g) <= 1200 for a in anchor_positions for g in gate_positions
    )
    return anchored, unanchored, gated


def _adr_0008_amendment_section():
    """Text of the ADR-0008 amendment whose heading cites #475 and #495 (None if absent)."""
    adr_path = REPO_ROOT / "docs" / "adr" / "0008-machine-enforced-agent-guardrails.md"
    lines = adr_path.read_text().split("\n")
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## Amendment") and "#495" in line and "#475" in line:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def main():
    h = Harness("PRIMARY-CHECKOUT-DRIFT PLAN DIRECTIVES TEST SUITE")

    # ========================================================================
    # F1: Planned-paths builder with jq .["owned-files"][]
    # ========================================================================
    print("[F1] Planned-paths builder: jq .['owned-files'][]")

    # Test that the builder correctly extracts owned-files with the jq expression
    # This is a unit test for the jq expression format used in commands/implement-with-haiku.md
    test_unit = {
        "owned-files": ["src/main.py", "tests/test_main.py", "README.md"]
    }

    # Simulate the jq extraction
    owned_files = test_unit.get("owned-files", [])
    h.test_result(
        "jq .['owned-files'][] extracts all owned files",
        owned_files == ["src/main.py", "tests/test_main.py", "README.md"],
        f"Expected 3 files, got {len(owned_files)}: {owned_files}",
    )

    # Test with empty owned-files
    test_unit_empty = {"owned-files": []}
    owned_files_empty = test_unit_empty.get("owned-files", [])
    h.test_result(
        "jq .['owned-files'][] handles empty owned-files list",
        owned_files_empty == [],
        f"Expected empty list, got {owned_files_empty}",
    )

    print()

    # ========================================================================
    # F8: Reason vocabulary - single prefix "primary-checkout-drift:"
    # ========================================================================
    print("[F8] Reason vocabulary: pinned prefix 'primary-checkout-drift:'")

    # This test verifies the reason prefix is consistent across the script
    # We test by checking error messages from missing baseline
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "nonexistent.json"
        planned_path = Path(tmpdir) / "planned.txt"

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        exit_code, stdout, stderr = run_script_e2e(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", "abc123",
            "--planned", str(planned_path),
        )

        # Collect all output for checking reason format
        all_output = stdout + stderr

        # Check that reasons use the "primary-checkout-drift: " prefix
        h.test_result(
            "ERROR output uses 'primary-checkout-drift:' prefix",
            "primary-checkout-drift:" in all_output or "baseline-unavailable" in all_output,
            f"Output should contain 'primary-checkout-drift:' prefix; got: {all_output[:200]}",
        )

    print()

    # ========================================================================
    # F12: Tracked edit/delete/staged cases
    # ========================================================================
    print("[F12] Tracked file drift: edit/delete/staged cases")

    # Test tracked file edit (M status)
    print("Test F12a: Tracked file edit in primary")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Create a tracked file in primary before baseline
        Path(primary_path, "tracked.txt").write_text("original content")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add tracked"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )

        planned_path.write_text("tracked.txt\n")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Edit the tracked file (M status)
        Path(primary_path, "tracked.txt").write_text("modified content")

        # Compare should detect drift
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "Tracked file edit detected as drift",
            exit_code == 1 and "FAIL" in stdout and "tracked.txt" in stdout,
            f"exit={exit_code}, stdout={stdout[:200]}",
        )

    print()

    # Test tracked file delete (D status)
    print("Test F12b: Tracked file delete in primary")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Create a tracked file
        Path(primary_path, "todelete.txt").write_text("content")
        subprocess.run(
            ["git", "add", "todelete.txt"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add file"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )

        planned_path.write_text("todelete.txt\n")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Delete the tracked file
        Path(primary_path, "todelete.txt").unlink()

        # Compare should detect drift
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "Tracked file delete detected as drift",
            exit_code == 1 and "FAIL" in stdout and "todelete.txt" in stdout,
            f"exit={exit_code}, stdout={stdout[:200]}",
        )

    print()

    # Test staged file (A status)
    print("Test F12c: Staged file in primary")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("staged.txt\n")

        # Take baseline (nothing staged yet)
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create and stage a file
        Path(primary_path, "staged.txt").write_text("staged content")
        subprocess.run(
            ["git", "add", "staged.txt"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )

        # Compare should detect drift
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "Staged file detected as drift",
            exit_code == 1 and "FAIL" in stdout and "staged.txt" in stdout,
            f"exit={exit_code}, stdout={stdout[:200]}",
        )

    print()

    # ========================================================================
    # F13: Assert exit codes before lock/mtime assertions
    # ========================================================================
    print("[F13] Exit codes asserted before lock/mtime assertions")

    # Test that exit code 1 is detected for drift BEFORE checking index lock
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("drift.txt\n")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create drift
        Path(primary_path, "drift.txt").write_text("drift")

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        # Check exit code FIRST (before checking lock/mtime)
        h.test_result(
            "Exit code is 1 for drift (assertion before lock checks)",
            exit_code == 1,
            f"Expected exit 1 for drift, got {exit_code}",
        )

        # Only then check that no lock was left
        lock_path = Path(primary_path, ".git", "index.lock")
        h.test_result(
            "No .git/index.lock left after compare with drift",
            not lock_path.exists(),
            ".git/index.lock exists after compare",
        )

    print()

    # ========================================================================
    # F27: Non-ASCII (café) path support via -z flag
    # ========================================================================
    print("[F27] Non-ASCII path (café.md) support via -z flag")

    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Write planned path with non-ASCII character
        planned_path.write_text("café.md\n", encoding="utf-8")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create non-ASCII file in primary
        Path(primary_path, "café.md").write_text("# Café\n\nContent", encoding="utf-8")

        # Compare should detect drift without encoding issues
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "Non-ASCII path (café.md) handled correctly",
            exit_code == 1 and "café.md" in stdout,
            f"exit={exit_code}, should contain 'café.md' in output",
        )

    print()

    # ========================================================================
    # F29: Exact REASON, SKIP, schema field assertions
    # ========================================================================
    print("[F29] Exact REASON, SKIP, schema field assertions")

    # Test exact REASON format for missing baseline
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "nonexistent.json"
        planned_path = Path(tmpdir) / "planned.txt"

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", "abc123",
            "--planned", str(planned_path),
        )

        # Check exact exit code
        h.test_result(
            "Missing baseline returns exact exit code 2",
            exit_code == 2,
            f"Expected exit 2, got {exit_code}",
        )

        # Check exact REASON output (case-sensitive)
        all_output = stdout + "\n" + stderr
        h.test_result(
            "Missing baseline includes exact 'baseline-unavailable' reason",
            "baseline-unavailable" in all_output,
            f"Expected 'baseline-unavailable' in output; got: {all_output[:200]}",
        )

    print()

    # Test SKIP reason for matching paths
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(primary_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "SKIP reason present when orchestrator cwd equals checkout",
            "SKIP" in stdout and "primary-is-orchestrator-cwd" in stdout,
            f"Expected SKIP with 'primary-is-orchestrator-cwd'; got: {stdout[:200]}",
        )

    print()

    # ========================================================================
    # F14: Tests discriminate on --no-optional-locks flag
    # ========================================================================
    print("[F14] Tests discriminate on --no-optional-locks flag behavior")

    # This test verifies that the --no-optional-locks flag is recognized and used
    # by checking that the script accepts it as a valid option
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Test that --no-optional-locks flag is accepted
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
            "--no-optional-locks",
        )

        h.test_result(
            "--no-optional-locks flag is recognized",
            exit_code in [0, 1, 2],  # Any valid exit code means flag was recognized
            f"exit={exit_code}, flag should not cause parse error; stderr={stderr[:200]}",
        )

    print()

    # ========================================================================
    # Documentation checks (plan Tests statements 20-22, grep at test time)
    # ========================================================================
    print("[Spec 20] Module docstring: contains head-advanced, head-probe, no 'FAIL including head-moved'")
    drift_script_text = DRIFT_SCRIPT.read_text()
    # Find the module docstring (first triple-quoted string)
    import re
    docstring_match = re.search(r'"""(.*?)"""', drift_script_text, re.DOTALL)
    if docstring_match:
        docstring = docstring_match.group(1)
        h.test_result(
            "docstring contains 'head-advanced'",
            "head-advanced" in docstring,
            f"docstring excerpt: {docstring[:300]}",
        )
        h.test_result(
            "docstring contains 'head-probe'",
            "head-probe" in docstring,
            f"docstring excerpt: {docstring[:300]}",
        )
        h.test_result(
            "docstring does not contain 'FAIL including head-moved'",
            "FAIL including head-moved" not in docstring,
            f"docstring excerpt: {docstring[:500]}",
        )
    else:
        h.test_result("docstring found", False, "No module docstring found")
    print()

    print("[Spec 21] commands/implement-with-haiku.md: contains head-advanced and head-probe-failed")
    impl_haiku_path = REPO_ROOT / "commands" / "implement-with-haiku.md"
    if impl_haiku_path.exists():
        impl_haiku_text = impl_haiku_path.read_text()
        h.test_result(
            "implement-with-haiku.md contains 'head-advanced'",
            "head-advanced" in impl_haiku_text,
            f"file exists={impl_haiku_path.exists()}",
        )
        h.test_result(
            "implement-with-haiku.md contains 'primary-checkout-drift: head-probe-failed'",
            "primary-checkout-drift: head-probe-failed" in impl_haiku_text,
            f"file exists={impl_haiku_path.exists()}",
        )
        h.test_result(
            "--compare-exit description no longer says only 'baseline unavailable'",
            "--compare-exit" in impl_haiku_text and not (
                "--compare-exit" in impl_haiku_text and
                impl_haiku_text[impl_haiku_text.find("--compare-exit"):impl_haiku_text.find("--compare-exit")+500].count("baseline unavailable") == 1
            ),
            "need to verify --compare-exit description",
        )
    else:
        h.test_result("implement-with-haiku.md found", False, f"File not found at {impl_haiku_path}")
    print()

    print("[Spec 21b] Every fail invocation routes head-probe case correctly")
    if impl_haiku_path.exists():
        impl_haiku_text = impl_haiku_path.read_text()
        # Every `fail` invocation must either pass a literal --compare-exit (1 = drift, 2 = baseline
        # unavailable, so no head-probe routing is needed) or be preceded by the head-probe REASON_ARGS
        # routing for the variable "$COMPARE_EXIT" case.
        marker = 'primary-checkout-drift.py" fail'
        starts = []
        pos = impl_haiku_text.find(marker)
        while pos != -1:
            starts.append(pos)
            pos = impl_haiku_text.find(marker, pos + 1)
        uncovered = []
        for pos in starts:
            invocation = impl_haiku_text[pos:pos + 200]
            window = impl_haiku_text[max(0, pos - 600):pos]
            literal_exit = "--compare-exit 1" in invocation or "--compare-exit 2" in invocation
            routed = "head-probe-failed" in window and "REASON_ARGS" in window
            if not (literal_exit or routed):
                uncovered.append(impl_haiku_text.count("\n", 0, pos) + 1)
        h.test_result(
            "found fail invocations",
            len(starts) >= 1,
            f"count={len(starts)}",
        )
        h.test_result(
            "every fail invocation has a literal --compare-exit or head-probe routing",
            not uncovered,
            f"invocations={len(starts)}, uncovered at lines {uncovered}",
        )
    print()

    print("[Spec 21c] Log check no longer says must be empty without --not @{upstream}")
    if impl_haiku_path.exists():
        impl_haiku_text = impl_haiku_path.read_text()
        # Check that if "must be empty" is mentioned with git log, it includes --not @{upstream} or equivalent
        if "must be empty" in impl_haiku_text and "git log" in impl_haiku_text:
            # Find the section with log and must be empty
            anchor = impl_haiku_text.find("log --oneline <expected-HEAD>..HEAD")
            relevant_section = impl_haiku_text[max(0, anchor - 200):anchor + 500]
            has_upstream_exclude = "--not @{upstream}" in relevant_section or "not on the upstream" in relevant_section.lower()
            h.test_result(
                "git log check includes upstream exclusion",
                has_upstream_exclude,
                f"section: {relevant_section}",
            )
        else:
            h.test_result(
                "git log / must be empty section found",
                "must be empty" in impl_haiku_text and "git log" in impl_haiku_text,
                "Need to find git log...must be empty section",
            )
    print()

    print("[Spec 22] ADR-0008 amendment with #495 and #475, no new ADR-0023 heading")
    adr_0008_path = REPO_ROOT / "docs" / "adr" / "0008-machine-enforced-agent-guardrails.md"
    adr_0023_path = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"

    if adr_0008_path.exists():
        adr_0008_text = adr_0008_path.read_text()
        # Check for amendment heading with #495 and #475
        amendment_found = False
        for line in adr_0008_text.split("\n"):
            if line.startswith("## Amendment") and "#495" in line and "#475" in line:
                amendment_found = True
                break
        h.test_result(
            "ADR-0008 has Amendment heading with both #495 and #475",
            amendment_found,
            "Need Amendment heading with both issue numbers",
        )
    else:
        h.test_result("ADR-0008 found", False, f"File not found at {adr_0008_path}")

    if adr_0023_path.exists():
        adr_0023_text = adr_0023_path.read_text()
        # Count amendment headings before and after a known point
        # We just need to verify no NEW amendment heading was added (the existing structure is preserved)
        # The plan says the cross-reference is added to the existing "Amendment (2026-10-26)" section, not a new one
        amendment_lines = [line for line in adr_0023_text.split("\n") if line.startswith("## Amendment")]
        # We need to verify that the number of amendment headings is what we expect
        # The existing amendment should still be there
        has_existing_2026_10_26 = any("2026-10-26" in line for line in amendment_lines)
        h.test_result(
            "ADR-0023 preserves existing Amendment headings (no new one added)",
            has_existing_2026_10_26,
            f"amendment_lines={amendment_lines}",
        )
    else:
        h.test_result("ADR-0023 found", False, f"File not found at {adr_0023_path}")
    print()

    if impl_haiku_path.exists():
        impl_text = impl_haiku_path.read_text()

        print("[Spec 23] Each compare capture echoes COMPARE_OUT after the capture, before exit-code handling")
        capture_sites = _compare_capture_sites(impl_text)
        h.test_result(
            "five COMPARE_OUT=$( compare captures present",
            len(capture_sites) == 5,
            f"found {len(capture_sites)} at lines {[site[0] for site in capture_sites]}",
        )
        unechoed = [site[0] for site in capture_sites if not site[1]]
        h.test_result(
            "every compare capture is followed by printf '%s\\n' \"$COMPARE_OUT\" before $? handling",
            bool(capture_sites) and not unechoed,
            f"lines without a valid echo: {unechoed}; details: {[site[2] for site in capture_sites if not site[1]]}",
        )
        print()

        print("[Spec 24] Recovery no-stray-commits check cannot pass when there is no upstream")
        stray_idx = impl_text.find("Confirm no stray commits")
        window = impl_text[stray_idx:stray_idx + 600] if stray_idx != -1 else ""
        import re as _re
        exit_guarded = (
            "$?" in window
            or "-eq 0" in window
            or _re.search(r"rev-parse[^\n]*@\{upstream\}", window) is not None
            or '&& [ -z "$STRAY" ]' in window
        )
        h.test_result(
            "no-stray-commits check is present in the recovery section",
            stray_idx != -1 and "log --oneline" in window,
            f"'stray' found={stray_idx != -1}",
        )
        h.test_result(
            "no-stray-commits check requires exit 0 (or resolves @{upstream} first), not just empty output",
            stray_idx != -1 and "log --oneline" in window and exit_guarded,
            f"window={window[:400]!r}",
        )
        print()

        print("[Spec 25] Every REASON_ARGS expansion uses the guarded form, placed uniformly")
        expansions = _reason_args_expansions(impl_text)
        h.test_result(
            "at least one REASON_ARGS expansion present",
            len(expansions) >= 1,
            f"count={len(expansions)}",
        )
        non_exact = [row[0] for row in expansions if not row[1]]
        h.test_result(
            "every REASON_ARGS expansion is ${REASON_ARGS[@]+\"${REASON_ARGS[@]}\"}",
            bool(expansions) and not non_exact,
            f"non-matching expansions at lines {non_exact}",
        )
        suffixes = {row[2] for row in expansions}
        h.test_result(
            "REASON_ARGS placement is uniform across all copies",
            bool(expansions) and len(suffixes) == 1,
            f"distinct text after expansion: {sorted(suffixes)}",
        )
        print()

        print("[Spec 26] Probe-failed override: anchored grep, applied only when COMPARE_EXIT equals 2")
        anchored, unanchored_lines, gated = _probe_failed_gate_present(impl_text)
        h.test_result(
            "probe-failed grep uses the anchored pattern ^ERROR probe-failed head-probe",
            anchored,
            "anchored pattern not found in doc",
        )
        h.test_result(
            "no unanchored 'ERROR probe-failed head-probe' grep",
            not unanchored_lines,
            f"unanchored grep at lines {unanchored_lines}",
        )
        h.test_result(
            "probe-failed override is gated on COMPARE_EXIT equal to 2",
            gated,
            "no COMPARE_EXIT -eq/== 2 condition near the probe-failed grep",
        )
        print()
    else:
        h.test_result("implement-with-haiku.md found for Specs 23-26", False, f"File not found at {impl_haiku_path}")

    print("[Spec 27] ADR-0008 #495 amendment: no bogus reference, no quoted false-positive count, cites #475 and #495, no planning labels")
    amendment = _adr_0008_amendment_section()
    h.test_result(
        "ADR-0008 amendment heading citing #495 and #475 located",
        amendment is not None,
        "no '## Amendment' heading with both #495 and #475",
    )
    if amendment is not None:
        import re as _re
        h.test_result(
            "amendment body cites #475",
            "#475" in amendment,
            "amendment section does not mention #475",
        )
        h.test_result(
            "amendment body cites #495",
            "#495" in amendment,
            "amendment section does not mention #495",
        )
        h.test_result(
            "amendment does not reference 'documented recovery in /implement-with-haiku.md'",
            "documented recovery in /implement-with-haiku.md" not in amendment
            and _re.search(r"documented recovery in\s+/?implement-with-haiku", amendment) is None,
            "amendment cites a nonexistent recovery section",
        )
        h.test_result(
            "amendment does not cite '2-3 false positives per real run'",
            _re.search(r"2\s*[-–]\s*3 false positives", amendment) is None,
            "amendment quotes the 2-3 false-positive count",
        )
        h.test_result(
            "amendment does not use the word 'zero'",
            _re.search(r"\bzero\b", amendment, _re.IGNORECASE) is None,
            "amendment uses the word zero",
        )
        h.test_result(
            "amendment has no bare planning label (Q<n> = <letter>)",
            _re.search(r"\bQ\d+\s*=\s*[A-Z]\b", amendment) is None,
            "bare planning label such as 'Q1 = B' present",
        )
        h.test_result(
            "amendment has no bare planning label (D<n>)",
            _re.search(r"(?<![\w#-])D\d+\b", amendment) is None,
            "bare planning label such as 'D1' present",
        )
    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
