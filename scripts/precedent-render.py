#!/usr/bin/env python3
r"""precedent-render.py: render a validated precedent file for display or integration.

Three subcommands validate Pat's output, extract display lines, and format markdown sections.

Usage:
    precedent-render.py validate --precedent P --questions Q --digest D [--out PATH]
        -> JSON {"status":"ok"|"invalid","reason":"<D7 reason>","blocks":{...}}
        Exit 0 even when invalid; nonzero only for usage errors.

    precedent-render.py line --validated V --questions Q [--out PATH]
        -> JSON object {"<id>": "<one display line, <=240 chars>"} for spoken ids only.

    precedent-render.py section --validated V [--out PATH]
        -> markdown "## Precedent" section for decisions.md.

Validation (Obligation 8.2):
  - File must exist; first non-blank body line after the `# Precedent` heading must
    match the receipt regex exactly: `^precedent-pat \| questions: \d+ \| spoke-on: \d+ \| citations: \d+ \| confidence: high=\d+ medium=\d+$`
  - Receipt counts must match the parsed body.
  - Every spoken block must have at least one Precedent line, Prediction, Confidence (high|medium),
    Why, and Distinguish line.
  - Total citations <= 5.
  - Each block header `## Q{n} [{id}] {header}` must have an id that exists in the questions file.
  - `Prediction` must byte-equal one of that question's option labels.
  - Every non-null cited `ref` must appear as a `ref` in the digest JSONL.
  - A curated citation (`**Precedent**: curated rule "<answer>" (<path>)`) must name the path of a
    digest entry with `source: "curated"` and the same answer; it carries no ref.

Failure modes (D7 reasons):
  - "pat-invalid": file missing, malformed, or validation failed
  - "pat-failed": file unreadable
  - "pat-silent": valid but spoke-on=0 (status still "ok")
  - "" (empty string): validation passed

Exit and stderr contract:
  - Exit 0 on every data-side outcome (invalid or not).
  - Exit 2 on invocation errors (bad args, missing --out parent dir, missing input files).
  - Exit 1 on internal errors (uncaught exceptions).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import traceback
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple, TypedDict


class _Usage(Exception):
    """An invocation error: exit 2, one stderr line, no traceback."""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports errors on one stderr line and exits 2."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        sys.stderr.write("precedent-render: %s\n" % message)
        raise SystemExit(2)


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


# Regex patterns for parsing pat's output
RECEIPT_RE = re.compile(
    r"^precedent-pat \| questions: (\d+) \| spoke-on: (\d+) \| citations: (\d+) \| confidence: high=(\d+) medium=(\d+)$"
)
Q_HEADER_RE = re.compile(r"^## Q(\d+) \[([^\]]+)\] (.*)$")
PRECEDENT_LINE_RE = re.compile(
    r"^\*\*Precedent\*\*: (.+?)(?:\s*· #(\d+))?\s*· (.+?)\s*· chose \"(.+?)\"(?:\s*\(recommended was \"(.+?)\"\))?(?:\s*· reason: \"(.+)\")?\s*$"
)
# Curated citation shape (ADR-0024 amendment, #297): a project-written rule from a digest entry
# with source "curated". The path is the absolute file path the digest recorded.
CURATED_LINE_RE = re.compile(
    r"^\*\*Precedent\*\*: curated rule \"(.+?)\" \((.+?)\)(?:\s*· spirit: \"(.+)\")?\s*$"
)
PREDICTION_RE = re.compile(r"^\*\*Prediction\*\*: (.+)$")
CONFIDENCE_RE = re.compile(r"^\*\*Confidence\*\*: (high|medium)$")
WHY_RE = re.compile(r"^\*\*Why\*\*: (.+)$")
DISTINGUISH_RE = re.compile(r"^\*\*Distinguish\*\*: (.+)$")
SILENT_RE = re.compile(r"^\(silent — nothing related\)$")


class _ValidatedBlock(TypedDict, total=False):
    """A validated Q block from the precedent file."""
    id: str
    n: int
    header: str
    silent: bool
    precedent_lines: List[Dict[str, Any]]  # date, ref, source, answer, recommended, reason
    prediction: str
    confidence: str
    why: str
    distinguish: str


def _sanitize(text: str) -> str:
    """Sanitize interpolated fields: collapse whitespace, strip markdown, strip control chars, neutralize injections.

    Strips Unicode categories Cc (control), Cf (format), Zl (line separator), Zp (paragraph separator).
    """
    # Replace line feeds/CRs/tabs with spaces
    text = re.sub(r"[\r\n\t]", " ", text)
    # Strip control characters (Cc), format chars (Cf), line/paragraph separators (Zl/Zp)
    text = "".join(c for c in text if not (c < ' ' or ord(c) in (0x7F,) or
                                           (0x200B <= ord(c) <= 0x200D) or  # Cf: zero-width chars
                                           (ord(c) in (0x2028, 0x2029))))     # Zl/Zp
    # Collapse multiple spaces
    text = re.sub(r" +", " ", text).strip()
    # Strip leading markdown chars and backticks
    text = re.sub(r"^[#>`*-]+", "", text).strip()
    # Neutralize forged Precedent/Recommended
    text = re.sub(r"Precedent:", "precedent:", text, flags=re.IGNORECASE)
    text = re.sub(r"\(Recommended\)", "(recommended)", text, flags=re.IGNORECASE)
    return text


def _read_text(path: str) -> str:
    """Read a file as UTF-8, raising OSError on failure."""
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def _is_curated(prec: Dict[str, Any]) -> bool:
    """A Precedent line is curated when it carries a path (only the curated shape parses one)."""
    return isinstance(prec.get("path"), str)


def _parse_receipt(text: str) -> Optional[Tuple[int, int, int, int, int]]:
    """Parse receipt line: return (questions, spoke-on, citations, high, medium) or None."""
    for line in text.splitlines():
        stripped = line.strip()
        match = RECEIPT_RE.match(stripped)
        if match:
            return (
                int(match.group(1)),  # questions
                int(match.group(2)),  # spoke-on
                int(match.group(3)),  # citations
                int(match.group(4)),  # high
                int(match.group(5)),  # medium
            )
    return None


def _load_questions(path: str) -> Dict[str, Any]:
    """Load questions from JSON file: {<id>: {header, question, options, recommended}...}"""
    try:
        text = _read_text(path)
        data = json.loads(text)
        if not isinstance(data, dict) or "questions" not in data:
            return {}
        questions = {}
        for item in data.get("questions", []):
            if isinstance(item, dict) and "id" in item:
                qid = item["id"]
                questions[qid] = {
                    "header": item.get("header", ""),
                    "question": item.get("question", ""),
                    "options": item.get("options", []),
                    "recommended": item.get("recommended"),
                }
        return questions
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _load_digest(path: str) -> Tuple[Dict[Optional[int], Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load digest JSONL.

    Returns (refs, curated_paths):
    - refs: {ref: entry, ...} where ref is the 'ref' field or None.
    - curated_paths: {path: entry, ...} for entries with source "curated" (ref is null for these,
      so they are indexed by their absolute path instead).
    """
    refs: Dict[Optional[int], Dict[str, Any]] = {}
    curated_paths: Dict[str, Dict[str, Any]] = {}
    try:
        text = _read_text(path)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ref = entry.get("ref")
                refs[ref] = entry
                if entry.get("source") == "curated" and isinstance(entry.get("path"), str):
                    curated_paths[entry["path"]] = entry
            except json.JSONDecodeError:
                continue
    except (OSError, UnicodeDecodeError):
        pass
    return refs, curated_paths


