"""
The mutating v2 driver loop (Layer 2 Steps 7 and 8; ADR-0026; sessions D3, D4, Q1, Q4 and the ship guard).

start, next, answer, clear-live, recover and abandon each take the flow lock once, through state.mutating. Inside that
hold one `next`:

  1. settles the pending receipt: lands a well-formed receipt, archives a stale one, stores a new fingerprint, or
     applies the D4 rerun gate to a malformed receipt (and the D5 cap);
  2. runs the edge checks that must precede a spawn or a stop (heads, the ship guard, merge arrival, preflights,
     ledger carry-forward and close-out); any failure becomes a failed state with a STOP directive;
  3. calls the pure steps.transition with the facts it gathered;
  4. takes a ledger snapshot for a new SPAWN and commits the returned state (state replace first);
  5. writes the step prompt (SPAWN only) and the nonce-named directive file;
  6. returns the one `NEXT: <VERB> <file>` line that cli.py prints.

A crash after step 4 leaves state.json ahead of the files. The next call rewrites the same files from the committed
pending record, so the nonce, and therefore the file names, are the same (Q1 = A).

Python 3.8 floor: typing.Callable, Dict, Optional, Tuple only.
"""

import dataclasses
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from scripts.workflow.fsutil import atomic_write_text

from . import driver
from . import ledger as lg
from . import preflight as pf
from . import receipts
from . import recovery as rc
from . import shipguard as sg
from .model import GENERAL_PURPOSE_AGENT, assert_never
from .recovery import Stop as RecoveryStop
from .recovery import malformed_rerun
from .state import (
    DRIVER_LOG_FILE,
    Heads,
    LockedFlow,
    PendingSpawn,
    Refusal,
    Step,
    TransitionKey,
    V2State,
    mutating,
    record_cleared,
)
from .steps import (
    HAPPY_PATH,
    Directive,
    DoneDirective,
    Facts,
    Notification,
    QuestionFact,
    ReceiptFact,
    SpawnDirective,
    StopDirective,
    WaitDirective,
    answer_file_path,
    directive_file_path,
    directive_to_dict,
    event_from_label,
    parse_stop_reason,
    question_file_path,
    step_prompt_path,
    stop_reason,
    transition,
)

# The D4 quiet interval: a malformed receipt is rerun only after this long since it was first seen, unless a
# notification says the step's subagent is gone. Measured across invocations from the injected clock.
RERUN_QUIET_SECONDS = 60
_TS = "%Y-%m-%dT%H:%M:%SZ"
# Clear-live is the human's way out of a wait-timeout STOP; this code reopens the step. A resume-failed STOP is never
# reopened by clear-live: the W1 re-spawn allowlist is empty (session D3), so every failed RESUME stays a STOP.
_CLEARABLE_STOPS = ("wait-timeout",)
# Steps whose agent carries the restricted guard hooks (flow-plan, flow-implement, flow-review, flow-ship).
RESTRICTED_STEPS = frozenset(("plan", "implement", "review", "fix", "ship"))
# Steps that run the North Star files preflight before they spawn (ADR-0003 2026-10-09 amendment).
NORTH_STAR_STEPS = frozenset(("plan", "review"))
# The command each step runs, named in its prompt. Mirrors the Steps table of commands/expert-flow.md.
STEP_COMMANDS: Dict[str, str] = {
    "worktree": "/track-and-start --issue {issue} --worktree-only",
    "plan": "/expert-plan {issue_url} --flow {flow}",
    "track": "/track-and-start --issue {issue}",
    "implement": "/implement-with-haiku <PLAN_PATH>",
    "review": "/expert-review --force",
    "fix": "/implement-with-haiku <REVIEW_DIR>/claude-action-plan.md",
    "verify": "the project's check command (run in the worktree)",
    "assess": "the assess relay (orchestrator stage)",
    "ship": "/shipit",
    "merge": "/merge-and-cleanup <WORKTREE>",
}
# A landed OK receipt of these steps carries its Addressed line forward (ADR-0026 ledger carry-forward).
CARRY_STEPS = frozenset(("plan", "implement", "review", "fix"))

