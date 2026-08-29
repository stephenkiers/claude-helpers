#!/usr/bin/env python3
"""
Test suite for merge timeout configuration and PR-scoped state protocol.

Tests spec changes:
1. Configurable merge timeout with DEFAULT_MERGE_APPLY_TIMEOUT_SECS and _get_merge_apply_timeout()
2. PR-scoped state directory /tmp/merge-and-cleanup.pr-{PR_NUM} with validation and self-cleaning

Run with: python3 tests/test_merge_timeout_and_state_protocol.py
"""

import sys
import os
import re
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.merge import DEFAULT_MERGE_APPLY_TIMEOUT_SECS, _get_merge_apply_timeout
from _test_harness import REPO_ROOT, Harness


def extract_bash_blocks(md_text):
    """Return the source of every ```bash fenced block in a command document."""
    return re.findall(r"```bash\n(.*?)```", md_text, re.DOTALL)


def extract_exit_code_guards(md_text):
    """
    Pull the real apply_exit_code guard(s) out of the command doc.

    The point is to execute the bash that actually ships, not a copy of it: a
    hand-transcribed guard in this file would keep passing even if the document
    regressed to the `[ "$X" -ne 0 ]` form whose usage-error-treated-as-false
    behaviour is the whole bug being guarded against.

    A guard runs from the line that reads apply_exit_code into a variable through
    the `if ...; then` that acts on it.
    """
    guards = []
    for block in extract_bash_blocks(md_text):
        block_lines = block.split("\n")
        start = None
        for i, line in enumerate(block_lines):
            is_if = re.match(r"\s*if .*; then\s*$", line)
            if start is not None and is_if:
                guards.append("\n".join(block_lines[start:i + 1]))
                start = None
            elif is_if and "apply_exit_code" in line:
                # Single-line form (including the pre-fix `[ "$(cat ...)" -ne 0 ]` shape).
                # Matching it too means a regression is caught by *executing* it and seeing
                # it wave an empty file through -- a semantic failure, not merely "the shape
                # I expected is gone".
                guards.append(line)
            elif start is None and "apply_exit_code" in line and "cat" in line and "=" in line:
                start = i
    return guards


def run_guard(guard_src, state_dir):
    """Execute an extracted guard against a fixture state dir; 0 = passed through, 1 = tripped."""
    script = f'MC_STATE_DIR="$1"\n{guard_src}\n  exit 1\nfi\nexit 0\n'
    return subprocess.run(["bash", "-c", script, "bash", str(state_dir)],
                          capture_output=True).returncode



