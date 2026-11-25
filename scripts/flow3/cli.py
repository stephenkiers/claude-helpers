"""Command line for Flow v3 (skeleton): start, resume, status, stop-screen, why."""

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional

from scripts.flow3 import engine, record, stages as stages_mod
from scripts.flow3.state import FlowDir
from scripts.flow3.types import Event


def _default_ctx(flow: FlowDir) -> Any:
    return flow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow3")
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="start a flow for an issue")
    start.add_argument("issue", type=int)
    start.add_argument("--repo-root", default=".")
    start.add_argument("--title", default="")
    resume = sub.add_parser("resume", help="resume a stopped or parked flow")
    resume.add_argument("repo")
    resume.add_argument("issue", type=int)
    status = sub.add_parser("status", help="one line per flow")
    status.add_argument("repo", nargs="?")
    screen = sub.add_parser("stop-screen", help="show a flow's stop")
    screen.add_argument("repo")
    screen.add_argument("issue", type=int)
    why = sub.add_parser("why", help="explain a decision or a file")
    why.add_argument("repo")
    why.add_argument("issue", type=int)
    why.add_argument("query")
    return parser


def _run(flow: FlowDir, event: Event, stages: Mapping[str, engine.StageFn],
         build_ctx: Callable[[FlowDir], Any]) -> int:
    action = engine.run_flow(flow, event, stages, build_ctx)
    if action.kind == "stop":
        print(record.stop_screen(flow))
        return 2
    print(f"{flow.name}: {action.kind}")
    return 0


def _need(repo: str, issue: int) -> Optional[FlowDir]:
    flow = FlowDir.find(repo, issue)
    if flow is None:
        print(f"no flow for issue {issue} in {repo}", file=sys.stderr)
    return flow


def main(argv: Optional[List[str]] = None,
         stages: Optional[Mapping[str, engine.StageFn]] = None,
         build_ctx: Optional[Callable[[FlowDir], Any]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    stage_map: Mapping[str, engine.StageFn] = stages if stages is not None else stages_mod.REGISTRY
    ctx_fn: Callable[[FlowDir], Any] = build_ctx if build_ctx is not None else _default_ctx

    if args.command == "start":
        root = Path(args.repo_root).resolve()
        title = args.title or f"issue {args.issue}"
        try:
            flow = FlowDir.create(root.name, args.issue, title, root)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return _run(flow, Event("start"), stage_map, ctx_fn)
    if args.command == "resume":
        found = _need(args.repo, args.issue)
        if found is None:
            return 1
        try:
            return _run(found, Event("resume"), stage_map, ctx_fn)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "status":
        for flow in FlowDir.all(args.repo):
            st = flow.flow_state()
            print(f"{flow.name}  {st.stage}  {st.status}  ${flow.total_cost():.2f}")
        return 0
    if args.command == "stop-screen":
        found = _need(args.repo, args.issue)
        if found is None:
            return 1
        print(record.stop_screen(found))
        return 0
    if args.command == "why":
        found = _need(args.repo, args.issue)
        if found is None:
            return 1
        print(record.why(found, args.query))
        return 0
    parser.print_usage(sys.stderr)
    return 1
