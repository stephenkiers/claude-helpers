"""
Recovery decision table for the v2 driver (Layer 2 Step 3; ADR-0026; sessions D1, D3, D5, Q2 and Q3).

Data plus pure lookups. Nothing here spawns, writes, locks or reads the clock: each lookup takes the counter it
needs as an argument and returns either the decision it allows or a Stop with the StopCode it maps to. loop.py and
steps.py own every effect.

Wiring (Layer 3, #487): `retry` and `take_over` are dispatched by loop.run_recover (retry through the single writer
`_retry_locked`, which an audit rejection also uses), and `redo` by the step gate. `malformed_rerun` is read by
loop._malformed_gate (D4/D5) alongside `receipts.rerun_malformed`. The other decisions (auto_rebase, heal,
unknown_skill_retry, keep_previous) are still table-only and not read by the loop; they belong to the recovery
follow-up named in docs/v2-inventory/rules.md. The single source for the flow-level heal count is `recovery.heals`
(state.py Recovery), which HEAL_CAP is checked against; the top-level `healed` boolean in state.json is a Layer 1
field and is not read here.

Python 3.8 floor: typing.FrozenSet, Literal, Optional, Union, Tuple only.
"""

from dataclasses import dataclass
from typing import FrozenSet, Optional, Union

from .model import assert_never
from .state import Step
from .steps import StopCode

RETRY_CAP = 2  # max retried outcomes per step
REDO_CAP = 2  # max redone outcomes per step (plan and review only)
UNKNOWN_SKILL_CAP = 1  # one automatic re-spawn per step after an Unknown-skill failure
REBASE_CAP = 1  # per flow, from recovery.rebases
HEAL_CAP = 1  # per flow, from recovery.heals
MALFORMED_CAP = 1  # malformed reruns per attempt (session D5)

# Session D3: the steps whose failed RESUME may re-spawn. Empty in Layer 2, so every failed RESUME is a STOP.
W1_ALLOWLIST: FrozenSet[Step] = frozenset()


@dataclass(frozen=True)
class Retry:
    step: Step


@dataclass(frozen=True)
class TakeOver:
    step: Step


@dataclass(frozen=True)
class KeepPrevious:
    step: Step
    previous_attempt: int


@dataclass(frozen=True)
class Redo:
    step: Step


@dataclass(frozen=True)
class UnknownSkillRetry:
    step: Step


@dataclass(frozen=True)
class AutoRebase:
    step: Step


@dataclass(frozen=True)
class Heal:
    step: Step


@dataclass(frozen=True)
class MalformedRerun:
    step: Step
    attempt: int


@dataclass(frozen=True)
class Stop:
    code: StopCode
    detail: str


RecoveryDecision = Union[Retry, TakeOver, KeepPrevious, Redo, UnknownSkillRetry, AutoRebase, Heal, MalformedRerun, Stop]


def retry(step: Step, retried: int) -> Union[Retry, Stop]:
    """Offered only on a FAILED receipt. A third retry is refused with retry-cap."""
    if retried >= RETRY_CAP:
        return Stop("retry-cap", f"step {step} has been retried {retried} times; the cap is {RETRY_CAP}")
    return Retry(step)


def redo(step: Step, redone: int) -> Union[Redo, Stop]:
    """Plan and review only. A third redo is refused with redo-cap."""
    if redone >= REDO_CAP:
        return Stop("redo-cap", f"step {step} has been redone {redone} times; the cap is {REDO_CAP}")
    return Redo(step)


def unknown_skill_retry(step: Step, stop_reason: str, skill: str, retried_unknown_skill: int) -> Optional[UnknownSkillRetry]:
    """
    Automatic one-shot. Only when stop_reason is exactly `skill invocation failed: Unknown skill: <skill>`, where
    skill is the step's own skill, and only while the step has no unknown-skill-retried outcome yet.
    Anything else is None, so the caller falls through to the normal recovery STOP.
    """
    if retried_unknown_skill >= UNKNOWN_SKILL_CAP:
        return None
    if stop_reason != f"skill invocation failed: Unknown skill: {skill}":
        return None
    return UnknownSkillRetry(step)


def auto_rebase(step: Step, rebases: int) -> Union[AutoRebase, Stop]:
    """A base-stale verify failure. A second base-stale failure in the same flow is rebase-cap."""
    if rebases >= REBASE_CAP:
        return Stop("rebase-cap", f"flow has already auto-rebased {rebases} time(s); the cap is {REBASE_CAP}")
    return AutoRebase(step)


def heal(step: Step, heals: int) -> Union[Heal, Stop]:
    """A lint or deterministic test verify failure. One heal per flow; a second is heal-cap."""
    if heals >= HEAL_CAP:
        return Stop("heal-cap", f"flow has already healed {heals} time(s); the cap is {HEAL_CAP}")
    return Heal(step)


def malformed_rerun(step: Step, attempt: int, previous_attempt_malformed: bool) -> Union[MalformedRerun, Stop]:
    """
    Session D5: one rerun of a malformed receipt. previous_attempt_malformed is true when the attempt before this
    one already recorded receipt-malformed, so a second malformed receipt in a row is malformed-cap.
    """
    if previous_attempt_malformed:
        return Stop("malformed-cap", f"step {step} receipt is malformed again; the rerun is capped at {MALFORMED_CAP}")
    return MalformedRerun(step, attempt)


def keep_previous(step: Step, current_is_redo: bool, previous_attempt: int, previous_landed_ok: bool) -> Union[KeepPrevious, Stop]:
    """
    Valid only for a redo whose previous attempt has an OK landed receipt in the archive. A FAILED receipt never
    becomes OK by a rebuild (plan Step 3), so only an OK previous receipt qualifies.
    """
    if not current_is_redo:
        return Stop("step-failed", f"keep-previous is valid only for a redo of step {step}")
    if not previous_landed_ok:
        return Stop("step-failed", f"step {step} attempt {previous_attempt} has no OK landed receipt to keep")
    return KeepPrevious(step, previous_attempt)


def take_over(step: Step) -> TakeOver:
    return TakeOver(step)


def decision_code(decision: RecoveryDecision) -> str:
    """A short name for the driver log. Every case of the union is handled."""
    if isinstance(decision, Retry):
        return "retry"
    if isinstance(decision, TakeOver):
        return "take-over"
    if isinstance(decision, KeepPrevious):
        return "keep-previous"
    if isinstance(decision, Redo):
        return "redo"
    if isinstance(decision, UnknownSkillRetry):
        return "unknown-skill-retry"
    if isinstance(decision, AutoRebase):
        return "auto-rebase"
    if isinstance(decision, Heal):
        return "heal"
    if isinstance(decision, MalformedRerun):
        return "malformed-rerun"
    if isinstance(decision, Stop):
        return f"stop:{decision.code}"
    assert_never(decision)
