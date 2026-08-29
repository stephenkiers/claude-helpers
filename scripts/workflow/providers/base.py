"""
Provider contract (Protocol) for GitHub and local implementations.

Phase 1: Read-only methods only (repo identity, current user).
Phase 3a: Mutation methods for issue creation and worktree tracking.

Ensures cli.py never special-cases GitHub vs. local — both implementations
validate against the same contract.
"""

from typing import Optional, Tuple, Protocol, Union, List
from ..models import IssueInfo


class Provider(Protocol):
    """
    Provider contract for repository operations.

    All implementations (GitHub, local, external) must implement these methods
    in order to be swappable at the cli.py level.
    """

    def repo_identity(self) -> Optional[Tuple[str, str]]:
        """
        Return (owner, name) tuple or None if not available.

        GitHub: queries gh repo view
        Local: returns None (no GitHub identity)
        External: normalized external tracker identity
        """
        ...

    def current_user(self) -> Optional[str]:
        """
        Return current user login/identifier or None if not available.

        GitHub: queries gh api user -q '.login'
        Local: returns None (no user concept)
        External: depends on tracker authentication
        """
        ...

    def list_open_issues(self) -> List[IssueInfo]:
        """
        List all open issues.

        GitHub: queries gh issue list --state open
        Local: reads from project-root array-format issues.json, filters by status "todo" or "planned"
        External: depends on tracker API

        Must never raise for ordinary "no issues / no remote / gh not authenticated" cases.
        Returns [] instead. Other infrastructure failures (file read errors, etc.) may be
        surfaced to the caller, but preferred behavior is [] to allow workflow continuation.
        """
        ...

    def create_issue(self, title: str, body: str, labels: List[str], assignee: Optional[str]) -> IssueInfo:
        """
        Create a new issue.

        GitHub: runs gh issue create with title, body (via --body-file), labels, and assignee
        Local: allocates new ID, writes plan file, appends to project-root issues.json
        External: delegates to tracker API

        Args:
            title: Issue title
            body: Issue body/description
            labels: List of label names (may be empty)
            assignee: Assignee login or "@me"; may be None

        Returns:
            IssueInfo with number, url, title, body, and state="open"

        Raises:
            RuntimeError: if mutation is not allowed or critical operation fails
                (e.g., issue created but plan file write failed, leaving a half-state)
        """
        ...

    def comment_issue(self, number: Union[int, str], body: str) -> None:
        """
        Add a comment to an issue.

        GitHub: runs gh issue comment <number> --body <body>
        Local: documented no-op (local mode has no separate comment concept)
        External: depends on tracker API

        Args:
            number: Issue number (int) or string ID for trackers using string IDs (e.g., "PPS-166")
            body: Comment text

        Raises:
            RuntimeError: if mutation is not allowed or command fails
        """
        ...

    def edit_issue_body(self, number: Union[int, str], body: str) -> None:
        """
        Replace an issue's body.

        GitHub: runs gh issue edit <number> --body <body>
        Local: rewrites the entry's plan file (plan path from entry metadata)
        External: depends on tracker API

        Args:
            number: Issue number (int) or string ID for trackers using string IDs
            body: New body text (replaces existing body entirely)

        Raises:
            RuntimeError: if mutation is not allowed or command fails
        """
        ...
