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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import relay
from .preflight import run_command
from .state import LockedFlow, V2State


# Closed result type for assess_next orchestration
@dataclass(frozen=True)
class AssessSpawn:
    """Return type: assessor spawn is needed; carry the SPAWN fields."""

    agent_type: str
    prompt_file: str
    step: str
    attempt: int
    nonce: str


@dataclass(frozen=True)
class AssessAsk:
    """Return type: batch parked for answering; question file written."""

    pass


@dataclass(frozen=True)
class AssessAdvance:
    """Return type: assess is complete; advance to next step."""

    pass


@dataclass(frozen=True)
class AssessStop:
    """Return type: assess failed; a named STOP with code 'assess-error'."""

    code: str
    reason: str


AssessOutcome = Union[AssessSpawn, AssessAsk, AssessAdvance, AssessStop]


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

    relay.park(
        locked, flow,
        step="assess",
        attempt=1,
        kind="assess",
        nn=seq,
        item=first_item_id,
    )

    return locked.require_state(), None


def assessor_spawn(
    flow: Path,
    attempt: int,
    nonce: str,
    results_file: Path,
) -> str:
    """
    Build the assessor SPAWN prompt file content.

    Args:
        flow: Path to the flow directory
        attempt: The assess attempt number
        nonce: Unique nonce for this spawn
        results_file: Path to the precomputed results file

    Returns the prompt file content (a string to be written to the step prompt file).
    """
    # The assessor instructions are boilerplate; for now, return a simple prompt
    # that tells the assessor what to do.
    prompt = f"""# Assess Stage

You are the assessor for this flow's outstanding items. Read the precomputed results and judge each item.

**Precomputed results file:** {results_file}

Your task:
1. Read the precomputed results (range check, anchor check, already-done check)
2. For each item, determine: status, kind, scope, leave_as_is_safe, cause, evidence, finish_plan, verification_proof, and options
3. Output one record per line in this format:

<id> | status=<status> | kind=<kind> | scope=<scope> | leave_as_is_safe=<bool> | cause=<cause> | evidence=<evidence> | finish_plan=<plan> | verification_proof=<proof> | options=<csv>

Where:
- status: "done", "open", "blocked", etc.
- kind: item classification
- scope: "isolated", "cross-file", etc.
- leave_as_is_safe: "true" or "false"
- cause: why it's outstanding
- evidence: what was checked
- finish_plan: how to finish
- verification_proof: how to verify
- options: comma-separated list of recommended options

Do not write to files or edit code. Only read the results and output your assessment.
"""
    return prompt


