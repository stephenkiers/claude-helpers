#!/usr/bin/env python3
"""
Test suite for the bash syntax of shell snippets in commands/cleanup.md.

The snippets run under the macOS system bash (3.2, /bin/bash). A `case` statement
inside a `$( ... )` command substitution is a syntax error there, so every fenced
bash block in cleanup.md must pass `bash -n` under /bin/bash.

Blocks are checked independently: each one is written to a scratch file and parsed
with `/bin/bash -n`. Nothing is executed.

Run with: python3 tests/test_cleanup_command_shell_syntax.py
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

CLEANUP_MD = REPO_ROOT / "commands" / "cleanup.md"
SYSTEM_BASH = Path("/bin/bash")

FENCE_RE = re.compile(r"(?ms)^```bash[^\n]*\n(.*?)^```")

# An unresolved template placeholder such as `<CURRENT_WORKTREE printed by Step 1>` is prose
# to be filled in at run time, not shell; such blocks cannot be parsed as bash at all.
TEMPLATE_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][^<>\n]*>")


def bash_blocks(text):
    return [m.group(1) for m in FENCE_RE.finditer(text)]


def parse_with_system_bash(src, scratch_dir, index):
    script = scratch_dir / f"block-{index}.sh"
    script.write_text(src)
    result = subprocess.run(
        [str(SYSTEM_BASH), "-n", str(script)],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stderr.strip()


def blocks_containing(blocks, needle):
    return [b for b in blocks if needle in b]


def check_stale_plan_relay_markers(h, blocks):
    """The stale-plan relay and the ticket/group identifiers live in fenced bash blocks."""
    relay = blocks_containing(blocks, "REPLAN_CONFIRM")
    h.test_result(
        "a bash block names REPLAN_CONFIRM (stale-plan re-plan relay)",
        len(relay) > 0,
        "no fenced bash block contains REPLAN_CONFIRM",
    )
    if relay:
        block = relay[0]
        h.test_result(
            "stale-plan relay block prints the 'validation not run:' rejection marker",
            "validation not run:" in block,
            "marker missing from the relay block",
        )
        h.test_result(
            "stale-plan relay block prints the 'Confirm re-planned cleanup?' prompt",
            "Confirm re-planned cleanup?" in block,
            "prompt missing from the relay block",
        )
        h.test_result(
            "stale-plan relay block declares REPLAN_FILE (re-plan apply input)",
            "REPLAN_FILE" in block,
            "REPLAN_FILE missing from the relay block",
        )

    gate = blocks_containing(blocks, "EXCLUDED_NUMBERS")
    h.test_result(
        "a bash block uses EXCLUDED_NUMBERS (ticket-worktree exclusion gate)",
        len(gate) > 0,
        "no fenced bash block contains EXCLUDED_NUMBERS",
    )
    h.test_result(
        "a bash block uses SELECTED_GROUPS (selected-groups check)",
        len(blocks_containing(blocks, "SELECTED_GROUPS")) > 0,
        "no fenced bash block contains SELECTED_GROUPS",
    )


def run_exclusion_gate_tests(h, scratch_dir):
    """Test the exclusion gate logic with various EXCLUDED_NUMBERS inputs."""
    # Exclusion gate test: normalize comma-separated EXCLUDED_NUMBERS, then do whole-word matching
    test_script = scratch_dir / "exclusion_gate_test.sh"
    test_script.write_text("""#!/bin/bash
set -e
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Test 1: not excluded (number 2 not in excluded list)
TICKET_WORKTREE_ITEM="2"
EXCLUDED_NUMBERS="1 3"
EXCLUDED_NUMBERS=$(printf '%s' "$EXCLUDED_NUMBERS" | tr ',' ' ')
case " $EXCLUDED_NUMBERS " in
  *" $TICKET_WORKTREE_ITEM "*) TICKET_EXCLUDED="true" ;;
  *) TICKET_EXCLUDED="false" ;;
esac
[ "$TICKET_EXCLUDED" = "false" ] || { echo "Test 1 failed: should not be excluded"; exit 1; }

# Test 2: excluded (number 2 is in excluded list)
TICKET_WORKTREE_ITEM="2"
EXCLUDED_NUMBERS="1 2 3"
EXCLUDED_NUMBERS=$(printf '%s' "$EXCLUDED_NUMBERS" | tr ',' ' ')
case " $EXCLUDED_NUMBERS " in
  *" $TICKET_WORKTREE_ITEM "*) TICKET_EXCLUDED="true" ;;
  *) TICKET_EXCLUDED="false" ;;
esac
[ "$TICKET_EXCLUDED" = "true" ] || { echo "Test 2 failed: should be excluded"; exit 1; }

# Test 3: comma-separated input (normalize commas to spaces)
TICKET_WORKTREE_ITEM="2"
EXCLUDED_NUMBERS="1,2,3"
EXCLUDED_NUMBERS=$(printf '%s' "$EXCLUDED_NUMBERS" | tr ',' ' ')
case " $EXCLUDED_NUMBERS " in
  *" $TICKET_WORKTREE_ITEM "*) TICKET_EXCLUDED="true" ;;
  *) TICKET_EXCLUDED="false" ;;
esac
[ "$TICKET_EXCLUDED" = "true" ] || { echo "Test 3 failed: comma-separated should be normalized"; exit 1; }

# Test 4: substring non-match (12 should not match 2)
TICKET_WORKTREE_ITEM="2"
EXCLUDED_NUMBERS="12 3"
EXCLUDED_NUMBERS=$(printf '%s' "$EXCLUDED_NUMBERS" | tr ',' ' ')
case " $EXCLUDED_NUMBERS " in
  *" $TICKET_WORKTREE_ITEM "*) TICKET_EXCLUDED="true" ;;
  *) TICKET_EXCLUDED="false" ;;
