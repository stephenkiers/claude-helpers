#!/usr/bin/env python3
"""test-cleanup-check.py: deterministic checks for /implement-with-haiku's spec-blind test flow (#458).

Usage:
    test-cleanup-check.py fence --base SHA --head SHA|INDEX|WORKTREE
                          --test FILE::[CLASS::]NAME [--test ...]
                          [--impl-files PATH [PATH ...]]
    test-cleanup-check.py survivors --deletions FILE --collected FILE [--merged FILE]
    test-cleanup-check.py names [--hygiene] --base SHA [--head WORKTREE|HEAD] [--allow PATH [PATH ...]]
    test-cleanup-check.py working-ids --base SHA --head REF
    test-cleanup-check.py layout --base SHA [--impl PATH [PATH ...]] [--max-names N]
    test-cleanup-check.py overlaps --report PATH [--report ...] --added-ids PATH --out PATH

Runs in the current directory, which must be inside the target git repo. Paths are repo-root
relative. Every subcommand prints one finding per line on stdout and exits:
    0  pass (no findings that are violations)
    1  violation found, or input that cannot be read, sized, located or parsed (fails closed;
       the reason goes to stderr)
    2  argparse usage error

Policy (fail closed): any block that cannot be sized, any test id that cannot be located
unambiguously, any row that cannot be parsed, and any git call that fails is an error (exit 1),
never a silent pass.

fence: the binding-edit fence (Q2, D1). Each --test id is FILE::NAME, or FILE::CLASS::NAME to pick
one of several same-named tests (Python). Only that test's span is compared between base and head:
the decorator/attribute lines directly above it, its body (the whole signature through the end of
the body), and the file's import lines. Other tests in the same file are never inspected. --head
INDEX compares the staged index, WORKTREE the working tree, and a SHA a commit. Blank lines are not
significant.

  Each changed line in a span is classified:
    binding-edit  an import line (IMPORT patterns), or a call-site line that is not an assertion
                  line, paired one-to-one with its base line, where the prefix and suffix are
                  identical, the callee and/or the argument count changed, the arguments that both
                  lines have are identical, and no literal argument of the base line is dropped.
    violation     anything else: an assertion line (ASSERT patterns), an open assertion's
                  continuation lines (a call whose parentheses are not yet closed), a line whose
                  first non-space character is "." (method chain continuation), a decorator or
                  attribute line, a call whose callee sits inside a string literal, a changed
                  expected-value assignment, a changed literal argument, an added or removed
                  non-import line, or a line the patterns cannot recognise (fail closed).

  Output per changed line: "binding-edit <id> <line>" or "violation <id> <line>". The line is the
  head line number for added or changed lines and the base line number for removed lines. A test
  with a violation also gets "restore <id> <start>-<end>", its base span range (1-based, inclusive,
  decorators included), so the caller can restore that span alone. A test id not found in base or
  head, or ambiguous, or whose block cannot be sized, prints "unlocated <id> base|head" plus
  "violation <id> 0".

  With --impl-files, each changed path not in that list is printed once as "support-edit <path>"
  when it changed outside any test span and outside import lines (or when its test spans cannot be
  sized). That covers shared fixtures, conftest.py, helpers, module-level test code, snapshot and
  golden files and test data. A support-edit finding alone does not fail the fence (exit stays 0).

survivors: every survivor named in a deletions table must appear in the collected test-id list.
  Deletions file: a markdown table with one header row, one separator row, then one data row per
  deletion. The columns are located by header name (case-insensitive, any order, extra columns
  allowed): "id" (the deleted test) and "survivor or proof". The header must name both exactly once;
  a missing header row or a missing required column fails closed. Every data row must have as many
  cells as the header; a row that does not, or whose id cell is empty, or whose survivor cell is
  neither a test id (contains "::", backticks stripped), a "proof: <kind>" marker, nor a commit SHA,
  fails closed. A header-only table is valid and names no survivors; a file with no such table fails
  closed.
  --merged FILE maps pre-merge ids to post-merge ids, one "<old id> -> <new id>" per line (blank
  lines and "#" comments ignored; any other line fails closed). A survivor with a mapping is looked
  up under its new id. The collected file holds one test id per line. Each missing survivor prints
  "missing-survivor <survivor> named-by <deleted>" (or "missing-survivor <new> (renamed from <old>)
  named-by <deleted>" for a mapped survivor).

names: no spec-blind name survives in the run's own working set (D2). "spec[-_ ]?blind" (case
  insensitive) is searched in the changed paths and in the added lines of the diff. The default
  --head WORKTREE compares the working tree with base and includes untracked files (their whole
  content counts as added); --head HEAD compares the committed HEAD with base. Hits print
  "spec-blind path <path>" or "spec-blind line <path>:<n> <text>". --allow exempts exact
  repo-relative paths.
  --hygiene: match HYGIENE_TOKENS (spec_blind, spec, hardening, blackbox, review_fixes, followups,
  phase_N, wave_N) as a suffix or entire value in ADDED test-file paths and test-definition names
  only. Hits print one per line in the same format; segment-only matches (tokens appearing only as
  inner segments) print after all hits, each prefixed "segment-only:", and never affect exit code.
  Plain names (no flag) behaves exactly as before (spec-blind only).

working-ids: print one file::name per line for every test definition ADDED between SHA and REF
  (used for change tracking), sorted and de-duplicated. Only test files count; pre-existing tests
  are not printed. Exit 0 on success (even with no output), 1 on git failure/unreadable input.

layout: from git ls-tree / git show AT SHA ONLY, list test files whose basename contains the
  module stem of any --impl path (stem = basename without extension, - and _ equivalent), each
  followed by its test-definition names. Also list up to 3 sibling test file paths per test directory
  as naming patterns. Never prints any line of a test body, only paths and names. A missing path at
  SHA yields zero names and exit 0. Output capped by --max-names (default 60 names); when cut off,
  prints final line "(truncated)". If --base is omitted, lists paths only (from current working tree)
  and no names.

overlaps: parse lines of the form "id | overlaps: id or none" from --report files (repeatable;
  missing file counts as zero lines, not error). Strip surrounding whitespace and list markers.
  Overwrite --out in full on every call. Write one markdown row per non-"none" overlap, tagged
  "class-1 candidate": "- <new id> | overlaps: <survivor id> | class-1 candidate". "none" writes no
  row. Unparseable lines and ids in --added-ids with no parseable report line are listed under
  "unreported:" as "- <raw line stripped>". Last line is summary: "candidates: <C>  none: <K>
  unreported: <U>". Print summary to stdout. Exit 0 on success, 1 if --added-ids or --out cannot be
  read/written.

Patterns, per framework (chosen from the file extension):
  python  (.py)      def NAME(  (test body sized by the AST: the def's lines through its end_lineno,
                     plus any deeper-indented comment lines directly after it; a file that does not
                     parse, or a def the AST does not contain, cannot be sized); tests are def test*;
                     import: "from x import" / "import x"; assert: assert*, pytest.raises;
                     decorators: "@" lines directly above
  js/ts   (.js .jsx .ts .tsx .mjs .cjs)
                     it("NAME", ...) / test("NAME", ...) (brace block); import: "import ..."
                     assert: expect(, assert*; decorators: "@" lines directly above
  rust    (.rs)      fn NAME( with a #[test] or #[tokio::test] attribute (brace block);
                     import: use ... ; assert: assert!, assert_eq!, assert_ne!; attributes: "#["
  swift   (.swift)   func testNAME( (brace block); import: import X; assert: XCTAssert*;
                     attributes: "@" lines directly above
  go      (.go)      func TestNAME( (brace block); import: import ...; assert: t.Error*, t.Fatal*,
                     require.*
  Any other extension has no test bodies: every changed non-blank line counts for support-edit.

Known limitation: multi-line decorators (e.g. @pytest.mark.parametrize spanning multiple lines) are
only recognized up to their opening line (the line starting with "@"). Continuation lines that do not
start with the decorator prefix (@, #[) are excluded from the span. A change to such a continuation
line (e.g. in a parametrize argument list) will not be flagged as a violation.
"""

