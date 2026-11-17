"""
CLI edge for the v2 driver (Layer 2 Step 7): init, resume, status --json, list, and the loop subcommands
start, next, answer and clear-live, which route to loop.py for v2 flows.

Layer 2 removes the skeleton: start requires --flow (create the flow with init). The loop subcommands take the
state lock once through loop.py; a refusal exits 1 on stderr and never prints NEXT.

Write order for every next and start is fixed by loop.py: state.json first (the commit point), then the nonce-named
step prompt (SPAWN only) and directive file, one driver-log.jsonl line, and then exactly one stdout line,
`NEXT: <VERB> <absolute directive file>`. Diagnostics go to stderr.

Exit codes and stdout (init prints `INIT flow=<dir> run_id=<id>`; resume prints `RESUME rule=<rule> outcome=<outcome>`):
- 0: success (init, resume with acted-on outcome, status, list)
- 1: refusal (locked, v1-owned, skeleton-owned, schema-invalid, no-state, exists, verifier-timeout)
- 2: usage error (FlowPathError, bad flag, mismatched --flow args, path inside neither root)
- 3: not-yet-built (fail-closed, distinct from refusal; for rules not yet built in Layer 1)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .flowdir import FlowPathError, flows_v2_root, open_flow_dir, repo_key as _repo_key, resolve_read_flow_dir, validate_v2_flow_path
from .loop import LoopRefusal, run_answer, run_clear_live, run_next, run_start
from .model import MODES, WAIT_MODES, assert_never
from .receipts import VerifierTimeout
from .resume import CascadeExhausted, ResumeFacts, decide as resume_decide
from .state import (
    InitSpec,
    InvalidState,
    NoState,
    Refusal,
    SkeletonForeign,
    StateSchemaError,
    V1Foreign,
    V2Owned,
    classify_state,
    init_state,
    mutating,
    no_state_refusal,
    read_flow,
    v1_owned_refusal,
)


class UsageError(Exception):
    """A caller error that is not a path problem (for example a mismatched flag). Exit 2, nothing on stdout."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_text(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _now_flow_root() -> Path:
    return flows_v2_root(Path.home())


def _refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _usage_error(message: str) -> int:
    """A usage error (exit 2): a flow path inside neither root, or a malformed path. Not a refusal."""
    print(message, file=sys.stderr)
    return 2


def _v2_flow(args: argparse.Namespace) -> Path:
    """The flow dir a v2 subcommand names, validated under the flows-v2 root (a bad path exits 2)."""
    return open_flow_dir(_now_flow_root(), Path(args.flow))


def _run_loop(call: Callable[[], str], *, to_stdout: bool) -> int:
    """Run one loop call. A refusal exits 1 on stderr; a success prints its line (NEXT on stdout for next and start)."""
    try:
        text = call()
    except Refusal as exc:
        return _refuse(str(exc))
    except LoopRefusal as exc:
        return _refuse(f"refused: {exc}")
    print(text, file=sys.stdout if to_stdout else sys.stderr)
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    """start --flow: the first `next` of an initialized v2 flow. Creating a flow dir is `init`'s job (Layer 2, D8)."""
    if args.flow is None:
        raise UsageError("start needs --flow: create the flow with init, then start it")
    flow = _v2_flow(args)
    return _run_loop(lambda: run_start(flow), to_stdout=True)


def _cmd_next(args: argparse.Namespace) -> int:
    """next: one mutating loop step. Single-caller invariant: the orchestrator runs one next at a time."""
    flow = _v2_flow(args)
    return _run_loop(lambda: run_next(flow, args.after), to_stdout=True)


def _cmd_answer(args: argparse.Namespace) -> int:
    flow = _v2_flow(args)
    return _run_loop(lambda: run_answer(flow, args.text), to_stdout=False)


def _cmd_clear_live(args: argparse.Namespace) -> int:
    flow = _v2_flow(args)
    return _run_loop(lambda: run_clear_live(flow, args.reason), to_stdout=False)