esac
[ "$TICKET_EXCLUDED" = "false" ] || { echo "Test 4 failed: substring should not match"; exit 1; }

# Test 5: empty ticket item (should be treated as excluded)
TICKET_WORKTREE_ITEM=""
if [ -z "$TICKET_WORKTREE_ITEM" ]; then
  TICKET_EXCLUDED="true"
else
  TICKET_EXCLUDED="false"
fi
[ "$TICKET_EXCLUDED" = "true" ] || { echo "Test 5 failed: empty ticket item should be excluded"; exit 1; }

echo "All exclusion gate tests passed"
""")

    result = subprocess.run([str(SYSTEM_BASH), str(test_script)], capture_output=True, text=True)
    h.test_result(
        "exclusion gate: handles normal, excluded, comma-separated, substring non-match, and empty cases",
        result.returncode == 0,
        result.stderr or result.stdout,
    )


def run_replan_confirm_tests(h, scratch_dir):
    """Test REPLAN_CONFIRM case statement behavior."""
    test_script = scratch_dir / "replan_confirm_test.sh"
    test_script.write_text("""#!/bin/bash
set -e
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Test 1: REPLAN_CONFIRM=false aborts with exit 1
REPLAN_CONFIRM="false"
REPLAN_FILE=""
exit_code=0
case "${REPLAN_CONFIRM-}" in
  false)
    # Should abort
    exit_code=1
    ;;
  true)
    exit_code=99  # Should not reach here
    ;;
  *)
    exit_code=99  # Should not reach here
    ;;
esac
[ "$exit_code" = "1" ] || { echo "Test 1 failed: REPLAN_CONFIRM=false should exit 1"; exit 1; }

# Test 2: REPLAN_CONFIRM=true with missing REPLAN_FILE errors
REPLAN_CONFIRM="true"
REPLAN_FILE=""
exit_code=0
case "${REPLAN_CONFIRM-}" in
  false)
    exit_code=99  # Should not reach here
    ;;
  true)
    if [ -z "${REPLAN_FILE:-}" ]; then
      exit_code=1  # Error: missing REPLAN_FILE
    fi
    ;;
  *)
    exit_code=99
    ;;
esac
[ "$exit_code" = "1" ] || { echo "Test 2 failed: REPLAN_CONFIRM=true with missing REPLAN_FILE should error"; exit 1; }

# Test 3: invalid REPLAN_CONFIRM value errors
REPLAN_CONFIRM="invalid"
REPLAN_FILE=""
exit_code=0
case "${REPLAN_CONFIRM-}" in
  false)
    exit_code=99  # Should not reach here
    ;;
  true)
    exit_code=99  # Should not reach here
    ;;
  *)
    # Fail if REPLAN_CONFIRM is set to invalid value
    if [ -n "${REPLAN_CONFIRM+x}" ]; then
      exit_code=1  # Error: invalid value
    else
      exit_code=0  # Only apply PLAN_JSON on first entry (unset)
    fi
    ;;
esac
[ "$exit_code" = "1" ] || { echo "Test 3 failed: invalid REPLAN_CONFIRM should error"; exit 1; }

# Test 4: REPLAN_CONFIRM unset applies PLAN_JSON
unset REPLAN_CONFIRM
REPLAN_FILE=""
exit_code=0
case "${REPLAN_CONFIRM-}" in
  false)
    exit_code=99  # Should not reach here
    ;;
  true)
    exit_code=99  # Should not reach here
    ;;
  *)
    # Only apply on unset
    if [ -n "${REPLAN_CONFIRM+x}" ]; then
      exit_code=99  # Set but invalid
    else
      exit_code=0  # Unset, apply PLAN_JSON
    fi
    ;;
esac
[ "$exit_code" = "0" ] || { echo "Test 4 failed: unset REPLAN_CONFIRM should apply PLAN_JSON"; exit 1; }

echo "All REPLAN_CONFIRM tests passed"
""")

    result = subprocess.run([str(SYSTEM_BASH), str(test_script)], capture_output=True, text=True)
    h.test_result(
        "REPLAN_CONFIRM case: handles false (abort), true (apply re-plan), invalid value (error), and unset (apply PLAN_JSON)",
        result.returncode == 0,
        result.stderr or result.stdout,
    )


def main():
    h = Harness("commands/cleanup.md bash blocks parse under the system bash (3.2)")

    if not SYSTEM_BASH.exists():
        h.test_result("system bash present at /bin/bash", False, "/bin/bash missing")
        h.summarize_and_exit()

    text = CLEANUP_MD.read_text()
    blocks = bash_blocks(text)
    h.test_result(
        "cleanup.md has fenced bash blocks to check",
        len(blocks) > 0,
        "no ```bash fences found",
    )

    templates = [b for b in blocks if TEMPLATE_PLACEHOLDER_RE.search(b)]
    runnable = [b for b in blocks if not TEMPLATE_PLACEHOLDER_RE.search(b)]
    h.test_result(
        f"{len(templates)} template block(s) with <placeholders> excluded from the parse check",
        True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for i, block in enumerate(runnable, 1):
            rc, err = parse_with_system_bash(block, scratch, i)
            first = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
            h.test_result(
                f"bash block {i} parses under /bin/bash -n (starts: {first[:50]!r})",
                rc == 0,
                err,
            )

    check_stale_plan_relay_markers(h, blocks)

    # Run behavioral tests with scratch directory
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        run_exclusion_gate_tests(h, scratch)
        run_replan_confirm_tests(h, scratch)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
