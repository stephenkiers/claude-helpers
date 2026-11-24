"""
Machine-wide stale worktree sweep and flow classification (Steps 9-10 of ADR-0013).

Provides:
  - classify_flow(): Read-only classification of a flow's state (ACTIVE, STALLED, DEAD, UNKNOWN)
  - discover_stale_repos(): Find stale worktrees across all repos

All functions are composable and use injected signals/runners so they can be tested without network or processes.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Any

from . import procs
from . import teardown


# Flow classification states
ACTIVE = "active"
STALLED = "stalled"
DEAD = "dead"
UNKNOWN = "unknown"


def discover_stale_repos(
    repos_root: Optional[str] = None,
    with_sizes: bool = False
) -> Dict[str, Any]:
    """
    Discover stale worktrees across all repos.

    Returns a JSON-able structure grouped by repo with numbered items.

    Args:
        repos_root: Override repos root (uses CLAUDE_REPOS_ROOT or ~/.claude/preferences.yaml if None)
        with_sizes: If True, include build directory sizes

    Returns:
        Dict with structure: {
            "success": bool,
            "repos": [
                {
                    "path": str,
                    "items": [
                        {"number": int, "kind": str, "name": str, "path": str, ...},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    if repos_root is None:
        repos_root = os.environ.get("CLAUDE_REPOS_ROOT")
        if not repos_root:
            prefs_path = Path.home() / ".claude" / "preferences.yaml"
            if prefs_path.exists():
                try:
                    import yaml  # type: ignore[import-untyped]
                    with open(prefs_path) as f:
                        prefs = yaml.safe_load(f) or {}
                        repos_root = prefs.get("reposRoot")
                except Exception:
                    pass
            if not repos_root:
                repos_root = str(Path.home() / "Repositories")

    repos_root_path = Path(repos_root).resolve()
    if not repos_root_path.exists():
        return {
            "success": False,
            "error": f"repos root does not exist: {repos_root_path}",
            "repos": []
        }

    repos_list = []
    try:
        for entry in repos_root_path.iterdir():
            if entry.is_dir() and (entry / "worktrees").exists():
                repos_list.append(entry)
    except OSError as e:
        return {
            "success": False,
            "error": f"Cannot enumerate repos root: {e}",
            "repos": []
        }

    result_repos = []
    for repo_path in repos_list:
        # For each repo, discover stale items
        repo_items = []
        item_number = 1

        # Try to list actual worktrees in this repo
        parent_path = repo_path / "worktrees"
        actual_dirs = set()
        try:
            for entry in parent_path.iterdir():
                if entry.is_dir():
                    actual_dirs.add(entry)
        except OSError:
            pass

        # Discover orphan directories (not fully comprehensive, but basic implementation)
        for wt_path in actual_dirs:
            repo_items.append({
                "number": item_number,
                "kind": "worktree",
                "name": f"Worktree {wt_path.name}",
                "path": str(wt_path),
                "selectable": True,
                "reason": "",
                "status": "ok"
            })
            item_number += 1

        # Check for trash directory
        trash_dir = parent_path / ".cleanup-trash"
        if trash_dir.exists():
            try:
                for entry in trash_dir.iterdir():
                    if entry.is_dir():
                        repo_items.append({
                            "number": item_number,
                            "kind": "trash",
                            "name": f"Trash entry {entry.name}",
                            "path": str(entry),
                            "selectable": True,
                            "reason": "",
                            "status": "ok"
                        })
                        item_number += 1
            except OSError:
                pass

        result_repos.append({
            "path": str(repo_path),
            "items": repo_items
        })

    return {
        "success": True,
        "repos": result_repos
    }


def classify_all_flows(flows_root: str) -> Dict[str, Any]:
    """
    Classify all flows in a flows root directory.

    Returns a JSON-able structure with classifications for each flow.

    Args:
        flows_root: Path to flows root directory (~/.claude/flows/)

    Returns:
        Dict with structure: {
            "success": bool,
            "flows": [
                {
                    "dir": str,
                    "issue": str or null,
                    "class": str,
                    "reason": str,
                    "suggestion": str or null,
                    "signals": str,
                    "is_duplicate": bool,
                    "newer_dir": str or null
                },
                ...
            ]
        }
    """
    flows_path = Path(flows_root).resolve()
    if not flows_path.exists():
        return {
            "success": False,
            "error": f"Flows root does not exist: {flows_path}",
            "flows": []
        }

    flows = []
    try:
        # Find all state.json files in the flows root
        for entry in flows_path.iterdir():
            if entry.is_dir():
                state_file = entry / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file) as f:
                            state = json.load(f)

                        # For now, return basic classification without full signal evaluation
                        # (full implementation would probe processes and PR state)
                        classification = {
                            "dir": str(entry),
                            "issue": state.get("issue"),
                            "class": UNKNOWN,
                            "reason": "Classification requires process and PR probes (not implemented in CLI)",
                            "suggestion": None,
                            "signals": "unknown",
                            "is_duplicate": False,
                            "newer_dir": None
                        }
                        flows.append(classification)
                    except (json.JSONDecodeError, OSError):
                        pass
    except OSError as e:
        return {
            "success": False,
            "error": f"Cannot enumerate flows root: {e}",
            "flows": []
        }

    return {
        "success": True,
        "flows": flows
    }


@dataclass
class FlowSignals:
    """Tri-state signals for flow classification."""
    liveness: str  # LIVE, NOT_LIVE, UNKNOWN (from procs module)
    worktree_exists: Optional[bool] = None
    commits_ahead: Optional[bool] = None
    uncommitted: Optional[bool] = None
    issue_state: Optional[str] = None  # "open" or "closed"
    pr_state: str = teardown.UNKNOWN  # MERGED, NOT_MERGED, UNKNOWN


@dataclass
class FlowClassification:
    """Classification result for a flow."""
    class_name: str  # ACTIVE, STALLED, DEAD, UNKNOWN
    reason: str  # Human-readable explanation
    suggestion: Optional[str]  # Suggested command (or None if no action suggested)
    signals_summary: str  # Brief summary of signals for display
    is_duplicate: bool = False  # True if newer flow exists for this issue
    newer_dir: Optional[str] = None  # Path to newer duplicate if is_duplicate


def classify_flow(
    state: Dict[str, Any],
    signals: FlowSignals
) -> FlowClassification:
    """
    Classify a flow's state based on its state.json and injected signals.

    Classification is ordered (first match wins):
    1. ACTIVE: liveness is LIVE
    2. STALLED (liveness unknown): liveness is UNKNOWN
    3. UNKNOWN: any other deciding signal is UNKNOWN
    4. DEAD: PR MERGED by head, or issue closed, or worktree missing, or clean & nothing ahead
    5. STALLED (work to keep): everything else (commits ahead or uncommitted, PR not merged, issue open)

    Args:
        state: Parsed state.json dict with keys: branch, worktree, issue, pr, etc.
        signals: FlowSignals with tri-state values

    Returns:
        FlowClassification with class_name, reason, suggestion, and signals_summary
    """
    # Build signals summary for display
    summary_parts = []
    if signals.liveness == procs.LIVE:
        summary_parts.append("live")
    elif signals.liveness == procs.UNKNOWN:
        summary_parts.append("liveness unknown")

    if signals.worktree_exists is False:
        summary_parts.append("no worktree")
    elif signals.worktree_exists is True:
        if signals.commits_ahead:
            summary_parts.append("commits ahead")
        if signals.uncommitted:
            summary_parts.append("uncommitted changes")

    if signals.issue_state == "closed":
        summary_parts.append("issue closed")

    if signals.pr_state == teardown.MERGED:
        summary_parts.append("PR merged")
    elif signals.pr_state == teardown.NOT_MERGED:
        summary_parts.append("PR not merged")
    elif signals.pr_state == teardown.UNKNOWN:
        summary_parts.append("PR state unknown")

    signals_summary = "; ".join(summary_parts) if summary_parts else "no signals"

    # Classification logic (first match wins)

    # 1. ACTIVE: liveness is LIVE
    if signals.liveness == procs.LIVE:
        return FlowClassification(
            class_name=ACTIVE,
            reason="A claude process is actively working in this flow's worktree",
            suggestion=None,
            signals_summary=signals_summary
        )

    # 2. STALLED (liveness unknown): liveness is UNKNOWN
    if signals.liveness == procs.UNKNOWN:
        return FlowClassification(
            class_name=STALLED,
            reason="Liveness check failed (ps/lsof error or live fleet pid without pid_started)",
            suggestion=None,
            signals_summary=signals_summary
        )

    # 3. UNKNOWN: any other deciding signal is UNKNOWN
    if signals.pr_state == teardown.UNKNOWN:
        return FlowClassification(
            class_name=UNKNOWN,
            reason="Cannot determine PR state (gh query failed, rate limited, or ambiguous)",
            suggestion=None,
            signals_summary=signals_summary
        )

    if signals.issue_state is None:
        return FlowClassification(
            class_name=UNKNOWN,
            reason="Cannot determine issue state",
            suggestion=None,
            signals_summary=signals_summary
        )

    if signals.worktree_exists is None:
        return FlowClassification(
            class_name=UNKNOWN,
            reason="Cannot determine if worktree exists",
            suggestion=None,
            signals_summary=signals_summary
        )

    # 4. DEAD: PR MERGED by head, or issue closed, or worktree missing, or clean & nothing ahead
    if signals.pr_state == teardown.MERGED:
        worktree_path = state.get("worktree")
        if signals.worktree_exists and worktree_path:
            suggestion = f"/cleanup {worktree_path}"
        else:
            suggestion = "/expert-flow --abandon <flow-dir>"
        return FlowClassification(
            class_name=DEAD,
            reason="PR is merged by head SHA",
            suggestion=suggestion,
            signals_summary=signals_summary
        )

    if signals.issue_state == "closed":
        worktree_path = state.get("worktree")
        if signals.worktree_exists and worktree_path:
            suggestion = f"/cleanup {worktree_path}"
        else:
            suggestion = "/expert-flow --abandon <flow-dir>"
        return FlowClassification(
            class_name=DEAD,
            reason="Issue is closed",
            suggestion=suggestion,
            signals_summary=signals_summary
        )

    if not signals.worktree_exists:
        return FlowClassification(
            class_name=DEAD,
            reason="Worktree directory does not exist",
            suggestion="/expert-flow --abandon <flow-dir>",
            signals_summary=signals_summary
        )

    # Worktree exists and is not merged yet; check if clean & nothing ahead
    if not signals.commits_ahead and not signals.uncommitted:
        return FlowClassification(
            class_name=DEAD,
            reason="Worktree is clean with nothing ahead",
            suggestion="/cleanup <worktree>",
            signals_summary=signals_summary
        )

    # 5. STALLED (work to keep): everything else
    # (worktree present with commits ahead or uncommitted, PR not merged, issue open)
    return FlowClassification(
        class_name=STALLED,
        reason="Worktree has work in progress",
        suggestion="/expert-flow --resume <flow-dir>",
        signals_summary=signals_summary
    )
