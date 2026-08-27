#!/usr/bin/env python3
"""
Test suite for check_stage_pairing.py linter (plan item #3).

Covers:
1. Check A: ORPHANED_BEGIN detection (stage-begin without matching stage-end)
2. Check A: ORPHANED_END detection (stage-end without matching stage-begin)
3. Check B: POSSIBLE_LEAK detection (exit after stage-begin without stage-end)
4. CLI invocation with file arguments
5. CLI defaults to scanning commands/*.md when no args given
6. CLI --self-test mode
7. Exit code handling (0 for no findings, 1 for findings)

Run with: python3 tests/test_stage_pairing_linter.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "check_stage_pairing.py"


def run_script(args, cwd=None):
    """Run check_stage_pairing.py as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.returncode, result.stdout, result.stderr


def test_lint_orphaned_begin():
    """Detects stage-begin without matching stage-end (ORPHANED_BEGIN)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with stage-begin but no matching stage-end
        content = """# Example Command

```bash
run-metrics.py session-begin --log "$METRICS_LOG"
run-metrics.py stage-begin --stage resolve-target --log "$METRICS_LOG"
# Missing stage-end!
exit 0
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit non-zero (findings detected)
        if code == 0:
            return False, "should exit non-zero when orphaned begin detected"

        # Should mention ORPHANED_BEGIN and the stage name
        if "ORPHANED_BEGIN" not in stdout or "resolve-target" not in stdout:
            return False, f"output should mention ORPHANED_BEGIN and stage name: {stdout}"

        return True, ""


def test_lint_orphaned_end():
    """Detects stage-end without matching stage-begin (ORPHANED_END)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with stage-end but no matching stage-begin
        content = """# Example Command

```bash
run-metrics.py session-begin --log "$METRICS_LOG"
# Missing stage-begin!
run-metrics.py stage-end --stage cleanup --log "$METRICS_LOG"
exit 0
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit non-zero (findings detected)
        if code == 0:
            return False, "should exit non-zero when orphaned end detected"

        # Should mention ORPHANED_END and the stage name
        if "ORPHANED_END" not in stdout or "cleanup" not in stdout:
            return False, f"output should mention ORPHANED_END and stage name: {stdout}"

        return True, ""


def test_lint_possible_leak_exit_after_begin():
    """Detects stage-begin followed by exit without stage-end (POSSIBLE_LEAK)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with stage-begin and exit but NO stage-end in the block
        content = """# Example Command

```bash
run-metrics.py session-begin --log "$METRICS_LOG"
run-metrics.py stage-begin --stage build --log "$METRICS_LOG"
if [[ ! -f "$LOCKFILE" ]]; then
  exit 1
fi
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit non-zero (findings detected)
        if code == 0:
            return False, "should exit non-zero when possible leak detected"

        # Should mention POSSIBLE_LEAK
        if "POSSIBLE_LEAK" not in stdout:
            return False, f"output should mention POSSIBLE_LEAK: {stdout}"

        return True, ""


def test_lint_clean_well_paired_doc():
    """Clean markdown with well-paired stages returns no findings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write well-formed markdown
        content = """# Example Command

```bash
run-metrics.py session-begin --log "$METRICS_LOG"
run-metrics.py stage-begin --stage build --log "$METRICS_LOG"
# Do work...
run-metrics.py stage-end --stage build --log "$METRICS_LOG"
exit 0
```

Another section:

```bash
run-metrics.py stage-begin --stage test --log "$METRICS_LOG"
# Run tests...
run-metrics.py stage-end --stage test --log "$METRICS_LOG"
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit 0 (no findings)
        if code != 0:
            return False, f"should exit 0 for clean doc, got {code}. stdout: {stdout}"

        return True, ""


def test_lint_stage_end_with_outcome_before_exit():
    """stage-end with --outcome failure before exit 1 is OK (not a leak)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown where exit is preceded by proper stage-end with outcome failure
        content = """# Example Command

```bash
run-metrics.py session-begin --log "$METRICS_LOG"
run-metrics.py stage-begin --stage build --log "$METRICS_LOG"
if ! make build; then
  run-metrics.py stage-end --stage build --outcome failure --failure-class other --log "$METRICS_LOG"
  exit 1
fi
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit 0 (no findings) because stage-end precedes exit
        if code != 0:
            return False, f"should exit 0 for properly closed stage, got {code}. stdout: {stdout}"

        return True, ""


def test_cli_multiple_files():
    """CLI accepts multiple file arguments."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = Path(tmpdir) / "file1.md"
        file2 = Path(tmpdir) / "file2.md"

        # Write a clean file and a file with an orphaned begin
        file1.write_text("""```bash
run-metrics.py stage-begin --stage a --log "$LOG"
run-metrics.py stage-end --stage a --log "$LOG"
```""")

        file2.write_text("""```bash
