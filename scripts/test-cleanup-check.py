#!/usr/bin/env python3
"""test-cleanup-check.py: deterministic checks for /implement-with-haiku's spec-blind test flow (#458).

Usage:
    test-cleanup-check.py fence --base SHA --head SHA|INDEX|WORKTREE
                          --test FILE::NAME [--test FILE::NAME ...]
                          [--impl-files PATH [PATH ...]]
    test-cleanup-check.py survivors --deletions FILE --collected FILE
    test-cleanup-check.py names --base SHA [--allow PATH [PATH ...]]

Runs in the current directory, which must be inside the target git repo. Paths are repo-root
relative. Every subcommand prints one finding per line on stdout and exits:
    0  pass (no findings that are violations)
    1  violation found, or input that cannot be read or resolved (fails closed; reason on stderr)
    2  argparse usage error

fence: the binding-edit fence (Q2, D1). Each --test id is a test (file plus the innermost test
name, after the last "::"). Only that test's body and the file's import lines are compared between
base and head; other tests in the same file are never inspected. --head INDEX compares the staged
index, WORKTREE the working tree, and a SHA a commit. Blank lines are not significant.

  Each changed line in a body is classified:
    binding-edit  an import line (IMPORT patterns), or a call-site line that is not an assertion
                  line, paired one-to-one with its base line, where the prefix and suffix are
                  identical, the callee and/or the argument count changed, the arguments that both
                  lines have are identical, and no literal argument of the base line is dropped.
    violation     anything else: an assertion line (ASSERT patterns), a changed expected-value
                  assignment, a changed literal argument, an added or removed non-import line,
                  or a line the patterns cannot recognise (fail closed).

  Output per changed line: "binding-edit <id> <line>" or "violation <id> <line>". The line is the
  head line number for added or changed lines and the base line number for removed lines. A test
  with a violation also gets "restore <id> <start>-<end>", its base body range (1-based, inclusive),
  so the caller can restore that body alone. A test id not found in base or head prints
  "unlocated <id> base|head" plus "violation <id> 0".

  With --impl-files, each changed path not in that list is printed once as "support-edit <path>"
  when it changed outside any test body and outside import lines. That covers shared fixtures,
  conftest.py, helpers, module-level test code, snapshot and golden files and test data. A
  support-edit finding alone does not fail the fence (exit stays 0).

survivors: every survivor named in a deletions table must appear in the collected test-id list.
  Deletions file rows are markdown table rows "| <deleted id> | <class> | <reason> | <survivor> |".
  A survivor cell containing "::" names a test (backticks are stripped); other cells, including
  "proof: <kind>" and commit SHAs, are not survivors and are skipped. The collected file holds one
  test id per line. Each missing survivor prints "missing-survivor <survivor> named-by <deleted>".

names: no spec-blind name survives in the run's own working set (D2). "spec[-_ ]?blind" (case
  insensitive) is searched in the changed paths (working tree against base, untracked included)
  and in the added lines of <base>..HEAD. Hits print "spec-blind path <path>" or
  "spec-blind line <path>:<n> <text>". --allow exempts exact repo-relative paths.

Patterns, per framework (chosen from the file extension):
  python  (.py)      def NAME(  (test body = the indented block); tests are def test*;
                     import: "from x import" / "import x"; assert: assert*, pytest.raises
  js/ts   (.js .jsx .ts .tsx .mjs .cjs)
                     it("NAME", ...) / test("NAME", ...) (brace block); import: "import ..."
                     assert: expect(, assert*
  rust    (.rs)      fn NAME( with a #[test] or #[tokio::test] attribute (brace block);
                     import: use ... ; assert: assert!, assert_eq!, assert_ne!
  swift   (.swift)   func testNAME( (brace block); import: import X; assert: XCTAssert*
  go      (.go)      func TestNAME( (brace block); import: import ...; assert: t.Error*, t.Fatal*,
                     require.*
  Any other extension has no test bodies: every changed non-blank line counts for support-edit.
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
from typing import Dict, List, NoReturn, Optional, Sequence, Tuple

WORKING_HEADS = ("INDEX", "WORKTREE")

EXT_LANG = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".rs": "rust",
    ".swift": "swift",
    ".go": "go",
}

IMPORT_RES = {
    "python": re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\s+\S)"),
    "js": re.compile(r"^\s*import\b"),
    "rust": re.compile(r"^\s*(?:use\s+\S|extern\s+crate\b)"),
    "swift": re.compile(r"^\s*import\s+\w"),
    "go": re.compile(r"^\s*import\b"),
}

# Start of a test or helper definition; group 1 is the name (group 2 for js, whose name is quoted).
DEF_RES = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
    "js": re.compile(r"^\s*(?:it|test)(?:\.\w+)?\s*\(\s*(['\"`])(.*?)\1"),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*[(<]"),
    "swift": re.compile(r"^\s*func\s+(\w+)\s*\("),
    "go": re.compile(r"^\s*func\s+(\w+)\s*\("),
}

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


def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def _git(args: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


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


def _read_at(ref: str, path: str) -> Optional[str]:
    """File text at a ref (SHA, INDEX or WORKTREE), or None if the path does not exist there."""
    if ref == "WORKTREE":
        full = os.path.join(_toplevel(), path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    spec = f":{path}" if ref == "INDEX" else f"{ref}:{path}"
    rc, out = _git(["show", spec])
    return out if rc == 0 else None


def _changed_paths(base: str, head: str) -> List[str]:
    """Repo-relative paths changed between base and head (working set when head is INDEX/WORKTREE)."""
    if head == "INDEX":
        _, out = _git(["diff", "--cached", "--name-only", "--no-renames", base])
        paths = out.splitlines()
    elif head == "WORKTREE":
        _, out = _git(["diff", "--name-only", "--no-renames", base])
        _, untracked = _git(["ls-files", "--others", "--exclude-standard", "--full-name"])
        paths = out.splitlines() + untracked.splitlines()
    else:
        _, out = _git(["diff", "--name-only", "--no-renames", base, head])
        paths = out.splitlines()
    return sorted({p for p in paths if p.strip()})


def _lang(path: str) -> Optional[str]:
    return EXT_LANG.get(os.path.splitext(path)[1].lower())


def _lines(text: Optional[str]) -> List[str]:
    return text.splitlines() if text is not None else []


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_import(lang: Optional[str], line: str) -> bool:
    if lang is None:
        return False
    return IMPORT_RES[lang].search(line) is not None


def _significant(lang: Optional[str], line: str) -> bool:
    return line.strip() != "" and not _is_import(lang, line)


def _rust_test_attr(lines: List[str], index: int) -> bool:
    j = index - 1
    while j >= 0 and lines[j].strip().startswith("#["):
        if "test" in lines[j]:
            return True
        j -= 1
    return False


def _find_starts(lang: Optional[str], lines: List[str]) -> List[Tuple[int, str, bool]]:
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


def _block_end(lang: str, lines: List[str], start: int) -> int:
    """Inclusive index of the last line of the block that starts at `start`."""
    if lang == "python":
        indent = _indent(lines[start])
        end = start
        for k in range(start + 1, len(lines)):
            if not lines[k].strip():
                continue
            if _indent(lines[k]) <= indent:
                break
            end = k
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
    if not seen:
        return start
    return len(lines) - 1


def _find_body(lang: Optional[str], lines: List[str], name: str) -> Optional[Tuple[int, int]]:
    for start, found, _ in _find_starts(lang, lines):
        if found == name:
            return start, _block_end(lang or "", lines, start)
    return None


def _test_bodies(lang: Optional[str], lines: List[str]) -> List[Tuple[int, int]]:
    return [(s, _block_end(lang or "", lines, s)) for s, _, is_t in _find_starts(lang, lines) if is_t]


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


def _is_literal(arg: str) -> bool:
    return LITERAL_RE.fullmatch(arg.strip()) is not None


def _parse_call(line: str) -> Optional[Tuple[str, str, List[str], str]]:
    """(prefix, callee, args, suffix) for a single call on the line, or None."""
    m = CALL_RE.search(line)
    if m is None:
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


def _classify_pair(base: str, head: str) -> str:
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


def _body_changes(lang: Optional[str], base_lines: List[str], head_lines: List[str],
                  brange: Tuple[int, int], hrange: Tuple[int, int]) -> List[Tuple[str, int]]:
    """(verdict, line number) for each changed significant line inside one test body."""
    b = [(i + 1, base_lines[i]) for i in range(brange[0], brange[1] + 1)
         if _significant(lang, base_lines[i])]
    h = [(i + 1, head_lines[i]) for i in range(hrange[0], hrange[1] + 1)
         if _significant(lang, head_lines[i])]
    matcher = difflib.SequenceMatcher(None, [t for _, t in b], [t for _, t in h], autojunk=False)
    out: List[Tuple[str, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        bl = b[i1:i2]
        hl = h[j1:j2]
        if tag == "replace" and len(bl) == len(hl):
            for (_, bt), (hn, ht) in zip(bl, hl):
                out.append((_classify_pair(bt, ht), hn))
        else:
            for bn, _ in bl:
                out.append(("violation", bn))
            for hn, _ in hl:
                out.append(("violation", hn))
    return out


def _import_changes(lang: Optional[str], base_lines: List[str], head_lines: List[str]) -> List[int]:
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
    bb = _test_bodies(lang, bl)
    hb = _test_bodies(lang, hl)
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


def _split_test_id(value: str) -> Tuple[str, str]:
    path, _, _ = value.partition("::")
    return path, value.rsplit("::", 1)[1]


def _test_id_arg(value: str) -> str:
    path, sep, name = value.partition("::")
    if not sep or not path or not name.rsplit("::", 1)[-1]:
        raise argparse.ArgumentTypeError(f"expected FILE::NAME, got {value!r}")
    return value


def _norm(path: str) -> str:
    return os.path.normpath(path)


def cmd_fence(args: argparse.Namespace) -> int:
    _require_ref(args.base, allow_working=False)
    _require_ref(args.head, allow_working=True)
    findings: List[str] = []
    violated = False
    cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for value in args.test:
        path, name = _split_test_id(value)
        tid = f"{path}::{name}"
        lang = _lang(path)
        if path not in cache:
            cache[path] = (_read_at(args.base, path), _read_at(args.head, path))
        base_text, head_text = cache[path]
        bl = _lines(base_text)
        hl = _lines(head_text)
        br = _find_body(lang, bl, name)
        hr = _find_body(lang, hl, name)
        if br is None or hr is None:
            missing = [side for side, rng in (("base", br), ("head", hr)) if rng is None]
            findings.append(f"unlocated {tid} {'/'.join(missing)}")
            findings.append(f"violation {tid} 0")
            violated = True
            if br is not None:
                findings.append(f"restore {tid} {br[0] + 1}-{br[1] + 1}")
            continue
        for n in _import_changes(lang, bl, hl):
            findings.append(f"binding-edit {tid} {n}")
        has_violation = False
        for verdict, n in _body_changes(lang, bl, hl, br, hr):
            findings.append(f"{verdict} {tid} {n}")
            if verdict == "violation":
                has_violation = True
        if has_violation:
            violated = True
            findings.append(f"restore {tid} {br[0] + 1}-{br[1] + 1}")
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


def cmd_survivors(args: argparse.Namespace) -> int:
    try:
        with open(args.deletions, encoding="utf-8", errors="replace") as fh:
            deletions_text = fh.read()
        with open(args.collected, encoding="utf-8", errors="replace") as fh:
            collected = {ln.strip() for ln in fh.read().splitlines() if ln.strip()}
    except OSError as exc:
        _fail(str(exc))
    missing = 0
    for line in deletions_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [p.strip() for p in stripped.strip("|").split("|")]
        if len(parts) < 4:
            continue
        deleted = parts[0]
        survivor = parts[3].strip("` ")
        if "::" not in survivor:
            continue
        if survivor not in collected:
            print(f"missing-survivor {survivor} named-by {deleted}")
            missing += 1
    return 1 if missing else 0


def cmd_names(args: argparse.Namespace) -> int:
    _require_ref(args.base, allow_working=False)
    allow = {_norm(p) for p in args.allow}
    hits = 0
    for path in _changed_paths(args.base, "WORKTREE"):
        if _norm(path) not in allow and SPEC_RE.search(path):
            print(f"spec-blind path {path}")
            hits += 1
    _, diff = _git(["diff", "--unified=0", "--no-renames", args.base, "HEAD"])
    current: Optional[str] = None
    new_line = 0
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_header = True
            current = None
            continue
        if in_header and line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            if current is not None and _norm(current) in allow:
                current = None
            continue
        if line.startswith("@@"):
            in_header = False
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
            continue
        if line.startswith("+"):
            if current is not None and SPEC_RE.search(line[1:]):
                print(f"spec-blind line {current}:{new_line} {line[1:].strip()}")
                hits += 1
            new_line += 1
    return 1 if hits else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic checks for /implement-with-haiku spec-blind tests.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fence = sub.add_parser("fence", help="binding-edit fence over test ids")
    fence.add_argument("--base", required=True, help="base SHA (pre-Round-3)")
    fence.add_argument("--head", required=True, help="SHA, INDEX or WORKTREE")
    fence.add_argument("--test", action="append", default=[], type=_test_id_arg,
                       help="FILE::NAME (repeatable)")
    fence.add_argument("--impl-files", action="extend", nargs="+", default=[],
                       help="implementation paths (not support edits)")

    surv = sub.add_parser("survivors", help="named survivors exist in the collected list")
    surv.add_argument("--deletions", required=True)
    surv.add_argument("--collected", required=True)

    names = sub.add_parser("names", help="no spec-blind name in the run's working set")
    names.add_argument("--base", required=True)
    names.add_argument("--allow", action="extend", nargs="+", default=[], help="exempt paths")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "fence":
        return cmd_fence(args)
    if args.cmd == "survivors":
        return cmd_survivors(args)
    return cmd_names(args)


if __name__ == "__main__":
    sys.exit(main())
