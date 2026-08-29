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
    # None means "stack key absent from the cache file" — distinct from a
    # confirmed, explicit StackInfo(is_stacked=False). Collapsing the two
    # made every reader treat "we don't know" as "confirmed not stacked".
    stack: Optional[StackInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.issue:
            d["issue"] = asdict(self.issue)
        if self.stack is not None:
            d["stack"] = {
                "isStacked": self.stack.is_stacked,
                "parentBranch": self.stack.parent_branch,
                "parentPr": self.stack.parent_pr
            }
        else:
            d["stack"] = None
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
        else:
            obj.stack = None
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
    # Keys from the source dict that failed to parse into a LocalIssueEntry
    # (malformed id or entry shape). Not part of to_dict()'s JSON output —
    # read_issues_cache() surfaces this as an Unknown so a silent drop isn't
    # indistinguishable from a fully-clean read.
    dropped_keys: List[str] = field(default_factory=list)

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
                obj.dropped_keys.append(str(key))
        return obj


@dataclass
class RepoCacheData:
    """Schema for .claude/repo-cache.json."""
    schema_version: str = REPO_CACHE_SCHEMA_VERSION
    repo_path: str = ""
    worktree_parent: str = ""
    # /shipit writes one entry per check type (format/lint/check/vet/typecheck/test/build);
    # a value of None means "not applicable to this project" (e.g. typecheck: null for Go) —
    # distinct from the key being absent entirely, which the .get(...) call sites below
    # already treat identically (both skip the command), so from_dict need not distinguish them.
    commands: Dict[str, Optional[str]] = field(default_factory=dict)
    # List of command keys that can be parallelized (typically lint, vet); defaults to []
    parallelizable: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepoCacheData":
        """Construct from parsed JSON dict."""
        # /shipit's real cache (commands/shipit.md) writes a top-level "version" int, never
        # "schema_version" — accept whichever key is present rather than losing the field.
        version = data.get("schema_version", data.get("version", REPO_CACHE_SCHEMA_VERSION))
        parallelizable = data.get("parallelizable", [])
        if not isinstance(parallelizable, list):
            parallelizable = []
        return cls(
            schema_version=str(version),
            repo_path=data.get("repo_path", ""),
            worktree_parent=data.get("worktree_parent", ""),
            commands=dict(data.get("commands") or {}),
            parallelizable=parallelizable
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
    """
    Validate repo-cache.json structure and required fields.

    Unlike github-cache.json/issues.json, this file's real-world writer (/shipit, per
    commands/shipit.md) uses a top-level "version" int and never writes "schema_version" —
    gating on an exact "schema_version" string this reader predates would reject every real
    file. Structural validation only: a dict, with "commands" a dict if present.

    Minimal forward-compat protection: version/schema_version field, if present, must be
    int or str (rejects nonsensical shapes like list or dict in that slot).
    """
    if not isinstance(data, dict):
        return False
    # Check version field type (minimal forward-compat protection)
    version = data.get("schema_version", data.get("version"))
    if version is not None and not isinstance(version, (str, int)):
        return False
    if "commands" in data and not isinstance(data["commands"], dict):
        return False
    # Validate commands dict values: must be None or str
    if "commands" in data and isinstance(data["commands"], dict):
        if not all(v is None or isinstance(v, str) for v in data["commands"].values()):
            return False
    return True


@dataclass
class LocalTrackerEntry:
    """One entry in the project-root array-format issues.json (Local Plan Mode)."""
    id: int
    title: str
    status: str = "todo"
    plan: Optional[str] = None


@dataclass
class LocalTrackerData:
    """Schema for project-root issues.json — array format, distinct from the
    dict-keyed worktree-parent IssuesCacheData.

    This format is a plain top-level array on disk: [{"id": 1, "title": "...", "status": "todo", "plan": "plans/1-foo.md"}, ...]

    It has no schema_version wrapper, unlike IssuesCacheData which lives in a worktree-parent
    and is dict-keyed by issue number. The two formats exist at different paths (project-root
    issues.json vs. worktree-parent issues.json) and serve different consumers, so they are
    deliberately kept separate.
    """
    entries: List[LocalTrackerEntry] = field(default_factory=list)
    # Indices/values that could not parse into a LocalTrackerEntry. Not part of to_dict()'s output —
    # read_local_tracker surfaces this as an Unknown so a silent drop is distinguishable from a clean read.
    dropped_entries: List[str] = field(default_factory=list)

    def to_dict(self) -> List[Dict[str, Any]]:
        """Convert to list of dicts for JSON serialization (top-level array, no wrapper)."""
        result = []
        for entry in self.entries:
            d = {
                "id": entry.id,
                "title": entry.title,
                "status": entry.status
            }
            if entry.plan is not None:
                d["plan"] = entry.plan
            result.append(d)
        return result

    @classmethod
    def from_dict(cls, data: List[Dict[str, Any]]) -> "LocalTrackerData":
        """Construct from parsed JSON list (top-level array).

        Round-tripping is lossless for well-formed data: LocalTrackerData.from_dict(d).to_dict() == d
        when every entry has exactly the known keys. Entries missing the optional `plan` key round-trip
        with `plan: None` present in the output.
        """
        obj = cls()
        if not isinstance(data, list):
            return obj
        for idx, entry_data in enumerate(data):
            try:
                if not isinstance(entry_data, dict):
                    obj.dropped_entries.append(str(idx))
                    continue
                entry_id = entry_data.get("id")
                title = entry_data.get("title")
                if not isinstance(entry_id, int) or isinstance(entry_id, bool):
                    obj.dropped_entries.append(str(idx))
                    continue
                if not isinstance(title, str):
                    obj.dropped_entries.append(str(idx))
                    continue
                status = entry_data.get("status", "todo")
                if not isinstance(status, str):
                    obj.dropped_entries.append(str(idx))
                    continue
                plan = entry_data.get("plan")
                obj.entries.append(LocalTrackerEntry(
                    id=entry_id,
                    title=title,
                    status=status,
                    plan=plan
                ))
            except (TypeError, ValueError):
                obj.dropped_entries.append(str(idx))
        return obj


def validate_local_tracker_data(data: Any) -> bool:
    """Validate project-root array-format issues.json.

    Returns True only if `data` is a `list` and every element is a dict with an `id` that is
    an `int` (and not a `bool`), a `title` that is a `str`, and a `status` that is a `str`.
    An empty list is valid.
    """
    if not isinstance(data, list):
        return False
    for entry in data:
        if not isinstance(entry, dict):
            return False
        entry_id = entry.get("id")
        if not isinstance(entry_id, int) or isinstance(entry_id, bool):
            return False
        if not isinstance(entry.get("title"), str):
            return False
        if not isinstance(entry.get("status"), str):
            return False
    return True
