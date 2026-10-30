#!/usr/bin/env python3
"""
Human-attested "Accept and continue" eligibility and trail writer for /expert-flow (#365).

The eligibility rule is a closed, default-deny allowlist keyed on typed state, not on prose
judgment and not on the LLM-written `failed (<reason>)` log suffix. A step is eligible only when:

  - STEP is one of plan|implement|review|fix|verify;
  - state.json stop_source[STEP] is `audit` or `validation` (never `receipt`, an agent's own FAILED);
  - steps/NN-STEP.md exists, its last `Decision:` line is `Decision: OK` and it has `<!-- step-end -->`;
  - state.json stop_reason[STEP] matches one allowlisted (stop_source, verifier-owned reason) pair;
  - the newest lifecycle line for STEP in log.md is a `failed` line.

Allowlist (stop_source, reason test) -> class:
  audit      + prefix "reduced run (audit): "      -> audit-reduced
  validation + prefix "reduced run: "              -> receipt-reduction
  validation + exact  "receipt admits deviation"   -> receipt-deviation
  validation + prefix "plan checkpoint incomplete (" -> plan-checkpoint
  validation + prefix "review rulings incomplete ("  -> review-rulings

Subcommands:

  eligible --flow-dir D --step S --nn NN
      Print exactly one line: `ACCEPT: yes <class>` or `ACCEPT: no <why>`. Exit 0.
      <why> is one of: step-<s>, stop-source-<v>, stop-source-absent, missing-output, unlisted-reason, not-failed.

  record --flow-dir D --step S --nn NN --attempt J --note-stdin
      Re-run the eligibility check. If eligibility check refuses, print the `ACCEPT: no <why>` line
      to stdout and exit 1. If note validation refuses, print to stderr and exit 1. Otherwise append
      one block to steps/NN-S.accepted.md (append-only trail), print the class to stdout, and exit 0.
      The note is read from stdin; an empty note is recorded as `none`.

Stdout:
  eligible: the ACCEPT line.
  record: the class on success; `ACCEPT: no <why>` on eligibility refusal.

Stderr:
  record note validation refusal: the error message from refuse().

Exit codes:
  0: success (eligible prints yes or no; record appends a block)
  1: record refused (eligibility check failed, note validation failed, or IO error during write)
  2: usage error, or fatal IO error (missing or unreadable state.json, unreadable receipt or log)
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, NamedTuple, NoReturn, Optional, Tuple

STEPS: Tuple[str, ...] = ("plan", "implement", "review", "fix", "verify")

NN_RE = re.compile(r"[0-9]+")
ATTEMPT_RE = re.compile(r"[1-9][0-9]*")
SAFE_TOKEN_RE = re.compile(r"[a-z0-9-]+")
DECISION_PREFIX = "Decision:"
DECISION_OK = "Decision: OK"
STEP_END_LINE = "<!-- step-end -->"
BLOCK_END_LINE = "<!-- accepted-end -->"
SENTINELS = ("HUMAN_TEXT_EOF", "REASON_EOF")

# Same 1 MiB tail cap as scripts/flow_log.py (LOG_READ_CAP_BYTES). Mirrored, not imported,
# so this CLI has no import coupling to other repo scripts.
LOG_READ_CAP_BYTES = 1_048_576


class AllowlistEntry(NamedTuple):
    """One entry in the allowlist: (stop_source, reason text, match kind, class name)."""

    stop_source: str
    text: str
    kind: Literal["prefix", "exact"]
    klass: str


# Allowlist of (stop_source, reason, match kind, class). match kind is "prefix" or "exact".
ALLOWLIST: Tuple[AllowlistEntry, ...] = (
    AllowlistEntry("audit", "reduced run (audit): ", "prefix", "audit-reduced"),
    AllowlistEntry("validation", "reduced run: ", "prefix", "receipt-reduction"),
    AllowlistEntry("validation", "receipt admits deviation", "exact", "receipt-deviation"),
    AllowlistEntry("validation", "plan checkpoint incomplete (", "prefix", "plan-checkpoint"),
    AllowlistEntry("validation", "review rulings incomplete (", "prefix", "review-rulings"),
)

# Derive eligible stop sources from allowlist, preserving order.
STOP_SOURCES_ELIGIBLE: Tuple[str, ...] = tuple(dict.fromkeys(entry.stop_source for entry in ALLOWLIST))


class Decision(NamedTuple):
    """Outcome of the eligibility check. accept True carries a class; False carries a why token."""

    accept: bool
    token: str
    stop_source: str = ""
    reason: str = ""

    def line(self) -> str:
        """Return the one-line ACCEPT verdict."""
        return f"ACCEPT: yes {self.token}" if self.accept else f"ACCEPT: no {self.token}"


def io_error(msg: str) -> NoReturn:
    """Print a one-line message to stderr and exit 2 (usage or IO error)."""
    print(f"flow-accept: {msg}", file=sys.stderr)
    sys.exit(2)


def refuse(msg: str) -> NoReturn:
    """Print a one-line message to stderr and exit 1 (record refusal)."""
    print(f"flow-accept: {msg}", file=sys.stderr)
    sys.exit(1)


def safe_token(value: str) -> str:
    """Return value if it is a plain lowercase token, otherwise the placeholder `other`.

    Keeps the ACCEPT line a single clean line whatever state.json holds.
    """
    return value if SAFE_TOKEN_RE.fullmatch(value) else "other"


def resolve_flow_dir(raw: str) -> Path:
    """Return the resolved flow directory, or exit 2 if it is not a directory."""
    flow = Path(raw).expanduser().resolve()
    if not flow.is_dir():
        io_error(f"flow dir does not exist or is not a directory: {raw}")
    return flow


def load_state(flow: Path) -> Dict[str, Any]:
    """Return the parsed state.json object, or exit 2 if it is missing, unreadable or not an object."""
    state_path = flow / "state.json"
    if not state_path.is_file():
        io_error(f"no state.json in {flow}")
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        io_error(f"cannot read state.json: {e}")
    if not isinstance(data, dict):
        io_error("state.json is not a JSON object")
    return data


def step_map(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return state[key] as a dict, treating an absent key as empty and exiting 2 on a wrong type."""
    value = state.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        io_error(f"state.json {key} is not a JSON object")
    return value