Clock = Callable[[], datetime]
Outcome = Tuple[V2State, Directive]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.strftime(_TS)


class LoopRefusal(Exception):
    """A refusal that is not a Refusal class: the command cannot act on the flow now. cli.py prints `refused: <msg>`."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def _log_locked_refusal(flow: Path, label: str) -> None:
    """A locked refusal appends one line in a single write and never takes the lock (plan Step 7)."""
    if not flow.is_dir():
        return
    record = {
        "ts": _stamp(_utc_now()),
        "event": "locked-refusal",
        "run_id": None,
        "step": None,
        "attempt": None,
        "label": label,
    }
    with open(flow / DRIVER_LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _archived_landed_text(locked: LockedFlow, pending: PendingSpawn) -> Optional[str]:
    archived = locked.path / "archive" / pending.step / f"a{pending.attempt}.landed.md"
    try:
        return archived.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _archived_landed_failed(locked: LockedFlow, pending: PendingSpawn) -> bool:
    """A receipt-landed pending record: the archived landed receipt says whether the attempt FAILED."""
    text = _archived_landed_text(locked, pending)
    if text is None:
        return False
    receipt = receipts.parse_receipt(pending.step, text)
    return receipt is not None and receipt.decision == "FAILED"


def _quiet_elapsed(first_seen: str, now: datetime) -> bool:
    first = datetime.strptime(first_seen, _TS).replace(tzinfo=timezone.utc)
    return now - first >= timedelta(seconds=RERUN_QUIET_SECONDS)


def _malformed_gate(locked: LockedFlow, pending: PendingSpawn, notified: bool, now: datetime) -> ReceiptFact:
    """The D4 rerun gate and the D5 cap for a malformed receipt."""
    state = locked.require_state()
    if not notified:
        # D4: only a gone subagent (a notification) satisfies the gate at once; otherwise the quiet interval applies.
        if pending.receipt_first_seen is None:
            stamped = dataclasses.replace(pending, receipt_first_seen=_stamp(now))
            locked.commit(dataclasses.replace(state, pending_spawn=stamped))
            return "gate-hold"
        if not _quiet_elapsed(pending.receipt_first_seen, now):
            return "gate-hold"
    previous = state.attempt_log.get(pending.step, {}).get(pending.attempt - 1)
    decision = malformed_rerun(pending.step, pending.attempt, previous == "receipt-malformed")
    if isinstance(decision, RecoveryStop):
        return "malformed-cap"
    receipts.rerun_malformed(locked, TransitionKey(state.run_id, pending.step, pending.attempt))
    return "rerun"


def _settle_receipt(locked: LockedFlow, notified: bool, now: datetime) -> ReceiptFact:
    """Act on the pending attempt's receipt under the lock. Returns the fact transition() reads."""
    state = locked.require_state()
    pending = state.pending_spawn
    if pending is None:
        return "none"
    if pending.state == "receipt-landed":
        return "failed" if _archived_landed_failed(locked, pending) else "none"
    key = TransitionKey(state.run_id, pending.step, pending.attempt)
    classified = receipts.classify(locked, pending)
    if isinstance(classified, receipts.Absent):
        # A crash between an archive move and its state commit leaves the receipt gone and the archive present:
        # finish that move here, so `next` and `resume` roll forward the same way.
        forward = receipts.roll_forward(locked, key, pending)
        if forward == "rerun":
            return "rerun"
        if forward == "receipt-landed":
            landed = locked.require_state().pending_spawn
            return "failed" if landed is not None and _archived_landed_failed(locked, landed) else "none"
        return "none"
    if isinstance(classified, receipts.InProgress):
        if pending.receipt_seen != classified.fingerprint:
            receipts.store_fingerprint(locked, key, classified.fingerprint)
        return "none"
    if isinstance(classified, receipts.Landed):
        receipts.land(locked, key)
        return "failed" if classified.receipt.decision == "FAILED" else "none"
    if isinstance(classified, receipts.Stale):
        receipts.archive_stale(locked, key, classified.fingerprint.sha256)
        locked.log(
            "stale-nonce",
            step=pending.step,
            attempt=pending.attempt,
            nonce=pending.nonce,
            receipt_attempt=classified.receipt.attempt,
        )
        return "none"
    if isinstance(classified, receipts.Malformed):
        return _malformed_gate(locked, pending, notified, now)
    assert_never(classified)


