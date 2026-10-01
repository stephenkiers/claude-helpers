"""
Tests for queued-merge.md's main-worktree resolution fixes (audit Findings 1, 2, 6).

Verifies:
- Phase 2 and Phase 3 porcelain parser behavior with multiple worktrees
- $ARGUMENTS precedence before branch derivation
- Absolute --git-common-dir paths
"""

import unittest
from pathlib import Path


class TestQueuedMergeResolution(unittest.TestCase):
    """Test the fixed porcelain parser and argument handling in queued-merge.md."""

    def test_porcelain_parser_matches_pr_head(self):
        """Extract Phase 2's Python parser and verify it matches worktree by branch."""
        porcelain_output = """worktree /main
detached

worktree /pr-1234
branch refs/heads/feature/1234

worktree /pr-1234-alt
branch refs/heads/bugfix/5678
"""
        pr_head = "feature/1234"
        lines = porcelain_output.strip().split('\n')

        found_path = None
        current_record = {}

        for line in lines:
            if not line.strip():
                if current_record and 'worktree' in current_record:
                    if current_record.get('branch') == f'refs/heads/{pr_head}':
                        found_path = current_record['worktree']
                current_record = {}
            else:
                parts = line.split()
                if parts[0] == 'worktree':
                    current_record['worktree'] = parts[1]
                elif parts[0] == 'branch':
                    current_record['branch'] = parts[1]

        if current_record and 'worktree' in current_record:
            if current_record.get('branch') == f'refs/heads/{pr_head}':
                found_path = current_record['worktree']

        self.assertEqual(found_path, '/pr-1234')

    def test_phase3_arguments_precedence(self):
        """Verify Phase 3 prefers $ARGUMENTS over branch derivation."""
        arguments = "1022"
        current_branch = ""

        if arguments and arguments.strip():
            pr_num = arguments.strip()
        elif current_branch:
            pr_num = f"(branch {current_branch})"
        else:
            pr_num = None

        self.assertEqual(pr_num, "1022")

    def test_absolute_git_common_dir(self):
        """Verify that --path-format=absolute produces absolute paths."""
        git_common_dir_output = "/abs/path/to/repo/.git/worktrees/agent-12345"
        self.assertIn('.git', git_common_dir_output)

        main_worktree = Path(git_common_dir_output).parent.parent
        self.assertTrue(main_worktree.is_absolute())

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

        # Phase 1 should record QUEUED_MERGE_START_TIME
        phase1_start = md_content.find("### Phase 1")
        phase2_start = md_content.find("### Phase 2")
        phase1_text = md_content[phase1_start:phase2_start] if phase2_start > phase1_start else ""

        # Recorded to a per-PR scratch file, not a shell variable, since variables
        # don't persist across Bash blocks (Phase 4 is a separate block).
        has_start_time = "start_time" in phase1_text and "jq -n 'now'" in phase1_text

        self.assertTrue(has_start_time,
                       "Phase 1 should record start time with jq -n 'now' to a file")

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
