#!/usr/bin/env python3
"""Verify step receipts against on-disk artifacts for /expert-flow.

The attempt-start bound for --since-from-log is derived from the flow's log.md by code (scripts/flow_log.py)
and is never copied by the model. Where it is used: `implement-ledger.py locate --flow-dir --step` in the
expert-flow Fidelity audit, and `--since-from-log` in the expert-flow resume path's rule-8 validation.
Why it matters: a bound that is too early or wrong lets a stale run pass the audit (a false pass that
masks a skipped step), and a bound that is too late or missing gives a false `ledger unavailable`.
Design: ADR-0013 (deterministic logic lives in scripts, not prose) and ADR-0023 (the receipt is a claim
that the Fidelity audit exists to check).

Exit codes:
  0: artifacts OK
  1: receipt admits deviation, artifact missing, or a plan/review checkpoint was not fully relayed
     (counting is marker-based: the questions in files named by receipt's awaiting-answers /
     reused-answers markers that have a corresponding answers/ file). With --since-from-log, a
     missing or malformed begin/retry line in log.md is exit 1.
  2: bad arguments or internal error (message on stderr). With --since-from-log, an absent or
     invalid flow dir is exit 2.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, NoReturn, Optional

# Import shared schema module
try:
    import plan_checkpoint_schema as schema
except ImportError:
    # If not in path, add scripts directory
    sys.path.insert(0, str(Path(__file__).parent))
    import plan_checkpoint_schema as schema

# Shared leaf module (log.md attempt-start parsing and ISO-8601); same pattern as plan_checkpoint_schema.
try:
    import flow_log
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    import flow_log

# Constants for timeouts and mtime skew tolerance
MTIME_SKEW_SECS = 5
GIT_TIMEOUT_SECS = 5

# Contribution failure reasons
CONTRIB_ABSENT = "absent"
CONTRIB_UNTERMINATED = "unterminated"
CONTRIB_STANDIN = "stand-in"


class RelayCount(NamedTuple):
    """Result of counting relayed questions from receipt markers.

    Invariants (see count_relayed_questions):
    - markers is the number of distinct `<step>-*.json` basenames named by the receipt.
    - Each marker basename lands in at most one of unanswered / unreadable / (answered and
      parsed). With no flow dir, nothing is classified, so unanswered and unreadable stay empty.
    - When the questions/ dir is missing, every marker basename is reported as unanswered.
    - asked counts only questions from answered, parsed marker files; it is 0 otherwise.
    """
    asked: int
    markers: int
    unanswered: List[str]
    unreadable: List[str]


# A decision brief heading in a plan session's decisions.md (`#### Q1. …`, `#### Q2 — …`).
# Captures the Q number as group 1 to allow deduplication.
BRIEF_HEADING_RE = re.compile(r"^####\s+Q(\d+)\b", re.MULTILINE)
# A *Needs you* item heading in claude-action-plan.md (`### 1. Title`).
NEEDS_YOU_ITEM_RE = re.compile(r"^###\s+\d+\.", re.MULTILINE)
NEEDS_YOU_HEADING_RE = re.compile(r"^##\s+Needs you\b.*$", re.MULTILINE)


def error(msg: str) -> NoReturn:
    """Print error to stderr and exit with code 2."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def read_file(path: Path) -> str:
    """Read file contents; error on missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        error(f"Could not read {path}: {e}")
    except (OSError, UnicodeDecodeError) as e:
        error(f"Could not read {path}: {e}")


def canonicalize_path(path: str) -> Path:
    """Canonicalize a path (symlinks and .. resolved). Containment is checked by callers."""
    return Path(path).expanduser().resolve()


def scan_for_deviation(content: str) -> bool:
    """Scan for Deviation: or NOT dispatched outside fenced blocks, blockquotes, backticks.

    Returns True if deviation found (should reject).

    Accepts list/bold prefixes before Deviation: (e.g. "- Deviation:", "**Deviation:**").
    Tracks code fences by marker character and length (``` vs ~~~). Treats an unterminated
    fence as malformed (flags deviation).
    """
    lines = content.split("\n")
    in_fence = False
    fence_marker = ""
    fence_length = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        # Handle fenced code blocks (triple backticks or tildes)
        # Match opening fence: at least 3 backticks or tildes, possibly with language tag
        fence_match = re.match(r"^(\s*)(```+|~~~+)", line)
        if fence_match:
            marker = line[fence_match.start(2):fence_match.end(2)]
            marker_char = marker[0]
            marker_len = len(marker)

            if not in_fence:
                in_fence = True
                fence_marker = marker_char
                fence_length = marker_len
            elif fence_marker == marker_char and marker_len >= fence_length:
                in_fence = False
                fence_marker = ""
                fence_length = 0
            i += 1
            continue

        # If inside fence, skip
        if in_fence:
            i += 1
            continue

        # If unterminated fence at EOF, treat as malformed
        if i == len(lines) - 1 and in_fence:
            return True

        # Check for blockquote line (starts with >)
        if line.lstrip().startswith(">"):
            i += 1
            continue

        # Check for Deviation: at line start, possibly with list/bold prefix
        # Patterns: "Deviation:", "- Deviation:", "* Deviation:", "**Deviation:**"
        if re.match(r"^(\s*[-*]\s+)?(\*\*)?Deviation:", line):
            return True

        # Check for NOT dispatched outside backticks
        # Remove text inside single-backtick spans
        cleaned_line = re.sub(r"`[^`]*`", "", line)
        if "NOT dispatched" in cleaned_line:
            return True

        i += 1

    # Check if fence was left unterminated at end
    if in_fence:
        return True

    return False


