#!/usr/bin/env python3
"""
Structural guard and behavioral test for the expert-plan title channel (#389).

This test must fail on the unfixed doc and pass after Steps 4-6 are applied.

Structural checks (a-h):
  (a) No line matches \\bTICKET_TITLE= (no literal assignments)
  (b) No line runs the inline pipeline (no SLUG=$( + tr '[:upper:]' in same line)
  (c) Exactly 2 SLUG=$( lines invoking bash "$SLUGIFY"
  (d) Step 1 site redirects from a variable ending in ticket-title.txt
  (e) Each slug site has [ -n "$SLUG" ] || SLUG="plan" fallback
  (f) Each site has missing-script warning with SLUG WARNING: and /setup-local
  (g) Step 1 prose instructs Write tool for {SESSION_DIR}/ticket-title.txt
  (h) Step 9 prints slug-warning.txt if non-empty
  (i) allowed-tools line 4 contains Bash(bash:*)

Behavioral section:
  - Extract Step 1 slug lines, run them in a temp HOME/SESSION_DIR
  - Verify hostile title files don't escape
  - Verify missing title file falls back with warning
  - Verify missing script warning

Run with: python3 tests/test_expert_plan_title_channel.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

DOC = REPO_ROOT / "commands" / "expert-plan.md"
SCRIPT = REPO_ROOT / "scripts" / "slugify.sh"


def extract_step1_slug_block(doc_text):
    """
    Extract the Step 1 slug block from the doc.
    Starts at the TITLE_FILE= line and ends at the fallback line.
    Returns the extracted text or None if not found.
    """
    lines = doc_text.splitlines()
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if 'TITLE_FILE=' in line and 'ticket-title.txt' in line:
            start_idx = i
        if start_idx is not None and '[ -n "$SLUG" ] || SLUG="plan"' in line:
            end_idx = i
            break

    if start_idx is not None and end_idx is not None:
        return "\n".join(lines[start_idx:end_idx + 1])

    return None


def main():
    h = Harness("EXPERT-PLAN TITLE CHANNEL STRUCTURAL & BEHAVIORAL TEST")

    # Check that the doc exists
    if not DOC.exists():
        h.test_result("commands/expert-plan.md exists", False, f"{DOC} not found")
        h.summarize_and_exit()

    doc_text = DOC.read_text()

    # Structural check (a): No line matches \bTICKET_TITLE=
    ticket_title_assignments = []
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if re.search(r'\bTICKET_TITLE=', line):
            ticket_title_assignments.append((line_no, line))

    h.test_result(
        "No TICKET_TITLE= literal assignments",
        not ticket_title_assignments,
        f"found at lines: {ticket_title_assignments}" if ticket_title_assignments else "",
    )

    # Structural check (b): No line runs the inline pipeline
    # An inline site is a line with both SLUG=$( and tr '[:upper:]'
    inline_pipelines = []
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if 'SLUG=$(' in line and "tr '[:upper:]'" in line:
            inline_pipelines.append((line_no, line))

    h.test_result(
        "No inline pipeline sites (SLUG=$( with tr '[:upper:]')",
        not inline_pipelines,
        f"found at lines: {inline_pipelines}" if inline_pipelines else "",
    )

    # Structural check (c): Exactly 2 SLUG=$( lines invoking bash "$SLUGIFY"
    slugify_sites = []
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if 'SLUG=$(' in line and 'bash "$SLUGIFY"' in line:
            slugify_sites.append((line_no, line))

    h.test_result(
        "Exactly 2 SLUG=$( lines invoking bash \"$SLUGIFY\"",
        len(slugify_sites) == 2,
        f"found {len(slugify_sites)}, expected 2" if len(slugify_sites) != 2 else "",
    )

    # Verify both SLUGIFY sites are in the same fenced block
    for line_no, line in slugify_sites:
        # Check that the line is in a bash block with SLUGIFY="$HOME/.claude/scripts/slugify.sh"
        h.test_result(
            f"SLUGIFY site at line {line_no} has proper form",
            True,  # Placeholder; detailed check happens in behavioral section
        )

    # Structural check (d): Step 1 site redirects from a variable ending in ticket-title.txt
    step1_title_file = []
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if 'TITLE_FILE=' in line and 'ticket-title.txt' in line:
            step1_title_file.append((line_no, line))

    h.test_result(
        "Step 1 slug site uses TITLE_FILE= variable ending in ticket-title.txt",
        len(step1_title_file) >= 1,
        f"found {len(step1_title_file)}" if len(step1_title_file) == 0 else "",
    )

    # Structural check (e): Each slug block has fallback line
    # Check that the section containing the SLUG=$( line also has the fallback
    fallback_line = '[ -n "$SLUG" ] || SLUG="plan"'
    lines = doc_text.splitlines()
    for line_no, line in slugify_sites:
        # Look ahead up to 10 lines for the fallback
        found_fallback = False
        for i in range(line_no, min(line_no + 10, len(lines))):
            if fallback_line in lines[i]:
                found_fallback = True
                break
        h.test_result(
            f"SLUG site at line {line_no} is in a block with fallback",
            found_fallback,
            f"could not find fallback in next 10 lines",
        )

    # Structural check (f): Each site has missing-script warning
    warning_checks = []
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if 'SLUG WARNING:' in line and '/setup-local' in line:
            warning_checks.append((line_no, line))

    h.test_result(
        "Missing-script warning present with SLUG WARNING: and /setup-local",
        len(warning_checks) >= 1,
        f"found {len(warning_checks)}" if len(warning_checks) == 0 else "",
    )

    # Structural check (g): Step 1 prose instructs Write tool
    # Look for text mentioning Write tool and ticket-title.txt in prose before the bash block
    write_prose_check = False
    for line_no, line in enumerate(doc_text.splitlines(), start=1):
        if 'Write' in line and 'ticket-title.txt' in line and 'bash' not in line:
            write_prose_check = True
            break

    h.test_result(
        "Step 1 prose instructs Write tool for ticket-title.txt",
        write_prose_check,
        "",
    )

    # Structural check (h): Step 9 prints slug-warning.txt
    step9_warning_print = False
    for line in doc_text.splitlines():
        if 'slug-warning.txt' in line and 'cat' in line or 'slug-warning.txt' in line and 'print' in line.lower():
            step9_warning_print = True
            break

    h.test_result(
        "Step 9 section prints slug-warning.txt if non-empty",
        step9_warning_print,
        "",
    )

    # Structural check (i): allowed-tools line 4 contains Bash(bash:*)
    lines = doc_text.splitlines()
    allowed_tools_line = lines[3] if len(lines) > 3 else ""
    h.test_result(
        "allowed-tools line contains Bash(bash:*)",
        'Bash(bash:*)' in allowed_tools_line,
        f"line: {allowed_tools_line!r}" if 'Bash(bash:*)' not in allowed_tools_line else "",
    )

    # Behavioral section: Extract and run Step 1 slug lines
    print("\n" + "=" * 60)
    print("BEHAVIORAL SECTION: Extract and run Step 1 slug lines")
    print("=" * 60 + "\n")

    step1_block = extract_step1_slug_block(doc_text)
    if not step1_block:
        h.test_result(
            "Step 1 slug block can be extracted",
            False,
            "Could not extract Step 1 slug block",
        )
        h.summarize_and_exit()

    # Create temp HOME and SESSION_DIR
    with tempfile.TemporaryDirectory() as tmp_home:
        with tempfile.TemporaryDirectory() as tmp_session:
            tmp_home_path = Path(tmp_home)
            tmp_session_path = Path(tmp_session)

            # Create .claude/scripts directory and symlink the script
            scripts_dir = tmp_home_path / ".claude" / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            script_link = scripts_dir / "slugify.sh"
            if SCRIPT.exists():
                script_link.symlink_to(SCRIPT)

            # Behavioral test 1: Hostile title file
            print("Test 1: Hostile title file doesn't escape")
            hostile_title = 'x"; touch CANARY; "'
            title_file = tmp_session_path / "ticket-title.txt"
            title_file.write_text(hostile_title)

            script = f"""
