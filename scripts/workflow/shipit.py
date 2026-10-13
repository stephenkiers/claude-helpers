"""
Shipit plan and apply (ADR-0013 Amendment 2).

Ports the deterministic shipit logic into a plan/apply pattern:
- plan_shipit: read-only inspection of current branch and PR state
- apply_shipit: execute commit, push, create/edit PR mutations
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from . import git
from .cache import (
    cached_pr_number, hash_cache_file, hash_file_content, positive_int,
    trusted_github_cache, verify_pr_head, write_cache
)
from .safety import Unknown, fail_closed
from .models import StackInfo, GITHUB_CACHE_SCHEMA_VERSION


@dataclass
class ShipitPlan:
    """
    Plan for committing, pushing, and creating/updating a PR.

    pr_exists / pr_number are a claim read from a github-cache that belongs to this
    branch (see trusted_github_cache). apply_shipit re-verifies the PR head against
    GitHub before any edit.

    cache_stale is None for a trusted cache, otherwise the stale reason: "missing",
    "invalid", or "branch_mismatch:<branch>". When set, pr/stack/issue are ignored.
    stale_stack_claimed is True when the stale cache claimed a stack, so the doc
    must not use it for the stacked push.
    """
    branch: str
    expected_head_sha: Optional[str] = None
    cache_hash: Optional[str] = None
    commit_message_path: str = ""
    pr_body_path: Optional[str] = None
    pr_title: Optional[str] = None
    pr_number: Optional[int] = None
    pr_exists: bool = False
    stack: Optional[StackInfo] = None
    base_branch: Optional[str] = None
    plan_hash: Optional[str] = None
    issue_number: Optional[int] = None
    cache_stale: Optional[str] = None
    stale_stack_claimed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
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
    def from_dict(cls, data: Dict[str, Any]) -> "ShipitPlan":
        """Construct from parsed JSON dict."""
        stack = None
        if "stack" in data and data["stack"]:
            stack_data = data["stack"]
            stack = StackInfo(
                is_stacked=stack_data.get("isStacked", False),
                parent_branch=stack_data.get("parentBranch"),
                parent_pr=stack_data.get("parentPr")
            )
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        data_copy = data.copy()
        data_copy["stack"] = stack
        return cls(**{k: v for k, v in data_copy.items() if k in field_names})


@dataclass
class ShipitResult:
    """Result of applying a shipit plan."""
    success: bool
    committed: bool = False
    pushed: bool = False
    pr_created: bool = False
    pr_updated: bool = False
    pr_url: Optional[str] = None
    # Set when the cached PR was MERGED and a new PR was created instead (issue #213).
    merged_pr_number: Optional[int] = None
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


def _fresh_github_cache_base(path: Path, branch: str) -> Dict[str, Any]:
    """
    Base dict for a write-back over a stale github-cache (issue #213).

    Drops pr, stack, issue, and localPlan. Keeps 'review' only when the existing file
    parses as a dict and review.branch equals this branch in write-review-cache.py's
    normalized form (slashes replaced by dashes), so a review-only cache keeps its
    review for this branch.
    """
    fresh: Dict[str, Any] = {}
    existing: Any = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
    if isinstance(existing, dict):
        review = existing.get("review")
        normalized_branch = branch.strip().replace("/", "-")
        if isinstance(review, dict) and review.get("branch") == normalized_branch:
            fresh["review"] = review
    return fresh


@fail_closed
def plan_shipit(
    commit_message_path: str,
    pr_body_path: Optional[str] = None,
    pr_title: Optional[str] = None,
    cwd: Optional[Path] = None
) -> Tuple[Optional[ShipitPlan], Optional[Unknown]]:
    """
    Plan a shipit operation (read-only, offline, no gh calls).

    The github-cache is read only when it is trusted for this branch (its 'branch'
    equals the current branch). Otherwise pr/stack/issue are ignored and cache_stale
    records why. pr_exists/pr_number are a cached claim; apply_shipit re-verifies
    the PR head before editing.

    Returns (ShipitPlan, None) on success.
    Returns (None, Unknown(...)) if branch/HEAD inspection fails.
    """
    try:
        branch = git.get_current_branch(cwd=cwd)
        expected_head_sha = git.get_head_sha(cwd=cwd)

        # Read cache hash
        repo_root = cwd or Path.cwd()
        cache_path = repo_root / ".claude" / "repo-cache.json"
        cache_hash = hash_cache_file(cache_path)

        # Whole-cache branch check (issue #213): pr, stack, and issue are read only
        # from a cache that describes this branch.
        github_cache_path = repo_root / ".claude" / "github-cache.json"
        trusted, pr_section, cache_stale, stale_stack_claimed = trusted_github_cache(
            github_cache_path, branch
        )

        pr_number = None
        pr_exists = False
        stack = None
        issue_number = None
        if trusted is not None:
            stack = trusted.stack
            if trusted.issue is not None:
                issue_number = positive_int(trusted.issue.number)
            pr_number = cached_pr_number(pr_section)
            pr_exists = pr_number is not None

        plan = ShipitPlan(
            branch=branch,
            expected_head_sha=expected_head_sha,
            cache_hash=cache_hash,
            commit_message_path=commit_message_path,
            pr_body_path=pr_body_path,
            pr_title=pr_title,
            pr_number=pr_number,
            pr_exists=pr_exists,
            stack=stack,
            base_branch=stack.parent_branch if stack and stack.is_stacked else None,
            issue_number=issue_number,
            cache_stale=cache_stale,
            stale_stack_claimed=stale_stack_claimed
        )

        plan_json = json.dumps(plan.to_dict())
        plan.plan_hash = hash_file_content(plan_json)

        return plan, None

    except Exception as e:
        return None, Unknown(f"plan_shipit failed: {e}")


@fail_closed
def apply_shipit(plan_json: str, cwd: Optional[Path] = None) -> Tuple[ShipitResult, Optional[Unknown]]:
    """
    Apply a shipit plan (mutating).

    Validates freshness triple (branch name, HEAD SHA, cache hash), then executes:
    1. git add -A
    2. git commit -F <message_path>
    3. git push -u origin <branch>
    4. If pr_exists, verify the cached PR's head is plan.branch and it is OPEN
       (edit-path-only live probe, issue #213). MERGED falls through to step 5 as a
       create; CLOSED, a head mismatch, or a failed lookup refuses with no PR mutation.
    5. gh pr create (no PR, or cached PR MERGED) or gh pr edit (cached PR OPEN)
    6. Write PR/stack data to .claude/github-cache.json (stamps branch and
       schema_version; a stale cache is replaced, not merged)

    Partial-success contract: if the probe or PR step refuses after the push, the
    branch is committed and pushed (committed/pushed stay True), the PR is untouched,
    and the cache is not rewritten. The cache is written only after a successful
    create or edit. Re-running is safe: the push is idempotent.

    Returns (ShipitResult, None) with execution result.
    Returns (ShipitResult, Unknown(...)) if a critical error occurs.
    """
    try:
        plan_data = json.loads(plan_json)
        plan = ShipitPlan.from_dict(plan_data)

        result = ShipitResult(success=False)

        try:
            repo_root = cwd or Path.cwd()

            # Validate freshness triple
            current_branch = git.get_current_branch(cwd=repo_root)
            if current_branch != plan.branch:
                result.error = Unknown(f"Branch changed: was {plan.branch}, now {current_branch}")
                return result, result.error

            current_head_sha = git.get_head_sha(cwd=repo_root)
            if current_head_sha != plan.expected_head_sha:
                result.error = Unknown("HEAD SHA changed (plan is stale)")
                return result, result.error

            cache_path = repo_root / ".claude" / "repo-cache.json"
            cache_hash = hash_cache_file(cache_path)
            if cache_hash != plan.cache_hash:
                result.error = Unknown("Cache has changed (plan is stale)")
                return result, result.error

        except Exception as e:
            result.error = Unknown(f"Freshness validation failed: {e}")
            return result, result.error

        # Stage all changes
        try:
            success, err = git.stage_all(cwd=repo_root)
            if not success and err:
                result.error = err
                return result, result.error
        except Exception as e:
            result.error = Unknown(f"Failed to stage changes: {e}")
            return result, result.error

        # Commit with message file
        try:
            success, err = git.commit_with_message_file(Path(plan.commit_message_path), cwd=repo_root)
            if success:
                result.committed = True
            elif err:
                result.error = err
                return result, result.error
        except Exception as e:
            result.error = Unknown(f"Failed to commit: {e}")
            return result, result.error

        # Push upstream
        try:
            success, err = git.push_upstream("origin", plan.branch, cwd=repo_root)
            if success:
                result.pushed = True
            elif err:
                result.error = err
                return result, result.error
        except Exception as e:
            result.error = Unknown(f"Failed to push: {e}")
            return result, result.error

        # Create or edit PR
        try:
            # Use provided paths or fall back to standard locations
            body_file = Path(plan.pr_body_path) if plan.pr_body_path else repo_root / ".claude" / "pr-body.tmp"
            if not body_file.exists():
                body_file = Path(plan.commit_message_path)

            title = plan.pr_title or "shipit"

            # Edit-path-only live probe (issue #213). Runs before any PR mutation:
            # the cached number is edited only if GitHub says its head is this branch
            # and it is OPEN. MERGED falls through to create; anything else refuses.
            if plan.pr_exists:
                if plan.pr_number is None:
                    result.error = Unknown(
                        "Plan says a PR exists but carries no PR number. Branch was pushed; "
                        "the PR was not touched. Re-run /shipit"
                    )
                    return result, result.error
                pr_state, probe_err = verify_pr_head(plan.pr_number, plan.branch, repo_root)
                if probe_err is not None:
                    result.error = Unknown(
                        f"{probe_err.reason}. Branch was pushed; the PR was not touched. Retry if the "
                        "lookup failed; on a head mismatch, clear the pr section in "
                        ".claude/github-cache.json and re-run"
                    )
                    return result, result.error
                if pr_state == "MERGED":
                    result.merged_pr_number = plan.pr_number
                    plan.pr_number = None
                    plan.pr_exists = False
                elif pr_state != "OPEN":
                    result.error = Unknown(
                        f"cached PR #{plan.pr_number} for {plan.branch} is {pr_state}; not editing or "
                        "creating. Reopen it or clear .claude/github-cache.json pr and re-run"
                    )
                    return result, result.error

            if not plan.pr_exists:
                # Create PR
                pr_url, err = git.pr_create(
                    title=title,
                    body_file=body_file,
                    base=plan.base_branch,
                    cwd=repo_root
                )
                if pr_url:
                    result.pr_created = True
                    result.pr_url = pr_url
                    # Extract PR number from URL
                    try:
                        pr_num = int(pr_url.rstrip().split('/')[-1])
                        plan.pr_number = pr_num
                    except (ValueError, IndexError):
                        pass
                elif err:
                    result.error = err
                    return result, result.error
            else:
                # Edit existing PR
                assert plan.pr_number is not None  # pr_exists is True implies pr_number is set
                success, err = git.pr_edit(
                    pr_number=plan.pr_number,
                    title=title,
                    body_file=body_file,
                    cwd=repo_root
                )
                if success:
                    result.pr_updated = True
                elif err:
                    result.error = err
                    return result, result.error
        except Exception as e:
            result.error = Unknown(f"Failed to create/edit PR: {e}")
            return result, result.error

        # Write PR data back to github cache
        try:
            github_cache_path = repo_root / ".claude" / "github-cache.json"
            if plan.cache_stale is None:
                existing_data = {}
                if github_cache_path.exists():
                    try:
                        existing_data = json.loads(github_cache_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
            else:
                # Stale cache (issue #213): start fresh so no stale pr/stack/issue survives.
                existing_data = _fresh_github_cache_base(github_cache_path, plan.branch)

            # Stamp branch and schema_version so the next plan_shipit trusts this cache.
            existing_data["schema_version"] = GITHUB_CACHE_SCHEMA_VERSION
            existing_data["branch"] = plan.branch

            # pr section: on the create path (including MERGED-create) replace it
            # wholesale. On the edit path keep the verified existing section.
            if result.pr_created:
                existing_data.pop("pr", None)

                # Merge PR data into cache
                # Attempt fallback extraction if plan.pr_number is still None
                pr_num_to_cache = plan.pr_number
                if not pr_num_to_cache and result.pr_url:
                    try:
                        pr_num_to_cache = int(result.pr_url.rstrip().split('/')[-1])
                    except (ValueError, IndexError):
                        pr_num_to_cache = None

                if pr_num_to_cache and result.pr_url:
                    pr_data = {
                        "number": pr_num_to_cache,
                        "url": result.pr_url,
                        "state": "OPEN"
                    }
                    existing_data["pr"] = pr_data

            # Update stack info if present
            if plan.stack:
                existing_data["stack"] = {
                    "isStacked": plan.stack.is_stacked,
                    "parentBranch": plan.stack.parent_branch,
                    "parentPr": plan.stack.parent_pr
                }

            # write_cache's lock file needs the parent dir to exist already.
            github_cache_path.parent.mkdir(parents=True, exist_ok=True)
            success, cache_err = write_cache(github_cache_path, existing_data)
            if not success and cache_err:
                # Cache write failure is non-fatal; log but don't fail the operation
                pass

        except Exception:
            # Non-fatal; cache write failures don't fail the whole operation
            pass

        result.success = result.committed and result.pushed
        return result, None

    except json.JSONDecodeError as e:
        return ShipitResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return ShipitResult(success=False, error=Unknown(f"apply_shipit failed: {e}")), None
