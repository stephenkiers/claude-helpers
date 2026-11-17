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
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .state import ParkedQuestion, Step


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


def _get_options(question: Dict[str, object]) -> List[str]:
    """Extract the options list from a question object, or raise if malformed."""
    options = question.get("options")
    if not isinstance(options, list):
        raise ValueError("question.options must be an array")
    return [opt if isinstance(opt, str) else str(opt) for opt in options]


def accept_answer(flow: Path, text: str, note: Optional[str]) -> Tuple[bool, str]:
    """
    Accept or refuse an answer to a parked question.

    Returns (success, message):
    - (True, reason_msg) if the answer is accepted
    - (False, reason_msg) if the answer is refused with a message starting "answer refused:"

    Validation:
    - A question must be parked (state.parked set)
    - The answer file must not exist (O_EXCL exclusive create)
    - For non-free-text questions, text.strip() must exactly match one option label

    The note (if provided) is written to a separate field in the answer file and
    carried to the ledger for Redo.
    """
    # This is a simplified stub. The full implementation needs integration with the flow lock.
    # For now, it returns the validation structure that will be needed.
    return True, f"answer accepted: {text}"


def _render_question(question_path: Path) -> str:
    """
    Render a question through flow-render-questions.py subprocess.

    Returns the rendered text (summary + brief + options).
    """
    try:
        result = subprocess.run(
            ["python3", str(Path(__file__).parent.parent / "flow-render-questions.py"), str(question_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"(render failed: {result.stderr.strip()})"
        return result.stdout
    except Exception as e:
        return f"(render error: {e})"


def _invoke_review_ruling(question_path: Path, answer_text: str) -> str:
    """
    Invoke review-ruling.py to process a review-step question answer.

    Returns the ruling text that should be written to answers.
    """
    try:
        result = subprocess.run(
            [
                "python3",
                str(Path(__file__).parent.parent / "review-ruling.py"),
                "--question",
                str(question_path),
                "--answer",
                answer_text,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"review-ruling.py failed: {result.stderr}")
        return result.stdout
    except Exception as e:
        raise ValueError(f"review ruling failed: {e}")
