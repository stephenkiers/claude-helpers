"""
Track plan and apply (Phase 3a of ADR-0013).

Ports GitHub mode and Local Plan Mode shared mechanics from /track-and-start
into a plan/apply pattern:
- plan_track: read-only inspection and duplicate detection
- apply_track: execute issue creation, worktree creation, cache write

Explicit out-of-scope for 3a: semantic duplicate comparison, pivot detection/execution,
Tracker Ticket mode, label inference judgment (keyword table moves, judgment stays).
"""

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from . import git, worktrees
from .cache import hash_cache_file, hash_file_content, write_cache
from .safety import Unknown, fail_closed
from .models import IssueInfo, GitHubCacheData
from .providers.base import Provider


# Step constants for partial-success reporting
STEP_CREATE_ISSUE = "create_issue"
STEP_CREATE_WORKTREE = "create_worktree"
STEP_WRITE_CACHE = "write_cache"


def slugify(title: str, max_len: int = 50) -> str:
    """
    Generate kebab-case slug from title.

    Lowercase, replace every non-[a-z0-9] run with -, collapse repeats,
    strip leading/trailing -, truncate to max_len (preserving no trailing -).

    Reproduces the existing shell pipeline semantics:
    tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' |
    sed 's/^-//' | sed 's/-$//' | cut -c1-N
    """
    # Lowercase
    slug = title.lower()
    # Replace non-alphanumeric with -
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Collapse repeats
    slug = re.sub(r"-+", "-", slug)
    # Strip leading/trailing
    slug = slug.strip("-")
    # Truncate and strip trailing - that may have been created by cut
    slug = slug[:max_len].rstrip("-")
    return slug


def infer_type(title: str, content: str = "") -> str:
    """
    Infer issue type from title and content keywords.

    Scans title first, then content (title takes precedence).
    Returns "fix" | "feature" | "chore", default "feature".
    Matches whole words only, case-insensitive.
    """
    # Define keywords per type (checked in precedence order)
    keywords = {
        "fix": ["fix", "bug", "broken", "error", "crash"],
        "feature": ["add", "new", "feature", "implement", "create"],
        "chore": ["refactor", "cleanup", "update", "chore", "rename", "move"],
    }

    for issue_type, words in keywords.items():
        # Check title first
        if _has_keyword(title, words):
            return issue_type
        # Then content
        if _has_keyword(content, words):
            return issue_type

    return "feature"


def infer_labels(title: str, content: str = "") -> List[str]:
    """
    Infer labels from title and content keywords.

    Returns all matching labels (may be empty), deduped, in table order.
    Matches whole words only, case-insensitive.
    Scans title first, then content.
    """
    keywords = {
        "bug": ["fix", "bug", "broken"],
        "enhancement": ["add", "new", "feature"],
        "documentation": ["doc", "readme", "guide"],
        "chore": ["refactor", "cleanup"],
    }

    result = []
    seen = set()

    for label, words in keywords.items():
        # Check title first, then content
        if _has_keyword(title, words) or _has_keyword(content, words):
            if label not in seen:
                result.append(label)
                seen.add(label)

    return result


def _has_keyword(text: str, keywords: List[str]) -> bool:
    """Check if text contains any whole-word keyword (case-insensitive)."""
    text_lower = text.lower()
    for keyword in keywords:
        # Whole-word match: word boundary before and after
        if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
            return True
    return False


def build_branch_name(issue_type: str, issue_number: Any, slug: str) -> str:
    """Build branch name from type, number, and slug."""
    return f"{issue_type}/{issue_number}-{slug}"


# Template placeholder for issue number (filled in by apply_track)
ISSUE_NUMBER_PLACEHOLDER = "{issue_number}"


@dataclass
class TrackPlan:
    """Plan for creating a GitHub issue and worktree."""
    mode: str  # "github" | "local"
    branch: str
    worktree_path: str
    issue_title: str
    issue_type: str  # fix|feature|chore
    labels: List[str] = field(default_factory=list)
    candidate_issues: List[IssueInfo] = field(default_factory=list)
    slug: str = ""
    main_worktree: str = ""
    worktree_parent: str = ""
    project_root: str = ""
    plan_content: str = ""
    assignee: Optional[str] = None
    issue_number: Optional[int] = None  # None in 3a: filled in by apply
    worktree_exists: bool = False
    branch_exists: bool = False
    expected_head_sha: Optional[str] = None
    cache_hash: Optional[str] = None
    plan_hash: Optional[str] = None
    needs_confirmation: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d["candidate_issues"] = [asdict(ci) for ci in self.candidate_issues]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrackPlan":
        """Construct from parsed JSON dict."""
        # Reconstruct candidate_issues
        issue_field_names = {f.name for f in IssueInfo.__dataclass_fields__.values()}
        candidate_issues = [
            IssueInfo(**{k: v for k, v in issue.items() if k in issue_field_names})
            for issue in data.get("candidate_issues", [])
        ]

        # Construct plan with filtered fields
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        data_copy = data.copy()
        data_copy["candidate_issues"] = candidate_issues
        return cls(**{k: v for k, v in data_copy.items() if k in field_names})


