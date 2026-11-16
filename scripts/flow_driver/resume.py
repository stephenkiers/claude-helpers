"""
Resume cascade for the v2 driver (Layer 1 Step 6, ADR-0026 §11).

The ordered rule table (rules.md §1.2 order). Layer 1 gives real behaviour only to Rule 1
(rule-1-live-step), which checks receipts and makes transitions (land, archive_stale, rerun_malformed,
store_fingerprint). Later-layer rules resolve to a named fail-closed "not yet built (Layer N)" outcome.

A fresh init state fires rule-1a-pending-input with an unknown fact, which fails closed. The fired rule
is logged and printed to stdout.

Python 3.8 floor: no match statements, no f-strings with = debug syntax, no X | Y at runtime.
"""

import dataclasses
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, NamedTuple, Optional, Tuple

from . import receipts
from .model import assert_never
from .state import LockedFlow, PendingSpawn, TransitionKey, V2State

RuleName = Literal[
    "rule-0-redo",
    "rule-1-live-step",
    "rule-2-paused-gate",
    "rule-2a-assess",
    "rule-3-done",
    "rule-3a-abandoned",
    "rule-1a-pending-input",
    "rule-1b-heal-in-progress",
    "rule-4-failed",
    "rule-5-subagent-gone",
    "rule-3b-failed-redo",
    "rule-6-merge-pr-state",
    "rule-7-worktree",
    "rule-8-epic",
    "rule-9-flags",
    "rule-10-telemetry",
]


@dataclass(frozen=True)
class ResumeFacts:
    """Tri-state facts for the resume cascade (Layer 1 fills only the layer-1 facts)."""

    state: V2State
    redo_requested: bool
    pending_human_input: Optional[bool]
    heal_in_progress: Optional[bool]
    subagent_gone: Optional[bool]
    assess_due: Optional[bool]
    passing_outstanding_check: Optional[bool]
    # Later-layer rules as overrides (default empty, tests may inject)
    later: Mapping[RuleName, Optional[bool]] = dataclasses.field(default_factory=dict)


class CascadeExhausted(Exception):
    """No rule fired: the cascade is incomplete. The CLI maps this to a refusal (exit 1)."""


class ResumeDecision(NamedTuple):
    """The outcome of decide(). Outcomes: wait, receipt-landed, rerun (acted-on); not-yet-built (fail-closed)."""

    rule: RuleName
    outcome: Literal["wait", "receipt-landed", "rerun", "not-yet-built"]
    layer: Literal[1, 2, 3, 4]


class ResumeRule(NamedTuple):
    name: RuleName
    layer: Literal[1, 2, 3, 4]
    # Predicate returns True (rule fires), False (rule doesn't fire), or None (unknown fact, fail closed)
    predicate: Callable[[ResumeFacts], Optional[bool]]
    # Action to execute if the predicate is True or None. Only Rule 1 has real action in Layer 1.
    # Other rules return a fail-closed "not-yet-built" outcome.


def _rule_0_redo(facts: ResumeFacts) -> Optional[bool]:
    """True when redo_requested is True (--redo given), else False. Pure."""
    return facts.redo_requested


def _rule_1_live_step(facts: ResumeFacts) -> Optional[bool]:
    """True when status is running or awaiting and pending_spawn is spawned or resumed, else False. Pure."""
    if facts.state.status not in ("running", "awaiting"):
        return False
    pending = facts.state.pending_spawn
    if pending is None:
        return False
    return pending.state in ("spawned", "resumed")


def _rule_2_paused_gate(facts: ResumeFacts) -> Optional[bool]:
    """True when status == "paused" and step is one of plan, implement, review, fix, verify, else False. Pure."""
    if facts.state.status != "paused":
        return False
    gated_steps = ("plan", "implement", "review", "fix", "verify")
    return facts.state.step in gated_steps  # type: ignore[return-value]


