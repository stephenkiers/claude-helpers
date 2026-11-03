#!/usr/bin/env python3
r"""flow-render-questions.py: render a question file for the human, to a file (#421).

One renderer for /expert-flow and /expert-fleet. The caller runs it with --out, then Reads that
file and prints the context in an assistant message before the question. The rendered content
never goes to stdout, because a Bash result is not shown to the human and may come back truncated.

Usage:
    flow-render-questions.py <question-file> --out <path> [--prefix "#N "]
        -> stdout, one line: "rendered: <kind> <n-batches|report> -> <out>"

Form kind (no `kind` field, or any kind other than "report") writes JSON:
    {"kind": "form", "warnings": [...], "batches": [
        {"preamble": "<markdown>", "questions": [{"id": "q1", "ask": {AskUserQuestion entry}}]}
    ]}
  - questions[] is chunked in file order into batches of at most 4.
  - Batch 1's preamble is the file's `summary` (when not null) and each brief (when not null) in
    file order. Later batches carry only their own questions' briefs. No `<id>:` label is printed.
  - All step-authored preamble text sits inside one backtick fence longer than any backtick run.
  - When summary is null and every brief is null, batch 1 carries the `context_path` file instead.
    If that file is unreadable, the preamble is a placeholder and a warning is recorded.
  - `ask` copies question, header, options (label and description) and multiSelect unchanged.
  - The header is capped at 12 characters only when --prefix is given (prefix + header, truncated
    at a word boundary or with '…').

Report kind (kind == "report") writes markdown: headline (blockquote), north_star_blind, the
"Needs your answer" line, the ticket, the Q, D and W sections, the roundup, and the fixed closing
sentence. Step-authored text that starts a line with `#`, `---` or a backtick is backslash-escaped.

Writes are atomic: a temp file in the output directory, then os.replace. On any failure --out
parent directory may be created but the file itself is not.

Exit codes:
  0: ok
  1: (a) usage error: bad arguments such as --prefix longer than 11 characters.
     (b) Unhandled exceptions (crash): exit 1 with traceback on stderr.
  2: deliberately unused (python3 exits 2 when this script file is missing)
  3: input file unreadable or not JSON
  4: invalid shape: questions not a list, empty questions list, a question lacking id or with null
     question, duplicate question text, malformed options entries, non-string header, items not a
     list in a report, or a report item with an unknown handle (not Q, D, or W)
"""
import argparse
import json
import os
import re
import sys
import tempfile
import traceback
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Set, TypedDict

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_UNREADABLE = 3
EXIT_INVALID = 4

BATCH_SIZE = 4
HEADER_CAP = 12
PREFIX_MAX = 11
NO_CONTEXT = "(no context provided by the step)"
REPORT_CLOSING = (
    "Reply in your own words: answer every Q, and name any D or W you want to change. "
    "Unmentioned D and W items stand; unanswered Qs will be asked again unless you say continue. "
    "Example: **Q1: B, Q2: A, change D1 to focus on security.**"
)
ESCAPE_STARTS = ("#", "---", "`")
BACKTICK_RUN_RE = re.compile(r"`+")


class _Unreadable(Exception):
    """Input unreadable or not JSON: exit 3."""


class _Invalid(Exception):
    """Invalid shape: exit 4."""


class _OptionEntry(TypedDict, total=False):
    """An option for an AskUserQuestion."""
    label: Optional[str]
    description: Optional[str]


class _AskQuestion(TypedDict, total=False):
    """The ask entry for a question in a form batch."""
    question: str
    header: Optional[str]
    options: List[_OptionEntry]
    multiSelect: bool


class _FormBatch(TypedDict, total=False):
    """A batch of questions in a form rendering."""
    preamble: str
    questions: List[Dict[str, Any]]


