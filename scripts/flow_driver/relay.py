"""
Question and answer file management for the v2 driver (Layer 3, Step 4; ADR-0026).

relay.py owns the question and answer paths. It handles:
- Mapping parked questions to their files, keeping step questions in v1 shape (questions/<STEP>-NN.json)
- Writing parked questions under the lock before the question file
- Exact-match answer validation from closed option sets
- Question rendering through subprocess, with no untrusted text in directives
- Review ruling through subprocess
- Repeat-question archival before parking the next question

Python 3.8 floor: typing.Literal, Optional, Tuple, Dict only.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .state import ParkedKind, ParkedQuestion, Step, LockedFlow
import dataclasses


class AnswerRefused(Exception):
    """An answer was refused; the message starts with 'answer refused:'."""


def question_path(flow: Path, parked: ParkedQuestion) -> Path:
    """
    The path to the question file for a parked question.

    Step questions use v1 shape: questions/<STEP>-NN.json
    Driver questions (gate, merge-gate, audit): questions/<step>-a<attempt>-<kind>.json
    Assess batch: questions/assess-<SEQ>.json
    """
    if parked.kind == "step":
        return flow / "questions" / f"{parked.step}-{parked.nn:02d}.json"
    elif parked.kind == "assess":
        return flow / "questions" / f"assess-{parked.nn}.json"
    else:  # gate, merge-gate, audit
        return flow / "questions" / f"{parked.step}-a{parked.attempt}-{parked.kind}.json"


def answer_path(flow: Path, parked: ParkedQuestion) -> Path:
    """
    The path to the answer file for a parked question.

    Step questions: answers/<step>-a<attempt>.json
    Driver questions: answers/<step>-a<attempt>-<kind>.json
    Assess batch: answers/assess-<SEQ>.json
    """
    if parked.kind == "step":
        return flow / "answers" / f"{parked.step}-a{parked.attempt}.json"
    elif parked.kind == "assess":
        return flow / "answers" / f"assess-{parked.nn}.json"
    else:  # gate, merge-gate, audit
        return flow / "answers" / f"{parked.step}-a{parked.attempt}-{parked.kind}.json"


def _load_question_json(path: Path) -> Dict[str, object]:
    """Load and parse a question file, raising on any error."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("question must be a JSON object")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"malformed question {path}: {e}")


def _get_options(question: Dict[str, object]) -> Optional[List[str]]:
    """
    Extract the options list from a question object, or return None if free-text.
    Returns None if no options or if options is "free_text".
    """
    options = question.get("options")
    if options is None or options == "free_text":
        return None
    if not isinstance(options, list):
        raise ValueError("question.options must be an array or 'free_text'")
    return [opt if isinstance(opt, str) else str(opt) for opt in options]


def _allocate_nn(flow: Path, step: Step) -> int:
    """
    Allocate the next NN for a step question by scanning existing question and archive files.
    Returns the smallest unused NN >= 1.
    """
    used = set()

    # Scan questions/ directory for <step>-NN.json files
    questions_dir = flow / "questions"
    if questions_dir.exists():
        pattern = re.compile(f"^{re.escape(step)}-([0-9]+)\\.json$")
        for file in questions_dir.iterdir():
            match = pattern.match(file.name)
            if match:
                used.add(int(match.group(1)))

    # Scan archive/<step>/ for a<attempt>.answer-NN.json files
    archive_dir = flow / "archive" / step
    if archive_dir.exists():
        pattern = re.compile(r"^a[0-9]+\.answer-([0-9]+)\.json$")
        for file in archive_dir.iterdir():
            match = pattern.match(file.name)
            if match:
                used.add(int(match.group(1)))

    # Find the smallest unused NN
    nn = 1
    while nn in used:
        nn += 1
    return nn