def _rule_2a_assess(facts: ResumeFacts) -> Optional[bool]:
    """
    False when pending_spawn is not None or status != "running".
    True when step == "assess".
    When step is "ship" or "merge", return not passing_outstanding_check (None when unknown).
    False for every other step.
    Pure until ship/merge.
    """
    if facts.state.pending_spawn is not None:
        return False
    if facts.state.status != "running":
        return False
    if facts.state.step == "assess":
        return True
    if facts.state.step in ("ship", "merge"):
        # When unknown, fail closed (return None, which makes this rule not fire)
        if facts.passing_outstanding_check is None:
            return None
        return not facts.passing_outstanding_check
    return False


def _rule_3_done(facts: ResumeFacts) -> Optional[bool]:
    """status == "done". Pure."""
    return facts.state.status == "done"


def _rule_3a_abandoned(facts: ResumeFacts) -> Optional[bool]:
    """status == "abandoned". Pure."""
    return facts.state.status == "abandoned"


def _rule_1a_pending_input(facts: ResumeFacts) -> Optional[bool]:
    """
    True when pending_spawn.state == "parked-awaiting-answer" or status == "awaiting".
    Otherwise the value of pending_human_input (None in Layer 1).
    """
    pending = facts.state.pending_spawn
    if pending is not None and pending.state == "parked-awaiting-answer":
        return True
    if facts.state.status == "awaiting":
        return True
    # Return the pending_human_input fact (None in Layer 1, fails closed)
    return facts.pending_human_input


# Layer 1 rules beyond 1a: fail-closed, return None to inherit the "not-yet-built" outcome


def _rule_1b_heal_in_progress(facts: ResumeFacts) -> Optional[bool]:
    """Layer 2+. Reads heal_in_progress; Layer 1 returns None (unknown)."""
    return facts.heal_in_progress


def _rule_4_failed(facts: ResumeFacts) -> Optional[bool]:
    """Layer 2+. status == "failed" pure; Layer 1 would see this in real states, but the action is Layer 2+."""
    return facts.state.status == "failed"


def _rule_5_subagent_gone(facts: ResumeFacts) -> Optional[bool]:
    """
    Layer 2+. False unless status is awaiting, paused or running, then reads subagent_gone.
    Layer 1 returns None (unknown).
    """
    if facts.state.status not in ("awaiting", "paused", "running"):
        return False
    return facts.subagent_gone


# Every later rule below reads facts.later[name], which is None unless a test injects it.


RULES: Tuple[ResumeRule, ...] = (
    ResumeRule("rule-0-redo", 3, _rule_0_redo),
    ResumeRule("rule-1-live-step", 1, _rule_1_live_step),
    ResumeRule("rule-2-paused-gate", 3, _rule_2_paused_gate),
    ResumeRule("rule-2a-assess", 3, _rule_2a_assess),
    ResumeRule("rule-3-done", 2, _rule_3_done),
    ResumeRule("rule-3a-abandoned", 3, _rule_3a_abandoned),
    ResumeRule("rule-1a-pending-input", 2, _rule_1a_pending_input),
    ResumeRule("rule-1b-heal-in-progress", 3, _rule_1b_heal_in_progress),
    ResumeRule("rule-4-failed", 3, _rule_4_failed),
    ResumeRule("rule-5-subagent-gone", 2, _rule_5_subagent_gone),
    ResumeRule("rule-3b-failed-redo", 3, lambda facts: facts.later.get("rule-3b-failed-redo")),
    ResumeRule("rule-6-merge-pr-state", 3, lambda facts: facts.later.get("rule-6-merge-pr-state")),
    ResumeRule("rule-7-worktree", 2, lambda facts: facts.later.get("rule-7-worktree")),
    ResumeRule("rule-8-epic", 4, lambda facts: facts.later.get("rule-8-epic")),
    ResumeRule("rule-9-flags", 3, lambda facts: facts.later.get("rule-9-flags")),
    ResumeRule("rule-10-telemetry", 2, lambda facts: facts.later.get("rule-10-telemetry")),
)


