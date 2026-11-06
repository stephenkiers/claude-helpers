#!/usr/bin/env python3
"""
Deterministic workflow CLI entrypoint (Phase 2 of ADR-0013).

Invoked by wrapper commands to plan and apply cleanup/merge operations.

Usage:
  python3 -m scripts.workflow.cli cleanup plan <target>
  python3 -m scripts.workflow.cli cleanup apply <plan_json_or_->
  python3 -m scripts.workflow.cli merge plan <arguments>
  python3 -m scripts.workflow.cli merge apply <plan_json>
  python3 -m scripts.workflow.cli merge queue-init --cwd <worktree> [--step <cmd>]... [--write]
  python3 -m scripts.workflow.cli verify-queue sync [--cwd <dir>] [--branch <name>]
  python3 -m scripts.workflow.cli track plan --mode github --plan-file <path> --title <title> [--assignee <a>]
  python3 -m scripts.workflow.cli track apply <plan_json_or_->
  python3 -m scripts.workflow.cli track seed-cache --from <main> --to <worktree>
  python3 -m scripts.workflow.cli worktree detect-parent [--cwd <dir>]
  python3 -m scripts.workflow.cli checks resolve-check --worktree <dir> [--main-worktree <dir>]
  python3 -m scripts.workflow.cli checks ensure-check --worktree <dir> --main-worktree <dir>
  python3 -m scripts.workflow.cli checks validate [--mode minimum|full] [--worktree <dir>] [--main-worktree <dir>] [--force]
  python3 -m scripts.workflow.cli checks record-check-answer --main-worktree <dir> (--command <cmd> | --opt-out <reason>)
  python3 -m scripts.workflow.cli deps ensure --worktree <dir> --main-worktree <dir> [--base <ref>] [--approve-manifest] [--ignore-approvals]
  python3 -m scripts.workflow.cli heal probe --worktree <dir> --main-worktree <dir> [--base <ref>] --flow-dir <dir> --issue <num>
  python3 -m scripts.workflow.cli heal run --worktree <dir> --main-worktree <dir> [--base <ref>] --flow-dir <dir> --issue <num> --tool <name> [--tail-file <path>]

Exit codes for `checks resolve-check`: 0 when a command resolved; 1 whenever failure_reason is
set, including an explicit checkOptOut. A caller that must tell opt-out from not-configured
reads `source` ("opt-out" vs null) from the JSON, not the exit code.

Exit codes for `deps ensure`: 0 current or installed; 1 failed; 3 ambiguous lockfiles;
4 manifest-changed (needs approval); 5 skipped (no lockfile and no commands.install).

Exit codes for `heal probe` and `heal run`: 0 once the arguments parse, whatever the outcome. Refusals
and internal errors are reported inside the printed JSON, never as a non-zero exit. Both print one JSON
object on stdout. argparse usage errors keep exit 2; a bare `heal` prints help and exits 1.
"""

import sys
import argparse
import json
from pathlib import Path
from typing import Callable, Any, Tuple

from . import cleanup, merge, shipit, checks, deps, track, verify_queue, issue_comments, heal
from .models import MODES, parse_mode
from .providers import GithubProvider, LocalProvider
from .providers.base import Provider, ProviderError
from . import worktrees


def _validate_queue_started_at(value: str) -> float:
    """
    Validate --queue-started-at as a finite non-negative float.

    Args:
        value: string representation of the timestamp

    Returns:
        float value if valid

    Raises:
        argparse.ArgumentTypeError: if value is invalid
    """
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--queue-started-at must be a number, not '{value}'")

    # Check for nan and infinity
    if f != f:  # NaN check (NaN != NaN is true)
        raise argparse.ArgumentTypeError("--queue-started-at must be finite (not NaN)")
    if f == float('inf') or f == float('-inf'):
        raise argparse.ArgumentTypeError("--queue-started-at must be finite (not inf)")
    if f < 0:
        raise argparse.ArgumentTypeError("--queue-started-at must be non-negative")

    return f


def _run_plan(plan_fn: Callable[[Any], Tuple[Any, Any]], arg: Any) -> None:
    """
    Execute a plan function and handle output/exit codes.

    Args:
        plan_fn: function that returns (plan_obj, error)
        arg: argument to pass to plan_fn
    """
    plan_obj, error = plan_fn(arg)
    if error:
        output = {"success": False, "error": str(error)}
    else:
        output = plan_obj.to_dict() if plan_obj else {"success": False}
    print(json.dumps(output))
    if error:
        sys.exit(1)