def _cmd_init(args: argparse.Namespace) -> int:
    """Initialize a new v2 flow.

    Exit codes:
    - 0: success, prints "INIT flow=<dir> run_id=<run_id>"
    - 1: refusal (locked, exists, v1-owned, skeleton-owned, schema-invalid)
    - 2: usage error (invalid path)
    """
    flow_path = Path(args.flow)
    worktree_path = Path(args.worktree)

    # Validate the flow path structure
    try:
        repo_key, issue = validate_v2_flow_path(Path.home(), flow_path)
        explicit_repo = getattr(args, "repo", None)
        if explicit_repo is not None:
            # The key must be the one this repo maps to, so a mistyped --repo cannot land in another repo's dir
            if _repo_key(explicit_repo) != repo_key:
                return _usage_error(f"error: --repo {explicit_repo} does not match the flow path key {repo_key}")
            repo = explicit_repo
        else:
            # Without --repo the key is reversed on its first underscore (exact only for names with no underscore)
            repo = repo_key.replace("_", "/", 1)
    except FlowPathError as exc:
        return _usage_error(f"error: {exc}")

    if not worktree_path.is_absolute():
        return _usage_error("error: --worktree must be an absolute path")

    spec = InitSpec(repo=repo, issue=issue, worktree=str(worktree_path))
    try:
        with mutating(flow_path, create=spec) as locked:
            init_state(locked, spec)
            if locked.state is None:
                return _refuse("error: init_state failed to create state")
            run_id = locked.state.run_id
    except Refusal as exc:
        return _refuse(str(exc))
    except StateSchemaError as exc:
        return _refuse(str(Refusal("schema-invalid", str(exc))))
    except OSError as exc:
        return _refuse(f"error: {type(exc).__name__}: {exc}")

    print(f"INIT flow={flow_path} run_id={run_id}")
    return 0


def main_resume(flow: str, redo: Optional[str] = None) -> int:
    """Programmatic entry point for the resume verb; returns the exit code the CLI would return."""
    return _cmd_resume(argparse.Namespace(flow=flow, redo=redo))


def _cmd_resume(args: argparse.Namespace) -> int:
    """Resume a v2 flow.

    Exit codes:
    - 0: success (wait, receipt-landed, rerun outcomes)
    - 1: refusal (locked, v1-owned, skeleton-owned, schema-invalid, no-state, verifier-timeout)
    - 2: usage error (path inside neither root)
    - 3: not-yet-built (fail-closed for rules not yet built in Layer 1)
    """
    flow_path = Path(args.flow)
    redo_step = args.redo

    # Resolve which root the flow is under
    try:
        owner_type, resolved_path = resolve_read_flow_dir(Path.home(), flow_path)
    except FlowPathError as exc:
        return _usage_error(f"error: {exc}")

    # For v1 flows, refuse as v1-owned
    if owner_type == "v1":
        return _refuse(str(v1_owned_refusal(resolved_path.name)))

    # A single verifier timeout is recoverable: mutating re-enters once (fresh lock and re-read).
    # A second timeout within this invocation is a refusal.
    for reentry in (1, 2):
        try:
            return _resume_locked(resolved_path, redo_step)
        except VerifierTimeout as exc:
            if reentry == 2:
                return _refuse(str(Refusal("verifier-timeout", str(exc))))
            _log_verifier_retry(resolved_path, str(exc))
    return _refuse(str(Refusal("verifier-timeout", "retries exhausted")))  # unreachable: the loop returns on every path


def _log_verifier_retry(flow_path: Path, detail: str) -> None:
    """Record the first verifier timeout (the retry is otherwise invisible). Best effort: a lock miss is skipped."""
    try:
        with mutating(flow_path) as locked:
            if locked.state is not None:
                locked.log(
                    "verifier-timeout-retry",
                    step=locked.state.step,
                    attempt=locked.state.attempts.get(locked.state.step, 0),
                    detail=detail,
                )
    except (Refusal, StateSchemaError, OSError):
        pass


