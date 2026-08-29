"""
Provider implementations (GitHub, local, external trackers).

Phase 3a: GithubProvider and LocalProvider implementations for /track-and-start.
Phase 3b+: External tracker support (Tracker Ticket mode).
"""

from .github import GithubProvider
from .local import LocalProvider

__all__ = ["GithubProvider", "LocalProvider"]
