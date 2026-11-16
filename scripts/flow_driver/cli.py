"""
CLI edge for the v2 driver (Layer 1 Step 7): init, resume, status --json, list, and skeleton subcommands.

Layer 1 adds locking and read-only split from the skeleton subcommands (start, next, answer, clear-live).
The skeleton subcommands are guarded to refuse v2 or v1 flows.

Write order for every next and start is fixed: the step prompt (SPAWN only), the directive file, state.json
(only when last_attempt changes), pending_spawn.json, one driver.log line, and then exactly one stdout line,
`NEXT: <VERB> <absolute directive file>`. Diagnostics go to stderr. An unexpected exception after the start of
a next or start still tries to record and print `NEXT: STOP <stop directive file>`, then exits 1.

Exit codes and stdout (init prints `INIT flow=<dir> run_id=<id>`; resume prints `RESUME rule=<rule> outcome=<outcome>`):
- 0: success (init, resume with acted-on outcome, status, list)
- 1: refusal (locked, v1-owned, skeleton-owned, schema-invalid, no-state, exists, verifier-timeout)
- 2: usage error (FlowPathError, bad flag, mismatched --flow args, path inside neither root)
- 3: not-yet-built (fail-closed, distinct from refusal; for rules not yet built in Layer 1)
"""

import argparse
import json
import secrets
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts.workflow.fsutil import atomic_write_text

from .driver import Decision, Observation, ReceiptView, decide, next_command, parse_receipt
from .flowdir import FlowPathError, create_flow_dir, repo_key as _repo_key, flows_v2_root, format_utc, open_flow_dir, resolve_read_flow_dir, validate_v2_flow_path
from .model import (
    MODES,
    WAIT_MODES,
    ClearPending,
    PendingSpawn,
    SetPending,
    SkeletonLoadError,
    SpawnDirective,
    StateRecord,
    StopDirective,
    answer_path,
    directive_path,
    directive_to_dict,
    load_pending,
    load_state,
    log_path,
    parse_after,
    pending_path,
    pending_to_dict,
    question_path,
    receipt_path,
    state_path,
    state_to_dict,
    stop_directive_path,
)
from .state import (
    InitSpec,
    InvalidState,
    NoState,
    Refusal,
    no_state_refusal,
    v1_owned_refusal,
    v2_owned_refusal,
    SkeletonForeign,
    StateSchemaError,
    V1Foreign,
    V2Owned,
    classify_state,
    init_state,
    mutating,
    read_flow,
)
from .receipts import VerifierTimeout
from .resume import CascadeExhausted, ResumeFacts, decide as resume_decide