if __name__ == "__main__":
    h = Harness("MERGE TIMEOUT AND STATE PROTOCOL TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Timeout constant exists and has correct default value")

    try:
        timeout_value = DEFAULT_MERGE_APPLY_TIMEOUT_SECS
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS is defined",
            timeout_value is not None,
        )
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS equals 1800",
            timeout_value == 1800,
            f"Expected 1800, got {timeout_value}"
        )
    except Exception as e:
        test_result(
            "DEFAULT_MERGE_APPLY_TIMEOUT_SECS is defined",
            False,
            str(e)
        )

    print()
    print("[Section 2] _get_merge_apply_timeout() with unset env var returns default")

    # Save original env var
    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        # Ensure env var is unset
        if "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is unset",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        # Restore original env var
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 3] _get_merge_apply_timeout() with empty env var returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = ""
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is empty",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 4] _get_merge_apply_timeout() with valid positive integer returns that value")

    test_cases = ["2400", "3600", "100", "1"]
    for test_val in test_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            result = _get_merge_apply_timeout()
            expected = int(test_val)
            test_result(
                f"Returns {expected} when env var is '{test_val}'",
                result == expected,
                f"Expected {expected}, got {result}"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 5] _get_merge_apply_timeout() with zero returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "0"
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is '0'",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 6] _get_merge_apply_timeout() with negative value returns default")

    original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
    try:
        os.environ["MERGE_APPLY_TIMEOUT_SECS"] = "-5"
        result = _get_merge_apply_timeout()
        test_result(
            "Returns default (1800) when env var is '-5'",
            result == 1800,
            f"Expected 1800, got {result}"
        )
    finally:
        if original_env is not None:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
        elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
            del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 7] _get_merge_apply_timeout() with non-numeric value returns default")

    test_cases = ["abc", "12.5", "10s", "timeout", "1e3"]
    for test_val in test_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            result = _get_merge_apply_timeout()
            test_result(
                f"Returns default (1800) when env var is '{test_val}'",
                result == 1800,
                f"Expected 1800, got {result}"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 8] _get_merge_apply_timeout() never raises an exception")

    invalid_cases = ["abc", "-1", "0", "", "   ", "!@#$%"]
    for test_val in invalid_cases:
        original_env = os.environ.get("MERGE_APPLY_TIMEOUT_SECS")
        try:
            os.environ["MERGE_APPLY_TIMEOUT_SECS"] = test_val
            exception_raised = False
            try:
                result = _get_merge_apply_timeout()
            except Exception:
                exception_raised = True

            test_result(
                f"Does not raise for env var '{test_val}'",
                not exception_raised,
                "Should not raise exception"
            )
        finally:
            if original_env is not None:
                os.environ["MERGE_APPLY_TIMEOUT_SECS"] = original_env
            elif "MERGE_APPLY_TIMEOUT_SECS" in os.environ:
                del os.environ["MERGE_APPLY_TIMEOUT_SECS"]

    print()
    print("[Section 9] merge-and-cleanup.md does not reference /tmp/merge-and-cleanup.latest")

    merge_cmd_path = REPO_ROOT / "commands" / "merge-and-cleanup.md"
    try:
        merge_cmd_content = merge_cmd_path.read_text()
        has_latest = "/tmp/merge-and-cleanup.latest" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md does not contain /tmp/merge-and-cleanup.latest",
            not has_latest,
            "The pointer file /tmp/merge-and-cleanup.latest should not be used"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md does not contain /tmp/merge-and-cleanup.latest",
            False,
            str(e)
        )

    print()
    print("[Section 10] merge-and-cleanup.md uses PR-scoped state directory pattern")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        has_pr_pattern = "/tmp/merge-and-cleanup.pr-" in merge_cmd_content
        test_result(
            "merge-and-cleanup.md contains /tmp/merge-and-cleanup.pr- pattern",
            has_pr_pattern,
            "Should use PR-scoped state directory"
        )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md contains /tmp/merge-and-cleanup.pr- pattern",
            False,
            str(e)
        )

    print()
    print("[Section 11] merge-and-cleanup.md cross-checks pr_num at BOTH read sites")

    merge_cmd_content = merge_cmd_path.read_text()
    # The guard that matters is a *comparison* wired to an exit, at each phase that reads
    # state -- not the mere presence of the string "pr_num" (which the Phase 1 write alone
    # would satisfy, leaving a deleted cross-check undetected).
    pr_num_checks = re.findall(
        r'if \[ "\$\(cat "\$MC_STATE_DIR/pr_num".*?\)" != "\$PR_NUM" \]; then',
        merge_cmd_content)
    test_result(
        "pr_num cross-check compares against $PR_NUM at both read sites",
        len(pr_num_checks) == 2,
        f"expected 2 pr_num cross-checks (Phase 3 and Phase 4), found {len(pr_num_checks)}")

    mismatch_exits = merge_cmd_content.count("ERROR: PR number mismatch in state directory")
    test_result(
        "each pr_num mismatch fails loudly",
        mismatch_exits == 2,
        f"expected 2 loud mismatch errors, found {mismatch_exits}")

    print()
    print("[Section 12] merge-and-cleanup.md guards apply_exit_code at BOTH read sites")

    guards = extract_exit_code_guards(merge_cmd_content)
    test_result(
        "an apply_exit_code guard is extractable from Phase 3 and Phase 4",
        len(guards) == 2,
        f"expected 2 extractable guards, found {len(guards)} -- the guard's shape may have "
        f"changed, which would silently disable Sections 16-20")

    print()
    print("[Section 13] merge-and-cleanup.md removes the state dir, and only the state dir")

    rm_lines = [ln.strip() for blk in extract_bash_blocks(merge_cmd_content)
                for ln in blk.split("\n")
                if re.search(r"\brm\b", ln) and not ln.strip().startswith("#")]
    test_result(
        "every rm targets the state dir",
        bool(rm_lines) and all("$MC_STATE_DIR" in ln for ln in rm_lines),
        f"found rm lines not scoped to $MC_STATE_DIR: "
        f"{[ln for ln in rm_lines if '$MC_STATE_DIR' not in ln]}")
    test_result(
        "the state dir is removed recursively on the success path",
        any(re.search(r'rm -rf "\$MC_STATE_DIR"\s*$', ln) for ln in rm_lines),
        'expected a `rm -rf "$MC_STATE_DIR"` line')

    print("[Section 14] merge-and-cleanup.md frontmatter grants Bash(rm:*)")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        # Extract frontmatter (between first --- and second ---)
        frontmatter_match = re.search(r'^---\n(.*?)\n---', merge_cmd_content, re.DOTALL | re.MULTILINE)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            has_rm_grant = "rm:*" in frontmatter or "Bash(rm" in frontmatter
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
                has_rm_grant,
                "Should grant rm capability for cleanup"
            )
        else:
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
                False,
                "Could not find frontmatter"
            )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md frontmatter includes Bash(rm:*) grant",
            False,
            str(e)
        )

    print()
    print("[Section 15] merge-and-cleanup.md frontmatter grants Bash(mkdir:*)")

    try:
        merge_cmd_content = merge_cmd_path.read_text()
        frontmatter_match = re.search(r'^---\n(.*?)\n---', merge_cmd_content, re.DOTALL | re.MULTILINE)
        if frontmatter_match:
            frontmatter = frontmatter_match.group(1)
            has_mkdir_grant = "mkdir:*" in frontmatter or "Bash(mkdir" in frontmatter
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
                has_mkdir_grant,
                "Should grant mkdir capability for state directory creation"
            )
        else:
            test_result(
                "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
                False,
                "Could not find frontmatter"
            )
    except Exception as e:
        test_result(
            "merge-and-cleanup.md frontmatter includes Bash(mkdir:*) grant",
            False,
            str(e)
        )

    print()
    print("[Sections 16-20] The REAL extracted guard, executed against fixtures")

    # Each case runs the bash lifted out of commands/merge-and-cleanup.md above. If the
    # document regresses, these fail -- which a hand-written copy of the guard would not.
    guards = extract_exit_code_guards(merge_cmd_path.read_text())
    if not guards:
        test_result("exit-code guard is extractable from the command doc", False,
                    "no guard could be extracted -- cannot execute the real logic")
    else:
        cases = [
            ("missing file",       None,   1),
            ("empty file",         "",     1),
            ("non-numeric content", "abc", 1),
            ("non-zero exit code", "127",  1),
            ("trailing newline, zero", "0\n", 0),
            ("zero exit code",     "0",    0),
        ]
        for phase_idx, guard_src in enumerate(guards, start=3):
            for label, content, expected in cases:
                with tempfile.TemporaryDirectory() as tmpdir:
                    state_dir = Path(tmpdir)
                    if content is not None:
                        (state_dir / "apply_exit_code").write_text(content)
                    rc = run_guard(guard_src, state_dir)
                    test_result(
                        f"Phase {phase_idx} guard: {label} -> "
                        f"{'trips' if expected else 'passes through'}",
                        rc == expected,
                        f"expected returncode {expected}, got {rc}")

    h.summarize_and_exit()
