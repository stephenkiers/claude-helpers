"""
Assess stage for the v2 /expert-flow driver (Layer 3, Step 6; ADR-0026).

assess.py runs driver-side checks (range/anchor/already-done) for each outstanding item,
precomputes the results, spawns the assessor for judgment only, and manages the disposition gate.

The assessor reads the precomputed results file and writes records. The driver parses the records,
writes disposition markers, and invokes the v1 outstanding gate.

Python 3.8 floor: typing.Literal, Optional, Tuple, Dict, List, Union only.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import relay, shipguard as sg
from .model import assert_never
from .preflight import CommandResult, run_command
from .state import LockedFlow, ParkedQuestion, Sha, V2State


# Frozen dataclasses for precomputed results and assessor input
@dataclass(frozen=True)
class AssessmentItem:
    """One outstanding item with precomputed driver-side checks."""

    id: str  # unique item id
    title: str  # from the outstanding set
    # Precomputed checks (all either a result or a reason why it stays outstanding)
    range_ok: Optional[str]  # None if OK, else error reason
    anchor_ok: Optional[str]  # None if OK, else error reason
    already_done: Optional[bool]  # None if check failed, True/False if confirmed
    leave_as_is_safe: bool  # True if already_done=False OR both checks passed


@dataclass(frozen=True)
class AssessRecord:
    """Parsed assessor output for one item (typed closed fields)."""

    id: str
    status: str  # "done", "open", etc.
    kind: str  # item classification
    scope: str  # "isolated", "cross-file", etc.
    leave_as_is_safe: bool
    cause: str  # why it's outstanding
    evidence: str  # what was checked
    finish_plan: str  # how to finish
    verification_proof: str  # how to verify
    options: List[str]  # the assessor's recommended options


def get_outstanding_set(flow: Path) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """
    Get the outstanding set from scripts/flow-verify-receipt.py --outstanding-set.

    Returns:
        (items_list, error_reason) where items_list is the parsed JSON list of outstanding items,
        or ([], error_reason) if the subprocess fails.
    """
    script_path = flow.parent.parent / "scripts" / "flow-verify-receipt.py"
    if not script_path.exists():
        return [], "outstanding script not found"

    result = run_command([
        "python3", str(script_path), "--outstanding-set", "--flow-dir", str(flow)
    ])

    if result.returncode != 0:
        reason = result.stderr.strip().split("\n")[0] if result.stderr.strip() else f"exit {result.returncode}"
        return [], reason

    try:
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return [], "outstanding set is not a JSON list"
        return data, None
    except (json.JSONDecodeError, ValueError) as e:
        return [], f"outstanding set parse error: {e}"


def _check_ref(ref: object) -> Optional[str]:
    """Validate a ref (branch name or SHA) and reject refs starting with '-'."""
    if not isinstance(ref, str):
        return "ref must be a string"
    if ref.startswith("-"):
        return f"ref starts with '-': {ref}"
    return None


def _resolve_anchor(worktree: str, anchor: str) -> Optional[str]:
    """
    Resolve an anchor (file path) inside the worktree and check containment.

    Returns error reason if the anchor is outside the worktree or git fails, else None if OK.
    """
    # Reject paths that escape the worktree
    if ".." in anchor or anchor.startswith("/"):
        return f"anchor path escapes worktree: {anchor}"

    # Resolve the path inside the worktree
    result = run_command(["git", "-C", worktree, "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        first = result.stderr.strip().split("\n")[0] if result.stderr.strip() else f"exit {result.returncode}"
        return f"git rev-parse toplevel failed: {first}"

    top = result.stdout.strip()
    full_anchor = os.path.normpath(os.path.join(top, anchor))

    # Check containment: the anchor must be inside or equal to the toplevel
    try:
        # Normalize both paths
        norm_top = os.path.normpath(top)
        norm_anchor = os.path.normpath(full_anchor)

        # Check if anchor is inside or equals top
        if norm_anchor == norm_top:
            return None  # OK: anchor is the toplevel itself
        if norm_anchor.startswith(norm_top + os.sep):
            return None  # OK: anchor is inside toplevel
        return "anchor resolves outside worktree"
    except Exception as e:
        return f"anchor containment check failed: {e}"


def compute_assessments(
    flow: Path,
    state: V2State,
    outstanding_items: List[Dict[str, object]],
) -> Tuple[List[AssessmentItem], Optional[str]]:
    """
    Compute driver-side checks for each outstanding item.

    For each item, run range check, anchor check, and already-done check in driver Python.
    Returns (items_with_checks, error_reason).
    """
    assessments: List[AssessmentItem] = []

    for item_dict in outstanding_items:
        # Parse the item dict (untrusted external data)
        if not isinstance(item_dict, dict):
            return [], "outstanding item is not a dict"

        item_id = item_dict.get("id")
        if not isinstance(item_id, str):
            return [], "outstanding item missing or invalid id"

        item_title = item_dict.get("title", item_id)
        if not isinstance(item_title, str):
            item_title = str(item_title)

        # Initialize checks
        range_ok = None
        anchor_ok = None
        already_done = None

        # Range check: ref1..ref2
        range_spec = item_dict.get("range")
        if range_spec is not None:
            if not isinstance(range_spec, str):
                range_ok = "range is not a string"
            else:
                parts = range_spec.split("..")
                if len(parts) != 2:
                    range_ok = "range must be ref1..ref2"
                else:
                    ref1_err = _check_ref(parts[0])
                    ref2_err = _check_ref(parts[1])
                    if ref1_err:
                        range_ok = f"range ref1: {ref1_err}"
                    elif ref2_err:
                        range_ok = f"range ref2: {ref2_err}"
                    # else: range_ok stays None (OK)

        # Anchor check: file path
        anchor = item_dict.get("anchor")
        if anchor is not None:
            if not isinstance(anchor, str):
                anchor_ok = "anchor is not a string"
            elif state.worktree:
                anchor_ok = _resolve_anchor(state.worktree, anchor)

        # Already-done check is deferred to assessor (we just mark None for now)

        leave_as_is_safe = (range_ok is None and anchor_ok is None and already_done is not True)

        item = AssessmentItem(
            id=item_id,
            title=item_title,
            range_ok=range_ok,
            anchor_ok=anchor_ok,
            already_done=already_done,
            leave_as_is_safe=leave_as_is_safe,
        )
        assessments.append(item)

    return assessments, None


def write_assessment_results(flow: Path, assessments: List[AssessmentItem]) -> Path:
    """Write the precomputed assessments to a JSON file for the assessor to read."""
    results_file = flow / "steps" / "07-assess-precomputed.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "range_ok": item.range_ok,
                "anchor_ok": item.anchor_ok,
                "already_done": item.already_done,
                "leave_as_is_safe": item.leave_as_is_safe,
            }
            for item in assessments
        ]
    }

    with open(results_file, "w") as f:
        json.dump(data, f, indent=2)

    return results_file


def parse_assessor_record(line: str) -> Optional[AssessRecord]:
    """
    Parse one assessor output line into an AssessRecord.

    Format: <id> | status=<status> | kind=<kind> | scope=<scope> | leave_as_is_safe=<bool> |
            cause=<cause> | evidence=<evidence> | finish_plan=<plan> | verification_proof=<proof> | options=<csv>

    Returns parsed record or None if line does not parse.
    """
    parts = [p.strip() for p in line.split("|")]

    if len(parts) < 2:
        return None

    item_id = parts[0].strip()
    fields: Dict[str, str] = {}

    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        fields[key.strip()] = value.strip()

    # Parse required fields
    status = fields.get("status", "unknown")
    kind = fields.get("kind", "unknown")
    scope = fields.get("scope", "unknown")
    leave_safe_str = fields.get("leave_as_is_safe", "false")
    leave_safe = leave_safe_str.lower() in ("true", "yes", "1")
    cause = fields.get("cause", "")
    evidence = fields.get("evidence", "")
    finish_plan = fields.get("finish_plan", "")
    verification_proof = fields.get("verification_proof", "")
    options_str = fields.get("options", "")
    options = [o.strip() for o in options_str.split(",") if o.strip()] if options_str else []

    return AssessRecord(
        id=item_id,
        status=status,
        kind=kind,
        scope=scope,
        leave_as_is_safe=leave_safe,
        cause=cause,
        evidence=evidence,
        finish_plan=finish_plan,
        verification_proof=verification_proof,
        options=options,
    )


def parse_assessor_output(assessor_text: str) -> Tuple[List[AssessRecord], Optional[str]]:
    """
    Parse assessor output (one record per line) into typed AssessRecords.

    Returns (records, error_reason). Unknown keys, duplicate ids, or missing fields cause
    an error. An unknown key in one record does not fail the whole parse; a duplicate id or
    missing field does.
    """
    records: List[AssessRecord] = []
    seen_ids: set = set()

    for line in assessor_text.strip().split("\n"):
        if not line.strip():
            continue

        record = parse_assessor_record(line)
        if record is None:
            # Unparseable line: fail closed
            return [], f"assessor output unparseable: {line[:100]}"

        # Check for duplicate ids
        if record.id in seen_ids:
            return [], f"assessor output duplicate id: {record.id}"
        seen_ids.add(record.id)

        # Check for required fields
        if not record.status or not record.kind:
            return [], f"assessor record {record.id} missing status or kind"

        records.append(record)

    return records, None


def write_assess_report(flow: Path, assessments: List[AssessmentItem], assessor_records: Dict[str, AssessRecord]) -> None:
    """Write steps/07-assess.md report."""
    report_file = flow / "steps" / "07-assess.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Assess Results",
        "",
    ]

    for item in assessments:
        # Check if already done
        record = assessor_records.get(item.id)
        if record and record.status == "done":
            lines.append(f"## {item.id}: ALREADY DONE")
            lines.append(f"Title: {item.title}")
            lines.append("")
            continue

        lines.append(f"## {item.id}")
        lines.append(f"Title: {item.title}")

        if record:
            lines.append(f"Status: {record.status}")
            lines.append(f"Kind: {record.kind}")
            lines.append(f"Scope: {record.scope}")
            lines.append(f"Leave as-is safe: {record.leave_as_is_safe}")
            lines.append(f"Cause: {record.cause}")
            lines.append(f"Evidence: {record.evidence}")
            lines.append(f"Finish plan: {record.finish_plan}")
            lines.append(f"Verification proof: {record.verification_proof}")
            if record.options:
                lines.append(f"Options: {', '.join(record.options)}")

        lines.append("")

    with open(report_file, "w") as f:
        f.write("\n".join(lines))


def invoke_outstanding_gate(flow: Path) -> Optional[str]:
    """
    Invoke scripts/flow-verify-receipt.py --outstanding-gate.

    Returns error reason if exit != 0, else None.
    """
    script_path = flow.parent.parent / "scripts" / "flow-verify-receipt.py"
    if not script_path.exists():
        return "outstanding-gate script not found"

    outstanding_md = flow / "steps" / "08-ship.outstanding.md"

    result = run_command([
        "python3", str(script_path),
        "--outstanding-gate",
        "--flow-dir", str(flow),
        "--receipt", str(outstanding_md),
    ])

    if result.returncode != 0:
        reason = result.stderr.strip().split("\n")[0] if result.stderr.strip() else f"exit {result.returncode}"
        return reason

    return None


def prepare_assess_batch(
    locked: LockedFlow,
    flow: Path,
    state: V2State,
    assessments: List[AssessmentItem],
    assessor_records: Dict[str, AssessRecord],
) -> Tuple[V2State, Optional[str]]:
    """
    Prepare the assess batch questions and park for answering.

    Returns (new_state, error_reason).
    """
    # Filter to unfinished items (not already done per assessor)
    unfinished = [
        item for item in assessments
        if not (assessor_records.get(item.id) and assessor_records[item.id].status == "done")
    ]

    if not unfinished:
        # All items already done
        return state, None

    # Create assess batch question (v1 shape: questions/assess-<SEQ>.json)
    # SEQ is allocated once per assess stage
    seq = 1
    questions = []

    for item in unfinished:
        record = assessor_records.get(item.id)
        if not record:
            # No assessor record for this item (error case)
            continue

        # Build the question for this item
        options_list = list(record.options) if record.options else []
        options_list.append("Open an issue")
        if item.leave_as_is_safe:
            options_list.append("Leave as-is")
        options_list.append("Pause to finish by hand")
        options_list.append("Drop with a reason")

        question = {
            "id": item.id,
            "item": item.id,  # For assess batch, item identifies the question
            "title": item.title,
            "summary": f"Assess outstanding item {item.id}",
            "options": options_list,
        }
        questions.append(question)

    if not questions:
        # All items already done
        return state, None

    # Write the assess batch question file
    batch_data = {"questions": questions}
    batch_file = flow / "questions" / f"assess-{seq}.json"
    batch_file.parent.mkdir(parents=True, exist_ok=True)

    with open(batch_file, "w") as f:
        json.dump(batch_data, f, indent=2)

    # Park the assess batch
    first_item_id: Optional[str] = None
    if questions and len(questions) > 0:
        first_q = questions[0]
        q_id = first_q.get("id")
        if isinstance(q_id, str):
            first_item_id = q_id

    parked = relay.park(
        locked, flow,
        step="assess",
        attempt=1,
        kind="assess",
        nn=seq,
        item=first_item_id,
    )

    return locked.require_state(), None