def _is_report_checkpoint(flow_dir: Path, relay_count: RelayCount) -> bool:
    """Check if any marker-named report question file has kind: "report"."""
    qdir = flow_dir / "questions"
    if not qdir.exists():
        return False

    for marker in relay_count.unanswered:
        qfile = qdir / marker
        try:
            data = json.loads(qfile.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("kind") == "report":
                return True
        except (OSError, UnicodeDecodeError, ValueError):
            pass

    # Also check answered files
    adir = flow_dir / "answers"
    if adir.exists():
        for answer_file in adir.glob("plan-*.json"):
            # Find corresponding question file by name
            qfile = qdir / answer_file.name
            if qfile.exists():
                try:
                    data = json.loads(qfile.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("kind") == "report":
                        return True
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

    return False


def _load_answers_file(path: Path) -> Optional[Dict]:
    """Load and parse an answers JSON file. Return None on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _load_reaction_file(path: Path) -> Optional[Dict]:
    """Load and parse a reaction JSON file. Return None on error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _validate_reply_excerpt(excerpt: str, reply_file: Path) -> bool:
    """Check if excerpt is present in reply file (whitespace-normalized).

    Args:
        excerpt: The excerpt to validate
        reply_file: Path to the .reply-<round>.md file

    Returns:
        True if excerpt is found in file, False otherwise
    """
    if not reply_file.exists():
        return False
    try:
        reply_text = reply_file.read_text(encoding="utf-8")
        normalized_excerpt = schema.normalize_whitespace(excerpt)
        normalized_reply = schema.normalize_whitespace(reply_text)
        return normalized_excerpt in normalized_reply
    except (OSError, UnicodeDecodeError):
        return False


# Post-tier ledger handle: a D/W/Q letter followed by digits (D1, W1, Q1, ...).
_LEDGER_HANDLE_RE = re.compile(r"[DWQ]\d+")


def _ledger_handles(ledger: dict) -> Dict[str, dict]:
    """Return {handle: data} for every post-tier handle in the checkpoint ledger.

    A key is a handle only if it is a string matching `_LEDGER_HANDLE_RE` and its value is a
    dict. Empty or non-matching keys are simply not handles (never an error).
    """
    return {
        key: data
        for key, data in ledger.items()
        if isinstance(key, str) and _LEDGER_HANDLE_RE.fullmatch(key) and isinstance(data, dict)
    }


def _ledger_handle_keys(ledger: dict) -> set:
    """Return the set of post-tier handle keys, regardless of value type.

    Used by the legacy fail-closed in check_plan: any D/W/Q-shaped key fails closed, even when
    its value is malformed. The dict requirement in `_ledger_handles` applies only where values
    are validated (the report branch).
    """
    return {key for key in ledger if isinstance(key, str) and _LEDGER_HANDLE_RE.fullmatch(key)}


def check_report_checkpoint(
    session_dir: Path, flow_dir: Path, decisions_text: str, attempt: int = 1, relay_count: Optional[RelayCount] = None
) -> Optional[str]:
    """Check report-kind plan checkpoint. Return error reason or None.

    Implements rules 1-10 from Step 5.

    Args:
        session_dir: Path to the plan session directory
        flow_dir: Path to the flow directory
        decisions_text: Content of decisions.md from the session
        attempt: The attempt number (from receipt)
        relay_count: Optional RelayCount from the receipt (for scoping verification)
    """
    # Load the checkpoint ledger (F6: use schema module)
    ledger = schema.extract_ledger_json(decisions_text)
    if not ledger:
        return "plan checkpoint incomplete (no checkpoint ledger in decisions.md)"

    # Validate ledger shape
    ledger_err = schema.validate_ledger_shape(ledger)
    if ledger_err:
        return f"plan checkpoint incomplete ({ledger_err})"

    seated = ledger.get("seated", [])
    expected_reactions = ledger.get("expected_reactions", [])
    briefs = ledger.get("briefs", {})

    if not isinstance(seated, list):
        seated = []
    if not isinstance(expected_reactions, list):
        expected_reactions = []

    # Rule 7a: Check that contrarian-carl is in seated list
    if "contrarian-carl" not in seated:
        return "plan checkpoint incomplete (missing contrarian-carl from seated list)"

    # Get all handles from ledger (post-tier handles: D1, W1, Q1, ...)
    ledger_handles_and_tiers = _ledger_handles(ledger)
    ledger_handles = set(ledger_handles_and_tiers.keys())

    # Determine which question files to check: from relay_count.unanswered and answered
    qdir = flow_dir / "questions"
    adir = flow_dir / "answers"

    # NOTE: the marker/ledger handling in this section (and the ledger-handle fail-closed in
    # check_plan below) is slated for rework under #310 and #308. Keep the fail-closed
    # behaviour and the existing reason prefixes stable until those issues land.

    # Fail closed: a marker-named report whose answers file is absent (or whose question file
    # is unreadable) never provably reached the human, wherever a decoy copy may sit.
    if relay_count and (relay_count.unanswered or relay_count.unreadable):
        bad = relay_count.unanswered + relay_count.unreadable
        return f"plan checkpoint incomplete (marker files without a readable answered report: {', '.join(sorted(bad))})"

    # Rule 1: Verify we have plan-* question files
    # (reaching this point means relay_count has no unanswered/unreadable markers)
    marker_files = set()
    unreadable_answers: List[str] = []
    if relay_count:
        # Also need to check answered files
        if adir.exists():
            answer_files = sorted(adir.glob("plan-*.json"), key=lambda f: schema.answer_file_sort_key(f.name))
            for answer_file in answer_files:
                # Check if this file has an attempt matching our attempt
                try:
                    adata = json.loads(answer_file.read_text(encoding="utf-8"))
                    if isinstance(adata, dict) and adata.get("attempt") == attempt:
                        marker_files.add(answer_file.name)
                except (OSError, UnicodeDecodeError, ValueError):
                    unreadable_answers.append(answer_file.name)
    else:
        # Without relay_count, fall back to finding all plan-*.json files for this attempt
        if qdir.exists():
            qfiles = sorted(qdir.glob("plan-*.json"), key=lambda f: schema.answer_file_sort_key(f.name))
            for qfile in qfiles:
                try:
                    qdata = json.loads(qfile.read_text(encoding="utf-8"))
                    if isinstance(qdata, dict) and qdata.get("attempt") == attempt:
                        marker_files.add(qfile.name)
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

    if not qdir.exists():
        return "plan checkpoint incomplete (questions directory missing)"

    # Find all report question files for this attempt
    report_files = {}
    for marker in marker_files:
        qfile = qdir / marker
        if not qfile.exists():
            continue
        try:
            data = json.loads(qfile.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("kind") == "report" and data.get("attempt") == attempt:
                report_files[marker] = data
        except (OSError, UnicodeDecodeError, ValueError):
            pass

    if not report_files:
        # An answers file that cannot be read never provably reached the human: name it.
        if unreadable_answers:
            return f"plan checkpoint incomplete (marker files without a readable answered report: {', '.join(sorted(unreadable_answers))})"
        return "plan checkpoint incomplete (no report question files for this attempt)"

    # Rule 2: Check that round-1 report's handle set matches ledger handles
    round1_basename = min(report_files.keys()) if report_files else None
    if round1_basename:
        round1_data = report_files[round1_basename]
        if not isinstance(round1_data.get("items"), list):
            return f"plan checkpoint incomplete (malformed report file: {round1_basename})"
        report_handles = set()
        for item in round1_data["items"]:
            if isinstance(item, dict) and "handle" in item:
                report_handles.add(item["handle"])

        if report_handles != ledger_handles:
            missing = ledger_handles - report_handles
            extra = report_handles - ledger_handles
            reason = "plan checkpoint incomplete (round-1 handles mismatch"
            if missing:
                reason += f": missing {', '.join(sorted(missing))}"
            if extra:
                reason += f": extra {', '.join(sorted(extra))}"
            reason += ")"
            return reason

    # Rule 3, 4: Check answers files for all report rounds and check for unresolved
    final_dispositions: Dict[str, str] = {}
    prose_rounds: Dict[str, int] = {}  # map basename to round number

    if adir.exists():
        # Sort by attempt and round to process in order (numeric sort by SEQ number)
        answer_files = sorted(adir.glob("plan-*.json"), key=lambda f: schema.answer_file_sort_key(f.name))
        for answer_file in answer_files:
            try:
                answers_data = json.loads(answer_file.read_text(encoding="utf-8"))
                if not isinstance(answers_data, dict):
                    return f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

                # F33: Key on the answers file's kind field
                kind = answers_data.get("kind")
                file_attempt = answers_data.get("attempt")

                # F17: Validate the shape up front
                shape_err = schema.validate_answers_file_shape(answers_data, kind or "ask")
                if shape_err:
                    return f"plan checkpoint incomplete ({shape_err})"

                # Only process files for this attempt
                if file_attempt != attempt:
                    continue

                if kind == "report":
                    # Use items for report variant
                    items = answers_data.get("items", {})
                    if not isinstance(items, dict):
                        return f"plan checkpoint incomplete (malformed answers file: {answer_file.name})"

                    # Rule 3: Check that all handles are present
                    for handle in ledger_handles:
                        if handle not in items:
                            return f"plan checkpoint incomplete (answer file missing handle: {handle})"

                    # Track final disposition and prose rounds
                    round_num = answers_data.get("round")
                    for handle, item in items.items():
                        if isinstance(item, dict):
                            disposition = item.get("disposition")
                            final_dispositions[handle] = disposition
                            source = item.get("source")
                            if source == "prose":
                                prose_rounds[answer_file.name] = round_num
                else:
                    # Standard form variant (legacy)
                    answers = answers_data.get("answers", {})
                    if not isinstance(answers, dict):
                        continue

                    for handle in ledger_handles:
                        if handle not in answers:
                            return f"plan checkpoint incomplete (answer file missing handle: {handle})"

                    for handle, answer in answers.items():
                        if isinstance(answer, dict):
                            disposition = answer.get("disposition")
                            final_dispositions[handle] = disposition
                            source = answer.get("source")
                            if source == "prose":
                                prose_rounds[answer_file.name] = 1

            except (OSError, UnicodeDecodeError, ValueError):
                return f"plan checkpoint incomplete (unreadable answers file: {answer_file.name})"

    # Rule 4: Check for unresolved dispositions
    for handle, disposition in final_dispositions.items():
        if disposition == "unresolved":
            return f"plan checkpoint incomplete (unresolved: {handle})"

    # Rebuild must-answer info from report files
    must_answer_items: Dict[str, bool] = {}
    must_answer_reasons: Dict[str, str] = {}

    for basename, report_data in report_files.items():
        items = report_data.get("items", [])
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            if not handle:
                continue

            must_answer = item.get("must_answer", False)
            reason_must_answer = item.get("reason_must_answer")

            if must_answer or reason_must_answer:
                must_answer_items[handle] = True
                if reason_must_answer:
                    must_answer_reasons[handle] = reason_must_answer

    # Rule 5: Check must-answer items have proper dispositions
    for handle in must_answer_items:
        disposition = final_dispositions.get(handle)
        if not disposition:
            return f"plan checkpoint incomplete (must-answer without quote: {handle})"

        if disposition == "accepted":
            return f"plan checkpoint incomplete (must-answer without quote: {handle})"

        if disposition in ["answered", "overridden"]:
            reply_excerpt = None
            round_num = None
            for answer_file in adir.glob("plan-*.json"):
                try:
                    answers_data = json.loads(answer_file.read_text(encoding="utf-8"))
                    if not isinstance(answers_data, dict):
                        continue
                    if answers_data.get("attempt") != attempt:
                        continue

                    kind = answers_data.get("kind")
                    if kind == "report":
                        items = answers_data.get("items", {})
                        if handle in items and items[handle].get("disposition") == disposition:
                            reply_excerpt = items[handle].get("reply_excerpt")
                            round_num = answers_data.get("round")
                            break
                    else:
                        answers = answers_data.get("answers", {})
                        if handle in answers and answers[handle].get("disposition") == disposition:
                            reply_excerpt = answers[handle].get("reply_excerpt")
                            round_num = answers_data.get("round")
                            break
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

            if not reply_excerpt:
                return f"plan checkpoint incomplete (must-answer without quote: {handle})"

            # F15: Validate that reply_excerpt is actually in the .reply-<round>.md file
            if round_num and reply_excerpt:
                # Find the corresponding answer file basename to construct the reply file path
                reply_basename = None
                for afile in adir.glob("plan-*.json"):
                    try:
                        adata = json.loads(afile.read_text(encoding="utf-8"))
                        if isinstance(adata, dict) and adata.get("round") == round_num and adata.get("attempt") == attempt:
                            # Found the answer file for this round; construct the reply file path
                            reply_basename = f"{afile.stem}.reply-{round_num}.md"
                            break
                    except (OSError, UnicodeDecodeError, ValueError):
                        pass

                if reply_basename:
                    reply_file = adir / reply_basename
                    if not _validate_reply_excerpt(reply_excerpt, reply_file):
                        return f"plan checkpoint incomplete (reply excerpt not found in {reply_file.name})"

        if disposition == "accepted-on-continue":
            found = False
            for answer_file in adir.glob("plan-*.json"):
                try:
                    answers_data = json.loads(answer_file.read_text(encoding="utf-8"))
                    if not isinstance(answers_data, dict):
                        continue
                    if answers_data.get("attempt") != attempt:
                        continue

                    kind = answers_data.get("kind")
                    if kind == "report":
                        items = answers_data.get("items", {})
                        if handle in items and items[handle].get("disposition") == "accepted-on-continue":
                            loop_end = answers_data.get("loop_end")
                            reply_excerpt = items[handle].get("reply_excerpt")
                            if loop_end != "continue":
                                return f"plan checkpoint incomplete (accepted-on-continue without loop_end: {handle})"
                            if not reply_excerpt:
                                return f"plan checkpoint incomplete (must-answer without quote: {handle})"
                            # F15: Validate excerpt against reply file
                            round_num = answers_data.get("round")
                            if round_num and reply_excerpt:
                                reply_basename = f"{answer_file.stem}.reply-{round_num}.md"
                                reply_file = adir / reply_basename
                                if not _validate_reply_excerpt(reply_excerpt, reply_file):
                                    return f"plan checkpoint incomplete (reply excerpt not found in {reply_file.name})"
                            found = True
                            break
                    else:
                        answers = answers_data.get("answers", {})
                        if handle in answers and answers[handle].get("disposition") == "accepted-on-continue":
                            loop_end = answers_data.get("loop_end")
                            reply_excerpt = answers[handle].get("reply_excerpt")
                            if loop_end != "continue":
                                return f"plan checkpoint incomplete (accepted-on-continue without loop_end: {handle})"
                            if not reply_excerpt:
                                return f"plan checkpoint incomplete (must-answer without quote: {handle})"
                            # F15: Validate excerpt against reply file
                            round_num = answers_data.get("round")
                            if round_num and reply_excerpt:
                                reply_basename = f"{answer_file.stem}.reply-{round_num}.md"
                                reply_file = adir / reply_basename
                                if not _validate_reply_excerpt(reply_excerpt, reply_file):
                                    return f"plan checkpoint incomplete (reply excerpt not found in {reply_file.name})"
                            found = True
                            break
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

            if not found:
                return f"plan checkpoint incomplete (accepted-on-continue not found: {handle})"

    # Rule 6: Check accepted items are only D or W
    for handle, disposition in final_dispositions.items():
        if disposition == "accepted":
            tier = ledger_handles_and_tiers.get(handle, {}).get("tier")
            if tier not in ["decided", "worth-a-look"]:
                return f"plan checkpoint incomplete (accepted non-D/W: {handle})"

    # Rule 7: Check reaction files exist and are complete
    missing_experts: List[str] = []
    for expert in seated:
        # Check for pre-show round-1
        found = False
        for entry in expected_reactions:
            if (
                isinstance(entry, dict)
                and entry.get("slug") == expert
                and entry.get("mode") == "pre-show"
                and entry.get("round") == 1
            ):
                found = True
                # Check file exists and has "complete": true sentinel
                path_str = entry.get("path")
                if path_str:
                    # F25: Confine path to session dir
                    confine_err = schema.confine_path_to_dir(path_str, session_dir)
                    if confine_err:
                        return f"plan checkpoint incomplete ({confine_err})"

                    path = session_dir / path_str
                    data = _load_reaction_file(path)
                    if not data or not data.get("complete"):
                        return f"plan checkpoint incomplete (reaction file missing or incomplete: {path_str})"
                break

        if not found:
            missing_experts.append(expert)

    if missing_experts:
        return f"plan checkpoint incomplete (missing pre-show reactions: {', '.join(missing_experts)})"

    # Rule 8, 8b: Check for protested D/W items and disagreement D/W items
    # Build evidence from pre-show reactions only
    evidence: Dict[str, Dict[str, str]] = {}

    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "pre-show":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data:
            continue

        # Process each brief_id in the reaction file
        for brief_id, reaction_data in data.items():
            if brief_id == "complete" or not isinstance(reaction_data, dict):
                continue

            # Get the handle for this brief_id from ledger.briefs
            # (we'll check this in Rule 8b)
            brief_type = briefs.get(brief_id, {}).get("brief_type")

            # Classify the reaction using schema module
            classification = schema.classify_reaction(reaction_data, slug)

            # Find the handle(s) for this brief_id (from post-tier ledger)
            for handle, handle_data in ledger_handles_and_tiers.items():
                if handle_data.get("brief_id") == brief_id:
                    if handle not in evidence:
                        evidence[handle] = {}
                    evidence[handle][slug] = classification

    for handle, tier_data in ledger_handles_and_tiers.items():
        tier = tier_data.get("tier")
        brief_id = tier_data.get("brief_id")
        brief_type = tier_data.get("brief_type")

        if tier in ["decided", "worth-a-look"]:
            # Rule 8b: Check that tier is not "disagreement"
            if brief_type == "disagreement":
                return f"plan checkpoint tier mismatch ({handle} is a disagreement)"

            # Rule 8: Check that no reaction says "protested"
            handle_evidence = evidence.get(handle, {})
            for expert, reaction in handle_evidence.items():
                if reaction == "protested":
                    return f"plan checkpoint tier mismatch ({handle} protested by {expert})"

    # Rule 9: Check raw-reply files exist for prose rounds
    for prose_basename in prose_rounds:
        round_num = prose_rounds[prose_basename]
        reply_file = adir / f"plan-{prose_basename.split('-')[1].split('.')[0]}.reply-{round_num}.md"
        if not reply_file.exists():
            return f"plan checkpoint incomplete (missing reply file: {reply_file.name})"

    # Rule 10: Check for repeated pushbacks on same handle by same expert
    pushback_tracker: Dict[tuple, int] = {}  # (expert, handle) -> count

    # F9: First, check that all expected reply-mode reaction files exist and are complete
    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "reply":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data or not data.get("complete"):
            return f"plan checkpoint incomplete (reaction file missing or incomplete: {path_str})"

    # Now process pushback reactions
    for entry in expected_reactions:
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "reply":
            continue

        slug = entry.get("slug")
        path_str = entry.get("path")
        if not slug or not path_str:
            continue

        path = session_dir / path_str
        data = _load_reaction_file(path)
        if not data:
            continue

        for handle, reaction_data in data.items():
            if handle == "complete" or not isinstance(reaction_data, dict):
                continue
            if reaction_data.get("pushback") is True:
                key = (slug, handle)
                pushback_tracker[key] = pushback_tracker.get(key, 0) + 1

                if pushback_tracker[key] > 1:
                    return f"plan checkpoint repeated pushback ({slug} on {handle})"

    return None


def check_plan(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check plan artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get FINAL_PLAN_PATH from last matching line
    plan_path_str = None
    for line in receipt.split("\n"):
        if line.startswith("FINAL_PLAN_PATH:"):
            plan_path_str = line.split(":", 1)[1].strip()

    if not plan_path_str:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    plan_path = canonicalize_path(plan_path_str)
    plans_dir = (home / ".claude" / "plans").resolve()

    # Check path is under plans/
    try:
        plan_path.relative_to(plans_dir)
    except ValueError:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    # Check that FINAL_PLAN_PATH file exists
    if not plan_path.is_file():
        return "plan artifacts missing (FINAL_PLAN_PATH file does not exist)"

    # Extract invocation ID from basename
    basename = plan_path.name
    # Pattern: {slug}-{YYYYMMDDTHHMMSS}-{NNNNN}.md
    match = re.search(r"-(\d{8}T\d{6}-\d{5})\.md$", basename)
    if not match:
        return "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"

    invocation_id = match.group(1)

    # Find session dir: glob ~/.claude/plan-sessions/*/*-{invocation_id}
    session_root = home / ".claude" / "plan-sessions"
    matches = []
    if session_root.exists():
        for dir_path in session_root.glob(f"*/*-{invocation_id}"):
            if dir_path.is_dir():
                matches.append(dir_path)

    if len(matches) != 1:
        return f"plan artifacts missing (session dir: {len(matches)} matches for {invocation_id})"

    session_dir = matches[0]

    # Check context.md mtime
    context_file = session_dir / "context.md"
    if not context_file.exists():
        return "plan artifacts missing (session dir older than this attempt)"

    try:
        context_mtime = context_file.stat().st_mtime
        if context_mtime < since - MTIME_SKEW_SECS:
            return "plan artifacts missing (session dir older than this attempt)"
    except OSError:
        return "plan artifacts missing (session dir older than this attempt)"

    # Check selected-experts.md
    roster_file = session_dir / "selected-experts.md"
    if not roster_file.exists():
        return "plan artifacts missing (roster)"

    roster = read_file(roster_file)

    # Parse roster: find first table with header's first cell = "Expert"
    roster_rows = _parse_roster_table(roster)
    if not roster_rows:
        return "plan artifacts missing (roster)"

    # Load reviewer index
    index_file = home / ".claude" / "reviewers" / "index.yaml"
    reviewer_map = _load_reviewer_index(index_file) if index_file.exists() else {}

    # Check contributions
    for row in roster_rows:
        # Skip non-contributors
        if row.lower().startswith("auditor") or row.lower().startswith("synthesis"):
            continue

        # Strip trailing (annotation)
        name = re.sub(r"\s*\([^)]*\)$", "", row).strip()

        # Resolve name to file stem
        file_stem = reviewer_map.get(name.lower())
        if not file_stem:
            # Fallback: name lowercased, non-alphanumerics -> '-'
            file_stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        # Try {stem}-contribution.md or kebab-name form
        kebab = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        contrib_file = session_dir / f"{file_stem}-contribution.md"
        if not contrib_file.exists():
            contrib_file = session_dir / f"{kebab}-contribution.md"
        if not contrib_file.exists():
            return f"plan artifacts missing (contribution: {name} {CONTRIB_ABSENT})"

        contrib = read_file(contrib_file)

        # Check sentinel: last non-empty line
        lines = [ln for ln in contrib.split("\n") if ln.strip()]
        if not lines or lines[-1] != "<!-- contribution-end -->":
            return f"plan artifacts missing (contribution: {name} {CONTRIB_UNTERMINATED})"

        # Check for stand-in
        if _is_standin(contrib):
            return f"plan artifacts missing (contribution: {name} {CONTRIB_STANDIN})"

    # Check that plan.md exists in the session directory
    plan_session_file = session_dir / "plan.md"
    if not plan_session_file.is_file():
        return "plan artifacts missing (plan.md)"

    # Checkpoint completeness: every decision brief in decisions.md must have been put to the
    # human through the relay. decisions.md is written before the first Ask (even with zero
    # briefs), so its absence means the checkpoint never ran.
    decisions_file = session_dir / "decisions.md"
    if not decisions_file.exists():
        return "plan artifacts missing (decisions.md)"

    decisions_text = read_file(decisions_file)

    # Parse ATTEMPT from receipt (default to 1 if not found)
    attempt = 1
    for line in receipt.split("\n"):
        if line.startswith("ATTEMPT:"):
            attempt_str = line.split(":", 1)[1].strip()
            try:
                attempt = int(attempt_str)
            except ValueError:
                pass
            break

    # Branch on checkpoint kind: report vs. legacy per-question
    relay_count = count_relayed_questions(flow_dir, "plan", receipt)
    if relay_count.markers > 0 and flow_dir is not None:
        # Check if any marker-named report has kind: "report"
        is_report_checkpoint = _is_report_checkpoint(flow_dir, relay_count)
        if is_report_checkpoint:
            return check_report_checkpoint(session_dir, flow_dir, decisions_text, attempt, relay_count)

    # Fail closed: if ledger carries D/W/Q handles but we didn't take the report branch,
    # or if unreadable is non-empty in the legacy branch.
    # NOTE: this ledger-handle fail-closed is slated for rework under #310 and #308; keep the
    # "plan checkpoint incomplete (" reason prefix stable until those issues land.
    ledger = schema.extract_ledger_json(decisions_text)
    if ledger:
        # Check if ledger has D/W/Q handles (post-tier handles)
        ledger_handles = _ledger_handle_keys(ledger)
        if ledger_handles:
            # Ledger has handles but we didn't validate via report checkpoint, fail closed.
            # Say which situation applies so the reason is actionable, using the same suffix
            # info as the legacy path below.
            handles_txt = ", ".join(sorted(ledger_handles))
            detail_parts = []
            if relay_count.markers == 0:
                detail_parts.append("no relayed marker-named report files")
            elif flow_dir is None:
                detail_parts.append("flow dir unresolved")
            else:
                detail_parts.append("marker-named files exist but none is a kind: report checkpoint")
            if relay_count.unanswered:
                detail_parts.append(f"unanswered: {', '.join(relay_count.unanswered)}")
            if relay_count.unreadable:
                detail_parts.append(f"unreadable: {', '.join(relay_count.unreadable)}")
            detail = ", ".join(detail_parts)
            return (
                f"plan checkpoint incomplete (ledger has D/W/Q handles but no report file was validated: "
                f"{detail}; handles: {handles_txt})"
            )

    # Legacy per-question rule (fail closed if unreadable files present)
    briefs = count_briefs(decisions_text)
    if relay_count.asked < briefs or relay_count.unanswered or relay_count.unreadable:
        reason = f"plan checkpoint incomplete (briefs: {briefs}, asked: {relay_count.asked}"
        if relay_count.markers == 0:
            reason += ", receipt carries no plan-* markers"
        if flow_dir is None and relay_count.markers:
            reason += ", flow dir unresolved"
        if relay_count.unanswered:
            reason += f", unanswered: {', '.join(relay_count.unanswered)}"
        if relay_count.unreadable:
            reason += f", unreadable: {', '.join(relay_count.unreadable)}"
        reason += ")"
        return reason

    return None


def check_review(receipt_file: Path, since: float, home: Path, flow_dir: Optional[Path]) -> Optional[str]:
    """Check review artifacts. Return error reason or None."""
    receipt = read_file(receipt_file)

    # Get REVIEW_DIR from last matching line
    review_dir_str = None
    for line in receipt.split("\n"):
        if line.startswith("REVIEW_DIR:"):
            review_dir_str = line.split(":", 1)[1].strip()

    if not review_dir_str:
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    review_dir = canonicalize_path(review_dir_str)
    reviews_root = (home / ".claude" / "reviews").resolve()

    # Check path is under reviews/
    try:
        review_dir.relative_to(reviews_root)
    except ValueError:
        return "review artifacts missing (REVIEW_DIR not under reviews/)"

    # Check required files
    for filename in ["claude-action-plan.md", "final-report.md", "findings.json"]:
        if not (review_dir / filename).exists():
            return f"review artifacts missing ({filename})"

    # Check claude-action-plan.md mtime
    action_plan = review_dir / "claude-action-plan.md"
    try:
        mtime = action_plan.stat().st_mtime
        if mtime < since - MTIME_SKEW_SECS:
            return "review artifacts missing (action plan older than this attempt)"
    except OSError:
        return "review artifacts missing (action plan older than this attempt)"

    # Check for reviewer checkpoints: >=2 *-pass1.md or *-pod.md
    checkpoints = list(review_dir.glob("*-pass1.md")) + list(review_dir.glob("*-pod.md"))
    if len(checkpoints) < 2:
        return f"review artifacts missing (reviewer checkpoints: {len(checkpoints)})"

    # Rulings completeness: every *Needs you* item must have been put to the human through the relay.
    needs_you = count_needs_you_items(read_file(action_plan))
    relay_count = count_relayed_questions(flow_dir, "review", receipt)
    if relay_count.asked < needs_you or relay_count.unanswered:
        reason = f"review rulings incomplete (needs-you: {needs_you}, asked: {relay_count.asked}"
        if relay_count.markers == 0:
            reason += ", receipt carries no review-* markers"
        if flow_dir is None and relay_count.markers:
            reason += ", flow dir unresolved"
        if relay_count.unanswered:
            reason += f", unanswered: {', '.join(relay_count.unanswered)}"
        if relay_count.unreadable:
            reason += f", unreadable: {', '.join(relay_count.unreadable)}"
        reason += ")"
        return reason

    return None


def count_briefs(decisions: str) -> int:
    """Count `#### Q<n>` decision-brief headings in a plan session's decisions.md.

    Uses the distinct Q numbers to handle repeated headings (e.g., in checkpoint results).
    """
    q_numbers = BRIEF_HEADING_RE.findall(decisions)
    return len(set(q_numbers))


def count_needs_you_items(action_plan: str) -> int:
    """Count `### N.` items under the `## Needs you` heading of claude-action-plan.md.

    The section ends at the next `## ` heading. No section means zero items.
    """
    m = NEEDS_YOU_HEADING_RE.search(action_plan)
    if not m:
        return 0
    rest = action_plan[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    return len(NEEDS_YOU_ITEM_RE.findall(section))


def count_relayed_questions(
    flow_dir: Optional[Path], step: str, receipt_text: str
) -> RelayCount:
    r"""Sum `questions[]` across files named by receipt markers.

    Parses the receipt's `<!-- awaiting-answers: questions/<STEP>-<SEQ>.json -->` and
    `<!-- reused-answers: questions/<STEP>-<SEQ>.json -->` markers (captured by regexes
    `<!-- awaiting-answers: (questions/[^ ]+\.json) -->` and
    `<!-- reused-answers: (questions/[^ ]+\.json) -->`). Only question files named by
    these markers are counted.

    Returns RelayCount with:
    - asked: sum of questions[] across all successfully parsed, marker-named files with answers/
    - markers: total number of distinct marker-named basenames found
    - unanswered: basenames of marker-named files without an answers/ file
    - unreadable: basenames of marker-named files that failed to read/parse

    Zero markers returns RelayCount(asked=0, markers=0, unanswered=[], unreadable=[]).
    With no flow dir but markers present, returns asked=0 and no "no markers" claim.
    The check fails closed when briefs or items exist and nothing provably reached the human.
    """
    # Find marker-named files from receipt text, before checking flow_dir
    awaiting_re = re.compile(r"<!-- awaiting-answers: (questions/[^ ]+\.json) -->")
    reused_re = re.compile(r"<!-- reused-answers: (questions/[^ ]+\.json) -->")

    marker_paths_raw = []
    marker_paths_raw.extend(awaiting_re.findall(receipt_text))
    marker_paths_raw.extend(reused_re.findall(receipt_text))

    # Keep only files matching <step>-*.json, deduplicate
    marker_files = set()
    for path_str in marker_paths_raw:
        # Extract basename and check it matches <step>-*.json
        basename = Path(path_str).name
        if basename.startswith(f"{step}-"):
            marker_files.add(basename)

    marker_count = len(marker_files)

    # If no markers, return early
    if marker_count == 0:
        return RelayCount(asked=0, markers=0, unanswered=[], unreadable=[])

    # If no flow dir, return with asked=0 but don't claim "no markers"
    if flow_dir is None:
        return RelayCount(asked=0, markers=marker_count, unanswered=[], unreadable=[])

    # Check that questions dir exists
    qdir = flow_dir / "questions"
    if not qdir.exists():
        # questions/ missing: nothing can be read, so every marker is reported as unanswered.
        return RelayCount(asked=0, markers=marker_count, unanswered=sorted(marker_files), unreadable=[])

    # Sum questions from files with answers/ and track unanswered/unreadable
    total = 0
    unanswered = []
    unreadable = []
    answers_dir = flow_dir / "answers"

    for basename in sorted(marker_files):
        # Answered means the answers file exists as a regular file. This is existence-only by
        # design: the answers/ file is evidence the relay ran, and its content is not validated.
        # Only the question file's questions[] are counted.
        answers_file = answers_dir / basename
        if not answers_file.is_file():
            unanswered.append(basename)
            continue

        # Try to read and parse the question file
        qfile = qdir / basename
        try:
            data = json.loads(qfile.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            unreadable.append(basename)
            continue
        questions = data.get("questions") if isinstance(data, dict) else None
        if isinstance(questions, list):
            total += len(questions)
        else:
            unreadable.append(basename)

    return RelayCount(asked=total, markers=marker_count, unanswered=unanswered, unreadable=unreadable)


def resolve_flow_dir(explicit: Optional[str], receipt_file: Path) -> Optional[Path]:
    """`--flow-dir` when given; else the receipt's grandparent when the receipt sits in `steps/`."""
    if explicit:
        return canonicalize_path(explicit)
    receipt = receipt_file.resolve()
    if receipt.parent.name == "steps":
        return receipt.parent.parent
    return None


def _count_commits(repo: str, base: str, branch: str) -> int:
    """Count commits from base to branch. Return commit count; error on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", "--end-of-options", base + ".." + branch],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            error(f"Could not count commits: {result.stderr.strip()}")

        return int(result.stdout.strip())
    except subprocess.TimeoutExpired as e:
        error(f"Git operation failed: {e}")
    except ValueError as e:
        error(f"Git operation failed: {e}")


def check_implement(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
) -> Optional[str]:
    """Check implement artifacts. Return error reason or None."""
    count = _count_commits(repo, base, branch)
    if count < 1:
        return f"implement artifacts missing (no new commits on {branch} since step began)"

    # Check discarded files (parse from receipt and validate against branch)
    receipt = read_file(receipt_file)
    discarded_files = parse_discarded_files_block(receipt)
    if discarded_files:
        discard_error = check_discarded_files(repo, branch, discarded_files)
        if discard_error:
            return discard_error

    return None


def check_fix(
    receipt_file: Path,
    repo: str,
    branch: str,
    base: str,
    review_dir: str,
    home: Path,
) -> Optional[str]:
    """Check fix artifacts with zero-commit carve-out. Return error reason or None."""
    count = _count_commits(repo, base, branch)

    # If zero commits, check for zero "Doing it" items in action plan
    if count == 0:
        action_plan_path = canonicalize_path(review_dir)
        action_plan_path = action_plan_path / "claude-action-plan.md"
        reviews_root = (home / ".claude" / "reviews").resolve()

        # Check path is under reviews/
        try:
            action_plan_path.relative_to(reviews_root)
        except ValueError:
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        if not action_plan_path.exists():
            return f"fix artifacts missing (no new commits on {branch} since step began)"

        action_plan = read_file(action_plan_path)
        doing_it_count = _count_doing_it_items(action_plan)

        if doing_it_count >= 1:
            return f"fix artifacts missing (no new commits on {branch} since step began)"

    return None


# Alias kept so existing call sites and tests keep working; the implementation lives in flow_log.
_parse_iso8601 = flow_log.parse_iso8601


def _parse_roster_table(content: str) -> List[str]:
    """Parse markdown roster table and return list of Expert names."""
    rows: List[str] = []
    header_seen = False

    for line in content.split("\n"):
        # Table lines start with |
        if not line.strip().startswith("|"):
            if rows:  # Found table, return it
                return rows
            continue

        # Parse table row
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells:
            continue

        if not header_seen:
            # Check if first cell is "Expert"
            if cells[0].lower() == "expert":
                header_seen = True
            continue

        # Skip the separator row, collect the rest
        if cells[0] and set(cells[0]) <= set("-: "):
            continue
        if header_seen and cells:
            rows.append(cells[0])

    return rows if rows else []


def _load_reviewer_index(index_file: Path) -> Dict[str, str]:
    """Load reviewer index YAML and return {name.lower(): file_stem} mapping.

    Parses simple YAML format with "- name:" and "file:" pairs. On parse errors
    or missing file, returns an empty dict (fallback behavior).
    """
    result: Dict[str, str] = {}
    current_name: Optional[str] = None
    try:
        content = index_file.read_text(encoding="utf-8")
        for line in content.split("\n"):
            # Parse lines like: "- name: Danielle the Designer"
            name_match = re.search(r'^\s*-\s+name:\s*(.+)$', line)
            if name_match:
                current_name = name_match.group(1).strip()
                continue

            # Parse lines like: "file: danielle-designer.yaml"
            file_match = re.search(r'^\s+file:\s*(.+?)\.yaml\s*$', line)
            if file_match and current_name is not None:
                file_stem = file_match.group(1).strip()
                result[current_name.lower()] = file_stem
                current_name = None
    except OSError:
        pass

    return result


def _is_standin(content: str) -> bool:
    """Check if contribution is a stand-in (## Decision / FAILED or Decision: FAILED)."""
    lines = [ln.strip() for ln in content.split("\n")]

    # Check for ## Decision followed by FAILED
    for i, line in enumerate(lines):
        if line == "## Decision":
            # Look for next non-empty line
            for j in range(i + 1, len(lines)):
                if lines[j]:
                    if lines[j] == "FAILED":
                        return True
                    break

    # Check for inline Decision: FAILED
    for line in lines:
        if line == "Decision: FAILED":
            return True

    return False


def _count_doing_it_items(action_plan: str) -> int:
    """Count items in the 'Doing it' section by parsing table rows.

    Parses the "## Doing it: N" section, bounded by the next ## heading.
    Counts data rows in the table (excluding header and separator rows).
    Returns 0 if section not found (fail closed).

    Use with --from verify to check against the real claude-action-plan.md template.
    """
    lines = action_plan.split("\n")
    count = 0
    in_doing_it = False
    table_started = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Match "## Doing it:" heading (could have :N at the end)
        if re.match(r"^##\s+Doing it:", line):
            in_doing_it = True
            table_started = False
            i += 1
            continue

        # Stop at next ## heading (same or higher level)
        if in_doing_it and re.match(r"^##\s+", line):
            break

        if in_doing_it:
            stripped = line.strip()

            # Check for table start (lines starting with |)
            if stripped.startswith("|") and not table_started:
                table_started = True
                i += 1
                continue

            # In table: count data rows, skip header and separator
            if table_started and stripped.startswith("|"):
                # Skip separator rows (all dashes and pipes)
                if not re.match(r"^\|\s*-+\s*(\|\s*-+\s*)*\|$", stripped):
                    count += 1
            elif table_started and not stripped.startswith("|"):
                # Table ended
                table_started = False

        i += 1

    # If section never found, fail closed
    if not in_doing_it:
        return 0

    return count


def parse_receipt_field(receipt: str, field_name: str) -> Optional[str]:
    """Parse a single receipt field (e.g., IMPLEMENTER_MODEL, FAILURE_CLASS).

    Searches for field_name in the header block (fields before the first blank line
    or <!-- step-end --> marker). Returns the value of the last matching line, or None
    if not found.
    """
    result = None
    for line in receipt.split("\n"):
        # Stop at blank line or step-end marker
        if not line.strip() or "<!-- step-end -->" in line:
            break
        if line.startswith(f"{field_name}:"):
            result = line.split(":", 1)[1].strip()
    return result


def parse_discarded_files_block(receipt: str) -> List[str]:
    """Parse DISCARDED_ADDED_FILES block from receipt.

    Returns a list of file paths from indented lines under the block.
    """
    files: List[str] = []
    in_block = False
    for line in receipt.split("\n"):
        if line.startswith("DISCARDED_ADDED_FILES:"):
            in_block = True
            continue
        if in_block:
            # Check if line is indented (part of the block)
            if line and line[0] == " ":
                # Strip leading whitespace and extract the path
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
                    # Handle both "- path" and plain "path" formats
                    if stripped.startswith("- "):
                        files.append(stripped[2:])
                    else:
                        files.append(stripped)
            elif line and line[0] != " ":
                # End of block (non-indented line)
                break
    return files


def check_discarded_files(repo: str, branch: str, discarded_files: List[str]) -> Optional[str]:
    """Check that discarded files are absent from the branch via git ls-tree.

    Requires repo and branch to be non-empty. Rejects absolute paths and .. components.
    Returns error reason if any files are present or if validation fails, None if all are absent.
    """
    if not discarded_files:
        return None

    # Validate that repo and branch are provided
    if not repo or not branch:
        return "discarded files check requires --repo and --branch"

    # Validate each file path
    for file_path in discarded_files:
        # Reject absolute paths
        if file_path.startswith("/"):
            return f"discarded files check: absolute path not allowed ({file_path})"
        # Reject .. components
        if ".." in file_path:
            return f"discarded files check: .. component not allowed ({file_path})"

    present_files = []
    for file_path in discarded_files:
        try:
            result = subprocess.run(
                ["git", "-C", repo, "ls-tree", "-r", branch, "--", file_path],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECS,
            )
            # If exit code is 0 and there's output, the file exists
            if result.returncode == 0 and result.stdout.strip():
                present_files.append(file_path)
            # On non-zero exit or other git errors, fail closed
            elif result.returncode != 0 and result.returncode != 128:
                # 128 typically means "not found", which is OK
                return f"git operation failed checking {file_path}: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return f"git operation timed out checking {file_path}"
        except Exception as e:
            # Fail closed: any other error is treated as a check failure
            return f"git operation error checking {file_path}: {e}"

    if present_files:
        return f"discarded files still present ({', '.join(present_files)})"
    return None


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Verify step receipts against on-disk artifacts"
    )
    parser.add_argument(
        "--step",
        required=True,
        choices=["plan", "review", "implement", "fix", "track", "verify", "ship", "merge"],
        help="Step name",
    )
    parser.add_argument("--receipt", required=True, help="Path to receipt file")
    since_group = parser.add_mutually_exclusive_group(required=False)
    since_group.add_argument("--since", help="ISO-8601 UTC timestamp")
    since_group.add_argument(
        "--since-from-log",
        action="store_true",
        dest="since_from_log",
        help="derive the bound from the newest --step begin|retry line in the flow's log.md",
    )
    parser.add_argument("--repo", help="Repo directory (for implement/fix)")
    parser.add_argument("--branch", help="Branch name (for implement/fix)")
    parser.add_argument("--base", help="Base SHA (for implement/fix)")
    parser.add_argument("--review-dir", help="Review directory (for fix)")
    parser.add_argument(
        "--flow-dir",
        help="Flow directory holding questions/ (plan/review); default: the receipt's grandparent when it sits in steps/",
    )
    parser.add_argument(
        "--escalation",
        action="store_true",
        help="Escalation mode: print ESCALATE line and exit 0",
    )
    parser.add_argument(
        "--stop-source",
        choices=["receipt", "validation", "audit"],
        help="Stop source for escalation mode",
    )
    parser.add_argument(
        "--escalations",
        type=int,
        help="Number of escalations so far",
    )
    parser.add_argument(
        "--failures",
        type=int,
        help="Number of failures so far",
    )
    parser.add_argument(
        "--implementer-model-state",
        choices=["unset", "sonnet"],
        help="Current implementer model state",
    )
    parser.add_argument(
        "--expect-model",
        help="Expected implementer model for OK receipts",
    )

    args = parser.parse_args()

    # Read receipt file
    receipt_file = Path(args.receipt)
    if not receipt_file.exists():
        error(f"Receipt file not found: {args.receipt}")

    receipt = read_file(receipt_file)

    # Handle --escalation mode (check before any --since requirement)
    if args.escalation:
        # Check if receipt has a decision of OK
        decision = parse_receipt_field(receipt, "Decision")
        if decision == "OK":
            print("ESCALATE: none receipt-ok")
            sys.exit(0)

        # For non-implement/fix steps, print none and exit 0
        if args.step not in ["implement", "fix"]:
            print(f"ESCALATE: none step-{args.step}")
            sys.exit(0)

        # For implement/fix steps, validate required arguments
        if not args.stop_source:
            error("--escalation requires --stop-source")
        if args.escalations is None:
            error("--escalation requires --escalations")
        if args.failures is None:
            error("--escalation requires --failures")

        # Validate stop_source value
        if args.stop_source not in ["receipt", "validation", "audit"]:
            error(f"unknown --stop-source value: {args.stop_source}")

        # Default --implementer-model-state to 'unset' if missing
        implementer_model_state = args.implementer_model_state or "unset"

        # Parse receipt fields
        failure_class = parse_receipt_field(receipt, "FAILURE_CLASS")
        turns_exhausted = parse_receipt_field(receipt, "TURNS_EXHAUSTED")
        # Treat missing IMPLEMENTER_MODEL as haiku (pre-feature attempts ran Haiku)
        implementer_model = parse_receipt_field(receipt, "IMPLEMENTER_MODEL") or "haiku"

        # Determine if it's a failure
        if not failure_class:
            print("ESCALATE: none not-failed")
            sys.exit(0)

        # Check auto path eligibility (all 7 conditions)
        auto_eligible = (
            args.stop_source == "receipt"
            and failure_class == "implementer-shortfall"
            and turns_exhausted == "no"
            and implementer_model == "haiku"
            and implementer_model_state == "unset"
            and args.escalations == 0
            and args.failures < 2
        )

        if auto_eligible:
            print("ESCALATE: auto sonnet")
            sys.exit(0)

        # Check manual path eligibility (once-per-step: escalations must be 0)
        if args.escalations == 0 and args.failures < 2 and implementer_model == "haiku":
            # Manual escalation applicable, determine reason
            if args.stop_source != "receipt":
                print(f"ESCALATE: manual sonnet stop-source {args.stop_source}")
                sys.exit(0)
            elif failure_class != "implementer-shortfall":
                print("ESCALATE: manual sonnet class-other")
                sys.exit(0)
            elif turns_exhausted != "no":
                # Determine reason for turns being exhausted/unknown/absent
                if turns_exhausted == "yes":
                    reason = "turns-yes"
                elif turns_exhausted == "unknown":
                    reason = "turns-unknown"
                else:
                    reason = "turns-absent"
                print(f"ESCALATE: manual sonnet {reason}")
                sys.exit(0)
            elif implementer_model_state != "unset" or args.escalations > 0:
                # Go to none path instead
                pass
            else:
                print("ESCALATE: manual sonnet")
                sys.exit(0)

        # None path
        none_reason: Optional[str] = None
        if args.escalations is not None and args.escalations >= 1:
            none_reason = "escalations-spent"
        elif args.failures is not None and args.failures >= 2:
            none_reason = "failures-at-cap"
        elif implementer_model not in [None, "haiku"]:
            none_reason = "model-not-haiku"

        if none_reason:
            print(f"ESCALATE: none {none_reason}")
        else:
            print("ESCALATE: none")
        sys.exit(0)

    # Handle --expect-model mode (does not require --since)
    if args.expect_model:
        # Expect-model mode for implement/fix on OK receipts
        if args.step not in ["implement", "fix"]:
            error("--expect-model only supported for implement/fix steps")

        # Check for deviation
        if scan_for_deviation(receipt):
            print("Reason: receipt admits deviation")
            sys.exit(1)

        # Parse receipt fields
        receipt_impl_model = parse_receipt_field(receipt, "IMPLEMENTER_MODEL")

        # Rule 8: For escalated receipts (escalations > 0), validate model match is required
        if args.escalations is not None and args.escalations > 0:
            if receipt_impl_model != args.expect_model:
                print(f"implementer model mismatch: escalated receipt expected {args.expect_model}, got {receipt_impl_model}")
                sys.exit(1)
        else:
            # Check model match (standard case)
            if receipt_impl_model != args.expect_model:
                print(f"implementer model mismatch (expected {args.expect_model}, receipt {receipt_impl_model})")
                sys.exit(1)

        # Check discarded files (requires both repo and branch)
        if args.repo and args.branch:
            discarded_files = parse_discarded_files_block(receipt)
            if discarded_files:
                discarded_error = check_discarded_files(args.repo, args.branch, discarded_files)
                if discarded_error:
                    print(discarded_error)
                    sys.exit(1)

        sys.exit(0)

    # Standard artifact verification mode
    if args.since is None and not args.since_from_log:
        error("--since or --since-from-log required for standard verification")

    if args.since_from_log:
        # Bound from code, never from the model: exit 2 on a bad flow dir, exit 1 on a missing or
        # malformed begin/retry line.
        flow_dir_arg = resolve_flow_dir(args.flow_dir, Path(args.receipt))
        if flow_dir_arg is None:
            error("--since-from-log needs --flow-dir (or a receipt under steps/)")
        try:
            flow_dir_real = flow_log.validate_flow_dir(str(flow_dir_arg))
        except flow_log.FlowLogError as e:
            error(f"--since-from-log: {e}")
        # Reason lines go to stdout as the verdict; error() (stderr, exit 2) is reserved for bad arguments.
        try:
            log_ts, window_cut = flow_log.newest_attempt_start_detail(flow_dir_real / "log.md", args.step)
        except flow_log.FlowLogError as e:
            print(f"Reason: malformed begin or retry line for {args.step} ({e})")
            sys.exit(1)
        if log_ts is None and window_cut:
            print(f"Reason: no begin or retry line for {args.step} in the readable tail of log.md "
                  f"(log.md is truncated to its last {flow_log.LOG_READ_CAP_BYTES} bytes; an older line may exist)")
            sys.exit(1)
        if log_ts is None:
            print(f"Reason: no begin or retry line for {args.step}")
            sys.exit(1)
        since_ts = flow_log.parse_iso8601(log_ts)
    else:
        # Parse --since once; bad value exits with code 2
        try:
            since_ts = _parse_iso8601(args.since)
        except ValueError as e:
            error(str(e))

    if scan_for_deviation(receipt):
        print("Reason: receipt admits deviation")
        sys.exit(1)

    # Home directory for artifact roots
    home = Path.home().resolve()

    # Step-specific checks
    error_reason: Optional[str] = None

    flow_dir = resolve_flow_dir(args.flow_dir, receipt_file)

    if args.step == "plan":
        error_reason = check_plan(receipt_file, since_ts, home, flow_dir)
    elif args.step == "review":
        error_reason = check_review(receipt_file, since_ts, home, flow_dir)
    elif args.step == "implement":
        if not all([args.repo, args.branch, args.base]):
            error("--repo, --branch, --base required for implement")
        # Validate base as hex SHA (7-40 chars)
        if not re.match(r"^[0-9a-f]{7,40}$", args.base):
            error(f"Invalid base SHA: {args.base}")
        error_reason = check_implement(receipt_file, args.repo, args.branch, args.base)
    elif args.step == "fix":
        if not all([args.repo, args.branch, args.base, args.review_dir]):
            error("--repo, --branch, --base, --review-dir required for fix")
        # Validate base as hex SHA (7-40 chars)
        if not re.match(r"^[0-9a-f]{7,40}$", args.base):
            error(f"Invalid base SHA: {args.base}")
        error_reason = check_fix(
            receipt_file, args.repo, args.branch, args.base, args.review_dir, home
        )
    elif args.step in ["track", "verify", "ship", "merge"]:
        # No artifact checks
        error_reason = None

    if error_reason:
        print(f"Reason: {error_reason}")
        sys.exit(1)

    print("ARTIFACTS: ok")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error(f"Unexpected error: {e}")
