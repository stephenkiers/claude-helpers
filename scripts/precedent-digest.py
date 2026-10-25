#!/usr/bin/env python3
"""precedent-digest.py: one deterministic digest of a repo's prior human rulings (#292).

Usage:
    precedent-digest.py <repo-root-or-owner/name> [--format jsonl|md]
        [--match <questions.json> [--top N]] [--stats] [--out FILE]

Reads three on-disk sources under $HOME/.claude for one repo and writes one
bounded digest of the human rulings found there:

  - plan-sessions/<key>/*/decisions.md                  (source "plan-session")
  - flows/<key>/*/questions|answers/<step>-NN.json      (source "flow")
  - reviews/<key>/*/claude-action-plan.md               (source "review")

Model-free and read-only. The only file it ever writes is the one named by --out.

Entry schema (the contract with #293, Pat). Keys, in this exact order:
  id           str    sha256(path + "\\n" + question + "\\n" + header) hex digest, first 16 chars.
  source       str    open enum. Today: "plan-session", "flow", "review".
                      Consumers MUST tolerate unknown source values.
  repo_key     str    hyphen spelling, "owner-name".
  date         str    ISO 8601 "YYYY-MM-DDTHH:MM:SS", naive local time.
  ref          int or null. From a leading "<digits>-" in the run dir name.
  path         str    absolute path built from on-disk names.
  header       str    plan: "Q<n> <header>"; review: the "### N. Title" heading line;
                      flow: the question header.
  question     str    full question text if present, otherwise the header.
  options      list[str]. May be empty.
  recommended  str or null. Never raw prose.
  answer       str    always a string: the chosen option label, or the raw text when
                      no label maps. List answers are joined with "; ".
  reason       str or null.
  deviated     bool or null. null when recommended is unknown; otherwise true when
                      the normalized answer differs from the normalized recommended.

Ordering: date desc, then path asc, header asc, id asc. The date comes from a
YYYYMMDDTHHMMSS token in the run dir name. When there is none it falls back to the
source file's mtime, rendered in local time. mtime is a hidden input: a corpus
whose dir names carry no timestamp can order differently on another checkout.

Bounded outputs. Without --match, output is capped at the 300 most recent entries.
With --match, output is bounded by --top N (default 60) and the 300 cap does not
apply; scoring runs over the full deduplicated corpus. Both bounds are volume
prefilters, not rulings. Absence from the digest does not mean "never asked". This
follows epic #291's information-only rule and ADR-0005's "lens not law".

Provenance. This reader is proposed under epic #291 as the first cross-run reader of
claude-action-plan.md. It relies on ADR-0007's chore/29 escape hatch, and that hatch
needs an ADR update first. The ADR-0007 amendment and ADR-0024 are owned by #293 and
have not landed, so this reader does not yet exercise the escape hatch.

Consumer boundary: read only by Pat (#293) and humans; never by reviewer prompts or
Triage (preserves ADR-0002 blind-first).

Exit and stderr contract:
  - Exit 0, with exactly one stderr summary line, on every data-side outcome: an
    empty corpus, missing dirs, an unresolvable repo key, skipped files.
  - Exit 1, with one stderr line and no traceback, for internal errors: bugs,
    uncaught exceptions. The --out file is left unwritten or incomplete on exit 1.
  - Exit 2, with one stderr line and no traceback, for invocation errors: bad
    arguments, --top < 1, a missing or malformed --match file, an unwritable --out.

Provenance note: This reader is proposed under epic #291 as the first cross-run
reader of claude-action-plan.md. The ADR-0007 amendment (#293) must explicitly
cover all three sources (plan-session, flow, review), including "Needs you" items
from review sections. The Pat wiring (#294, #296) must not wire this reader
until the amendment lands.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Callable, Dict, List, NoReturn, Optional, Sequence, Set, Tuple, TypedDict

PINNED_KEYS = (
    "id", "source", "repo_key", "date", "ref", "path", "header", "question",
    "options", "recommended", "answer", "reason", "deviated",
)
SOURCES = ("plan-session", "flow", "review")
SKIP_BUCKETS = ("unreadable", "malformed", "unanswered", "orphan_answer", "linked_skipped")
SOURCE_ROOTS = (("plan-sessions", "plan-session"), ("flows", "flow"), ("reviews", "review"))
PLAN_DOC = "decisions.md"
REVIEW_DOC = "claude-action-plan.md"

# Ruling markers, held as named constants (Q4, Q3, Q5).
RULING_STATUSES = frozenset({"decided", "no-op"})
RECOMMENDED_RE = re.compile(r"\(\s*recommended\b[^)]*\)", re.IGNORECASE)
BRIEF_HEADING = "Decisions (asked at checkpoint)"
CHECKPOINT_HEADINGS = (
    "Checkpoint results",
    "Checkpoint results (user answers)",
    "Decisions (checkpoint results)",
    "User answers",
)
EXCLUDED_HEADINGS = ("Expert round-up",)
REVIEW_SECTION = "Needs you"

CAP = 300
DEFAULT_TOP = 60
TOP_TOKENS = 20
ANSWER_SEP = "; "
FIELD_WEIGHTS = {"header": 3, "question": 2, "options": 1}
MIN_TOKEN_LEN = 3
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "not", "but", "all",
    "any", "use", "should", "would", "could", "how", "what", "which", "when", "who",
    "its", "into", "via", "one", "two", "has", "have", "was", "were", "will", "can",
    "our", "out", "you", "your", "than", "then", "there", "their", "they", "them",
    "also", "only", "just", "more", "some", "such", "each", "does", "did", "had",
})

KEY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")  # matches owner/name
SCP_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:(.+)$")  # matches git@host:path
URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+/(.+)$")  # matches scheme://host/path
GIT_TIMEOUT_SECS = 5
STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})")  # matches YYYYMMDDTHHMMSS
LEADING_NUM_RE = re.compile(r"^(\d+)-")  # matches leading digits before hyphen (e.g., "9-run-name")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")  # matches markdown headings
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")  # matches bullets/list markers
BRIEF_START_RE = re.compile(r"^#{3,6}\s+Q(\d+)\.?\s*(.*?)\s*$")  # matches "#### Q1. Header"
OPTION_LABEL_RE = re.compile(r"^([A-Za-z])(?:[.)]\s*(.*)|$)")  # matches "A. Option text" or "A"
RECOMMENDED_LINE_RE = re.compile(r"^\**\s*recommended\**\s*:\s*(.*)$", re.IGNORECASE)  # **recommended**: value
LETTER_RE = re.compile(r"^\**\s*([A-Za-z])(?![A-Za-z])")  # lone letter not followed by another letter
REVIEW_HEAD_RE = re.compile(r"^\**\s*([A-Za-z0-9]+)\b")  # letter/digit(s) at start of value
REVIEW_OPTION_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*")  # matches "- **Option text**"
REVIEW_OPTIONS_START_RE = re.compile(r"^- \*\*Options\*\*\s*:")  # options section marker
REVIEW_KEY_SPLIT_RE = re.compile(r"\s*(?:—|:)\s*|\s+-\s*|\s*-\s+")  # splits on em-dash, colon, or hyphen
REVIEW_LEAD_SPLIT_RE = re.compile(r"[,;.(]")  # splits on comma, semicolon, period, or paren
LEADING_LETTER_RE = re.compile(r"^([A-Za-z])(?=[.)\s—:]|$)")  # lone letter before specific punct or end


class _Usage(Exception):
    """An invocation error: exit 2, one stderr line, no traceback."""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports errors on one stderr line and exits 2."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        sys.stderr.write("precedent-digest: %s\n" % message)
        raise SystemExit(2)


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


class _RawRecord(TypedDict, total=False):
    """Intermediate record format before entry construction."""
    header: str
    question: str
    options: List[str]
    recommended: Optional[str]
    answer: str
    reason: Optional[str]
    n: int  # for briefs
    recommended_key: Optional[str]  # for briefs


class _Entry(TypedDict):
    """Complete entry in pinned key order."""
    id: str
    source: str
    repo_key: str
    date: str
    ref: Optional[int]
    path: str
    header: str
    question: str
    options: List[str]
    recommended: Optional[str]
    answer: str
    reason: Optional[str]
    deviated: Optional[bool]


# ---------------------------------------------------------------------------
# Normalization and entry construction
# ---------------------------------------------------------------------------

def _norm(label: str) -> str:
    """Strip a (Recommended ...) marker, trim, and collapse internal whitespace."""
    return " ".join(RECOMMENDED_RE.sub("", label or "").split())


def _has_marker(text: str) -> bool:
    return RECOMMENDED_RE.search(text) is not None


def _fold(text: str) -> str:
    """Normalize text for comparison: lowercase (not casefold, to avoid cross-attribute issues).

    casefold() would map ß→ss, breaking case-insensitive repo-key matching across attributes;
    lower() is safer for cross-attribute consistency.
    """
    return text.lower()


def _option_key(label: str) -> str:
    """The text before the first em dash, colon or hyphen separator, folded."""
    head = REVIEW_KEY_SPLIT_RE.split(label, maxsplit=1)[0]
    return _fold(head.strip())


def _entry_id(path: str, question: str, header: str) -> str:
    """Unique entry ID from path, question, and header to disambiguate multiple rulings."""
    return hashlib.sha256((path + "\n" + question + "\n" + header).encode("utf-8")).hexdigest()[:16]


def _deviated(answer: str, recommended: Optional[str]) -> Optional[bool]:
    if recommended is None:
        return None
    return _norm(answer) != _norm(recommended)


def _make_entry(
    source: str,
    repo_key: str,
    path: str,
    date: str,
    ref: Optional[int],
    rec: Dict[str, Any],
) -> Dict[str, Any]:
    """Build one entry in the pinned key order. The only place entries are built."""
    question = rec["question"]
    header = rec["header"]
    return {
        "id": _entry_id(path, question, header),
        "source": source,
        "repo_key": repo_key,
        "date": date,
        "ref": ref,
        "path": path,
        "header": header,
        "question": question,
        "options": list(rec["options"]),
        "recommended": rec["recommended"],
        "answer": rec["answer"],
        "reason": rec["reason"],
        "deviated": _deviated(rec["answer"], rec["recommended"]),
    }


def _date_for(dir_name: str, file_path: str) -> str:
    match = STAMP_RE.search(dir_name)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return "%s-%s-%sT%s:%s:%s" % (year, month, day, hour, minute, second)
    mtime = os.path.getmtime(file_path)
    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")


def _ref_for(dir_name: str) -> Optional[int]:
    """Extract leading digits from a run directory name, or None if not present."""
    match = LEADING_NUM_RE.match(dir_name)
    return int(match.group(1)) if match else None


def _text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _option_strings(raw: Any) -> List[str]:
    """Raw option labels, from dicts with a 'label' or from plain strings."""
    if not isinstance(raw, list):
        return []
    labels: List[str] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("label"), str):
            labels.append(item["label"])
        elif isinstance(item, str):
            labels.append(item)
    return labels


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_flatten(v) for v in value]
    elif isinstance(value, dict):
        parts = [_flatten(v) for v in value.values()]
    else:
        return ""
    return ANSWER_SEP.join(p for p in parts if p)


def _join_answer(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list) and value and all(
        isinstance(v, str) and v.strip() for v in value
    ):
        return ANSWER_SEP.join(v.strip() for v in value)
    return None


def _strip_bullet(line: str) -> str:
    return BULLET_RE.sub("", line).strip()


def _tokens(text: str) -> List[str]:
    return [
        tok for tok in TOKEN_RE.findall(text.lower())
        if len(tok) >= MIN_TOKEN_LEN and tok not in STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Repo-key resolution
# ---------------------------------------------------------------------------

def resolve_key(arg: str) -> Tuple[Optional[str], str]:
    """Return ("owner/name", "") or (None, reason) for a positional argument.

    A literal owner/name that is not an existing directory is taken as the key.
    Anything else is a repo root: its origin remote is read with git, with stderr
    captured so git text never leaks.

    Precedence: if arg is a directory (absolute or relative to cwd), use it as
    a git root; otherwise treat arg as a literal "owner/name" key. Relative
    paths depend on the current working directory.
    """
    if KEY_RE.match(arg) and not os.path.isdir(arg):
        return arg, ""
    try:
        proc = subprocess.run(
            ["git", "-C", arg, "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "git unavailable or timed out"
    if proc.returncode != 0:
        return None, "no origin remote in a git repository"
    url = proc.stdout.decode("utf-8", errors="replace").strip()
    key = _key_from_url(url)
    if key is None:
        return None, "unrecognized origin URL"
    return key, ""


def _key_from_url(url: str) -> Optional[str]:
    path: Optional[str] = None
    scp = SCP_RE.match(url)
    if scp:
        path = scp.group(1)
    else:
        scheme = URL_RE.match(url)
        if scheme:
            path = scheme.group(1)
    if path is None:
        return None
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) < 2:
        return None
    return "%s/%s" % (segments[-2], segments[-1])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _subdirs(path: str) -> List[Tuple[str, str]]:
    """Sorted (path, name) pairs for the directory children of path."""
    try:
        with os.scandir(path) as it:
            found = [(entry.path, entry.name) for entry in it if entry.is_dir()]
    except OSError:
        return []
    return sorted(found, key=lambda item: item[1])


def _json_names(path: str) -> List[str]:
    try:
        with os.scandir(path) as it:
            names = [e.name for e in it if e.name.endswith(".json") and e.is_file()]
    except OSError:
        return []
    return sorted(names)


def discover(pair: Tuple[str, str], home: str) -> Dict[str, Any]:
    """List matched and legacy dirs under the three roots. Reads no file contents.

    Both spellings (owner-name, owner_name) match case-insensitively. Matched dirs
    are deduplicated by realpath. Legacy dirs (name-only) are information only.
    Flat review dirs, which hold claude-action-plan.md directly, carry no repo key.
    repo_key is best-effort normalized to "owner-name" (hyphen, lowercase), but
    on-disk dirs may use different casings or underscores.
    """
    owner, name = pair
    hyphen = _fold("%s-%s" % (owner, name))
    wanted = {hyphen, _fold("%s_%s" % (owner, name))}
    info: Dict[str, Any] = {
        "repo_key": hyphen,
        "dirs": {source: [] for source in SOURCES},
        "legacy": [],
        "unkeyed_review_dirs": 0,
    }
    for root_name, source in SOURCE_ROOTS:
        root = os.path.join(home, ".claude", root_name)
        seen_real: Set[str] = set()
        for child_path, child_name in _subdirs(root):
            if source == "review" and os.path.isfile(os.path.join(child_path, REVIEW_DOC)):
                info["unkeyed_review_dirs"] += 1
                continue
            folded = _fold(child_name)
            if folded in wanted:
                real = os.path.realpath(child_path)
                if real not in seen_real:
                    seen_real.add(real)
                    info["dirs"][source].append(child_path)
            elif folded == _fold(name):
                info["legacy"].append("%s/%s" % (root_name, child_name))
    return info


# ---------------------------------------------------------------------------
# Markdown section helpers (shared by the plan-session and review parsers)
# ---------------------------------------------------------------------------

def _heading_of(line: str) -> Optional[Tuple[int, str]]:
    match = HEADING_RE.match(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _is_excluded_heading(title: str) -> bool:
    folded = _fold(title)
    return any(folded.startswith(_fold(item)) for item in EXCLUDED_HEADINGS)


def _title_is(target: str) -> Callable[[str], bool]:
    return lambda title: _fold(title.strip()) == _fold(target)


def _title_in(targets: Sequence[str]) -> Callable[[str], bool]:
    folded = {_fold(item) for item in targets}
    return lambda title: _fold(title.strip()) in folded


def _section_lines(lines: List[str], wanted: Callable[[str], bool]) -> List[str]:
    """Body lines of every level-1/2 section whose title matches, nested headings kept.

    A section ends at the next heading of the same or a higher level. Excluded
    headings (and everything nested under them) are never returned. Headings
    inside fenced code blocks do not end sections.
    """
    out: List[str] = []
    index = 0
    total = len(lines)
    while index < total:
        head = _heading_of(lines[index])
        if head is None or head[0] > 2 or not wanted(head[1]):
            index += 1
            continue
        level = head[0]
        skip_level: Optional[int] = None
        in_fence = False
        index += 1
        while index < total:
            line = lines[index]
            # Toggle fence state on triple-backtick.
            if line.strip().startswith("```"):
                in_fence = not in_fence
            sub = _heading_of(line) if not in_fence else None
            if sub is not None:
                if sub[0] <= level:
                    break
                if skip_level is not None and sub[0] <= skip_level:
                    skip_level = None
                if skip_level is None and _is_excluded_heading(sub[1]):
                    skip_level = sub[0]
            if skip_level is None:
                out.append(line)
            index += 1
    return out


# ---------------------------------------------------------------------------
# Per-source parsers: text in, raw records and counts out
# ---------------------------------------------------------------------------

def _answer_maps(answers_text: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(answers, annotations). Answers is None when the file is absent or unparseable."""
    if answers_text is None:
        return None, {}
    try:
        data = json.loads(answers_text)
    except ValueError:
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    if "answers" in data:
        answers = data.get("answers")
        if not isinstance(answers, dict):
            return None, {}
        annotations = data.get("annotations")
    else:
        answers = data
        annotations = None
    return answers, (annotations if isinstance(annotations, dict) else {})


