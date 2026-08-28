"""
Shared git fixture helper for integration tests.

Provides real git init setup with worktrees for testing mutation operations.
"""

import os
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple


class GitFixture:
    """Real git repository fixture for integration testing."""

    def __init__(self, tmpdir: Optional[Path] = None):
        """
        Initialize a real git repository in a temp directory.

        Args:
            tmpdir: Use this directory instead of creating a temp one.
                   Must be empty or not exist.
        """
        if tmpdir is None:
            self.tmpdir = Path(tempfile.mkdtemp())
        else:
            self.tmpdir = Path(tmpdir)
            self.tmpdir.mkdir(parents=True, exist_ok=True)

        self.repo_root = self.tmpdir / "repo"
        self.repo_root.mkdir(exist_ok=True)
        self.worktree_parent = self.tmpdir / "worktrees"

        self._run_git(["init", "-b", "main"], cwd=self.repo_root)
        self._run_git(["config", "user.email", "test@example.com"], cwd=self.repo_root)
        self._run_git(["config", "user.name", "Test User"], cwd=self.repo_root)

        self.main_worktree = self.repo_root

    def _run_git(self, args, cwd=None, check=True):
        """Run a git command and return stdout."""
        try:
            result = subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"] + args,
                cwd=cwd or self.repo_root,
                capture_output=True,
                text=True,
                check=check
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git command failed: {e.stderr}")

    def create_initial_commit(self, message: str = "Initial commit") -> str:
        """Create an initial commit in main worktree. Returns HEAD SHA."""
        test_file = self.main_worktree / "README.md"
        test_file.write_text("# Test Repo\n")
        self._run_git(["add", "README.md"], cwd=self.main_worktree)
        self._run_git(["commit", "-m", message], cwd=self.main_worktree)
        sha = self._run_git(["rev-parse", "HEAD"], cwd=self.main_worktree)
        return sha

    def create_branch(self, branch_name: str) -> None:
        """Create a new branch from current HEAD."""
        self._run_git(["checkout", "-b", branch_name], cwd=self.main_worktree)
        self._run_git(["checkout", "main"], cwd=self.main_worktree)

    def create_worktree(self, branch_name: str) -> Path:
        """
        Create a new worktree for the given branch.

        Worktree directory name is created by replacing '/' with '-' in branch name
        to avoid nested directories. Returns the worktree path.
        """
        self.worktree_parent.mkdir(parents=True, exist_ok=True)
        wt_dirname = branch_name.replace("/", "-")
        wt_path = self.worktree_parent / wt_dirname
        self._run_git(
            ["worktree", "add", str(wt_path), branch_name],
            cwd=self.repo_root
        )
        return wt_path

    def commit_in_worktree(self, wt_path: Path, message: str) -> str:
        """Commit a file change in a worktree. Returns HEAD SHA."""
        test_file = wt_path / "test.txt"
        test_file.write_text(f"content: {message}\n")
        self._run_git(["add", "test.txt"], cwd=wt_path)
        self._run_git(["commit", "-m", message], cwd=wt_path)
        sha = self._run_git(["rev-parse", "HEAD"], cwd=wt_path)
        return sha

    def get_current_branch(self, cwd: Optional[Path] = None) -> str:
        """Get the current branch in a worktree."""
        return self._run_git(["branch", "--show-current"], cwd=cwd or self.main_worktree)

    def get_head_sha(self, cwd: Optional[Path] = None) -> str:
        """Get HEAD SHA."""
        return self._run_git(["rev-parse", "HEAD"], cwd=cwd or self.main_worktree)

    def worktree_exists(self, wt_path: Path) -> bool:
        """Check if a worktree exists."""
        porcelain = self._run_git(["worktree", "list", "--porcelain"], cwd=self.repo_root)
        return f"worktree {wt_path}" in porcelain

    def branch_exists(self, branch_name: str) -> bool:
        """Check if a branch exists."""
        try:
            self._run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], cwd=self.repo_root)
            return True
        except RuntimeError:
            return False

    def write_cache_file(self, wt_path: Path, cache_data: dict) -> Path:
        """Write a .claude/github-cache.json file to a worktree."""
        claude_dir = wt_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        cache_file = claude_dir / "github-cache.json"
        cache_file.write_text(json.dumps(cache_data))
        return cache_file

    def read_cache_file(self, wt_path: Path) -> Optional[dict]:
        """Read .claude/github-cache.json from a worktree."""
        cache_file = wt_path / ".claude" / "github-cache.json"
        if not cache_file.exists():
            return None
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, IOError):
            return None

    def cleanup(self):
        """Clean up the temporary directory."""
        import shutil
        if self.tmpdir.exists():
            shutil.rmtree(self.tmpdir)
