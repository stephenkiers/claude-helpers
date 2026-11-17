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
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ledger as lg
from .state import ParkedQuestion, Step, LockedFlow, V2State
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


def park(locked: LockedFlow, flow: Path, step: Step, attempt: int, kind: str, nn: Optional[int] = None, item: Optional[str] = None) -> ParkedQuestion:
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
        RuntimeError: if state cannot be accessed or committed
    """
    state = locked.require_state()

    # If already parked for this step+attempt, re-use the NN
    if state.parked is not None and state.parked.step == step and state.parked.attempt == attempt:
        return state.parked

    # Allocate or use provided NN
    if nn is None:
        if kind == "step":
            nn = _allocate_nn(flow, step)
        else:
            raise ValueError(f"nn must be provided for kind {kind}")

    # Create and commit the parked question
    parked = ParkedQuestion(step=step, attempt=attempt, nn=nn, kind=kind, item=item)  # type: ignore
    new_state = dataclasses.replace(state, parked=parked)
    locked.commit(new_state)

    return parked


def archive_consumed(flow: Path, parked: ParkedQuestion) -> None:
    """
    When a consumed answer exists for the step+attempt and a new question is about to park,
    move the answer file to archive/<step>/a<attempt>.answer-<NN>.json before the next question parks.

    If no answer file exists, this is a no-op.
    """
    old_answer = answer_path(flow, parked)
    if not old_answer.exists():
        return

    # Create archive directory if needed
    archive_dir = flow / "archive" / parked.step
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Move the file to archive
    archived = archive_dir / f"a{parked.attempt}.answer-{parked.nn}.json"
    old_answer.rename(archived)


def render_question(flow: Path, question_path: Path) -> str:
    """
    Render a question through flow-render-questions.py subprocess.

    Returns the rendered text (summary + brief + options).
    On failure, returns an explicit marker string that starts with "(render".
    """
    try:
        # Resolve the script path using ~/.claude/scripts convention or relative to repo
        script_path = Path.home() / ".claude" / "scripts" / "flow-render-questions.py"
        if not script_path.exists():
            # Fallback to relative to repo scripts directory
            script_path = flow.parent.parent / "scripts" / "flow-render-questions.py"

        result = subprocess.run(
            ["python3", str(script_path), str(question_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"(render failed: {result.stderr.strip()})"
        return result.stdout
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

    The note (if provided) is written to a separate field in the answer file and
    carried to the ledger for Redo.

    Returns:
        (True, success_message) if the answer is accepted
        (False, refusal_message) if the answer is refused (message starts 'answer refused:')

    Raises:
        AnswerRefused: if the answer is refused (message starts 'answer refused:')
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

        # Build the answer payload
        payload = {
            "attempt": parked.attempt,
            "step": parked.step,
            "text": stripped_text,
            "answered_utc": locked.require_state().updated,
        }
        if note is not None:
            payload["note"] = note

        # Write the answer file with exclusive create
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Use O_EXCL to ensure atomicity
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f, indent=2, sort_keys=True)
                    f.write("\n")
            except Exception:
                os.close(fd)
                target.unlink(missing_ok=True)
                raise
        except FileExistsError:
            raise AnswerRefused(f"answer refused: {target} already exists (race condition)")
        except Exception as e:
            raise AnswerRefused(f"answer refused: could not write answer: {e}")

        # A Redo note reaches the ledger as a direction through gates.record_redo_direction, once, at the gate.

        # For review-step answers, call review-ruling.py if applicable
        if parked.kind == "step" and parked.step == "review":
            # The review step records its ruling through scripts/review-ruling.py
            try:
                invoke_review_ruling(flow, parked, stripped_text, note)
            except Exception:
                # Log but don't fail - the answer was written
                pass

        return True, f"answer recorded: {target}"


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

    Raises:
        RuntimeError: if the script invocation fails (non-zero exit)
    """
    from .preflight import run_command

    # Determine the review-dir and item from the flow structure
    # For a review step question, the item number is the NN
    item_num = parked.nn

    # Build the command: scripts/review-ruling.py decide --review-dir <dir> --item <n> (--option <label> | --custom <text>)
    script_path = flow.parent.parent / "scripts" / "review-ruling.py"
    if not script_path.exists():
        # Script not available; skip
        return

    cmd = ["python3", str(script_path), "decide", "--review-dir", str(flow), "--item", str(item_num)]

    # Determine if this is an option or custom text
    # Try to match against known options first
    if text in ("", "continue", "accept"):
        # Standard option
        cmd.extend(["--option", text])
    else:
        # Custom text
        cmd.extend(["--custom", text])

    # Add note if provided
    if note:
        cmd.extend(["--note", note])

    result = run_command(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"review-ruling.py failed: {result.stderr.strip().split(chr(10))[0]}")