def _question_fact(flow: Path, pending: Optional[PendingSpawn]) -> QuestionFact:
    if pending is None:
        return "none"
    if answer_file_path(flow, pending.step, pending.attempt).exists():
        return "answered"
    if question_file_path(flow, pending.step, pending.attempt).exists():
        return "awaiting"
    return "none"


def _changed(old: V2State, new: V2State) -> bool:
    """True when the transition changed anything other than the stamp; an unchanged state is not re-committed."""
    return dataclasses.replace(old, updated="") != dataclasses.replace(new, updated="")


def _halt(state: V2State, flow: Path, code: str, detail: str) -> Outcome:
    """A failed state and its STOP. The reason is built by steps.stop_reason, the one builder."""
    reason = stop_reason(code, detail)  # type: ignore[arg-type]
    reasons: Dict[Step, str] = dict(state.stop_reason)
    reasons[state.step] = reason
    failed = dataclasses.replace(state, status="failed", stop_reason=reasons)
    return failed, StopDirective(next_command=driver.next_command(flow), reason=reason)


def _done(state: V2State, flow: Path) -> Outcome:
    """The merge is confirmed on GitHub: the flow is done and the record is cleared."""
    done = dataclasses.replace(state, status="done", pending_spawn=None, live=None)
    return done, DoneDirective(next_command=driver.next_command(flow))


def _branch_of(worktree: Optional[str]) -> Optional[str]:
    """The ticket branch of the worktree, or None when git cannot name it. Pure reads."""
    if not worktree:
        return None
    result = pf.run_command(["git", "-C", worktree, "branch", "--show-current"])
    name = result.stdout.strip()
    if result.returncode != 0 or not name:
        return None
    return name


def _spawn_step(state: V2State, receipt_fact: ReceiptFact) -> Optional[Step]:
    """The step the transition will spawn on this call, or None when it will not spawn."""
    if state.status != "running" or receipt_fact == "failed":
        return None
    pending = state.pending_spawn
    if pending is None:
        return state.step
    if pending.state != "receipt-landed" or pending.step not in HAPPY_PATH:
        return None
    index = HAPPY_PATH.index(pending.step)
    if index + 1 >= len(HAPPY_PATH):
        return None
    return HAPPY_PATH[index + 1]


def _ship_arrival(state: V2State, flow: Path) -> Optional[Outcome]:
    """A landed ship receipt: MERGED on GitHub is done; anything else falls through to merge-not-built."""
    if state.heads.ship is None:
        return None
    branch = _branch_of(state.worktree)
    if branch is None:
        return _halt(state, flow, "gh-error", "could not read the ticket branch for the merge check")
    result = sg.check_pr(branch, state.heads.ship)
    if isinstance(result, sg.Merged):
        return _done(state, flow)
    if isinstance(result, sg.GhError):
        return _halt(state, flow, "gh-error", result.detail)
    return None


def _ship_guard(state: V2State, flow: Path) -> Optional[Outcome]:
    """Before ship spawns: the worktree HEAD and the PR head must still be the verified HEAD (ADR-0026 ship guard)."""
    if state.worktree is None or state.heads.verify is None:
        return None
    local = sg.check_local_head(state.worktree, state.heads.verify)
    if isinstance(local, sg.LocalHeadMismatch):
        return _halt(state, flow, "expected-head-mismatch", f"worktree HEAD {local.actual} is not the verified {local.expected}")
    if isinstance(local, sg.GitError):
        return _halt(state, flow, "step-failed", local.detail)
    branch = _branch_of(state.worktree)
    if branch is None:
        return _halt(state, flow, "gh-error", "could not read the ticket branch for the ship guard")
    pr = sg.check_pr(branch, state.heads.verify)
    if isinstance(pr, sg.HeadDiffers):
        return _halt(state, flow, "ship-head-differs", sg.describe_ship_guard(pr))
    if isinstance(pr, sg.Closed):
        return _halt(state, flow, "ship-pr-closed", sg.describe_ship_guard(pr))
    if isinstance(pr, sg.GhError):
        return _halt(state, flow, "gh-error", pr.detail)
    if isinstance(pr, sg.Merged):
        return _done(state, flow)
    if isinstance(pr, sg.SameHead):
        # A PR is already OPEN at the verified head: skip ship (plan Step 4), close the directions out, and stop at
        # the merge that Layer 2 does not build.
        closed = lg.close_out_directions(flow, by=_verify_receipt_name(flow))
        if isinstance(closed, lg.LedgerError):
            return _halt(state, flow, "ledger-error", closed.detail)
        skipped = dataclasses.replace(
            state, heads=Heads(verify=state.heads.verify, ship=state.heads.verify), pending_spawn=None, live=None
        )
        return _halt(skipped, flow, "merge-not-built", f"ship skipped; {sg.describe_ship_guard(pr)}; merge gate not built (Layer 3)")
    return None


