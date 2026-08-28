"""
Shipit plan and apply (ADR-0013 Amendment 2).

Ports the deterministic shipit logic into a plan/apply pattern:
- plan_shipit: read-only inspection of current branch and PR state
- apply_shipit: execute commit, push, create/edit PR mutations
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from . import git
from .cache import hash_cache_file, hash_file_content, read_github_cache, write_cache
from .safety import Unknown, fail_closed
from .models import GitHubCacheData, StackInfo


@dataclass
class ShipitPlan:
    """Plan for committing, pushing, and creating/updating a PR."""
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
    error: Optional[Unknown] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        if self.error:
            d["error"] = str(self.error)
        return d


@fail_closed
def plan_shipit(
    commit_message_path: str,
    pr_body_path: Optional[str] = None,
    pr_title: Optional[str] = None,
    cwd: Optional[Path] = None
) -> Tuple[Optional[ShipitPlan], Optional[Unknown]]:
    """
    Plan a shipit operation (read-only).

    Returns (ShipitPlan, None) on success.
    Returns (None, Unknown(...)) if branch/HEAD/cache inspection fails.
    """
    try:
        branch = git.get_current_branch(cwd=cwd)
        expected_head_sha = git.get_head_sha(cwd=cwd)

        # Read cache hash
        repo_root = cwd or Path.cwd()
        cache_path = repo_root / ".claude" / "repo-cache.json"
        cache_hash = hash_cache_file(cache_path)

        # Read github cache for PR/stack info
        github_cache_path = repo_root / ".claude" / "github-cache.json"
        github_cache_data, cache_err = read_github_cache(github_cache_path)

        pr_number = None
        pr_exists = False
        stack = None
        if github_cache_data:
            pr_number = github_cache_data.pr.get("number") if github_cache_data.pr else None
            pr_exists = pr_number is not None
            stack = github_cache_data.stack

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
            base_branch=stack.parent_branch if stack and stack.is_stacked else None
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
    4. gh pr create or gh pr edit (depending on pr_exists)
    5. Write PR/stack data to .claude/github-cache.json

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
                result.error = Unknown(f"HEAD SHA changed (plan is stale)")
                return result, result.error

            cache_path = repo_root / ".claude" / "repo-cache.json"
            cache_hash = hash_cache_file(cache_path)
            if cache_hash != plan.cache_hash:
                result.error = Unknown(f"Cache has changed (plan is stale)")
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
                        pr_num = int(pr_url.split('/')[-1])
                        plan.pr_number = pr_num
                    except (ValueError, IndexError):
                        pass
                elif err:
                    result.error = err
                    return result, result.error
            else:
                # Edit existing PR
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
            existing_data = {}
            if github_cache_path.exists():
                try:
                    existing_data = json.loads(github_cache_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

            # Merge PR data into cache
            if plan.pr_number and result.pr_url:
                pr_data = {
                    "number": plan.pr_number,
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

            success, cache_err = write_cache(github_cache_path, existing_data)
            if not success and cache_err:
                # Cache write failure is non-fatal; log but don't fail the operation
                pass

        except Exception as e:
            # Non-fatal; cache write failures don't fail the whole operation
            pass

        result.success = result.committed and result.pushed
        return result, None

    except json.JSONDecodeError as e:
        return ShipitResult(success=False, error=Unknown(f"Invalid plan JSON: {e}")), None
    except Exception as e:
        return ShipitResult(success=False, error=Unknown(f"apply_shipit failed: {e}")), None
