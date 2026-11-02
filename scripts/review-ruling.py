#!/usr/bin/env python3
"""
Record /expert-review Step 12 rulings in claude-action-plan.md (#408).

This script is the Step 12 writer of a review's STATUS, DECISION and rejected-options
fold. It touches one file, claude-action-plan.md, and only the fields of one item.

Subcommands:
  decide  --review-dir DIR --item N (--option LABEL | --custom TEXT)
          [--reasoning TEXT] [--mode local|pr]
  measure --review-dir DIR --item N --result TEXT [--mode local|pr]

decide rewrites item N of the "Needs you" section into a resolved record:
  - the Options block keeps only the chosen option (its bullet and Pro/Con lines);
  - STATUS becomes "decided", or "no-op" for a "Leave as-is" option;
  - the DECISION placeholder is replaced in place with "{label} — {reasoning}",
    where reasoning defaults to the chosen option's rationale;
  - the rejected options move into a collapsed <details> block after DECISION.
    <details> and </details> inside the folded text are entity-escaped.
measure sets STATUS to "measured" and replaces the DECISION placeholder in place.

Containment: the review dir must resolve (symlinks included) to a directory under
Path.home()/.claude/reviews. There is no flag or environment override for that root.
".." segments, a symlinked claude-action-plan.md and --mode pr are refused.

Free text (reasoning, --custom, --result) is reduced to one bounded line, with no
control or line-separator characters, no backtick fences, no <details> tags, and no
leading STATUS:, DECISION: or # markers. So it cannot forge a field or heading.

Writes are atomic: a temp file in the same directory, then os.replace.

Re-runs: an item already recorded with the same answer exits 0 with no write. A
different answer on an already-ruled item exits 3 with no write. Reasoning is excluded
from the idempotency comparison; the first recorded reasoning is preserved on re-runs
with a different --reasoning.

Option matching reuses precedent-digest.py's own helpers (_norm, _fold, _option_key,
_review_options), so an answer is matched the way parse_review reads it back.

Sequential execution: this script performs an unlocked read-modify-write on the plan
file. It is designed for sequential-only execution; concurrent calls may lose writes.
The Step 12 orchestrator ensures sequential operation.

PR mode: the --mode pr flag is refused by design; Step 12 in a coworker's PR review
(ADR-0009) must not write to the clone. The prompt ensures --mode is not passed in
that scenario; the flag itself is merely a documented safety layer at the script level.

Exit codes:
  0: recorded, or already recorded with the same answer
  1: named refusal (reason on stderr) or unexpected internal failure; the file is byte-identical
  3: conflicting prior ruling (existing DECISION on stderr); the file is byte-identical
"""

import argparse
import contextlib
import dataclasses
import importlib.util
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Callable, List, Literal, NoReturn, Optional, Protocol, Sequence, Tuple

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CONFLICT = 3

# Item status type, used for _Item dataclass and status value tracking
ItemStatus = Literal["pending-decision", "pending-measurement", "decided", "no-op", "measured"]

