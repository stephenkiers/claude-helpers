"""Shared harness for this repo's invariant test scripts (no pytest dependency)."""

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"

# Shared constants for slug and flag-loop tests
SLUG_PREFIX = "SLUG=$(printf"
FALLBACK_LINE = '[ -n "$SLUG" ] || SLUG="plan"'
FLAG_LOOP_HEAD = "while [ $# -gt 0 ]; do"

EXPERT_PLAN = REPO_ROOT / "commands" / "expert-plan.md"
EXPERT_PLAN_V2 = REPO_ROOT / "commands" / "expert-plan-deprecated-v2.md"
EXPECTED_SITES = {EXPERT_PLAN: 2, EXPERT_PLAN_V2: 1}

USR_BIN_SED = Path("/usr/bin/sed")


def detect_sed_flavor() -> Tuple[str, Optional[str]]:
    """
    Detect sed flavor and return (sed_type, notice).
    - sed_type: "gnu" or "bsd" based on /usr/bin/sed or system detection
    - notice: informational message if native sed is used instead of /usr/bin/sed
    """
    if sys.platform == "darwin":
        # macOS has BSD sed
        if USR_BIN_SED.exists():
            return "bsd", None
        else:
            return "bsd", (
                "NOTICE: /usr/bin/sed not found; testing this host's native sed "
                "(not the BSD-first PATH the test normally enforces)"
            )
    else:
        # On Linux, check if /usr/bin/sed is available and test it
        if USR_BIN_SED.exists():
            result = subprocess.run(
                ["/usr/bin/sed", "--version"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return "gnu", None
            else:
                return "bsd", None
        else:
            # Try to detect with the native sed
            result = subprocess.run(
                ["sed", "--version"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return "gnu", (
                    "NOTICE: /usr/bin/sed not found; testing this host's native sed "
                    "(which is GNU, not the BSD-first PATH the test normally enforces)"
                )
            else:
                return "bsd", (
                    "NOTICE: /usr/bin/sed not found; testing this host's native sed "
                    "(not the BSD-first PATH the test normally enforces)"
                )


def sed_path_env() -> Tuple[dict, Optional[str]]:
    """Return (env, notice). PATH puts /usr/bin and /bin first when /usr/bin/sed exists."""
    env = dict(os.environ)
    if USR_BIN_SED.exists():
        path_parts = ["/usr/bin", "/bin", os.environ.get("PATH", "")]
        env["PATH"] = ":".join(part for part in path_parts if part)
        return env, None
    return env, (
        "NOTICE: /usr/bin/sed not found; testing this host's native sed "
        "(not the BSD-first PATH the test normally enforces)"
    )


def extract_slug_sites(path: Path) -> list:
    """
    Return [(line_number, slug_line, following)] for each SLUG=$(printf line in path.
    Does not filter by fallback line; all SLUG= lines are returned.
    """
    lines = path.read_text().splitlines()
    sites = []
    for index, line in enumerate(lines):
        if line.startswith(SLUG_PREFIX):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            sites.append((index + 1, line, following))
    return sites


def extract_slug_sites_edge_cases() -> list:
    """
    Return [(doc relative path, SLUG line)] for every SLUG line followed by the fallback line.
    For each doc in EXPECTED_SITES, includes an entry with None if no SLUG line is found.
    """
    sites = []
    for doc in EXPECTED_SITES.keys():
        doc_sites = []
        for line_no, slug_line, following in extract_slug_sites(doc):
            if following == FALLBACK_LINE:
                sites.append((doc.relative_to(REPO_ROOT), slug_line))
                doc_sites.append((doc.relative_to(REPO_ROOT), slug_line))
        if not doc_sites:
            sites.append((doc.relative_to(REPO_ROOT), None))
    return sites


def run_slug(site_line: str, title: str, env: dict) -> Tuple[int, str, str]:
    """Run the extracted SLUG line plus fallback under bash. Returns (returncode, stdout, stderr)."""
    script = site_line + "\n" + FALLBACK_LINE + '\nprintf "%s" "$SLUG"\n'
    run_env = dict(env)
    run_env["TICKET_TITLE"] = title
    run_env["LC_ALL"] = "C"
    result = subprocess.run(
        ["bash", "-c", script],
        env=run_env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_pipeline(slug_line: str, fallback_line: str, title: str, env: dict) -> Tuple[int, str, str]:
    """Run the extracted two lines with the title passed only via env, never interpolated."""
    script = f"{slug_line}\n{fallback_line}\nprintf '%s' \"$SLUG\""
    run_env = dict(env)
    run_env["TICKET_TITLE"] = title
    run_env["LC_ALL"] = "C"
    result = subprocess.run(
        ["bash", "-c", script],
        env=run_env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def extract_flag_loop(text: str) -> Optional[str]:
    """
    Return the `while [ $# -gt 0 ]; do` ... matching `done` block (indent-aware),
    or None if not exactly one found.
    """
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if line.strip() == FLAG_LOOP_HEAD:
            indent = line[: len(line) - len(line.lstrip())]
            for j in range(i + 1, len(lines)):
                if lines[j] == indent + "done":
                    blocks.append("\n".join(lines[i : j + 1]))
                    break
    return blocks[0] if len(blocks) == 1 else None


def run_flag_loop(flag_block: str, args: list, home: str) -> Tuple[int, str, str]:
    """
    Run the extracted loop under bash with args as positional parameters and HOME in the given directory.
    Returns (returncode, stdout, stderr).
    """
    preamble = (
        "EFFORT=''\n"
        "MODELS=''\n"
        "FLOW_FLAG_DIR=''\n"
        "EPIC_MODE=''\n"
        "ALLOW_DEGRADED=''\n"
    )
    script = "set -euo pipefail\n" + preamble + flag_block + '\nprintf "MODELS=%s\\n" "$MODELS"\n'
    env = dict(os.environ)
    env["HOME"] = home
    result = subprocess.run(
        ["bash", "-c", script, "_", *args],
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_script_helper(args, stdin_text=None, env=None):
    """Run the run-metrics.py script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)] + args
    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


class Harness:
    def __init__(self, title):
        """Initialise a named test suite and print its banner."""
        self.pass_count = 0
        self.fail_count = 0
        self.failures = []
        print("=" * len(title))
        print(title)
        print("=" * len(title))
        print()

    def test_result(self, test_name, passed, message=""):
        """Record one test result, printing a checkmark or failure line."""
        if passed:
            print(f"✓ {test_name}")
            self.pass_count += 1
        else:
            error_msg = f"✗ {test_name}"
            if message:
                error_msg += f": {message}"
            print(error_msg)
            self.failures.append(error_msg)
            self.fail_count += 1

    def summarize_and_exit(self) -> NoReturn:
        """Print the pass/fail summary and exit with an appropriate status code."""
        print()
        print("=" * 60)
        print("TEST SUMMARY")
        print("=" * 60)
        print(f"Passed: {self.pass_count}")
        print(f"Failed: {self.fail_count}")
        print()

        if self.failures:
            print("FAILURES:")
            for failure in self.failures:
                print(f"  {failure}")
            print()

        if self.fail_count == 0:
            print("✓ All tests PASSED")
            sys.exit(0)
        else:
            print("✗ Some tests FAILED")
            sys.exit(1)