class UsageError(Exception):
    """A caller error that is not a path problem (for example a mismatched flag). Exit 2, nothing on stdout."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_text(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _read_optional(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_state(flow: Path) -> StateRecord:
    text = _read_optional(state_path(flow))
    if text is None:
        raise SkeletonLoadError("state.json is missing")
    return load_state(text)


def read_pending(flow: Path) -> Optional[PendingSpawn]:
    text = _read_optional(pending_path(flow))
    return None if text is None else load_pending(text)


def gather(flow: Path, fresh_nonce: str) -> Observation:
    """The I/O edge for decide(): every boundary read happens here, and any failure becomes a fault."""
    state: Optional[StateRecord] = None
    pending: Optional[PendingSpawn] = None
    receipt: Optional[ReceiptView] = None
    fault: Optional[str] = None
    try:
        state = read_state(flow)
        pending = read_pending(flow)
    except (SkeletonLoadError, OSError) as exc:
        fault = f"boundary file unreadable: {exc}"
    ref = pending.attempt if pending is not None else (state.last_attempt if state is not None else 0)
    if fault is None:
        try:
            receipt_text = _read_optional(receipt_path(flow, ref))
            receipt = None if receipt_text is None else parse_receipt(receipt_text)
        except (SkeletonLoadError, OSError) as exc:
            fault = f"boundary file unreadable: {exc}"
    return Observation(
        flow_dir=flow,
        state=state,
        pending=pending,
        receipt=receipt,
        question_present=question_path(flow, ref).exists(),
        answer_present=answer_path(flow, ref).exists(),
        fresh_nonce=fresh_nonce,
        fault=fault,
    )


def append_log(flow: Path, entry: Dict[str, Any]) -> None:
    """Append one JSON line to driver.log. The log is evidence; the driver never reads it back."""
    record: Dict[str, Any] = {"utc": format_utc(_now())}
    record.update(entry)
    with open(log_path(flow), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def render_step_prompt(flow: Path, attempt: int, nonce: str, step_minutes: int, prompt_file: Path) -> str:
    """The driver-owned prompt for the general-purpose step agent (Step 4, the SPAWN prompt)."""
    heartbeat = flow / f"heartbeat-a{attempt}"
    lines = [
        f"# Skeleton step, attempt {attempt}",
        "",
        "This prompt was written by the v2 skeleton driver. It is a measuring instrument, not a real step.",
        "Every path below is absolute. Follow the steps in order.",
        "",
        f"Nonce: {nonce}",
        f"Prompt file: {prompt_file}",
        f"Step minutes: {step_minutes}",
        "",
        "1. As your first action, append one JSON line to the spawns file, then carry on.",
        f'   {{"nonce": "{nonce}", "step": "skeleton", "attempt": {attempt}, '
        f'"utc": "<current UTC, ISO 8601>", "prompt_file": "{prompt_file}"}}',
        f"   Spawns file: {flow / 'spawns.jsonl'}",
        "2. Start one bounded heartbeat in the background with a single Bash call that returns at once:",
        f"   ( for i in $(seq 1 {step_minutes}); do date -u +%Y-%m-%dT%H:%M:%SZ >> {heartbeat}; "
        f"sleep 60; done; echo done >> {heartbeat} ) >/dev/null 2>&1 &",
        "   Do not wait for it. Do not write heartbeat lines by hand and do not backfill timestamps.",
        f"3. Write this question to {question_path(flow, attempt)}:",
        f'   {{"attempt": {attempt}, "question": "Reply with the word ok to finish this trivial step."}}',
        f"   Then write {receipt_path(flow, attempt)} with exactly these lines:",
        f"   ATTEMPT: {attempt}",
        f"   <!-- awaiting-answers: skeleton-a{attempt} -->",
        "   Then end your turn. Do nothing else until you are resumed.",
        f"4. Only when you are resumed: read the answer file {answer_path(flow, attempt)}, append one "
        f"heartbeat line with the current UTC time to {heartbeat}, then rewrite {receipt_path(flow, attempt)} with "
        "exactly these lines:",
        f"   ATTEMPT: {attempt}",
        "   Decision: done",
        "   End your turn.",
        "",
    ]
    return "\n".join(lines)


def _advance_unchecked(flow: Path, subcommand: str, after: str) -> str:
    obs = gather(flow, secrets.token_hex(8))
    decision: Decision = decide(obs)
    directive = decision.directive
    if isinstance(directive, SpawnDirective):
        if obs.state is None:
            raise AssertionError("SPAWN decided without a readable state.json")
        atomic_write_text(
            Path(directive.prompt_file),
            render_step_prompt(
                flow,
                directive.attempt,
                directive.nonce,
                obs.state.step_minutes,
                Path(directive.prompt_file),
            ),
        )
    if isinstance(directive, StopDirective):
        target = stop_directive_path(flow)
    else:
        target = directive_path(flow, decision.attempt)
    atomic_write_text(target, _json_text(directive_to_dict(directive)))
    if obs.state is not None and decision.last_attempt != obs.state.last_attempt:
        updated = replace(obs.state, last_attempt=decision.last_attempt)
        atomic_write_text(state_path(flow), _json_text(state_to_dict(updated)))
    # Append log before pending write so that a failed log append does not leave pending_spawn.json unwritten.
    nonce: Optional[str] = obs.pending.nonce if obs.pending is not None else None
    change = decision.pending
    if isinstance(change, SetPending):
        nonce = change.record.nonce
    observed = "fault" if obs.fault is not None else (obs.pending.state if obs.pending is not None else "none")
    append_log(
        flow,
        {
            "subcommand": subcommand,
            "after": after,
            "observed": observed,
            "verb": directive.verb,
            "attempt": decision.attempt,
            "nonce": nonce,
            "events": [{"event": event.event, "detail": event.detail} for event in decision.events],
        },
    )
    # Write pending after log, so a crash during pending write leaves the log as evidence.
    if isinstance(change, SetPending):
        atomic_write_text(pending_path(flow), _json_text(pending_to_dict(change.record)))
    elif isinstance(change, ClearPending):
        _remove_pending(flow)
    return f"NEXT: {directive.verb} {target}"


def _remove_pending(flow: Path) -> None:
    try:
        pending_path(flow).unlink()
    except FileNotFoundError:
        pass


def _crash_stop(flow: Path, subcommand: str, after: str, exc: Exception) -> int:
    """After an unexpected exception: try to record and print NEXT: STOP, then exit non-zero.

    A crashed next or start leaves the pending record unchanged. Recovery is via clear-live (if the record
    was a stale spawn) or by re-running next when the fault is transient. The orchestrator must not run
    a new next after a crash STOP until clear-live has been called or the fault is diagnosed.
    """
    reason = f"driver error: {type(exc).__name__}: {exc}"
    print(reason, file=sys.stderr)
    target = stop_directive_path(flow)
    try:
        stop = StopDirective(next_command=next_command(flow), reason=reason)
        atomic_write_text(target, _json_text(directive_to_dict(stop)))
        append_log(
            flow,
            {
                "subcommand": subcommand,
                "after": after,
                "observed": "crash",
                "verb": "STOP",
                "attempt": None,
                "nonce": None,
                "events": [{"event": "crash", "detail": reason}],
            },
        )
    except Exception as nested:
        print(f"driver could not record STOP: {nested}", file=sys.stderr)
        return 1
    print(f"NEXT: STOP {target}")
    return 1


def advance(flow: Path, subcommand: str, after: str) -> int:
    """One `next` (or the `next` inside `start`): decide, write the files in order, then print one NEXT line."""
    try:
        line = _advance_unchecked(flow, subcommand, after)
    except Exception as exc:
        return _crash_stop(flow, subcommand, after, exc)
    print(line)
    return 0


def _now_flow_root() -> Path:
    return flows_v2_root(Path.home())


def _refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _usage_error(message: str) -> int:
    """A usage error (exit 2): a flow path inside neither root, or a malformed path. Not a refusal."""
    print(message, file=sys.stderr)
    return 2


def _guard_skeleton_subcommand(flow_dir: Path) -> Optional[int]:
    """
    Guard for skeleton subcommands (start, next, answer, clear-live).

    Allow only a skeleton flow; refuse every other class. Returns exit code 1 if refused, None if allowed.
    """
    owned = classify_state(flow_dir)
    # A skeleton flow, or an unreadable state (the skeleton turns that into a STOP directive), is allowed
    if isinstance(owned, (SkeletonForeign, InvalidState)):
        return None
    if isinstance(owned, V2Owned):
        return _refuse(str(v2_owned_refusal(flow_dir.name)))
    if isinstance(owned, V1Foreign):
        return _refuse(str(v1_owned_refusal(flow_dir.name)))
    # Allowlist: anything else (an empty directory included) is refused
    return _refuse(str(Refusal("not-skeleton", f"flow {flow_dir.name} is not a skeleton flow ({type(owned).__name__})")))


def _check_args_match(flow: Path, args: argparse.Namespace) -> None:
    """With --flow, the create-only flags must agree with the flow's state.json (an unreadable state is a STOP)."""
    try:
        state = read_state(flow)
    except (SkeletonLoadError, OSError):
        return
    given = (args.repo, args.mode, args.wait_mode, args.step_minutes)
    recorded = (state.repo, state.mode, state.wait_mode, state.step_minutes)
    if given != recorded:
        raise UsageError("start --flow: --repo, --mode, --wait-mode and --step-minutes must match state.json")