def _resume_locked(resolved_path: Path, redo_step: Optional[str]) -> int:
    """Run the resume cascade under the flow lock and map the decision to an exit code."""
    try:
        with mutating(resolved_path) as locked:
            if locked.state is None:
                return _refuse(str(no_state_refusal(resolved_path.name)))

            # Gather facts for resume
            facts = ResumeFacts(
                state=locked.state,
                redo_requested=redo_step is not None,
                pending_human_input=None,
                heal_in_progress=None,
                subagent_gone=None,
                assess_due=None,
                passing_outstanding_check=None,
            )

            # Decide which resume rule applies (decide prints the RESUME line and logs the rule)
            decision = resume_decide(locked, facts)

            # Handle the decision
            if decision.outcome == "not-yet-built":
                print(f"not-yet-built: {decision.rule} is Layer {decision.layer}", file=sys.stderr)
                return 3
            else:
                # Acted-on outcome (wait, receipt-landed, rerun)
                return 0
    except VerifierTimeout:
        raise
    except Refusal as exc:
        return _refuse(str(exc))
    except StateSchemaError as exc:
        return _refuse(str(Refusal("schema-invalid", str(exc))))
    except CascadeExhausted as exc:
        return _refuse(str(Refusal("cascade-exhausted", str(exc))))
    except OSError as exc:
        return _refuse(f"error: {type(exc).__name__}: {exc}")


def _cmd_status(args: argparse.Namespace) -> int:
    """Print JSON status of a flow.

    Exit codes: 0 on success; 2 for a flow path inside neither root (usage error).
    """
    flow_path = Path(args.flow)

    # Resolve which root the flow is under
    try:
        owner_type, resolved_path = resolve_read_flow_dir(Path.home(), flow_path)
    except FlowPathError as exc:
        return _usage_error(f"error: {exc}")

    # Read the flow without locking
    flow_view = read_flow(resolved_path)

    # Determine the owner type
    owner_str: str
    state_obj: Optional[Dict[str, Any]] = None
    if isinstance(flow_view.owned, V2Owned):
        owner_str = "v2"
        from .state import v2_state_to_dict
        state_obj = v2_state_to_dict(flow_view.owned.state)
    elif isinstance(flow_view.owned, V1Foreign):
        owner_str = "v1"
        # Try to read v1 state.json if present
        try:
            v1_state_text = (resolved_path / "state.json").read_text(encoding="utf-8")
            state_obj = json.loads(v1_state_text)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state_obj = None
    elif isinstance(flow_view.owned, SkeletonForeign):
        owner_str = "skeleton"
        try:
            skeleton_state_text = (resolved_path / "state.json").read_text(encoding="utf-8")
            state_obj = json.loads(skeleton_state_text)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state_obj = None
    elif isinstance(flow_view.owned, InvalidState):
        owner_str = "invalid"
        state_obj = None
    elif isinstance(flow_view.owned, NoState):
        owner_str = "initializing"
        state_obj = None
    else:
        assert_never(flow_view.owned)

    result = {
        "owner": owner_str,
        "flow": str(resolved_path),
        "state": state_obj,
    }
    print(json.dumps(result, indent=2))
    return 0


