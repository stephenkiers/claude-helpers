"""
Shared types for Flow v3. Cross-module types live here so that modules built separately agree.

Model outputs stay plain dicts (validated against scripts/flow3/schemas.py). The dataclasses here are
the driver's own vocabulary: calls, checks, failures, stops, stage results, and the state machine.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

# The stages a flow moves through, in order. "merge_gate" is the human gate; "done" is terminal.
STAGES: Tuple[str, ...] = (
    "worktree", "plan", "spec_tests", "implement", "review", "fix", "ship", "merge_gate", "merge", "done",
)

# Failure classes: the rows of the table in PLAN.md section 5.
FAILURE_CLASSES: Tuple[str, ...] = (
    "limit", "transient", "schema", "environment", "base-stale", "check-flaky", "check-work",
    "unit-shortfall", "denied", "cleanup", "protected",
)

# Stop classes: what stop.json may carry. A failure class maps to the stop class in STOP_FOR.
STOP_CLASSES: Tuple[str, ...] = (
    "limit", "runner", "environment", "conflict", "check", "implement", "permission", "protected",
    "base-red", "budget", "merge-contended", "merge", "dependency", "config", "human",
)

# What a failure class becomes once its heal budget is spent. "check-flaky" and "cleanup" never stop.
STOP_FOR: Dict[str, str] = {
    "limit": "limit",
    "transient": "runner",
    "schema": "runner",
    "environment": "environment",
    "base-stale": "conflict",
    "check-work": "check",
    "unit-shortfall": "implement",
    "denied": "permission",
    "protected": "protected",
}

# Question kinds (PLAN.md section 6).
QUESTION_KINDS: Tuple[str, ...] = ("purpose", "architecture", "behaviour", "risk", "detail", "fact")
# Kinds that always go to the human unless a principle may decide them (see questions.py).
HUMAN_KINDS: Tuple[str, ...] = ("purpose", "architecture", "behaviour", "risk")
# Who answered a question.
ANSWER_SOURCES: Tuple[str, ...] = ("human", "principle", "assumed", "fact", "driver")
AnswerSource = Literal["human", "principle", "assumed", "fact", "driver"]

FLOW_STATUSES: Tuple[str, ...] = ("running", "waiting_human", "merge_gate", "stopped", "done")


class FlowStop(Exception):
    """
    A hard stop. It cannot be constructed without a known class and a non-empty reason, so a stop with
    no recorded reason cannot exist (PLAN.md section 5).
    """

    def __init__(self, cls: str, reason: str, evidence: Optional[str] = None,
                 tried: Optional[List[str]] = None) -> None:
        if cls not in STOP_CLASSES:
            raise ValueError(f"unknown stop class: {cls!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a stop requires a non-empty reason")
        super().__init__(f"{cls}: {reason}")
        self.cls = cls
        self.reason = reason.strip()
        self.evidence = evidence
        self.tried: List[str] = list(tried or [])


@dataclass(frozen=True)
class CallSpec:
    """One headless model call. `role` selects the model and limits from config unless overridden."""

    role: str
    prompt: str
    schema: Dict[str, Any]
    cwd: Path
    model: Optional[str] = None
    # Built-in tools made available (--tools). Empty means none.
    tools: Tuple[str, ...] = ()
    # Permission allowlist patterns (--allowedTools), e.g. "Edit", "Bash(git status:*)".
    allowed_tools: Tuple[str, ...] = ()
    resume_session: Optional[str] = None
    max_turns: Optional[int] = None
    timeout_secs: Optional[int] = None
    # True when the call may change files. Decides how running out of turns is classified.
    writes: bool = False
    # Free-form label recorded with the call (unit id, reviewer name, ...).
    label: str = ""


@dataclass(frozen=True)
class CallResult:
    """What one launch of `claude` produced. `ok` means a schema-valid structured output came back."""

    ok: bool
    role: str
    model: str
    output: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    exit_code: int = 0
    subtype: str = ""
    is_error: bool = False
    terminal_reason: str = ""
    api_error_status: Optional[int] = None
    num_turns: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    permission_denials: Tuple[Dict[str, Any], ...] = ()
    rate_limit: Optional[Dict[str, Any]] = None
    result_text: str = ""
    stderr: str = ""
    timed_out: bool = False
    # True when stdout held a parseable event array with a result event.
    parsed: bool = False
    schema_errors: Tuple[str, ...] = ()
    call_dir: Optional[str] = None


@dataclass(frozen=True)
class CheckRun:
    """One run of a repo command by the driver. The exit code is the fact; no model is asked."""

    ok: bool
    command: str
    mode: str  # "fast" | "full" | "pre_merge" | "post_merge" | "install"
    exit_code: int
    timed_out: bool
    duration_secs: float
    tree: str  # git tree id the run was made against ("" when it could not be read)
    output_path: str  # full output on disk
    trimmed: str  # the part worth showing a fixer or a human
    reused: bool = False  # True when a recorded result for this tree was reused (which may be a failure); readers wanting a passed result should check `reused and ok`


@dataclass(frozen=True)
class Failure:
    """A classified failure (PLAN.md section 5)."""

    cls: str
    reason: str
    evidence: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cls not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure class: {self.cls!r}")
        if not self.reason.strip():
            raise ValueError("a failure requires a non-empty reason")


@dataclass(frozen=True)
class StopInfo:
    """What stop.json carries. Constructed only through make_stop, which enforces class and reason."""

    cls: str
    reason: str
    stage: str
    evidence: Optional[str]
    tried: Tuple[str, ...]
    resume_command: str


def make_stop(cls: str, reason: str, stage: str, resume_command: str,
              evidence: Optional[str] = None, tried: Optional[List[str]] = None) -> StopInfo:
    """Build a StopInfo. Raises ValueError for an unknown class or an empty reason or resume command."""
    if cls not in STOP_CLASSES:
        raise ValueError(f"unknown stop class: {cls!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a stop requires a non-empty reason")
    if not resume_command.strip():
        raise ValueError("a stop requires a resume command")
    return StopInfo(cls=cls, reason=reason.strip(), stage=stage, evidence=evidence,
                    tried=tuple(tried or ()), resume_command=resume_command)


@dataclass(frozen=True)
class StageResult:
    """
    What a stage function returns to the engine.

    kind "done": the stage finished; `info` carries what the driver needs to pick the next stage
        (review sets info["fix_needed"]: bool).
    kind "needs_human": the stage wrote an open question batch (questions.json) and must be re-entered
        after the answers arrive.
    A stage that cannot continue raises FlowStop instead of returning.
    """

    kind: str  # "done" | "needs_human"
    info: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("done", "needs_human"):
            raise ValueError(f"unknown stage result kind: {self.kind!r}")


# ---------------------------------------------------------------------------------------------
# The state machine's vocabulary (driver.py). Events come in, actions go out.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowState:
    """The driver's state. Stage working data lives separately in state.json under "data"."""

    stage: str = "worktree"
    status: str = "running"
    # Review rounds completed (0 before the first review finishes).
    review_round: int = 0
    # Times the human sent the flow back from the merge gate.
    sendbacks: int = 0
    stop: Optional[StopInfo] = None


