"""
Fidelity audit spawn and verdict parsing for the v2 /expert-flow driver (Layer 3, Step 7; ADR-0026).

audit.py orchestrates the fidelity-audit spawn after OK receipts for plan, implement, review or fix.
It parses the audit verdict (closed type), handles rejections with Accept eligibility checking,
and manages the Retry path.

Python 3.8 floor: typing.Literal, Optional, Union only.
"""

import dataclasses
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union

from .state import LockedFlow, ParkedQuestion, Step, V2State


# run_recover executes Accept and continue; the rejection question offers it when flow-accept.py's allowlist classifies the reason.
ACCEPT_WIRED = True


# Closed result type for audit verdict
@dataclass(frozen=True)
class AuditPass:
    """Verdict: audit passed; advance the step."""

    pass


@dataclass(frozen=True)
class AuditReject:
    """Verdict: audit rejected; park the rejection question."""

    reason: str


@dataclass(frozen=True)
class AuditError:
    """Verdict: audit error (malformed verdict, missing receipt, crash)."""

    reason: str


AuditVerdict = Union[AuditPass, AuditReject, AuditError]


def audit_spawn(
    flow: Path,
    step: Step,
    attempt: int,
    nonce: str,
    receipt_path: Path,
    ledger_snapshot_path: Optional[Path] = None,
    verdict_path: Optional[Path] = None,
) -> str:
    """
    Build the audit SPAWN prompt file content.

    Carries the existing v1 fidelity-audit prompt and points at the step receipt and ledger snapshot.

    Args:
        flow: Path to the flow directory
        step: The step being audited (plan, implement, review, fix)
        attempt: The attempt number
        nonce: Unique nonce for this spawn
        receipt_path: Path to the step receipt (steps/NN-<step>.md or similar)
        ledger_snapshot_path: Path to the ledger snapshot (optional)
        verdict_path: Where the auditor writes its one verdict line (the only file it may write)

    Returns:
        The prompt file content as a string.
    """
    prompt = f"""# Fidelity Audit

You are running the fidelity audit on this {step} step attempt.

**Step Receipt:** {receipt_path}
"""
    if ledger_snapshot_path:
        prompt += f"**Ledger Snapshot:** {ledger_snapshot_path}\n"

    prompt += """
Your task is to audit the receipt for completeness and correctness. Check:
1. The receipt has the expected structure and fields
2. The findings are justified by the code and reasoning
3. No critical findings were missed or suppressed
4. The delivery is commensurate with the stated effort level

Write exactly one line as the whole content of the verdict file:
- `pass` if the audit passes
- `reject: <reason>` if the audit fails (begin reason with a classification like "reduced run (audit):" or "insufficient evidence (audit):" etc.)
- Any other output is treated as an error

Do not edit code. Write only the verdict file named below; read everything else.
"""
    if verdict_path is not None:
        prompt += f"\n**Verdict file:** {verdict_path}\n"
    return prompt


def parse_audit_verdict(output: str) -> AuditVerdict:
    """
    Parse the audit verdict from subprocess output.

    Expects exactly one line:
    - "pass" => AuditPass
    - "reject: <reason>" => AuditReject(reason)
    - Anything else => AuditError

    Returns:
        AuditVerdict: one of AuditPass, AuditReject, or AuditError
    """
    lines = output.strip().split("\n")
    if not lines:
        return AuditError(reason="audit output is empty")

    first_line = lines[0].strip()
    if first_line == "pass":
        return AuditPass()

    if first_line.startswith("reject:"):
        reason = first_line[7:].strip()
        if not reason:
            return AuditError(reason="reject verdict has no reason")
        return AuditReject(reason=reason)

    return AuditError(reason=f"malformed audit verdict: {first_line}")


def audit_receipt_path(flow: Path, step: Step, attempt: int) -> Path:
    """
    Where the audit worker's receipt lives: steps/audit-<step>-a<attempt>.md. Distinct from
    receipts.receipt_path(flow, step) (steps/NN-<step>.md), so an audit never overwrites the step receipt it audits.
    """
    return flow / "steps" / f"audit-{step}-a{attempt}.md"