import argparse
import ast
import difflib
import os
import re
import subprocess
import sys
from typing import Dict, List, Literal, NoReturn, Optional, Pattern, Sequence, Set, Tuple

Lang = Literal["python", "js", "rust", "swift", "go"]
Verdict = Literal["binding-edit", "violation"]
# (first line of decorators/attributes, definition line, last body line); 0-based, inclusive.
Span = Tuple[int, int, int]

WORKING_HEADS = ("INDEX", "WORKTREE")

EXT_LANG: Dict[str, Lang] = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".rs": "rust",
    ".swift": "swift",
    ".go": "go",
}

IMPORT_RES: Dict[Lang, Pattern[str]] = {
    "python": re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\s+\S)"),
    "js": re.compile(r"^\s*import\b"),
    "rust": re.compile(r"^\s*(?:use\s+\S|extern\s+crate\b)"),
    "swift": re.compile(r"^\s*import\s+\w"),
    "go": re.compile(r"^\s*import\b"),
}

# Start of a test or helper definition; group 1 is the name (group 2 for js, whose name is quoted).
DEF_RES: Dict[Lang, Pattern[str]] = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
    "js": re.compile(r"^\s*(?:it|test)(?:\.\w+)?\s*\(\s*(['\"`])(.*?)\1"),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*[(<]"),
    "swift": re.compile(r"^\s*func\s+(\w+)\s*\("),
    "go": re.compile(r"^\s*func\s+(\w+)\s*\("),
}

# Prefix of a decorator/attribute line directly above a definition (Go has none).
DECO_PREFIX: Dict[str, str] = {"python": "@", "js": "@", "rust": "#[", "swift": "@"}

ASSERT_RE = re.compile(
    r"\bassert\w*|\bpytest\.raises\b|\bexpect\s*\(|\bXCTAssert\w*"
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf)\b|\brequire\.\w+"
)

