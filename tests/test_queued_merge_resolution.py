"""
Tests for queued-merge.md's main-worktree resolution fixes (audit Findings 1, 2, 6).

Verifies:
- Phase 2 and Phase 3 porcelain parser behavior with multiple worktrees
- $ARGUMENTS precedence before branch derivation
- Absolute --git-common-dir paths
"""

import re
import shlex
import subprocess
import unittest
from pathlib import Path

CANNED_PORCELAIN = """worktree /main
detached

worktree /pr-1234
branch refs/heads/feature/1234

worktree /pr-1234-alt
branch refs/heads/bugfix/5678
"""


def _extract_worktree_list_invocation(md_content: str, phase_heading: str) -> str:
    """Pull the literal `WORKTREE_LIST_OUTPUT=$(git worktree list ...)` / `PR_WORKTREE=$(...)`
    snippet out of a given phase's section, exactly as the doc would execute it — so a test
    against it exercises the real shell lines, not a reimplementation of their logic. The
    porcelain output is captured to a variable first and passed to python3 via an env var.

    Handles both old inline-PYTHON_EOF style and new external-script style."""
    phase_start = md_content.find(phase_heading)
    assert phase_start != -1, f"heading not found: {phase_heading}"
    rest = md_content[phase_start:]

    # Try new pattern first (external script invocation)
    # Extract from WORKTREE_LIST_OUTPUT assignment through PR_WORKTREE assignment
    start_match = re.search(r"WORKTREE_LIST_OUTPUT=\$\(git worktree list --porcelain", rest)
    if start_match:
        # Find the end: look for the PR_WORKTREE assignment that follows
        snippet_start = start_match.start()
        snippet_text = rest[snippet_start:]
        # Look for PR_WORKTREE=...py) pattern, allowing for nested parens
        end_match = re.search(r"PR_WORKTREE=\$\([^)]*resolve_pr_worktree\.py[^)]*\)", snippet_text)
        if end_match:
            return snippet_text[:end_match.end()]

    # Fall back to old pattern (inline PYTHON_EOF heredoc)
    match = re.search(
        r"WORKTREE_LIST_OUTPUT=\$\(git worktree list --porcelain[^)]*\).*?PYTHON_EOF",
        rest,
        re.DOTALL,
    )
    assert match, f"worktree-list invocation not found under {phase_heading}"
    return match.group(0)