class _FormOutput(TypedDict, total=False):
    """The output shape of render_form."""
    kind: str
    warnings: List[str]
    batches: List[_FormBatch]


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports usage errors on one stderr line and exits 1."""

    def error(self, message: str) -> NoReturn:
        sys.stderr.write("flow-render-questions: %s\n" % message)
        sys.exit(EXIT_USAGE)


def _load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _Unreadable("cannot read question file %s: %s" % (path, exc))


def _fence(content: str) -> str:
    """Wrap content in a backtick fence longer than any backtick run inside it."""
    longest = max((len(run) for run in BACKTICK_RUN_RE.findall(content)), default=0)
    fence = "`" * max(3, longest + 1)
    return "%s\n%s\n%s" % (fence, content, fence)


def _read_context(path: Any, warnings: List[str]) -> Optional[str]:
    """Read a context file with containment check and size cap.

    The realpath should be under ~/.claude/flows/ for production use.
    Caps bytes read at 64 KiB and warns if truncated.
    """
    if not isinstance(path, str) or not path:
        warnings.append("no summary or brief, and context_path is null: no context shown")
        return None

    # Validate containment: realpath should be under ~/.claude/flows/
    try:
        real = os.path.realpath(path)
        flows_base = os.path.expanduser("~/.claude/flows")
        # Check if real path is under flows base
        try:
            common = os.path.commonpath([real, flows_base])
            is_contained = common == flows_base
        except ValueError:
            # Paths on different drives (Windows)
            is_contained = False

        if not is_contained:
            # Warn but skip reading (security boundary)
            warnings.append(
                "context_path %s is not under ~/.claude/flows: skipped (full path: %s)" % (path, real)
            )
            return None
    except OSError:
        # Permission issues with realpath
        warnings.append("context_path %s: permission error" % path)
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read(64 * 1024)  # 64 KiB cap
        # Check if truncated
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.seek(64 * 1024)
                extra = fh.read(1)
                if extra:
                    content += "\n… (context truncated at 64 KiB)"
        except (OSError, UnicodeDecodeError):
            pass
        return content
    except (OSError, UnicodeDecodeError):
        warnings.append("context_path %s is unreadable: no context shown" % path)
        return None


def _escape(text: str) -> str:
    """Backslash-escape each line that starts with a heading, rule or backtick marker."""
    lines = []
    for line in text.split("\n"):
        if line.startswith(ESCAPE_STARTS):
            line = "\\" + line
        lines.append(line)
    return "\n".join(lines)


def _truncate_header(header: str, cap: int) -> str:
    """Truncate header at word boundary or with ellipsis if it exceeds cap."""
    if len(header) <= cap:
        return header
    # Try to truncate at a word boundary
    truncated = header[:cap]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        # Truncate at last space and add ellipsis
        return (truncated[:last_space] + "…")[:cap]
    else:
        # No space found; just truncate and add ellipsis
        return (header[:cap - 1] + "…")


def _as_text(value: Any) -> str:
    """Convert a value to text. None becomes a placeholder."""
    if value is None:
        return "(none)"
    return value if isinstance(value, str) else str(value)


def _ask_options(raw: Any) -> Any:
    """Extract label and description from options, validating each entry."""
    if not isinstance(raw, list):
        return raw
    result = []
    for opt in raw:
        if not isinstance(opt, dict):
            raise _Invalid("an options entry is not a JSON object")
        # label and description are optional; use None as default
        entry: _OptionEntry = {
            "label": opt.get("label"),
            "description": opt.get("description"),
        }
        result.append(entry)
    return result


def render_form(data: Any, prefix: Optional[str]) -> _FormOutput:
    if not isinstance(data, dict):
        raise _Invalid("question file is not a JSON object")
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise _Invalid("questions is not a list")
    if not questions:
        raise _Invalid("questions is empty")

    seen: Set[str] = set()
    for q in questions:
        if not isinstance(q, dict) or q.get("id") is None:
            raise _Invalid("a question lacks id")
        text = q.get("question")
        if not isinstance(text, str):
            raise _Invalid("question %s has a null or non-string question" % q.get("id"))
        if text in seen:
            raise _Invalid("duplicate question text: %s" % text)
        seen.add(text)

    warnings: List[str] = []
    summary = data.get("summary")
    fallback = summary is None and all(q.get("brief") is None for q in questions)

    batches: List[_FormBatch] = []
    for start in range(0, len(questions), BATCH_SIZE):
        chunk = questions[start:start + BATCH_SIZE]
        preamble = ""
        if start == 0 and fallback:
            ctx = _read_context(data.get("context_path"), warnings)
            preamble = NO_CONTEXT if ctx is None else _fence(ctx.rstrip("\n"))
        else:
            blocks: List[str] = []
            if start == 0 and summary is not None:
                blocks.append(_as_text(summary))
            for q in chunk:
                if q.get("brief") is not None:
                    brief_text = q["brief"]
                    if isinstance(brief_text, str):
                        blocks.append(_fence(brief_text))
                    else:
                        blocks.append(_fence(_as_text(brief_text)))
            if blocks:
                preamble = "\n\n".join(blocks)

        entries: List[Dict[str, Any]] = []
        for q in chunk:
            header = q.get("header")
            # Validate header is a string if provided
            if header is not None and not isinstance(header, str):
                raise _Invalid("question %s has a non-string header" % q.get("id"))

            # Apply prefix and truncation
            if prefix is not None:
                full_header = prefix + (header or "")
                header = _truncate_header(full_header, HEADER_CAP)

            ask: _AskQuestion = {
                "question": q["question"],
                "header": header,
                "options": _ask_options(q.get("options")),
                "multiSelect": q.get("multiSelect", False),
            }
            entries.append({"id": q["id"], "ask": ask})
        batches.append({"preamble": preamble, "questions": entries})

    return {"kind": "form", "warnings": warnings, "batches": batches}


def _bullets_for_item(item: Dict[str, Any], prefix: str) -> List[str]:
    lines: List[str] = []
    title = _escape(_as_text(item.get("title") or "(untitled)"))
    handle = _as_text(item.get("handle") or "")
    lines.append("**%s.** %s" % (handle, title))
    lines.append("")
    if prefix == "Q":
        options = item.get("options")
        if isinstance(options, list):
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                # Only include label and description if they are present (not None)
                label = opt.get("label")
                desc_for = opt.get("for")
                desc_against = opt.get("against")
                label_text = _escape(_as_text(label)) if label is not None else "(no label)"
                for_text = _escape(_as_text(desc_for)) if desc_for is not None else ""
                against_text = _escape(_as_text(desc_against)) if desc_against is not None else ""
                lines.append("- Option %s: for: %s; against: %s" % (
                    label_text, for_text, against_text,
                ))
        label = "Recommendation"
    elif prefix == "D":
        label = "Decision"
    else:
        label = "Recommendation"
    rec = item.get("decision_or_recommendation")
    if rec is not None:
        lines.append("- %s: %s" % (label, _escape(_as_text(rec))))
    backed = item.get("backed_by")
    if isinstance(backed, list):
        backed_text = ", ".join(_as_text(b) for b in backed)
    else:
        backed_text = _as_text(backed) if backed is not None else "none"
    lines.append("- Backed by: %s" % _escape(backed_text))
    why = item.get("why")
    if why is not None:
        lines.append("- Why: %s" % _escape(_as_text(why)))
    if prefix == "Q" and item.get("business_case") is not None:
        lines.append("- Business case: %s" % _escape(_as_text(item["business_case"])))
    return lines


def render_report(data: Any) -> str:
    if not isinstance(data, dict):
        raise _Invalid("question file is not a JSON object")
    items = data.get("items")
    if not isinstance(items, list):
        raise _Invalid("items is not a list")
    groups: Dict[str, List[Dict[str, Any]]] = {"Q": [], "D": [], "W": []}
    for item in items:
        if not isinstance(item, dict):
            raise _Invalid("an item is not a JSON object")
        handle = item.get("handle")
        if handle is not None:
            key = _as_text(handle)[:1]
        else:
            key = ""
        # Validate handle is one of Q, D, W
        if key not in groups:
            raise _Invalid("a report item has an unknown handle: %s" % handle)
        groups[key].append(item)

    parts: List[str] = []
    headline = data.get("headline")
    if headline is not None:
        quoted = ["> " + line if line else ">" for line in _escape(_as_text(headline)).split("\n")]
        parts.append("\n".join(quoted))
    nsb = data.get("north_star_blind")
    if nsb is not None:
        parts.append(_escape(_as_text(nsb)))

    must = [_as_text(i.get("handle")) for i in groups["Q"] if i.get("must_answer") is True]
    parts.append("Needs your answer: %s" % (", ".join(must) if must else "none"))

    ticket = data.get("ticket")
    if ticket is not None:
        parts.append("## What this ticket does\n\n%s" % _escape(_as_text(ticket)))

    for key, heading in (("Q", "Questions for you"), ("D", "Decided"), ("W", "Worth a look")):
        section = ["## %s (%d)" % (heading, len(groups[key]))]
        if not groups[key]:
            section.append("")
            section.append("None.")
        for item in groups[key]:
            section.append("")
            section.append("\n".join(_bullets_for_item(item, key)))
        parts.append("\n".join(section))

    roundup = data.get("roundup")
    if roundup is not None:
        parts.append(_escape(_as_text(roundup)))

    parts.append(REPORT_CLOSING)
    return "\n\n".join(parts) + "\n"


def _write_atomic(out: str, content: str) -> None:
    out_dir = os.path.dirname(os.path.abspath(out))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".flow-render-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Set readable file mode (0o644)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description="Render a question file to --out for the human (#421).",
    )
    parser.add_argument("question_file", help="Question JSON file written by the step")
    parser.add_argument("--out", required=True, help="Output path (created atomically)")
    parser.add_argument("--prefix", default=None,
                        help="Header prefix such as '#N '; caps the header at 12 characters "
                             "(prefix of at most 11 characters)")
    return parser


def _run(argv: Optional[Sequence[str]]) -> int:
    args = _build_parser().parse_args(argv)
    if args.prefix is not None and len(args.prefix) > PREFIX_MAX:
        sys.stderr.write("flow-render-questions: --prefix must be at most %d characters\n" % PREFIX_MAX)
        return EXIT_USAGE

    try:
        data = _load(args.question_file)
        if isinstance(data, dict) and data.get("kind") == "report":
            content = render_report(data)
            _write_atomic(args.out, content)
            sys.stdout.write("rendered: report report -> %s\n" % args.out)
            return EXIT_OK
        doc = render_form(data, args.prefix)
    except _Unreadable as exc:
        sys.stderr.write("flow-render-questions: %s\n" % exc)
        return EXIT_UNREADABLE
    except _Invalid as exc:
        sys.stderr.write("flow-render-questions: invalid question file: %s\n" % exc)
        return EXIT_INVALID

    _write_atomic(args.out, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.write("rendered: form %d -> %s\n" % (len(doc["batches"]), args.out))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return _run(argv)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
