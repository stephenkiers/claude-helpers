#!/usr/bin/env python3
"""test-cleanup-check.py: deterministic checks for /implement-with-haiku's spec-blind test flow (#458).

Usage:
    test-cleanup-check.py fence --base SHA --head SHA|INDEX|WORKTREE
                          --test FILE::[CLASS::]NAME [--test ...]
                          [--impl-files PATH [PATH ...]]
    test-cleanup-check.py survivors --deletions FILE --collected FILE [--merged FILE]
    test-cleanup-check.py names [--hygiene] --base SHA [--head WORKTREE|HEAD] [--allow PATH [PATH ...]]
    test-cleanup-check.py working-ids --base SHA --head REF
    test-cleanup-check.py layout [--base SHA] [--impl PATH [PATH ...]] [--max-names N]
    test-cleanup-check.py overlaps --report PATH [--report ...] --added-ids PATH --out PATH
    test-cleanup-check.py layout-evidence --step implement|fix (--flow-dir DIR | --scratch DIR)
                          [--impl PATH [PATH ...]] --out FILE [--names-given-file FILE]
    test-cleanup-check.py hygiene-json --scratch DIR --step implement|fix [--layout-names-given-file FILE]
                          [--skip-round2] [--skip-round4] [--flow-dir DIR] --out FILE

Output paths (overlaps --out, hygiene-json --out, layout-evidence --out and --names-given-file) are
confined to the repo toplevel, FLOW_DIR (the environment variable, or the --flow-dir the subcommand
was given), or the system temp dir, where the run's scratch directory lives (mktemp -d).

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
  phase_N, wave_N) as a `_`-bounded suffix or entire value. Only ADDED test files count for the
  path check (absent at SHA; a framework .spec/.test infix is dropped from the stem first), and only
  test definitions whose name is absent at SHA count for the name check (an edited test is not
  added). Untracked files count as added under the default WORKTREE head. Hits print one per line in
  the same format; segment-only matches (tokens appearing only as inner segments) print after all
  hits, each prefixed "segment-only:", and never affect exit code.
  Plain names (no flag) behaves exactly as before (spec-blind only). In --hygiene mode an error
  (bad ref, failed git call) exits 3, so a caller can tell an error from hits (exit 1).

working-ids: print one file::name per line for every test definition ADDED between SHA and REF
  (used for change tracking), sorted and de-duplicated. Only test files count, and an edited test
  whose name already existed at SHA is not printed. Exit 0 on success (even with no output), 1 on
  git failure/unreadable input.

layout: from git ls-tree / git show AT SHA ONLY, list test files that match the module stem of any
  --impl path (stem = basename without extension, - and _ equivalent). A stem-exact match (test_parser,
  parser_test, parser.spec all name module "parser") is preferred; substring matches are used only
  when no exact match exists. Each matched file is followed by its test-definition names. Also list up
  to 3 sibling test file paths per test directory as naming patterns. Never prints any line of a test
  body, only paths and names. A missing path at SHA yields zero names and exit 0. A git ls-tree
  failure exits 1. Output capped by --max-names (default 60 names); "(truncated)" is printed only when
  a name was actually cut off. If --base is omitted, lists test paths only (from the working tree)
  and no names (the listing is of the repo toplevel, whatever the current directory).

layout-evidence: the layout step in one call. Resolves layout_base_sha by running unit-progress.py
  (next to this script) with the global flags first, then `layout-base`. On exit 0 with a SHA, runs
  the base-aware layout; otherwise (no usable SHA, or the base-aware layout fails) runs the base-less
  layout. Writes the layout output to --out and `true` (base-aware, names given) or `false` to the
  names-given file (default <flow-dir or scratch>/layout-names-given.txt). Prints --out on success.

overlaps: parse lines of the form "id | overlaps: id or none" from --report files (repeatable;
  missing or unreadable file counts as zero lines, not error). Strip surrounding whitespace and list
  markers. Only ids in --added-ids are considered; each id counts once, and "none" (any case) is a
  no-overlap verdict. Overwrite --out in full on every call. --out is confined to the repo toplevel,
  FLOW_DIR, or the system temp dir (an absolute path must resolve under one of them). Write one markdown row per
  non-"none" overlap, tagged "class-1 candidate": "- <new id> | overlaps: <survivor id> | class-1
  candidate". Unparseable lines and ids in --added-ids with no parseable report line are listed
  under the "unreported:" heading as "- <id>". Last line is summary: "candidates: <C>  none: <K>
  unreported: <U>". Print summary to stdout. Exit 0 on success, 1 if --added-ids or --out cannot be
  read/written or --out is outside the confinement.

hygiene-json: file-only (no git). Reads the Round 2 and Round 4 hygiene evidence, the overlap
  candidates summary, the Round 4 round4.md summary and the layout-names-given flag from --scratch,
  and writes one JSON object to --out (json.dump): round2_hygiene_hits, round2_segment_only,
  round4_hygiene_hits, round4_segment_only, overlap_candidates, overlap_none, overlap_unreported,
  round4_deleted, round4_relocated, layout_names_given, step. Missing or unavailable evidence is
  null (counts default to 0 for round4.md). A `hygiene-error:` first line in a hygiene file also
  gives null. --skip-round2 / --skip-round4 force the fields of a round the run did not re-produce
  to null, ignoring any stale file from an earlier run (--skip-round4 covers the Round 4 hygiene
  fields and the overlap fields). --out is confined as above. Prints the --out path on success. Exit 2 when --scratch
  is not a directory.

empty-suites --root DIR FILE...: read-only, writes nothing. Finds test units left empty by a deletion.
  JS/TS (.js .jsx .ts .tsx .mjs .cjs .mts .cts): a describe/suite block whose body holds only whitespace,
  comments, hooks and other empty suites is "empty-suite" (action remove). A file whose remaining code is only
  imports and hooks is "empty-file" (delete-file) when nothing imports it, else "empty-file-unsafe" (stop); other
  top-level code also makes it unsafe. A suite the scanner cannot prove empty is "kept-suspicious" (report).
  pytest (.py): a top-level Test* class with no base other than object, no test* method and no nested class is
  "empty-class" (report). Go (_test.go): no Test/Benchmark/Fuzz/Example function is "empty-go-test-file" (report).
  Prints one JSON object {"findings": [...]} with keys file, line, end_line, stack, kind, name, action, reason,
  imported_by, text. Exit 1 if any finding is remove, delete-file or stop; else 0. Exit 2 when a FILE is outside
  --root or cannot be read. A FILE that no longer exists is skipped with a stderr note, so a re-run after a
  delete-file exits 0; a FILE with no rule for its type (for example .md) is skipped the same way.

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
import contextlib
import difflib
import io
import json
import os
import posixpath
import tempfile
import re
import subprocess
import sys
from typing import Dict, List, Literal, NamedTuple, NoReturn, Optional, Pattern, Sequence, Set, Tuple, TypedDict

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


def _token_pattern(token: str) -> str:
    """Regex body for one hygiene token, matched against a lower-case, underscore-normalised value."""
    if token in ("phase_N", "wave_N"):
        return r"(?:phase|wave)_(?:\d+|[a-z])"
    return re.escape(token.lower())


def _hygiene_token_match(stem_or_name: str) -> Tuple[bool, bool]:
    """Match hygiene tokens in a stem or test name.

    Returns (hit, segment_only):
    - hit: a token is the entire value or a `_`-bounded suffix ("test_parser_hardening", "respec" is not)
    - segment_only: a token is an inner `_`-bounded segment ("test_spec_loader")

    Two separate passes: every token is tried as a hit before any segment-only match is reported, so
    an inner segment never hides a suffix (e.g. "test_spec_hardening" is a hit, not segment-only).
    """
    normalized = stem_or_name.lower().replace("-", "_")
    for token in HYGIENE_TOKENS:
        if re.search(rf"(?:^|_){_token_pattern(token)}$", normalized):
            return True, False
    for token in HYGIENE_TOKENS:
        if re.search(rf"(?:^|_){_token_pattern(token)}(?=_|$)", normalized):
            return False, True
    return False, False


def _hygiene_stem(path: str) -> str:
    """Basename without its extension and without a framework .spec/.test infix (foo.spec.ts -> foo)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r"\.(?:spec|test)$", "", stem, flags=re.IGNORECASE)


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