def _preflight(state: V2State, flow: Path, spawn: Step, event_is_start: bool) -> Tuple[Optional[Outcome], str]:
    """Hook and North Star preflights for a spawn. Returns (a stop outcome or None, extra prompt lines)."""
    if spawn in RESTRICTED_STEPS or event_is_start:
        hooks = pf.check_hook_preflight()
        if isinstance(hooks, pf.Stop):
            return _halt(state, flow, hooks.code, hooks.detail), ""
    if spawn in NORTH_STAR_STEPS:
        north = pf.check_north_star_files()
        if isinstance(north, pf.Stop):
            return _halt(state, flow, north.code, north.detail), ""
        if isinstance(north, pf.Warn):
            return None, f"{north.line}\nALLOW_DEGRADED: --allow-degraded\n"
    return None, ""


def _verify_receipt_name(flow: Path) -> str:
    """The relative artifact path the close-out cites: the verify receipt inside the flow dir."""
    return str(receipts.receipt_path(flow, "verify").relative_to(flow))


def _pre_transition(
    locked: LockedFlow, flow: Path, state: V2State, receipt_fact: ReceiptFact, event_is_start: bool
) -> Tuple[Optional[Outcome], str]:
    """
    The edge checks that run before the pure transition. Returns (an outcome that replaces the transition, the extra
    prompt lines). Heads are recorded here and committed, so the transition reads the updated state.
    """
    if state.status != "running":
        return None, ""
    pending = state.pending_spawn
    if pending is not None and pending.state == "receipt-landed" and receipt_fact != "failed":
        if pending.step in ("verify", "ship") and state.worktree is not None:
            head = sg.read_head(state.worktree)
            if isinstance(head, sg.GitError):
                return _halt(state, flow, "step-failed", head.detail), ""
            if pending.step == "verify":
                heads = Heads(verify=head, ship=state.heads.ship)
            else:
                heads = Heads(verify=state.heads.verify, ship=head)
            locked.commit(dataclasses.replace(state, heads=heads))
            state = locked.require_state()
        if pending.step == "ship":
            arrived = _ship_arrival(state, flow)
            if arrived is not None:
                return arrived, ""
        if pending.step in CARRY_STEPS and pending.ledger_seq is not None:
            text = _archived_landed_text(locked, pending)
            if text is not None:
                carried = lg.carry_forward(flow, pending.ledger_seq, text)
                if isinstance(carried, lg.UncarriedDirection):
                    seqs = ", ".join(str(seq) for seq in carried.seqs)
                    return _halt(state, flow, "uncarried-direction", f"directions {seqs} are not in the Addressed line"), ""
                if isinstance(carried, lg.LedgerError):
                    return _halt(state, flow, "ledger-error", carried.detail), ""
    spawn = _spawn_step(state, receipt_fact)
    if spawn is None:
        return None, ""
    if spawn == "ship":
        guarded = _ship_guard(state, flow)
        if guarded is not None:
            return guarded, ""
        closed = lg.close_out_directions(flow, by=_verify_receipt_name(flow))
        if isinstance(closed, lg.LedgerError):
            return _halt(state, flow, "ledger-error", closed.detail), ""
    return _preflight(state, flow, spawn, event_is_start)


