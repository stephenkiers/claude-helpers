"""
Provider contract (Protocol) for GitHub and local implementations.

Phase 1: Read-only methods only (repo identity, current user).
GitHub and local implementations come in Phase 2+.

Ensures cli.py never special-cases GitHub vs. local — both implementations
validate against the same contract.
"""

from typing import Optional, Tuple, Protocol


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