@dataclass
class TrackApplyResult:
    """Result of applying a track plan."""
    success: bool
    steps_completed: List[str] = field(default_factory=list)
    steps_failed: List[str] = field(default_factory=list)
    issue_number: Optional[int] = None
    issue_url: Optional[str] = None
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    cache_written: bool = False
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_track(
    provider: Provider,
    plan_content: str,
    title: str,
    mode: str = "github",
    assignee: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Tuple[Optional[TrackPlan], Optional[Unknown]]:
    """
    Plan a track operation (read-only).

    Args:
        provider: Provider implementation (GitHub or local)
        plan_content: Full plan content (body for the issue)
        title: Issue title
        mode: "github" or "local"
        assignee: Optional assignee
        cwd: Working directory

    Returns:
        (TrackPlan, None) on success.
        (None, Unknown(...)) if planning fails.
    """
    try:
        # 1. Resolve worktree paths
        main_worktree = worktrees.detect_main_worktree(cwd=cwd)
        if not main_worktree:
            return None, Unknown("not in a git repository")

        worktree_parent = worktrees.detect_worktree_parent(cwd=cwd)
        if not worktree_parent:
            return None, Unknown("could not detect worktree parent")

        project_root = worktrees.detect_project_root(cwd=cwd)
        if not project_root:
            return None, Unknown("could not detect project root")

        # 2. Infer type and labels, generate slug
        slug = slugify(title, max_len=50)
        issue_type = infer_type(title, plan_content)
        labels = infer_labels(title, plan_content)

        # 3. Build templated branch name (issue number not known yet)
        branch = build_branch_name(issue_type, ISSUE_NUMBER_PLACEHOLDER, slug)
        worktree_path = f"{worktree_parent}/{ISSUE_NUMBER_PLACEHOLDER}-{slug}"

        # 5. Collision detection (limited at plan time since issue number unknown)
        needs_confirmation = []
        needs_confirmation.append("collision_unchecked")  # Collision check deferred to apply

        # 4. Fetch open issues for duplicate detection
        candidate_issues = []
        try:
            candidate_issues = provider.list_open_issues()
        except Exception as e:
            # list_open_issues() does not raise under normal conditions (returns []
            # for no issues / no remote / not authenticated). An exception here is
            # genuinely exceptional and defeats duplicate detection, which is
            # load-bearing. Signal it to the wrapper but don't fail the plan.
            needs_confirmation.append("candidate_fetch_failed")
            candidate_issues = []

        # 6. Freshness hashes
        expected_head_sha = None
        try:
            expected_head_sha = git.get_head_sha(cwd=Path(main_worktree) if main_worktree else None)
        except Exception as e:
            # get_head_sha() is load-bearing: apply_track compares expected_sha != plan.expected_head_sha,
            # so a None here means the plan is guaranteed to be rejected as stale. Emitting a plan that
            # cannot possibly apply is worse than failing now.
            return None, Unknown(f"failed to capture HEAD SHA (needed for freshness check): {e}")

        # hash_cache_file() already catches OSError internally and returns None legitimately
        # when the file doesn't exist or if hashing fails. No exception will escape from it.
        # We compute the hash to detect stale plans; None is a valid state (file doesn't exist yet).
        cache_hash = hash_cache_file(Path(main_worktree) / ".claude" / "repo-cache.json")

        # Build plan
        plan = TrackPlan(
            mode=mode,
            branch=branch,
            worktree_path=worktree_path,
            issue_title=title,
            issue_type=issue_type,
            labels=labels,
            candidate_issues=candidate_issues,
            slug=slug,
            main_worktree=str(main_worktree) if main_worktree else "",
            worktree_parent=str(worktree_parent) if worktree_parent else "",
            project_root=str(project_root) if project_root else "",
            plan_content=plan_content,
            assignee=assignee,
            expected_head_sha=expected_head_sha,
            cache_hash=cache_hash,
            needs_confirmation=needs_confirmation,
        )

        # 7. Hash the plan
        plan_json = json.dumps(plan.to_dict())
        plan.plan_hash = hash_file_content(plan_json)

        return plan, None

    except Exception as e:
        return None, Unknown(f"plan_track failed: {e}")


@fail_closed
def apply_track(provider: Provider, plan_json: str, cwd: Optional[Path] = None) -> Tuple[TrackApplyResult, Optional[Unknown]]:
    """
    Apply a track plan (mutating).

    Validates freshness, creates issue, creates worktree, writes cache.

    Args:
        provider: Provider implementation
        plan_json: JSON plan to apply
        cwd: Working directory

    Returns:
        (TrackApplyResult, None) with execution result.
        (TrackApplyResult, Unknown(...)) if critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = TrackPlan.from_dict(plan_data)

        result = TrackApplyResult(success=False)

        try:
            # 1. Freshness validation
            main_wt = Path(plan.main_worktree)
            expected_sha = git.get_head_sha(cwd=main_wt)
            if expected_sha != plan.expected_head_sha:
                result.error = Unknown("plan went stale (HEAD SHA changed)")
                return result, result.error

            cache_hash = hash_cache_file(main_wt / ".claude" / "repo-cache.json")
            if cache_hash != plan.cache_hash:
                result.error = Unknown("plan went stale (cache changed)")
                return result, result.error

        except Exception as e:
            result.error = Unknown(f"freshness validation failed: {e}")
            return result, result.error

        # 2. Create issue
        try:
            issue_info = provider.create_issue(
                plan.issue_title,
                plan.plan_content,
                plan.labels,
                plan.assignee
            )
            result.issue_number = issue_info.number
            result.issue_url = issue_info.url
            result.steps_completed.append(STEP_CREATE_ISSUE)
        except Exception as e:
            result.error = Unknown(f"issue creation failed: {e}")
            result.steps_failed.append(STEP_CREATE_ISSUE)
            return result, result.error

        # 3. Resolve real branch/worktree names now that issue number exists
        real_branch = build_branch_name(plan.issue_type, issue_info.number, plan.slug)
        real_worktree_path = Path(plan.worktree_parent) / f"{issue_info.number}-{plan.slug}"

        # 4. Check collisions now (real names)
        try:
            if real_worktree_path.exists():
                msg = f"worktree already exists at {real_worktree_path}"
                result.error = Unknown(msg)
                result.steps_failed.append(STEP_CREATE_WORKTREE)
                return result, result.error

            # Check if branch exists
            exists, err = git.branch_exists(real_branch, cwd=main_wt)
            if err:
                # The issue was already created above (issue #{result.issue_number});
                # a failed collision check must not silently proceed as "no collision."
                msg = f"branch existence check failed (issue #{result.issue_number} already created): {err}"
                result.error = Unknown(msg)
                result.steps_failed.append(STEP_CREATE_WORKTREE)
                return result, result.error
            if exists:
                msg = f"branch {real_branch} already exists"
                result.error = Unknown(msg)
                result.steps_failed.append(STEP_CREATE_WORKTREE)
                return result, result.error

        except Exception as e:
            result.error = Unknown(f"collision check failed: {e}")
            result.steps_failed.append(STEP_CREATE_WORKTREE)
            return result, result.error

        # 5. Create worktree
        try:
            success, err = git.add_worktree(real_worktree_path, real_branch, cwd=main_wt)
            if not success:
                result.error = err or Unknown("failed to create worktree")
                result.steps_failed.append(STEP_CREATE_WORKTREE)
                return result, result.error
            result.steps_completed.append(STEP_CREATE_WORKTREE)
            result.worktree_path = str(real_worktree_path)
            result.branch = real_branch
        except Exception as e:
            result.error = Unknown(f"worktree creation failed: {e}")
            result.steps_failed.append(STEP_CREATE_WORKTREE)
            return result, result.error

        # 6. Write cache in new worktree
        try:
            cache_data = GitHubCacheData(
                branch=real_branch,
                issue=issue_info
            )
            cache_path = real_worktree_path / ".claude" / "github-cache.json"
            success, err = write_cache(cache_path, cache_data.to_dict())
            if not success:
                result.error = err or Unknown("failed to write cache")
                result.steps_failed.append(STEP_WRITE_CACHE)
                return result, result.error
            result.steps_completed.append(STEP_WRITE_CACHE)
            result.cache_written = True
        except Exception as e:
            result.error = Unknown(f"cache write failed: {e}")
            result.steps_failed.append(STEP_WRITE_CACHE)
            return result, result.error

        # Success!
        result.success = True
        return result, None

    except json.JSONDecodeError as e:
        return TrackApplyResult(success=False, error=Unknown(f"invalid plan JSON: {e}")), None
    except Exception as e:
        return TrackApplyResult(success=False, error=Unknown(f"apply_track failed: {e}")), None