def receipt_problem(flow: Path, nn: str, step: str) -> Optional[str]:
    """Return None if steps/NN-STEP.md is a complete OK receipt, else a short reason string."""
    receipt = flow / "steps" / f"{nn}-{step}.md"
    if not receipt.is_file():
        return "missing"
    try:
        text = receipt.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        io_error(f"cannot read receipt {receipt}: {e.strerror or e}")
    lines = [line.rstrip() for line in text.split("\n")]
    decisions = [line for line in lines if line.startswith(DECISION_PREFIX)]
    if not decisions or decisions[-1] != DECISION_OK:
        return "decision"
    if STEP_END_LINE not in lines:
        return "step-end"
    return None


def classify(stop_source: str, reason: str) -> Optional[str]:
    """Return the allowlisted class for (stop_source, reason), or None if it is not allowlisted."""
    for source, text, kind, klass in ALLOWLIST:
        if stop_source != source:
            continue
        if kind == "exact" and reason == text:
            return klass
        if kind == "prefix" and reason.startswith(text):
            return klass
    return None


def read_log_tail(log: Path) -> str:
    """Return the last LOG_READ_CAP_BYTES of log.md, dropping the partial first line after a seek."""
    try:
        size = log.stat().st_size
        truncated = size > LOG_READ_CAP_BYTES
        with open(log, "rb") as fh:
            if truncated:
                fh.seek(size - LOG_READ_CAP_BYTES)
            raw = fh.read()
    except OSError as e:
        io_error(f"cannot read log.md: {e.strerror or e}")
    if truncated:
        newline = raw.find(b"\n")
        raw = raw[newline + 1:] if newline != -1 else b""
    return raw.decode("utf-8", errors="replace")


def newest_lifecycle_is_failed(flow: Path, step: str) -> bool:
    """True only if the newest lifecycle line for STEP in log.md is a `failed` line.

    The alternation matches expert-flow resume rule 2. The `(reason)` suffix is display text
    and is never parsed here. A missing log.md has no lifecycle line, so this returns False.
    """
    log = flow / "log.md"
    if not log.is_file():
        return False
    lifecycle = re.compile(
        r"[^ ]+ " + re.escape(step) + r" (begin|retry|ok|taken-over|accepted \(.*\)|failed( \(.*\))?)"
    )
    newest: Optional[str] = None
    for line in read_log_tail(log).split("\n"):
        match = lifecycle.fullmatch(line)
        if match is not None:
            newest = match.group(1)
    return newest is not None and newest.startswith("failed")


def decide(flow: Path, step: str, nn: str) -> Decision:
    """Run the closed, default-deny eligibility check.

    Returns Decision(accept=True, token=<class>, stop_source, reason) if eligible.
    Returns Decision(accept=False, token=<why>) otherwise, where <why> is one of:
    step-<s>, stop-source-<v>, stop-source-absent, missing-output, unlisted-reason, not-failed.

    Note: missing-output is returned for three failure cases that all indicate missing receipt
    output: the receipt file is absent, the last Decision line is not "Decision: OK", or the
    step-end marker is missing.
    """
    if step not in STEPS:
        return Decision(False, f"step-{safe_token(step)}")

    state = load_state(flow)
    sources = step_map(state, "stop_source")
    source = sources.get(step)
    if source is None:
        return Decision(False, "stop-source-absent")
    if not isinstance(source, str) or source not in STOP_SOURCES_ELIGIBLE:
        return Decision(False, f"stop-source-{safe_token(source) if isinstance(source, str) else 'other'}")

    problem = receipt_problem(flow, nn, step)
    if problem is not None:
        return Decision(False, "missing-output", source)

    reasons = step_map(state, "stop_reason")
    reason = reasons.get(step)
    if not isinstance(reason, str):
        return Decision(False, "unlisted-reason", source)
    klass = classify(source, reason)
    if klass is None:
        return Decision(False, "unlisted-reason", source, reason)

    if not newest_lifecycle_is_failed(flow, step):
        return Decision(False, "not-failed", source, reason)

    return Decision(True, klass, source, reason)


