#!/usr/bin/env python3
"""
Deterministic workflow CLI entrypoint (Phase 2 of ADR-0013).

Invoked by wrapper commands to plan and apply cleanup/merge operations.

Usage:
  python3 -m scripts.workflow.cli cleanup plan <target>
  python3 -m scripts.workflow.cli cleanup apply <plan_json_or_->
  python3 -m scripts.workflow.cli merge plan <arguments>
  python3 -m scripts.workflow.cli merge apply <plan_json>
"""

import sys
import argparse
import json
import dataclasses
from pathlib import Path

from . import cleanup, merge, shipit, checks
from .models import RepoCacheData


def _run_plan(plan_fn, arg):
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


def _run_apply(apply_fn, plan_json):
    """
    Execute an apply function and handle output/exit codes.

    Args:
        apply_fn: function that returns (result, error)
        plan_json: JSON string plan to apply
    """
    result, error = apply_fn(plan_json)
    if error:
        result.error = error
    print(json.dumps(result.to_dict()))
    if error or result.error:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic workflow CLI",
        prog="python3 -m scripts.workflow.cli"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command: cleanup or merge")

    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup a worktree")
    cleanup_subparsers = cleanup_parser.add_subparsers(dest="cleanup_action")

    cleanup_plan_parser = cleanup_subparsers.add_parser("plan", help="Plan cleanup")
    cleanup_plan_parser.add_argument("target", help="Target worktree path")

    cleanup_apply_parser = cleanup_subparsers.add_parser("apply", help="Apply cleanup plan")
    cleanup_apply_parser.add_argument(
        "plan",
        help="Plan JSON or '-' to read from stdin"
    )

    merge_parser = subparsers.add_parser("merge", help="Merge a PR")
    merge_subparsers = merge_parser.add_subparsers(dest="merge_action")

    merge_plan_parser = merge_subparsers.add_parser("plan", help="Plan merge")
    merge_plan_parser.add_argument(
        "arguments",
        help="PR number or worktree path"
    )

    merge_apply_parser = merge_subparsers.add_parser("apply", help="Apply merge plan")
    merge_apply_parser.add_argument("plan", help="Plan JSON")

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

    checks_parser = subparsers.add_parser("checks", help="Run checks")
    checks_subparsers = checks_parser.add_subparsers(dest="checks_action")

    checks_run_parser = checks_subparsers.add_parser("run", help="Run checks")
    checks_run_parser.add_argument(
        "cache",
        help="Repo cache JSON path or '-' to read from stdin"
    )

    args = parser.parse_args()

    if args.command == "cleanup":
        if args.cleanup_action == "plan":
            _run_plan(cleanup.plan_cleanup, args.target)
        elif args.cleanup_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            _run_apply(cleanup.apply_cleanup, plan_json)
        else:
            cleanup_parser.print_help()
            sys.exit(1)
    elif args.command == "merge":
        if args.merge_action == "plan":
            _run_plan(merge.plan_merge, args.arguments)
        elif args.merge_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            _run_apply(merge.apply_merge, plan_json)
        else:
            merge_parser.print_help()
            sys.exit(1)
    elif args.command == "shipit":
        if args.shipit_action == "plan":
            def plan_shipit_wrapper(_):
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
    elif args.command == "checks":
        if args.checks_action == "run":
            cache_input = args.cache
            if cache_input == "-":
                cache_input = sys.stdin.read()
            try:
                cache_data = json.loads(cache_input)
                repo_cache = RepoCacheData.from_dict(cache_data)
                result = checks.run_checks(
                    commands=repo_cache.commands,
                    repo_root=Path.cwd(),
                    parallelizable=repo_cache.parallelizable
                )
                print(json.dumps(result.to_dict()))
                if not result.all_passed:
                    sys.exit(1)
            except json.JSONDecodeError as e:
                output = {"success": False, "error": f"Invalid cache JSON: {e}"}
                print(json.dumps(output))
                sys.exit(1)
        else:
            checks_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