def _parse_blocks(text: str) -> List[_ValidatedBlock]:
    """Parse the body of the precedent file into Q blocks."""
    lines = text.splitlines()
    blocks: List[_ValidatedBlock] = []
    seen_ids: set = set()
    current: Optional[_ValidatedBlock] = None

    for line in lines:
        # Check for Q header
        match = Q_HEADER_RE.match(line)
        if match:
            block_id = match.group(2)
            current = {
                "n": int(match.group(1)),
                "id": block_id,
                "header": match.group(3),
                "silent": False,
                "precedent_lines": [],
                "prediction": "",
                "confidence": "",
                "why": "",
                "distinguish": "",
            }
            seen_ids.add(block_id)
            blocks.append(current)
            continue

        if current is None:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Check for silent marker
        if SILENT_RE.match(stripped):
            current["silent"] = True
            current = None
            continue

        # Parse curated Precedent lines (project-written rules; no ref, carry a path)
        curated_match = CURATED_LINE_RE.match(stripped)
        if curated_match:
            current["precedent_lines"].append({
                "ref": None,
                "source": "curated",
                "answer": curated_match.group(1),
                "path": curated_match.group(2),
                "recommended": None,
                "reason": curated_match.group(3),
            })
            continue

        # Parse Precedent lines
        prec_match = PRECEDENT_LINE_RE.match(stripped)
        if prec_match:
            current["precedent_lines"].append({
                "date": prec_match.group(1),
                "ref": int(prec_match.group(2)) if prec_match.group(2) else None,
                "source": prec_match.group(3),
                "answer": prec_match.group(4),
                "recommended": prec_match.group(5),
                "reason": prec_match.group(6),
            })
            continue

        # Parse Prediction
        pred_match = PREDICTION_RE.match(stripped)
        if pred_match:
            current["prediction"] = pred_match.group(1)
            continue

        # Parse Confidence
        conf_match = CONFIDENCE_RE.match(stripped)
        if conf_match:
            current["confidence"] = conf_match.group(1)
            continue

        # Parse Why
        why_match = WHY_RE.match(stripped)
        if why_match:
            current["why"] = why_match.group(1)
            continue

        # Parse Distinguish
        dist_match = DISTINGUISH_RE.match(stripped)
        if dist_match:
            current["distinguish"] = dist_match.group(1)
            continue

    return blocks


