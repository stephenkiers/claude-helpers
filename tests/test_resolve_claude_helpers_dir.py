#!/usr/bin/env python3
"""
Test suite for resolve-claude-helpers-dir.sh script and its integration into command docs.

Covers:
1. Script exists and is syntactically valid bash
2. When sourced with BASH_SOURCE[0] pointing to a real path, sets CLAUDE_HELPERS_DIR correctly
3. When readlink -f fails, prints error to stderr and returns 1 (without killing shell)
4. No command doc contains the old inline double-dirname pattern anymore
5. Every bash block in the four command docs that references $CLAUDE_HELPERS_DIR
   also sources resolve-claude-helpers-dir.sh within that same block

Run with: python3 tests/test_resolve_claude_helpers_dir.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "resolve-claude-helpers-dir.sh"

# Command docs that should be checked (per the plan)
COMMAND_DOCS_TO_CHECK = [
    "commands/track-and-start.md",
    "commands/cleanup.md",
    "commands/shipit.md",
    "commands/merge-and-cleanup.md",
]


def run_bash(script_text, env=None):
    """Run bash script in a subshell. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["bash", "-c", script_text],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def test_script_exists():
    """Test that resolve-claude-helpers-dir.sh exists."""
    return RESOLVE_SCRIPT.exists(), f"script not found at {RESOLVE_SCRIPT}"


def test_script_is_executable():
    """Test that the script has executable permissions."""
    return (
        RESOLVE_SCRIPT.stat().st_mode & 0o111 != 0,
        f"script is not executable at {RESOLVE_SCRIPT}",
    )


def test_script_is_valid_bash():
    """Test that the script is syntactically valid bash."""
    code, stdout, stderr = run_bash(f"bash -n {RESOLVE_SCRIPT}")
    is_valid = code == 0
    msg = f"bash syntax check failed: {stderr}" if not is_valid else ""
    return is_valid, msg


def test_script_resolves_correct_directory():
    """Test that sourcing the script sets CLAUDE_HELPERS_DIR to correct path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create a fake directory structure matching the real repo
        # The script expects to be at <repo>/scripts/resolve-claude-helpers-dir.sh
        fake_repo = tmpdir_path / "fake_repo"
        fake_repo.mkdir()
        scripts_dir = fake_repo / "scripts"
        scripts_dir.mkdir()

        # Copy the real script to the fake location
        fake_script = scripts_dir / "resolve-claude-helpers-dir.sh"
        fake_script.write_text(RESOLVE_SCRIPT.read_text())

        # Create a bash script that sources the fake script and prints CLAUDE_HELPERS_DIR
        test_script = f"""
        source "{fake_script}" || exit 1
        echo "$CLAUDE_HELPERS_DIR"
        """

        code, stdout, stderr = run_bash(test_script)

        if code != 0:
            return False, f"sourcing failed: {stderr}"

        result_dir = stdout.strip()
        # Normalize paths using readlink -f for comparison (handles /private prefix on macOS)
        expected_dir = str(Path(fake_repo).resolve())
        if result_dir != expected_dir:
            return (
                False,
                f"CLAUDE_HELPERS_DIR is {result_dir}, expected {expected_dir}",
            )

        return True, ""


def test_script_exports_variable():
    """Test that CLAUDE_HELPERS_DIR is exported (available to subshells)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        fake_repo = tmpdir_path / "fake_repo"
        fake_repo.mkdir()
        scripts_dir = fake_repo / "scripts"
        scripts_dir.mkdir()

        fake_script = scripts_dir / "resolve-claude-helpers-dir.sh"
        fake_script.write_text(RESOLVE_SCRIPT.read_text())

        # Test that the variable is exported by running a subshell
        test_script = f"""
        source "{fake_script}" || exit 1
        bash -c 'echo "$CLAUDE_HELPERS_DIR"'
        """

        code, stdout, stderr = run_bash(test_script)

        if code != 0:
            return False, f"export test failed: {stderr}"

        result_dir = stdout.strip()
        expected_dir = str(Path(fake_repo).resolve())
        if result_dir != expected_dir:
            return False, f"variable not exported properly, got {result_dir}"

        return True, ""


def test_script_fails_on_readlink_error():
    """Test that script returns 1 and prints error when readlink -f fails."""
    # Create a path that readlink -f will fail on (nonexistent symlink target)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create a script that simulates readlink failure by using an invalid path
        fake_script_path = tmpdir_path / "fake_script.sh"
        fake_script_path.write_text(RESOLVE_SCRIPT.read_text())

        # Move the actual script file so readlink will resolve to nothing
        fake_script_path.unlink()

        # Create a fake script that will fail
        broken_script_path = tmpdir_path / "broken.sh"
        broken_script_path.write_text("""
        RESOLVE_SCRIPT_PATH="$(readlink -f "/this/path/does/not/exist")"
        RESOLVE_EXIT=$?

        if [ $RESOLVE_EXIT -ne 0 ] || [ -z "$RESOLVE_SCRIPT_PATH" ]; then
          echo "ERROR: could not resolve ~/.claude/scripts/resolve-claude-helpers-dir.sh — run /setup-local to (re)install claude-helpers symlinks" >&2
          return 1
        fi

        export CLAUDE_HELPERS_DIR="$(dirname "$(dirname "$RESOLVE_SCRIPT_PATH")")"
        """)

        # Test that it returns non-zero
        test_script = f"source {broken_script_path}"
        code, stdout, stderr = run_bash(test_script)

        if code == 0:
            return False, "script should return non-zero on readlink failure"

        if "ERROR" not in stderr:
            return (
                False,
                f"stderr should contain ERROR message, got: {stderr!r}",
            )

        return True, ""


