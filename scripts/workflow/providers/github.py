"""
GitHub provider implementation for tracking and mutation operations.

Wraps gh commands via argv builders in scripts/workflow/git.py.
"""

import tempfile
from pathlib import Path
from typing import List, Optional, Union

from ..models import IssueInfo
from ..safety import Unknown
from .. import git, project


class GithubProvider:
    """GitHub implementation of the Provider protocol."""

    def __init__(self, cwd: Optional[Path] = None):
        """Initialize GitHub provider with optional working directory."""
        self.cwd = cwd

    def repo_identity(self) -> Optional[tuple]:
        """
        Return (owner, name) tuple or None if not available.

        Queries gh repo view and collapses the (value, Unknown) tuple to
        value or None per the Protocol contract.
        """
        identity, err = project.detect_repo_identity(cwd=self.cwd)
        return identity

    def current_user(self) -> Optional[str]:
        """
        Return current user login/identifier or None if not available.

        Queries gh api user and collapses the tuple to str or None.
        """
        user, err = project.detect_current_user(cwd=self.cwd)
        return user

    def list_open_issues(self) -> List[IssueInfo]:
        """
        List all open issues from GitHub.

        Queries gh issue list --state open and maps to IssueInfo list.
        Returns [] on any failure (no issues, not authenticated, etc.).
        """
        issues = git.issue_list_json(["number", "url", "title", "body", "state"], cwd=self.cwd)
        result = []
        for issue in issues:
            # Skip records missing required fields
            if "number" not in issue or not isinstance(issue["number"], int):
                continue
            result.append(IssueInfo(
                number=issue["number"],
                url=issue.get("url", ""),
                title=issue.get("title", ""),
                body=issue.get("body", ""),
                state=issue.get("state", "open")
            ))
        return result

    def create_issue(self, title: str, body: str, labels: List[str], assignee: Optional[str]) -> IssueInfo:
        """
        Create a GitHub issue.

        Writes body to a temp file, invokes gh issue create, and parses the returned URL
        to extract the issue number. Raises RuntimeError if the number cannot be determined.
        """
        # Write body to a temp file. body_file is assigned before the write, and a
        # write failure is caught and cleaned up immediately, so a write failure
        # can no longer leak the temp file on disk (it previously escaped before
        # body_file was even bound). The file is closed (exiting the `with`) before
        # invoking gh so its contents are flushed and visible to the subprocess.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            body_file = Path(f.name)
            try:
                f.write(body)
            except Exception:
                body_file.unlink(missing_ok=True)
                raise

        try:
            url, err = git.issue_create(
                title,
                body_file,
                assignee=assignee,
                labels=labels if labels else None,
                cwd=self.cwd
            )
            if err:
                raise RuntimeError(str(err))
            if not url:
                raise RuntimeError("gh issue create returned empty URL")

            # Parse issue number from URL (trailing segment after last /)
            trailing = url.rstrip("/").split("/")[-1]
            try:
                issue_number = int(trailing)
            except ValueError:
                raise RuntimeError(f"could not parse issue number from URL: {url}")

            return IssueInfo(
                number=issue_number,
                url=url,
                title=title,
                body=body,
                state="open"
            )
        finally:
            body_file.unlink(missing_ok=True)

    def comment_issue(self, number: Union[int, str], body: str) -> None:
        """
        Add a comment to a GitHub issue.

        Writes body to a temp file and invokes gh issue comment.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            body_file = Path(f.name)
            try:
                f.write(body)
            except Exception:
                body_file.unlink(missing_ok=True)
                raise

        try:
            success, err = git.issue_comment(number, body_file, cwd=self.cwd)
            if err:
                raise RuntimeError(str(err))
            if not success:
                raise RuntimeError(f"failed to comment on issue {number}")
        finally:
            body_file.unlink(missing_ok=True)

    def edit_issue_body(self, number: Union[int, str], body: str) -> None:
        """
        Replace a GitHub issue's body.

        Writes body to a temp file and invokes gh issue edit.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            body_file = Path(f.name)
            try:
                f.write(body)
            except Exception:
                body_file.unlink(missing_ok=True)
                raise

        try:
            success, err = git.issue_edit_body(number, body_file, cwd=self.cwd)
            if err:
                raise RuntimeError(str(err))
            if not success:
                raise RuntimeError(f"failed to edit issue {number}")
        finally:
            body_file.unlink(missing_ok=True)
