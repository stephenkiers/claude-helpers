"""
The mutating v2 driver loop (Layer 2 Steps 7 and 8; ADR-0026; sessions D3, D4, Q1, Q4 and the ship guard).

start, next, answer, clear-live, recover and abandon each take the flow lock once, through state.mutating. Inside that
hold one `next`:

  1. settles the pending receipt: lands a well-formed receipt, archives a stale one, stores a new fingerprint, or
     applies the D4 rerun gate to a malformed receipt (and the D5 cap);
  2. records the landed edges (heads, merge arrival, ledger carry-forward); any failure becomes a failed state with a
     STOP directive;
  3. calls the pure steps.transition with the facts it gathered;
  4. for a SPAWN the transition chose, runs the spawn edge checks in order (the ship guard, the input resolution, the
     hook and North Star preflights, then the ship ledger close-out) and takes a ledger snapshot; any failure replaces
     the spawn with a STOP. Then commits the returned state (state replace first);
  5. writes the step prompt (SPAWN only) and the verb-named directive file;
  6. returns the one `NEXT: <VERB> <file>` line that cli.py prints.

A crash after step 4 leaves state.json ahead of the files. The next call rewrites the same files from the committed
pending record, so the nonce, and therefore the file names, are the same (Q1 = A).

Python 3.8 floor: typing.Callable, Dict, Mapping, Optional, Tuple, Union only.
"""

import dataclasses
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple, Union

from scripts.workflow.fsutil import atomic_write_text