def format_block(
    step: str, attempt: int, decision: Decision, note: str, timestamp: str
) -> str:
    """Render one accept block. FAILED_REASON is kept on one line; NOTE uses an indented block when multi-line."""
    reason = decision.reason.replace("\r", " ").replace("\n", " ")
    lines = [
        f"STEP: {step}",
        f"ATTEMPT: {attempt}",
        f"ACCEPT_CLASS: {decision.token}",
        f"STOP_SOURCE: {decision.stop_source}",
        f"FAILED_REASON: {reason}",
    ]
    if not note:
        lines.append("NOTE: none")
    elif "\n" not in note:
        lines.append(f"NOTE: {note}")
    else:
        lines.append("NOTE:")
        lines.extend(f"  {line}" for line in note.split("\n"))
    lines.append("ACCEPTED_BY: human")
    lines.append(f"TS: {timestamp}")
    lines.append(BLOCK_END_LINE)
    return "\n".join(lines) + "\n"


def read_note(note_stdin_bytes: bytes) -> str:
    """Decode and normalise the human note from stdin. Refuses sentinel or block-end lines."""
    try:
        text = note_stdin_bytes.decode("utf-8")
    except UnicodeDecodeError:
        refuse("note on stdin is not valid UTF-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    for line in text.split("\n"):
        if line in SENTINELS:
            refuse("note has a line reading HUMAN_TEXT_EOF or REASON_EOF, which would end the heredoc early; remove that line")
        if line.strip() == BLOCK_END_LINE:
            refuse(f"note has a line reading {BLOCK_END_LINE}, which would end the trail block early; remove that line")
    return text


def cmd_eligible(flow: Path, step: str, nn: str) -> None:
    """Print the ACCEPT verdict for the step and exit 0."""
    print(decide(flow, step, nn).line())


def cmd_record(flow: Path, step: str, nn: str, attempt: int, note: str) -> None:
    """Check eligibility and record an accept block. Exit 1 if eligibility check or note validation fails.

    Eligibility refusal prints `ACCEPT: no <why>` to stdout and exits 1. Note validation refusal
    prints to stderr via refuse() and exits 1. Success appends a block to the trail file and prints
    the class to stdout, then exits 0.

    Non-idempotent: each call appends a new block to the trail file if the step is eligible, even if the
    same note and attempt are passed multiple times. The required caller sequence is: check eligibility,
    prompt for the note, validate the note (refusing on HUMAN_TEXT_EOF, REASON_EOF, or block-end sentinel),
    then record. Re-running record with the same arguments is safe only if the second run's note is
    different or if the step's eligibility has changed (e.g., the receipt was updated).
    """
    decision = decide(flow, step, nn)
    if not decision.accept:
        print(decision.line())
        sys.exit(1)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = format_block(step, attempt, decision, note, timestamp)
    trail = flow / "steps" / f"{nn}-{step}.accepted.md"
    try:
        with open(trail, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as e:
        io_error(f"cannot write {trail}: {e.strerror or e}")
    print(decision.token)


def validate_nn(nn: str) -> str:
    """Return NN if it is digits only, else exit 2."""
    if not NN_RE.fullmatch(nn):
        io_error(f"invalid NN '{nn}': use digits only, for example 01")
    return nn


def main() -> None:
    """Parse arguments and dispatch to eligible or record."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommand")

    eligible_parser = subparsers.add_parser("eligible", help="Print ACCEPT: yes/no for a hard-stopped step")
    eligible_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    eligible_parser.add_argument("--step", required=True, help="Step name (plan|implement|review|fix|verify)")
    eligible_parser.add_argument("--nn", required=True, help="Step number (digits only)")

    record_parser = subparsers.add_parser("record", help="Append an accept trail block for an eligible step")
    record_parser.add_argument("--flow-dir", required=True, help="Flow directory")
    record_parser.add_argument("--step", required=True, help="Step name (plan|implement|review|fix|verify)")
    record_parser.add_argument("--nn", required=True, help="Step number (digits only)")
    record_parser.add_argument("--attempt", required=True, help="Attempt number J (positive integer)")
    record_parser.add_argument("--note-stdin", action="store_true", required=True,
                               help="Deliberate stdin marker: read the human note from stdin")

    args = parser.parse_args()

    flow = resolve_flow_dir(args.flow_dir)
    nn = validate_nn(args.nn)

    if args.command == "eligible":
        cmd_eligible(flow, args.step, nn)
        return

    if not ATTEMPT_RE.fullmatch(args.attempt):
        io_error(f"invalid attempt '{args.attempt}': must be a positive integer with no leading zero")
    note = read_note(sys.stdin.buffer.read())
    cmd_record(flow, args.step, nn, int(args.attempt), note)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        io_error(f"unexpected error: {e}")
