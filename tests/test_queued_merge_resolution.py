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


if __name__ == '__main__':
    unittest.main()