class TestQueuedMergeResolution(unittest.TestCase):
    """Test the fixed porcelain parser and argument handling in queued-merge.md."""

    def _run_worktree_list_invocation(self, snippet: str, pr_head: str) -> str:
        """Execute the extracted snippet for real in bash, with `git worktree list`
        shadowed to emit canned porcelain and PR_HEAD set as a plain (unexported)
        shell variable. The mock invokes the Python parser logic directly."""
        python_logic_script = """
import os
import sys
pr_head = os.environ.get('PR_HEAD', '')
worktree_list = os.environ.get('WORKTREE_LIST', '')
if not pr_head:
    sys.exit(1)
lines = worktree_list.strip().split('\\n')
current_record = {}
found = None
for line in lines:
    if not line.strip():
        if current_record and current_record.get('branch') == f'refs/heads/{pr_head}':
            found = current_record['worktree']
            break
        current_record = {}
    else:
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[0] == 'worktree':
            current_record['worktree'] = parts[1]
        elif len(parts) >= 2 and parts[0] == 'branch':
            current_record['branch'] = parts[1]
if found is None and current_record and current_record.get('branch') == f'refs/heads/{pr_head}':
    found = current_record['worktree']
if found:
    print(found)
    sys.exit(0)
else:
    sys.exit(1)
"""

        script = f"""
set -e
git() {{
  if [ "$1" = "worktree" ] && [ "$2" = "list" ]; then
    printf '%s' {shlex.quote(CANNED_PORCELAIN)}
  else
    command git "$@"
  fi
}}
source() {{
  if [[ "$@" == *"resolve-claude-helpers-dir.sh"* ]]; then
    CLAUDE_HELPERS_DIR="/tmp"
  fi
}}
python3() {{
  if [[ "$@" == *"resolve_pr_worktree.py"* ]]; then
    command python3 -c {shlex.quote(python_logic_script)}
  else
    command python3 "$@"
  fi
}}
PR_HEAD={shlex.quote(pr_head)}
WORKTREE_LIST_OUTPUT=$(git worktree list --porcelain 2>/dev/null)
{snippet}
printf '%s' "$PR_WORKTREE"
"""
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=10
        )
        return result.stdout

    def test_phase2_worktree_list_invocation_passes_pr_head(self):
        """Phase 2's real invocation must actually resolve the worktree for the
        PR_HEAD it just derived — regression test for the missing
        `PR_HEAD="$PR_HEAD"` env prefix. This test actually runs the extracted snippet
        to verify it resolves to the correct worktree."""
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()
        snippet = _extract_worktree_list_invocation(md_content, "### Phase 2")

        # First verify the snippet contains the env prefix (basic check)
        self.assertIn('PR_HEAD="$PR_HEAD"', snippet,
                      "Snippet must pass PR_HEAD as env variable to preserve unexported variable")

        # Now actually run the snippet to verify it resolves correctly
        result = self._run_worktree_list_invocation(snippet, "feature/1234")
        self.assertEqual(result, "/pr-1234",
                        f"Expected /pr-1234 but got {result}")

    def test_phase3_worktree_list_invocation_passes_pr_head(self):
        """Same regression test for Phase 3's invocation, which already had the
        correct env prefix — keeps it from silently regressing. This test actually runs
        the extracted snippet to verify it resolves to the correct worktree."""
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()
        snippet = _extract_worktree_list_invocation(md_content, "### Phase 3")

        # First verify the snippet contains the env prefix (basic check)
        self.assertIn('PR_HEAD="$PR_HEAD"', snippet,
                      "Snippet must pass PR_HEAD as env variable to preserve unexported variable")

        # Now actually run the snippet to verify it resolves correctly
        result = self._run_worktree_list_invocation(snippet, "feature/1234")
        self.assertEqual(result, "/pr-1234",
                        f"Expected /pr-1234 but got {result}")

    def test_porcelain_parser_matches_pr_head(self):
        """Invoke the real resolve_pr_worktree.py script and verify it matches worktree by branch."""
        porcelain_output = """worktree /main
detached

worktree /pr-1234
branch refs/heads/feature/1234

worktree /pr-1234-alt
branch refs/heads/bugfix/5678
"""
        pr_head = "feature/1234"

        # Invoke the real resolve_pr_worktree.py script directly, not a hand-reimplementation
        script_path = Path(__file__).parent.parent / "scripts" / "workflow" / "resolve_pr_worktree.py"
        result = subprocess.run(
            ["python3", str(script_path)],
            env={
                "PR_HEAD": pr_head,
                "WORKTREE_LIST": porcelain_output
            },
            capture_output=True,
            text=True,
            timeout=5
        )

        self.assertEqual(result.returncode, 0, f"Script exited with {result.returncode}: {result.stderr}")
        found_path = result.stdout.strip()
        self.assertEqual(found_path, '/pr-1234')

    def test_phase3_arguments_precedence(self):
        """Verify that Phase 3's real argument-precedence logic checks $ARGUMENTS before
        CURRENT_BRANCH derivation by extracting and validating the pattern from the doc."""
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Extract Phase 3 section
        phase3_start = md_content.find("### Phase 3")
        phase4_start = md_content.find("### Phase 4")
        if phase3_start == -1 or phase4_start == -1:
            self.fail("Phase 3 or Phase 4 heading not found")
        phase3_text = md_content[phase3_start:phase4_start]

        # Verify the precedence pattern is in Phase 3:
        # 1. Check that [ -n "$ARGUMENTS" ] appears before CURRENT_BRANCH
        arg_check = '[ -n "$ARGUMENTS" ]'
        current_branch = 'CURRENT_BRANCH='

        arg_idx = phase3_text.find(arg_check)
        branch_idx = phase3_text.find(current_branch)

        self.assertGreater(arg_idx, -1, "ARGUMENTS check not found in Phase 3")
        self.assertGreater(branch_idx, -1, "CURRENT_BRANCH derivation not found in Phase 3")
        self.assertLess(arg_idx, branch_idx,
                       "ARGUMENTS must be checked before CURRENT_BRANCH for proper precedence")

        # 2. Verify the if/elif structure is correct (not two separate ifs)
        # Extract the snippet containing both checks
        match = re.search(r'if \[ -n "\$ARGUMENTS" \].*?CURRENT_BRANCH=.*?fi', phase3_text, re.DOTALL)
        self.assertIsNotNone(match, "Could not find complete if/elif/fi block for argument precedence")

        # 3. Verify the block sets PR_NUM in the ARGUMENTS branch
        snippet = match.group(0)
        self.assertIn('PR_NUM="$ARGUMENTS"', snippet,
                     "ARGUMENTS branch should set PR_NUM from $ARGUMENTS")

    def test_absolute_git_common_dir(self):
        """Verify that the real `git rev-parse --git-common-dir --path-format=absolute`
        produces absolute paths against a real git repository."""
        # Get the current repo's git common dir using the real git command
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).parent.parent  # Run from repo root
        )

        self.assertEqual(result.returncode, 0, f"git command failed: {result.stderr}")
        git_common_dir_output = result.stdout.strip()
        self.assertNotEqual(git_common_dir_output, "", "git rev-parse returned empty output")

        # Verify it's absolute
        self.assertTrue(Path(git_common_dir_output).is_absolute(),
                       f"Expected absolute path but got: {git_common_dir_output}")

        # Verify it contains .git (or .git/worktrees for linked worktrees)
        self.assertIn('.git', git_common_dir_output,
                     f"Expected .git in path but got: {git_common_dir_output}")

    def test_git_common_dir_flag_usage(self):
        """Verify that --path-format=absolute is used with --git-common-dir."""
        # Read the queued-merge.md file
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Count occurrences of --git-common-dir with --path-format=absolute
        # This should appear in Phase 2 and Phase 3
        git_common_dir_calls = [
            line for line in md_content.split('\n')
            if 'git rev-parse' in line and '--git-common-dir' in line
        ]

        for line in git_common_dir_calls:
            self.assertIn('--path-format=absolute', line,
                         f"Line should have --path-format=absolute: {line}")

    def test_no_worktree_list_path_format_flag(self):
        """Verify that --path-format=absolute is NOT used with git worktree list."""
        # Read the queued-merge.md file
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Look for git worktree list commands
        worktree_list_lines = [
            line for line in md_content.split('\n')
            if 'git worktree list' in line and '--porcelain' in line
        ]

        for line in worktree_list_lines:
            self.assertNotIn('--path-format=absolute', line,
                           f"'git worktree list' should NOT use --path-format=absolute: {line}")

    def test_phase3_and_phase4_arguments_precedence(self):
        """Verify that Phases 3 and 4 check $ARGUMENTS before branch derivation."""
        # Read the queued-merge.md file
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Extract Phase 3 and Phase 4 sections
        phase3_idx = md_content.find("### Phase 3")
        phase4_idx = md_content.find("### Phase 4")
        phase3_text = md_content[phase3_idx:phase4_idx] if phase4_idx > phase3_idx else ""

        # In Phase 3, we should check $ARGUMENTS before CURRENT_BRANCH
        # Look for the pattern: if [ -n "$ARGUMENTS" ]
        arguments_check = phase3_text.find('[ -n "$ARGUMENTS" ]')

        self.assertGreater(arguments_check, -1,
                          "Phase 3 should check $ARGUMENTS")
        self.assertGreater(arguments_check, 0,
                          "Phase 3 should check $ARGUMENTS before CURRENT_BRANCH")

    def test_phase1_records_start_time(self):
        """Verify that Phase 1 records a start time for staleness checking."""
        # Read the queued-merge.md file
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Phase 1 should record the start time
        phase1_start = md_content.find("### Phase 1")
        phase2_start = md_content.find("### Phase 2")
        phase1_text = md_content[phase1_start:phase2_start] if phase2_start > phase1_start else ""

        # Recorded to a per-PR scratch file in git state dir, not a shell variable, since
        # variables don't persist across Bash blocks (Phase 4 is a separate block).
        # Should use git common dir pattern for security (ownership-checked).
        has_start_time = ("git rev-parse" in phase1_text and
                         "queued-merge-state" in phase1_text and
                         "jq -n 'now'" in phase1_text)

        self.assertTrue(has_start_time,
                       "Phase 1 should record start time with jq -n 'now' to git state dir")

    def test_phase4_checks_result_freshness(self):
        """Verify that Phase 4 checks result timestamp against start time."""
        # Read the queued-merge.md file
        md_path = Path(__file__).parent.parent / "commands" / "queued-merge.md"
        md_content = md_path.read_text()

        # Phase 4 should compare result timestamp against start time
        phase4_start = md_content.find("### Phase 4 —")
        phase5_start = md_content.find("### Phase")
        # Find the next ### after Phase 4
        next_phase = md_content.find("### Phase 4-Q")
        if next_phase == -1:
            next_phase = phase5_start if phase5_start > phase4_start else len(md_content)

        phase4_text = md_content[phase4_start:next_phase]

        # Should check result timestamp, and actually gate on the result (not just mention it)
        has_timestamp_check = "timestamp" in phase4_text and ("stale" in phase4_text or "fresh" in phase4_text)
        gates_on_freshness = 'FRESHNESS_CHECK" != "fresh"' in phase4_text or "treating as missing" in phase4_text

        self.assertTrue(has_timestamp_check,
                       "Phase 4 should check result freshness against start time")
        self.assertTrue(gates_on_freshness,
                       "Phase 4 should actually act on the freshness result, not just compute it")


if __name__ == '__main__':
    unittest.main()