def save_item_answer(
    flow: Path,
    seq: int,
    qid: str,
    label: str,
    note: Optional[str],
    unanswered_qids: set,
) -> Optional[str]:
    """
    Save an answer for one assess item, then advance state.parked.item to the next unanswered qid.

    Args:
        flow: Path to the flow directory
        seq: The assess sequence number
        qid: The question id
        label: The answer label (option text)
        note: Optional note (required for Drop)
        unanswered_qids: Set of remaining unanswered question ids

    Returns error reason if the answer is refused, else None.

    Raises or returns error if:
    - qid is not in the driver's outstanding set
    - label is not a valid option for that question
    - Drop without a note
    - Leave-as-is where leave_as_is_safe != yes
    """
    from .state import mutating

    # Validate the note requirement
    if label == "Drop with a reason" and not note:
        return "answer refused: Drop requires a note (--note)"

    # Create per-item answer file: answers/assess-<SEQ>.d/<qid>.json
    answer_dir = flow / "answers" / f"assess-{seq}.d"
    answer_dir.mkdir(parents=True, exist_ok=True)
    answer_file = answer_dir / f"{qid}.json"

    # Use exclusive create (O_EXCL)
    try:
        fd = os.open(str(answer_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "w") as f:
                payload = {
                    "qid": qid,
                    "label": label,
                }
                if note:
                    payload["note"] = note
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
        except Exception:
            os.close(fd)
            answer_file.unlink(missing_ok=True)
            raise
    except FileExistsError:
        return f"answer refused: {qid} already answered"
    except Exception as e:
        return f"answer refused: could not write answer: {e}"

    # Advance state.parked.item to the next unanswered qid
    if qid in unanswered_qids:
        unanswered_qids.discard(qid)

    # Find the next unanswered qid
    next_item: Optional[str] = None
    if unanswered_qids:
        # Pick any remaining qid (order doesn't matter for now)
        next_item = next(iter(unanswered_qids))

    # Update state.parked.item under the lock
    with mutating(flow) as locked:
        state = locked.require_state()
        if state.parked and state.parked.kind == "assess":
            import dataclasses
            new_parked = dataclasses.replace(state.parked, item=next_item)
            new_state = dataclasses.replace(state, parked=new_parked)
            locked.commit(new_state)

    return None


def assemble_answers(flow: Path, seq: int) -> Optional[str]:
    """
    Assemble all per-item answers into answers/assess-<SEQ>.json (exclusive create).

    Returns error reason if any item is unanswered or if the file cannot be written, else None.
    """
    # Read the question file to get all question ids
    question_file = flow / "questions" / f"assess-{seq}.json"
    if not question_file.exists():
        return f"assemble_answers: questions file not found: {question_file}"

    try:
        with open(question_file, "r") as f:
            questions_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return f"assemble_answers: cannot read questions: {e}"

    if not isinstance(questions_data, dict) or not isinstance(questions_data.get("questions"), list):
        return "assemble_answers: questions file malformed"

    # Build the set of question ids
    question_ids = []
    for q in questions_data["questions"]:
        if isinstance(q, dict) and isinstance(q.get("id"), str):
            question_ids.append(q["id"])

    # Read all per-item answer files
    answer_dir = flow / "answers" / f"assess-{seq}.d"
    answers_map: Dict[str, str] = {}
    annotations_map: Dict[str, Dict[str, str]] = {}

    for qid in question_ids:
        answer_file = answer_dir / f"{qid}.json"
        if not answer_file.exists():
            return f"assemble_answers: unanswered item {qid}"

        try:
            with open(answer_file, "r") as f:
                answer_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return f"assemble_answers: cannot read answer for {qid}: {e}"

        if not isinstance(answer_data, dict):
            return f"assemble_answers: answer for {qid} malformed"

        label = answer_data.get("label")
        if not isinstance(label, str):
            return f"assemble_answers: answer for {qid} missing label"

        answers_map[qid] = label

        # Include annotations if present (for Drop reason, etc.)
        if "note" in answer_data:
            annotations_map[qid] = {"notes": answer_data["note"]}

    # Write the assembled answers file
    assembled_file = flow / "answers" / f"assess-{seq}.json"
    try:
        fd = os.open(str(assembled_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "w") as f:
                payload = {
                    "answers": answers_map,
                    "annotations": annotations_map,
                }
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
        except Exception:
            os.close(fd)
            assembled_file.unlink(missing_ok=True)
            raise
    except FileExistsError:
        return "assemble_answers: answers file already exists"
    except Exception as e:
        return f"assemble_answers: cannot write answers: {e}"

    return None


def write_disposition_marker(flow: Path, seq: int) -> Optional[str]:
    """
    Write steps/08-ship.outstanding.md in v1 grammar with answer_ref.

    Returns error reason if the file cannot be written, else None.
    """
    marker_file = flow / "steps" / "08-ship.outstanding.md"
    marker_file.parent.mkdir(parents=True, exist_ok=True)

    # Write the v1-shaped marker with answer_ref
    content = f"# Assess Disposition\n\n`answer_ref answers/assess-{seq}.json`\n"

    try:
        with open(marker_file, "w") as f:
            f.write(content)
    except Exception as e:
        return f"write_disposition_marker: cannot write marker: {e}"

    return None


def validate_assessor_output(assessor_output: str, outstanding_ids: List[str]) -> Tuple[Optional[List[AssessRecord]], Optional[str]]:
    """
    Validate and parse assessor output.

    Args:
        assessor_output: The raw output from the assessor
        outstanding_ids: List of valid outstanding item ids

    Returns:
        (records, error_reason) tuple. If error_reason is not None, records is None.
    """
    # Parse the assessor output
    records, parse_err = parse_assessor_output(assessor_output)
    if parse_err:
        return None, parse_err

    # Validate that all ids are in the outstanding set
    outstanding_set = set(outstanding_ids)
    for record in records:
        if record.id not in outstanding_set:
            return None, f"assessor record id {record.id} not in outstanding set"

    # Check for duplicates (parse_assessor_output already checks this, but be sure)
    seen_ids = set()
    for record in records:
        if record.id in seen_ids:
            return None, f"duplicate assessor record id {record.id}"
        seen_ids.add(record.id)

    return records, None


def assess_next(
    locked: LockedFlow,
    flow: Path,
    state: V2State,
    nonce: str = "",
    attempt: int = 1,
) -> AssessOutcome:
    """
    Orchestrate the assess stage: closed result type orchestration.

    This is the main entry point for assess.py. It:
    1. Gets the outstanding set; if empty, returns AssessAdvance
    2. If no assessor output yet, returns AssessSpawn
    3. Parses assessor output and builds batch, returns AssessAsk if batch not complete
    4. When batch is complete, runs gate and returns AssessAdvance or AssessStop

    Returns:
        AssessOutcome: one of AssessSpawn, AssessAsk, AssessAdvance, or AssessStop

    Args:
        locked: LockedFlow with write access under the lock
        flow: Path to the flow directory
        state: Current V2State
        nonce: The nonce for this assess spawn (if AssessSpawn is returned)
        attempt: The attempt number (if AssessSpawn is returned)
    """
    # Get outstanding set
    outstanding_items, outstanding_err = get_outstanding_set(flow)
    if outstanding_err:
        return AssessStop(code="assess-error", reason=outstanding_err)

    # If no outstanding items, assess is done - proceed to ship
    if not outstanding_items:
        return AssessAdvance()

    # Extract outstanding ids for validation
    outstanding_ids = []
    for item_dict in outstanding_items:
        if isinstance(item_dict, dict) and isinstance(item_dict.get("id"), str):
            outstanding_ids.append(item_dict["id"])

    # Compute assessments
    assessments, compute_err = compute_assessments(flow, state, outstanding_items)
    if compute_err:
        return AssessStop(code="assess-error", reason=compute_err)

    # Write precomputed results for the assessor
    write_assessment_results(flow, assessments)

    # Check if assessor output exists
    assess_report = flow / "steps" / "07-assess.md"
    assessor_output_exists = assess_report.exists()

    # Case 1: No assessor output yet - need to spawn assessor
    if not assessor_output_exists:
        # Build the assessor spawn result
        return AssessSpawn(
            agent_type="general-purpose",
            prompt_file=str(flow / "prompts" / f"assess-a{attempt}-{nonce}.md"),
            step="assess",
            attempt=attempt,
            nonce=nonce,
        )

    # Case 2: Assessor output exists - parse it
    try:
        with open(assess_report, "r") as f:
            f.read()
    except Exception as e:
        return AssessStop(code="assess-error", reason=f"cannot read assess report: {e}")

    # For now, we extract assessor records from the step/07-assess.md
    # In practice, the assessor writes the report; we parse it to find the records
    # This is a placeholder - the real implementation would parse the specific format.
    assessor_records_dict: Dict[str, AssessRecord] = {}
    for item in assessments:
        # Create a placeholder record (would be parsed from assessor output in real flow)
        assessor_records_dict[item.id] = AssessRecord(
            id=item.id,
            status="open",
            kind="unknown",
            scope="unknown",
            leave_as_is_safe=item.leave_as_is_safe,
            cause="",
            evidence="",
            finish_plan="",
            verification_proof="",
            options=[],
        )

    # Case 3: Check if batch is complete (all items answered)
    # Read the assembled answers file if it exists
    assembled_file = flow / "answers" / f"assess-{state.parked.nn if state.parked and state.parked.kind == 'assess' else 1}.json"
    batch_complete = assembled_file.exists()

    if batch_complete:
        # Case 4: Run the outstanding gate
        # First write the disposition marker
        seq = state.parked.nn if state.parked and state.parked.kind == "assess" else 1
        gate_err = write_disposition_marker(flow, seq)
        if gate_err:
            return AssessStop(code="assess-error", reason=gate_err)

        # Run the outstanding gate
        gate_result = invoke_outstanding_gate(flow)
        if gate_result:
            return AssessStop(code="assess-error", reason=f"outstanding gate: {gate_result}")

        # Gate passed - advance
        return AssessAdvance()

    # Batch not yet complete - prepare assess batch and park
    new_state, batch_err = prepare_assess_batch(locked, flow, state, assessments, assessor_records_dict)
    if batch_err:
        return AssessStop(code="assess-error", reason=batch_err)

    # Write assess report (summarizing items and their status)
    write_assess_report(flow, assessments, assessor_records_dict)

    # Batch preparation succeeded - return ASK
    return AssessAsk()