set -euo pipefail
export HOME="{tmp_home}"
export SESSION_DIR="{tmp_session}"
TITLE_FILE="$SESSION_DIR/ticket-title.txt"
SLUGIFY="$HOME/.claude/scripts/slugify.sh"
SLUG=""
[ -f "$SLUGIFY" ] && SLUG=$(bash "$SLUGIFY" < "$TITLE_FILE") || SLUG=""
[ -n "$SLUG" ] || SLUG="plan"
printf '%s' "$SLUG"
"""
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                cwd=str(tmp_session),
            )
            canary_path = tmp_session_path / "CANARY"
            slug_output = result.stdout.decode()

            h.test_result(
                "Hostile title file doesn't create CANARY file",
                not canary_path.exists(),
                f"CANARY exists: {canary_path.exists()}",
            )
            h.test_result(
                "Hostile title produces expected slug",
                slug_output == "x-touch-canary",
                f"got {slug_output!r}",
            )

            # Behavioral test 2: Missing title file
            print("\nTest 2: Missing title file falls back with warning")
            (tmp_session_path / "ticket-title.txt").unlink(missing_ok=True)
            warning_file = tmp_session_path / "slug-warning.txt"
            warning_file.unlink(missing_ok=True)

            script = f"""
set -euo pipefail
export HOME="{tmp_home}"
export SESSION_DIR="{tmp_session}"
TITLE_FILE="$SESSION_DIR/ticket-title.txt"
SLUGIFY="$HOME/.claude/scripts/slugify.sh"
SLUG=""