def _validate_blocks(
    blocks: List[_ValidatedBlock],
    questions: Dict[str, Any],
    digest_refs: Dict[Optional[int], Dict[str, Any]],
    curated_paths: Dict[str, Dict[str, Any]],
    receipt: Tuple[int, int, int, int, int],
) -> Tuple[bool, str, List[str]]:
    """Validate blocks against questions and digest. Return (valid, reason, valid_ids).

    valid_ids is the list of block ids that pass validation (for building result_blocks).
    Enforces: no duplicate ids, no null refs in raw citations, Distinguish present when spoken.
    A curated citation (one carrying a path) must name a digest curated entry by path, with the
    same answer; the ref rules do not apply to it.
    """
    q_count, spoke_on, citations, high, medium = receipt

    # Reject duplicate ids
    seen_ids: set = set()
    for block in blocks:
        if not block["silent"]:
            if block["id"] in seen_ids:
                return False, "pat-invalid", []
            seen_ids.add(block["id"])

    # Count spoke-on
    actual_spoke_on = sum(1 for b in blocks if not b["silent"])
    if actual_spoke_on != spoke_on:
        return False, "pat-invalid", []

    # Count citations
    actual_citations = sum(len(b["precedent_lines"]) for b in blocks if not b["silent"])
    if actual_citations != citations:
        return False, "pat-invalid", []

    # Count high/medium
    actual_high = sum(1 for b in blocks if not b["silent"] and b["confidence"] == "high")
    actual_medium = sum(1 for b in blocks if not b["silent"] and b["confidence"] == "medium")
    if actual_high != high or actual_medium != medium:
        return False, "pat-invalid", []

    # Citations total must be <= 5
    if citations > 5:
        return False, "pat-invalid", []

    # Check per-block citation limit: each question must have <= 3 citations
    for block in blocks:
        if not block["silent"]:
            if len(block["precedent_lines"]) > 3:
                return False, "pat-invalid", []

    # Validate each spoken block, tracking which ones are valid
    valid_ids: List[str] = []
    for block in blocks:
        if block["silent"]:
            continue

        qid = block["id"]

        # Must have id that exists in questions (unknown -> dropped, not invalid)
        if qid not in questions:
            continue

        question = questions[qid]
        options = question.get("options", [])

        # Must have at least one Precedent line with non-null ref
        if not block["precedent_lines"]:
            return False, "pat-invalid", []

        # Raw refs must be non-null (requirement: mandatory ref per obligation).
        # Curated citations are checked against the curated path index instead.
        for prec_line in block["precedent_lines"]:
            if _is_curated(prec_line):
                entry = curated_paths.get(prec_line["path"])
                if entry is None or entry.get("answer") != prec_line["answer"]:
                    return False, "pat-invalid", []
                continue
            ref = prec_line["ref"]
            if ref is None:
                return False, "pat-invalid", []

        # Must have Prediction, Confidence, Why, Distinguish (Distinguish guard: non-empty when spoken)
        if not block["prediction"] or not block["confidence"] or not block["why"] or not block["distinguish"]:
            return False, "pat-invalid", []

        # Prediction must match one of the options (mismatch -> dropped, not invalid)
        if block["prediction"] not in options:
            continue

        # All cited raw refs must exist in digest (missing -> invalid)
        for prec_line in block["precedent_lines"]:
            if _is_curated(prec_line):
                continue
            ref = prec_line["ref"]
            if ref not in digest_refs:
                return False, "pat-invalid", []

        # This block is valid
        valid_ids.append(qid)

    if spoke_on == 0:
        return True, "pat-silent", []

    return True, "", valid_ids