def _run_apply(apply_fn: Callable[[str], Tuple[Any, Any]], plan_json: str) -> None:
    """
    Execute an apply function and handle output/exit codes.

    Exit code contract (D4):
    - Exit 0: apply succeeded (result.error is None or falsy)
    - Exit 1: apply failed (result.error is truthy or apply_fn returned an error)

    The full output is built before printing, ensuring all diagnostics are visible
    regardless of exit code.

    Args:
        apply_fn: function that returns (result, error)
        plan_json: JSON string plan to apply
    """
    result, error = apply_fn(plan_json)
    if error:
        result.error = error
    output = result.to_dict()
    print(json.dumps(output))
    if error or result.error:
        sys.exit(1)


def main() -> None:
    # Runtime guard: scripts/workflow/ requires python3 3.8+
    if sys.version_info < (3, 8):
        print(
            "Error: Python 3.8 or later is required by scripts/workflow/ "
            "(used by /track-and-start, /shipit, /cleanup, /merge-and-cleanup).",
            file=sys.stderr
        )
        print(
            "Current Python version: {}.{}.{}".format(*sys.version_info[:3]),
            file=sys.stderr
        )
        print(
            "Install a newer python3 via https://www.python.org/downloads/ or your platform package manager "
            "(e.g., 'brew install python3' on macOS).",
            file=sys.stderr
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Deterministic workflow CLI",
        prog="python3 -m scripts.workflow.cli"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command: cleanup or merge")

    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup a worktree")
    cleanup_subparsers = cleanup_parser.add_subparsers(dest="cleanup_action")

    cleanup_plan_parser = cleanup_subparsers.add_parser("plan", help="Plan cleanup")
    cleanup_plan_parser.add_argument("target", help="Target worktree path")

    cleanup_apply_parser = cleanup_subparsers.add_parser(
        "apply",
        help="Apply cleanup plan (exit 0 on success, exit 1 on failure; all output printed before exit)"
    )
    cleanup_apply_parser.add_argument(
        "plan",
        help="Plan JSON or '-' to read from stdin"
    )
    cleanup_apply_parser.add_argument(
        "--queue-started-at",
        type=_validate_queue_started_at,
        default=None,
        help="Queue start timestamp for skip proof (optional)"
    )

    cleanup_render_parser = cleanup_subparsers.add_parser("render-validation", help="Render validation result")
    cleanup_render_parser.add_argument(
        "input",
        help="Input JSON or '-' to read from stdin"
    )

    merge_parser = subparsers.add_parser("merge", help="Merge a PR")
    merge_subparsers = merge_parser.add_subparsers(dest="merge_action")

    merge_plan_parser = merge_subparsers.add_parser("plan", help="Plan merge")
    merge_plan_parser.add_argument(
        "arguments", nargs="?", default=None,
        help="PR number or worktree path (defaults to detecting from the current directory)"
    )
    merge_plan_parser.add_argument(
        "--cwd", default=None,
        help="Working directory for PR/worktree resolution (defaults to current directory)"
    )

    merge_apply_parser = merge_subparsers.add_parser("apply", help="Apply merge plan")
    merge_apply_parser.add_argument("plan", help="Plan JSON")

    merge_queue_init_parser = merge_subparsers.add_parser(
        "queue-init", help="Propose (or with --write, create) a merge-queue config"
    )
    merge_queue_init_parser.add_argument(
        "--cwd", required=True, help="Worktree of the repo to set up"
    )
    merge_queue_init_parser.add_argument(
        "--step", action="append", default=None,
        help="Gate step command (repeatable); overrides the detected steps"
    )
    merge_queue_init_parser.add_argument(
        "--write", action="store_true", help="Create the config (never overwrites)"
    )

    vq_parser = subparsers.add_parser("verify-queue", help="Verify-queue operations")
    vq_subparsers = vq_parser.add_subparsers(dest="vq_action")
    vq_sync_parser = vq_subparsers.add_parser(
        "sync", help="Enqueue pending rulings from claude-action-plans (idempotent)"
    )
    vq_sync_parser.add_argument(
        "--cwd", default=None, help="Directory inside the repo (defaults to current directory)"
    )
    vq_sync_parser.add_argument(
        "--branch", default=None,
        help="Only sync reviews of this branch (defaults to every review for the repo)"
    )

    shipit_parser = subparsers.add_parser("shipit", help="Commit, push, and create/update PR")
    shipit_subparsers = shipit_parser.add_subparsers(dest="shipit_action")

    shipit_plan_parser = shipit_subparsers.add_parser("plan", help="Plan shipit")
    shipit_plan_parser.add_argument(
        "message_path",
        help="Path to commit message file"
    )
    shipit_plan_parser.add_argument(
        "--body-file",
        help="Path to PR body file (optional)"
    )
    shipit_plan_parser.add_argument(
        "--title",
        help="PR title (optional)"
    )

    shipit_apply_parser = shipit_subparsers.add_parser("apply", help="Apply shipit plan")
    shipit_apply_parser.add_argument(
        "plan",
        help="Plan JSON or '-' to read from stdin"
    )

    track_parser = subparsers.add_parser("track", help="Create issue and worktree")
    track_subparsers = track_parser.add_subparsers(dest="track_action")

    track_plan_parser = track_subparsers.add_parser("plan", help="Plan track")
    track_plan_parser.add_argument(
        "--mode",
        choices=["github", "local"],
        default="github",
        help="Mode: github or local (default: github)"
    )
    track_plan_parser.add_argument(
        "--plan-file",
        required=True,
        help="Path to plan file"
    )
    track_plan_parser.add_argument(
        "--title",
        required=True,
        help="Issue title"
    )
    track_plan_parser.add_argument(
        "--assignee",
        help="Assignee (optional)"
    )
    track_plan_parser.add_argument(
        "--tracker-path",
        help="Path to local issues.json (local mode only; defaults to <project-root>/issues.json)"
    )
    track_plan_parser.add_argument(
        "--plans-dir",
        help="Path to plans directory (local mode only; defaults to <project-root>/plans)"
    )

    track_apply_parser = track_subparsers.add_parser("apply", help="Apply track plan")
    track_apply_parser.add_argument(
        "plan",
        help="Plan JSON or '-' to read from stdin"
    )

    track_seed_cache_parser = track_subparsers.add_parser("seed-cache", help="Seed cache files from main worktree")
    track_seed_cache_parser.add_argument(
        "--from",
        dest="from_worktree",
        required=True,
        help="Main worktree path to copy from"
    )
    track_seed_cache_parser.add_argument(
        "--to",
        dest="to_worktree",
        required=True,
        help="New worktree path to copy to"
    )

    checks_parser = subparsers.add_parser("checks", help="Run checks")
    checks_subparsers = checks_parser.add_subparsers(dest="checks_action")

    checks_resolve_check_parser = checks_subparsers.add_parser(
        "resolve-check",
        help="Resolve check command and timeout",
        description=(
            "Print JSON with command, source, failure_reason, opt_out_reason, timeout_secs, "
            "and timeout_source. Exit 1 whenever failure_reason is set, including an explicit "
            "checkOptOut. A caller that must tell opt-out from not-configured must read `source` "
            "(\"opt-out\" vs null) from the JSON, not the exit code."
        ),
    )
    checks_resolve_check_parser.add_argument(
        "--worktree",
        required=True,
        help="Worktree path to resolve check command for"
    )
    checks_resolve_check_parser.add_argument(
        "--main-worktree",
        default=None,
        help=(
            "Main worktree path for fallback (optional). Exit 1 means failure_reason is set "
            "(opt-out or not configured); read `source` from the JSON to tell them apart."
        )
    )
    checks_resolve_check_parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="full",
        help="Check mode: 'minimum' or 'full' (default: full)"
    )

    checks_ensure_check_parser = checks_subparsers.add_parser(
        "ensure-check",
        help="Resolve the check, or derive it from package.json into the main repo-cache (never asks)",
        description=(
            "Print CHECK_SOURCE (first line), CHECK_ACTION (resolved | detected | opt-out | unresolved), "
            "then CHECK_COMMAND or OPT_OUT, or SUGGESTION for unresolved. Exit 0 for every state; "
            "exit 1 only when the main repo-cache.json write fails."
        ),
    )
    checks_ensure_check_parser.add_argument("--worktree", required=True, help="Step worktree path")
    checks_ensure_check_parser.add_argument("--main-worktree", required=True, help="Main worktree path")

    checks_record_answer_parser = checks_subparsers.add_parser(
        "record-check-answer",
        help="Record a human's check answer in the main project.yaml, then verify it resolves",
    )
    checks_record_answer_parser.add_argument("--main-worktree", required=True, help="Main worktree path")
    record_answer_group = checks_record_answer_parser.add_mutually_exclusive_group(required=True)
    record_answer_group.add_argument(
        "--command", dest="check_command", default=None, help="Check command to record as commands.check"
    )
    record_answer_group.add_argument("--opt-out", default=None, help="Reason to record as checkOptOut")

    checks_validate_parser = checks_subparsers.add_parser(
        "validate",
        help="Validate checks idempotently with caching"
    )
    checks_validate_parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="full",
        help="Check mode: 'minimum' or 'full' (default: full)"
    )
    checks_validate_parser.add_argument(
        "--worktree",
        default=".",
        help="Worktree directory (default: cwd)"
    )
    checks_validate_parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run, ignoring cache"
    )
    checks_validate_parser.add_argument(
        "--main-worktree",
        default=None,
        help="Main worktree path (command and timeout sources; default: none)"
    )

    deps_parser = subparsers.add_parser("deps", help="Dependency install precondition")
    deps_subparsers = deps_parser.add_subparsers(dest="deps_action")

    deps_ensure_parser = deps_subparsers.add_parser(
        "ensure",
        help="Make dependencies current for the worktree's lockfile",
        description=(
            "Print DEPS: <status>[: detail] then, on failure, a redacted tail of the install output. "
            "Exit 0 current/installed, 1 failed, 3 ambiguous, 4 manifest-changed, 5 skipped."
        ),
    )
    deps_ensure_parser.add_argument("--worktree", required=True, help="Step worktree path")
    deps_ensure_parser.add_argument("--main-worktree", required=True, help="Main worktree path (commands.install source)")
    deps_ensure_parser.add_argument(
        "--base", default=None, help="Flow base ref; enables the manifest gate against merge-base(base, HEAD)"
    )
    deps_ensure_parser.add_argument(
        "--approve-manifest", action="store_true",
        help="Approve the current package.json/lockfile hash so the manifest gate clears"
    )
    deps_ensure_parser.add_argument(
        "--ignore-approvals", action="store_true",
        help="Ignore stored manifest approval (used by heal tools)"
    )

    heal_parser = subparsers.add_parser("heal", help="Bounded heal harness for fleet")
    heal_subparsers = heal_parser.add_subparsers(dest="heal_action")

    heal_probe_parser = heal_subparsers.add_parser(
        "probe",
        help="Probe for a tool to heal a stopped ticket (read-only)",
        description="Probe for a tool to heal a stopped ticket (read-only, no cap spent). Prints JSON: {tool, timeout_secs, stop_key} or {line, stop_key}, or {stop_key: null} when there is nothing to do. Exits 0 once the arguments parse, whatever the outcome (refusals and internal errors are in the JSON); argparse usage errors exit 2."
    )
    heal_probe_parser.add_argument("--worktree", required=True, help="Step worktree path")
    heal_probe_parser.add_argument("--main-worktree", required=True, help="Main worktree path")
    heal_probe_parser.add_argument("--base", default=None, help="Flow base ref")
    heal_probe_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    heal_probe_parser.add_argument("--issue", type=int, required=True, help="Issue number")

    heal_run_parser = heal_subparsers.add_parser(
        "run",
        help="Run a heal tool and return the result",
        description="Run a heal tool and return the result. Prints JSON: {tool, status, reason, changed, recommendation, stop_key}. Exits 0 once the arguments parse, whatever the outcome (refusals and internal errors are in the JSON); argparse usage errors exit 2."
    )
    heal_run_parser.add_argument("--worktree", required=True, help="Step worktree path")
    heal_run_parser.add_argument("--main-worktree", required=True, help="Main worktree path")
    heal_run_parser.add_argument("--base", default=None, help="Flow base ref")
    heal_run_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    heal_run_parser.add_argument("--issue", type=int, required=True, help="Issue number")
    heal_run_parser.add_argument("--tool", required=True, help="Tool name to run")
    heal_run_parser.add_argument("--tail-file", default=None, help="File to write output tail (mode 0600)")

    worktree_parser = subparsers.add_parser("worktree", help="Worktree layout detection")
    worktree_subparsers = worktree_parser.add_subparsers(dest="worktree_action")

    worktree_detect_parent_parser = worktree_subparsers.add_parser(
        "detect-parent",
        help="Print the detected worktree parent directory as plain text (exit 1 with ERROR on stderr on failure)"
    )
    worktree_detect_parent_parser.add_argument(
        "--cwd", default=None,
        help="Directory inside the repo (defaults to current directory)"
    )

    issue_comments_parser = subparsers.add_parser("issue-comments", help="Fetch and filter issue comments")
    issue_comments_subparsers = issue_comments_parser.add_subparsers(dest="issue_comments_action")

    issue_comments_fetch_parser = issue_comments_subparsers.add_parser(
        "fetch",
        help="Fetch issue comments with filtering",
        description="Fetch issue comments with filtering. Exit codes: 0 ok (comments_status ok/unavailable/unsupported with no error), 2 usage/parse/unexpected error, 3 provider error."
    )
    issue_comments_fetch_parser.add_argument(
        "--issue", required=True,
        help="Issue number or GitHub URL (e.g., 123 or https://github.com/owner/repo/issues/123)"
    )
    issue_comments_fetch_parser.add_argument(
        "--cwd", required=True,
        help="Working directory of the target repo"
    )
    issue_comments_fetch_parser.add_argument(
        "--trusted-only", action="store_true",
        help="Withhold unverified (non-OWNER/COLLABORATOR) comments"
    )
    issue_comments_fetch_parser.add_argument(
        "--flow-dir", default=None,
        help="Flow directory (reads acknowledged ids and plan fetch record)"
    )
    issue_comments_fetch_parser.add_argument(
        "--format", choices=["json", "markdown"], default="json",
        help="Output format (default: json)"
    )
    issue_comments_fetch_parser.add_argument(
        "--allow-other-repo", action="store_true",
        help="Allow URLs for repos other than the target"
    )
    issue_comments_fetch_parser.add_argument(
        "--mode", choices=["github", "local"], default="github",
        help="Provider to fetch through: github (default) or local (no comment channel: comments_status unsupported)"
    )

    issue_comments_finalize_parser = issue_comments_subparsers.add_parser(
        "finalize",
        help="Apply scout verdicts and re-apply budgets",
        description="Apply scout verdicts and re-apply budgets. Exit codes: 0 ok, 2 error."
    )
    issue_comments_finalize_parser.add_argument(
        "--fetched", required=True,
        help="Path to fetched JSON file from issue-comments fetch"
    )
    issue_comments_finalize_parser.add_argument(
        "--verdicts", required=True,
        help="Directory containing scout verdict JSON files"
    )
    issue_comments_finalize_parser.add_argument(
        "--format", choices=["json", "markdown"], default="json",
        help="Output format (default: json)"
    )

    args = parser.parse_args()

    if args.command == "cleanup":
        if args.cleanup_action == "plan":
            _run_plan(cleanup.plan_cleanup, args.target)
        elif args.cleanup_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()

            def apply_cleanup_wrapper(pj: str) -> Tuple[Any, Any]:
                return cleanup.apply_cleanup(pj, queue_started_at=args.queue_started_at)

            _run_apply(apply_cleanup_wrapper, plan_json)
        elif args.cleanup_action == "render-validation":
            input_json = args.input
            if input_json == "-":
                input_json = sys.stdin.read()
            try:
                data = json.loads(input_json)

                # Validate input shape: must be a dict
                if not isinstance(data, dict):
                    print("VALIDATION=fail — verdict unreadable (fail-closed)")
                    sys.exit(1)

                from . import validation

                # Coerce notes and steps to lists if present
                if "notes" in data and not isinstance(data["notes"], list):
                    data["notes"] = []
                if "queue_tested_steps" in data and not isinstance(data["queue_tested_steps"], list):
                    data["queue_tested_steps"] = []

                reason = data.get("validation_reason", "")

                verdict = validation.ValidationVerdict.parse(data.get("validation"))
                notes = data.get("notes", [])
                steps = data.get("queue_tested_steps", [])
                headline = validation.render_headline(verdict, reason, notes, steps)
                print(headline)
                for failure in data.get("validation_failures", []):
                    # Indent each line of the failure
                    for line in failure.split("\n"):
                        print(f"  {line}")
            except json.JSONDecodeError:
                print("VALIDATION=fail — verdict unreadable (fail-closed)")
            except Exception as e:
                print(f"VALIDATION=fail — verdict unreadable (fail-closed): {e}")
        else:
            cleanup_parser.print_help()
            sys.exit(1)
    elif args.command == "merge":
        if args.merge_action == "plan":
            # Custom handler for merge plan to support --cwd and exit 3 on queue refusal
            plan_obj, error = merge.plan_merge(
                args.arguments,
                cwd=Path(args.cwd) if args.cwd else None
            )
            if error:
                output = {"success": False, "error": str(error)}
            else:
                output = plan_obj.to_dict() if plan_obj else {"success": False}
            print(json.dumps(output))
            if error:
                sys.exit(1)
            # Check if queue guard refused
            if plan_obj and plan_obj.queue.get("decision") == "refuse":
                sys.exit(3)
        elif args.merge_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            _run_apply(merge.apply_merge, plan_json)
        elif args.merge_action == "queue-init":
            try:
                init_result = merge.queue_init(Path(args.cwd), steps=args.step, write=args.write)
            except RuntimeError as e:
                print(json.dumps({"success": False, "error": str(e)}))
                sys.exit(1)
            print(json.dumps(init_result))
        else:
            merge_parser.print_help()
            sys.exit(1)
    elif args.command == "verify-queue":
        if args.vq_action == "sync":
            try:
                sync_result = verify_queue.sync(
                    cwd=Path(args.cwd) if args.cwd else None, branch=args.branch
                )
            except (RuntimeError, OSError) as e:
                print(json.dumps({"success": False, "error": str(e)}))
                sys.exit(1)
            print(json.dumps(sync_result.to_dict()))
        else:
            vq_parser.print_help()
            sys.exit(1)
    elif args.command == "shipit":
        if args.shipit_action == "plan":
            def plan_shipit_wrapper(_: Any) -> Tuple[Any, Any]:
                return shipit.plan_shipit(
                    commit_message_path=args.message_path,
                    pr_body_path=args.body_file,
                    pr_title=args.title
                )
            _run_plan(plan_shipit_wrapper, None)
        elif args.shipit_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            _run_apply(shipit.apply_shipit, plan_json)
        else:
            shipit_parser.print_help()
            sys.exit(1)
    elif args.command == "track":
        if args.track_action == "plan":
            # Read plan file
            try:
                plan_file = Path(args.plan_file)
                plan_content = plan_file.read_text()
            except (OSError, IOError) as e:
                output = {"success": False, "error": f"Failed to read plan file: {e}"}
                print(json.dumps(output))
                sys.exit(1)

            # Construct provider based on mode
            provider: Provider
            if args.mode == "local":
                # For local mode, determine tracker and plans paths
                project_root = worktrees.detect_project_root()
                if not project_root:
                    output = {"success": False, "error": "Could not detect project root for local mode"}
                    print(json.dumps(output))
                    sys.exit(1)

                tracker_path = Path(args.tracker_path) if args.tracker_path else Path(project_root) / "issues.json"
                plans_dir = Path(args.plans_dir) if args.plans_dir else Path(project_root) / "plans"
                provider = LocalProvider(tracker_path, plans_dir)
            else:
                provider = GithubProvider()

            def plan_track_wrapper(_: Any) -> Tuple[Any, Any]:
                return track.plan_track(
                    provider=provider,
                    plan_content=plan_content,
                    title=args.title,
                    mode=args.mode,
                    assignee=args.assignee
                )

            _run_plan(plan_track_wrapper, None)

        elif args.track_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()

            # Construct provider from plan (same as in plan step)
            try:
                plan_data = json.loads(plan_json)
                mode = plan_data.get("mode", "github")
            except json.JSONDecodeError:
                output = {"success": False, "error": "Could not parse plan JSON to determine mode"}
                print(json.dumps(output))
                sys.exit(1)

            provider_apply: Provider
            if mode == "local":
                # Prefer the project_root already serialized in the plan (matches
                # apply_track's own approach of reading plan.main_worktree rather
                # than re-detecting), so apply is consistent with the paths plan
                # time actually used. Only fall back to a fresh detect when the
                # plan predates this field or the field is empty.
                # NOTE: --tracker-path/--plans-dir overrides used at plan time are
                # still not threaded through the plan JSON here; apply always
                # derives tracker_path/plans_dir from project_root. Fixing that
                # fully requires a plan schema change and is left for Phase 3b.
                project_root = plan_data.get("project_root") or worktrees.detect_project_root()
                if not project_root:
                    output = {"success": False, "error": "Could not detect project root for local mode"}
                    print(json.dumps(output))
                    sys.exit(1)

                tracker_path = Path(project_root) / "issues.json"
                plans_dir = Path(project_root) / "plans"
                provider_apply = LocalProvider(tracker_path, plans_dir)
            else:
                provider_apply = GithubProvider()

            def apply_track_wrapper(pj: str) -> Tuple[Any, Any]:
                return track.apply_track(provider=provider_apply, plan_json=pj)

            _run_apply(apply_track_wrapper, plan_json)

        elif args.track_action == "seed-cache":
            from .cache import seed_worktree_cache
            try:
                main_wt = Path(args.from_worktree)
                new_wt = Path(args.to_worktree)
                seeded, skipped, seed_error = seed_worktree_cache(main_wt, new_wt)
                output = {
                    "seeded": seeded,
                    "skipped": skipped,
                    "error": seed_error
                }
                print(json.dumps(output))
                if seed_error:
                    sys.exit(1)
            except Exception as e:
                output = {
                    "seeded": [],
                    "skipped": [],
                    "error": str(e)
                }
                print(json.dumps(output))
                sys.exit(1)
        else:
            track_parser.print_help()
            sys.exit(1)
    elif args.command == "worktree":
        if args.worktree_action == "detect-parent":
            cwd_arg = Path(args.cwd) if args.cwd else None
            try:
                detected_parent = worktrees.detect_worktree_parent(cwd=cwd_arg)
            except worktrees.WorktreeParentError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)
            if not detected_parent:
                print("ERROR: no worktree parent could be determined", file=sys.stderr)
                sys.exit(1)
            print(detected_parent)
        else:
            worktree_parser.print_help()
            sys.exit(1)
    elif args.command == "checks":
        if args.checks_action == "resolve-check":
            try:
                worktree_path = Path(args.worktree)
                main_worktree_path = Path(args.main_worktree) if args.main_worktree else None
                mode = parse_mode(args.mode or "full") or "full"
                resolution = checks.resolve_check_detail(worktree_path, main_worktree_path, mode=mode)
                source_kind: checks.ConfigKind = "project.yaml" if resolution.source == "project.yaml" else "repo-cache"
                timeout_secs, timeout_source = checks.resolve_check_timeout(
                    worktree_path, main_worktree_path, resolution.source_file, source_kind,
                    resolution.source_scope,
                )
                output = {
                    "command": resolution.command,
                    "source": resolution.source,
                    "failure_reason": resolution.failure_reason,
                    "opt_out_reason": resolution.opt_out_reason,
                    "timeout_secs": timeout_secs,
                    "timeout_source": timeout_source,
                    "fell_back_to_full": getattr(resolution, "fell_back_to_full", False),
                }
                print(json.dumps(output))
                if resolution.failure_reason:
                    sys.exit(1)
            except Exception as e:
                output = {
                    "command": None,
                    "source": None,
                    "failure_reason": str(e),
                    "opt_out_reason": None,
                    "timeout_secs": None,
                    "timeout_source": None,
                    "fell_back_to_full": False,
                }
                print(json.dumps(output))
                sys.exit(1)
        elif args.checks_action == "ensure-check":
            ensured = checks.ensure_check(Path(args.worktree), Path(args.main_worktree))
            if ensured.action == "error":
                print("CHECK_SOURCE: unresolved")
                print("CHECK_ACTION: error")
                print(f"ERROR: {ensured.error}")
                sys.exit(1)
            print(f"CHECK_SOURCE: {ensured.source if ensured.source is not None else 'unresolved'}")
            print(f"CHECK_ACTION: {ensured.action}")
            if ensured.command is not None:
                print(f"CHECK_COMMAND: {ensured.command}")
            if ensured.opt_out_reason is not None:
                print(f"OPT_OUT: {ensured.opt_out_reason}")
            if ensured.suggestion is not None:
                print(f"SUGGESTION: {ensured.suggestion} (from CLAUDE.md, unverified)")
        elif args.checks_action == "record-check-answer":
            ok, message = checks.record_check_answer(
                Path(args.main_worktree), command=args.check_command, opt_out=args.opt_out
            )
            print(message)
            if not ok:
                sys.exit(1)
        elif args.checks_action == "validate":
            try:
                worktree = Path(args.worktree).resolve()
                main_worktree_arg = Path(args.main_worktree) if args.main_worktree else None
                validate_result: Any = checks.validate(
                    worktree=worktree,
                    mode=args.mode,
                    force=args.force,
                    main_worktree=main_worktree_arg,
                )
                print(json.dumps(validate_result))
                # Exit 0 if passed, 1 if failed, 2 for a config or usage error (status "error")
                if validate_result.get("status") == "error":
                    exit_code = 2
                else:
                    exit_code = 0 if (validate_result.get("status") == "passed") else 1
                sys.exit(exit_code)
            except Exception as e:
                requested = parse_mode(args.mode) if isinstance(args.mode, str) else None
                print(json.dumps(checks.validate_error_result(str(e), requested or "full")))
                sys.exit(2)
        else:
            checks_parser.print_help()
            sys.exit(1)
    elif args.command == "issue-comments":
        if args.issue_comments_action == "fetch":
            try:
                ic_result, ic_error = issue_comments.fetch_issue_comments(
                    issue_ref=args.issue,
                    cwd=Path(args.cwd).resolve(),
                    trusted_only=args.trusted_only,
                    flow_dir=Path(args.flow_dir).resolve() if args.flow_dir else None,
                    allow_other_repo=args.allow_other_repo,
                    mode=args.mode
                )
                if ic_error:
                    # Every error exit carries comments_status (unavailable when there is no result)
                    output = ic_result.to_dict() if ic_result else {}
                    output["comments_status"] = ic_result.comments_status if ic_result else "unavailable"
                    output["error"] = str(ic_error)
                    print(json.dumps(output))
                    sys.exit(3 if isinstance(ic_error, ProviderError) else 2)
                else:
                    if ic_result is None:
                        output = {"error": "Unexpected: no result or error"}
                        print(json.dumps(output))
                        sys.exit(2)
                    if args.format == "markdown":
                        print(issue_comments.render_markdown(ic_result))
                    else:
                        print(json.dumps(ic_result.to_dict()))
            except Exception as e:
                output = {"comments_status": "unavailable", "error": str(e)}
                print(json.dumps(output))
                sys.exit(2)
        elif args.issue_comments_action == "finalize":
            try:
                # Read fetched JSON file
                fetched_path = Path(args.fetched)
                if not fetched_path.exists():
                    print(json.dumps({"error": f"Fetched file not found: {fetched_path}"}))
                    sys.exit(2)

                with open(fetched_path) as f:
                    fetched_data = json.load(f)

                # Call finalize
                finalize_result, finalize_error = issue_comments.finalize_comments(
                    fetched=fetched_data,
                    verdicts_dir=Path(args.verdicts)
                )

                if finalize_error:
                    output = finalize_result.to_dict() if finalize_result else {}
                    output["error"] = str(finalize_error)
                    print(json.dumps(output))
                    sys.exit(2)
                else:
                    if finalize_result is None:
                        output = {"error": "Unexpected: no result or error"}
                        print(json.dumps(output))
                        sys.exit(2)
                    if args.format == "markdown":
                        print(issue_comments.render_markdown(finalize_result))
                    else:
                        print(json.dumps(finalize_result.to_dict()))
            except json.JSONDecodeError as e:
                output = {"comments_status": "unavailable", "error": f"Invalid fetched JSON: {e}"}
                print(json.dumps(output))
                sys.exit(2)
            except Exception as e:
                output = {"comments_status": "unavailable", "error": str(e)}
                print(json.dumps(output))
                sys.exit(2)
        else:
            issue_comments_parser.print_help()
            sys.exit(1)
    elif args.command == "deps":
        if args.deps_action == "ensure":
            try:
                deps_result = deps.ensure_deps(
                    Path(args.worktree),
                    Path(args.main_worktree),
                    base=args.base,
                    approve_manifest=args.approve_manifest,
                    ignore_approvals=getattr(args, "ignore_approvals", False),
                )
            except KeyboardInterrupt:
                print("DEPS: failed: interrupted")
                sys.exit(130)
            except SystemExit as interrupted:
                # SIGTERM turns into SystemExit(143) inside the install; still print a DEPS line.
                if isinstance(interrupted.code, int) and interrupted.code >= 128:
                    print("DEPS: failed: interrupted")
                raise
            for out_line in deps_result.output_lines():
                print(out_line)
            sys.exit(deps_result.exit_code)
        else:
            deps_parser.print_help()
            sys.exit(1)
    elif args.command == "heal":
        if args.heal_action == "probe":
            heal_result = heal.cli_probe(
                Path(args.worktree),
                Path(args.main_worktree),
                args.base,
                Path(args.flow_dir),
                args.issue,
            )
            print(json.dumps(heal_result))
            sys.exit(0)
        elif args.heal_action == "run":
            tail_file = Path(args.tail_file) if args.tail_file else None
            heal_result = heal.cli_run(
                Path(args.worktree),
                Path(args.main_worktree),
                args.base,
                Path(args.flow_dir),
                args.issue,
                args.tool,
                tail_file,
            )
            print(json.dumps(heal_result))
            sys.exit(0)
        else:
            heal_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