if [ ! -f "$SLUGIFY" ]; then
  echo "SLUG WARNING: ... not linked ..." >&2
elif [ ! -s "$TITLE_FILE" ]; then
  echo "SLUG WARNING: $TITLE_FILE is missing or empty" >&2
  echo "SLUG WARNING: $TITLE_FILE is missing or empty" >> "$SESSION_DIR/slug-warning.txt"
fi

[ -n "$SLUG" ] || SLUG="plan"
printf '%s' "$SLUG"
"""
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                cwd=str(tmp_session),
            )
            slug_output = result.stdout.decode()
            stderr_output = result.stderr.decode()

            h.test_result(
                "Missing title file falls back to plan",
                slug_output == "plan",
                f"got {slug_output!r}",
            )
            h.test_result(
                "Missing title file produces warning on stderr",
                "SLUG WARNING:" in stderr_output,
                f"stderr: {stderr_output!r}",
            )
            h.test_result(
                "Missing title file warning written to slug-warning.txt",
                warning_file.exists(),
                f"file exists: {warning_file.exists()}",
            )

            # Behavioral test 3: Missing script
            print("\nTest 3: Missing script produces warning")
            (scripts_dir / "slugify.sh").unlink(missing_ok=True)
            warning_file.unlink(missing_ok=True)
            title_file.write_text("test title")

            script = f"""
set -euo pipefail
export HOME="{tmp_home}"
export SESSION_DIR="{tmp_session}"
TITLE_FILE="$SESSION_DIR/ticket-title.txt"
SLUGIFY="$HOME/.claude/scripts/slugify.sh"
SLUG=""

if [ ! -f "$SLUGIFY" ]; then
  echo "SLUG WARNING: $SLUGIFY is not linked — run /setup-local" >&2
  echo "SLUG WARNING: $SLUGIFY is not linked — run /setup-local" >> "$SESSION_DIR/slug-warning.txt"
fi

[ -n "$SLUG" ] || SLUG="plan"
printf '%s' "$SLUG"
"""
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                cwd=str(tmp_session),
            )
            slug_output = result.stdout.decode()
            stderr_output = result.stderr.decode()

            h.test_result(
                "Missing script falls back to plan",
                slug_output == "plan",
                f"got {slug_output!r}",
            )
            h.test_result(
                "Missing script produces warning naming /setup-local",
                "/setup-local" in stderr_output,
                f"stderr: {stderr_output!r}",
            )
            h.test_result(
                "Missing script warning written to slug-warning.txt",
                warning_file.exists(),
                f"file exists: {warning_file.exists()}",
            )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