ACTION_PLAN = "claude-action-plan.md"
NEEDS_YOU_TITLE = re.compile(r"^needs you$", re.IGNORECASE)
NEEDS_MEASUREMENT_TITLE = re.compile(r"^needs measurement\s*(:.*)?$", re.IGNORECASE)
REJECTED_SUMMARY = "Options considered and rejected (record only — do not act on these)"
DETAILS_OPEN = "<details><summary>%s</summary>" % REJECTED_SUMMARY
DETAILS_CLOSE = "</details>"
PENDING_DECISION = "pending-decision"
PENDING_MEASUREMENT = "pending-measurement"
DECIDED = "decided"
NO_OP = "no-op"
MEASURED = "measured"
RULED_STATUSES = frozenset({DECIDED, NO_OP})
LEAVE_AS_IS = "leave as-is"
MAX_TEXT = 1000

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")
ITEM_NUMBER_RE = re.compile(r"^(\d+)\.(?:\s|$)")
OPTIONS_LINE_RE = re.compile(r"^- \*\*Options\*\*\s*:")
COMMAND_LINE_RE = re.compile(r"^\s*[-*]\s+\*\*Command\*\*\s*:")
RECOMMENDATION_LINE_RE = re.compile(r"^\s*[-*]\s+\*\*Recommendation\*\*\s*:")
OPTION_BULLET_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*(.*)$")
RECOMMENDED_MARK_RE = re.compile(r"\(\s*recommended\b[^)]*\)", re.IGNORECASE)
DETAILS_TAG_RE = re.compile(r"<(/?details\b)", re.IGNORECASE)
LEADING_FIELD_RE = re.compile(r"^(STATUS|DECISION)\s*:", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*[-*]\s+\*\*STATUS\*\*\s*:\s*(.*?)\s*$")
DECISION_RE = re.compile(r"^\s*[-*]\s+\*\*DECISION\*\*\s*:\s*(.*?)\s*$")


class _DigestProtocol(Protocol):
    """Protocol for the precedent-digest.py module's helpers used by review-ruling.py.

    Do not rename these methods in precedent-digest.py; review-ruling.py depends on them.
    """

    def _norm(self, text: str) -> str:
        """Normalize option text for comparison."""
        ...

    def _fold(self, text: str) -> str:
        """Fold option text to lowercase for case-insensitive matching."""
        ...

    def _option_key(self, label: str) -> str:
        """Extract the canonical option key from a label."""
        ...

    def _review_options(self, lines: List[str]) -> Tuple[List[str], ...]:
        """Parse review options from lines."""
        ...


class Refusal(Exception):
    """A named refusal: exit 1, one stderr line, file untouched."""


class Conflict(Exception):
    """A conflicting prior ruling: exit 3, file untouched."""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports usage errors as named refusals (exit 1), never exit 2."""

    def error(self, message: str) -> NoReturn:
        _stderr("review-ruling: refused: %s" % message)
        raise SystemExit(EXIT_REFUSED)


@dataclasses.dataclass(frozen=True)
class _Item:
    """Line indices (into the plan's split lines) for one located item."""

    heading: int
    end: int
    anchor: int
    status: int
    decision: int
    status_value: ItemStatus
    decision_value: str


def _stdout(message: str) -> None:
    sys.stdout.write(message + "\n")


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="review-ruling.py", description="Record an expert-review ruling in claude-action-plan.md.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    decide = sub.add_parser("decide", help="record a Needs you ruling")
    decide.add_argument("--review-dir", required=True)
    decide.add_argument("--item", required=True, type=int)
    answer = decide.add_mutually_exclusive_group(required=True)
    answer.add_argument("--option")
    answer.add_argument("--custom")
    decide.add_argument("--reasoning")
    decide.add_argument("--mode", choices=("local", "pr"), default="local")

    measure = sub.add_parser("measure", help="record a Needs measurement result")
    measure.add_argument("--review-dir", required=True)
    measure.add_argument("--item", required=True, type=int)
    measure.add_argument("--result", required=True)
    measure.add_argument("--mode", choices=("local", "pr"), default="local")
    return parser


def _load_digest() -> "_DigestProtocol":
    """Import precedent-digest.py from this script's own resolved directory. No silent fallback."""
    target = os.path.join(os.path.dirname(os.path.realpath(__file__)), "precedent-digest.py")
    try:
        spec = importlib.util.spec_from_file_location("precedent_digest", target)
        if spec is None or spec.loader is None:
            raise ImportError("no module loader for %s" % target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        raise Refusal("cannot load precedent-digest.py for option matching (%s: %s)" % (type(exc).__name__, exc))
    return module  # type: ignore[return-value]


def _require_local(mode: str) -> None:
    if mode != "local":
        raise Refusal("--mode pr is not recorded (ADR-0009: never write to a repo you do not own)")


def _item_number(value: int) -> int:
    if value < 1:
        raise Refusal("--item must be a positive integer")
    return value


def _resolve_review_dir(raw: str) -> str:
    """Realpath of the review dir, which must sit under Path.home()/.claude/reviews."""
    if not raw:
        raise Refusal("--review-dir is empty")
    if ".." in re.split(r"[\\/]", raw):
        raise Refusal("--review-dir may not contain '..' segments")
    if not os.path.isdir(raw):
        raise Refusal("review dir is not a directory")
    real = os.path.realpath(raw)
    root = os.path.realpath(str(Path.home() / ".claude" / "reviews"))
    try:
        inside = os.path.commonpath([real, root]) == root
    except ValueError:
        inside = False
    if not inside or real == root:
        raise Refusal("review dir resolves outside the reviews root %s" % root)
    return real


def _plan_path(review: str) -> str:
    path = os.path.join(review, ACTION_PLAN)
    try:
        info = os.lstat(path)
    except OSError:
        raise Refusal("%s not found in the review dir" % ACTION_PLAN)
    if stat.S_ISLNK(info.st_mode):
        raise Refusal("%s is a symlink; refusing to write through it" % ACTION_PLAN)
    if not stat.S_ISREG(info.st_mode):
        raise Refusal("%s is not a regular file" % ACTION_PLAN)
    return path


def _read_plan(path: str) -> str:
    """Read the plan. O_NOFOLLOW makes the open refuse a symlink swapped in after _plan_path's lstat."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise Refusal("%s cannot be opened without following a symlink" % ACTION_PLAN)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("%s is not valid UTF-8" % ACTION_PLAN)


def _atomic_write(path: str, text: str) -> None:
    """Write a temp file beside the target, then os.replace it over the target."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude-action-plan.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _clean(value: str) -> str:
    """Reduce free text to one bounded line that cannot forge a field, heading, fence or fold."""
    pieces = [" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in value]
    text = " ".join("".join(pieces).split())
    text = text.replace("`", "'")
    text = DETAILS_TAG_RE.sub(r"&lt;\1", text)
    if text.startswith("#"):
        text = "&#35;" + text[1:]
    match = LEADING_FIELD_RE.match(text)
    if match is not None:
        colon = match.end() - 1
        text = text[:colon] + "&#58;" + text[colon + 1:]
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 3].rstrip() + "..."
    return text


def _escape_details(line: str) -> str:
    return DETAILS_TAG_RE.sub(r"&lt;\1", line)


def _plain(text: str) -> str:
    return " ".join(text.split()).lower()


def _answer_matches(existing: str, answer: str) -> bool:
    """True when a recorded DECISION is this answer: equal, or the answer followed by reasoning."""
    have = _plain(existing)
    want = _plain(answer)
    return have == want or have.startswith(want + " —")


def _headings(lines: List[str]) -> List[Tuple[int, int, str]]:
    """(index, level, title) for every heading outside a fenced code block (``` or ~~~, CommonMark-style)."""
    found: List[Tuple[int, int, str]] = []
    fence = ""  # the opening fence run while inside a fenced block, else ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if fence:
            # A closing fence uses the same character, is at least as long, and has no info string.
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
            continue
        opened = FENCE_OPEN_RE.match(line)
        if opened is not None:
            fence = opened.group(1)
            continue
        match = HEADING_RE.match(line)
        if match is not None:
            found.append((index, len(match.group(1)), match.group(2)))
    return found


def _section_end(headings: List[Tuple[int, int, str]], index: int, level: int, limit: int) -> int:
    """Index of the next heading at this level or higher after index, capped at limit."""
    for later, later_level, _ in headings:
        if later > index and later_level <= level:
            return min(later, limit)
    return limit


def _section_span(
    lines: List[str],
    headings: List[Tuple[int, int, str]],
    title_re: "re.Pattern[str]",
    name: str,
) -> Tuple[int, int]:
    """(start, end) line indices of the single level-2 section whose title matches."""
    spans: List[Tuple[int, int]] = []
    for index, level, title in headings:
        if level == 2 and title_re.match(title.strip()):
            spans.append((index, _section_end(headings, index, 2, len(lines))))
    if not spans:
        raise Refusal("no '%s' section in %s" % (name, ACTION_PLAN))
    if len(spans) > 1:
        raise Refusal("more than one '%s' section in %s" % (name, ACTION_PLAN))
    return spans[0]


def _single(lines: List[str], lo: int, hi: int, pattern: "re.Pattern[str]", what: str) -> int:
    found = [index for index in range(lo, hi) if pattern.match(lines[index].rstrip("\r"))]
    if len(found) != 1:
        raise Refusal("item field %s must appear exactly once (found %d)" % (what, len(found)))
    return found[0]


def _locate(
    lines: List[str],
    headings: List[Tuple[int, int, str]],
    span: Tuple[int, int],
    number: int,
    anchor_re: "re.Pattern[str]",
    anchor_name: str,
    section: str,
) -> _Item:
    start, end = span
    hits: List[int] = []
    for index, level, title in headings:
        if level != 3 or not start < index < end:
            continue
        match = ITEM_NUMBER_RE.match(title.strip())
        if match is not None and int(match.group(1)) == number:
            hits.append(index)
    if not hits:
        raise Refusal("no item %d under '%s'" % (number, section))
    if len(hits) > 1:
        raise Refusal("item %d is not unique under '%s'" % (number, section))
    heading = hits[0]
    item_end = _section_end(headings, heading, 3, end)
    anchor = _single(lines, heading + 1, item_end, anchor_re, "- **%s**:" % anchor_name)
    status = _single(lines, heading + 1, item_end, STATUS_RE, "STATUS")
    decision = _single(lines, heading + 1, item_end, DECISION_RE, "DECISION")
    status_match = STATUS_RE.match(lines[status].rstrip("\r"))
    decision_match = DECISION_RE.match(lines[decision].rstrip("\r"))
    assert status_match is not None and decision_match is not None
    return _Item(
        heading=heading,
        end=item_end,
        anchor=anchor,
        status=status,
        decision=decision,
        status_value=status_match.group(1).strip().lower(),
        decision_value=decision_match.group(1),
    )


def _resolve_option(request: str, labels: List[str], digest: "_DigestProtocol") -> str:
    """The canonical label for a requested option: exact after normalising, else by option key."""
    wanted = digest._norm(request)  # do not rename: pinned by test_review_ruling_wiring.py
    if not wanted:
        raise Refusal("--option is empty")
    exact = [label for label in labels if digest._fold(label) == digest._fold(wanted)]  # do not rename
    keyed = [label for label in labels if digest._option_key(label) == digest._option_key(wanted)]  # do not rename
    pool = exact or keyed
    if not pool:
        raise Refusal("unknown option label: %s" % request)
    if len(pool) > 1:
        raise Refusal("option label is ambiguous: %s" % request)
    return pool[0]


def _split_groups(region: List[str], digest: "_DigestProtocol") -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """Nested option bullets (label, lines) in file order, plus any lines before the first bullet."""
    preamble: List[str] = []
    groups: List[Tuple[str, List[str]]] = []
    for line in region:
        bullet = OPTION_BULLET_RE.match(line.rstrip("\r"))
        if bullet is not None and line[:1].isspace():
            groups.append((digest._norm(bullet.group(1)), [line]))  # do not rename: pinned by test_review_ruling_wiring.py
        elif groups:
            groups[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, groups


def _trim(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _partition(
    lines: List[str], item: _Item, chosen: Optional[str], digest: "_DigestProtocol"
) -> Tuple[List[str], List[str], List[str]]:
    """(chosen option lines, Recommendation lines, rejected lines) from the Options region."""
    region = lines[item.anchor + 1 : item.status]
    recs = [line for line in region if RECOMMENDATION_LINE_RE.match(line.rstrip("\r"))]
    rest = [line for line in region if not RECOMMENDATION_LINE_RE.match(line.rstrip("\r"))]
    preamble, groups = _split_groups(rest, digest)
    chosen_lines: List[str] = []
    rejected: List[str] = list(preamble)
    picked = False
    for label, group_lines in groups:
        if chosen is not None and not picked and label == chosen:
            chosen_lines = _trim(group_lines)
            picked = True
        else:
            rejected.extend(_trim(group_lines))
            rejected.append("")
    if chosen is not None and not picked:
        raise Refusal("the chosen option is not among the item's option bullets")
    return chosen_lines, recs, _trim(rejected)


def _rationale(chosen_lines: List[str]) -> str:
    """The chosen option's own text after its label: what it does, with Pro and Con."""
    if not chosen_lines:
        return ""
    bullet = OPTION_BULLET_RE.match(chosen_lines[0].rstrip("\r"))
    if bullet is None:
        return ""
    rest = RECOMMENDED_MARK_RE.sub("", bullet.group(2)).strip()
    if rest.startswith(":"):
        rest = rest[1:].strip()
    return rest


def _rewrite_decision(
    lines: List[str],
    item: _Item,
    chosen_lines: List[str],
    rec_lines: List[str],
    rejected: List[str],
    status: str,
    decision: str,
) -> List[str]:
    fold: List[str] = []
    if rejected:
        fold = [DETAILS_OPEN, ""] + [_escape_details(line) for line in rejected] + ["", DETAILS_CLOSE]
    return (
        lines[: item.anchor + 1]
        + chosen_lines
        + rec_lines
        + ["- **STATUS**: %s" % status]
        + lines[item.status + 1 : item.decision]
        + ["- **DECISION**: %s" % _escape_details(decision)]
        + fold
        + lines[item.decision + 1 :]
    )


def _load_plan_data(args: argparse.Namespace) -> Tuple[int, str, List[str], List[Tuple[int, int, str]]]:
    """Shared preamble for both decide and measure: load and parse the plan file.

    Returns (number, path, lines, headings).
    Raises Refusal on any containment or format error.
    """
    _require_local(args.mode)
    number = _item_number(args.item)
    review = _resolve_review_dir(args.review_dir)
    path = _plan_path(review)
    lines = _read_plan(path).split("\n")
    headings = _headings(lines)
    return number, path, lines, headings


def _decide(args: argparse.Namespace) -> int:
    number, path, lines, headings = _load_plan_data(args)
    span = _section_span(lines, headings, NEEDS_YOU_TITLE, "Needs you")
    item = _locate(lines, headings, span, number, OPTIONS_LINE_RE, "Options", "Needs you")
    digest = _load_digest()
    labels = digest._review_options(lines[item.heading + 1 : item.end])[0]  # do not rename: pinned by test_review_ruling_wiring.py

    if args.custom is not None:
        answer = _clean(args.custom)
        if not answer:
            raise Refusal("--custom is empty after sanitising")
        chosen: Optional[str] = None
    else:
        answer = _resolve_option(args.option, labels, digest)
        chosen = answer

    if item.status_value in RULED_STATUSES:
        same = _answer_matches(item.decision_value, answer)
        if chosen is not None and not same:
            same = digest._option_key(item.decision_value) == digest._option_key(answer)
        if same:
            _stdout("already recorded: item %d (STATUS %s, no change)" % (number, item.status_value))
            return EXIT_OK
        raise Conflict(item.decision_value)

    if item.status_value != PENDING_DECISION:
        raise Refusal("item %d has STATUS %s, not %s" % (number, item.status_value, PENDING_DECISION))
    if not item.anchor < item.status < item.decision:
        raise Refusal("item %d fields are out of order" % number)

    status = NO_OP if chosen is not None and LEAVE_AS_IS in digest._fold(answer) else DECIDED
    chosen_lines, rec_lines, rejected = _partition(lines, item, chosen, digest)
    if args.reasoning is not None:
        reason = _clean(args.reasoning)
    elif chosen is not None:
        reason = _clean(_rationale(chosen_lines))
    else:
        reason = ""
    decision = answer + (" — " + reason if reason else "")
    new_lines = _rewrite_decision(lines, item, chosen_lines, rec_lines, rejected, status, decision)
    _atomic_write(path, "\n".join(new_lines))
    _stdout("recorded: item %d (STATUS %s)" % (number, status))
    return EXIT_OK


def _measure(args: argparse.Namespace) -> int:
    number, path, lines, headings = _load_plan_data(args)
    span = _section_span(lines, headings, NEEDS_MEASUREMENT_TITLE, "Needs measurement")
    item = _locate(lines, headings, span, number, COMMAND_LINE_RE, "Command", "Needs measurement")
    result = _clean(args.result)
    if not result:
        raise Refusal("--result is empty after sanitising")

    if item.status_value == MEASURED:
        if _answer_matches(item.decision_value, result):
            _stdout("already recorded: item %d (STATUS measured, no change)" % number)
            return EXIT_OK
        raise Conflict(item.decision_value)
    if item.status_value != PENDING_MEASUREMENT:
        raise Refusal("item %d has STATUS %s, not %s" % (number, item.status_value, PENDING_MEASUREMENT))

    new_lines = list(lines)
    new_lines[item.status] = "- **STATUS**: %s" % MEASURED
    new_lines[item.decision] = "- **DECISION**: %s" % _escape_details(result)
    _atomic_write(path, "\n".join(new_lines))
    _stdout("recorded: item %d (STATUS measured)" % number)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    handlers: Callable[[argparse.Namespace], int] = _decide if args.command == "decide" else _measure
    try:
        return handlers(args)
    except Refusal as exc:
        _stderr("review-ruling: refused: %s" % exc)
        return EXIT_REFUSED
    except Conflict as exc:
        _stderr("review-ruling: conflicting prior ruling: %s" % exc)
        return EXIT_CONFLICT
    except Exception as exc:  # keep the failure named; never a traceback on the data path
        _stderr("review-ruling: internal error (%s: %s)" % (type(exc).__name__, exc))
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