def _rule_1_action(locked: LockedFlow, pending: PendingSpawn) -> Literal["wait", "receipt-landed", "rerun"]:
    """
    Rule 1 (rule-1-live-step) real action: classify the receipt and execute transitions.

    Outcomes:
    - Absent -> wait (pending_spawn with no receipt is recoverable)
    - InProgress -> store fingerprint, then wait
    - Landed -> land, then receipt-landed
    - Stale -> archive_stale, then wait
    - Malformed -> rerun_malformed, then rerun
    """
    key = TransitionKey(locked.state.run_id, pending.step, pending.attempt)

    # Roll forward a move that crashed between the archive rename and the state commit: the receipt is gone
    # but its archive exists while pending_spawn still says live.
    forward = receipts.roll_forward(locked, key, pending)
    if forward is not None:
        return forward

    classification = receipts.classify(locked, pending)

    if classification.state == "absent":
        return "wait"
    elif classification.state == "in-progress":
        # Store the fingerprint for two-read stability detection
        if classification.fingerprint is not None:
            receipts.store_fingerprint(locked, key, pending.step, pending.attempt, classification.fingerprint)
        return "wait"
    elif classification.state == "landed":
        try:
            receipts.land(locked, key, pending.step, pending.attempt)
        except receipts.ReceiptChanged:
            locked.log("receipt-changed-while-archiving", step=pending.step, attempt=pending.attempt, action="land")
            return "wait"
        return "receipt-landed"
    elif classification.state == "stale":
        # Archive the stale receipt; pending_spawn stays pending
        if classification.fingerprint is not None:
            try:
                receipts.archive_stale(locked, key, pending.step, pending.attempt, classification.fingerprint.sha256)
            except receipts.ReceiptChanged:
                locked.log("receipt-changed-while-archiving", step=pending.step, attempt=pending.attempt, action="archive_stale")
        return "wait"
    elif classification.state == "malformed":
        # Rerun: archive malformed, clear pending, return rerun decision
        try:
            receipts.rerun_malformed(locked, key, pending.step, pending.attempt)
        except receipts.ReceiptChanged:
            locked.log("receipt-changed-while-archiving", step=pending.step, attempt=pending.attempt, action="rerun_malformed")
            return "wait"
        return "rerun"
    else:
        # Exhaustive dispatch
        assert_never(classification.state)


def decide(locked: LockedFlow, facts: ResumeFacts) -> ResumeDecision:
    """
    Walk RULES in order. The first rule whose predicate is True or None fires.

    For Rule 1 (rule-1-live-step), execute the action and return the outcome.
    For later-layer rules, return a fail-closed "not-yet-built" outcome.
    """
    for rule in RULES:
        predicate_result = rule.predicate(facts)

        # Predicate is False: continue to next rule
        if predicate_result is False:
            continue

        # Predicate is True or None: rule fires
        outcome: Literal["wait", "receipt-landed", "rerun", "not-yet-built"]
        if rule.name == "rule-1-live-step":
            # Rule 1 has real behavior in Layer 1; its predicate guarantees a live pending_spawn
            pending = facts.state.pending_spawn
            if pending is None:
                raise RuntimeError("rule-1-live-step fired without a pending_spawn")
            outcome = _rule_1_action(locked, pending)
        else:
            # All other rules: fail-closed "not-yet-built" outcome
            outcome = "not-yet-built"

        # Log and print the decision
        locked.log("resume-rule-fired", step=facts.state.step, attempt=facts.state.attempts.get(facts.state.step, 0), rule=rule.name, outcome=outcome, layer=rule.layer)
        stdout_line = f"RESUME rule={rule.name} outcome={outcome}"
        print(stdout_line)

        return ResumeDecision(rule=rule.name, outcome=outcome, layer=rule.layer)

    # Exhaustion: every predicate returned False. The table is meant to be fail-closed, so this is a defect,
    # reported as a defined refusal rather than a traceback.
    raise CascadeExhausted("no resume rule fired; the cascade is incomplete")