def render_step_prompt(
    flow: Path,
    step: str,
    attempt: int,
    nonce: str,
    *,
    issue: int,
    worktree: Optional[str],
    ledger_line: str,
    extra: str = "",
) -> str:
    """
    The prompt for one SPAWN: the FLOW_DIR, WORKTREE, RECEIPT, ATTEMPT and NONCE lines the step needs for its receipt,
    the HUMAN_LEDGER line, the North Star lines when degraded, and the command the step runs.
    """
    template = STEP_COMMANDS.get(step, "the step's command")
    command = template.format(issue=issue, issue_url=f"<issue {issue}>", flow=flow)
    receipt = receipts.receipt_path(flow, step)  # type: ignore[arg-type]
    return (
        f"You are the `{step}` step of the v2 /expert-flow driver (attempt {attempt}).\n"
        f"Run this step's work with: {command}\n"
        "Write your receipt to RECEIPT, then end your turn. The driver reads the receipt on the next call.\n"
        "\n"
        f"FLOW_DIR={flow}\n"
        f"WORKTREE={worktree or '<none>'}\n"
        f"RECEIPT={receipt}\n"
        f"STEP={step}\n"
        f"ATTEMPT: {attempt}\n"
        f"NONCE={nonce}\n"
        f"{ledger_line}\n"
        f"{extra}"
    )


def _snapshot_for_spawn(flow: Path, directive: SpawnDirective, before: V2State, new_state: V2State) -> Tuple[V2State, str, Optional[Outcome]]:
    """Take the ledger snapshot for a new spawn; a ledger error replaces the spawn with a ledger-error STOP."""
    snap = lg.snapshot_at_spawn(flow, directive.step, directive.attempt)  # type: ignore[arg-type]
    if isinstance(snap, lg.LedgerError):
        return before, "", _halt(before, flow, "ledger-error", snap.detail)
    pending = new_state.pending_spawn
    if pending is None:
        raise RuntimeError("snapshot_for_spawn: the spawn left no pending record")
    stamped = dataclasses.replace(new_state, pending_spawn=dataclasses.replace(pending, ledger_seq=snap.ledger_seq))
    return stamped, lg.human_ledger_line(snap), None


def _replay_spawn(
    flow: Path, state: V2State, pending: PendingSpawn
) -> Optional[Tuple[Optional[V2State], Directive, str, str]]:
    """
    A live spawned record whose step prompt is missing means the crash window between the state commit and the files.
    Re-render the SPAWN from the committed record, with the same nonce, the ledger snapshot re-rendered from the stored
    seq, and the preflight lines. Returns (a failed state when a preflight or the ledger now stops it, directive,
    ledger line, extra lines), or None when the prompt exists and a WAIT is right.
    """
    if step_prompt_path(flow, pending.step, pending.attempt, pending.nonce).exists():
        return None
    ledger_line = ""
    if pending.ledger_seq is not None:
        snap = lg.ensure_snapshot(flow, pending.step, pending.attempt, pending.ledger_seq)  # type: ignore[arg-type]
        if isinstance(snap, lg.LedgerError):
            failed, stop = _halt(state, flow, "ledger-error", snap.detail)
            return failed, stop, "", ""
        ledger_line = lg.human_ledger_line(snap)
    stopped, extra = _preflight(state, flow, pending.step, False)
    if stopped is not None:
        return stopped[0], stopped[1], "", ""
    spawn = SpawnDirective(
        next_command=driver.next_command(flow),
        agent_type=GENERAL_PURPOSE_AGENT,
        prompt_file=str(step_prompt_path(flow, pending.step, pending.attempt, pending.nonce)),
        step=pending.step,
        attempt=pending.attempt,
        nonce=pending.nonce,
    )
    return None, spawn, ledger_line, extra