def park(locked: LockedFlow, flow: Path, step: Step, attempt: int, kind: ParkedKind, nn: Optional[int] = None, item: Optional[str] = None) -> ParkedQuestion:
    """
    Park a question under the lock. Allocates NN once for step questions, records state.parked,
    commits the state, and returns the ParkedQuestion.

    For step questions: allocates NN once (re-entry with state.parked already set returns same NN)
    For other kinds: uses provided nn parameter
    For assess: uses provided nn as the SEQ

    Args:
        locked: LockedFlow with write access under the lock
        flow: Path to the flow directory
        step: The step name
        attempt: The attempt number
        kind: The kind of question ("step", "gate", "merge-gate", "audit", "assess")
        nn: The NN/SEQ to use (optional, allocated if not provided and kind=="step")
        item: For assess, the item id currently being asked

    Returns:
        ParkedQuestion with the allocated or provided NN

    Raises:
        RuntimeError: if state cannot be accessed or committed; if a parked question for this step and attempt
            exists and its kind, nn (when given) or item differs from the request; or if a gate or merge-gate
            is about to park while its answer file still exists (archive the consumed answer first, see
            archive_answer)
        ValueError: if kind is not step and nn is not given
    """
    state = locked.require_state()

    # If already parked for this step+attempt, re-use the NN, but only for the same question
    if state.parked is not None and state.parked.step == step and state.parked.attempt == attempt:
        existing = state.parked
        if existing.kind != kind:
            raise RuntimeError(f"park re-entry for {step} attempt {attempt}: parked kind {existing.kind} is not {kind}")
        if nn is not None and existing.nn != nn:
            raise RuntimeError(f"park re-entry for {step} attempt {attempt}: parked nn {existing.nn} is not {nn}")
        if existing.item != item:
            raise RuntimeError(f"park re-entry for {step} attempt {attempt}: parked item {existing.item} is not {item}")
        return existing

    # Allocate or use provided NN
    if nn is None:
        if kind == "step":
            nn = _allocate_nn(flow, step)
        else:
            raise ValueError(f"nn must be provided for kind {kind}")

    # A gate answer left unarchived would answer the new question before anyone is asked: refuse to park over it.
    if kind in ("gate", "merge-gate"):
        stale = answer_path(flow, ParkedQuestion(step=step, attempt=attempt, nn=nn, kind=kind, item=item))
        if stale.exists():
            raise RuntimeError(f"cannot park {kind} for {step} attempt {attempt}: {stale} still exists; archive it first")

    # Create and commit the parked question
    parked = ParkedQuestion(step=step, attempt=attempt, nn=nn, kind=kind, item=item)
    new_state = dataclasses.replace(state, parked=parked)
    locked.commit(new_state)

    return parked


def archive_answer(flow: Path, parked: ParkedQuestion) -> Optional[Path]:
    """
    Move the consumed answer file for parked into archive/<step>/a<attempt>.answer-<k>.json and return its new path.

    k starts at parked.nn and is the first number whose file does not exist yet, so repeated archives of the same
    attempt never overwrite each other (gate answers all carry nn 1). Returns None, and changes nothing, when there
    is no answer file. Relay owns archival of consumed answers; resume paths call this rather than moving files.
    """
    old_answer = answer_path(flow, parked)
    if not old_answer.exists():
        return None

    archive_dir = flow / "archive" / parked.step
    archive_dir.mkdir(parents=True, exist_ok=True)

    nn = parked.nn
    archived = archive_dir / f"a{parked.attempt}.answer-{nn}.json"
    while archived.exists():
        nn += 1
        archived = archive_dir / f"a{parked.attempt}.answer-{nn}.json"
    old_answer.rename(archived)
    return archived


def archive_consumed(flow: Path, parked: ParkedQuestion) -> None:
    """
    When a consumed answer exists for the step+attempt and a new question is about to park,
    move the answer file to archive/<step>/a<attempt>.answer-<NN>.json before the next question parks.

    If no answer file exists, this is a no-op. A name already taken is never overwritten (see archive_answer).
    """
    archive_answer(flow, parked)


