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

from . import cleanup, merge


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

    args = parser.parse_args()

    if args.command == "cleanup":
        if args.cleanup_action == "plan":
            plan_obj, error = cleanup.plan_cleanup(args.target)
            if error:
                output = {
                    "success": False,
                    "error": str(error)
                }
            else:
                output = plan_obj.to_dict() if plan_obj else {"success": False}
            print(json.dumps(output))
        elif args.cleanup_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            result, error = cleanup.apply_cleanup(plan_json)
            if error:
                result.error = error
            print(json.dumps(result.to_dict()))
        else:
            cleanup_parser.print_help()
            sys.exit(1)
    elif args.command == "merge":
        if args.merge_action == "plan":
            plan_obj, error = merge.plan_merge(args.arguments)
            if error:
                output = {
                    "success": False,
                    "error": str(error)
                }
            else:
                output = plan_obj.to_dict() if plan_obj else {"success": False}
            print(json.dumps(output))
        elif args.merge_action == "apply":
            plan_json = args.plan
            if plan_json == "-":
                plan_json = sys.stdin.read()
            result, error = merge.apply_merge(plan_json)
            if error:
                result.error = error
            print(json.dumps(result.to_dict()))
        else:
            merge_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