def _confine_output(path: str, extra_roots: Sequence[str] = ()) -> None:
    """Fail closed unless an output path lies inside the repo toplevel, $FLOW_DIR (when set), the
    system temp dir (where the run's scratch lives) or one of extra_roots.

    A relative path goes through _confine. An absolute path (the flow passes its scratch directory)
    must resolve under one of those roots; anything else is rejected."""
    if not os.path.isabs(path):
        _confine(path)
        return
    full = os.path.realpath(path)
    roots = [os.path.realpath(_toplevel())]
    flow_dir = os.environ.get("FLOW_DIR", "")
    if flow_dir:
        roots.append(os.path.realpath(flow_dir))
    roots.extend(os.path.realpath(r) for r in extra_roots if r)
    # the run's scratch directory comes from mktemp -d, i.e. under the system temp dir
    roots.append(os.path.realpath(tempfile.gettempdir()))
    if not any(full.startswith(root + os.sep) for root in roots):
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


def _hygiene_added_paths(base: str, head: str) -> Dict[str, List[int]]:
    """Added line numbers per changed path for the hygiene scan. Under WORKTREE, untracked files count
    as added in full. A changed path with no added lines (a deletion, or a new empty file) maps to []."""
    diff_args = ["diff", "--unified=0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", base]
    if head == "HEAD":
        diff_args.append("HEAD")
    added: Dict[str, List[int]] = {}
    for path in _diff_paths(base, head):
        added.setdefault(path, [])
    for path, n, _text in _added_lines(_git_ok(diff_args)):
        added.setdefault(path, []).append(n)
    if head == "WORKTREE":
        for path in _untracked_paths():
            count = len(_lines(_read_at("WORKTREE", path)))
            added.setdefault(path, []).extend(range(1, count + 1))
    return added


def _added_test_names(path: str, base: str, head: str, added_ns: List[int]) -> List[Tuple[int, str]]:
    """(start index, name) of each test definition ADDED between base and head in a test file.

    A definition counts when the file is a test path and the name is absent from the file at base
    (an edited test that already existed is not added), and its span holds an added line."""
    lang = _lang(path)
    if lang is None or not _is_test_path(path):
        return []
    base_text = _read_at(base, path)
    base_names: Set[str] = set()
    if base_text is not None:
        base_names = {name for _, name, is_test in _find_starts(lang, _lines(base_text)) if is_test}
    return [(start, name) for start, name in _added_tests(path, head, added_ns) if name not in base_names]


def cmd_names(args: argparse.Namespace) -> int:
    _require_ref(args.base, allow_working=False)
    _require_ref(args.head, allow_working=True)
    allow = {_norm(p) for p in args.allow}
    hits = 0

    if getattr(args, "hygiene", False):
        # Hygiene mode: added test file paths (new at base) and added test-definition names only
        segment_only_hits: List[str] = []
        added = _hygiene_added_paths(args.base, args.head)
        for path in sorted(added):
            if _norm(path) in allow or _lang(path) is None or not _is_test_path(path):
                continue
            if _read_at(args.base, path) is None:
                hit, segment_only = _hygiene_token_match(_hygiene_stem(path))
                if hit:
                    print(f"spec-blind path {path}")
                    hits += 1
                elif segment_only:
                    segment_only_hits.append(f"segment-only: spec-blind path {path}")
            for start, name in _added_test_names(path, args.base, args.head, added[path]):
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
    for path, n, _text in _added_lines(_git_ok(diff_args)):
        added_by_path.setdefault(path, []).append(n)
    for path in sorted(added_by_path):
        for _, name in _added_test_names(path, args.base, args.head, added_by_path[path]):
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


def _layout_core(stem: str) -> str:
    """The module a test file's stem names: test_parser, parser_test and parser.spec all give parser."""
    core = re.sub(r"\.(?:spec|test)$", "", stem.lower().replace("-", "_"))
    core = re.sub(r"^tests?_", "", core)
    return re.sub(r"_tests?$", "", core)