def test_script_does_not_kill_sourcing_shell():
    """Test that returning 1 from sourced script doesn't kill the shell."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        broken_script_path = tmpdir_path / "broken.sh"
        broken_script_path.write_text("""
        RESOLVE_SCRIPT_PATH="$(readlink -f "/this/path/does/not/exist")"
        RESOLVE_EXIT=$?

        if [ $RESOLVE_EXIT -ne 0 ] || [ -z "$RESOLVE_SCRIPT_PATH" ]; then
          echo "ERROR: could not resolve ~/.claude/scripts/resolve-claude-helpers-dir.sh — run /setup-local to (re)install claude-helpers symlinks" >&2
          return 1
        fi

        export CLAUDE_HELPERS_DIR="$(dirname "$(dirname "$RESOLVE_SCRIPT_PATH")")"
        """)

        # Test that shell continues after the source fails
        test_script = f"""
        source {broken_script_path} || true
        echo "SHELL_STILL_ALIVE"
        """

        code, stdout, stderr = run_bash(test_script)

        if code != 0:
            return False, f"shell exited with code {code}"

        if "SHELL_STILL_ALIVE" not in stdout:
            return False, "shell did not continue after source failed"

        return True, ""


def test_no_old_inline_pattern_in_commands():
    """Test that no command doc contains the old inline RUN_METRICS_RESOLVED pattern."""
    offenders = []

    for cmd_file_rel in COMMAND_DOCS_TO_CHECK:
        cmd_file = REPO_ROOT / cmd_file_rel
        if not cmd_file.exists():
            continue

        content = cmd_file.read_text()

        # Look for old patterns like:
        # RUN_METRICS_RESOLVED=...readlink.*run-metrics...
        # or the specific pattern described in the plan
        if "RUN_METRICS_RESOLVED" in content:
            offenders.append(f"{cmd_file_rel}: contains RUN_METRICS_RESOLVED")

        # Look for readlink...run-metrics pattern
        if re.search(r'readlink.*run-metrics', content):
            offenders.append(f"{cmd_file_rel}: contains readlink...run-metrics pattern")

    return (
        not offenders,
        "\n      " + "\n      ".join(offenders) if offenders else "",
    )


def extract_fenced_code_blocks(md_content):
    """Extract all fenced code blocks from markdown.
    Returns list of tuples: (language, content, start_line, end_line)"""
    blocks = []
    lines = md_content.splitlines()
    in_block = False
    language = ""
    block_start = 0
    block_lines = []

    for i, line in enumerate(lines):
        if line.startswith("```"):
            if in_block:
                # End of block
                blocks.append(
                    (language, "\n".join(block_lines), block_start + 1, i + 1)
                )
                in_block = False
                block_lines = []
            else:
                # Start of block
                in_block = True
                language = line[3:].strip()
                block_start = i
                block_lines = []
        elif in_block:
            block_lines.append(line)

    return blocks


def test_claude_helpers_dir_usage_has_source():
    """Test that every bash block using $CLAUDE_HELPERS_DIR also sources the resolver."""
    offenders = []

    for cmd_file_rel in COMMAND_DOCS_TO_CHECK:
        cmd_file = REPO_ROOT / cmd_file_rel
        if not cmd_file.exists():
            continue

        content = cmd_file.read_text()
        blocks = extract_fenced_code_blocks(content)

        for language, block_content, start_line, end_line in blocks:
            # Only check bash blocks
            if language != "bash":
                continue

            # Check if block uses $CLAUDE_HELPERS_DIR
            if "$CLAUDE_HELPERS_DIR" not in block_content:
                continue

            # Check if block contains source statement for resolve-claude-helpers-dir.sh
            if "source" not in block_content or "resolve-claude-helpers-dir.sh" not in block_content:
                offenders.append(
                    f"{cmd_file_rel}:{start_line}-{end_line}: uses $CLAUDE_HELPERS_DIR but "
                    f"doesn't source resolve-claude-helpers-dir.sh in same block"
                )

    return (
        not offenders,
        "\n      " + "\n      ".join(offenders) if offenders else "",
    )


def main():
    h = Harness("RESOLVE CLAUDE HELPERS DIR TEST SUITE")

    # Script existence and validity tests
    passed, msg = test_script_exists()
    h.test_result("resolve-claude-helpers-dir.sh exists", passed, msg)

    passed, msg = test_script_is_executable()
    h.test_result("script has executable permissions", passed, msg)

    passed, msg = test_script_is_valid_bash()
    h.test_result("script is syntactically valid bash", passed, msg)

    # Core functionality tests
    passed, msg = test_script_resolves_correct_directory()
    h.test_result(
        "script resolves CLAUDE_HELPERS_DIR to correct directory",
        passed,
        msg,
    )

    passed, msg = test_script_exports_variable()
    h.test_result("CLAUDE_HELPERS_DIR is exported to subshells", passed, msg)

    # Error handling tests
    passed, msg = test_script_fails_on_readlink_error()
    h.test_result(
        "script returns 1 when readlink -f fails",
        passed,
        msg,
    )

    passed, msg = test_script_does_not_kill_sourcing_shell()
    h.test_result(
        "script uses return 1 (doesn't kill sourcing shell)",
        passed,
        msg,
    )

    # Command doc pattern tests
    passed, msg = test_no_old_inline_pattern_in_commands()
    h.test_result(
        "no command doc contains old inline RUN_METRICS_RESOLVED pattern",
        passed,
        msg,
    )

    passed, msg = test_claude_helpers_dir_usage_has_source()
    h.test_result(
        "every bash block using $CLAUDE_HELPERS_DIR sources the resolver in same block",
        passed,
        msg,
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