def write_exclusive(path: Path, text: str) -> None:
    """
    Create path with O_EXCL and write text to it.

    Raises FileExistsError, and touches nothing, when path already exists. Any failure after the create unlinks the
    partial file and re-raises. The fd is closed exactly once, by the file object that owns it.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def render_question(flow: Path, question_path: Path) -> str:
    """
    Render a question through flow-render-questions.py, which writes to a file (--out is required) that is read back.

    Returns the rendered text (summary + brief + options).
    On failure, returns an explicit marker string that starts with "(render".
    """
    try:
        script_path = Path.home() / ".claude" / "scripts" / "flow-render-questions.py"
        if not script_path.exists():
            script_path = Path(__file__).resolve().parent.parent / "flow-render-questions.py"

        out_path = flow / "rendered" / f"{question_path.stem}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["python3", str(script_path), str(question_path), "--out", str(out_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"(render failed: {result.stderr.strip()})"
        return out_path.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired:
        return "(render timeout)"
    except Exception as e:
        return f"(render error: {e})"


def accept_answer(flow: Path, text: str, note: Optional[str]) -> Tuple[bool, str]:
    """
    Accept or refuse an answer to a parked question.

    Runs under the flow lock via mutating. Refuses with a message starting 'answer refused:' when:
    - Nothing is parked (no state.parked and no parked-awaiting-answer pending)
    - The answer file already exists (O_EXCL exclusive create)
    - For non-free-text questions, text.strip() is not exactly one of the option labels

    For a review-step question the ruling is recorded through review-ruling.py before the answer file is written.
    If that ruling fails, the answer is refused and no answer file is written, so the human can answer again.

    The note (if provided) is written to a separate field in the answer file and
    carried to the ledger for Redo.

    Returns:
        (True, success_message) when the answer is accepted. This function never returns a failure value.

    Raises:
        AnswerRefused: on every refusal; the message starts 'answer refused:'. Refusals are raised, never returned.
    """
    from .state import mutating

    with mutating(flow) as locked:
        state = locked.require_state()

        # Check if something is parked
        if state.parked is None and (state.pending_spawn is None or state.pending_spawn.state != "parked-awaiting-answer"):
            raise AnswerRefused("answer refused: nothing is parked")

        # Determine which parked question to use
        parked = state.parked
        if parked is None and state.pending_spawn is not None and state.pending_spawn.state == "parked-awaiting-answer":
            # Legacy Layer 2 path: construct ParkedQuestion from pending_spawn
            parked = ParkedQuestion(step=state.pending_spawn.step, attempt=state.pending_spawn.attempt, nn=state.pending_spawn.attempt, kind="step")  # type: ignore

        if parked is None:
            raise AnswerRefused("answer refused: cannot determine parked question")

        # Get the answer file path
        target = answer_path(flow, parked)

        # Check if answer file already exists (exclusive create with O_EXCL)
        if target.exists():
            raise AnswerRefused(f"answer refused: {target} already exists")

        # Load the question to validate the answer
        question_file = question_path(flow, parked)
        try:
            question = _load_question_json(question_file)
        except Exception as e:
            raise AnswerRefused(f"answer refused: could not load question: {e}")

        # Validate the answer against options (if any)
        options = None
        try:
            options = _get_options(question)
        except Exception as e:
            raise AnswerRefused(f"answer refused: could not parse options: {e}")

        stripped_text = text.strip()
        if options is not None and stripped_text not in options:
            raise AnswerRefused(f"answer refused: '{stripped_text}' is not a valid option")

        # For review-step answers, the ruling is recorded first: a failure refuses the answer, so nothing is consumed.
        # Re-answering after a recorded ruling is idempotent in review-ruling.py (same answer exits 0).
        if parked.kind == "step" and parked.step == "review":
            try:
                invoke_review_ruling(flow, parked, stripped_text, note)
            except (RuntimeError, ValueError, OSError) as e:
                raise AnswerRefused(f"answer refused: review ruling not recorded: {e}")

        # Build the answer payload
        payload = {
            "attempt": parked.attempt,
            "step": parked.step,
            "text": stripped_text,
            "answered_utc": _utc_now(),
        }
        if note is not None:
            payload["note"] = note

        # Write the answer file with exclusive create
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_exclusive(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except FileExistsError:
            raise AnswerRefused(f"answer refused: {target} already exists (race condition)")
        except Exception as e:
            raise AnswerRefused(f"answer refused: could not write answer: {e}")

        # A Redo note reaches the ledger as a direction through gates.record_redo_direction, once, at the gate.

        return True, f"answer recorded: {target}"


def _utc_now() -> str:
    """The current time as YYYY-MM-DDTHH:MM:SSZ, the format state.json uses for its timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def invoke_review_ruling(flow: Path, parked: ParkedQuestion, text: str, note: Optional[str]) -> None:
    """
    Invoke scripts/review-ruling.py decide to record a review-step ruling.

    For review-step questions, the recorded ruling goes through scripts/review-ruling.py
    instead of a direct file write.

    Args:
        flow: Path to the flow directory
        parked: The parked review question
        text: The answer text (the option label or custom text)
        note: Optional note text

    Review dir and item come from the question file: its context_path is <review-dir>/claude-action-plan.md, and the
    item number is the leading "### N." heading of the question's brief (the item's own Needs you block). Option
    labels are passed as --option, anything else as --custom, both in the = form so a leading dash is never a flag.

    Raises:
        RuntimeError: if the script cannot be found, the review dir or item cannot be derived, or the script exits
            non-zero (including 3, a conflicting prior ruling)
        ValueError: if the question file is malformed
    """
    from .preflight import run_command

    question = _load_question_json(question_path(flow, parked))
    review_dir = _review_dir_from_question(question)
    item_num = _review_item_number(question)
    options = _get_options(question)

    # Resolve the script path the way render_question does: the installed copy, else the one in this checkout
    script_path = Path.home() / ".claude" / "scripts" / "review-ruling.py"
    if not script_path.exists():
        script_path = Path(__file__).resolve().parent.parent / "review-ruling.py"
    if not script_path.exists():
        raise RuntimeError(f"review-ruling.py not found at {script_path}")

    cmd = ["python3", str(script_path), "decide", "--review-dir", str(review_dir), "--item", str(item_num)]
    if text in ("", "continue", "accept") or (options is not None and text in options):
        cmd.append(f"--option={text}")
    else:
        cmd.append(f"--custom={text}")
    if note:
        cmd.append(f"--reasoning={note}")

    result = run_command(cmd)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        detail = lines[0] if lines else f"exit {result.returncode}"
        raise RuntimeError(f"review-ruling.py failed: {detail}")