from . import driver
from . import gates
from . import ledger as lg
from . import preflight as pf
from . import receipts
from . import recovery as rc
from . import shipguard as sg
from .model import assert_never
from .recovery import Stop as RecoveryStop
from .recovery import malformed_rerun
from .state import (
    DRIVER_LOG_FILE,
    AttemptOutcome,
    Heads,
    LockedFlow,
    ParkedQuestion,
    PendingSpawn,
    Refusal,
    Step,
    TransitionKey,
    V2State,
    apply_transition,
    mutating,
)
from . import relay
from .steps import (
    _SPAWN_AGENT,
    Directive,
    DoneDirective,
    Facts,
    GateAdvance,
    GateAsk,
    GateDone,
    GateFact,
    GateMerge,
    GateStop,
    MergeDirective,
    Notification,
    QuestionFact,
    ReceiptFact,
    SpawnDirective,
    StopCode,
    StopDirective,
    WaitDirective,
    directive_file_path,
    directive_to_dict,
    event_from_label,
    parse_stop_reason,
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
# Steps whose agent carries the restricted guard hooks (flow-plan, flow-implement, flow-review, flow-ship, flow-merge).
RESTRICTED_STEPS = frozenset(("plan", "implement", "review", "fix", "ship", "merge"))
# Steps that run the North Star files preflight before they spawn (ADR-0003 2026-10-09 amendment).
NORTH_STAR_STEPS = frozenset(("plan", "review"))
# The command each step runs, named in its prompt. Mirrors the Steps table of commands/expert-flow.md. Every
# placeholder is filled by render_step_prompt from values _step_inputs resolved before the spawn was committed.
STEP_COMMANDS: Dict[str, str] = {
    "worktree": "/track-and-start --issue {issue} --worktree-only",
    "plan": "/expert-plan {issue_url} --flow {flow}",
    "track": "/track-and-start --issue {issue}",
    "implement": "/implement-with-haiku {plan_path}",
    "review": "/expert-review --force",
    "fix": "/implement-with-haiku {review_dir}/claude-action-plan.md",
    "verify": "the project's check command (run in the worktree)",
    "assess": "the assess relay (orchestrator stage)",
    "ship": "/shipit",
    "merge": "/merge-and-cleanup {worktree}",
}
# A step prompt never carries a `<UPPER_CASE>` placeholder: a leftover one fails the render (D6).
_NOT_RECORDED = "(not recorded in the landed receipt)"
_LEFTOVER_PLACEHOLDER = re.compile(r"<[A-Za-z_ ]+>")
# A landed OK receipt of these steps carries its Addressed line forward (ADR-0026 ledger carry-forward).
CARRY_STEPS = frozenset(("plan", "implement", "review", "fix"))

Clock = Callable[[], datetime]
Outcome = Tuple[V2State, Directive]
Outcomes = Dict[Step, Dict[int, AttemptOutcome]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.strftime(_TS)


def _parse_stamp(text: str) -> Optional[datetime]:
    """A stored UTC stamp, or None when it does not parse. An unreadable D4 anchor restarts the quiet interval."""
    try:
        return datetime.strptime(text, _TS).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class LoopRefusal(Exception):
    """A refusal that is not a Refusal class: the command cannot act on the flow now. cli.py prints `refused: <msg>`."""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def _issue_url(state: V2State) -> str:
    return f"https://github.com/{state.repo}/issues/{state.issue}"


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


def _landed_failed(locked: LockedFlow, pending: Optional[PendingSpawn]) -> bool:
    """True only for a pending attempt whose archived receipt landed FAILED (the only case retry is offered)."""
    return pending is not None and pending.state == "receipt-landed" and _archived_landed_failed(locked, pending)


def _failed_stop(locked: LockedFlow, pending: Optional[PendingSpawn]) -> Optional[Tuple[StopCode, str]]:
    """The test-path stop a landed FAILED receipt names in its Reason, or None (a plain step-failed)."""
    if pending is None:
        return None
    text = _archived_landed_text(locked, pending)
    found = None if text is None else receipts.failed_test_path_stop(text)
    if found is None:
        return None
    code, detail = found
    parsed, _ = parse_stop_reason(f"{code}: {detail}")
    return None if parsed is None else (parsed, detail)


def _quiet_elapsed(first: datetime, now: datetime) -> bool:
    return now - first >= timedelta(seconds=RERUN_QUIET_SECONDS)


def _malformed_gate(locked: LockedFlow, pending: PendingSpawn, notified: bool, now: datetime) -> ReceiptFact:
    """The D4 rerun gate and the D5 cap for a malformed receipt."""
    state = locked.require_state()
    if not notified:
        # D4: only a gone subagent (a notification) satisfies the gate at once; otherwise the quiet interval applies.
        first = None if pending.receipt_first_seen is None else _parse_stamp(pending.receipt_first_seen)
        if first is None:
            # The first sighting, or an unreadable anchor, starts the quiet interval again.
            stamped = dataclasses.replace(pending, receipt_first_seen=_stamp(now))
            locked.commit(dataclasses.replace(state, pending_spawn=stamped))
            return "gate-hold"
        if not _quiet_elapsed(first, now):
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
    # For Layer 2 step questions, construct a temporary ParkedQuestion for path resolution
    from .state import ParkedQuestion
    parked = ParkedQuestion(step=pending.step, attempt=pending.attempt, nn=pending.attempt, kind="step")
    if relay.answer_path(flow, parked).exists():
        return "answered"
    if relay.question_path(flow, parked).exists():
        return "awaiting"
    return "none"


def _changed(old: V2State, new: V2State) -> bool:
    """True when the transition changed anything other than the stamp; an unchanged state is not re-committed."""
    return dataclasses.replace(old, updated="") != dataclasses.replace(new, updated="")


def _halt(state: V2State, flow: Path, code: StopCode, detail: str) -> Outcome:
    """A failed state and its STOP. The reason is built by steps.stop_reason, the one builder."""
    reason = stop_reason(code, detail)
    reasons: Dict[Step, str] = dict(state.stop_reason)
    reasons[state.step] = reason
    failed = dataclasses.replace(state, status="failed", stop_reason=reasons)
    return failed, StopDirective(next_command=driver.next_command(flow), reason=reason)


def _done(state: V2State, flow: Path) -> Outcome:
    """The merge is confirmed on GitHub: the flow is done and the record is cleared."""
    done = dataclasses.replace(state, status="done", pending_spawn=None, live=None)
    return done, DoneDirective(next_command=driver.next_command(flow))


def _copy_outcomes(state: V2State) -> Outcomes:
    return {step: dict(per_step) for step, per_step in state.attempt_log.items()}


def _set_outcome(outcomes: Outcomes, step: Step, attempt: int, value: AttemptOutcome, *, replaces: Optional[AttemptOutcome] = None) -> None:
    """
    Record one attempt outcome. Outcomes are write-once: a recorded value is replaced only by the one value named
    `replaces` (the retry of a FAILED landing, the only rewrite the loop makes). Anything else is a defect.
    """
    current = outcomes.get(step, {}).get(attempt)
    if current is not None and current != replaces:
        raise RuntimeError(f"attempt_log {step} attempt {attempt} already records {current!r}; outcomes are write-once")
    outcomes.setdefault(step, {})[attempt] = value


def _branch_of(worktree: Optional[str]) -> Union[str, sg.GitError]:
    """The ticket branch of the worktree, or the git failure that stopped the read. Pure reads."""
    if not worktree:
        return sg.GitError("no worktree is recorded for the flow")
    result = pf.run_command(["git", "-C", worktree, "branch", "--show-current"])
    if result.returncode != 0:
        return sg.GitError(result.stderr.strip() or f"git exited {result.returncode}")
    name = result.stdout.strip()
    if not name:
        return sg.GitError("the worktree is on no branch")
    return name


def _heal_commits(worktree: Optional[str]) -> Tuple[str, ...]:
    """Subjects of the Flow-Heal commits on the ticket branch (merge gate context); () on any git failure."""
    if not worktree:
        return ()
    result = pf.run_command(["git", "-C", worktree, "log", "--format=%s", "--grep=^Flow-Heal:", "origin/main..HEAD"])
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def _merge_arrival(state: V2State, flow: Path) -> Outcome:
    """
    A landed merge receipt: the PR is re-read by number against the approved head (never a branch from the worktree).
    MERGED is done; anything else is a STOP naming the receipt's claim and what GitHub says.
    """
    record = state.merge_gate
    if record is None:
        return _halt(state, flow, "gh-error", "the merge receipt landed but no merge gate is recorded to check it against")
    verdict = sg.check_pr_by_number(record.pr_number, record.repo, record.approved_head)
    if isinstance(verdict, sg.Merged):
        return _done(state, flow)
    if isinstance(verdict, sg.NotMerged):
        return _halt(
            state,
            flow,
            "merge-not-merged",
            f"the merge receipt claims success (Decision: OK) but PR #{verdict.number} is {verdict.state} on GitHub, not merged",
        )
    if isinstance(verdict, sg.HeadMismatch):
        return _halt(
            state,
            flow,
            "merge-head-mismatch",
            f"PR #{verdict.number} head {verdict.actual} differs from the approved head {verdict.expected}",
        )
    if isinstance(verdict, sg.GhError):
        return _halt(state, flow, "gh-error", verdict.detail)
    assert_never(verdict)


class _SkipShip:
    """The ship guard found a PR already OPEN at the verified head: ship is skipped and the merge gate is next."""

    def __init__(self, skipped: V2State) -> None:
        self.skipped = skipped


def _ship_guard(state: V2State, flow: Path) -> Union[Outcome, _SkipShip, None]:
    """Before ship spawns: the worktree HEAD and the PR head must still be the verified HEAD (ADR-0026 ship guard)."""
    if state.worktree is None or state.heads.verify is None:
        return None
    local = sg.check_local_head(state.worktree, state.heads.verify)
    if isinstance(local, sg.LocalHeadMismatch):
        return _halt(state, flow, "expected-head-mismatch", f"worktree HEAD {local.actual} is not the verified {local.expected}")
    if isinstance(local, sg.GitError):
        return _halt(state, flow, "step-failed", local.detail)
    branch = _branch_of(state.worktree)
    if isinstance(branch, sg.GitError):
        return _halt(state, flow, "gh-error", f"could not read the ticket branch for the ship guard: {branch.detail}")
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
        # A PR is already OPEN at the verified head: skip ship (plan Step 4) and close the directions out. The skip is
        # certain here, so the close-out runs now. The caller then takes the same merge-gate path a landed ship takes.
        closed = lg.close_out_directions(flow, by=_verify_receipt_name(flow))
        if isinstance(closed, lg.LedgerError):
            return _halt(state, flow, "ledger-error", closed.detail)
        skipped = dataclasses.replace(
            state,
            heads=Heads(verify=state.heads.verify, ship=state.heads.verify),
            step="ship",
            pending_spawn=None,
            live=None,
        )
        return _SkipShip(skipped)
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


def _landed_header(flow: Path, state: V2State, step: Step, header: str) -> Optional[str]:
    """The value of `<header>:` in the archived receipt of the latest landed attempt of step, or None when absent."""
    landed = [attempt for attempt, outcome in state.attempt_log.get(step, {}).items() if outcome == "receipt-landed"]
    if not landed:
        return None
    try:
        text = (flow / "archive" / step / f"a{max(landed)}.landed.md").read_text(encoding="utf-8")
    except OSError:
        return None
    value: Optional[str] = None
    for line in text.split("\n"):
        if line.startswith(f"{header}:"):
            value = line.split(":", 1)[1].strip()
            break
    return value or None


def _step_inputs(flow: Path, state: V2State, step: Step) -> Union[Dict[str, str], str]:
    """
    The values a step command names beyond the issue: PLAN_PATH for implement (the landed plan's FINAL_PLAN_PATH) and
    REVIEW_DIR for fix (the landed review's REVIEW_DIR). A value the receipts do not carry resolves to the documented stub
    `_NOT_RECORDED`, so no prompt is ever rendered with a `<...>` placeholder in it (D6: pinned stub, never a STOP).
    """
    inputs: Dict[str, str] = {"plan_path": "", "review_dir": ""}
    if step == "implement":
        plan_path = _landed_header(flow, state, "plan", "FINAL_PLAN_PATH")
        inputs["plan_path"] = plan_path if plan_path is not None else _NOT_RECORDED
    if step == "fix":
        review_dir = _landed_header(flow, state, "review", "REVIEW_DIR")
        inputs["review_dir"] = review_dir if review_dir is not None else _NOT_RECORDED
    return inputs


def _record_landed_edges(locked: LockedFlow, flow: Path, state: V2State, receipt_fact: ReceiptFact) -> Optional[Outcome]:
    """
    The edges of a landed receipt that run before the transition: the heads are recorded and committed, a landed ship
    is verified against GitHub, and a landed step's Addressed line carries its directions forward. Returns an outcome that
    replaces the transition, or None to let the transition decide.
    """
    if state.status != "running":
        return None
    pending = state.pending_spawn
    if pending is None or pending.state != "receipt-landed" or receipt_fact == "failed":
        return None
    if pending.step == "merge":
        return _merge_arrival(state, flow)
    # The heads are recorded once, when the landed step is first seen; a parked gate question means they already were.
    if pending.step in ("verify", "ship") and state.worktree is not None and state.parked is None:
        head = sg.read_head(state.worktree)
        if isinstance(head, sg.GitError):
            return _halt(state, flow, "step-failed", head.detail)
        if pending.step == "verify":
            heads = Heads(verify=head, ship=state.heads.ship)
        else:
            heads = Heads(verify=state.heads.verify, ship=head)
        locked.commit(dataclasses.replace(state, heads=heads))
        state = locked.require_state()
    if pending.step in CARRY_STEPS and pending.ledger_seq is not None:
        text = _archived_landed_text(locked, pending)
        if text is not None:
            carried = lg.carry_forward(flow, pending.ledger_seq, text)
            if isinstance(carried, lg.UncarriedDirection):
                seqs = ", ".join(str(seq) for seq in carried.seqs)
                return _halt(state, flow, "uncarried-direction", f"directions {seqs} are not in the Addressed line")
            if isinstance(carried, lg.LedgerError):
                return _halt(state, flow, "ledger-error", carried.detail)
    return None


def _spawn_edges(flow: Path, state: V2State, spawn: Step, event_is_start: bool) -> Tuple[Optional[Outcome], str]:
    """
    The edge checks for a SPAWN or MERGE the transition chose, in order: the hook and North Star preflights, then the
    ship ledger close-out (the ship guard runs before this, in _decide). The close-out runs last, so a ship spawn that
    a preflight refuses never closes the directions out. Returns (a stop outcome or None, extra prompt lines).
    """
    stopped, extra = _preflight(state, flow, spawn, event_is_start)
    if stopped is not None:
        return stopped, ""
    if spawn == "ship":
        closed = lg.close_out_directions(flow, by=_verify_receipt_name(flow))
        if isinstance(closed, lg.LedgerError):
            return _halt(state, flow, "ledger-error", closed.detail), ""
    return None, extra


def render_step_prompt(
    flow: Path,
    step: Step,
    attempt: int,
    nonce: str,
    *,
    issue: int,
    issue_url: str,
    worktree: Optional[str],
    ledger_line: str,
    inputs: Mapping[str, str],
    extra: str = "",
) -> str:
    """
    The prompt for one SPAWN: the FLOW_DIR, WORKTREE, RECEIPT, ATTEMPT and NONCE lines the step needs for its receipt,
    the HUMAN_LEDGER line, the North Star lines when degraded, and the command the step runs. The command's placeholders
    come from issue, issue_url, worktree and inputs (the values _step_inputs resolved); a leftover placeholder is a defect.
    """
    template = STEP_COMMANDS.get(step, "the step's command")
    command = template.format(
        issue=issue,
        issue_url=issue_url,
        flow=flow,
        worktree=worktree or "",
        plan_path=inputs.get("plan_path", ""),
        review_dir=inputs.get("review_dir", ""),
    )
    leftover = _LEFTOVER_PLACEHOLDER.findall(command)
    if leftover:
        raise RuntimeError(f"step prompt for {step} still names placeholders {leftover}")
    receipt = receipts.receipt_path(flow, step)
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


def _write_step_prompt(
    flow: Path, state: V2State, directive: Union[SpawnDirective, MergeDirective], ledger_line: str, extra: str
) -> None:
    """
    Write the prompt of a SPAWN or MERGE. Its inputs were resolved before the spawn was committed, so a miss here is a
    defect. A MERGE prompt also names the PR and the approved head the merge gate recorded.
    """
    if isinstance(directive, MergeDirective) and state.merge_gate is not None:
        extra = f"{extra}PR_NUMBER={state.merge_gate.pr_number}\nAPPROVED_HEAD={state.merge_gate.approved_head}\n"
    inputs = _step_inputs(flow, state, directive.step)
    if isinstance(inputs, str):
        raise RuntimeError(f"step prompt for {directive.step}: {inputs}")
    prompt = render_step_prompt(
        flow,
        directive.step,
        directive.attempt,
        directive.nonce,
        issue=state.issue,
        issue_url=_issue_url(state),
        worktree=state.worktree,
        ledger_line=ledger_line,
        inputs=inputs,
        extra=extra,
    )
    _write(step_prompt_path(flow, directive.step, directive.attempt, directive.nonce), prompt)


def _snapshot_for_spawn(
    flow: Path, directive: Union[SpawnDirective, MergeDirective], before: V2State, new_state: V2State
) -> Tuple[V2State, str, Optional[Outcome]]:
    """Take the ledger snapshot for a new spawn; a ledger error replaces the spawn with a ledger-error STOP."""
    snap = lg.snapshot_at_spawn(flow, directive.step, directive.attempt)
    if isinstance(snap, lg.LedgerError):
        return before, "", _halt(before, flow, "ledger-error", snap.detail)
    pending = new_state.pending_spawn
    if pending is None:
        raise RuntimeError("snapshot_for_spawn: the spawn left no pending record")
    stamped = dataclasses.replace(new_state, pending_spawn=dataclasses.replace(pending, ledger_seq=snap.ledger_seq))
    return stamped, lg.human_ledger_line(snap), None


def _legacy_directive_path(flow: Path, pending: PendingSpawn) -> Path:
    """The directive name before the verb was put in it: a SPAWN and a WAIT of one attempt shared this file."""
    return flow / "directives" / f"{pending.step}-a{pending.attempt}-{pending.nonce}.json"


def _spawn_verb(step: Step) -> str:
    """The verb a live attempt of this step was emitted with: the merge step is a MERGE, every other step a SPAWN."""
    return "MERGE" if step == "merge" else "SPAWN"


def _spawn_directive_present(flow: Path, pending: PendingSpawn) -> bool:
    """True when the SPAWN (or MERGE) directive file of the live attempt exists, under its verb-named or legacy name."""
    verb = _spawn_verb(pending.step)
    if directive_file_path(flow, pending.step, pending.attempt, pending.nonce, verb).exists():
        return True
    try:
        legacy = json.loads(_legacy_directive_path(flow, pending).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(legacy, dict) and legacy.get("verb") == verb


def _merge_directive_again(flow: Path, state: V2State, pending: PendingSpawn) -> MergeDirective:
    """
    The MERGE directive of a live merge attempt, rebuilt for a re-render. MergeDirective is constructed only in gates.py,
    so this asks gates for it with the attempt counter wound back one (gates numbers the next attempt) and the live nonce.
    """
    attempts: Dict[Step, int] = dict(state.attempts)
    attempts["merge"] = pending.attempt - 1
    wound = dataclasses.replace(state, attempts=attempts)
    return gates._build_merge_directive(wound, gates.GateFacts(flow=flow, nonce=pending.nonce, landed="merge"))


def _replay_spawn(
    flow: Path, state: V2State, pending: PendingSpawn
) -> Optional[Tuple[Optional[V2State], Directive, str, str]]:
    """
    A live spawned record whose SPAWN files are missing means the crash window between the state commit and the files.
    Re-render the SPAWN from the committed record, with the same nonce, the ledger snapshot re-rendered from the stored
    seq, and the preflight lines. The SPAWN files are the step prompt and the SPAWN-named directive (a live merge
    attempt re-renders as a MERGE, never a second one). Returns (a failed state when a preflight or the ledger now stops
    it, directive, ledger line, extra lines), or None when the files are already on disk and a WAIT is right.
    """
    if step_prompt_path(flow, pending.step, pending.attempt, pending.nonce).exists() and _spawn_directive_present(flow, pending):
        return None
    ledger_line = ""
    if pending.ledger_seq is not None:
        snap = lg.ensure_snapshot(flow, pending.step, pending.attempt, pending.ledger_seq)
        if isinstance(snap, lg.LedgerError):
            failed, stop = _halt(state, flow, "ledger-error", snap.detail)
            return failed, stop, "", ""
        ledger_line = lg.human_ledger_line(snap)
    inputs = _step_inputs(flow, state, pending.step)
    if isinstance(inputs, str):
        failed, stop = _halt(state, flow, "step-failed", inputs)
        return failed, stop, "", ""
    stopped, extra = _preflight(state, flow, pending.step, False)
    if stopped is not None:
        return stopped[0], stopped[1], "", ""
    if pending.step == "merge":
        return None, _merge_directive_again(flow, state, pending), ledger_line, extra
    spawn = SpawnDirective(
        next_command=driver.next_command(flow),
        agent_type=_SPAWN_AGENT,
        prompt_file=str(step_prompt_path(flow, pending.step, pending.attempt, pending.nonce)),
        step=pending.step,
        attempt=pending.attempt,
        nonce=pending.nonce,
    )
    return None, spawn, ledger_line, extra


_MERGE_GATE_STEPS = ("ship", "merge")


def _gate_landed(state: V2State, receipt_fact: ReceiptFact, forced: Optional[Step]) -> Optional[Step]:
    """
    The step the gate stage decides for, or None when no gate stage applies to this call. A parked gate or merge-gate
    question always re-enters its gate; a freshly landed gate step, a skipped ship and a flow re-entered at the merge
    step go through gates.decide too, so PR state is read first on every path to a MERGE.
    """
    if forced is not None:
        return forced
    if state.status != "running":
        return None
    parked = state.parked
    if parked is not None and parked.kind == "gate":
        return parked.step
    if parked is not None and parked.kind == "merge-gate":
        return "ship"
    pending = state.pending_spawn
    if pending is not None and pending.state == "receipt-landed" and receipt_fact != "failed":
        if pending.step in gates.STEP_GATE_STEPS or pending.step == "ship":
            return pending.step
    if pending is None and state.step == "merge":
        return "merge"
    return None


def _gate_attempt(state: V2State, landed: Step) -> int:
    """The attempt a gate question and its answer file are named by: stable across every re-entry of the gate."""
    if landed in _MERGE_GATE_STEPS:
        return max(state.attempts.get("ship", 0), 1)
    if state.parked is not None and state.parked.kind == "gate":
        return state.parked.attempt
    pending = state.pending_spawn
    if pending is not None and pending.step == landed:
        return pending.attempt
    return max(state.attempts.get(landed, 0), 1)


def _read_answer(flow: Path, probe: ParkedQuestion) -> Tuple[Optional[str], Optional[str]]:
    """The (text, note) of the answer file for a parked question, or (None, None) when absent or unreadable."""
    try:
        data = json.loads(relay.answer_path(flow, probe).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    text = data.get("text")
    note = data.get("note")
    return (text if isinstance(text, str) else None), (note if isinstance(note, str) else None)


def _park_question(locked: LockedFlow, flow: Path, spec: gates.QuestionSpec) -> GateAsk:
    """Commit the parked question first (relay.park), then write its question file if it is not there yet."""
    parked = relay.park(locked, flow, spec.step, spec.attempt, spec.kind, nn=1)
    question = relay.question_path(flow, parked)
    if not question.exists():
        options = list(spec.options)
        payload = {
            "summary": spec.question,
            "brief": spec.brief,
            "question": spec.question,
            "options": options,
            "questions": [
                {
                    "id": spec.kind,
                    "question": spec.question,
                    "brief": spec.brief,
                    "options": [{"label": option} for option in options],
                }
            ],
        }
        _write(question, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return GateAsk(options=spec.options)


def _gate_effects(
    locked: LockedFlow, flow: Path, state: V2State, outcome: gates.GateOutcome
) -> Tuple[Optional[GateFact], Optional[Outcome]]:
    """Perform what a gate outcome needs (park, record the merge gate, redo) and turn it into the fact transition reads."""
    if isinstance(outcome, gates.Advance):
        return GateAdvance(), None
    if isinstance(outcome, gates.Park):
        return _park_question(locked, flow, outcome.spec), None
    if isinstance(outcome, gates.RecordMergeGate):
        locked.commit(dataclasses.replace(state, merge_gate=outcome.record))
        return _park_question(locked, flow, outcome.park.spec), None
    if isinstance(outcome, gates.Stop):
        return GateStop(code=outcome.code, detail=outcome.detail, status=outcome.status), None
    if isinstance(outcome, gates.MergeSpawn):
        return GateMerge(outcome.directive), None
    if isinstance(outcome, gates.MergeDone):
        return GateDone(), None
    if isinstance(outcome, gates.Redo):
        pending = state.pending_spawn
        if pending is None or pending.step != outcome.step or pending.state != "receipt-landed":
            return None, _halt(state, flow, "step-failed", f"redo of {outcome.step} found no landed attempt to redo")
        recorded = gates.record_redo_direction(flow, outcome.step, pending.attempt, outcome.note)
        if isinstance(recorded, lg.LedgerError):
            return None, _halt(state, flow, "ledger-error", recorded.detail)
        outcomes = _copy_outcomes(state)
        _set_outcome(outcomes, outcome.step, pending.attempt, "redone", replaces="receipt-landed")
        locked.commit(
            dataclasses.replace(
                state, step=outcome.step, pending_spawn=None, live=None, parked=None, attempt_log=outcomes
            )
        )
        return None, None
    assert_never(outcome)


def _gate_stage(
    locked: LockedFlow, flow: Path, receipt_fact: ReceiptFact, nonce: str, forced: Optional[Step]
) -> Tuple[Optional[GateFact], Optional[Outcome]]:
    """
    The gate stage of one call: gates.decide, then its effects. Returns (the gate fact, None), (None, None) when no gate
    applies (or a redo reopened the step), or (None, a stop outcome). Under the flow lock.
    """
    state = locked.require_state()
    landed = _gate_landed(state, receipt_fact, forced)
    if landed is None:
        return None, None
    attempt = _gate_attempt(state, landed)
    kind = "merge-gate" if landed in _MERGE_GATE_STEPS else "gate"
    probe = ParkedQuestion(
        step="merge" if kind == "merge-gate" else landed, attempt=attempt, nn=1, kind=kind  # type: ignore[arg-type]
    )
    answer, note = _read_answer(flow, probe)
    branch = ""
    if landed in _MERGE_GATE_STEPS and state.merge_gate is None:
        found = _branch_of(state.worktree)
        if isinstance(found, sg.GitError):
            return None, _halt(state, flow, "gh-error", f"could not read the ticket branch for the merge gate: {found.detail}")
        branch = found
    heals = _heal_commits(state.worktree) if landed in _MERGE_GATE_STEPS else ()
    facts = gates.GateFacts(
        flow=flow, nonce=nonce, landed=landed, attempt=attempt, answer=answer, note=note, branch=branch, heal_commits=heals
    )
    return _gate_effects(locked, flow, state, gates.decide(state, facts))


def _decide(
    locked: LockedFlow,
    flow: Path,
    event: object,
    event_is_start: bool,
    receipt_fact: ReceiptFact,
    nonce: str,
    headless: bool,
    forced_gate: Optional[Step] = None,
) -> Tuple[V2State, Directive, str, str]:
    """
    The gate stage, the pure transition for this call and, when it chose a SPAWN or MERGE, the spawn edge checks and the
    ledger snapshot. Returns (the state to commit, the directive, the ledger line, the extra prompt lines). A failed
    edge check replaces the spawn. A ship the guard skips (a PR is already open at the verified head) re-enters here
    with the ship gate forced, so it converges with a landed ship on the same merge-gate path.
    """
    gate, halted = _gate_stage(locked, flow, receipt_fact, nonce, forced_gate)
    if halted is not None:
        return halted[0], halted[1], "", ""
    before = locked.require_state()
    facts = Facts(
        flow=flow,
        nonce=nonce,
        receipt=receipt_fact,
        question=_question_fact(flow, before.pending_spawn),
        headless=headless,
        gate=gate,
        failed_stop=_failed_stop(locked, before.pending_spawn) if receipt_fact == "failed" else None,
    )
    new_state, directive = transition(before, event, facts)  # type: ignore[arg-type]
    if isinstance(directive, SpawnDirective) and directive.step == "ship":
        guarded = _ship_guard(before, flow)
        if isinstance(guarded, _SkipShip):
            locked.commit(guarded.skipped)
            return _decide(locked, flow, event, event_is_start, receipt_fact, nonce, headless, forced_gate="ship")
        if guarded is not None:
            return guarded[0], guarded[1], "", ""
    if not isinstance(directive, (SpawnDirective, MergeDirective)):
        return new_state, directive, "", ""
    edged, extra = _spawn_edges(flow, before, directive.step, event_is_start)
    if edged is not None:
        return edged[0], edged[1], "", ""
    stamped, ledger_line, failure = _snapshot_for_spawn(flow, directive, before, new_state)
    if failure is not None:
        return failure[0], failure[1], "", ""
    return stamped, directive, ledger_line, extra


def _replay_unwritten(
    locked: LockedFlow, flow: Path, directive: Directive, ledger_line: str, extra: str
) -> Tuple[Directive, str, str]:
    """
    A WAIT for a live spawned record whose SPAWN files never reached the disk is re-rendered as the SPAWN (the crash
    window, Q1 = A). A replay that a preflight or the ledger stops commits the failed state here.
    """
    committed = locked.require_state()
    pending = committed.pending_spawn
    if not (isinstance(directive, WaitDirective) and pending is not None and pending.state == "spawned"):
        return directive, ledger_line, extra
    replayed = _replay_spawn(flow, committed, pending)
    if replayed is None:
        return directive, ledger_line, extra
    halted, replay_directive, replay_line, replay_extra = replayed
    if halted is not None:
        locked.commit(halted)
    return replay_directive, replay_line, replay_extra


def _write_directive(flow: Path, state: V2State, directive: Directive) -> Path:
    """Write the verb-named directive file for the live attempt (or for the committed step when no record is live)."""
    pending = state.pending_spawn
    file_nonce = "none" if pending is None else pending.nonce
    attempt = state.attempts.get(state.step, 0) if pending is None else pending.attempt
    step = state.step if pending is None else pending.step
    dfile = directive_file_path(flow, step, attempt, file_nonce, directive.verb)
    _write(dfile, json.dumps(directive_to_dict(directive), indent=2, sort_keys=True) + "\n")
    return dfile


def _step_once(locked: LockedFlow, event_label: Optional[str], flow: Path, now: datetime, headless: bool) -> str:
    event = event_from_label(event_label)
    notified = isinstance(event, Notification)
    event_is_start = event_label == "start"
    nonce = secrets.token_hex(4)
    receipt_fact = _settle_receipt(locked, notified, now)
    landed = _record_landed_edges(locked, flow, locked.require_state(), receipt_fact)
    ledger_line = ""
    extra = ""
    if landed is not None:
        new_state, directive = landed
    else:
        new_state, directive, ledger_line, extra = _decide(locked, flow, event, event_is_start, receipt_fact, nonce, headless)
    if _changed(locked.require_state(), new_state):
        locked.commit(new_state)  # the state replace is the first write of the commit order
    directive, ledger_line, extra = _replay_unwritten(locked, flow, directive, ledger_line, extra)
    committed = locked.require_state()
    if isinstance(directive, (SpawnDirective, MergeDirective)):
        _write_step_prompt(flow, committed, directive, ledger_line, extra)
    dfile = _write_directive(flow, committed, directive)
    pending = committed.pending_spawn
    file_nonce = "none" if pending is None else pending.nonce
    attempt = committed.attempts.get(committed.step, 0) if pending is None else pending.attempt
    step = committed.step if pending is None else pending.step
    locked.log(directive.verb.lower(), step=step, attempt=attempt, nonce=file_nonce)
    return f"NEXT: {directive.verb} {dfile}"


def run_next(flow: Path, after: Optional[str], *, now: Clock = _utc_now, headless: bool = False) -> str:
    """
    One mutating `next`. after is the logged event label (None for a bare re-entry, `start` for the first call).
    Returns the NEXT line.

    Raises:
        Refusal: the flow cannot be driven (a locked flow, or a state.json that is missing or invalid).
        RuntimeError: an internal invariant was broken; the defect is never turned into a STOP.
    """
    try:
        with mutating(flow) as locked:
            return _step_once(locked, after, flow, now(), headless)
    except Refusal as exc:
        if exc.refusal_class == "locked":
            _log_locked_refusal(flow, after or "")
        raise


def run_start(flow: Path, *, step_gates: bool = False, now: Clock = _utc_now) -> str:
    """The first `next` of a v2 flow, sent as the `start` label. Raises what run_next raises."""
    # Set step_gates on the initial state if requested
    if step_gates:
        with mutating(flow) as locked:
            state = locked.require_state()
            if state.step_gates != step_gates:
                new_state = dataclasses.replace(state, step_gates=step_gates)
                locked.commit(new_state)
    return run_next(flow, "start", now=now)


def run_answer(flow: Path, text: str, note: Optional[str] = None) -> str:
    """
    Write the human's answer for the parked attempt. The next `next` resumes it. One answer per attempt.

    Raises:
        LoopRefusal: the record is not parked-awaiting-answer, or an answer file already exists.
        Refusal: the flow is locked or its state.json is missing or invalid.
    """
    try:
        success, message = relay.accept_answer(flow, text, note)
        if success:
            # Log the answer
            with mutating(flow) as locked:
                state = locked.require_state()
                pending = state.pending_spawn
                if pending is not None:
                    locked.log("answer", step=pending.step, attempt=pending.attempt, nonce=pending.nonce)
            return message
        else:
            raise LoopRefusal(message)
    except relay.AnswerRefused as exc:
        raise LoopRefusal(str(exc))


def run_clear_live(flow: Path, reason: str) -> str:
    """
    Clear a spawned or resumed record without a receipt (logged with its nonce). Only a wait-timeout STOP is reopened,
    so the next `next` re-spawns the step with a fresh nonce; a resume-failed STOP stays stopped (D3). A parked record
    is answered, never cleared. The clear and any reopen are one committed transition.

    Raises:
        LoopRefusal: there is no spawned or resumed record to clear.
        Refusal: the flow is locked or its state.json is missing or invalid.
    """
    with mutating(flow) as locked:
        state = locked.require_state()
        pending = state.pending_spawn
        if pending is None or pending.state not in ("spawned", "resumed"):
            raise LoopRefusal("clear-live refused: there is no live record to clear")
        key = TransitionKey(state.run_id, pending.step, pending.attempt)
        code = state.stop_reason.get(state.step, "").partition(": ")[0]
        reopen = state.status == "failed" and code in _CLEARABLE_STOPS

        def _clear_and_reopen(_: LockedFlow, current: V2State) -> V2State:
            outcomes = _copy_outcomes(current)
            _set_outcome(outcomes, key.step, key.attempt, "cleared")
            reasons = dict(current.stop_reason)
            status = current.status
            if reopen:
                reasons.pop(current.step, None)
                status = "running"
            return dataclasses.replace(current, pending_spawn=None, attempt_log=outcomes, status=status, stop_reason=reasons)

        apply_transition(locked, key, "record_cleared", _clear_and_reopen)
        locked.log(
            "clear-live",
            step=pending.step,
            attempt=pending.attempt,
            nonce=pending.nonce,
            reason=reason,
            cleared_state=pending.state,
        )
        return f"cleared live record: attempt {pending.attempt}, nonce {pending.nonce}"


def _count_outcome(state: V2State, step: Step, outcome: AttemptOutcome) -> int:
    return sum(1 for value in state.attempt_log.get(step, {}).values() if value == outcome)


def _retry_locked(locked: LockedFlow, state: V2State, step: Step, attempt: int) -> V2State:
    """
    The single writer of a retry: the cap check (recovery.retry), the "retried" outcome on the attempt, the dropped
    pending record and the cleared stop, in one commit under the lock. Every retry path (a FAILED receipt, an audit
    rejection) goes through here so the cap counts one way.

    Raises:
        LoopRefusal: the step has used its retries (retry-cap).
    """
    decision = rc.retry(step, _count_outcome(state, step, "retried"))
    if isinstance(decision, rc.Stop):
        raise LoopRefusal(stop_reason(decision.code, decision.detail))
    outcomes = _copy_outcomes(state)
    _set_outcome(outcomes, step, attempt, "retried", replaces="receipt-landed")
    reasons: Dict[Step, str] = dict(state.stop_reason)
    reasons.pop(step, None)
    stop_sources = {k: v for k, v in state.stop_source.items() if k != step}
    new_state = dataclasses.replace(
        state,
        status="running",
        pending_spawn=None,
        parked=None,
        stop_reason=reasons,
        stop_source=stop_sources,
        attempt_log=outcomes,
    )
    locked.commit(new_state)
    return new_state


def run_recover(flow: Path, choice: str) -> str:
    """
    The human's answer to a hard stop: retry (a FAILED receipt, at most two times), take-over, or keep-previous.
    Each is one replace under the lock and is logged. A refused choice names its cap or reason.

    Raises:
        LoopRefusal: the flow is not stopped on a step, the choice is refused (retry without a FAILED landing, a cap,
            keep-previous, or an unknown choice).
        Refusal: the flow is locked or its state.json is missing or invalid.
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
            audit_rejected = code == "audit-rejected" and state.parked is not None and state.parked.kind == "audit"
            if not audit_rejected and (code != "step-failed" or not _landed_failed(locked, pending)):
                raise LoopRefusal(
                    "recover refused: retry is offered only after a FAILED receipt or an audit rejection"
                )
            _retry_locked(locked, state, step, attempt)
            locked.log("recover", step=step, attempt=attempt, choice="retry")
            return f"recovered: retry {step} (attempt {attempt} is retried; the next next spawns a new attempt)"
        if choice == "take-over":
            reasons[step] = stop_reason("take-over", f"step {step} was taken over by hand")
            locked.commit(dataclasses.replace(state, status="paused", stop_reason=reasons))
            locked.log("recover", step=step, attempt=attempt, choice="take-over")
            return f"recovered: take-over of {step} recorded; the driver will not spawn it again"
        if choice == "keep-previous":
            raise LoopRefusal("recover refused: keep-previous needs a redo lineage, which Layer 2 does not build")
        raise LoopRefusal(f"recover refused: unknown choice {choice!r}")


def run_abandon(flow: Path) -> str:
    """
    Abandon a flow. A live record is refused and names clear-live; a parked or landed record is dropped. A landed
    attempt keeps its receipt-landed outcome (outcomes are write-once).

    Raises:
        LoopRefusal: the flow is already done or abandoned, or its record is live.
        Refusal: the flow is locked or its state.json is missing or invalid.
    """
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
        outcomes = _copy_outcomes(state)
        if pending is not None and outcomes.get(pending.step, {}).get(pending.attempt) is None:
            _set_outcome(outcomes, pending.step, pending.attempt, "abandoned")
        locked.commit(
            dataclasses.replace(
                state,
                status="abandoned",
                pending_spawn=None,
                attempt_log=outcomes,
            )
        )
        locked.log("abandon", step=state.step, attempt=state.attempts.get(state.step, 0))
        return f"abandoned: {flow}"
