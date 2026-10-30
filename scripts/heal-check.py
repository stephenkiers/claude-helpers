#!/usr/bin/env python3
"""Mechanical classifier, complexity floor and behaviour check for /expert-flow check heals.

Subcommands print one JSON object on stdout. All subcommands exit 0 on success; argparse errors exit 2.
Every unparseable input fails closed.

  signature  --output-file <path>
      Classify one fenced check-output data file.
      -> {"class": lint|test|infra|timeout|unknown, "signature": <sha256>,
          "ids": [...], "counts": {"run": n, "failed": m} | null}

  compare    --first <json file> --second <json file>
      -> {"recurs": true|false}; true only if both are lint/test with equal signatures.

  complexity --repo <path> --base <sha> --head <sha> --failing-ids <json file>
             --before-counts <json file|null> --after-counts <json file|null>
             [--healer-review required]
      -> {"review": "required"|"skipped", "reasons": [...]}
      Nothing can lower a required result; the healer flag can only raise it.

Stdlib only, Python 3.8+.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from typing import Any, Dict, List, Literal, Optional, Tuple

# Type aliases for classifier contracts
ClassifyResult = Literal["lint", "test", "infra", "timeout", "unknown"]
ComplexityVerdict = Literal["required", "skipped"]

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_TMP_PATH_RE = re.compile(r"(?:/private)?/var/folders/\S+|/private/tmp/\S+|/tmp/\S+")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"
)
_DURATION_RE = re.compile(r"\b(?:in\s+)?\d+(?:\.\d+)?\s?(?:ms|sec|seconds|s)\b")


def _normalize_id(raw: str) -> str:
    text = _TMP_PATH_RE.sub("<tmp>", raw)
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _DURATION_RE.sub("<dur>", text)
    return text.strip()


def _signature(cls: str, ids: List[str]) -> str:
    payload = "\n".join(sorted(ids)) + "\n" + cls
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_INFRA_PATTERNS = [
    re.compile(r"command not found"),
    re.compile(r"Permission denied", re.IGNORECASE),
    re.compile(r"another cleanup is validating main"),
    re.compile(r"No such file or directory"),
    re.compile(r"\bDB_NAME\b.{0,40}(?:unset|not set|missing|required|empty)", re.IGNORECASE),
    re.compile(r"(?:unset|not set|missing|required)\b.{0,40}\bDB_NAME\b", re.IGNORECASE),
    re.compile(r"\b[A-Z][A-Z0-9_]*: (?:unbound variable|parameter not set)"),
    re.compile(r"environment variable\b.{0,60}(?:not set|unset|missing|is required)", re.IGNORECASE),
    re.compile(r"Could not resolve host", re.IGNORECASE),
    re.compile(r"Temporary failure in name resolution", re.IGNORECASE),
    re.compile(r"Network is unreachable", re.IGNORECASE),
    re.compile(r"Connection (?:refused|reset by peer|timed out)", re.IGNORECASE),
    re.compile(r"\bECONN(?:REFUSED|RESET)\b|\bENOTFOUND\b|\bgetaddrinfo\b"),
    re.compile(r"\bConnectionError\b|\bNameResolutionError\b"),
]
_TIMEOUT_RE = re.compile(r"timed out", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Lint parsers (ruff, mypy, shellcheck)
# ---------------------------------------------------------------------------

_RUFF_NEW_HEAD_RE = re.compile(r"^([A-Z]{1,5}\d{2,4})\b(?:\s+\[\*\])?\s+\S")
_RUFF_NEW_LOC_RE = re.compile(r"^\s*-->\s+(.+?):\d+(?::\d+)?\s*$")
_RUFF_OLD_RE = re.compile(r"^(.+?):\d+:\d+:\s+([A-Z]{1,5}\d{2,4})\b")
_MYPY_RE = re.compile(r"^(.+?):\d+(?::\d+)?:\s+error:\s.*?(?:\[([a-z0-9_-]+)\])?\s*$")
_SHELLCHECK_FILE_RE = re.compile(r"^In (.+?) line \d+:\s*$")
_SHELLCHECK_CODE_RE = re.compile(r"^\s*(SC\d{4})\b")


def _parse_lint(lines: List[str]) -> List[str]:
    ids: List[str] = []
    pending_rule: Optional[str] = None
    sc_file: Optional[str] = None
    for line in lines:
        m = _RUFF_OLD_RE.match(line)
        if m:
            ids.append("%s:%s" % (m.group(2), m.group(1)))
            pending_rule = None
            continue
        m = _RUFF_NEW_HEAD_RE.match(line)
        if m:
            pending_rule = m.group(1)
            continue
        m = _RUFF_NEW_LOC_RE.match(line)
        if m and pending_rule:
            ids.append("%s:%s" % (pending_rule, m.group(1)))
            pending_rule = None
            continue
        m = _MYPY_RE.match(line)
        if m:
            ids.append("%s:%s" % (m.group(2) or "error", m.group(1)))
            continue
        m = _SHELLCHECK_FILE_RE.match(line)
        if m:
            sc_file = m.group(1)
            continue
        m = _SHELLCHECK_CODE_RE.match(line)
        if m and sc_file:
            ids.append("%s:%s" % (m.group(1), sc_file))
            continue
    return ids


# ---------------------------------------------------------------------------
# Test parsers (tests/run_all.py aggregate, pytest -q)
# ---------------------------------------------------------------------------

_RUN_ALL_RUN_RE = re.compile(r"^\s*Suites run:\s*(\d+)\s*$")
_RUN_ALL_FAILED_RE = re.compile(r"^\s*Suites failed:\s*(\d+)\s*$")
_RUN_ALL_ID_RE = re.compile("^\\s*✗\\s+(\\S.*?)\\s*$")


def _parse_run_all(lines: List[str]) -> Optional[Tuple[List[str], Dict[str, int]]]:
    run: Optional[int] = None
    failed: Optional[int] = None
    ids: List[str] = []
    seen_summary = False
    for line in lines:
        m = _RUN_ALL_RUN_RE.match(line)
        if m:
            run = int(m.group(1))
            seen_summary = True
            continue
        m = _RUN_ALL_FAILED_RE.match(line)
        if m:
            failed = int(m.group(1))
            continue
        if seen_summary:
            m = _RUN_ALL_ID_RE.match(line)
            if m:
                ids.append(m.group(1))
    if run is None or failed is None or failed <= 0 or not ids:
        return None
    return ids, {"run": run, "failed": failed}


_PYTEST_SUMMARY_RE = re.compile(r"^=*\s*(\d+ \w+(?:, \d+ \w+)*) in \d+(?:\.\d+)?s\b")
_PYTEST_COUNT_RE = re.compile(r"(\d+) (\w+)")
_PYTEST_FAIL_RE = re.compile(r"^(?:FAILED|ERROR) (\S.*?)(?: - .*)?\s*$")


def _parse_pytest(lines: List[str]) -> Optional[Tuple[List[str], Dict[str, int]]]:
    summary: Optional[Dict[str, int]] = None
    ids: List[str] = []
    for line in lines:
        s = line.strip()
        m = _PYTEST_SUMMARY_RE.match(s)
        if m:
            summary = {}
            for num, word in _PYTEST_COUNT_RE.findall(m.group(1)):
                summary[word] = summary.get(word, 0) + int(num)
            continue
        m = _PYTEST_FAIL_RE.match(line)
        if m and "::" in m.group(1):
            ids.append(m.group(1))
    if summary is None or not ids:
        return None
    failed = summary.get("failed", 0) + summary.get("error", 0) + summary.get("errors", 0)
    if failed <= 0:
        return None
    run = summary.get("passed", 0) + summary.get("failed", 0) + summary.get("skipped", 0)
    return ids, {"run": run, "failed": failed}


# ---------------------------------------------------------------------------
# signature
# ---------------------------------------------------------------------------


def classify(text: str) -> Dict[str, Any]:
    lines = text.splitlines()

    def result(cls: ClassifyResult, ids: List[str], counts: Optional[Dict[str, int]]) -> Dict[str, Any]:
        norm = [_normalize_id(i) for i in ids]
        return {
            "class": cls,
            "signature": _signature(cls, norm),
            "ids": sorted(norm),
            "counts": counts,
        }

    for pat in _INFRA_PATTERNS:
        if pat.search(text):
            return result("infra", [], None)
    if _TIMEOUT_RE.search(text):
        return result("timeout", [], None)

    lint_ids = _parse_lint(lines)
    if lint_ids:
        return result("lint", lint_ids, None)

    parsed = _parse_run_all(lines)
    if parsed is None:
        parsed = _parse_pytest(lines)
    if parsed is not None:
        ids, counts = parsed
        # Validate that counts match the number of ids; return unknown if mismatch
        if counts.get("failed") != len(ids):
            return result("unknown", [], None)
        return result("test", ids, counts)

    return result("unknown", [], None)


def cmd_signature(args: argparse.Namespace) -> int:
    try:
        with open(args.output_file, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        text = ""
    print(json.dumps(classify(text)))
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cmd_compare(args: argparse.Namespace) -> int:
    recurs = False
    try:
        first = _load_json(args.first)
        second = _load_json(args.second)
        if (
            isinstance(first, dict)
            and isinstance(second, dict)
            and first.get("class") in ("lint", "test")
            and second.get("class") in ("lint", "test")
            and isinstance(first.get("signature"), str)
            and first.get("signature")
            and first.get("class") == second.get("class")
            and first.get("signature") == second.get("signature")
        ):
            recurs = True
    except (OSError, ValueError):
        recurs = False
    print(json.dumps({"recurs": recurs}))
    return 0


# ---------------------------------------------------------------------------
# complexity
# ---------------------------------------------------------------------------

_PROTECTED_PREFIXES = ("commands/", "prompts/", "agents/", "reviewers/", "scripts/", "docs/adr/")
_DEF_RE = re.compile(
    r"^(?:async\s+def\b|def\b|class\b|function\b|[A-Za-z_][\w-]*\s*\(\)\s*\{$)"
)
_BRANCH_RE = re.compile(
    r"^(?:if|elif|else|try|except|finally|case|for|while|switch|fi|done|esac)\b"
)


def _git(repo: str, argv: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _verify_git_ref(repo: str, ref: str) -> bool:
    """Verify a git ref exists using git rev-parse --verify."""
    result = _git(repo, ["rev-parse", "--verify", "--end-of-options", ref])
    return result is not None and len(result.strip()) == 40


def _failing_files(ids: List[str]) -> List[str]:
    files: List[str] = []
    for raw in ids:
        item = str(raw)
        # lint id: RULE:path ; test id: path::node or a suite/file name.
        if "::" in item:
            item = item.split("::", 1)[0]
        elif re.match(r"^[a-z0-9_-]+:", item):
            # Handles ruff (A-Za-z{1,5}\d{2,4}), mypy (hyphenated), shellcheck (SC\d{4}), and error
            item = item.split(":", 1)[1]
        files.append(item.strip())
    return files


def _file_named(changed: str, named: List[str]) -> bool:
    for n in named:
        if not n:
            continue
        if changed == n or changed.endswith("/" + n) or n.endswith("/" + changed):
            return True
    return False


def _norm_code_line(line: str) -> str:
    return " ".join(line.strip().lstrip("}").split())


def _def_or_branch_changed(diff_text: str) -> bool:
    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            norm = _norm_code_line(line[1:])
            if _DEF_RE.match(norm) or _BRANCH_RE.match(norm):
                added[norm] += 1
        elif line.startswith("-"):
            norm = _norm_code_line(line[1:])
            if _DEF_RE.match(norm) or _BRANCH_RE.match(norm):
                removed[norm] += 1
    return added != removed


def _valid_counts(value: Any) -> Optional[Dict[str, int]]:
    if not isinstance(value, dict):
        return None
    run = value.get("run")
    failed = value.get("failed")
    if isinstance(run, bool) or isinstance(failed, bool):
        return None
    if not isinstance(run, int) or not isinstance(failed, int):
        return None
    if run < 0 or failed < 0:
        return None
    return {"run": run, "failed": failed}


def _load_counts(arg: str) -> Optional[Dict[str, int]]:
    if arg.strip() == "null":
        return None
    try:
        return _valid_counts(_load_json(arg))
    except (OSError, ValueError):
        return None


def compute_complexity(args: argparse.Namespace) -> Dict[str, Any]:
    """Compute complexity verdict: 'required' if any issues found, 'skipped' if clean formatting fix.

    The 'skipped' case is when a formatting-only change (no def/branch additions) to named files
    results in all tests passing. This is rare (synthetic-only in this repo) but valid.
    """
    reasons: List[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if args.healer_review == "required":
        add("healer-raised")

    # Validate git refs before using them
    if not _verify_git_ref(args.repo, args.base):
        add("diff-unparseable")
    elif not _verify_git_ref(args.repo, args.head):
        add("diff-unparseable")

    ids: Optional[List[str]] = None
    try:
        loaded = _load_json(args.failing_ids)
        if isinstance(loaded, list):
            ids = [str(i) for i in loaded]
    except (OSError, ValueError):
        ids = None

    names = _git(args.repo, ["diff", "--no-renames", "--name-only", "-z", args.base, args.head])
    diff_text = _git(args.repo, ["diff", "--no-renames", "-U0", args.base, args.head])
    if ids is None or names is None or diff_text is None:
        add("diff-unparseable")
        changed: List[str] = []
    else:
        changed = [n for n in names.split("\0") if n]
        named = _failing_files(ids)
        if any(not _file_named(c, named) for c in changed):
            add("file-not-in-failing-ids")
        if _def_or_branch_changed(diff_text):
            add("def-or-branch-changed")
        if any(c.startswith(_PROTECTED_PREFIXES) for c in changed):
            add("protected-dir-edit")

    before = _load_counts(args.before_counts)
    after = _load_counts(args.after_counts)
    if before is None or after is None:
        add("counts-missing")
    else:
        if after["run"] < before["run"]:
            add("fewer-tests-ran")
        if after["failed"] != 0:
            add("after-failed-nonzero")
        if ids is None or before["failed"] != len(ids):
            add("before-failed-mismatch")

    verdict: ComplexityVerdict = "required" if reasons else "skipped"
    return {"review": verdict, "reasons": reasons}


def cmd_complexity(args: argparse.Namespace) -> int:
    try:
        out = compute_complexity(args)
    except Exception:  # fail closed on anything unexpected
        out = {"review": "required", "reasons": ["diff-unparseable"]}
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_sig = sub.add_parser("signature", help="classify one check-output data file")
    p_sig.add_argument("--output-file", required=True)
    p_sig.set_defaults(func=cmd_signature)

    p_cmp = sub.add_parser("compare", help="did the same failure recur?")
    p_cmp.add_argument("--first", required=True)
    p_cmp.add_argument("--second", required=True)
    p_cmp.set_defaults(func=cmd_compare)

    p_cx = sub.add_parser("complexity", help="complexity floor and behaviour check")
    p_cx.add_argument("--repo", required=True)
    p_cx.add_argument("--base", required=True)
    p_cx.add_argument("--head", required=True)
    p_cx.add_argument("--failing-ids", required=True)
    p_cx.add_argument("--before-counts", required=True)
    p_cx.add_argument("--after-counts", required=True)
    p_cx.add_argument("--healer-review", choices=["required"], default=None)
    p_cx.set_defaults(func=cmd_complexity)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
