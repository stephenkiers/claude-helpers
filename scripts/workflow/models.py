"""
Schemas for workflow cache files and state objects.

Schema versioning allows safe migration of cache formats; each cache validates
its own schema_version and content structure.
"""

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List, Literal, TypedDict, Union, NoReturn, Tuple, Mapping, get_args


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
    """Schema for .claude/github-cache.json.

    NOTE: This cache also contains a 'review' key (a dict with branch, commit, lastRun, etc.)
    written by scripts/write-review-cache.py and read by scripts/expert-review-status.py. Both
    scripts have their own hand-rolled parsing (avoiding a scripts/workflow/ dependency).
    Future schema changes to the 'review' key must be applied to both of those scripts as well.
    """
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


def valid_timeout_secs(value: Any) -> Optional[int]:
    """
    Return value if it is a positive integer, otherwise None. Never raises.

    This is the one validity rule for every checkTimeoutSecs source: project.yaml, repo-cache.json,
    and SHIPIT_CHECK_TIMEOUT_SECS (after int() parsing). Accepted: an int greater than zero.
    Ignored (callers fall through to the next source, then the default timeout): bool
    (an int subclass), strings such as "900", floats, lists, None, zero, and negative integers.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


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
    # Advisory metadata; not read by run_checks. Kept on-disk only to preserve cache round-tripping.
    parallelizable: List[str] = field(default_factory=list)
    # Per-repo override for the per-check timeout (seconds), for projects whose suite outlasts the
    # 300s default. None = no override. Written by hand/detection as "checkTimeoutSecs".
    check_timeout_secs: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization.

        NOTE: Explicitly maps check_timeout_secs to "checkTimeoutSecs" (camelCase) for
        round-trip consistency with from_dict(), which reads the "checkTimeoutSecs" key
        from JSON. asdict() alone would produce "check_timeout_secs" (snake_case), breaking
        the round-trip: to_dict(from_dict(data)) would lose the value if data was read back.
        """
        d = asdict(self)
        # Map the snake_case field key to camelCase JSON key for round-trip consistency.
        if "check_timeout_secs" in d:
            check_timeout = d.pop("check_timeout_secs")
            if check_timeout is not None:
                d["checkTimeoutSecs"] = check_timeout
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepoCacheData":
        """Construct from parsed JSON dict."""
        # /shipit's real cache (commands/shipit.md) writes a top-level "version" int, never
        # "schema_version" — accept whichever key is present rather than losing the field.
        version = data.get("schema_version", data.get("version", REPO_CACHE_SCHEMA_VERSION))
        parallelizable = data.get("parallelizable", [])
        if not isinstance(parallelizable, list):
            parallelizable = []
        # Invalid values (bool, string, float, zero, negative) become None, the same ignore rule
        # resolve_check_timeout applies to every checkTimeoutSecs source.
        check_timeout_secs = valid_timeout_secs(data.get("checkTimeoutSecs"))
        return cls(
            schema_version=str(version),
            repo_path=data.get("repo_path", ""),
            worktree_parent=data.get("worktree_parent", ""),
            commands=dict(data.get("commands") or {}),
            parallelizable=parallelizable,
            check_timeout_secs=check_timeout_secs
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
    next_id = data.get("next_id")
    if not isinstance(next_id, int) or next_id < 1:
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

        Round-tripping is lossless for well-formed data: LocalTrackerData.from_dict(d).to_dict() == d,
        for both 3-key entries (no `plan` key) and 4-key entries (`plan` present). to_dict() omits
        the `plan` key entirely when entry.plan is None, so a 3-key input round-trips back to a
        3-key output rather than gaining a `plan: None` key it never had.
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


# Issue comments types (Step 2 of plan)
CommentTrust = Literal["trusted", "unverified"]
CommentsStatus = Literal["ok", "unavailable", "unsupported"]
CommentSource = Literal["issue"]
CommentKind = Literal["ordinary", "implementation_plan"]
DropReason = Literal["outdated", "superseded_plan_content", "off_topic", "duplicate", "low_signal", "instruction_like"]


class RelevanceFilterDict(TypedDict, total=False):
    """TypedDict for relevance_filter field: required (bool), threshold (int), batches (List[List[str]])."""
    required: bool
    threshold: int
    batches: List[List[str]]


def validate_relevance_filter(data: Any) -> RelevanceFilterDict:
    """Validate a relevance_filter mapping. Ignores unknown keys (same policy as the other from_dict methods).

    Raises ValueError if a known key has the wrong type.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Invalid relevance_filter: {data!r}")
    result: RelevanceFilterDict = {}
    if "required" in data:
        if not isinstance(data["required"], bool):
            raise ValueError(f"Invalid relevance_filter.required: {data['required']!r}")
        result["required"] = data["required"]
    if "threshold" in data:
        threshold = data["threshold"]
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            raise ValueError(f"Invalid relevance_filter.threshold: {threshold!r}")
        result["threshold"] = threshold
    if "batches" in data:
        batches = data["batches"]
        if not isinstance(batches, list) or not all(
            isinstance(batch, list) and all(isinstance(i, str) for i in batch) for batch in batches
        ):
            raise ValueError("Invalid relevance_filter.batches: must be a list of lists of strings")
        result["batches"] = [list(batch) for batch in batches]
    return result


@dataclass
class IssueComment:
    """A single issue comment with trust, kind, and acknowledged status."""
    id: str  # GraphQL node id (string)
    author_login: Optional[str]
    author_association: Optional[str]
    trust: CommentTrust
    created_at: str  # ISO 8601 UTC
    is_minimized: bool
    kind: CommentKind
    body: str
    acknowledged: bool
    source: CommentSource = "issue"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IssueComment":
        """Construct from parsed JSON dict. Ignores unknown keys. Validates Literal fields."""
        # Extract only known fields, ignoring unknowns
        known_fields = {
            "id", "author_login", "author_association", "trust", "created_at",
            "is_minimized", "kind", "body", "acknowledged", "source"
        }
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        # Validate Literal fields if present
        if "trust" in filtered_data and filtered_data["trust"] not in ("trusted", "unverified"):
            raise ValueError(f"Invalid trust value: {filtered_data['trust']}")
        if "kind" in filtered_data and filtered_data["kind"] not in ("ordinary", "implementation_plan"):
            raise ValueError(f"Invalid kind value: {filtered_data['kind']}")
        if "source" in filtered_data and filtered_data["source"] not in ("issue",):
            raise ValueError(f"Invalid source value: {filtered_data['source']}")

        return cls(**filtered_data)


@dataclass
class CommentCounts:
    """Counters for comment drops and truncation."""
    dropped_minimized: int = 0
    dropped_malformed: int = 0
    dropped_superseded: int = 0
    dropped_pre_watermark: int = 0
    dropped_pre_plan: int = 0
    dropped_stale_implementation_plan: int = 0
    dropped_relevance: int = 0
    dropped_scout_invalid: int = 0
    truncated_trusted: int = 0
    truncated_unverified: int = 0
    withheld_unverified: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CommentCounts":
        """Construct from parsed JSON dict."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class IssueWithComments:
    """Issue body plus fetched comments with filtering metadata."""
    number: int
    title: str
    url: str
    state: str
    labels: List[str]
    body: str
    created_at: str
    fetched_at: str  # UTC time taken immediately BEFORE the gh call
    comments: List[IssueComment]
    comments_status: CommentsStatus
    watermark: Optional[str]  # ISO 8601 UTC of newest trusted superseded-plan comment
    lower_bound_source: Literal["marker_watermark", "plan_fetch"]
    counts: CommentCounts
    truncated: bool
    withheld_unverified_authors: List[str]
    relevance_filter: RelevanceFilterDict  # {"required": bool, "threshold": int, "batches": [[id, ...], ...]}
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d["counts"] = self.counts.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IssueWithComments":
        """Construct from parsed JSON dict. Validates Literal fields, schema_version and relevance_filter.

        Unknown keys are ignored, matching IssueComment, CommentCounts and ScoutVerdict.
        """
        # Validate schema_version: exactly the version this code writes (bool is not an int here)
        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError(f"Invalid schema_version: {schema_version!r}")

        relevance_filter = validate_relevance_filter(data.get("relevance_filter", {}))

        # Validate comments_status Literal
        comments_status = data.get("comments_status", "unavailable")
        if comments_status not in ("ok", "unavailable", "unsupported"):
            raise ValueError(f"Invalid comments_status: {comments_status}")

        # Validate lower_bound_source Literal
        lower_bound_source = data.get("lower_bound_source", "marker_watermark")
        if lower_bound_source not in ("marker_watermark", "plan_fetch"):
            raise ValueError(f"Invalid lower_bound_source: {lower_bound_source}")

        comments_data = data.get("comments", [])
        comments = [IssueComment.from_dict(c) for c in comments_data]
        counts_data = data.get("counts", {})
        counts = CommentCounts.from_dict(counts_data)
        return cls(
            number=data.get("number", 0),
            title=data.get("title", ""),
            url=data.get("url", ""),
            state=data.get("state", "open"),
            labels=data.get("labels", []),
            body=data.get("body", ""),
            created_at=data.get("created_at", ""),
            fetched_at=data.get("fetched_at", ""),
            comments=comments,
            comments_status=comments_status,
            watermark=data.get("watermark"),
            lower_bound_source=lower_bound_source,
            counts=counts,
            truncated=data.get("truncated", False),
            withheld_unverified_authors=data.get("withheld_unverified_authors", []),
            relevance_filter=relevance_filter,
            schema_version=schema_version
        )


@dataclass
class ScoutVerdict:
    """A scout's verdict on a single comment."""
    comment_id: str
    verdict: Literal["include", "drop"]
    reason: Optional[DropReason] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScoutVerdict":
        """Construct from parsed JSON dict. Ignores unknown keys; validates verdict and reason."""
        if not isinstance(data, dict):
            raise ValueError(f"Invalid scout verdict: {data!r}")
        comment_id = data.get("comment_id")
        if not isinstance(comment_id, str):
            raise ValueError(f"Invalid scout verdict comment_id: {comment_id!r}")
        verdict = data.get("verdict")
        if verdict not in ("include", "drop"):
            raise ValueError(f"Invalid scout verdict value: {verdict!r}")
        reason = data.get("reason")
        if reason is not None and reason not in (
            "outdated", "superseded_plan_content", "off_topic", "duplicate", "low_signal", "instruction_like"
        ):
            raise ValueError(f"Invalid scout verdict reason: {reason!r}")
        return cls(comment_id=comment_id, verdict=verdict, reason=reason)


# Check modes and records (ADR-0013 Amendment 11, Step 4 of plan)

Mode = Literal["minimum", "full"]
MODES: Tuple[Mode, ...] = get_args(Mode)  # ("minimum", "full")


def parse_mode(s: str) -> Optional[Mode]:
    """Parse a string to Mode. Returns None if not a valid mode."""
    if s in MODES:
        return s  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class FullKey:
    """Check record key for full-mode checks. tree and full_digest must not be empty."""
    tree: str  # 40-hex SHA
    full_digest: str  # 64-hex SHA256

    def __post_init__(self) -> None:
        """Validate hex fields."""
        if not re.fullmatch(r"[0-9a-f]{40}", self.tree):
            raise ValueError(f"Invalid tree (not 40-hex): {self.tree}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.full_digest):
            raise ValueError(f"Invalid full_digest (not 64-hex): {self.full_digest}")


@dataclass(frozen=True)
class MinimumKey:
    """Check record key for minimum-mode checks. All fields must not be empty."""
    tree: str  # 40-hex SHA
    full_digest: str  # 64-hex SHA256
    minimum_digest: str  # 64-hex SHA256
    changed_files_hash: str  # 64-hex SHA256

    def __post_init__(self) -> None:
        """Validate hex fields."""
        if not re.fullmatch(r"[0-9a-f]{40}", self.tree):
            raise ValueError(f"Invalid tree (not 40-hex): {self.tree}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.full_digest):
            raise ValueError(f"Invalid full_digest (not 64-hex): {self.full_digest}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.minimum_digest):
            raise ValueError(f"Invalid minimum_digest (not 64-hex): {self.minimum_digest}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.changed_files_hash):
            raise ValueError(f"Invalid changed_files_hash (not 64-hex): {self.changed_files_hash}")


CheckKey = Union[FullKey, MinimumKey]


def check_key_of(
    mode: Mode,
    tree: Optional[str],
    full_digest: str,
    minimum_digest: Optional[str] = None,
    changed_files_hash: Optional[str] = None
) -> Optional[CheckKey]:
    """
    Build a CheckKey for the given mode. Returns None if the tree is empty/missing
    or if minimum mode but changed_files_hash is missing.

    Pure builder; returns None when cache is impossible (no tree or no base).
    """
    if not tree:  # empty or None
        return None
    if mode == "full":
        try:
            return FullKey(tree=tree, full_digest=full_digest)
        except ValueError:
            return None
    elif mode == "minimum":
        if not changed_files_hash:
            return None
        try:
            return MinimumKey(
                tree=tree,
                full_digest=full_digest,
                minimum_digest=minimum_digest or "",  # Must be provided; validate will catch empty
                changed_files_hash=changed_files_hash
            )
        except ValueError:
            return None
    return None


def _canonical_key_serialisation(key: CheckKey) -> str:
    """
    Canonical serialisation of a CheckKey for hashing and integrity checks.

    Order is: mode:tree:digest1:digest2 (with mode-specific digest count).
    """
    if isinstance(key, FullKey):
        return f"full:{key.tree}:{key.full_digest}"
    elif isinstance(key, MinimumKey):
        return f"minimum:{key.tree}:{key.full_digest}:{key.minimum_digest}:{key.changed_files_hash}"
    else:
        _never(key)


def _never(x: NoReturn) -> NoReturn:
    """Helper for exhaustive type checks (Python 3.8 has no assert_never)."""
    raise AssertionError(f"Unhandled case: {x}")


def satisfies(record: "CheckRecord", requested: CheckKey) -> bool:
    """
    Check if a record satisfies a requested key.

    A full record satisfies a full or minimum request when tree and full_digest match.
    A minimum record satisfies only a minimum request when tree, full_digest, minimum_digest,
    and changed_files_hash all match. A minimum record never satisfies a full request.
    """
    if not isinstance(record, CheckRecord):
        return False

    record_key = record.key
    if isinstance(record_key, FullKey):
        if isinstance(requested, FullKey):
            return record_key.tree == requested.tree and record_key.full_digest == requested.full_digest
        elif isinstance(requested, MinimumKey):
            return record_key.tree == requested.tree and record_key.full_digest == requested.full_digest
        else:
            _never(requested)
    elif isinstance(record_key, MinimumKey):
        if isinstance(requested, FullKey):
            return False  # minimum never satisfies full
        elif isinstance(requested, MinimumKey):
            return (
                record_key.tree == requested.tree
                and record_key.full_digest == requested.full_digest
                and record_key.minimum_digest == requested.minimum_digest
                and record_key.changed_files_hash == requested.changed_files_hash
            )
        else:
            _never(requested)
    else:
        _never(record_key)


@dataclass
class CheckRecord:
    """One cache record for a check result, keyed by (tree, digests, changed_files_hash)."""
    schema_version: int  # Currently 1
    key: CheckKey  # Embedded for re-verification
    passed: bool  # True iff the check passed
    mode_executed: Mode  # Mode that was actually run ("full" or "minimum")
    written_at: float  # Unix timestamp when record was written

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        if isinstance(self.key, FullKey):
            key_dict = {
                "tree": self.key.tree,
                "full_digest": self.key.full_digest,
            }
        else:  # MinimumKey
            key_dict = {
                "tree": self.key.tree,
                "full_digest": self.key.full_digest,
                "minimum_digest": self.key.minimum_digest,
                "changed_files_hash": self.key.changed_files_hash,
            }
        return {
            "schema_version": self.schema_version,
            "key": key_dict,
            "passed": self.passed,
            "mode_executed": self.mode_executed,
            "written_at": self.written_at,
        }

    @staticmethod
    def from_dict(data: Mapping[str, object], filename_hash: Optional[str] = None) -> Optional["CheckRecord"]:
        """
        Parse a CheckRecord from a dict, fail-closed. Returns None on any validation failure.
        Never raises. The single parse boundary; see parse_check_record for the rules and reasons.
        """
        record, _reason = parse_check_record(data, filename_hash)
        return record


CHECK_RECORD_SCHEMA_VERSION = 1
_RECORD_KEYS = frozenset({"schema_version", "key", "passed", "mode_executed", "written_at"})
_FULL_KEY_FIELDS = frozenset({"tree", "full_digest"})
_MINIMUM_KEY_FIELDS = frozenset({"tree", "full_digest", "minimum_digest", "changed_files_hash"})


def record_filename_hash(key: CheckKey) -> str:
    """Record filename stem: sha256 of the canonical key serialisation (the one canonical form)."""
    return hashlib.sha256(_canonical_key_serialisation(key).encode()).hexdigest()


def parse_check_record(
    data: object, filename_hash: Optional[str] = None
) -> Tuple[Optional[CheckRecord], Optional[str]]:
    """
    Fail-closed parse of one record. Returns (record, None) or (None, reason); never raises.

    Rules (ADR-0013 Amendment 11, CP-W4): the exact top-level and key-level key sets; bool is never
    accepted as an int; passed must be exactly True; mode_executed must match the key type (a
    minimum record carries a MinimumKey, a full record a FullKey); when filename_hash is given the
    embedded key must re-hash to it.
    """
    if not isinstance(data, dict):
        return None, "record is not a JSON object"
    if set(data.keys()) != _RECORD_KEYS:
        return None, f"key set mismatch: {sorted(str(k) for k in data.keys())}"

    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != CHECK_RECORD_SCHEMA_VERSION:
        return None, f"schema_version is not {CHECK_RECORD_SCHEMA_VERSION}: {schema_version!r}"

    passed = data["passed"]
    if type(passed) is not bool or passed is not True:
        return None, f"passed is not exactly true: {passed!r}"

    mode_raw = data["mode_executed"]
    mode = parse_mode(mode_raw) if isinstance(mode_raw, str) else None
    if mode is None:
        return None, f"mode_executed is not a mode: {mode_raw!r}"

    written_at = data["written_at"]
    if type(written_at) not in (int, float):
        return None, f"written_at is not a number: {written_at!r}"

    key_data = data["key"]
    if not isinstance(key_data, dict):
        return None, "key is not a JSON object"
    key_fields = set(key_data.keys())
    if not all(type(v) is str for v in key_data.values()):
        return None, "key fields are not all strings"

    key: CheckKey
    try:
        if key_fields == _FULL_KEY_FIELDS:
            key = FullKey(tree=key_data["tree"], full_digest=key_data["full_digest"])
        elif key_fields == _MINIMUM_KEY_FIELDS:
            key = MinimumKey(
                tree=key_data["tree"],
                full_digest=key_data["full_digest"],
                minimum_digest=key_data["minimum_digest"],
                changed_files_hash=key_data["changed_files_hash"],
            )
        else:
            return None, f"key has unknown shape: {sorted(str(k) for k in key_fields)}"
    except ValueError as e:
        return None, f"key rejected: {e}"

    if isinstance(key, FullKey) and mode != "full":
        return None, "full-shaped key with mode_executed minimum"
    if isinstance(key, MinimumKey) and mode != "minimum":
        return None, "minimum-shaped key with mode_executed full"

    if filename_hash is not None and record_filename_hash(key) != filename_hash:
        return None, "embedded key does not hash to the record filename"

    return CheckRecord(
        schema_version=schema_version,
        key=key,
        passed=True,
        mode_executed=mode,
        written_at=written_at,
    ), None


class CheckStepResultDict(TypedDict, total=True):
    """One step result."""
    step: str
    passed: bool
    message: str


class SkippedCheckReasonDict(TypedDict, total=True):
    """One skipped check reason."""
    reason: str
    explanation: str


class CheckResultsDict(TypedDict, total=True):
    """Base TypedDict for CheckResults.to_dict()."""
    results: List[CheckStepResultDict]
    all_passed: bool
    failed_at: Optional[str]
    planned: List[str]
    executed: List[str]
    skipped: List[SkippedCheckReasonDict]
    status: str


class ValidateResult(CheckResultsDict, total=True):
    """Result of checks validate, extending CheckResultsDict with mode info."""
    schema_version: int
    mode_requested: Mode
    mode_executed: Mode
    fell_back_to_full: bool
    cache: Literal["hit", "miss", "forced"]
    satisfied_by: Optional[str]  # filename hash or None
    changed_files_source: str  # "computed" or reason for why it's not available