CALL_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\(")

LITERAL_RE = re.compile(
    r"""-?\d+(?:\.\d+)?|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`"""
    r"|True|False|None|true|false|null|nil"
)

SPEC_RE = re.compile(r"spec[-_ ]?blind", re.IGNORECASE)
CLASS_RE = re.compile(r"^\s*class\s+(\w+)")
TABLE_SEP_RE = re.compile(r"^:?-{3,}:?$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Deletions-table columns the survivors check needs, located by header name (see round4-deletions.md, 4.4).
DELETION_ID_COL = "id"
DELETION_SURVIVOR_COL = "survivor or proof"
C_ESCAPES = {"\\": "\\", '"': '"', "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
# Hygiene tokens for test file and test name classification
HYGIENE_TOKENS: Tuple[str, ...] = ("spec_blind", "spec", "hardening", "blackbox", "review_fixes", "followups", "phase_N", "wave_N")


def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _hygiene_token_match(stem_or_name: str) -> Tuple[bool, bool]:
    """Check if stem or test name contains a hygiene token as a suffix or entire value.

    Returns (hit, segment_only):
    - hit: True if token is a suffix or the entire value
    - segment_only: True if token is only a segment in the middle (never part of the answer)
    """
    normalized = stem_or_name.lower().replace("-", "_")
    segments = normalized.split("_")

    for token in HYGIENE_TOKENS:
        token_lower = token.lower()

        if token_lower == "phase_n" or token_lower == "wave_n":
            # Special handling for phase_N and wave_N
            pattern = r"(phase|wave)_(\d+|[a-z])$"
            if re.search(pattern, normalized):
                return True, False  # Hit as suffix
            # Check if it appears as a segment only
            for segment in segments:
                if re.match(r"(phase|wave)_(\d+|[a-z])$", segment):
                    continue  # This is fine, skip
                if segment in ("phase", "wave"):
                    return False, True  # Segment-only match
        else:
            token_lower_norm = token_lower.replace("_", "")
            normalized_clean = normalized.replace("_", "")

            # Check if it's a suffix or entire value
            if normalized == token_lower or normalized_clean.endswith(token_lower_norm):
                return True, False

            # Check if it's a segment-only
            for segment in segments:
                if segment == token_lower:
                    return False, True

    return False, False


def _test_name_hygiene(test_name: str) -> Tuple[bool, bool]:
    """Extract test name for hygiene checking (remove class/file prefix if present)."""
    # For test names like "test_foo_spec_blind", check the name part
    return _hygiene_token_match(test_name)


def _git(args: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _git_ok(args: List[str]) -> str:
    """Output of a git call that must succeed; a failed call fails closed."""
    rc, out = _git(args)
    if rc != 0:
        subcommand = args[2] if args[:1] == ["-C"] and len(args) > 2 else (args[0] if args else "")
        _fail(f"git {subcommand} failed (exit {rc})")
    return out


def _require_ref(ref: str, allow_working: bool) -> None:
    if allow_working and ref in WORKING_HEADS:
        return
    rc, _ = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if rc != 0:
        _fail(f"bad-ref {ref}")


def _toplevel() -> str:
    rc, out = _git(["rev-parse", "--show-toplevel"])
    if rc != 0:
        _fail("not inside a git repository")
    return out.strip()


def _confine(path: str) -> None:
    """Fail closed on a path that could leave the repo root (absolute, or with a '..' component)."""
    if not path or os.path.isabs(path) or ".." in re.split(r"[\\/]", path):
        _fail(f"path escapes the repo root: {path!r}")
    root = os.path.realpath(_toplevel())
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        _fail(f"path escapes the repo root: {path!r}")


def _read_at(ref: str, path: str) -> Optional[str]:
    """File text at a ref (SHA, INDEX or WORKTREE), or None if the path does not exist there."""
    _confine(path)
    if ref == "WORKTREE":
        full = os.path.join(_toplevel(), path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    spec = f":{path}" if ref == "INDEX" else f"{ref}:{path}"
    rc, out = _git(["show", spec])
    return out if rc == 0 else None


def _nul_split(out: str) -> List[str]:
    return [p for p in out.split("\0") if p]


def _diff_paths(base: str, head: str) -> List[str]:
    if head == "INDEX":
        args = ["diff", "--cached", "--name-only", "-z", "--no-renames", base]
    elif head == "WORKTREE":
        args = ["diff", "--name-only", "-z", "--no-renames", base]
    else:
        args = ["diff", "--name-only", "-z", "--no-renames", base, head]
    return _nul_split(_git_ok(args))


def _untracked_paths() -> List[str]:
    """Untracked files of the whole repo (listed from the toplevel, not the current directory)."""
    top = _toplevel()
    return _nul_split(_git_ok(["-C", top, "ls-files", "--others", "--exclude-standard", "--full-name", "-z"]))


def _changed_paths(base: str, head: str) -> List[str]:
    """Repo-relative paths changed between base and head (working set when head is INDEX/WORKTREE)."""
    paths = _diff_paths(base, head)
    if head == "WORKTREE":
        paths += _untracked_paths()
    return sorted(set(paths))


def _lang(path: str) -> Optional[Lang]:
    return EXT_LANG.get(os.path.splitext(path)[1].lower())


def _lines(text: Optional[str]) -> List[str]:
    return text.splitlines() if text is not None else []


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_import(lang: Optional[Lang], line: str) -> bool:
    if lang is None:
        return False
    return IMPORT_RES[lang].search(line) is not None


def _significant(lang: Optional[Lang], line: str) -> bool:
    return line.strip() != "" and not _is_import(lang, line)


def _rust_test_attr(lines: List[str], index: int) -> bool:
    j = index - 1
    while j >= 0 and lines[j].strip().startswith("#["):
        if "test" in lines[j]:
            return True
        j -= 1
    return False


def _find_starts(lang: Optional[Lang], lines: List[str]) -> List[Tuple[int, str, bool]]:
    """(line index, name, is_test) for every definition the language's pattern recognises."""
    if lang is None:
        return []
    rx = DEF_RES[lang]
    out: List[Tuple[int, str, bool]] = []
    for i, line in enumerate(lines):
        m = rx.match(line)
        if m is None:
            continue
        if lang == "js":
            name = m.group(2)
            is_test = True
        else:
            name = m.group(1)
            if lang == "python":
                is_test = name.startswith("test")
            elif lang == "rust":
                is_test = _rust_test_attr(lines, i)
            elif lang == "swift":
                is_test = name.startswith("test")
            else:
                is_test = name.startswith("Test")
        out.append((i, name, is_test))
    return out


def _enclosing_class(lang: Lang, lines: List[str], index: int) -> Optional[str]:
    """Name of the Python class directly enclosing the definition at `index`, else None."""
    if lang != "python":
        return None
    indent = _indent(lines[index])
    for k in range(index - 1, -1, -1):
        text = lines[k]
        if not text.strip() or _indent(text) >= indent:
            continue
        m = CLASS_RE.match(text)
        if m is not None:
            return m.group(1)
        indent = _indent(text)
        if indent == 0:
            return None
    return None


def _python_block_ends(lines: List[str]) -> Optional[Dict[int, int]]:
    """0-based index of each def statement's first line -> 0-based index of its last line (from the
    AST, so column-0 lines inside a string or body do not end a block). None when the file does not
    parse, in which case no Python span in it can be sized (fail closed)."""
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError):
        return None
    ends: Dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno is not None:
            ends[node.lineno - 1] = node.end_lineno - 1
    return ends


def _block_end(lang: Lang, lines: List[str], start: int) -> Optional[int]:
    """Inclusive index of the last line of the block that starts at `start`, or None if unsizable."""
    if lang == "python":
        ends = _python_block_ends(lines)
        if ends is None or start not in ends:
            return None
        end = ends[start]
        indent = _indent(lines[start])
        # The AST stops at the last statement; comments indented into the body still belong to it.
        for k in range(end + 1, len(lines)):
            text = lines[k]
            if not text.strip():
                continue
            if text.lstrip().startswith("#") and _indent(text) > indent:
                end = k
                continue
            break
        return end
    depth = 0
    seen = False
    for k in range(start, len(lines)):
        for ch in lines[k]:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return k
    return None


def _decorator_start(lang: Lang, lines: List[str], start: int) -> int:
    """First line of the contiguous decorator/attribute block directly above `start`."""
    prefix = DECO_PREFIX.get(lang)
    if prefix is None:
        return start
    first = start
    k = start - 1
    while k >= 0 and lines[k].strip().startswith(prefix):
        first = k
        k -= 1
    return first


def _span_at(lang: Lang, lines: List[str], start: int) -> Optional[Span]:
    end = _block_end(lang, lines, start)
    if end is None:
        return None
    return (_decorator_start(lang, lines, start), start, end)


def _test_spans(lang: Optional[Lang], lines: List[str]) -> Tuple[List[Span], bool]:
    """Spans of every test definition, and whether every one could be sized."""
    if lang is None:
        return [], True
    spans: List[Span] = []
    sized = True
    for start, _, is_test in _find_starts(lang, lines):
        if not is_test:
            continue
        span = _span_at(lang, lines, start)
        if span is None:
            sized = False
        else:
            spans.append(span)
    return spans, sized


def _locate(lang: Optional[Lang], lines: List[str], name: str,
            cls: Optional[str]) -> Tuple[Optional[Span], str]:
    """The one span named by the test id, or (None, reason) when missing, ambiguous or unsizable."""
    if lang is None:
        return None, "no test framework for this file type"
    hits = [start for start, found, _ in _find_starts(lang, lines)
            if found == name and (cls is None or _enclosing_class(lang, lines, start) == cls)]
    if not hits:
        return None, "not found"
    if len(hits) > 1:
        return None, "ambiguous (several definitions match; add ::Class to the id)"
    span = _span_at(lang, lines, hits[0])
    if span is None:
        return None, "block cannot be sized (it never closes)"
    return span, ""


def _split_args(s: str) -> List[str]:
    """Top-level comma split that respects brackets and quotes; empty pieces are dropped."""
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    quote: Optional[str] = None
    escaped = False
    for ch in s:
        if quote is not None:
            cur.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _in_string(line: str, pos: int) -> bool:
    """Whether character `pos` of `line` lies inside a string literal."""
    quote: Optional[str] = None
    escaped = False
    for ch in line[:pos]:
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
    return quote is not None


def _is_literal(arg: str) -> bool:
    return LITERAL_RE.fullmatch(arg.strip()) is not None


def _parse_call(line: str) -> Optional[Tuple[str, str, List[str], str]]:
    """(prefix, callee, args, suffix) for a single call on the line, or None."""
    m = CALL_RE.search(line)
    if m is None:
        return None
    if _in_string(line, m.start(1)):
        return None
    rp = line.rfind(")")
    if rp < m.end():
        return None
    return line[: m.start(1)], m.group(1), _split_args(line[m.end():rp]), line[rp + 1:]


def _args_allowed(base_args: List[str], head_args: List[str]) -> bool:
    for i in range(min(len(base_args), len(head_args))):
        if base_args[i] != head_args[i]:
            return False
    for arg in base_args[len(head_args):]:
        if _is_literal(arg):
            return False
    return True


def _classify_pair(base: str, head: str) -> Verdict:
    """'binding-edit' or 'violation' for one base/head line pair that differs (Q2 allowlist)."""
    if ASSERT_RE.search(base) or ASSERT_RE.search(head):
        return "violation"
    cb = _parse_call(base)
    ch = _parse_call(head)
    if cb is None or ch is None:
        return "violation"
    prefix_b, callee_b, args_b, suffix_b = cb
    prefix_h, callee_h, args_h, suffix_h = ch
    if prefix_b != prefix_h or suffix_b != suffix_h:
        return "violation"
    if not _args_allowed(args_b, args_h):
        return "violation"
    if callee_b == callee_h and len(args_b) == len(args_h):
        return "violation"
    return "binding-edit"


def _assertion_flags(texts: List[str]) -> List[bool]:
    """Which lines are assertion lines: assert lines, lines inside an open assertion call (its
    parentheses not yet closed), and method-chain continuations (first non-space char is '.')."""
    flags: List[bool] = []
    depth = 0
    for text in texts:
        marked = depth > 0 or ASSERT_RE.search(text) is not None or text.lstrip().startswith(".")
        flags.append(marked)
        if marked:
            depth = max(0, depth + text.count("(") - text.count(")"))
    return flags


def _entries(lang: Optional[Lang], lines: List[str], span: Span) -> List[Tuple[int, str, bool]]:
    """(1-based line number, text, is_decorator) for each significant line in a span."""
    dec_start, def_idx, end = span
    return [(i + 1, lines[i], i < def_idx)
            for i in range(dec_start, end + 1) if _significant(lang, lines[i])]


def _is_def(lang: Optional[Lang], line: str) -> bool:
    """Whether the line is a definition header (a signature line, never a call site)."""
    return lang is not None and DEF_RES[lang].match(line) is not None


def _body_changes(lang: Optional[Lang], base_lines: List[str], head_lines: List[str],
                  bspan: Span, hspan: Span) -> List[Tuple[Verdict, int]]:
    """(verdict, line number) for each changed significant line inside one test span."""
    b = _entries(lang, base_lines, bspan)
    h = _entries(lang, head_lines, hspan)
    btexts = [t for _, t, _ in b]
    htexts = [t for _, t, _ in h]
    bflags = _assertion_flags(btexts)
    hflags = _assertion_flags(htexts)
    matcher = difflib.SequenceMatcher(None, btexts, htexts, autojunk=False)
    out: List[Tuple[Verdict, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                _, bt, bdeco = b[i1 + k]
                hn, ht, hdeco = h[j1 + k]
                guarded = (bdeco or hdeco or bflags[i1 + k] or hflags[j1 + k]
                           or _is_def(lang, bt) or _is_def(lang, ht))
                verdict: Verdict = "violation" if guarded else _classify_pair(bt, ht)
                out.append((verdict, hn))
        else:
            for bn, _, _ in b[i1:i2]:
                out.append(("violation", bn))
            for hn, _, _ in h[j1:j2]:
                out.append(("violation", hn))
    return out


def _import_changes(lang: Optional[Lang], base_lines: List[str], head_lines: List[str]) -> List[int]:
    """Line numbers of import lines that changed anywhere in the file (always binding-edit)."""
    b = [(i + 1, t) for i, t in enumerate(base_lines) if _is_import(lang, t)]
    h = [(i + 1, t) for i, t in enumerate(head_lines) if _is_import(lang, t)]
    matcher = difflib.SequenceMatcher(None, [t for _, t in b], [t for _, t in h], autojunk=False)
    out: List[int] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.extend(n for n, _ in (h[j1:j2] or b[i1:i2]))
    return out


def _outside(idx: int, lines: List[str], bodies: List[Tuple[int, int]],
             import_flags: List[bool]) -> bool:
    if not lines[idx].strip() or import_flags[idx]:
        return False
    return not any(s <= idx <= e for s, e in bodies)


def _support_edit(path: str, base_text: Optional[str], head_text: Optional[str]) -> bool:
    lang = _lang(path)
    bl = _lines(base_text)
    hl = _lines(head_text)
    bspans, bsized = _test_spans(lang, bl)
    hspans, hsized = _test_spans(lang, hl)
    if not (bsized and hsized):
        return True
    bb = [(s, e) for s, _, e in bspans]
    hb = [(s, e) for s, _, e in hspans]
    bi = [_is_import(lang, t) for t in bl]
    hi = [_is_import(lang, t) for t in hl]
    matcher = difflib.SequenceMatcher(None, bl, hl, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if any(_outside(i, bl, bb, bi) for i in range(i1, i2)):
            return True
        if any(_outside(j, hl, hb, hi) for j in range(j1, j2)):
            return True
    return False


def _split_test_id(value: str) -> Tuple[str, Optional[str], str]:
    """(path, class or None, test name) for FILE::NAME or FILE::CLASS::NAME. Raises ValueError."""
    path, sep, rest = value.partition("::")
    segments = rest.split("::") if sep else []
    if not path or not segments or any(not s for s in segments):
        raise ValueError(f"expected FILE::NAME or FILE::CLASS::NAME, got {value!r}")
    cls = segments[-2] if len(segments) >= 2 else None
    return path, cls, segments[-1]


def _test_id_arg(value: str) -> str:
    try:
        _split_test_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _restore_line(tid: str, span: Span) -> str:
    return f"restore {tid} {span[0] + 1}-{span[2] + 1}"


def _check_test(tid: str, lang: Lang, base_lines: List[str], head_lines: List[str],
                bspan: Span, hspan: Span) -> Tuple[List[str], bool]:
    """Findings for one located test, and whether any of them is a violation."""
    out = [f"binding-edit {tid} {n}" for n in _import_changes(lang, base_lines, head_lines)]
    violated = False
    for verdict, n in _body_changes(lang, base_lines, head_lines, bspan, hspan):
        out.append(f"{verdict} {tid} {n}")
        if verdict == "violation":
            violated = True
    if violated:
        out.append(_restore_line(tid, bspan))
    return out, violated


def _norm(path: str) -> str:
    return os.path.normpath(path)


def cmd_fence(args: argparse.Namespace) -> int:
    _require_ref(args.base, allow_working=False)
    _require_ref(args.head, allow_working=True)
    findings: List[str] = []
    violated = False
    cache: Dict[str, Tuple[List[str], List[str]]] = {}
    for value in args.test:
        path, cls, name = _split_test_id(value)
        if path not in cache:
            cache[path] = (_lines(_read_at(args.base, path)), _lines(_read_at(args.head, path)))
        bl, hl = cache[path]
        lang = _lang(path)
        bspan, bwhy = _locate(lang, bl, name, cls)
        hspan, hwhy = _locate(lang, hl, name, cls)
        if lang is None or bspan is None or hspan is None:
            missing = [side for side, span in (("base", bspan), ("head", hspan)) if span is None]
            for side, why in (("base", bwhy), ("head", hwhy)):
                if side in missing:
                    print(f"error: {value} {side}: {why}", file=sys.stderr)
            findings.append(f"unlocated {value} {'/'.join(missing)}")
            findings.append(f"violation {value} 0")
            if bspan is not None:
                findings.append(_restore_line(value, bspan))
            violated = True
            continue
        test_out, bad = _check_test(value, lang, bl, hl, bspan, hspan)
        findings.extend(test_out)
        violated = violated or bad
    if args.impl_files:
        impl = {_norm(p) for p in args.impl_files}
        for path in _changed_paths(args.base, args.head):
            if _norm(path) in impl:
                continue
            if _support_edit(path, _read_at(args.base, path), _read_at(args.head, path)):
                findings.append(f"support-edit {path}")
    for line in findings:
        print(line)
    return 1 if violated else 0


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        _fail(str(exc))


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _survivor_rows(label: str, text: str) -> List[Tuple[int, str, str]]:
    """(line number, deleted cell, survivor cell) for each data row of the deletions table.

    The id and survivor-or-proof columns are located by header name, so extra columns and any
    column order are tolerated; a data row must have as many cells as the header."""
    if not text.strip():
        _fail(f"{label} is empty; expected a deletions table")
    table = [(n, line) for n, line in enumerate(text.splitlines(), 1) if "|" in line]
    if not table:
        _fail(f"{label}: no markdown table found")
    header_n, header = table[0]
    if len(table) < 2 or not all(TABLE_SEP_RE.match(c) for c in _cells(table[1][1])):
        _fail(f"{label} line {header_n}: table header is not followed by a separator row")
    names = [c.strip().lower() for c in _cells(header)]
    for required in (DELETION_ID_COL, DELETION_SURVIVOR_COL):
        if names.count(required) != 1:
            _fail(f"{label} line {header_n}: table header must name exactly one column {required!r}")
    id_col = names.index(DELETION_ID_COL)
    survivor_col = names.index(DELETION_SURVIVOR_COL)
    rows: List[Tuple[int, str, str]] = []
    for n, line in table[2:]:
        cells = _cells(line)
        if len(cells) != len(names) or not cells[id_col]:
            _fail(f"{label} line {n}: row cannot be parsed into {len(names)} columns: {line.strip()!r}")
        rows.append((n, cells[id_col], cells[survivor_col]))
    return rows


def _survivor_of(cell: str, label: str, n: int) -> Optional[str]:
    """The survivor test id named by a cell, or None for an exempt proof:/SHA cell."""
    value = cell.strip().strip("`").strip()
    if value.startswith("proof:") or SHA_RE.match(value):
        return None
    if "::" in value:
        return value
    _fail(f"{label} line {n}: survivor cell {cell!r} is not a test id, proof: marker, or commit SHA")


def _read_mapping(path: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for n, raw in enumerate(_read_text(path).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" -> ")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            _fail(f"{path} line {n}: expected '<old test id> -> <new test id>', got {line!r}")
        old, new = parts[0].strip(), parts[1].strip()
        if mapping.get(old, new) != new:
            _fail(f"{path} line {n}: conflicting mapping for {old}")
        mapping[old] = new
    return mapping


def cmd_survivors(args: argparse.Namespace) -> int:
    rows = _survivor_rows(args.deletions, _read_text(args.deletions))
    collected = {ln.strip() for ln in _read_text(args.collected).splitlines() if ln.strip()}
    mapping = _read_mapping(args.merged) if args.merged else {}
    missing = 0
    for n, deleted, cell in rows:
        survivor = _survivor_of(cell, args.deletions, n)
        if survivor is None:
            continue
        target = mapping.get(survivor, survivor)
        if target in collected:
            continue
        if target == survivor:
            print(f"missing-survivor {survivor} named-by {deleted}")
        else:
            print(f"missing-survivor {target} (renamed from {survivor}) named-by {deleted}")
        missing += 1
    return 1 if missing else 0


def _unquote_git_path(raw: str) -> Optional[str]:
    """Path from a git C-quoted name ("..." with \\ and octal escapes), or None if it is malformed."""
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        octal = body[i + 1:i + 4]
        if nxt in C_ESCAPES:
            out.extend(C_ESCAPES[nxt].encode("utf-8"))
            i += 2
        elif len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(int(octal, 8) & 0xFF)
            i += 4
        else:
            return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _diff_target(line: str) -> Optional[str]:
    """Repo-relative path named by a '+++ ' header line, or None for /dev/null (a deletion).
    Fails closed on any target that is not a b/ path, quoted or plain."""
    target = line[4:]
    if target.endswith("\t"):
        # git appends a tab after names that contain a space.
        target = target[:-1]
    if target == "/dev/null":
        return None
    if target.startswith('"'):
        unquoted = _unquote_git_path(target) if len(target) >= 2 and target.endswith('"') else None
        if unquoted is None:
            _fail(f"unparsable diff target: {line!r}")
        target = unquoted
    if not target.startswith("b/") or len(target) <= 2:
        _fail(f"unparsable diff target: {line!r}")
    return target[2:]


def _added_lines(diff: str) -> List[Tuple[str, int, str]]:
    """(path, new line number, text) for each added line of a unified diff."""
    out: List[Tuple[str, int, str]] = []
    current: Optional[str] = None
    new_line = 0
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_header = True
            current = None
            continue
        if in_header and line.startswith("+++ "):
            current = _diff_target(line)
            continue
        if line.startswith("@@"):
            in_header = False
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
            continue
        if line.startswith("+"):
            if current is not None:
                out.append((current, new_line, line[1:]))
            new_line += 1
    return out


def _added_tests(path: str, head: str, added_ns: List[int]) -> List[Tuple[int, str]]:
    """(start index, name) of each test definition in `path` at `head` whose span holds an added line."""
    lang = _lang(path)
    if lang is None:
        return []
    lines_list = _lines(_read_at(head, path))
    added0 = {n - 1 for n in added_ns}
    found: List[Tuple[int, str]] = []
    for start, name, is_test in _find_starts(lang, lines_list):
        if not is_test:
            continue
        span = _span_at(lang, lines_list, start)
        first, last = (span[0], span[2]) if span is not None else (start, start)
        if any(first <= k <= last for k in added0):
            found.append((start, name))
    return found


def cmd_names(args: argparse.Namespace) -> int:
    _require_ref(args.base, allow_working=False)
    _require_ref(args.head, allow_working=True)
    allow = {_norm(p) for p in args.allow}
    hits = 0

    if getattr(args, "hygiene", False):
        # Hygiene mode: check added test file paths and test names only
        segment_only_hits = []
        diff_args = ["diff", "--unified=0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", args.base]
        if args.head == "HEAD":
            diff_args.append("HEAD")

        # Check added test file paths (once per file) and the names of tests that gained lines
        added_by_path: Dict[str, List[int]] = {}
        for path, n, text in _added_lines(_git_ok(diff_args)):
            added_by_path.setdefault(path, []).append(n)
        for path, ns in added_by_path.items():
            if _norm(path) in allow:
                continue
            lang = _lang(path)
            if lang is None:
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            hit, segment_only = _hygiene_token_match(stem)
            if hit:
                print(f"spec-blind path {path}")
                hits += 1
            elif segment_only:
                segment_only_hits.append(f"segment-only: spec-blind path {path}")

            for start, name in _added_tests(path, args.head, ns):
                hit, segment_only = _hygiene_token_match(name)
                if hit:
                    print(f"spec-blind line {path}:{start + 1} test {name}")
                    hits += 1
                elif segment_only:
                    segment_only_hits.append(f"segment-only: spec-blind line {path}:{start + 1} test {name}")

        # Print segment-only hits after all hits
        for line in segment_only_hits:
            print(line)
    else:
        # Original spec-blind mode
        for path in _changed_paths(args.base, args.head):
            if _norm(path) not in allow and SPEC_RE.search(path):
                print(f"spec-blind path {path}")
                hits += 1
        # Explicit prefixes so the '+++ b/' header parse holds whatever diff.noprefix config says.
        diff_args = ["diff", "--unified=0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", args.base]
        if args.head == "HEAD":
            diff_args.append("HEAD")
        for path, n, text in _added_lines(_git_ok(diff_args)):
            if _norm(path) in allow or not SPEC_RE.search(text):
                continue
            print(f"spec-blind line {path}:{n} {text.strip()}")
            hits += 1
        if args.head == "WORKTREE":
            for path in _untracked_paths():
                if _norm(path) in allow:
                    continue
                for n, text in enumerate(_lines(_read_at("WORKTREE", path)), 1):
                    if SPEC_RE.search(text):
                        print(f"spec-blind line {path}:{n} {text.strip()}")
                        hits += 1

    return 1 if hits else 0


def cmd_working_ids(args: argparse.Namespace) -> int:
    """Print one file::name per line for every test definition added between base and head."""
    _require_ref(args.base, allow_working=False)
    _require_ref(args.head, allow_working=False)

    ids: List[str] = []
    diff_args = ["diff", "--unified=0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", args.base, args.head]

    added_by_path: Dict[str, List[int]] = {}
    for path, n, text in _added_lines(_git_ok(diff_args)):
        added_by_path.setdefault(path, []).append(n)
    for path, ns in added_by_path.items():
        for _, name in _added_tests(path, args.head, ns):
            ids.append(f"{path}::{name}")

    # Sort and deduplicate
    for test_id in sorted(set(ids)):
        print(test_id)

    return 0


def _is_test_path(path: str) -> bool:
    """True for a path that names a test file (by directory or by basename convention)."""
    parts = path.lower().replace("\\", "/").split("/")
    base = parts[-1]
    if any(seg in ("tests", "test", "__tests__", "spec") for seg in parts[:-1]):
        return True
    return (base.startswith("test_") or base.startswith("test.") or "_test." in base
            or ".test." in base or ".spec." in base or "_spec." in base)


def cmd_layout(args: argparse.Namespace) -> int:
    """List test files and names matching impl stems at a given SHA."""
    _require_ref(args.base, allow_working=False)

    impl_stems = set()
    for impl_path in args.impl_paths:
        stem = os.path.splitext(os.path.basename(impl_path))[0]
        impl_stems.add(stem.lower().replace("-", "_"))

    names_printed = 0
    truncated = False

    if args.impl_paths:
        # Get test files at SHA
        rc, out = _git(["ls-tree", "-r", "--name-only", args.base])
        if rc != 0:
            return 0

        test_files: List[str] = []
        for path in out.strip().split("\n"):
            if not path:
                continue
            if _lang(path) is not None and _is_test_path(path):
                stem = os.path.splitext(os.path.basename(path))[0]
                stem_normalized = stem.lower().replace("-", "_")
                if any(impl_stem in stem_normalized for impl_stem in impl_stems):
                    test_files.append(path)
                    print(path)

                    # Print test names
                    file_text = _read_at(args.base, path)
                    if file_text:
                        lines_list = _lines(file_text)
                        lang = _lang(path)
                        for start, name, is_test in _find_starts(lang, lines_list):
                            if is_test:
                                print(f"  {name}")
                                names_printed += 1
                                if names_printed >= args.max_names:
                                    truncated = True
                                    break
                    if truncated:
                        break

        # List sibling test files (up to 3 per directory)
        test_dirs: Dict[str, List[str]] = {}
        for path in out.strip().split("\n"):
            if not path or _lang(path) is None or not _is_test_path(path):
                continue
            dir_path = os.path.dirname(path)
            if dir_path not in test_dirs:
                test_dirs[dir_path] = []
            test_dirs[dir_path].append(os.path.basename(path))

        for dir_path, files in sorted(test_dirs.items()):
            if files and dir_path:
                # Show up to 3 sibling patterns
                for fname in sorted(files)[:3]:
                    if f"{dir_path}/{fname}" not in test_files:
                        print(f"{dir_path}/{fname}")
    else:
        # List paths only from working tree
        for path in _git_ok(["ls-files"]).strip().split("\n"):
            if path and _lang(path) is not None and _is_test_path(path):
                print(path)

    if truncated:
        print("(truncated)")

    return 0


_OVERLAP_LINE_RE = re.compile(r"^(?P<id>[^|\s][^|]*?)\s*\|\s*overlaps:\s*(?P<survivor>[^|]+?)\s*(?:\|.*)?$")


def cmd_overlaps(args: argparse.Namespace) -> int:
    """Parse `<id> | overlaps: <id|none>` lines from the reports and overwrite --out with the result."""
    try:
        added_ids = [ln.strip() for ln in _read_text(args.added_ids).split("\n") if ln.strip()]
    except OSError:
        _fail(f"cannot read {args.added_ids}")

    candidate_rows: List[str] = []
    nones = 0
    reported: Set[str] = set()
    unreported: Set[str] = set()

    for report_path in args.reports:
        try:
            report_text = _read_text(report_path)
        except OSError:
            continue  # a missing report is zero lines
        for raw in report_text.split("\n"):
            line = raw.strip().lstrip("-* ").strip("`").strip()
            if "overlaps" not in line:
                continue
            m = _OVERLAP_LINE_RE.match(line)
            if m is None:
                first = line.split()[0].strip("`") if line.split() else ""
                if first:
                    unreported.add(first)
                continue
            new_id = m.group("id").strip().strip("`").strip()
            survivor = m.group("survivor").strip().strip("`").strip()
            if not new_id or not survivor:
                unreported.add(new_id or line)
                continue
            reported.add(new_id)
            if survivor.lower() == "none":
                nones += 1
            else:
                candidate_rows.append(f"- {new_id} | overlaps: {survivor} | class-1 candidate")

    unreported -= reported
    unreported.update(i for i in added_ids if i not in reported)

    out_lines = candidate_rows + [f"- {i}" for i in sorted(unreported)]
    summary = f"candidates: {len(candidate_rows)}  none: {nones}  unreported: {len(unreported)}"
    out_lines.append(summary)
    try:
        with open(args.out, "w") as f:
            f.write("\n".join(out_lines) + "\n")
    except OSError as e:
        _fail(str(e))
    print(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic checks for /implement-with-haiku spec-blind tests.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fence = sub.add_parser("fence", help="binding-edit fence over test ids")
    fence.add_argument("--base", required=True, help="base SHA (pre-Round-3)")
    fence.add_argument("--head", required=True, help="SHA, INDEX or WORKTREE")
    fence.add_argument("--test", action="append", default=[], type=_test_id_arg,
                       help="FILE::NAME or FILE::CLASS::NAME (repeatable)")
    fence.add_argument("--impl-files", action="extend", nargs="+", default=[],
                       help="implementation paths (not support edits)")

    surv = sub.add_parser("survivors", help="named survivors exist in the collected list")
    surv.add_argument("--deletions", required=True)
    surv.add_argument("--collected", required=True)
    surv.add_argument("--merged", default=None,
                      help="pre-merge -> post-merge id mapping, one '<old> -> <new>' per line")

    names = sub.add_parser("names", help="no spec-blind name in the run's working set")
    names.add_argument("--hygiene", action="store_true", help="match hygiene tokens in test file stems and names")
    names.add_argument("--base", required=True)
    names.add_argument("--head", choices=["WORKTREE", "HEAD"], default="WORKTREE",
                       help="WORKTREE (default: working tree plus untracked files) or HEAD")
    names.add_argument("--allow", action="extend", nargs="+", default=[], help="exempt paths")

    # working-ids subcommand
    working_ids = sub.add_parser("working-ids", help="test ids added between base and head")
    working_ids.add_argument("--base", required=True, help="base SHA")
    working_ids.add_argument("--head", required=True, help="head ref")

    # layout subcommand
    layout = sub.add_parser("layout", help="test files and names matching impl stems at SHA")
    layout.add_argument("--base", required=True, help="base SHA (commit only, not INDEX/WORKTREE)")
    layout.add_argument("--impl", action="extend", nargs="+", default=[], dest="impl_paths",
                        help="implementation paths (repeatable)")
    layout.add_argument("--max-names", type=int, default=60, help="max test names per output (default 60)")

    # overlaps subcommand
    overlaps = sub.add_parser("overlaps", help="parse overlap classifications from reports")
    overlaps.add_argument("--report", action="append", default=[], dest="reports",
                          help="report file paths (repeatable)")
    overlaps.add_argument("--added-ids", required=True, help="file with added test ids")
    overlaps.add_argument("--out", required=True, help="output file path")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "fence":
        return cmd_fence(args)
    if args.cmd == "survivors":
        return cmd_survivors(args)
    if args.cmd == "names":
        return cmd_names(args)
    if args.cmd == "working-ids":
        return cmd_working_ids(args)
    if args.cmd == "layout":
        return cmd_layout(args)
    if args.cmd == "overlaps":
        return cmd_overlaps(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