def validate_cmd(precedent_path: str, questions_path: str, digest_path: str, out_path: Optional[str]) -> int:
    """The validate subcommand: read Pat's output and validate it.

    Exit codes:
    - 0: data-side outcome (valid or invalid)
    - 2: invocation error (missing input files)
    """
    # Try to read and parse the precedent file
    try:
        pat_text = _read_text(precedent_path)
    except (OSError, UnicodeDecodeError):
        result = {
            "status": "invalid",
            "reason": "pat-failed",
            "blocks": {},
        }
        output = json.dumps(result, ensure_ascii=False)
        if out_path:
            try:
                _write_atomic(out_path, output)
            except _Usage:
                return 2
        else:
            sys.stdout.write(output + "\n")
        return 0

    # Parse receipt
    receipt = _parse_receipt(pat_text)
    if receipt is None:
        result = {
            "status": "invalid",
            "reason": "pat-invalid",
            "blocks": {},
        }
        output = json.dumps(result, ensure_ascii=False)
        if out_path:
            try:
                _write_atomic(out_path, output)
            except _Usage:
                return 2
        else:
            sys.stdout.write(output + "\n")
        return 0

    # Load questions and digest; exit 2 if files are unreadable
    try:
        questions_text = _read_text(questions_path)
        questions_data = json.loads(questions_text)
        if not isinstance(questions_data, dict) or "questions" not in questions_data:
            questions = {}
        else:
            questions = {}
            for item in questions_data.get("questions", []):
                if isinstance(item, dict) and "id" in item:
                    qid = item["id"]
                    questions[qid] = {
                        "header": item.get("header", ""),
                        "question": item.get("question", ""),
                        "options": item.get("options", []),
                        "recommended": item.get("recommended"),
                    }
    except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
        raise _Usage("cannot read or parse questions file: %s" % questions_path)

    try:
        digest_refs, curated_paths = _load_digest(digest_path)
    except (OSError, UnicodeDecodeError):
        raise _Usage("cannot read digest file: %s" % digest_path)

    # Parse blocks
    blocks = _parse_blocks(pat_text)

    # Count total asked (non-silent blocks in input)
    asked_count = sum(1 for b in blocks if not b["silent"])

    # Validate blocks
    valid, reason, valid_ids = _validate_blocks(blocks, questions, digest_refs, curated_paths, receipt)

    # Build result
    if valid or reason == "pat-silent":
        status = "ok"
    else:
        status = "invalid"

    result_blocks: Dict[str, Any] = {}
    shown_count = 0
    if status == "ok":
        # Build result blocks only for valid ids
        for block in blocks:
            if not block["silent"] and block["id"] in valid_ids:
                result_blocks[block["id"]] = {
                    "precedent_lines": block["precedent_lines"],
                    "prediction": block["prediction"],
                    "confidence": block["confidence"],
                    "why": block["why"],
                    "distinguish": block["distinguish"],
                }
                shown_count += 1

    result = {
        "status": status,
        "reason": reason if status == "invalid" else ("pat-silent" if reason == "pat-silent" else ""),
        "blocks": result_blocks,
        "shown": shown_count,
        "asked": asked_count,
    }

    output = json.dumps(result, ensure_ascii=False)
    if out_path:
        try:
            _write_atomic(out_path, output)
        except _Usage:
            return 2
    else:
        sys.stdout.write(output + "\n")

    return 0