def _cmd_start(args: argparse.Namespace) -> int:
    if args.step_minutes < 1:
        raise UsageError("--step-minutes must be at least 1")
    root = _now_flow_root()
    flow: Path
    if args.flow is None:
        flow = create_flow_dir(
            root,
            args.repo,
            _now(),
            secrets.token_hex(4),
            mode=args.mode,
            wait_mode=args.wait_mode,
            step_minutes=args.step_minutes,
        )
    else:
        flow = open_flow_dir(root, Path(args.flow))
        guard_result = _guard_skeleton_subcommand(flow)
        if guard_result is not None:
            return guard_result
        _check_args_match(flow, args)
    return advance(flow, "start", "start")


def _cmd_next(args: argparse.Namespace) -> int:
    """Advance the flow to its next state. Single-caller invariant: only one next command runs at a time.

    Concurrent calls to `next` from different processes will race and one may overwrite the other's
    decision. The orchestrator enforces that only one next is running.
    """
    flow = open_flow_dir(_now_flow_root(), Path(args.flow))
    guard_result = _guard_skeleton_subcommand(flow)
    if guard_result is not None:
        return guard_result
    return advance(flow, "next", parse_after(args.after))


def _cmd_answer(args: argparse.Namespace) -> int:
    """Write the human's answer for a parked attempt. Single-writer invariant: only one answer writes the file.

    This function checks that the answer file does not exist before writing, but does not lock. The check-then-write
    race is documented: only one answer command should be called per parked attempt.
    """
    flow = open_flow_dir(_now_flow_root(), Path(args.flow))
    guard_result = _guard_skeleton_subcommand(flow)
    if guard_result is not None:
        return guard_result
    try:
        read_state(flow)
        pending = read_pending(flow)
    except (SkeletonLoadError, OSError) as exc:
        return _refuse(f"answer refused: boundary file unreadable: {exc}")
    if pending is None or pending.state != "parked-awaiting-answer":
        return _refuse("answer refused: the record is not parked-awaiting-answer")
    target = answer_path(flow, pending.attempt)
    if target.exists():
        return _refuse(f"answer refused: {target} already exists")
    atomic_write_text(
        target,
        _json_text({"attempt": pending.attempt, "text": args.text, "answered_utc": format_utc(_now())}),
    )
    append_log(
        flow,
        {
            "subcommand": "answer",
            "attempt": pending.attempt,
            "nonce": pending.nonce,
            "text": args.text,
        },
    )
    print(f"answer recorded: {target}", file=sys.stderr)
    return 0