def _step_once(locked: LockedFlow, event_label: Optional[str], flow: Path, now: datetime, headless: bool) -> str:
    event = event_from_label(event_label)
    notified = isinstance(event, Notification)
    nonce = secrets.token_hex(4)
    receipt_fact = _settle_receipt(locked, notified, now)
    state = locked.require_state()
    outcome, extra = _pre_transition(locked, flow, state, receipt_fact, event_label == "start")
    ledger_line = ""
    if outcome is not None:
        new_state, directive = outcome
    else:
        before = locked.require_state()
        facts = Facts(
            flow=flow,
            nonce=nonce,
            receipt=receipt_fact,
            question=_question_fact(flow, before.pending_spawn),
            headless=headless,
        )
        new_state, directive = transition(before, event, facts)
        if isinstance(directive, SpawnDirective):
            new_state, ledger_line, failure = _snapshot_for_spawn(flow, directive, before, new_state)
            if failure is not None:
                new_state, directive = failure
    if _changed(locked.require_state(), new_state):
        locked.commit(new_state)  # the state replace is the first write of the commit order
    committed = locked.require_state()
    pending = committed.pending_spawn
    if isinstance(directive, WaitDirective) and pending is not None and pending.state == "spawned":
        replayed = _replay_spawn(flow, committed, pending)
        if replayed is not None:
            halted, directive, ledger_line, extra = replayed
            if halted is not None:
                locked.commit(halted)
                committed = locked.require_state()
                pending = committed.pending_spawn
    file_nonce = "none" if pending is None else pending.nonce
    attempt = committed.attempts.get(committed.step, 0) if pending is None else pending.attempt
    step = committed.step if pending is None else pending.step
    if isinstance(directive, SpawnDirective):
        prompt = render_step_prompt(
            flow,
            directive.step,
            directive.attempt,
            directive.nonce,
            issue=committed.issue,
            worktree=committed.worktree,
            ledger_line=ledger_line,
            extra=extra,
        )
        _write(step_prompt_path(flow, directive.step, directive.attempt, directive.nonce), prompt)
    dfile = directive_file_path(flow, step, attempt, file_nonce)
    _write(dfile, json.dumps(directive_to_dict(directive), indent=2, sort_keys=True) + "\n")
    locked.log(directive.verb.lower(), step=step, attempt=attempt, nonce=file_nonce)
    return f"NEXT: {directive.verb} {dfile}"


def run_next(flow: Path, after: Optional[str], *, now: Clock = _utc_now, headless: bool = False) -> str:
    """
    One mutating `next`. after is the logged event label (None for a bare re-entry, `start` for the first call).
    Returns the NEXT line. Raises Refusal (state.Refusal, including `locked`) when the flow cannot be driven.
    """
    try:
        with mutating(flow) as locked:
            return _step_once(locked, after, flow, now(), headless)
    except Refusal as exc:
        if exc.refusal_class == "locked":
            _log_locked_refusal(flow, after or "")
        raise


def run_start(flow: Path, *, now: Clock = _utc_now) -> str:
    """The first `next` of a v2 flow, sent as the `start` label."""
    return run_next(flow, "start", now=now)


def run_answer(flow: Path, text: str) -> str:
    """Write the human's answer for the parked attempt. The next `next` resumes it. One answer per attempt."""
    with mutating(flow) as locked:
        state = locked.require_state()
        pending = state.pending_spawn
        if pending is None or pending.state != "parked-awaiting-answer":
            raise LoopRefusal("answer refused: the record is not parked-awaiting-answer")
        target = answer_file_path(flow, pending.step, pending.attempt)
        if target.exists():
            raise LoopRefusal(f"answer refused: {target} already exists")
        payload = {"attempt": pending.attempt, "step": pending.step, "text": text, "answered_utc": _stamp(_utc_now())}
        _write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        locked.log("answer", step=pending.step, attempt=pending.attempt, nonce=pending.nonce)
        return f"answer recorded: {target}"


def run_clear_live(flow: Path, reason: str) -> str:
    """
    Clear a spawned or resumed record without a receipt (logged with its nonce). A wait-timeout or resume-failed STOP
    is reopened, so the next `next` re-spawns the step with a fresh nonce. A parked record is answered, never cleared.
    """
    with mutating(flow) as locked:
        state = locked.require_state()
        pending = state.pending_spawn
        if pending is None or pending.state not in ("spawned", "resumed"):
            raise LoopRefusal("clear-live refused: there is no live record to clear")
        record_cleared(locked, TransitionKey(state.run_id, pending.step, pending.attempt))
        state = locked.require_state()
        reasons = dict(state.stop_reason)
        code = reasons.get(state.step, "").partition(": ")[0]
        if state.status == "failed" and code in _CLEARABLE_STOPS:
            reasons.pop(state.step, None)
            locked.commit(dataclasses.replace(state, status="running", stop_reason=reasons))
        locked.log(
            "clear-live",
            step=pending.step,
            attempt=pending.attempt,
            nonce=pending.nonce,
            reason=reason,
            cleared_state=pending.state,
        )
        return f"cleared live record: attempt {pending.attempt}, nonce {pending.nonce}"