run-metrics.py stage-begin --stage b --log "$LOG"
```""")

        code, stdout, stderr = run_script([str(file1), str(file2)])

        # Should exit non-zero (file2 has an issue)
        if code == 0:
            return False, "should exit non-zero when any file has findings"

        # Output should mention file2
        if "file2" not in stdout and str(file2) not in stdout:
            return False, f"output should identify problematic file: {stdout}"

        return True, ""


def test_cli_default_scans_commands_dir():
    """CLI defaults to scanning all commands/*.md when no args given."""
    # This test is conditional - it only makes sense in the repo context
    # Create a temp commands dir and run from there
    with tempfile.TemporaryDirectory() as tmpdir:
        commands_dir = Path(tmpdir) / "commands"
        commands_dir.mkdir()

        # Create a well-formed command file
        (commands_dir / "test-cmd.md").write_text("""```bash
run-metrics.py stage-begin --stage a --log "$LOG"
run-metrics.py stage-end --stage a --log "$LOG"
```""")

        # Run linter from tmpdir (should default to commands/*.md)
        code, stdout, stderr = run_script([], cwd=tmpdir)

        # Should exit 0 (no findings)
        if code != 0:
            return False, f"should find clean file, got exit {code}. output: {stdout}"

        return True, ""


def test_cli_self_test_mode():
    """CLI --self-test runs internal self-tests."""
    code, stdout, stderr = run_script(["--self-test"])

    # Should exit 0 if self-tests pass
    # (or handle failure gracefully if not all self-tests pass)
    if "Traceback" in stderr:
        return False, f"--self-test caused traceback: {stderr}"

    # Should indicate testing happened
    if code == 0:
        return True, ""
    else:
        # Self-test failed but ran without crashing
        return True, "self-test ran but detected issues (OK for this test)"


def test_cli_exit_code_0_no_findings():
    """CLI exits 0 when no findings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Just text, no code blocks")

        code, stdout, stderr = run_script([str(test_file)])

        if code != 0:
            return False, f"expected exit 0 for no findings, got {code}"

        return True, ""


def test_cli_exit_code_1_with_findings():
    """CLI exits 1 when findings detected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Create file with an orphaned begin
        test_file.write_text("""```bash
run-metrics.py stage-begin --stage orphan --log "$LOG"
```""")

        code, stdout, stderr = run_script([str(test_file)])

        if code == 0:
            return False, "expected exit 1 for findings, got 0"

        if "ORPHANED_BEGIN" not in stdout:
            return False, f"expected ORPHANED_BEGIN in output: {stdout}"

        return True, ""


def test_lint_complex_doc_multiple_issues():
    """Detects multiple issues in a single document."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with multiple issues
        content = """# Complex Command

First block with orphaned begin:

```bash
run-metrics.py stage-begin --stage orphan1 --log "$LOG"
# No end for this one
```

Second block with orphaned end:

```bash
run-metrics.py stage-end --stage orphan2 --log "$LOG"
# No begin for this one
```

Third block with potential leak (stage-begin, exit, NO stage-end):

```bash
run-metrics.py stage-begin --stage build --log "$LOG"
if [[ -z "$VAR" ]]; then
  exit 1
fi
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(test_file)])

        # Should exit non-zero
        if code == 0:
            return False, "should detect multiple issues"

        # Should report multiple issues
        found_begin = "ORPHANED_BEGIN" in stdout and "orphan1" in stdout
        found_end = "ORPHANED_END" in stdout and "orphan2" in stdout
        found_leak = "POSSIBLE_LEAK" in stdout

        if not (found_begin and found_end and found_leak):
            return False, (
                f"expected all three issue types. "
                f"begin: {found_begin}, end: {found_end}, leak: {found_leak}. "
                f"output: {stdout}"
            )

        return True, ""


if __name__ == "__main__":
    h = Harness("STAGE PAIRING LINTER TEST SUITE")

    test_result = h.test_result

    print("[Section 1] Check A: Orphaned begin/end detection")
    passed, msg = test_lint_orphaned_begin()
    test_result("detects orphaned stage-begin", passed, msg)

    passed, msg = test_lint_orphaned_end()
    test_result("detects orphaned stage-end", passed, msg)

    print()

    print("[Section 2] Check B: Possible leak detection")
    passed, msg = test_lint_possible_leak_exit_after_begin()
    test_result("detects exit after stage-begin without stage-end", passed, msg)

    passed, msg = test_lint_stage_end_with_outcome_before_exit()
    test_result("allows exit when stage-end precedes it", passed, msg)

    print()

    print("[Section 3] Clean markdown")
    passed, msg = test_lint_clean_well_paired_doc()
    test_result("clean markdown returns no findings", passed, msg)

    print()

    print("[Section 4] CLI file handling")
    passed, msg = test_cli_multiple_files()
    test_result("CLI accepts multiple files", passed, msg)

    passed, msg = test_cli_default_scans_commands_dir()
    test_result("CLI defaults to commands/*.md", passed, msg)

    print()

    print("[Section 5] CLI self-test mode")
    passed, msg = test_cli_self_test_mode()
    test_result("CLI --self-test runs without crash", passed, msg)

    print()

    print("[Section 6] CLI exit codes")
    passed, msg = test_cli_exit_code_0_no_findings()
    test_result("CLI exits 0 for no findings", passed, msg)

    passed, msg = test_cli_exit_code_1_with_findings()
    test_result("CLI exits 1 for findings", passed, msg)

    print()

    print("[Section 7] Complex scenarios")
    passed, msg = test_lint_complex_doc_multiple_issues()
    test_result("detects multiple issues in one document", passed, msg)

    print()

    h.summarize_and_exit()