def import_flow_accept() -> Tuple[Optional[object], Optional[object]]:
    """
    Import scripts/flow-accept.py's ALLOWLIST and classify function via importlib.

    Returns:
        (ALLOWLIST, classify) tuple, or (None, None) if import fails
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "flow_accept", str(Path(__file__).resolve().parent.parent / "flow-accept.py")
        )
        if spec is None or spec.loader is None:
            return None, None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        allowlist = getattr(module, "ALLOWLIST", None)
        classify = getattr(module, "classify", None)

        return allowlist, classify
    except Exception:
        return None, None


def lifecycle_check_audit_eligible(state: V2State, step: Step) -> bool:
    """
    Check if Accept is eligible for an audit rejection.

    Requires exactly these conditions:
    - status == "failed"
    - parsed stop code == "audit-rejected"
    - stop_source[step] in ("audit", "validation")
    - parked is set with kind == "audit" and parked.step == step
    - state.json is readable and all parts present

    Returns:
        True if Accept is eligible, False otherwise (fail closed)
    """
    # Check status
    if state.status != "failed":
        return False

    # Check stop_reason and parse it
    if step not in state.stop_reason:
        return False

    stop_text = state.stop_reason[step]
    # Parse stop_reason format: "code: detail"
    code_part, _, _ = stop_text.partition(": ")
    if code_part != "audit-rejected":
        return False

    # Check stop_source
    if step not in state.stop_source:
        return False
    source = state.stop_source[step]
    if source not in ("audit", "validation"):
        return False

    # Check parked
    if state.parked is None:
        return False
    if state.parked.kind != "audit":
        return False
    if state.parked.step != step:
        return False

    return True


def handle_audit_rejection(
    locked: LockedFlow,
    flow: Path,
    state: V2State,
    step: Step,
    attempt: int,
    reason: str,
    retry_callback: Callable[[LockedFlow, V2State, Step, int], V2State],
) -> Tuple[V2State, Optional[str]]:
    """
    Handle an audit rejection by writing the rejection state and parking the question.

    One committed transition under the lock:
    - status = "failed"
    - stop_reason[step] = "audit-rejected: <reason>"
    - stop_source[step] = "audit"
    - pending_spawn left at the step's receipt-landed record
    - parked = ParkedQuestion(step, attempt, nn, kind="audit", item=None)

    Then relay writes the question file and returns the updated state.

    Args:
        locked: LockedFlow with write access under the lock
        flow: Path to the flow directory
        state: Current V2State
        step: The step being audited
        attempt: The attempt number
        reason: The audit rejection reason
        retry_callback: Callback for Retry answer (unused here, wired by caller)

    Returns:
        (new_state, error) tuple
    """

    # Write the rejection state under the lock
    new_stop_reason = dict(state.stop_reason)
    new_stop_reason[step] = f"audit-rejected: {reason}"

    new_stop_source = dict(state.stop_source)
    new_stop_source[step] = "audit"

    # Park the audit rejection question
    # For audit, nn is a sequence number; use 1 for the first audit question in an attempt
    parked = ParkedQuestion(step=step, attempt=attempt, nn=1, kind="audit", item=None)

    new_state = dataclasses.replace(
        state,
        status="failed",
        stop_reason=new_stop_reason,
        stop_source=new_stop_source,
        parked=parked,
        audit_gate=None,
    )

    locked.commit(new_state)

    return new_state, None


def build_audit_rejection_question(reason: str, allowlist: Optional[object], classify: Optional[object]) -> Dict[str, object]:
    """
    Build the audit rejection question with Retry and optional Accept and continue.

    Args:
        reason: The audit rejection reason
        allowlist: The flow-accept.py ALLOWLIST (or None if import failed)
        classify: The flow-accept.py classify function (or None if import failed)

    Returns:
        A question dict with the rejection details and options.
    """
    from typing import List as ListType

    # Determine which options are available
    options: ListType[str] = ["Retry"]

    # Try to match the allowlist
    accept_eligible = False
    if allowlist is not None and classify is not None:
        try:
            # Call classify("audit", reason) to check if it matches the allowlist
            # flow-accept.py's classify(stop_source: str, reason: str) -> Optional[str]
            # Returns the matched allowlist entry or None
            if callable(classify):
                classification = classify("audit", reason)
                accept_eligible = classification is not None
        except Exception:
            accept_eligible = False

    if accept_eligible:
        options.append("Accept and continue")

    question: Dict[str, object] = {
        "summary": "Fidelity audit rejection",
        "brief": f"The audit rejected the step: {reason}",
        "options": options,
    }

    return question


def audit_orchestrate(
    locked: LockedFlow,
    flow: Path,
    state: V2State,
    step: Step,
    attempt: int,
    nonce: str,
    receipt_path: Path,
    ledger_snapshot_path: Optional[Path] = None,
    retry_callback: Optional[Callable[[LockedFlow, V2State, Step, int], V2State]] = None,
) -> Tuple[Optional[str], Optional[ParkedQuestion]]:
    """
    Orchestrate the audit: check verdict, handle rejection, return error or parked question.

    This is called after the audit receipt lands. It parses the verdict and:
    - On AuditPass: returns (None, None) to advance
    - On AuditReject: writes rejection state, parks question, returns (None, parked)
    - On AuditError: returns (error_reason, None)

    Args:
        locked: LockedFlow with write access under the lock
        flow: Path to the flow directory
        state: Current V2State
        step: The step being audited
        attempt: The attempt number
        nonce: Nonce for this audit spawn
        receipt_path: Path to the audit receipt
        ledger_snapshot_path: Optional path to ledger snapshot
        retry_callback: Optional callback for Retry (unused here, wired by caller)

    Returns:
        (error_reason, parked_question) tuple:
        - (None, None): audit passed, advance
        - (None, parked): audit rejected, parked question returned
        - (error, None): audit error (malformed verdict, missing receipt, etc.)
    """
    # Read the audit receipt
    try:
        with open(receipt_path, "r") as f:
            audit_output = f.read()
    except Exception as e:
        return f"audit-error: cannot read audit receipt: {e}", None

    # Parse the verdict
    verdict = parse_audit_verdict(audit_output)

    if isinstance(verdict, AuditPass):
        return None, None

    if isinstance(verdict, AuditError):
        return f"audit-error: {verdict.reason}", None

    if isinstance(verdict, AuditReject):
        # Import flow-accept for eligibility checking
        # Accept and continue is offered only when flow-accept.py classifies the reason; run_recover executes it.
        allowlist, classify = (import_flow_accept() if ACCEPT_WIRED else (None, None))

        # Handle rejection: write state and park question
        new_state, write_err = handle_audit_rejection(
            locked, flow, state, step, attempt, verdict.reason, retry_callback or (lambda *args: state)
        )
        if write_err:
            return f"audit-error: {write_err}", None

        # Build and write the question file
        question_data = build_audit_rejection_question(verdict.reason, allowlist, classify)
        question_file = flow / "questions" / f"{step}-a{attempt}-audit.json"
        question_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            import json

            with open(question_file, "w") as f:
                json.dump(question_data, f, indent=2)
        except Exception as e:
            return f"audit-error: cannot write question file: {e}", None

        # Return the parked question for relay
        parked = ParkedQuestion(step=step, attempt=attempt, nn=1, kind="audit", item=None)
        return None, parked

    return "audit-error: unexpected verdict type", None
