#!/usr/bin/env python3
"""test-change-check.py: static removal check for test files.

Flags removals that leave suites empty or leave dangling references.
Never modifies the working tree or runs repo code or test runners.

Usage:
    test-change-check.py --repo PATH --base REV --remove PATH [--remove PATH ...]
                         [--json]

Outputs JSON: {"verdict": "ok"|"flag", "findings": [...]}

Exit codes:
  0: ok (no findings)
  1: flag (findings recorded)
  2: error (bad ref, unreadable tree; output never contains "verdict": "ok")

Findings:
  - 'suite-emptied: <dir>' when the removed file was the only test-case file in a
    directory (nested paths included) and that directory still holds other files.
  - 'referenced: <removed path> by <file>:<line>' for each occurrence of the removed
    path, its basename without extension, its dotted import path, or its stem in a
    runner-config or suite-list file.

--json is accepted for compatibility; output is always JSON.

Second definition of an emptied suite; converge when #498 lands.
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple


def _git(repo: str, args: List[str]) -> bytes:
    """Run git command, fail closed on error."""
    cmd = ["git", "--literal-pathspecs", "-C", repo] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ValueError("cannot run git: %s" % exc)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(msg[0] if msg else "git exited %d" % proc.returncode)
    return proc.stdout


def resolve_ref(repo: str, ref: str) -> str:
    """Resolve ref to SHA."""
    if not ref or ref.startswith("-"):
        raise ValueError("bad ref: %s" % ref)
    cmd = ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ValueError("cannot run git: %s" % exc)
    sha = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not sha:
        raise ValueError("bad ref: %s" % ref)
    return sha


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_test_path(path: str) -> bool:
    """Check if path is under a test directory."""
    _TEST_DIRS = ("tests", "__tests__", "spec")
    base = _basename(path)
    _TEST_BASENAME_GLOBS = ("*.test.*", "*.spec.*", "*_test.*")
    for glob in _TEST_BASENAME_GLOBS:
        if fnmatch.fnmatchcase(base, glob):
            return True
    for ext in ("ts", "js", "py", "rs", "swift"):
        if fnmatch.fnmatchcase(base, "test_*." + ext):
            return True
    parts = path.split("/")[:-1]
    return any(p in _TEST_DIRS for p in parts)


def is_test_case_path(path: str) -> bool:
    """Check if a path is a test-case file (matches anti-cheat-scan.py logic)."""
    if not is_test_path(path):
        return False

    base = _basename(path)

    # Reject known fixture/config files
    if base in ("conftest.py", "__init__.py"):
        return False
    if fnmatch.fnmatchcase(base, "jest.config.*"):
        return False
    if fnmatch.fnmatchcase(base, "vitest.config.*"):
        return False

    # Reject paths with fixture/helper segments
    parts = path.split("/")
    if any(p in ("fixtures", "__fixtures__", "helpers") for p in parts):
        return False

    # Check basename against allowed patterns
    allowed_patterns = (
        "test_*.py",
        "*_test.py",
        "*_test.go",
        "*.test.js",
        "*.test.jsx",
        "*.test.ts",
        "*.test.tsx",
        "*.test.mjs",
        "*.test.cjs",
        "*.spec.js",
        "*.spec.jsx",
        "*.spec.ts",
        "*.spec.tsx",
        "*.spec.mjs",
        "*.spec.cjs",
        "*_spec.rb",
    )
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in allowed_patterns)


def get_tree_files(repo: str, rev: str) -> Dict[str, str]:
    """Get all files at rev with their paths. Returns {path: mode}."""
    out = _git(repo, ["ls-tree", "-r", "-z", rev])
    lines = out.decode("utf-8", "replace").split("\0")
    files: Dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        # Format: <mode> SP <type> SP <object> TAB <file>
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files[parts[1]] = parts[0].split()[0]
    return files


_GREP_LINE = re.compile(r"^(?P<path>.*?):(?P<line>\d+):(?P<text>.*)$")


def get_grep_results(repo: str, rev: str, pattern: str) -> List[Tuple[str, int, str]]:
    """Run git grep for a fixed string, return [(file, line_num, line_text)].

    Exit 1 with empty stderr means no match. Any other failure raises ValueError.
    """
    cmd = [
        "git", "--literal-pathspecs", "-C", repo,
        "grep", "-F", "-n", "-e", pattern, rev, "--",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ValueError("cannot run git: %s" % exc)
    if proc.returncode == 1 and not proc.stderr.strip():
        return []
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(
            "git grep failed for %r: %s"
            % (pattern, msg[0] if msg else "exit %d" % proc.returncode)
        )

    # Without -z, each line is "<rev>:<path>:<line>:<text>"; strip the rev prefix.
    prefix = rev + ":"
    results: List[Tuple[str, int, str]] = []
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
        match = _GREP_LINE.match(raw)
        if match is None:
            continue
        results.append((match.group("path"), int(match.group("line")), match.group("text")))
    return results


def check_removal(
    repo: str,
    rev: str,
    removed_path: str,
    removed_set: Optional[Set[str]] = None,
) -> List[str]:
    """Check a single removed file for suite-emptied and references. Returns findings.

    removed_set is the full set of paths removed in this run; suite-emptied is
    judged against what remains after subtracting all of them. Defaults to
    {removed_path} when omitted.
    """
    findings: List[str] = []
    removal_set: Set[str] = removed_set if removed_set is not None else {removed_path}

    # Get all files at base
    files = get_tree_files(repo, rev)

    # Fail closed on a target that does not exist in the base tree.
    if removed_path not in files:
        raise ValueError("removed path not in base tree: %s" % removed_path)

    # Determine directory containing the removed file
    if "/" in removed_path:
        removed_dir = removed_path.rsplit("/", 1)[0]
    else:
        removed_dir = "."

    # Reject an empty module name: grepping "" would match every line.
    basename = _basename(removed_path)
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if not stem:
        raise ValueError("empty module name for removed path: %s" % removed_path)

    # Check if suite would be emptied
    # Suite is emptied if:
    # 1. Directory held test-case files
    # 2. After removing every path in this run, holds no test-case files
    # 3. But still holds other files or is in config/suite lists

    if removed_dir != ".":
        prefix = removed_dir + "/"
        in_dir = [path for path in files.keys() if path.startswith(prefix)]
        test_files_in_dir = [path for path in in_dir if is_test_case_path(path)]
        remaining_tests = [path for path in test_files_in_dir if path not in removal_set]

        # Removed path is a test-case file and nothing test-case is left behind
        if removed_path in test_files_in_dir and not remaining_tests:
            other_files_in_dir = [path for path in in_dir if path not in removal_set]
            if other_files_in_dir:
                # Dir holds other files, report suite-emptied
                findings.append("suite-emptied: %s" % removed_dir)

    # Search for references
    # 1. Full path
    for file_path, line_num, text in get_grep_results(repo, rev, removed_path):
        if file_path != removed_path:  # Skip the file itself
            findings.append("referenced: %s by %s:%d" % (removed_path, file_path, line_num))

    # 2. Basename without extension (as module name)
    module_name = stem
    if "." in basename:
        for file_path, line_num, text in get_grep_results(repo, rev, module_name):
            if file_path != removed_path:
                findings.append("referenced: %s by %s:%d" % (removed_path, file_path, line_num))

    # 3. Dotted import path (e.g., tests.test_a for tests/test_a.py)
    # Strip extension and replace / with .

    if removed_dir != ".":
        import_path = removed_dir.replace("/", ".") + "." + module_name
        for file_path, line_num, text in get_grep_results(repo, rev, import_path):
            if file_path != removed_path:
                findings.append("referenced: %s by %s:%d" % (removed_path, file_path, line_num))

    # 4. Runner config and suite lists
    config_files = {
        "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
        "jest.config.js", "jest.config.ts", "jest.config.cjs", "jest.config.mjs",
        "vitest.config.js", "vitest.config.ts", "vitest.config.cjs", "vitest.config.mjs",
        "package.json", "tests/run_all.py", "scripts/check-minimum.py", "justfile"
    }

    for config_file in config_files:
        if config_file not in files:
            continue

        # Search for the stem in this config file
        for grep_pattern in [removed_path, module_name]:
            for file_path, line_num, text in get_grep_results(repo, rev, grep_pattern):
                if file_path == config_file:
                    findings.append("referenced: %s by %s:%d" % (removed_path, file_path, line_num))

    # Deduplicate findings (keep first occurrence per unique finding)
    seen: Set[str] = set()
    unique_findings: List[str] = []
    for finding in findings:
        if finding not in seen:
            seen.add(finding)
            unique_findings.append(finding)

    return unique_findings


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Static removal check for test files."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument(
        "--remove",
        dest="removed_paths",
        action="append",
        default=None,
        metavar="PATH",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Accepted for compatibility; output is always JSON",
    )
    args = parser.parse_args(argv)

    if not args.removed_paths:
        parser.error("at least one --remove PATH is required")

    removed_paths = args.removed_paths or []

    try:
        rev_sha = resolve_ref(args.repo, args.base)
    except ValueError as exc:
        # Error resolving ref - exit 2
        output = {
            "verdict": "error",
            "findings": [],
            "error": str(exc)
        }
        sys.stdout.write(json.dumps(output) + "\n")
        return 2

    all_findings: List[str] = []

    try:
        removal_set = set(removed_paths)
        for removed_path in removed_paths:
            findings = check_removal(args.repo, rev_sha, removed_path, removal_set)
            all_findings.extend(findings)
    except ValueError as exc:
        # Error reading tree - exit 2
        output = {
            "verdict": "error",
            "findings": [],
            "error": str(exc)
        }
        sys.stdout.write(json.dumps(output) + "\n")
        return 2

    # Deduplicate all findings
    seen: Set[str] = set()
    unique_findings: List[str] = []
    for finding in all_findings:
        if finding not in seen:
            seen.add(finding)
            unique_findings.append(finding)

    verdict = "flag" if unique_findings else "ok"
    output = {
        "verdict": verdict,
        "findings": sorted(unique_findings),
    }

    sys.stdout.write(json.dumps(output) + "\n")

    if verdict == "ok":
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
