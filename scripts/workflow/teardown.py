"""
Tri-state PR merged signal for /cleanup (Step 5 of ADR-0013).

Provides pr_merged_by_head, the deciding signal for whether a PR merged.
Caches results per branch for the length of one run to avoid repeated lookups.
Failure to query is UNKNOWN, never interpreted as "no PR".
"""

import json
import subprocess
from typing import Tuple, Optional, Dict, Callable, List

# Tri-state PR merge signals
MERGED = "merged"
NOT_MERGED = "not_merged"
UNKNOWN = "unknown"

# Module-level cache: (repo, branch, local_tip) -> (state, merged_head_oid, detail)
_pr_cache: Dict[Tuple[str, str, str], Tuple[str, Optional[str], str]] = {}


def clear_pr_cache() -> None:
    """Clear the module-level PR cache."""
    global _pr_cache
    _pr_cache.clear()


def pr_merged_by_head(
    branch: str,
    local_tip: str,
    repo: Optional[str] = None,
    runner: Optional[Callable[[List[str], int], Tuple[int, str, str]]] = None,
    cache: Optional[Dict[Tuple[str, str, str], Tuple[str, Optional[str], str]]] = None
) -> Tuple[str, Optional[str], str]:
    """
    Tri-state signal: is the given branch merged, based on PR head comparison?

    Uses `gh pr list --head <branch> --state all --json state,headRefOid,number`
    to check if there is a MERGED PR whose headRefOid matches local_tip.

    Results are cached per branch for the run, so repeated lookups don't re-query gh.

    Args:
        branch: Branch name to check (passed as an argv element, not shell-expanded)
        local_tip: Expected commit SHA of the branch's HEAD
        repo: Repository context (optional; passed to runner if provided)
        runner: Injectable subprocess runner (default: subprocess.run with timeout).
                Signature: runner(argv, timeout) -> (returncode, stdout, stderr)
        cache: Optional cache dict; default is module-level _pr_cache.
               Key: (repo or "", branch, local_tip)

    Returns:
        (state, merged_head_oid, detail) where:
        - state is MERGED, NOT_MERGED, or UNKNOWN
        - merged_head_oid is the MERGED PR's headRefOid (None for NOT_MERGED or UNKNOWN)
        - detail is a human-readable description of the result or error
    """
    if cache is None:
        cache = _pr_cache

    repo_key = repo or ""
    cache_key = (repo_key, branch, local_tip)

    # Return cached result if available
    if cache_key in cache:
        return cache[cache_key]

    # Default runner: subprocess.run with timeout
    if runner is None:
        def default_runner(argv: List[str], timeout: int) -> Tuple[int, str, str]:
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                return result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                return -1, "", "timeout"

        runner = default_runner

    # Build gh pr list command
    argv = ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state,headRefOid,number"]

    # Run the command
    returncode, stdout, stderr = runner(argv, timeout=30)

    # Check for failures
    if returncode != 0:
        result = (UNKNOWN, None, f"gh pr list failed: {stderr}")
        cache[cache_key] = result
        return result

    if "rate limit" in stderr.lower():
        result = (UNKNOWN, None, "gh rate limit")
        cache[cache_key] = result
        return result

    # Parse JSON output
    try:
        pr_list = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        result = (UNKNOWN, None, "unparseable gh output")
        cache[cache_key] = result
        return result

    # Validate structure: should be a list
    if not isinstance(pr_list, list):
        result = (UNKNOWN, None, "gh output not a list")
        cache[cache_key] = result
        return result

    # Look for exactly one MERGED PR
    merged_prs = []
    for pr in pr_list:
        if not isinstance(pr, dict):
            continue
        if pr.get("state") == "MERGED":
            merged_prs.append(pr)

    # If no MERGED PR, return NOT_MERGED
    if not merged_prs:
        result = (NOT_MERGED, None, "no MERGED PR found")
        cache[cache_key] = result
        return result

    # If more than one MERGED PR, that's unexpected: return UNKNOWN
    if len(merged_prs) > 1:
        result = (UNKNOWN, None, f"multiple MERGED PRs found ({len(merged_prs)})")
        cache[cache_key] = result
        return result

    # Exactly one MERGED PR: check headRefOid
    merged_pr = merged_prs[0]
    merged_oid = merged_pr.get("headRefOid")

    if not isinstance(merged_oid, str):
        result = (UNKNOWN, None, "MERGED PR headRefOid not a string")
        cache[cache_key] = result
        return result

    # Compare headRefOid to local_tip
    if merged_oid == local_tip:
        result = (MERGED, merged_oid, f"MERGED PR #{merged_pr.get('number')}")
        cache[cache_key] = result
        return result
    else:
        result = (NOT_MERGED, None, f"MERGED PR headRefOid {merged_oid} != local tip {local_tip}")
        cache[cache_key] = result
        return result


def display_disagreement(
    cached_state: Optional[str],
    gh_view_state: Optional[str],
    signal_state: str
) -> Optional[str]:
    """
    Return a summary line when display-only values disagree with the deciding signal.

    The deciding signal is pr_merged_by_head's returned state.
    Display-only values are from the cached pr.state and `gh pr view` state.
    When they disagree, return a human-readable line; otherwise return None.

    Args:
        cached_state: pr.state from the cached github-cache.json (None if absent)
        gh_view_state: state from `gh pr view` (None if absent)
        signal_state: The deciding signal state (MERGED, NOT_MERGED, UNKNOWN)

    Returns:
        A summary line if there is disagreement, else None.
    """
    # Normalize states for comparison
    # Cached and gh_view might have different casing or presence
    cached_normalized = cached_state.upper() if cached_state else None
    gh_normalized = gh_view_state.upper() if gh_view_state else None
    signal_normalized = signal_state.upper() if signal_state else None

    # Check if any display-only value disagrees with the signal
    if cached_normalized and cached_normalized != signal_normalized:
        return f"Disagreement: cached pr.state={cached_state}, signal={signal_state}"

    if gh_normalized and gh_normalized != signal_normalized:
        return f"Disagreement: gh pr view state={gh_view_state}, signal={signal_state}"

    # No disagreement
    return None