def _lookup_answer(
    answers: Optional[Dict[str, Any]],
    index: int,
    qid: Optional[str],
    question: str,
) -> Tuple[Optional[str], Any]:
    """Find the answer key: id, else q<n> when id is absent, else the question text."""
    if answers is None:
        return None, None
    if qid is not None:
        if qid in answers:
            return qid, answers[qid]
    else:
        legacy_key = "q%d" % (index + 1)
        if legacy_key in answers:
            return legacy_key, answers[legacy_key]
    if question in answers:
        return question, answers[question]
    return None, None


def parse_flow(questions_text: str, answers_text: Optional[str]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """One raw record per answered question. Raises ValueError on a malformed question file."""
    data = json.loads(questions_text)
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError("question file has no questions list")
    answers, annotations = _answer_maps(answers_text)
    records: List[Dict[str, Any]] = []
    counts = {"unanswered": 0, "malformed": 0}
    for index, item in enumerate(data["questions"]):
        if not isinstance(item, dict):
            counts["malformed"] += 1
            continue
        header = _text_of(item.get("header"))
        question = _text_of(item.get("question")) or header
        if not question:
            counts["malformed"] += 1
            continue
        qid = item["id"] if isinstance(item.get("id"), str) else None
        raw_labels = _option_strings(item.get("options"))
        options: List[str] = []
        recommended: Optional[str] = None
        for raw in raw_labels:
            label = _norm(raw)
            if not label:
                continue
            if label not in options:
                options.append(label)
            if recommended is None and _has_marker(raw):
                recommended = label
        key, raw_answer = _lookup_answer(answers, index, qid, question)
        answer = _join_answer(raw_answer)
        if answer is None:
            counts["unanswered"] += 1
            continue
        if _norm(answer) in options:
            answer = _norm(answer)
        reason = None
        for note_key in (qid, key):
            if note_key is not None and note_key in annotations:
                reason = _flatten(annotations[note_key]) or None
                if reason:
                    break
        records.append({
            "header": header or question,
            "question": question,
            "options": options,
            "recommended": recommended,
            "answer": answer,
            "reason": reason,
        })
    return records, counts


def flow_context_path(questions_text: str) -> Optional[str]:
    """The context_path of a flow question file, or None. Raises ValueError on bad JSON."""
    data = json.loads(questions_text)
    if isinstance(data, dict):
        ctx = data.get("context_path")
        if isinstance(ctx, str) and ctx.strip():
            return ctx.strip()
    return None


def _table_option(stripped: str) -> Optional[Tuple[str, str]]:
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if not cells:
        return None
    first = re.sub(r"[*`]", "", cells[0]).strip()
    match = OPTION_LABEL_RE.match(first)
    if match is None:
        return None
    letter = match.group(1).upper()
    rest = (match.group(2) or "").strip()
    label = "%s. %s" % (letter, rest) if rest else letter
    return letter, _norm(label)


def _parse_briefs(lines: List[str]) -> List[Dict[str, Any]]:
    blocks: List[Tuple[int, str, List[str]]] = []
    current: Optional[Tuple[int, str, List[str]]] = None
    for line in lines:
        start = BRIEF_START_RE.match(line)
        if start:
            current = (int(start.group(1)), start.group(2), [])
            blocks.append(current)
            continue
        if _heading_of(line) is not None:
            current = None
            continue
        if current is not None:
            current[2].append(line)
    briefs: List[Dict[str, Any]] = []
    for number, title, body in blocks:
        options: List[Tuple[str, str]] = []
        question: Optional[str] = None
        recommended_key: Optional[str] = None
        for line in body:
            stripped = line.strip()
            if stripped.startswith("|"):
                option = _table_option(stripped)
                if option is not None:
                    options.append(option)
                continue
            if question is None and stripped and not stripped.startswith(("**", "-", "<", ">")):
                question = stripped
            if recommended_key is None:
                rec = RECOMMENDED_LINE_RE.match(stripped)
                letter = LETTER_RE.match(rec.group(1)) if rec else None
                if letter:
                    recommended_key = letter.group(1).upper()
        briefs.append({
            "n": number,
            "header": ("Q%d %s" % (number, title)).strip(),
            "question": question or title or ("Q%d" % number),
            "options": options,
            "recommended_key": recommended_key,
        })
    return briefs


def _brief_answer(brief: Dict[str, Any], checkpoint_lines: List[str]) -> Optional[str]:
    """The answer for one brief, or None when no result line exists for it."""
    number = brief["n"]
    result_re = re.compile(r"\bQ%d\b(?:\s*\([^)]*\))?\s*[=;:]" % number)
    map_re = re.compile(
        r"\bQ%d\b(?:\s*\([^)]*\))?\s*=\s*([A-Za-z])(?![A-Za-z0-9])"
        r"(?![ \t]*(?:[+&/][ \t]*[A-Za-z]|and[ \t]+[A-Za-z]"
        r"|or[ \t]+[A-Za-z](?![A-Za-z])"
        r"|[,;][ \t]*(?:(?:and|or)[ \t]+)?[A-Za-z](?![A-Za-z])))" % number
    )
    for line in checkpoint_lines:
        if not result_re.search(line):
            continue
        mapped = map_re.search(line)
        if mapped:
            letter = mapped.group(1).upper()
            for key, label in brief["options"]:
                if key == letter:
                    return label
        return _strip_bullet(line) or None
    return None


def parse_plan(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Briefs with their checkpoint results, else one blob for a results section."""
    lines = text.splitlines()
    counts = {"unanswered": 0, "malformed": 0}
    briefs = _parse_briefs(_section_lines(lines, _title_is(BRIEF_HEADING)))
    checkpoint_lines = _section_lines(lines, _title_in(CHECKPOINT_HEADINGS))
    records: List[Dict[str, Any]] = []
    if not briefs:
        blob = "\n".join(checkpoint_lines).strip()
        if blob:
            records.append({
                "header": "Checkpoint results",
                "question": "Checkpoint results",
                "options": [],
                "recommended": None,
                "answer": blob,
                "reason": None,
            })
        return records, counts
    for brief in briefs:
        answer = _brief_answer(brief, checkpoint_lines)
        if answer is None:
            counts["unanswered"] += 1
            continue
        labels = [label for _, label in brief["options"]]
        recommended = None
        for key, label in brief["options"]:
            if key == brief["recommended_key"]:
                recommended = label
                break
        records.append({
            "header": brief["header"],
            "question": brief["question"],
            "options": labels,
            "recommended": recommended,
            "answer": answer,
            "reason": None,
        })
    return records, counts


def plan_headings(text: str) -> List[str]:
    """Distinct level-1/2 heading texts in one decisions.md, for the --stats census."""
    found: Set[str] = set()
    for line in text.splitlines():
        head = _heading_of(line)
        if head is not None and head[0] <= 2:
            found.add(head[1])
    return sorted(found)


def _split_items(lines: List[str]) -> List[Tuple[str, List[str]]]:
    items: List[Tuple[str, List[str]]] = []
    current: Optional[Tuple[str, List[str]]] = None
    for line in lines:
        head = _heading_of(line)
        if head is not None and head[0] == 3:
            current = ("### %s" % head[1], [])
            items.append(current)
        elif head is not None and head[0] < 3:
            current = None
        elif current is not None:
            current[1].append(line)
    return items


def _field(body: List[str], name: str) -> Optional[str]:
    pattern = re.compile(r"^\s*[-*]\s+\*\*%s\*\*\s*:\s*(.*?)\s*$" % re.escape(name))
    for line in body:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _review_options(body: List[str]) -> Tuple[List[str], Optional[str]]:
    """Option labels from the Options bullets and <details> items, in file order."""
    labels: List[str] = []
    marked: Optional[str] = None
    in_options = False
    in_details = False
    for line in body:
        if REVIEW_OPTIONS_START_RE.match(line):
            in_options = True
            continue
        if line.startswith("- "):
            in_options = False
        if "<details" in line:
            in_details = True
        bullet = REVIEW_OPTION_RE.match(line)
        nested = in_options and line[:1].isspace()
        if bullet and (nested or in_details):
            label = _norm(bullet.group(1))
            if label:
                if label not in labels:
                    labels.append(label)
                if marked is None and _has_marker(line):
                    marked = label
        if "</details>" in line:
            in_details = False
    return labels, marked


def _review_recommended(value: Optional[str], labels: List[str], marked: Optional[str]) -> Optional[str]:
    if value:
        head = REVIEW_HEAD_RE.match(value)
        if head:
            wanted_key = _fold(head.group(1))
            # Only treat as a letter option if it's a single letter followed by
            # one of: period, close paren, space, em dash, end of string.
            if len(wanted_key) == 1 and (
                len(value) == 1 or value[1:2] in ".): —" or value[1] in " .);—"
            ):
                for label in labels:
                    lead_match = LEADING_LETTER_RE.match(label)
                    if lead_match is not None and _fold(lead_match.group(1)) == wanted_key:
                        return label
            # Try matching as a multi-letter key (from option label).
            for label in labels:
                if _option_key(label) == wanted_key:
                    return label
        lead = _fold(_norm(REVIEW_LEAD_SPLIT_RE.split(value, maxsplit=1)[0]))
        for label in labels:
            if _fold(_norm(label)) == lead or _option_key(label) == lead:
                return label
    return marked


def _review_answer(decision: str, labels: List[str]) -> str:
    key = _option_key(decision)
    folded = _fold(_norm(decision))
    for label in labels:
        if _option_key(label) == key or _fold(_norm(label)) == folded:
            return label
    return decision.strip()


def parse_review(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Ruled items in the Needs-you section only (STATUS decided or no-op)."""
    lines = text.splitlines()
    counts = {"unanswered": 0, "malformed": 0}
    records: List[Dict[str, Any]] = []
    for header, body in _split_items(_section_lines(lines, _title_is(REVIEW_SECTION))):
        status = _field(body, "STATUS")
        if status is None or _fold(status.strip()) not in RULING_STATUSES:
            continue
        decision = _field(body, "DECISION")
        if not decision or decision.strip().startswith("_("):
            counts["unanswered"] += 1
            continue
        labels, marked = _review_options(body)
        finding = _field(body, "The finding")
        question = (finding or "").strip() or header
        recommended = _review_recommended(_field(body, "Recommendation"), labels, marked)
        records.append({
            "header": header,
            "question": question,
            "options": labels,
            "recommended": recommended,
            "answer": _review_answer(decision, labels),
            "reason": None,
        })
    return records, counts


# ---------------------------------------------------------------------------
# Collection, dedupe and ordering
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def _read_answer(path: str) -> Optional[str]:
    try:
        return _read_text(path)
    except (OSError, UnicodeDecodeError):
        return None


def _empty_collection(repo_key: str) -> Dict[str, Any]:
    return {
        "repo_key": repo_key,
        "entries": [],
        "skipped": {s: {b: 0 for b in SKIP_BUCKETS} for s in SOURCES},
        "census": {},
        "legacy": [],
        "unkeyed": 0,
        "matched": False,
    }


def collect(pair: Tuple[str, str], home: str) -> Dict[str, Any]:
    """Discover, read and parse every source for one repo. Never raises on bad data."""
    info = discover(pair, home)
    repo_key = info["repo_key"]
    result = _empty_collection(repo_key)
    skipped = result["skipped"]
    seen_real: Set[str] = set()
    entries: List[Dict[str, Any]] = []

    # Flows first: their context_path values drive the Step 5 exact-link dedupe.
    flow_items: List[Tuple[str, str, str, str, Optional[str]]] = []
    for keyed_dir in info["dirs"]["flow"]:
        for run_path, run_name in _subdirs(keyed_dir):
            qdir = os.path.join(run_path, "questions")
            adir = os.path.join(run_path, "answers")
            qnames = _json_names(qdir)
            anames = set(_json_names(adir))
            skipped["flow"]["orphan_answer"] += len(anames - set(qnames))
            for qname in qnames:
                qpath = os.path.join(qdir, qname)
                real = os.path.realpath(qpath)
                if real in seen_real:
                    continue
                seen_real.add(real)
                try:
                    qtext = _read_text(qpath)
                except (OSError, UnicodeDecodeError):
                    skipped["flow"]["unreadable"] += 1
                    continue
                atext = _read_answer(os.path.join(adir, qname)) if qname in anames else None
                flow_items.append((qpath, run_path, run_name, qtext, atext))

    # Collect context paths only from flows that produce entries (not all flows).
    context: Set[str] = set()
    for qpath, _run_path, run_name, qtext, atext in flow_items:
        try:
            records, counts = parse_flow(qtext, atext)
            date = _date_for(run_name, qpath)
            ref = _ref_for(run_name)
        except Exception:
            skipped["flow"]["malformed"] += 1
            continue
        skipped["flow"]["malformed"] += counts["malformed"]
        skipped["flow"]["unanswered"] += counts["unanswered"]
        # Only add context path if this flow produced entries.
        if records:
            try:
                ctx = flow_context_path(qtext)
            except ValueError:
                ctx = None
            if ctx:
                context.add(os.path.realpath(os.path.expanduser(ctx)))
        for rec in records:
            entries.append(_make_entry("flow", repo_key, os.path.abspath(qpath), date, ref, rec))

    census: Dict[str, int] = result["census"]
    for source, doc_name, parser in (
        ("plan-session", PLAN_DOC, parse_plan),
        ("review", REVIEW_DOC, parse_review),
    ):
        # Check if root directory exists but is unreadable.
        root_name = [name for name, src in SOURCE_ROOTS if src == source][0]
        root = os.path.join(home, ".claude", root_name)
        if os.path.exists(root) and not os.access(root, os.R_OK):
            skipped[source]["unreadable"] += 1
        for keyed_dir in info["dirs"][source]:
            for run_path, run_name in _subdirs(keyed_dir):
                doc = os.path.join(run_path, doc_name)
                if not os.path.isfile(doc):
                    continue
                real = os.path.realpath(doc)
                if real in seen_real:
                    continue
                seen_real.add(real)
                if real in context:
                    skipped[source]["linked_skipped"] += 1
                    continue
                try:
                    text = _read_text(doc)
                    date = _date_for(run_name, doc)
                    if source == "plan-session":
                        for heading in plan_headings(text):
                            census[heading] = census.get(heading, 0) + 1
                    records, counts = parser(text)
                    ref = _ref_for(run_name)
                except Exception:
                    skipped[source]["malformed"] += 1
                    continue
                skipped[source]["unanswered"] += counts["unanswered"]
                skipped[source]["malformed"] += counts["malformed"]
                for rec in records:
                    entries.append(_make_entry(source, repo_key, os.path.abspath(doc), date, ref, rec))

    result["entries"] = entries
    result["legacy"] = sorted(info["legacy"])
    result["unkeyed"] = info["unkeyed_review_dirs"]
    result["matched"] = any(info["dirs"][s] for s in SOURCES)
    return result


def order(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Total order: date desc, path asc, header asc, id asc."""
    ascending = sorted(entries, key=lambda e: (e["path"], e["header"], e["id"]))
    return sorted(ascending, key=lambda e: e["date"], reverse=True)


# ---------------------------------------------------------------------------
# --match, stats, rendering and atomic output
# ---------------------------------------------------------------------------

def load_match(path: str) -> List[Dict[str, Any]]:
    """Pending questions from a flow question file, a list, or one question object."""
    try:
        text = _read_text(path)
    except (OSError, UnicodeDecodeError):
        raise _Usage("cannot read --match file %s" % path)
    try:
        data = json.loads(text)
    except ValueError:
        raise _Usage("--match file is not valid JSON: %s" % path)
    if isinstance(data, dict) and isinstance(data.get("questions"), list):
        items = data["questions"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and ("header" in data or "question" in data):
        items = [data]
    else:
        raise _Usage("--match file has no questions: %s" % path)
    questions: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise _Usage("--match file has a malformed question: %s" % path)
        for field in ("header", "question"):
            if item.get(field) is not None and not isinstance(item.get(field), str):
                raise _Usage("--match file has a malformed question: %s" % path)
        questions.append({
            "header": _text_of(item.get("header")),
            "question": _text_of(item.get("question")),
            "options": [_norm(label) for label in _option_strings(item.get("options"))],
        })
    return questions


def _entry_weights(entry: Dict[str, Any]) -> Dict[str, int]:
    weights: Dict[str, int] = {}
    fields: List[Tuple[str, int]] = [
        (entry["header"], FIELD_WEIGHTS["header"]),
        (entry["question"], FIELD_WEIGHTS["question"]),
    ]
    fields.extend((label, FIELD_WEIGHTS["options"]) for label in entry["options"])
    for text, weight in fields:
        for tok in _tokens(text):
            if weights.get(tok, 0) < weight:
                weights[tok] = weight
    return weights


def match_entries(entries: List[Dict[str, Any]], questions: List[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """Rank by weighted token overlap with the pending questions; drop zero scores.

    Score = sum, over tokens shared with the pending questions, of the entry's
    max field weight (header 3, question 2, option 1) multiplied by log(1 + N/df),
    where N is the total number of entries and df is the number of entries
    containing the token. Ties keep the base order. The score is never emitted.
    """
    pending: Set[str] = set()
    for question in questions:
        pending.update(_tokens(question["header"]))
        pending.update(_tokens(question["question"]))
        for label in question["options"]:
            pending.update(_tokens(label))
    if not pending:
        return []
    # Compute document frequency for each token in pending.
    df: Dict[str, int] = {}
    for entry in entries:
        entry_tokens: Set[str] = set()
        for tok in _tokens(entry["header"]):
            entry_tokens.add(tok)
        for tok in _tokens(entry["question"]):
            entry_tokens.add(tok)
        for label in entry["options"]:
            for tok in _tokens(label):
                entry_tokens.add(tok)
        for tok in entry_tokens:
            if tok in pending:
                df[tok] = df.get(tok, 0) + 1
    N = len(entries)
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        score = 0.0
        for tok, w in _entry_weights(entry).items():
            if tok in pending:
                idf = math.log(1.0 + N / (df.get(tok, 1) or 1))
                score += w * idf
        if score > 0:
            scored.append((-score, index, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _score, _index, entry in scored[:top]]


def _count_sources(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {source: 0 for source in SOURCES}
    for entry in entries:
        counts[entry["source"]] = counts.get(entry["source"], 0) + 1
    return counts


def _load_predictions(repo_key: str, home: str) -> Tuple[List[Dict[str, Any]], int, int]:
    """Load prediction log for project scope (project-scoped only).

    Returns (rows, malformed_lines, duplicate_keys).
    Missing file gives empty list, zero counts.
    """
    repo_key_normalized = _fold(repo_key)
    log_path = os.path.join(home, ".claude", "precedent", repo_key_normalized, "predictions.jsonl")

    rows: List[Dict[str, Any]] = []
    malformed = 0
    duplicates = 0
    seen_keys: Set[Tuple[str, str]] = set()

    if not os.path.exists(log_path):
        return rows, malformed, duplicates

    try:
        with open(log_path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        malformed += 1
                        continue
                    # First-wins dedup on (run, question_id, command) - ensure string values
                    run_val = row.get("run", "")
                    qid_val = row.get("question_id", "")
                    cmd_val = row.get("command", "")
                    # Convert to strings to ensure hashability
                    key = (str(run_val) if run_val is not None else "",
                           str(qid_val) if qid_val is not None else "",
                           str(cmd_val) if cmd_val is not None else "")
                    if key in seen_keys:
                        duplicates += 1
                        continue
                    seen_keys.add(key)
                    rows.append(row)
                except (json.JSONDecodeError, ValueError, TypeError):
                    malformed += 1
    except (IOError, OSError):
        pass

    return rows, malformed, duplicates


def build_prediction_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute prediction statistics from logged rows.

    Always has the same keys:
      - Counts: log_path, logged, runs, scored, null, malformed_lines, duplicate_keys
      - Rates: hit_rate, baseline_recommended_hit_rate, baseline_n
      - by_confidence: for each of high/medium, keys logged/scored/hits/hit_rate/baseline_n/baseline_hit_rate
      - deviation: n, excluded_null, pat_right, panel_right, neither
    """
    # Count logged rows (across all attempts, since we loaded per repo_key)
    logged = len(rows)

    # Count unique runs
    runs = set(row.get("run") for row in rows)
    run_count = len(runs)

    # Classify null rows by actual_source
    null_rows = [r for r in rows if r.get("hit") is None]
    free_text_count = sum(1 for r in null_rows if r.get("actual_source") == "free_text")
    accepted_on_continue_count = sum(1 for r in null_rows if r.get("actual_source") == "accepted-on-continue")

    # Scored rows: hit is not None
    scored_rows = [r for r in rows if r.get("hit") is not None]
    scored = len(scored_rows)

    # Overall hit rate
    hits = sum(1 for r in scored_rows if r.get("hit") is True)
    hit_rate = round(hits / float(scored), 4) if scored > 0 else None

    # Baseline: recommended always chosen, over scored rows whose recommended is not null
    baseline_rows = [r for r in scored_rows if r.get("recommended") is not None]
    baseline_n = len(baseline_rows)
    baseline_hits = sum(1 for r in baseline_rows if r.get("actual") == r.get("recommended"))
    baseline_recommended_hit_rate = round(baseline_hits / float(baseline_n), 4) if baseline_n > 0 else None
    # Pat rate on baseline subset (F6: same denominators)
    pat_baseline_hits = sum(1 for r in baseline_rows if r.get("hit") is True)
    pat_baseline_hit_rate = round(pat_baseline_hits / float(baseline_n), 4) if baseline_n > 0 else None

    # By confidence
    by_confidence: Dict[str, Any] = {}
    for confidence in ("high", "medium"):
        conf_rows = [r for r in scored_rows if r.get("confidence") == confidence]
        conf_logged = len([r for r in rows if r.get("confidence") == confidence])
        conf_hits = sum(1 for r in conf_rows if r.get("hit") is True)
        conf_hit_rate = round(conf_hits / float(len(conf_rows)), 4) if conf_rows else None

        # Baseline for this confidence
        conf_baseline = [r for r in conf_rows if r.get("recommended") is not None]
        conf_baseline_n = len(conf_baseline)
        conf_baseline_hits = sum(1 for r in conf_baseline if r.get("actual") == r.get("recommended"))
        conf_baseline_rate = round(conf_baseline_hits / float(conf_baseline_n), 4) if conf_baseline_n > 0 else None

        by_confidence[confidence] = {
            "logged": conf_logged,
            "scored": len(conf_rows),
            "hits": conf_hits,
            "hit_rate": conf_hit_rate,
            "baseline_n": conf_baseline_n,
            "baseline_hit_rate": conf_baseline_rate,
        }

    # Deviation cell: rows where recommended is not null and predicted != recommended
    deviation_rows = [
        r for r in scored_rows
        if r.get("recommended") is not None and r.get("predicted") != r.get("recommended")
    ]
    deviation_n = len(deviation_rows)
    deviation_excluded_null = sum(
        1 for r in rows
        if r.get("recommended") is not None and r.get("predicted") != r.get("recommended") and r.get("hit") is None
    )
    deviation_pat_right = sum(1 for r in deviation_rows if r.get("actual") == r.get("predicted"))
    deviation_panel_right = sum(1 for r in deviation_rows if r.get("actual") == r.get("recommended"))
    deviation_neither = deviation_n - deviation_pat_right - deviation_panel_right

    return {
        "log_path": "~/.claude/precedent/<owner-name>/predictions.jsonl",
        "logged": logged,
        "runs": run_count,
        "scored": scored,
        "null": {
            "free_text": free_text_count,
            "accepted-on-continue": accepted_on_continue_count,
        },
        "malformed_lines": 0,  # Set by caller
        "duplicate_keys": 0,   # Set by caller
        "hit_rate": hit_rate,
        "baseline_recommended_hit_rate": baseline_recommended_hit_rate,
        "pat_baseline_hit_rate": pat_baseline_hit_rate,
        "baseline_n": baseline_n,
        "by_confidence": by_confidence,
        "deviation": {
            "n": deviation_n,
            "excluded_null": deviation_excluded_null,
            "pat_right": deviation_pat_right,
            "panel_right": deviation_panel_right,
            "neither": deviation_neither,
        },
    }


def build_stats(
    entries: List[Dict[str, Any]],
    skipped: Dict[str, Dict[str, int]],
    census: Dict[str, int],
    legacy: List[str],
    unkeyed: int,
    kept: int,
    total: int,
) -> Dict[str, Any]:
    decided = [e["deviated"] for e in entries if e["deviated"] is not None]
    deviation_rate = (
        round(sum(1 for flag in decided if flag) / float(len(decided)), 4) if decided else None
    )
    free_text = 0
    doc_freq: Dict[str, int] = {}
    for entry in entries:
        labels = {_norm(label) for label in entry["options"]}
        if _norm(entry["answer"]) not in labels:
            free_text += 1
        tokens = set(_tokens(" ".join([entry["header"], entry["question"], entry["answer"]])))
        for tok in tokens:
            doc_freq[tok] = doc_freq.get(tok, 0) + 1
    free_text_rate = round(free_text / float(len(entries)), 4) if entries else None
    top_tokens = [
        {"token": tok, "count": count}
        for tok, count in sorted(doc_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TOKENS]
    ]
    return {
        "per_source": _count_sources(entries),
        "skipped": {s: {b: skipped[s][b] for b in SKIP_BUCKETS} for s in SOURCES},
        "kept": kept,
        "total": total,
        "deviation_rate": deviation_rate,
        "free_text_rate": free_text_rate,
        "top_tokens": top_tokens,
        "plan_heading_census": {k: census[k] for k in sorted(census)},
        "legacy_dirs": legacy,
        "unkeyed_review_dirs": unkeyed,
    }


def _render_md_entry(entry: Dict[str, Any]) -> str:
    lines = [
        "### %s · %s · %s" % (entry["date"], entry["source"], entry["header"]),
        "",
        entry["question"],
        "",
    ]
    for option in entry["options"]:
        marker = " (recommended)" if option == entry["recommended"] else ""
        lines.append("- %s%s" % (option, marker))
    if entry["options"]:
        lines.append("")
    deviated = entry["deviated"]
    deviated_text = "unknown" if deviated is None else ("yes" if deviated else "no")
    reason = entry["reason"] if entry["reason"] else "none"
    ref = "" if entry["ref"] is None else " (ref %d)" % entry["ref"]
    lines.append("**Answer**: %s" % entry["answer"])
    lines.append("**Reason**: %s" % reason)
    lines.append("**Deviated**: %s" % deviated_text)
    lines.append("`%s`%s" % (entry["path"], ref))
    return "\n".join(lines)


def _fmt_rate(rate: Optional[float]) -> str:
    """Format a rate value as string, showing 'null' if None."""
    return "%.4f" % rate if rate is not None else "null"


def render(entries: List[Dict[str, Any]], fmt: str) -> str:
    """jsonl: one entry per line in pinned key order. md: one block per entry."""
    if fmt == "md":
        blocks = [_render_md_entry(entry) for entry in entries]
        return "\n\n".join(blocks) + ("\n" if blocks else "")
    return "".join(
        json.dumps({key: entry[key] for key in PINNED_KEYS}, ensure_ascii=False) + "\n"
        for entry in entries
    )


def render_stats(stats: Dict[str, Any], fmt: str) -> str:
    if fmt == "md":
        sections = []
        for key, value in stats.items():
            if key == "predictions":
                # Special formatting for predictions with rates next to numbers
                pred_md = "## Predictions\n\n"
                pred_md += "**Counts**: logged=%d, runs=%d, scored=%d, null (free_text=%d, accepted-on-continue=%d), malformed=%d, duplicate=%d\n\n" % (
                    value.get("logged", 0),
                    value.get("runs", 0),
                    value.get("scored", 0),
                    value.get("null", {}).get("free_text", 0),
                    value.get("null", {}).get("accepted-on-continue", 0),
                    value.get("malformed_lines", 0),
                    value.get("duplicate_keys", 0),
                )
                # Add anchoring caveat (D-C: all rows are anchored, so hit rate is upper bound)
                pred_md += "**Note**: All rows are anchored; hit rate is an upper bound.\n\n"

                hit_rate = value.get("hit_rate")
                hit_rate_str = _fmt_rate(hit_rate)
                baseline_rate = value.get("baseline_recommended_hit_rate")
                baseline_rate_str = _fmt_rate(baseline_rate)
                pred_md += "**Hit rates**: pat=(%s), baseline-recommended=(%s, n=%d)\n\n" % (
                    hit_rate_str, baseline_rate_str, value.get("baseline_n", 0)
                )
                for confidence in ("high", "medium"):
                    conf = value.get("by_confidence", {}).get(confidence, {})
                    conf_hit_rate = conf.get("hit_rate")
                    conf_baseline_rate = conf.get("baseline_hit_rate")
                    conf_n = conf.get("baseline_n", 0)
                    pred_md += "**%s**: logged=%d, scored=%d, hits=%d, pat=(%s), baseline-recommended=(%s, n=%d)\n\n" % (
                        confidence.capitalize(),
                        conf.get("logged", 0),
                        conf.get("scored", 0),
                        conf.get("hits", 0),
                        _fmt_rate(conf_hit_rate),
                        _fmt_rate(conf_baseline_rate),
                        conf_n,
                    )
                dev = value.get("deviation", {})
                pred_md += "**Deviation** (pat vs. panel): n=%d, excluded_null=%d, pat_right=%d, panel_right=%d, neither=%d" % (
                    dev.get("n", 0),
                    dev.get("excluded_null", 0),
                    dev.get("pat_right", 0),
                    dev.get("panel_right", 0),
                    dev.get("neither", 0),
                )
                sections.append(pred_md)
            else:
                sections.append("## %s\n\n```json\n%s\n```" % (key, json.dumps(value, indent=2, ensure_ascii=False)))
        return "\n\n".join(sections) + "\n"
    return json.dumps(stats, ensure_ascii=False) + "\n"


def _write_atomic(path: str, text: str) -> None:
    """Write text to path via a temp file in the same dir, then os.replace.

    The output file is created with mode 0o600 (read/write for owner only).
    On any error (including internal errors), the temp file is cleaned up and
    the output file is left unwritten or incomplete.
    """
    target = os.path.abspath(path)
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(target),
            prefix=".precedent-digest-", suffix=".tmp", delete=False,
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
        raise _Usage("cannot write --out %s: %s" % (path, exc.strerror or exc))
    except BaseException:
        try:
            os.unlink(temp_name)
        except BaseException:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="precedent-digest.py",
        description="Deterministic digest of a repo's prior human rulings (#292).",
    )
    parser.add_argument("repo", help="repo root path, or an owner/name key")
    parser.add_argument("--format", choices=("jsonl", "md"), default="jsonl")
    parser.add_argument("--match", metavar="QUESTIONS_JSON", default=None,
                        help="rank entries against the pending questions in this JSON file")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, metavar="N",
                        help="with --match, keep the top N entries (default %d)" % DEFAULT_TOP)
    parser.add_argument("--stats", action="store_true", help="emit the stats object instead of entries")
    parser.add_argument("--out", metavar="FILE", default=None,
                        help="write atomically to FILE (created with mode 0o600)")
    return parser


def _summary(repo: str, counts: Dict[str, int], skipped_total: int, linked_skipped: int, kept: int, total: int, reason: str) -> str:
    line = "precedent-digest: repo=%s plan-session=%d flow=%d review=%d skipped=%d linked_skipped=%d kept=%d of %d" % (
        repo, counts["plan-session"], counts["flow"], counts["review"], skipped_total, linked_skipped, kept, total,
    )
    if reason:
        line += " (%s)" % reason
    return line


def _run(argv: Optional[Sequence[str]]) -> int:
    args = _build_parser().parse_args(argv)
    if args.top < 1:
        raise _Usage("--top must be at least 1")
    if args.match is None and args.top != DEFAULT_TOP:
        raise _Usage("--top is only used with --match")
    questions = load_match(args.match) if args.match else None
    home = os.path.expanduser("~")

    key, why = resolve_key(args.repo)
    if key is None:
        collected = _empty_collection(args.repo)
        reason = "could not resolve repo key: %s" % why
    else:
        collected = collect(tuple(key.split("/", 1)), home)  # type: ignore[arg-type]
        reason = ""
    repo = collected["repo_key"]

    entries = order(collected["entries"])
    total = len(entries)
    if questions is not None:
        output = match_entries(entries, questions, args.top)
    else:
        output = entries[:CAP]
    if total == 0 and not reason and not collected["matched"]:
        reason = "no source dirs for %s" % repo

    if args.stats:
        stats = build_stats(
            entries, collected["skipped"], collected["census"],
            collected["legacy"], collected["unkeyed"], len(output), total,
        )
        # Add predictions section (project-scoped only, when --match is not used)
        if questions is None:
            pred_rows, pred_malformed, pred_duplicates = _load_predictions(repo, home)
            pred_stats = build_prediction_stats(pred_rows)
            pred_stats["malformed_lines"] = pred_malformed
            pred_stats["duplicate_keys"] = pred_duplicates
            stats["predictions"] = pred_stats
        else:
            # --match path: do not load predictions
            stats["predictions"] = {
                "log_path": "~/.claude/precedent/<owner-name>/predictions.jsonl",
                "logged": 0,
                "runs": 0,
                "scored": 0,
                "null": {"free_text": 0, "accepted-on-continue": 0},
                "malformed_lines": 0,
                "duplicate_keys": 0,
                "hit_rate": None,
                "baseline_recommended_hit_rate": None,
                "baseline_n": 0,
                "by_confidence": {
                    "high": {
                        "logged": 0, "scored": 0, "hits": 0, "hit_rate": None,
                        "baseline_n": 0, "baseline_hit_rate": None,
                    },
                    "medium": {
                        "logged": 0, "scored": 0, "hits": 0, "hit_rate": None,
                        "baseline_n": 0, "baseline_hit_rate": None,
                    },
                },
                "deviation": {"n": 0, "excluded_null": 0, "pat_right": 0, "panel_right": 0, "neither": 0},
            }
        text = render_stats(stats, args.format)
    else:
        text = render(output, args.format)

    if args.out:
        _write_atomic(args.out, text)
    elif text:
        sys.stdout.write(text)
        sys.stdout.flush()

    skipped_total = sum(
        collected["skipped"][s][b] for s in SOURCES for b in SKIP_BUCKETS if b != "linked_skipped"
    )
    linked_skipped = sum(
        collected["skipped"][s]["linked_skipped"] for s in SOURCES
    )
    _stderr(_summary(repo, _count_sources(entries), skipped_total, linked_skipped, len(output), total, reason))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns: 0 for data outcomes, 1 for internal errors, 2 for invocation errors."""
    try:
        return _run(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except _Usage as exc:
        _stderr("precedent-digest: %s" % exc)
        return 2
    except Exception as exc:  # never raise on data paths; keep the bug visible
        # Extract the innermost frame: get the file, line, and function name
        tb = traceback.extract_tb(sys.exc_info()[2])
        if tb:
            frame = tb[-1]
            lineno = frame.lineno if frame.lineno is not None else 0
            source = "%s:%d in %s" % (os.path.basename(frame.filename), lineno, frame.name)
        else:
            source = "unknown"
        _stderr("precedent-digest: internal error (%s: %s) [%s]" % (type(exc).__name__, exc, source))
        return 1


if __name__ == "__main__":
    sys.exit(main())
