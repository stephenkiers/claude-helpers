"""
The audit record of a flow: journal, step records, verbatim human input, decision records, check
output paths, and the stop screen. Everything here is written by the driver, never by a model.
"""

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts.flow3.state import FlowDir
from scripts.flow3.types import STAGES, Answer, StopInfo
from scripts.workflow.fsutil import atomic_write_text, read_json_object


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%S%fZ")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "x"


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


# -- journal ---------------------------------------------------------------------------------

def journal(flow: FlowDir, text: str) -> None:
    """Append a timestamped entry to journal.md."""
    _append(flow.path / "journal.md", f"## {_now().isoformat()}\n\n{text}\n\n")


# -- step records ----------------------------------------------------------------------------

def write_step_record(flow: FlowDir, stage: str, sections: List[Tuple[str, str]]) -> Path:
    """steps/<nn>-<stage>.md. A second call for the same stage appends its sections."""
    position = STAGES.index(stage) + 1 if stage in STAGES else len(STAGES) + 1
    path = flow.path / "steps" / f"{position:02d}-{stage}.md"
    body = "".join(f"## {heading}\n\n{text}\n\n" for heading, text in sections)
    if path.exists():
        _append(path, body)
    else:
        _append(path, f"# {stage}\n\n{body}")
    return path


# -- human input -----------------------------------------------------------------------------

def save_human(flow: FlowDir, text: str, kind: str = "reply") -> str:
    """Write the human's text verbatim to human/<ts>-<kind>.md. Returns the path relative to the flow."""
    rel = f"human/{_stamp()}-{_safe(kind)}.md"
    path = flow.path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return rel


def save_interpretation(flow: FlowDir, human_rel: str, interpretation: Dict[str, Any]) -> None:
    """Write the interpretation beside the verbatim reply it came from."""
    src = flow.path / human_rel
    target = src.with_name(src.stem + ".interpretation.json")
    atomic_write_text(target, json.dumps(interpretation, indent=2, sort_keys=True) + "\n")


# -- decisions -------------------------------------------------------------------------------

def _decision_path(flow: FlowDir, question_id: str) -> Path:
    return flow.path / "decisions" / f"{_safe(question_id)}.json"


def write_decision(flow: FlowDir, card: Dict[str, Any], answer: Answer, stage: str) -> str:
    """decisions/<question id>.json. Returns the path relative to the flow."""
    path = _decision_path(flow, answer.question_id)
    doc: Dict[str, Any] = {
        "question_id": answer.question_id,
        "card": card,
        "answer": dataclasses.asdict(answer),
        "stage": stage,
        "ts": _now().isoformat(),
        "downstream": "",
    }
    atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return f"decisions/{path.name}"


def note_downstream(flow: FlowDir, question_id: str, text: str) -> None:
    """Record what a decision changed. Appends when a note already exists."""
    path = _decision_path(flow, question_id)
    doc = read_json_object(path)
    if doc is None:
        raise FileNotFoundError(f"no decision recorded for {question_id}")
    existing = str(doc.get("downstream") or "")
    doc["downstream"] = f"{existing}\n{text}" if existing else text
    atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True) + "\n")


def read_decisions(flow: FlowDir) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted((flow.path / "decisions").glob("*.json")):
        doc = read_json_object(path)
        if doc is not None:
            out.append(doc)
    return out


# -- checks ----------------------------------------------------------------------------------

def check_output_path(flow: FlowDir, mode: str, tree: str) -> Path:
    """checks/<nnn>-<mode>-<tree12>.log, reserved (created empty) with the next free number."""
    checks = flow.path / "checks"
    checks.mkdir(parents=True, exist_ok=True)
    tree12 = _safe(tree[:12]) if tree else "notree"
    with flow._locked():
        highest = 0
        for child in checks.iterdir():
            match = re.match(r"^(\d+)-", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
        path = checks / f"{highest + 1:03d}-{_safe(mode)}-{tree12}.log"
        path.touch()
    return path


# -- stop ------------------------------------------------------------------------------------

def write_stop(flow: FlowDir, stop: StopInfo) -> None:
    atomic_write_text(flow.path / "stop.json", json.dumps(dataclasses.asdict(stop), indent=2, sort_keys=True) + "\n")


def read_stop(flow: FlowDir) -> Optional[Dict[str, Any]]:
    return read_json_object(flow.path / "stop.json")


def clear_stop(flow: FlowDir) -> None:
    try:
        (flow.path / "stop.json").unlink()
    except FileNotFoundError:
        pass


def stop_screen(flow: FlowDir) -> str:
    """One screen for a stop: class, what failed, where, evidence, what was tried, how to resume."""
    stop = read_stop(flow)
    if stop is None:
        return f"{flow.name}: no stop recorded."
    tried = [str(t) for t in stop.get("tried") or []]
    tried_text = "\n".join(f"  - {t}" for t in tried) if tried else "  nothing"
    evidence = stop.get("evidence") or "none recorded"
    return (
        f"STOPPED  {flow.name}\n"
        f"\n"
        f"Class:     {stop.get('cls', '')}\n"
        f"What failed: {stop.get('reason', '')}\n"
        f"Where:     stage {stop.get('stage', '')}\n"
        f"Evidence:  {evidence}\n"
        f"Already tried:\n{tried_text}\n"
        f"\n"
        f"Resume:    {stop.get('resume_command', '')}\n"
    )


# -- why -------------------------------------------------------------------------------------

def _explain(doc: Dict[str, Any]) -> str:
    card = doc.get("card") or {}
    answer = doc.get("answer") or {}
    options = card.get("options") or []
    chosen = str(answer.get("option_id") or "")
    lines = [f"Decision {doc.get('question_id', '')} (stage {doc.get('stage', '')}, {doc.get('ts', '')})", ""]
    lines.append(f"Question: {card.get('decision') or card.get('question') or ''}")
    lines.append("Options:")
    for opt in options:
        if isinstance(opt, dict):
            mark = " (chosen)" if chosen and opt.get("id") == chosen else ""
            lines.append(f"  - {opt.get('id', '')}: {opt.get('label', '')}{mark}")
    lines.append(f"Chosen: {chosen or '(free text)'}: {answer.get('text', '')}")
    source = str(answer.get("source", ""))
    if source == "principle":
        source += f" ({answer.get('principle_id', '')})"
    lines.append(f"Decided by: {source}")
    lines.append(f"Reason: {answer.get('reason', '')}")
    if answer.get("verbatim_path"):
        lines.append(f"Human's words: {answer['verbatim_path']}")
    lines.append(f"Downstream: {doc.get('downstream') or '(none recorded)'}")
    return "\n".join(lines) + "\n"


def why(flow: FlowDir, query: str) -> str:
    """Explain a decision by id, or list what in the record mentions a file path."""
    if query and "/" not in query:
        direct = _decision_path(flow, query)
        doc = read_json_object(direct)
        if doc is not None:
            return _explain(doc)
    parts: List[str] = []
    for doc in read_decisions(flow):
        haystack = str(doc.get("downstream") or "") + "\n" + json.dumps(doc.get("card") or {})
        if query and query in haystack:
            parts.append(_explain(doc))
    steps = sorted((flow.path / "steps").glob("*.md"))
    mentioned = [p for p in steps if query and query in p.read_text(encoding="utf-8", errors="replace")]
    if mentioned:
        parts.append("Step records mentioning it:\n" + "\n".join(f"  - steps/{p.name}" for p in mentioned) + "\n")
    if not parts:
        return f"No decision is recorded for {query!r} in {flow.name}.\n"
    return "\n".join(parts)
