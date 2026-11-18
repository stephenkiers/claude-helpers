"""
Step gates, the merge gate and the sole MergeDirective constructor for the v2 driver (Layer 3 Step 8; ADR-0026;
sessions W1 and Q4).

One entry point, decide(state, facts) -> GateOutcome. It is pure except through the two injected PR readers
(`check_pr`, `check_pr_by_number`), which default to the shipguard functions. It takes no lock, writes nothing and never
imports loop (loop.py owns every effect: committing state, relay.park, writing the directive file, archiving a redone
attempt). It does not call loop._ship_guard, so no ledger close-out runs inside the gate's pre-check.

Step gates (state.step_gates): after plan, implement, review, fix or verify lands and passes audit, a `gate` question is
parked. Options are Continue / Redo with notes (plan and review only) / Pause / Abort, or Continue / Pause / Abort for
the other steps. Redo is capped by recovery.REDO_CAP, counted from attempt_log; the note reaches the ledger only through
record_redo_direction(), which runs scripts/flow-ledger.py by argv.

Merge gate: reached once ship has landed (or was skipped at the same head) and the assess gate passed. PR state is read
first. An already-merged PR is MergeDone with no question. Otherwise the gate records the PR identity and approved head
and parks a `merge-gate` question with exactly [Merge, Hold, Take over]. Every merge asks, healed or not: no parameter,
flag or state field skips the question. On Merge the PR is re-read by number against the approved head (TOCTOU) and only
a same-head OPEN PR yields MergeSpawn. MergeDirective is constructed here and nowhere else.

The question brief is built from fixed templates. Healing context (state.healed, recovery.heals, Flow-Heal: commit
subjects) is shown as data only: it is never an option and never part of a command.

Python 3.8 floor: typing.Callable/Literal/Optional/Tuple/Union only.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Protocol, Tuple, Union

from . import driver, recovery, shipguard as sg
from .ledger import LedgerError, ledger_script
from .model import GENERAL_PURPOSE_AGENT, assert_never
from .preflight import CANNOT_START, Runner, run_command
from .state import MergeGateRecord, Sha, Step, V2State
from .steps import AgentType, MergeDirective, StopCode, step_prompt_path

REDO_CAP = recovery.REDO_CAP

_MERGE_AGENT: AgentType = "general-purpose"
if _MERGE_AGENT != GENERAL_PURPOSE_AGENT:
    raise RuntimeError("gates._MERGE_AGENT does not match model.GENERAL_PURPOSE_AGENT")

STEP_GATE_STEPS: Tuple[Step, ...] = ("plan", "implement", "review", "fix", "verify")
REDO_STEPS: Tuple[Step, ...] = ("plan", "review")

OPT_CONTINUE = "Continue"
OPT_REDO = "Redo with notes"
OPT_PAUSE = "Pause"
OPT_ABORT = "Abort"
OPT_MERGE = "Merge"
OPT_HOLD = "Hold"
OPT_TAKE_OVER = "Take over"
MERGE_GATE_OPTIONS: Tuple[str, ...] = (OPT_MERGE, OPT_HOLD, OPT_TAKE_OVER)

_MAX_CONTEXT_CHARS = 100
_MAX_CONTEXT_ITEMS = 5
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

class PrReader(Protocol):
    """The by-branch PR check (shipguard.check_pr); tests inject a fake with this shape."""

    def __call__(
        self, branch: str, expected: Sha, run: Runner = run_command, repo: Optional[str] = None
    ) -> sg.ShipGuardResult: ...


class PrNumberReader(Protocol):
    """The by-number PR check (shipguard.check_pr_by_number); tests inject a fake with this shape."""

    def __call__(self, number: int, repo: str, expected: Sha, run: Runner = run_command) -> sg.MergeVerdict: ...


@dataclass(frozen=True)
class GateFacts:
    """
    What the loop gathers for one decide() call.

    landed: the step whose receipt just landed and passed audit (a step-gate step, or "ship"/"merge" for the merge gate).
    attempt: the landed step's attempt number (names the gate question and its answer file).
    answer: the current answer text for the parked gate question, or None when it is not yet answered.
    note: the optional free-text note given with a Redo answer.
    branch: the ticket branch, read by the loop from the worktree (used only for the first PR discovery).
    heal_commits: raw `Flow-Heal:` commit subjects from `git log <base>..HEAD`, shown as context only.
    """

    flow: Path
    nonce: str
    landed: Step
    attempt: int = 1
    answer: Optional[str] = None
    note: Optional[str] = None
    branch: str = ""
    heal_commits: Tuple[str, ...] = ()
    check_pr: PrReader = field(default=sg.check_pr)
    check_pr_by_number: PrNumberReader = field(default=sg.check_pr_by_number)


@dataclass(frozen=True)
class QuestionSpec:
    """The question relay.park and the question-file writer need: kind, step, attempt, text, brief and exact options."""

    kind: Literal["gate", "merge-gate"]
    step: Step
    attempt: int
    question: str
    brief: str
    options: Tuple[str, ...]


@dataclass(frozen=True)
class Advance:
    """No gate stands in the way: the caller advances to the next step."""


@dataclass(frozen=True)
class Park:
    """Park this question (relay.park, then the question file) and wait for the answer."""

    spec: QuestionSpec


@dataclass(frozen=True)
class Stop:
    """Stop with this code. status is `paused` for Pause and Hold (resume re-asks), else `failed`."""

    code: StopCode
    detail: str
    status: Literal["failed", "paused"] = "failed"


@dataclass(frozen=True)
class Redo:
    """
    Redo this step. The caller archives the attempt under archive/<step>/, records outcome `redone`, calls
    record_redo_direction with the note and re-spawns the step.
    """

    step: Step
    note: Optional[str]


@dataclass(frozen=True)
class MergeSpawn:
    """Emit this MERGE directive. The PR was re-read at the approved head immediately before."""

    directive: MergeDirective


@dataclass(frozen=True)
class MergeDone:
    """The PR is MERGED at the expected head. No question and no MERGE; the caller moves to DONE."""

    pr_number: int


@dataclass(frozen=True)
class RecordMergeGate:
    """Record the merge_gate identity in state, then park `park` (one commit, one flow-lock hold)."""

    record: MergeGateRecord
    park: Park


GateOutcome = Union[Advance, Park, Stop, Redo, MergeSpawn, MergeDone, RecordMergeGate]


def record_redo_direction(
    flow: Path, step: str, attempt: int, note: Optional[str], run: Runner = run_command
) -> Union[int, None, LedgerError]:
    """
    Record a Redo note as a `direction` ledger record, only through scripts/flow-ledger.py (list argv, no shell).

    Returns the new seq, None when there is no note (the ledger refuses empty text, so nothing is written), or a
    LedgerError. The note is passed as `--text=<note>` so a leading dash is never read as a flag.
    """
    if note is None or not note.strip():
        return None
    if attempt < 1:
        return LedgerError("redo direction attempt must be >= 1")
    argv = [
        sys.executable,
        str(ledger_script()),
        "append",
        "--flow-dir",
        str(flow),
        "--kind",
        "direction",
        "--step",
        step,
        "--attempt",
        str(attempt),
        f"--text={note.strip()}",
    ]
    result = run(argv)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        detail = lines[0] if lines else f"exit {result.returncode}"
        prefix = "could not start" if result.returncode == CANNOT_START else f"exited {result.returncode}"
        return LedgerError(f"flow-ledger.py append {prefix}: {detail}")
    try:
        return int(result.stdout.strip())
    except ValueError:
        return LedgerError("flow-ledger.py append did not print a seq number")


def _redone(state: V2State, step: Step) -> int:
    return sum(1 for outcome in state.attempt_log.get(step, {}).values() if outcome == "redone")


def _clean(text: str) -> str:
    """Untrusted text reduced to one bounded printable line. It is shown as data and never executed."""
    one_line = _CONTROL.sub(" ", text).strip()
    if len(one_line) > _MAX_CONTEXT_CHARS:
        return one_line[:_MAX_CONTEXT_CHARS] + "..."
    return one_line


def _heal_context(state: V2State, facts: GateFacts) -> str:
    """The healing context for the merge-gate brief, from fixed templates. Context only; it is never an option."""
    healed = "yes" if state.healed else "no"
    lines = [f"Healed during this flow: {healed}.", f"Recovery heals recorded: {state.recovery.heals}."]
    if facts.heal_commits:
        shown = facts.heal_commits[:_MAX_CONTEXT_ITEMS]
        lines.append(f"Flow-Heal commits on the branch: {len(facts.heal_commits)}.")
        for subject in shown:
            lines.append(f"  - (commit text, data only) {_clean(subject)}")
        if len(facts.heal_commits) > len(shown):
            lines.append(f"  - ... and {len(facts.heal_commits) - len(shown)} more.")
    else:
        lines.append("Flow-Heal commits on the branch: 0.")
    return "\n".join(lines)


def _step_gate_options(step: Step) -> Tuple[str, ...]:
    if step in REDO_STEPS:
        return (OPT_CONTINUE, OPT_REDO, OPT_PAUSE, OPT_ABORT)
    return (OPT_CONTINUE, OPT_PAUSE, OPT_ABORT)


def _step_gate_park(state: V2State, facts: GateFacts) -> Park:
    step = facts.landed
    spec = QuestionSpec(
        kind="gate",
        step=step,
        attempt=facts.attempt,
        question=f"Step {step} landed and passed its audit. How should the flow proceed?",
        brief=(
            f"Step {step} attempt {facts.attempt} is complete for issue #{state.issue}. "
            f"Continue moves on to the next step. Pause stops the flow so you can resume later; resuming asks again."
        ),
        options=_step_gate_options(step),
    )
    return Park(spec)


def _decide_step_gate(state: V2State, facts: GateFacts) -> GateOutcome:
    step = facts.landed
    if not state.step_gates:
        return Advance()
    answer = facts.answer.strip() if facts.answer is not None else None
    if answer is None:
        return _step_gate_park(state, facts)
    if answer not in _step_gate_options(step):
        return Stop("gate-aborted", f"step {step} gate got an answer that is not one of its options")
    if answer == OPT_CONTINUE:
        return Advance()
    if answer == OPT_PAUSE:
        return Stop("gate-paused", f"the flow is paused at the step {step} gate; resume asks again", "paused")
    if answer == OPT_ABORT:
        return Stop("gate-aborted", f"the human aborted at the step {step} gate")
    # OPT_REDO: only offered for plan and review, checked above through the option set
    decision = recovery.redo(step, _redone(state, step))
    if isinstance(decision, recovery.Stop):
        return Stop("redo-cap", decision.detail)
    return Redo(step=step, note=facts.note)


def _merge_gate_park(state: V2State, facts: GateFacts, record: MergeGateRecord) -> Park:
    brief = (
        f"PR #{record.pr_number} in {record.repo} is OPEN at head {record.approved_head} for issue #{state.issue}. "
        f"Merge runs the merge step against exactly this head; if the head changes first, the merge stops. "
        f"Hold pauses the flow. Take over leaves the merge to you.\n" + _heal_context(state, facts)
    )
    spec = QuestionSpec(
        kind="merge-gate",
        step="merge",
        attempt=facts.attempt,
        question=f"Merge PR #{record.pr_number} at head {record.approved_head}?",
        brief=brief,
        options=MERGE_GATE_OPTIONS,
    )
    return Park(spec)


def _build_merge_directive(state: V2State, facts: GateFacts) -> MergeDirective:
    """The sole construction of a MergeDirective in the codebase."""
    attempt = state.attempts.get("merge", 0) + 1
    return MergeDirective(
        next_command=driver.next_command(facts.flow),
        agent_type=_MERGE_AGENT,
        prompt_file=str(step_prompt_path(facts.flow, "merge", attempt, facts.nonce)),
        step="merge",
        attempt=attempt,
        nonce=facts.nonce,
    )


def _answer_outcome(state: V2State, facts: GateFacts, record: MergeGateRecord, verdict: sg.MergeVerdict) -> GateOutcome:
    """Map the merge-gate answer, given the fresh by-number read, to an outcome."""
    answer = facts.answer.strip() if facts.answer is not None else None
    if isinstance(verdict, sg.GhError):
        # A read failure never lets a merge through; Hold and Take over need no read, so they are still honoured.
        if answer == OPT_HOLD:
            return Stop("merge-held", "the human held the merge", "paused")
        if answer == OPT_TAKE_OVER:
            return Stop("take-over", "the human took over the merge")
        return Stop("gh-error", f"could not re-read PR #{record.pr_number} before the merge: {verdict.detail}")
    if isinstance(verdict, sg.Merged):
        return MergeDone(pr_number=verdict.number)
    if isinstance(verdict, sg.HeadMismatch):
        return Stop(
            "merge-head-mismatch",
            f"PR #{verdict.number} head {verdict.actual} differs from the approved head {verdict.expected}",
        )
    if isinstance(verdict, sg.NotMerged):
        if verdict.state != "OPEN":
            return Stop("merge-not-merged", f"PR #{verdict.number} is {verdict.state} and not merged")
        if answer == OPT_MERGE:
            return MergeSpawn(_build_merge_directive(state, facts))
        if answer == OPT_HOLD:
            return Stop("merge-held", "the human held the merge", "paused")
        if answer == OPT_TAKE_OVER:
            return Stop("take-over", "the human took over the merge")
        if answer is None:
            return _merge_gate_park(state, facts, record)
        return Stop("take-over", "the merge gate got an answer that is not one of its options")
    assert_never(verdict)


def _decide_merge_gate(state: V2State, facts: GateFacts) -> GateOutcome:
    existing = state.merge_gate
    if existing is not None:
        verdict = facts.check_pr_by_number(existing.pr_number, existing.repo, existing.approved_head)
        return _answer_outcome(state, facts, existing, verdict)

    expected = state.heads.ship
    if expected is None:
        return Stop("gh-error", "no ship head is recorded, so the PR cannot be checked before the merge gate")
    if not facts.branch:
        return Stop("gh-error", "no ticket branch was supplied for the PR discovery")
    result = facts.check_pr(facts.branch, expected, repo=state.repo)
    if isinstance(result, sg.Merged):
        return MergeDone(pr_number=result.number)
    if isinstance(result, sg.SameHead):
        record = MergeGateRecord(pr_number=result.number, approved_head=Sha(result.head), repo=state.repo)
        return RecordMergeGate(record=record, park=_merge_gate_park(state, facts, record))
    if isinstance(result, sg.HeadDiffers):
        return Stop(
            "merge-head-mismatch",
            f"PR #{result.number} head {result.actual} differs from the shipped head {result.expected}",
        )
    if isinstance(result, sg.Closed):
        return Stop("merge-not-merged", f"PR #{result.number} is closed and not merged")
    if isinstance(result, sg.NoPr):
        return Stop("gh-error", "no PR exists for the ticket branch at the merge gate")
    if isinstance(result, sg.GhError):
        return Stop("gh-error", result.detail)
    assert_never(result)


def decide(state: V2State, facts: GateFacts) -> GateOutcome:
    """
    The one gate decision. A step-gate step gets the step gate; ship (landed or skipped at the same head) and merge
    re-entry get the merge gate, PR state first. Any other step has no gate and advances.
    """
    if facts.landed in STEP_GATE_STEPS:
        return _decide_step_gate(state, facts)
    if facts.landed in ("ship", "merge"):
        return _decide_merge_gate(state, facts)
    return Advance()