def _flow_row(flow_dir: Path) -> Tuple[str, str]:
    """(step, status) for one flow dir, reusing classify_state so ownership has one definition."""
    owned = classify_state(flow_dir)
    if isinstance(owned, V2Owned):
        return owned.state.step, owned.state.status
    if isinstance(owned, NoState):
        return "-", "initializing"
    if isinstance(owned, InvalidState):
        return "-", f"invalid ({owned.field}: {owned.reason})"
    if isinstance(owned, SkeletonForeign):
        return "-", "skeleton"
    if isinstance(owned, V1Foreign):
        try:
            data = json.loads((flow_dir / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "-", "initializing"
        except (OSError, ValueError) as exc:
            return "-", f"invalid (state.json: unreadable {exc.__class__.__name__})"
        if not isinstance(data, dict):
            return "-", "invalid (state.json: top level is not a JSON object)"
        return str(data.get("step", "-")), str(data.get("status", "-"))
    assert_never(owned)


def _cmd_list(args: argparse.Namespace) -> int:
    """List all flows in flows-v2 and flows. Bad state prints as invalid with its class, never silently.

    Exit code: 0 (always)
    """
    home = Path.home()
    repo_filter = args.repo
    wanted = repo_filter.replace("/", "_") if repo_filter else None
    rows: List[Tuple[str, str, str, str]] = []
    for label, root in (("v2", flows_v2_root(home)), ("v1", home / ".claude" / "flows")):
        if not root.exists():
            continue
        for repo_dir in root.iterdir():
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            if wanted and repo_dir.name != wanted:
                continue
            for flow_dir in repo_dir.iterdir():
                if not flow_dir.is_dir() or flow_dir.name.startswith("."):
                    continue
                step, status = _flow_row(flow_dir)
                rows.append((label, f"{repo_dir.name}/{flow_dir.name}", step, status))
    rows.sort(key=lambda x: x[1])
    for owner, path, step, status in rows:
        print(f"{owner}\t{path}\t{step}\t{status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flow_driver",
        description="v2 and skeleton /expert-flow driver (Layer 1 and skeleton modes). Disk-only boundary.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # V2 driver commands (Layer 1)
    init = sub.add_parser("init", help="initialize a new v2 flow")
    init.add_argument("--flow", required=True, help="absolute flow dir path (must match flows-v2 structure)")
    init.add_argument("--worktree", required=True, help="absolute worktree path")
    init.add_argument("--repo", help="OWNER/NAME; required when the repo name contains an underscore (the key cannot be reversed)")

    resume = sub.add_parser("resume", help="resume a v2 flow")
    resume.add_argument("--flow", required=True, help="absolute flow dir path")
    resume.add_argument("--redo", help="re-do a step (not yet implemented in Layer 1)")

    status = sub.add_parser("status", help="get JSON status of a flow (read-only)")
    status.add_argument("--json", action="store_true", required=True, help="output JSON")
    status.add_argument("--flow", required=True, help="absolute flow dir path")

    lst = sub.add_parser("list", help="list all flows in flows-v2 and flows (read-only)")
    lst.add_argument("--repo", help="filter by OWNER/NAME")

    # Skeleton driver commands (unchanged)
    start = sub.add_parser("start", help="create a flow dir (or open --flow) and perform one next")
    start.add_argument("--repo", required=True, help="OWNER/NAME")
    start.add_argument("--mode", required=True, choices=list(MODES))
    start.add_argument("--wait-mode", required=True, choices=list(WAIT_MODES))
    start.add_argument("--step-minutes", required=True, type=int)
    start.add_argument("--flow", help="absolute path of an existing flow dir to resume")

    nxt = sub.add_parser("next", help="decide the next directive and print one NEXT line")
    nxt.add_argument("--flow", required=True, help="absolute flow dir")
    nxt.add_argument("--after", help="the event label that led here (logged only)")

    ans = sub.add_parser("answer", help="write the human's answer for the parked attempt")
    ans.add_argument("--flow", required=True, help="absolute flow dir")
    ans.add_argument("--text", required=True)

    clear = sub.add_parser("clear-live", help="clear a spawned or resumed record (logged)")
    clear.add_argument("--flow", required=True, help="absolute flow dir")
    clear.add_argument("--reason", required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # V2 driver commands
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "resume":
            return _cmd_resume(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "list":
            return _cmd_list(args)
        # Skeleton driver commands
        if args.command == "start":
            return _cmd_start(args)
        if args.command == "next":
            return _cmd_next(args)
        if args.command == "answer":
            return _cmd_answer(args)
        if args.command == "clear-live":
            return _cmd_clear_live(args)
        return 2
    except (FlowPathError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