def cmd_layout(args: argparse.Namespace) -> int:
    """List test files and names matching impl stems. With --base, reads only from that SHA; without
    it, lists the test paths of the working tree and no names."""
    if args.base is None:
        for path in _git_ok(["-C", _toplevel(), "ls-files"]).strip().split("\n"):
            if path and _lang(path) is not None and _is_test_path(path):
                print(path)
        return 0

    _require_ref(args.base, allow_working=False)
    impl_stems = {os.path.splitext(os.path.basename(p))[0].lower().replace("-", "_") for p in args.impl_paths}

    # Test files at SHA: exact (module-named) matches are preferred; substring matches are the fallback.
    listing = _git_ok(["ls-tree", "-r", "--name-only", args.base])
    exact: List[str] = []
    substring: List[str] = []
    for path in listing.strip().split("\n"):
        if not path or _lang(path) is None or not _is_test_path(path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        if _layout_core(stem) in impl_stems:
            exact.append(path)
        elif any(impl_stem in stem.lower().replace("-", "_") for impl_stem in impl_stems):
            substring.append(path)
    matched = exact if exact else substring

    names_printed = 0
    truncated = False
    for path in matched:
        if truncated:
            break
        print(path)
        file_text = _read_at(args.base, path)
        names = [name for _, name, is_test in _find_starts(_lang(path), _lines(file_text)) if is_test]
        for name in names:
            if names_printed >= args.max_names:
                truncated = True
                break
            print(f"  {name}")
            names_printed += 1

    # List sibling test files (up to 3 per directory)
    test_dirs: Dict[str, List[str]] = {}
    for path in listing.strip().split("\n"):
        if not path or _lang(path) is None or not _is_test_path(path):
            continue
        test_dirs.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
    for dir_path, files in sorted(test_dirs.items()):
        if files and dir_path:
            # Show up to 3 sibling patterns
            for fname in sorted(files)[:3]:
                if f"{dir_path}/{fname}" not in matched:
                    print(f"{dir_path}/{fname}")

    if truncated:
        print("(truncated)")

    return 0


_OVERLAP_LINE_RE = re.compile(r"^(?P<id>[^|\s][^|]*?)\s*\|\s*overlaps:\s*(?P<survivor>[^|]+?)\s*(?:\|.*)?$")


def _is_none_survivor(survivor: str) -> bool:
    """A survivor cell that says there is no overlap ("none", any case, with stray punctuation)."""
    return survivor.strip("`*_. ").lower() == "none"


def cmd_overlaps(args: argparse.Namespace) -> int:
    """Parse `<id> | overlaps: <id|none>` lines from the reports and overwrite --out with the result."""
    _confine_output(args.out)
    added = {ln.strip() for ln in _read_text(args.added_ids).split("\n") if ln.strip()}

    # verdict per added id: a survivor id, or "none". First-seen order is kept; a non-none verdict
    # from any report beats a none verdict for the same id.
    verdicts: Dict[str, str] = {}
    unreported: Set[str] = set()

    for report_path in args.reports:
        try:
            with open(report_path, encoding="utf-8", errors="replace") as fh:
                report_text = fh.read()
        except OSError:
            continue  # a missing or unreadable report is zero lines
        for raw in report_text.split("\n"):
            line = raw.strip().lstrip("-* ").strip("`").strip()
            if "overlaps" not in line:
                continue
            m = _OVERLAP_LINE_RE.match(line)
            new_id = m.group("id").strip().strip("`").strip() if m else ""
            survivor = m.group("survivor").strip().strip("`").strip() if m else ""
            if not new_id or not survivor:
                first = line.split()[0].strip("`") if line.split() else ""
                if first in added:
                    unreported.add(first)
                continue
            if new_id not in added:
                continue
            verdict = "none" if _is_none_survivor(survivor) else survivor
            prior = verdicts.get(new_id)
            if prior is None or (prior == "none" and verdict != "none"):
                verdicts[new_id] = verdict

    unreported |= {i for i in added if i not in verdicts}
    unreported -= set(verdicts)

    rows = [f"- {i} | overlaps: {v} | class-1 candidate" for i, v in verdicts.items() if v != "none"]
    nones = sum(1 for v in verdicts.values() if v == "none")
    summary = f"candidates: {len(rows)}  none: {nones}  unreported: {len(unreported)}"
    out_lines = rows + ["unreported:"] + [f"- {i}" for i in sorted(unreported)] + [summary]
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    except OSError as e:
        _fail(str(e))
    print(summary)
    return 0


SEGMENT_PREFIX = "segment-only:"
ROUND4_CANDIDATES_FILE = "round4-candidates.md"


def _existing_file(scratch: str, names: Sequence[str]) -> Optional[str]:
    for name in names:
        path = os.path.join(scratch, name)
        if os.path.isfile(path):
            return path
    return None


HYGIENE_ERROR_PREFIX = "hygiene-error:"


def _round2_hygiene(scratch: str) -> Tuple[Optional[int], Optional[int]]:
    """(hits, segment_only) from round2-hygiene.txt (or .md). Missing or unavailable gives (None, None).

    A `round2-hygiene: N hits` summary header gives N; otherwise hits are the non-segment-only lines."""
    path = _existing_file(scratch, ("round2-hygiene.txt", "round2-hygiene.md"))
    if path is None:
        return None, None
    lines = _lines(_read_text(path))
    segment_only = sum(1 for ln in lines if ln.startswith(SEGMENT_PREFIX))
    first = lines[0] if lines else ""
    if first.startswith(HYGIENE_ERROR_PREFIX):
        return None, None
    is_summary = first.startswith("round2-hygiene:")
    tail = first.rsplit(": ", 1)[-1].strip() if is_summary else first.strip()
    if tail.startswith("unavailable"):
        return None, None
    if is_summary:
        digits = re.search(r"\d+", tail)
        if digits is not None:
            return int(digits.group()), segment_only
    hits = sum(1 for ln in lines if ln.strip() and not ln.startswith(SEGMENT_PREFIX))
    return hits, segment_only


def _round4_hygiene(scratch: str) -> Tuple[Optional[int], Optional[int]]:
    """(hits, segment_only) from round4-hygiene.txt. Missing gives (None, None)."""
    path = os.path.join(scratch, "round4-hygiene.txt")
    if not os.path.isfile(path):
        return None, None
    lines = [ln for ln in _lines(_read_text(path)) if ln.strip()]
    if lines and lines[0].startswith(HYGIENE_ERROR_PREFIX):
        return None, None
    segment_only = sum(1 for ln in lines if ln.startswith(SEGMENT_PREFIX))
    return len(lines) - segment_only, segment_only


def _round4_candidates(scratch: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(candidates, none, unreported) from the `candidates:` summary line. Absent gives all None."""
    path = os.path.join(scratch, ROUND4_CANDIDATES_FILE)
    summary: Optional[str] = None
    if os.path.isfile(path):
        summary = next((ln for ln in _lines(_read_text(path)) if ln.startswith("candidates:")), None)
    values: List[Optional[int]] = []
    for key in ("candidates", "none", "unreported"):
        found = re.search(rf"\b{key}: (\d+)", summary) if summary is not None else None
        values.append(int(found.group(1)) if found is not None else None)
    return values[0], values[1], values[2]


def _round4_counts(scratch: str) -> Tuple[int, int]:
    """(deleted, relocated) from the first line `round4: <D> deleted, <R> relocated`; default 0."""
    path = os.path.join(scratch, "round4.md")
    if not os.path.isfile(path):
        return 0, 0
    first = (_lines(_read_text(path)) or [""])[0]
    deleted = re.search(r"(\d+) deleted", first)
    relocated = re.search(r"(\d+) relocated", first)
    return (int(deleted.group(1)) if deleted else 0), (int(relocated.group(1)) if relocated else 0)


def cmd_hygiene_json(args: argparse.Namespace) -> int:
    """Write the per-run test hygiene measurement JSON from the scratch evidence files (file-only)."""
    scratch = args.scratch
    if not os.path.isdir(scratch):
        print(f"error: --scratch is not a directory: {scratch}", file=sys.stderr)
        return 2
    _confine_output(args.out, [args.flow_dir] if args.flow_dir else [])
    names_file = args.layout_names_given_file or os.path.join(scratch, "layout-names-given.txt")
    layout_given = os.path.isfile(names_file) and _read_text(names_file).strip() == "true"

    r2_hits, r2_segment = (None, None) if args.skip_round2 else _round2_hygiene(scratch)
    r4_hits, r4_segment = (None, None) if args.skip_round4 else _round4_hygiene(scratch)
    cand, none, unreported = (None, None, None) if args.skip_round4 else _round4_candidates(scratch)
    deleted, relocated = _round4_counts(scratch)
    measurement = {
        "round2_hygiene_hits": r2_hits,
        "round2_segment_only": r2_segment,
        "round4_hygiene_hits": r4_hits,
        "round4_segment_only": r4_segment,
        "overlap_candidates": cand,
        "overlap_none": none,
        "overlap_unreported": unreported,
        "round4_deleted": deleted,
        "round4_relocated": relocated,
        "layout_names_given": layout_given,
        "step": args.step,
    }
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(measurement, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        _fail(str(exc))
    print(args.out)
    return 0


def _capture_layout(base: Optional[str], impl_paths: List[str]) -> Optional[str]:
    """Output of cmd_layout, or None when it fails (a failed layout never partially counts)."""
    ns = argparse.Namespace(base=base, impl_paths=impl_paths, max_names=60)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cmd_layout(ns)
    except SystemExit:
        return None
    return buf.getvalue() if rc == 0 else None


def _resolve_layout_base(step: str, flow_dir: Optional[str], scratch: Optional[str]) -> Optional[str]:
    """layout_base_sha from unit-progress.py layout-base (global flags first); None when unusable."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit-progress.py")
    where = ["--flow-dir", flow_dir] if flow_dir else ["--scratch", scratch or ""]
    proc = subprocess.run([sys.executable, script, "--step", step, *where, "layout-base"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    value = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        return None
    return value


def cmd_layout_evidence(args: argparse.Namespace) -> int:
    """Write the layout evidence and the names-given flag for the spec-blind test author prompt."""
    where = args.flow_dir or args.scratch
    extra = [args.flow_dir] if args.flow_dir else []
    names_file = args.names_given_file or os.path.join(where, "layout-names-given.txt")
    _confine_output(args.out, extra)
    _confine_output(names_file, extra)
    base = _resolve_layout_base(args.step, args.flow_dir, args.scratch)
    evidence = _capture_layout(base, args.impl_paths) if base else None
    names_given = evidence is not None
    if evidence is None:
        evidence = _capture_layout(None, args.impl_paths)
    if evidence is None:
        _fail("layout failed")
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(evidence)
        with open(names_file, "w", encoding="utf-8") as fh:
            fh.write("true\n" if names_given else "false\n")
    except OSError as exc:
        _fail(str(exc))
    print(args.out)
    return 0


# ---------------------------------------------------------------------------
# empty-suites (#498): read-only detection of test units left empty by a deletion.
# One rule table covers JS/TS (describe suites and emptied files), pytest (empty Test* classes)
# and Go (_test.go files with no test functions). Only JS findings are removable.
# ---------------------------------------------------------------------------

JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
JS_REGEX_KEYWORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
                     "case", "do", "else", "yield", "await"}
SUITE_MODIFIERS = {"skip", "only", "todo", "concurrent", "sequential", "shuffle", "each", "skipIf", "runIf"}
# Playwright's test.describe is a suite, so its callee is matched as one and never as a test call.
SUITE_CALLEE_RE = re.compile(r"(?<![\w$.])(?:test\s*\.\s*describe|describe|suite|fdescribe|xdescribe)(?![\w$])")
SUITE_HEAD_RE = re.compile(r"(?:test\s*\.\s*)?(?:describe|suite|fdescribe|xdescribe)((?:\s*\.\s*\w+)*)\s*")
CALLBACK_RE = re.compile(
    r"(?:async\s+)?(?:function\b\s*\*?\s*[\w$]*\s*\([^()]*\)|\([^()]*\)\s*=>|[\w$]+\s*=>)\s*\{"
)
# A test call. test.describe is excluded: it is a suite (see SUITE_CALLEE_RE).
TEST_CALL_RE = re.compile(r"(?<![\w$.])(?:it|test|fit|xit|xtest)(?!\s*\.\s*describe\b)(?:\s*\.\s*\w+)*\s*\(")
HOOK_RE = re.compile(r"(?<![\w$.])(?:beforeEach|afterEach|beforeAll|afterAll)\s*\(")
IMPORT_STMT_RE = re.compile(r"(?<![\w$.])import\b(?!\s*\()(?:[^;{]|\{[^}]*\})*?(?:;|$)", re.M)
IMPORT_SPEC_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+)(['"])([^'"\n]+)\1""")
GO_TEST_FUNC_RE = re.compile(r"^func\s+(?:Test|Benchmark|Fuzz|Example)\w*\s*\(", re.M)
# Go test frameworks whose spec files may hold their tests outside a Test function.
GO_FRAMEWORK_RE = re.compile(r'"github\.com/(?:onsi/(?:ginkgo|gomega)|stretchr/testify)')

FindingKind = Literal["empty-suite", "kept-suspicious", "empty-file", "empty-file-unsafe", "empty-class",
                      "empty-go-test-file"]
FindingAction = Literal["remove", "delete-file", "stop", "report"]


class Finding(TypedDict):
    """One empty-suites finding, as printed in the JSON output.

    A block is identified by file, line and end_line. text is its source, for display only: identical
    blocks (the same helper copied twice) share their text, so a consumer that removes a block must
    match file, line and end_line together, never text alone.
    """
    file: str
    line: int
    end_line: int
    stack: str
    kind: FindingKind
    name: Optional[str]
    action: FindingAction
    reason: Optional[str]
    imported_by: List[str]
    text: str


class JsSuite(NamedTuple):
    start: int  # index of the callee in the masked text
    end: int  # index of the ')' that closes the outermost call
    name: Optional[str]
    each: bool
    body: Optional[Tuple[int, int]]  # (index of '{', index of its matching '}')


def _new_suite(start: int, end: int, name: Optional[str], each: bool,
               body: Optional[Tuple[int, int]]) -> JsSuite:
    """A JsSuite whose invariants hold: it ends at or after its start, and a body is a brace pair
    (open before close) lying inside the call."""
    if end < start:
        raise ValueError("suite ends before it starts")
    if body is not None and not (start <= body[0] < body[1] <= end):
        raise ValueError("suite body lies outside its call")
    return JsSuite(start, end, name, each, body)


def _finding(rel: str, line: int, end_line: int, stack: str, kind: FindingKind, name: Optional[str],
             action: FindingAction, reason: Optional[str], imported_by: List[str], text: str) -> Finding:
    return {"file": rel, "line": line, "end_line": end_line, "stack": stack, "kind": kind, "name": name,
            "action": action, "reason": reason, "imported_by": imported_by, "text": text}


def _total_lines(src: str) -> int:
    return len(src.splitlines()) or 1


def _block_text(lines: List[str], line: int, end_line: int) -> str:
    return "\n".join(lines[line - 1:end_line])


def _line_of(src: str, index: int) -> int:
    return src.count("\n", 0, index) + 1


def _blank(text: str, spans: List[Tuple[int, int]]) -> str:
    """Replace each [a, b) span with spaces, keeping newlines so line numbers stay true."""
    chars = list(text)
    for a, b in spans:
        for k in range(max(a, 0), min(b, len(chars))):
            if chars[k] != "\n":
                chars[k] = " "
    return "".join(chars)


def _js_close(text: str, i: int) -> Optional[int]:
    """Index of the bracket closing the one at text[i], or None when the brackets do not balance."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: List[str] = []
    j = i
    while j < len(text):
        c = text[j]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack.pop() != c:
                return None
            if not stack:
                return j
        j += 1
    return None


def _js_split_args(text: str, lo: int, hi: int) -> List[Tuple[int, int]]:
    """Top-level comma-separated ranges in text[lo:hi]; empty when the span is blank."""
    if text[lo:hi].strip() == "":
        return []
    ranges: List[Tuple[int, int]] = []
    depth = 0
    start = lo
    for j in range(lo, hi):
        c = text[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            ranges.append((start, j))
            start = j + 1
    ranges.append((start, hi))
    return ranges


def _js_regex_ok(last: str) -> bool:
    """Whether a '/' after the previous significant token starts a regex literal (not division).

    Heuristic limit: JSX and TSX text is lexed as code. An apostrophe or backtick in JSX text
    (`<p>Don't</p>`) starts a masked literal that runs to the end of its line, and a '/' in JSX text
    is read by this same previous-token rule. Either can hide a brace or paren. A suite whose braces
    then fail to balance is reported suspicious rather than removed, but no other guarantee holds for
    JSX/TSX files.
    """
    if last == "":
        return True
    if len(last) == 1:
        return last in "(,=:[!&|?{};+-*%<>~^"
    return last in JS_REGEX_KEYWORDS


def _hide(out: List[str], a: int, b: int) -> None:
    for k in range(a, b):
        if out[k] != "\n":
            out[k] = " "


def _mask_line_comment(src: str, out: List[str], i: int) -> int:
    """Blank a // comment starting at i; returns the index of its newline (or the end of src)."""
    j = src.find("\n", i)
    j = len(src) if j < 0 else j
    _hide(out, i, j)
    return j


def _mask_block_comment(src: str, out: List[str], i: int) -> int:
    """Blank a /* */ comment starting at i; returns the index just past it (or the end of src)."""
    j = src.find("*/", i + 2)
    j = len(src) if j < 0 else j + 2
    _hide(out, i, j)
    return j


def _mask_quoted(src: str, out: List[str], i: int) -> int:
    """Blank a '...' or "..." literal starting at i. It ends at its closing quote or at the line end
    (an unterminated literal is not carried across lines). Returns the index just past it."""
    quote = src[i]
    n = len(src)
    j = i + 1
    while j < n and src[j] != quote and src[j] != "\n":
        j += 2 if src[j] == "\\" else 1
    end = j + 1 if (j < n and src[j] == quote) else j
    end = min(end, n)
    _hide(out, i, end)
    return end


def _mask_regex(src: str, out: List[str], i: int) -> Optional[int]:
    """Blank a regex literal whose opening '/' is at i. Returns the index just past it (with its flags),
    or None when no closing '/' appears on the line, in which case the '/' is not a regex."""
    n = len(src)
    j = i + 1
    in_class = False
    while j < n and src[j] != "\n":
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            j += 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            j = min(j, n)
            _hide(out, i, j)
            return j
        j += 1
    return None


def _js_mask(src: str) -> str:
    """Same-length copy of src with string, template, comment and regex contents blanked.

    Template `${...}` expressions stay visible as code. Brackets and identifiers outside literals
    survive, so brace matching and call matching can run on the masked text; names are read from src.
    This function is the dispatch: each literal kind is blanked by its own scanner above. The lexer's
    JSX/TSX limit is documented on _js_regex_ok.
    """
    out = list(src)
    n = len(src)
    stack: List[str] = []  # "tpl" (inside a template literal) or "expr" (inside a ${...})
    depth: List[int] = []  # brace depth of each frame (used by "expr" frames)
    last = ""  # previous significant token: a word, one punctuation char, or "'" after a literal
    i = 0
    while i < n:
        ch = src[i]
        top = stack[-1] if stack else "code"
        if top == "tpl":
            if ch == "\\":
                _hide(out, i, min(i + 2, n))
                i += 2
                continue
            if ch == "`":
                _hide(out, i, i + 1)
                stack.pop()
                depth.pop()
                last = "'"
                i += 1
                continue
            if src.startswith("${", i):
                _hide(out, i, i + 2)
                stack.append("expr")
                depth.append(0)
                last = "{"
                i += 2
                continue
            _hide(out, i, i + 1)
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            i = _mask_line_comment(src, out, i)
            continue
        if src.startswith("/*", i):
            i = _mask_block_comment(src, out, i)
            continue
        if ch in "'\"":
            i = _mask_quoted(src, out, i)
            last = "'"
            continue
        if ch == "`":
            stack.append("tpl")
            depth.append(0)
            _hide(out, i, i + 1)
            i += 1
            continue
        if ch == "/" and _js_regex_ok(last):
            end = _mask_regex(src, out, i)
            if end is not None:
                i = end
                last = "'"
                continue
        if ch == "{":
            if top == "expr":
                depth[-1] += 1
            last = "{"
            i += 1
            continue
        if ch == "}":
            if top == "expr":
                if depth[-1] == 0:
                    stack.pop()
                    depth.pop()
                    _hide(out, i, i + 1)
                    last = "'"
                    i += 1
                    continue
                depth[-1] -= 1
            last = "}"
            i += 1
            continue
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            last = src[i:j]
            i = j
            continue
        last = ch
        i += 1
    return "".join(out)


def _js_suite_at(masked: str, src: str, index: int) -> Optional[JsSuite]:
    """Parse a suite call whose callee starts at index; None when it is not a recognised suite call."""
    head = SUITE_HEAD_RE.match(masked, index)
    if head is None:
        return None
    words = [w.strip() for w in head.group(1).split(".") if w.strip()]
    if any(w not in SUITE_MODIFIERS for w in words):
        return None
    groups: List[Tuple[int, int]] = []
    pos = head.end()
    while pos < len(masked) and masked[pos] == "(":
        close = _js_close(masked, pos)
        if close is None:
            # Unbalanced text: keep it visible as an unparseable suite (never removable), ending on its line.
            nl = masked.find("\n", pos)
            end = len(masked) - 1 if nl < 0 else max(index, nl - 1)
            return _new_suite(index, end, None, "each" in words, None)
        groups.append((pos, close))
        pos = close + 1
        while pos < len(masked) and masked[pos].isspace():
            pos += 1
    if not groups:
        return None
    open_i, close_i = groups[-1]
    args = _js_split_args(masked, open_i + 1, close_i)
    name: Optional[str] = None
    if args:
        s, e = args[0]
        raw = src[s:e].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"`":
            name = raw[1:-1]
    body: Optional[Tuple[int, int]] = None
    if len(args) == 2:
        cs, ce = args[1]
        seg = masked[cs:ce]
        lead = len(seg) - len(seg.lstrip())
        cm = CALLBACK_RE.match(seg.lstrip())
        if cm is not None:
            brace = cs + lead + cm.end() - 1
            close_b = _js_close(masked, brace)
            if close_b is not None and masked[close_b + 1:ce].strip() == "":
                body = (brace, close_b)
    return _new_suite(index, close_i, name, "each" in words, body)


def _js_suites_in(masked: str, src: str, lo: int, hi: int) -> List[JsSuite]:
    """Direct suite children of masked[lo:hi]: suites not nested inside another suite found here."""
    found: List[JsSuite] = []
    last_end = -1
    for m in SUITE_CALLEE_RE.finditer(masked, lo, hi):
        if m.start() < last_end:
            continue
        suite = _js_suite_at(masked, src, m.start())
        if suite is None:
            continue
        found.append(suite)
        last_end = suite.end
    return found


def _blank_hooks(text: str) -> str:
    spans: List[Tuple[int, int]] = []
    for m in HOOK_RE.finditer(text):
        close = _js_close(text, m.end() - 1)
        if close is not None:
            spans.append((m.start(), close + 1))
    return _blank(text, spans)


def _js_block_class(masked: str, src: str, lo: int, hi: int) -> str:
    """'empty', 'nonempty' or 'suspicious' for the block text masked[lo:hi] (a virtual fixpoint:
    empty child suites are blanked before the rest is judged)."""
    body = masked[lo:hi]
    if TEST_CALL_RE.search(body):
        return "nonempty"
    spans: List[Tuple[int, int]] = []
    for child in _js_suites_in(masked, src, lo, hi):
        cls = _js_suite_class(masked, src, child)
        if cls != "empty":
            return cls
        spans.append((child.start - lo, child.end + 1 - lo))
    rest = _blank_hooks(_blank(body, spans))
    return "empty" if rest.replace(";", " ").strip() == "" else "suspicious"


def _js_suite_class(masked: str, src: str, suite: JsSuite) -> str:
    if suite.body is None:
        return "suspicious"
    cls = _js_block_class(masked, src, suite.body[0] + 1, suite.body[1])
    if suite.each and cls == "empty":
        return "suspicious"
    return cls


def _strip_js_ext(path: str) -> str:
    for ext in JS_EXTS:
        if path.endswith(ext):
            return path[:-len(ext)]
    return path


def _is_config_file(fn: str) -> bool:
    """Config files name test paths as plain text (globs, include lists), so any mention counts."""
    low = fn.lower()
    return "config" in low or low.endswith((".json", ".yml", ".yaml", ".toml", ".ini", ".cfg"))


class _ImportIndex(NamedTuple):
    """What the files under one root import or mention, read once per run (not once per deleted file)."""
    relative: Dict[str, List[str]]  # resolved relative import target (no extension) -> importing files
    nonrelative: List[Tuple[str, str]]  # (file, specifier) for each non-relative JS/TS import
    texts: List[Tuple[str, str]]  # (file, text) for each config file, which may name a path in plain text


def _build_import_index(root: str) -> _ImportIndex:
    """Walk root once (node_modules and .git skipped). Text is decoded with errors="replace", so an
    undecodable byte never hides a mention."""
    relative: Dict[str, List[str]] = {}
    nonrelative: List[Tuple[str, str]] = []
    texts: List[Tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if _is_config_file(fn):
                texts.append((rel, text))
            if os.path.splitext(fn)[1] not in JS_EXTS:
                continue
            for m in IMPORT_SPEC_RE.finditer(text):
                spec = m.group(2)
                if not spec.startswith("."):
                    nonrelative.append((rel, spec))
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
                relative.setdefault(_strip_js_ext(resolved), []).append(rel)
    return _ImportIndex(relative, nonrelative, texts)


def _js_importers(index: _ImportIndex, target: str) -> Tuple[List[str], List[str]]:
    """(relative importers, other mentions) of target, each sorted and never including target itself.

    Other mentions are a non-relative import whose specifier names the file (an alias such as
    "@/lib/x" cannot be resolved here, so its last segment is compared) or a config file that names
    the file's path. Either one blocks delete-file, the same as a relative importer."""
    stem = _strip_js_ext(target)
    base = posixpath.basename(stem)
    relative = sorted({f for f in index.relative.get(stem, []) if f != target})
    other: Set[str] = set()
    for rel, spec in index.nonrelative:
        if rel == target:
            continue
        tail = spec.rstrip("/").rsplit("/", 1)[-1]
        if stem in spec or _strip_js_ext(tail) == base:
            other.add(rel)
    for rel, text in index.texts:
        if rel != target and (target in text or stem in text):
            other.add(rel)
    return relative, sorted(other)


def _js_suite_shares_line(masked: str, suite: JsSuite) -> bool:
    """Whether other code shares the suite's first or last line. Such a block is not removed whole:
    deleting its lines would take that code with it. Reads masked text, so string contents never count."""
    # Anything before the callee on its first line, other than a stray ';', is code this removal would take.
    line_start = masked.rfind("\n", 0, suite.start) + 1
    before = masked[line_start:suite.start].strip()
    if before and before != ";":
        return True
    # Likewise anything after the closing paren on its last line.
    line_end = masked.find("\n", suite.end)
    if line_end == -1:
        line_end = len(masked)
    after = masked[suite.end + 1:line_end].strip()
    return bool(after and after != ";")


def _suite_finding(rel: str, src: str, lines: List[str], suite: JsSuite, kind: FindingKind,
                   action: FindingAction, reason: Optional[str]) -> Finding:
    line = _line_of(src, suite.start)
    end_line = _line_of(src, suite.end)
    return _finding(rel, line, end_line, "js", kind, suite.name, action, reason, [],
                    _block_text(lines, line, end_line))


def _classify_suites(masked: str, src: str, suites: List[JsSuite]) -> Tuple[List[JsSuite], List[JsSuite]]:
    """(outermost empty suites, outermost kept-suspicious suites) among suites. Recurses only through
    non-empty suites, so an empty suite nested in an empty one is reported once, for the outer one."""
    empties: List[JsSuite] = []
    kept: List[JsSuite] = []
    for suite in suites:
        cls = _js_suite_class(masked, src, suite)
        if cls == "empty":
            empties.append(suite)
        elif cls == "suspicious":
            kept.append(suite)
        elif suite.body is not None:
            inner_empty, inner_kept = _classify_suites(
                masked, src, _js_suites_in(masked, src, suite.body[0] + 1, suite.body[1]))
            empties.extend(inner_empty)
            kept.extend(inner_kept)
    return empties, kept


def _js_findings(index: _ImportIndex, rel: str, src: str) -> List[Finding]:
    masked = _js_mask(src)
    lines = src.split("\n")
    empties, kept = _classify_suites(masked, src, _js_suites_in(masked, src, 0, len(masked)))

    # The flat file rule runs whenever no suite is kept, not only when a suite was emptied: a test file
    # whose tests were all removed, leaving imports only, is still an emptied file. A file with no
    # empty suites must be a test path with an import to count, so an unrelated helper is not swept in.
    if not kept and (empties or (_is_test_path(rel) and IMPORT_STMT_RE.search(masked))):
        spans = [(s.start, s.end + 1) for s in empties]
        spans += [(m.start(), m.end()) for m in IMPORT_STMT_RE.finditer(masked)]
        rest = _blank_hooks(_blank(masked, spans))
        if not TEST_CALL_RE.search(rest):
            other_code = rest.replace(";", " ").strip() != ""
            relative, others = _js_importers(index, rel)
            reasons: List[str] = []
            if other_code:
                reasons.append("other top-level code remains")
            if relative:
                reasons.append("imported by other files")
            if others:
                reasons.append("named by a non-relative import or a config file")
            total = _total_lines(src)
            if reasons:
                return [_finding(rel, 1, total, "js", "empty-file-unsafe", None, "stop", "; ".join(reasons),
                                 sorted(set(relative) | set(others)), src)]
            return [_finding(rel, 1, total, "js", "empty-file", None, "delete-file", None, [], src)]

    findings: List[Finding] = []
    for suite in kept:
        findings.append(_suite_finding(rel, src, lines, suite, "kept-suspicious", "report",
                                       "body holds code that is not provably empty"))
    for suite in empties:
        if _js_suite_shares_line(masked, suite):
            findings.append(_suite_finding(rel, src, lines, suite, "kept-suspicious", "report",
                                           "shares a line with other code"))
        else:
            findings.append(_suite_finding(rel, src, lines, suite, "empty-suite", "remove", None))
    return findings


def _py_findings(rel: str, src: str) -> List[Finding]:
    total = _total_lines(src)
    try:
        tree = ast.parse(src, filename=rel)
    except (SyntaxError, ValueError):
        return [_finding(rel, 1, total, "python", "kept-suspicious", None, "report",
                         "file does not parse", [], src)]
    lines = src.split("\n")
    findings: List[Finding] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test") or node.keywords:
            continue
        if node.bases and not (len(node.bases) == 1 and isinstance(node.bases[0], ast.Name)
                               and node.bases[0].id == "object"):
            continue
        if any(isinstance(m, ast.ClassDef) for m in node.body):
            continue
        if any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")
               for m in node.body):
            continue
        end = node.end_lineno or node.lineno
        findings.append(_finding(rel, node.lineno, end, "python", "empty-class", node.name, "report", None,
                                 [], _block_text(lines, node.lineno, end)))
    return findings


def _go_findings(rel: str, src: str) -> List[Finding]:
    if GO_TEST_FUNC_RE.search(src):
        return []
    # Report only. A helper file with no tests can be legitimate, and a Ginkgo or testify file can
    # keep its specs outside a Test function, so the reason names that possible false positive.
    reason = "no Test, Benchmark, Fuzz or Example function; a helper or framework file can be legitimate (possible false positive)"
    return [_finding(rel, 1, _total_lines(src), "go", "empty-go-test-file", None, "report", reason, [], src)]


def _empty_stack(rel: str) -> Optional[str]:
    if rel.endswith("_test.go"):
        return "go"
    ext = os.path.splitext(rel)[1]
    if ext in JS_EXTS:
        return "js"
    if ext == ".py":
        return "python"
    return None


def _inside_root(root: str, raw: str) -> Optional[str]:
    """Real path of raw (relative to root, or absolute) when it lies strictly inside root; else None."""
    full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    try:
        inside = full != root and os.path.commonpath([root, full]) == root
    except ValueError:
        # Different drives, or a mix of absolute and relative paths: never inside.
        inside = False
    return full if inside else None


def cmd_empty_suites(args: argparse.Namespace) -> int:
    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        print(f"error: --root is not a directory: {args.root!r}", file=sys.stderr)
        return 2
    index: Optional[_ImportIndex] = None  # built on the first JS file, once per run
    findings: List[Finding] = []
    for raw in args.files:
        full = _inside_root(root, raw)
        if full is None:
            print(f"error: path is outside --root: {raw!r}", file=sys.stderr)
            return 2
        if not os.path.exists(full):
            # Already deleted (a re-run after delete-file): nothing left to scan, not an error.
            print(f"skipped: {raw!r} does not exist", file=sys.stderr)
            continue
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        stack = _empty_stack(rel)
        if stack is None:
            print(f"skipped: {raw!r} has no empty-suites rule for its file type", file=sys.stderr)
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {raw!r}: {exc}", file=sys.stderr)
            return 2
        if stack == "js":
            if index is None:
                index = _build_import_index(root)
            findings.extend(_js_findings(index, rel, src))
        elif stack == "python":
            findings.extend(_py_findings(rel, src))
        else:
            findings.extend(_go_findings(rel, src))
    findings.sort(key=lambda f: (f["file"], f["line"], f["end_line"], f["kind"], str(f["name"])))
    print(json.dumps({"findings": findings}, sort_keys=True))
    return 1 if any(f["action"] in ("remove", "delete-file", "stop") for f in findings) else 0


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
    layout.add_argument("--base", default=None, help="base SHA (commit only, not INDEX/WORKTREE); "
                        "omit to list test paths from the working tree only")
    layout.add_argument("--impl", action="extend", nargs="+", default=[], dest="impl_paths",
                        help="implementation paths (repeatable)")
    layout.add_argument("--max-names", type=int, default=60, help="max test names per output (default 60)")

    # overlaps subcommand
    overlaps = sub.add_parser("overlaps", help="parse overlap classifications from reports")
    overlaps.add_argument("--report", action="append", default=[], dest="reports",
                          help="report file paths (repeatable)")
    overlaps.add_argument("--added-ids", required=True, help="file with added test ids")
    overlaps.add_argument("--out", required=True, help="output file path")

    # hygiene-json subcommand
    hygiene_json = sub.add_parser("hygiene-json", help="write the test hygiene measurement JSON")
    hygiene_json.add_argument("--scratch", required=True, help="scratch directory with round evidence files")
    hygiene_json.add_argument("--step", required=True, choices=["implement", "fix"],
                              help="step id recorded in the JSON")
    hygiene_json.add_argument("--skip-round2", action="store_true",
                              help="Round 2 evidence was not re-produced this run: write null for it")
    hygiene_json.add_argument("--skip-round4", action="store_true",
                              help="Round 4 evidence was not re-produced this run: write null for it")
    hygiene_json.add_argument("--flow-dir", default=None, dest="flow_dir",
                              help="flow directory, an additional allowed root for --out")
    hygiene_json.add_argument("--layout-names-given-file", default=None, dest="layout_names_given_file",
                              help="file holding 'true' when layout names were given (default: "
                              "<scratch>/layout-names-given.txt)")
    hygiene_json.add_argument("--out", required=True, help="output JSON file path")

    # layout-evidence subcommand
    layout_ev = sub.add_parser("layout-evidence", help="resolve the layout base and write layout evidence")
    layout_ev.add_argument("--step", required=True, choices=["implement", "fix"])
    where = layout_ev.add_mutually_exclusive_group(required=True)
    where.add_argument("--flow-dir", default=None, dest="flow_dir")
    where.add_argument("--scratch", default=None)
    layout_ev.add_argument("--impl", action="extend", nargs="+", default=[], dest="impl_paths",
                           help="implementation paths")
    layout_ev.add_argument("--out", required=True, help="evidence output file path")
    layout_ev.add_argument("--names-given-file", default=None, dest="names_given_file",
                           help="file to receive true/false (default: <flow-dir or scratch>/layout-names-given.txt)")

    # empty-suites subcommand (read-only)
    empty = sub.add_parser("empty-suites", help="read-only: test units left empty after a deletion (JSON findings)")
    empty.add_argument("--root", required=True, help="repo root; every FILE must resolve inside it")
    empty.add_argument("files", nargs="*", metavar="FILE", help="touched files to check")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "fence":
        return cmd_fence(args)
    if args.cmd == "survivors":
        return cmd_survivors(args)
    if args.cmd == "names":
        if not args.hygiene:
            return cmd_names(args)
        try:
            return cmd_names(args)
        except SystemExit as exc:
            # hygiene mode: an error (exit 1 from _fail) is distinct from hits (also exit 1)
            return 3 if exc.code == 1 else int(exc.code or 0)
    if args.cmd == "working-ids":
        return cmd_working_ids(args)
    if args.cmd == "layout":
        return cmd_layout(args)
    if args.cmd == "overlaps":
        return cmd_overlaps(args)
    if args.cmd == "hygiene-json":
        return cmd_hygiene_json(args)
    if args.cmd == "layout-evidence":
        return cmd_layout_evidence(args)
    if args.cmd == "empty-suites":
        return cmd_empty_suites(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