@dataclass(frozen=True)
class Event:
    """
    kind is one of:
      "start"                                   begin a new flow
      "stage_done"     stage, info              a stage returned done
      "stage_needs_human" stage                 a stage wrote a question batch
      "stage_failed"   stage, stop_cls, reason, evidence, tried
      "answered"                                the open batch has been answered
      "merge_decision" decision ("merge" | "send_back" | "stop"), notes
      "resume"                                  the human (or fleet) re-entered the flow
    """

    kind: str
    stage: str = ""
    info: Dict[str, Any] = field(default_factory=dict)
    stop_cls: str = ""
    reason: str = ""
    evidence: Optional[str] = None
    tried: Tuple[str, ...] = ()
    decision: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Action:
    """
    kind is one of:
      "run_stage"  stage         run that stage's function
      "park"                     wait for the human to answer the open batch; the process exits
      "merge_gate"               show the merge gate; the process exits
      "stop"       stop          write stop.json; the process exits
      "finish"                   the flow is merged and done
    """

    kind: str
    stage: str = ""
    stop: Optional[StopInfo] = None

    def __post_init__(self) -> None:
        if self.kind not in ("run_stage", "park", "merge_gate", "stop", "finish"):
            raise ValueError(f"unknown action kind: {self.kind!r}")
        if self.kind == "stop" and self.stop is None:
            raise ValueError("a stop action requires a StopInfo")
        if self.kind == "run_stage" and self.stage not in STAGES:
            raise ValueError(f"run_stage needs a known stage, got {self.stage!r}")


# ---------------------------------------------------------------------------------------------
# Questions and decisions.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """How one question was resolved."""

    question_id: str
    option_id: str  # the chosen option's id, or "" when the answer is free text only
    text: str  # the answer in words (the option label, or what the human said)
    source: AnswerSource  # who answered: human, principle, assumed, fact, or driver
    principle_id: str = ""  # set when source == "principle"
    reason: str = ""  # why: the principle's statement, the fact found, the human's words (quoted)
    verbatim_path: str = ""  # human/<timestamp>.md when source == "human"

    def __post_init__(self) -> None:
        if self.source not in ANSWER_SOURCES:
            raise ValueError(f"unknown answer source: {self.source!r}")