def _review_dir_from_question(question: Dict[str, object]) -> Path:
    """The expert-review dir: the parent of the question's context_path, which must name claude-action-plan.md."""
    context = question.get("context_path")
    if not isinstance(context, str) or not context:
        raise RuntimeError("review question has no context_path, so the review dir is unknown")
    path = Path(context)
    if path.name != "claude-action-plan.md" or not path.is_absolute():
        raise RuntimeError(f"review question context_path is not an absolute claude-action-plan.md: {context}")
    return path.parent


_ITEM_HEADING = re.compile(r"^\s*#{1,6}\s+(\d+)\.", re.MULTILINE)


def _review_item_number(question: Dict[str, object]) -> int:
    """The Needs you item number: the leading '### N.' heading of the first question brief that carries one."""
    questions = question.get("questions")
    briefs: List[str] = []
    if isinstance(questions, list):
        for entry in questions:
            if isinstance(entry, dict) and isinstance(entry.get("brief"), str):
                briefs.append(str(entry["brief"]))
    if isinstance(question.get("brief"), str):
        briefs.append(str(question["brief"]))
    for brief in briefs:
        match = _ITEM_HEADING.search(brief)
        if match:
            return int(match.group(1))
    raise RuntimeError("review question brief has no '### N.' Needs you heading, so the item number is unknown")