def _curated_display_line(prec: Dict[str, Any], prediction: str, confidence: str, differs: Any) -> Optional[str]:
    """Build the display line for a curated citation, or None to drop it.

    Shape: Precedent: curated rule "<answer>" (<path>). Prediction: <p> (<conf>)[. Differs...]
    Same 240-char budget priority as raw lines: Differs and Prediction are kept, then the answer is truncated.
    """
    if confidence not in ("high", "medium"):
        return None

    answer = _sanitize(prec.get("answer", ""))
    path = _sanitize(prec.get("path", ""))
    pred_clause = ". Prediction: %s (%s)." % (prediction, confidence)
    diff_clause = " Differs from the panel's recommendation." if differs else ""

    line_text = "Precedent: curated rule \"" + answer + "\" (" + path + ")" + pred_clause + diff_clause
    if len(line_text) > 240:
        frame = "Precedent: curated rule \"\" (" + path + ")"
        budget = 240 - len(pred_clause) - len(diff_clause) - len(frame)
        if budget < 2:
            return None
        answer = answer[:budget - 1] + "…"
        line_text = "Precedent: curated rule \"" + answer + "\" (" + path + ")" + pred_clause + diff_clause

    if len(line_text) > 240:
        return None
    return line_text


def line_cmd(validated_path: str, questions_path: str, out_path: Optional[str]) -> int:
    """The line subcommand: extract display lines for spoken questions.

    Returns JSON object {<id>: "<one display line, <=240 chars>"} for spoken ids only.
    """
    # Load validated result
    try:
        validated_text = _read_text(validated_path)
        validated = json.loads(validated_text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raise _Usage("cannot read or parse --validated %s" % validated_path)

    # Load questions
    questions = _load_questions(questions_path)

    blocks = validated.get("blocks", {})
    lines: Dict[str, str] = {}

    for qid, block in blocks.items():
        if qid not in questions:
            continue

        question = questions[qid]
        prediction = _sanitize(block.get("prediction", ""))

        # Check if prediction differs from recommendation
        recommended = question.get("recommended")
        differs = recommended and recommended != prediction

        # Get the first (most recent) precedent line
        prec_list = block.get("precedent_lines", [])
        if not prec_list:
            continue

        prec = prec_list[0]
        if _is_curated(prec):
            # Curated lines carry no date, so the date guard does not apply
            curated_line = _curated_display_line(prec, prediction, block.get("confidence", ""), differs)
            if curated_line is not None:
                lines[qid] = curated_line
            continue

        date = prec.get("date", "")
        # Validate date format: YYYY-MM-DD
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            # Invalid date; skip this line
            continue
        ref = prec.get("ref")
        reason = prec.get("reason")
        answer = _sanitize(prec.get("answer", ""))

        # Build the line
        confidence = block.get("confidence", "")
        if confidence not in ("high", "medium"):
            # Invalid confidence; skip this line
            continue

        line_parts = ["Precedent: you chose \"", answer, "\" on ", date]

        if ref is not None:
            line_parts.append(" (#")
            line_parts.append(str(ref))
            line_parts.append(")")

        if reason:
            line_parts.append(": \"")
            line_parts.append(_sanitize(reason))
            line_parts.append("\"")

        line_parts.append(". Prediction: ")
        line_parts.append(prediction)
        line_parts.append(" (")
        line_parts.append(confidence)
        line_parts.append(").")

        if differs:
            line_parts.append(" Differs from the panel's recommendation.")

        line_text = "".join(line_parts)

        # Enforce 240-char budget with priority: Differs, Prediction, date/ref, then reason/answer
        if len(line_text) > 240:
            # Reserve Differs, Prediction clauses
            diff_clause = " Differs from the panel's recommendation." if differs else ""
            pred_clause = ". Prediction: %s (%s)." % (prediction, confidence)

            # Calculate remaining budget
            fixed_len = len(pred_clause) + len(diff_clause)
            if fixed_len > 240:
                # Can't fit even with minimal content; drop this line
                continue

            budget = 240 - fixed_len
            # Build base: "Precedent: you chose "{answer}" on {date} [{ref}]: "{reason}""
            base_start = "Precedent: you chose \""
            date_part = "\" on " + date
            if ref is not None:
                date_part += " (#" + str(ref) + ")"

            # Try to fit as much as possible
            available = budget - len(base_start) - len(date_part)
            if available <= 0:
                # Not enough space; drop reason
                answer_part = _sanitize(answer)
                if len(base_start + answer_part + date_part) <= budget:
                    line_text = base_start + answer_part + date_part + pred_clause + diff_clause
                else:
                    # Truncate answer
                    max_answer = budget - len(base_start) - len(date_part) - 1
                    if max_answer > 0:
                        answer_part = _sanitize(answer)[:max_answer] + "…"
                        line_text = base_start + answer_part + date_part + pred_clause + diff_clause
                    else:
                        continue
            else:
                # Fit reason first, then truncate if needed
                reason_text = _sanitize(reason) if reason else ""
                available_for_reason = available - len(": \"") - len("\"")
                if available_for_reason > 0:
                    if len(reason_text) <= available_for_reason:
                        line_text = base_start + _sanitize(answer) + date_part + ": \"" + reason_text + "\"" + pred_clause + diff_clause
                    else:
                        # Truncate reason
                        truncated_reason = reason_text[:available_for_reason - 1] + "…"
                        line_text = base_start + _sanitize(answer) + date_part + ": \"" + truncated_reason + "\"" + pred_clause + diff_clause
                else:
                    # No room for reason, try without it
                    line_text = base_start + _sanitize(answer) + date_part + pred_clause + diff_clause

        # Final check: ensure it doesn't exceed 240
        if len(line_text) > 240:
            continue

        lines[qid] = line_text

    output = json.dumps(lines, ensure_ascii=False)
    if out_path:
        _write_atomic(out_path, output)
    else:
        sys.stdout.write(output + "\n")

    return 0


def section_cmd(validated_path: str, out_path: Optional[str]) -> int:
    """The section subcommand: emit markdown section for decisions.md."""
    # Load validated result
    try:
        validated_text = _read_text(validated_path)
        validated = json.loads(validated_text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raise _Usage("cannot read or parse --validated %s" % validated_path)

    blocks = validated.get("blocks", {})

    # Build markdown section
    lines = ["## Precedent"]
    if not blocks:
        lines.append("> (no precedent shown)")
    else:
        for qid, block in blocks.items():
            # Emit each field as a blockquote
            for prec in block.get("precedent_lines", []):
                if _is_curated(prec):
                    curated_answer = _sanitize(prec.get("answer", ""))
                    curated_path = _sanitize(prec.get("path", ""))
                    lines.append("> **Precedent**: curated rule \"" + curated_answer + "\" (" + curated_path + ")")
                    continue

                date = _sanitize(prec.get("date", ""))
                ref = prec.get("ref")
                source = _sanitize(prec.get("source", ""))
                answer = _sanitize(prec.get("answer", ""))
                recommended = _sanitize(prec.get("recommended", "")) if prec.get("recommended") else ""
                reason = _sanitize(prec.get("reason", "")) if prec.get("reason") else ""

                prec_line = "> **Precedent**: " + date
                if ref is not None:
                    prec_line += " · #" + str(ref)
                prec_line += " · " + source + " · chose \"" + answer + "\""
                if recommended:
                    prec_line += " (recommended was \"" + recommended + "\")"
                if reason:
                    prec_line += " · reason: \"" + reason + "\""

                lines.append(prec_line)

            # Prediction
            prediction = _sanitize(block.get("prediction", ""))
            lines.append("> **Prediction**: " + prediction)

            # Confidence
            confidence = block.get("confidence", "")
            lines.append("> **Confidence**: " + confidence)

            # Why
            why = _sanitize(block.get("why", ""))
            lines.append("> **Why**: " + why)

            # Distinguish
            distinguish = _sanitize(block.get("distinguish", ""))
            lines.append("> **Distinguish**: " + distinguish)

            # Blank line between blocks
            lines.append("")

    output = "\n".join(lines) + "\n"
    if out_path:
        _write_atomic(out_path, output)
    else:
        sys.stdout.write(output)

    return 0


def _write_atomic(path: str, text: str) -> None:
    """Write text to path via a temp file in the same dir, then os.replace.

    Raises _Usage for invocation errors (parent dir missing, --out argument invalid).
    May raise OSError for post-parse I/O failures; callers must map to exit code 1.
    """
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise _Usage("cannot write --out %s: parent directory does not exist" % path)

    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=parent,
            prefix=".precedent-render-", suffix=".tmp", delete=False,
        )
    except OSError as exc:
        raise _Usage("cannot write --out %s: %s" % (path, exc.strerror or exc))

    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
        os.replace(temp_name, target)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except BaseException:
            pass
        # Post-parse I/O error: raise as OSError, not _Usage
        raise exc
    except BaseException:
        try:
            os.unlink(temp_name)
        except BaseException:
            pass
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="precedent-render.py",
        description="Render a validated precedent file for display or integration.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate subcommand
    validate_parser = subparsers.add_parser("validate", help="Validate Pat's output")
    validate_parser.add_argument("--precedent", required=True, metavar="P", help="path to precedent.md file")
    validate_parser.add_argument("--questions", required=True, metavar="Q", help="path to questions JSON file")
    validate_parser.add_argument("--digest", required=True, metavar="D", help="path to digest JSONL file")
    validate_parser.add_argument("--out", metavar="PATH", default=None, help="write output to PATH")

    # line subcommand
    line_parser = subparsers.add_parser("line", help="Extract display lines for spoken questions")
    line_parser.add_argument("--validated", required=True, metavar="V", help="path to validated JSON from validate")
    line_parser.add_argument("--questions", required=True, metavar="Q", help="path to questions JSON file")
    line_parser.add_argument("--out", metavar="PATH", default=None, help="write output to PATH")

    # section subcommand
    section_parser = subparsers.add_parser("section", help="Emit markdown section for decisions.md")
    section_parser.add_argument("--validated", required=True, metavar="V", help="path to validated JSON from validate")
    section_parser.add_argument("--out", metavar="PATH", default=None, help="write output to PATH")

    return parser


def _run(argv: Optional[Sequence[str]]) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "validate":
        return validate_cmd(args.precedent, args.questions, args.digest, args.out)
    elif args.command == "line":
        return line_cmd(args.validated, args.questions, args.out)
    elif args.command == "section":
        return section_cmd(args.validated, args.out)
    else:
        raise _Usage("unknown command: %s" % args.command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns: 0 for data outcomes, 2 for invocation errors, 1 for internal errors."""
    try:
        return _run(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except _Usage as exc:
        _stderr("precedent-render: %s" % exc)
        return 2
    except Exception as exc:
        tb = traceback.extract_tb(sys.exc_info()[2])
        if tb:
            frame = tb[-1]
            lineno = frame.lineno if frame.lineno is not None else 0
            source = "%s:%d in %s" % (os.path.basename(frame.filename), lineno, frame.name)
        else:
            source = "unknown"
        _stderr("precedent-render: internal error (%s: %s) [%s]" % (type(exc).__name__, exc, source))
        return 1


if __name__ == "__main__":
    sys.exit(main())