def _count_outcome(state: V2State, step: Step, outcome: str) -> int:
    return sum(1 for value in state.attempt_log.get(step, {}).values() if value == outcome)


def run_recover(flow: Path, choice: str) -> str:
    """
    The human's answer to a hard stop: retry (a FAILED receipt, at most two times), take-over, or keep-previous.
    Each is one replace under the lock and is logged. A refused choice names its cap or reason.
    """
    with mutating(flow) as locked:
        state = locked.require_state()
        if state.status != "failed":
            raise LoopRefusal(f"recover refused: the flow is not stopped on a step (status {state.status})")
        step = state.step
        code, _ = parse_stop_reason(state.stop_reason.get(step, ""))
        pending = state.pending_spawn
        attempt = state.attempts.get(step, 0) if pending is None else pending.attempt
        reasons: Dict[Step, str] = dict(state.stop_reason)
        if choice == "retry":
            if code != "step-failed":
                raise LoopRefusal("recover refused: retry is offered only after a step landed a FAILED receipt")
            decision = rc.retry(step, _count_outcome(state, step, "retried"))
            if isinstance(decision, rc.Stop):
                raise LoopRefusal(stop_reason(decision.code, decision.detail))  # type: ignore[arg-type]
            outcomes: Dict[Step, Dict[int, str]] = {k: dict(v) for k, v in state.attempt_log.items()}
            outcomes.setdefault(step, {})[attempt] = "retried"
            reasons.pop(step, None)
            locked.commit(
                dataclasses.replace(
                    state,
                    status="running",
                    pending_spawn=None,
                    stop_reason=reasons,
                    attempt_log=outcomes,  # type: ignore[arg-type]
                )
            )
            locked.log("recover", step=step, attempt=attempt, choice="retry")
            return f"recovered: retry {step} (attempt {attempt} is retried; the next next spawns a new attempt)"
        if choice == "take-over":
            reasons[step] = stop_reason("take-over", f"step {step} was taken over by hand")  # type: ignore[arg-type]
            locked.commit(dataclasses.replace(state, status="paused", stop_reason=reasons))
            locked.log("recover", step=step, attempt=attempt, choice="take-over")
            return f"recovered: take-over of {step} recorded; the driver will not spawn it again"
        if choice == "keep-previous":
            raise LoopRefusal("recover refused: keep-previous needs a redo lineage, which Layer 2 does not build")
        raise LoopRefusal(f"recover refused: unknown choice {choice!r}")


def run_abandon(flow: Path) -> str:
    """Abandon a flow. A live record is refused and names clear-live; a parked or landed record is dropped."""
    with mutating(flow) as locked:
        state = locked.require_state()
        if state.status in ("done", "abandoned"):
            raise LoopRefusal(f"flow already {state.status}")
        pending = state.pending_spawn
        if pending is not None and pending.state in ("spawned", "resumed"):
            raise LoopRefusal(
                f"abandon refused: attempt {pending.attempt} of {pending.step} is live; "
                f"clear it first with {driver.clear_live_command(flow)}"
            )
        outcomes: Dict[Step, Dict[int, str]] = {k: dict(v) for k, v in state.attempt_log.items()}
        if pending is not None:
            outcomes.setdefault(pending.step, {})[pending.attempt] = "abandoned"
        locked.commit(
            dataclasses.replace(
                state,
                status="abandoned",
                pending_spawn=None,
                attempt_log=outcomes,  # type: ignore[arg-type]
            )
        )
        locked.log("abandon", step=state.step, attempt=state.attempts.get(state.step, 0))
        return f"abandoned: {flow}"
