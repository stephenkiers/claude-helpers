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

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