def _cmd_clear_live(args: argparse.Namespace) -> int:
    flow = open_flow_dir(_now_flow_root(), Path(args.flow))
    guard_result = _guard_skeleton_subcommand(flow)
    if guard_result is not None:
        return guard_result
    try:
        read_state(flow)
        pending = read_pending(flow)
    except (SkeletonLoadError, OSError) as exc:
        return _refuse(f"clear-live refused: boundary file unreadable: {exc}")
    if pending is None:
        return _refuse("clear-live refused: there is no live record to clear")
    if pending.state == "parked-awaiting-answer":
        return _refuse("clear-live refused: a parked record is answered, never cleared")
    _remove_pending(flow)
    append_log(
        flow,
        {
            "subcommand": "clear-live",
            "reason": args.reason,
            "cleared_state": pending.state,
            "attempt": pending.attempt,
            "nonce": pending.nonce,
        },
    )
    print(f"cleared live record: attempt {pending.attempt}, nonce {pending.nonce}", file=sys.stderr)
    return 0


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
        from .model import assert_never
        assert_never(flow_view.owned)

    result = {
        "owner": owner_str,
        "flow": str(resolved_path),
        "state": state_obj,
    }
    print(json.dumps(result, indent=2))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """List all flows in flows-v2 and flows.

    Exit code: 0 (always)
    """
    home = Path.home()
    repo_filter = args.repo

    flows_to_list: List[Tuple[str, str, str, str]] = []

    # Scan v2 flows
    v2_root = flows_v2_root(home)
    if v2_root.exists():
        for repo_dir in v2_root.iterdir():
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            for flow_dir in repo_dir.iterdir():
                if not flow_dir.is_dir() or flow_dir.name.startswith("."):
                    continue

                # Apply repo filter if given
                if repo_filter and repo_dir.name != repo_filter.replace("/", "_"):
                    continue

                # Try to read state
                step_str = "-"
                status_str = "-"
                try:
                    state_file = flow_dir / "state.json"
                    if state_file.exists():
                        from .state import load_v2_state
                        state_text = state_file.read_text(encoding="utf-8")
                        state_obj = load_v2_state(json.loads(state_text))
                        step_str = state_obj.step
                        status_str = state_obj.status
                    else:
                        step_str = "-"
                        status_str = "initializing"
                except Exception:
                    pass

                flows_to_list.append(("v2", f"{repo_dir.name}/{flow_dir.name}", step_str, status_str))

    # Scan v1 flows
    v1_root = home / ".claude" / "flows"
    if v1_root.exists():
        for repo_dir in v1_root.iterdir():
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            for flow_dir in repo_dir.iterdir():
                if not flow_dir.is_dir() or flow_dir.name.startswith("."):
                    continue

                # Apply repo filter if given
                if repo_filter and repo_dir.name != repo_filter.replace("/", "_"):
                    continue

                # Try to read state
                step_str = "-"
                status_str = "-"
                try:
                    state_file = flow_dir / "state.json"
                    if state_file.exists():
                        state_text = state_file.read_text(encoding="utf-8")
                        state_data = json.loads(state_text)
                        step_str = state_data.get("step", "-")
                        status_str = state_data.get("status", "-")
                    else:
                        step_str = "-"
                        status_str = "initializing"
                except Exception:
                    pass

                flows_to_list.append(("v1", f"{repo_dir.name}/{flow_dir.name}", step_str, status_str))

    # Sort and print
    flows_to_list.sort(key=lambda x: x[1])
    for owner, path, step, status in flows_to_list:
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
