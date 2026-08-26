"""
Schemas for workflow cache files and state objects.

Schema versioning allows safe migration of cache formats; each cache validates
its own schema_version and content structure.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
import json


GITHUB_CACHE_SCHEMA_VERSION = "1.0"
REPO_CACHE_SCHEMA_VERSION = "1.0"
ISSUES_CACHE_SCHEMA_VERSION = "1.0"


@dataclass
class IssueInfo:
    """GitHub issue metadata cached from gh api."""
    number: int
    url: str
    title: str
    body: str
    state: str = "open"


@dataclass
class StackInfo:
    """Stack (parent-branch) metadata for the current branch."""
    is_stacked: bool = False
    parent_branch: Optional[str] = None
    parent_pr: Optional[int] = None


@dataclass
class GitHubCacheData:
    """Schema for .claude/github-cache.json."""
    schema_version: str = GITHUB_CACHE_SCHEMA_VERSION
    branch: str = ""
    issue: Optional[IssueInfo] = None
    stack: StackInfo = field(default_factory=StackInfo)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.issue:
            d["issue"] = asdict(self.issue)
        d["stack"] = {
            "isStacked": self.stack.is_stacked,
            "parentBranch": self.stack.parent_branch,
            "parentPr": self.stack.parent_pr
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitHubCacheData":
        """Construct from parsed JSON dict."""
        obj = cls(schema_version=data.get("schema_version", GITHUB_CACHE_SCHEMA_VERSION))
        obj.branch = data.get("branch", "")
        if "issue" in data and data["issue"]:
            obj.issue = IssueInfo(**data["issue"])
        if "stack" in data and data["stack"]:
            stack_data = data["stack"]
            obj.stack = StackInfo(
                is_stacked=stack_data.get("isStacked", False),
                parent_branch=stack_data.get("parentBranch"),
                parent_pr=stack_data.get("parentPr")
            )
        return obj


@dataclass
class LocalIssueEntry:
    """One entry in local issues.json cache."""
    id: int
    title: str
    body: str = ""
    status: str = "open"


@dataclass
class IssuesCacheData:
    """Schema for local issues.json (worktree-parent level)."""
    schema_version: str = ISSUES_CACHE_SCHEMA_VERSION
    next_id: int = 1
    issues: Dict[int, LocalIssueEntry] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "next_id": self.next_id,
            "issues": {str(k): asdict(v) for k, v in self.issues.items()}
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IssuesCacheData":
        """Construct from parsed JSON dict."""
        obj = cls(schema_version=data.get("schema_version", ISSUES_CACHE_SCHEMA_VERSION))
        obj.next_id = data.get("next_id", 1)
        issues_dict = data.get("issues", {})
        for key, entry_data in issues_dict.items():
            try:
                issue_id = int(key)
                obj.issues[issue_id] = LocalIssueEntry(**entry_data)
            except (ValueError, TypeError):
                pass
        return obj


@dataclass
class RepoCacheData:
    """Schema for .claude/repo-cache.json (future use; stubbed for Phase 1)."""
    schema_version: str = REPO_CACHE_SCHEMA_VERSION
    repo_path: str = ""
    worktree_parent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepoCacheData":
        """Construct from parsed JSON dict."""
        return cls(
            schema_version=data.get("schema_version", REPO_CACHE_SCHEMA_VERSION),
            repo_path=data.get("repo_path", ""),
            worktree_parent=data.get("worktree_parent", "")
        )


def validate_github_cache(data: Dict[str, Any]) -> bool:
    """Validate github-cache.json structure and required fields."""
    if not isinstance(data, dict):
        return False
    version = data.get("schema_version")
    if version != GITHUB_CACHE_SCHEMA_VERSION:
        return False
    if not isinstance(data.get("branch"), str):
        return False
    return True


def validate_issues_cache(data: Dict[str, Any]) -> bool:
    """Validate issues.json structure and required fields."""
    if not isinstance(data, dict):
        return False
    version = data.get("schema_version")
    if version != ISSUES_CACHE_SCHEMA_VERSION:
        return False
    if not isinstance(data.get("next_id"), int) or data.get("next_id") < 1:
        return False
    if not isinstance(data.get("issues"), dict):
        return False
    return True


def validate_repo_cache(data: Dict[str, Any]) -> bool:
    """Validate repo-cache.json structure and required fields."""
    if not isinstance(data, dict):
        return False
    version = data.get("schema_version")
    if version != REPO_CACHE_SCHEMA_VERSION:
        return False
    return True
