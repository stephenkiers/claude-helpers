"""
Cache reading, validation, hashing, and freshness checks.

Phase 1: read-only only. Write/locking/atomic-rename in Phase 2+.
"""

import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from .models import (
    GitHubCacheData, IssuesCacheData, RepoCacheData,
    validate_github_cache, validate_issues_cache, validate_repo_cache
)
from .safety import Unknown


def read_github_cache(path: Path) -> Tuple[Optional[GitHubCacheData], Optional[Unknown]]:
    """
    Read and validate .claude/github-cache.json.

    Returns (GitHubCacheData or None, Unknown error or None).
    """
    if not path.exists():
        return None, Unknown("github-cache.json not found")

    try:
        data = json.loads(path.read_text())
        if not validate_github_cache(data):
            return None, Unknown("github-cache.json schema validation failed")
        return GitHubCacheData.from_dict(data), None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read github-cache.json: {e}")


def read_issues_cache(path: Path) -> Tuple[Optional[IssuesCacheData], Optional[Unknown]]:
    """
    Read and validate local issues.json.

    Returns (IssuesCacheData or None, Unknown error or None).
    """
    if not path.exists():
        return None, Unknown("issues.json not found")

    try:
        data = json.loads(path.read_text())
        if not validate_issues_cache(data):
            return None, Unknown("issues.json schema validation failed")
        return IssuesCacheData.from_dict(data), None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read issues.json: {e}")


def read_repo_cache(path: Path) -> Tuple[Optional[RepoCacheData], Optional[Unknown]]:
    """
    Read and validate .claude/repo-cache.json.

    Returns (RepoCacheData or None, Unknown error or None).
    """
    if not path.exists():
        return None, None

    try:
        data = json.loads(path.read_text())
        if not validate_repo_cache(data):
            return None, Unknown("repo-cache.json schema validation failed")
        return RepoCacheData.from_dict(data), None
    except (json.JSONDecodeError, OSError) as e:
        return None, Unknown(f"Failed to read repo-cache.json: {e}")


def hash_file_content(content: str) -> str:
    """Compute SHA256 hash of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def hash_github_cache_file(path: Path) -> Optional[str]:
    """Compute content hash of github-cache.json if it exists."""
    if not path.exists():
        return None
    try:
        content = path.read_text()
        return hash_file_content(content)
    except OSError:
        return None


def hash_issues_cache_file(path: Path) -> Optional[str]:
    """Compute content hash of issues.json if it exists."""
    if not path.exists():
        return None
    try:
        content = path.read_text()
        return hash_file_content(content)
    except OSError:
        return None


def write_cache(path: Path, data: Dict[str, Any]) -> Tuple[bool, Optional[Unknown]]:
    """
    Write cache file atomically (stub for Phase 1).

    Decision 1: temp-file + os.replace() atomic rename, plus sidecar lock file.
    TODO: Implement actual write path in Phase 2 when mutations begin.
    """
    return False, Unknown("Cache write not implemented in Phase 1")
