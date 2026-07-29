#!/usr/bin/env python3
"""
Test suite for ADR-0008: machine-enforced git guard on plan-implementer.

Covers: the hook is wired into plan-implementer.md's frontmatter, the guard script exists and is
executable, its allowlist matches the prose in plan-implementer.md's Constraints section, and the
matching logic itself (both via subprocess and via direct import) blocks destructive git while
allowing the staging-only subset.

Run with: python3 tests/test_git_guard.py
"""

import importlib.util
import json
import re
import subprocess
import sys
from importlib.machinery import SourceFileLoader

from _test_harness import REPO_ROOT, Harness

AGENTS_DIR = REPO_ROOT / "agents"
SCRIPTS_DIR = REPO_ROOT / "scripts"
ADRS_DIR = REPO_ROOT / "docs" / "adr"

GUARD_SCRIPT = SCRIPTS_DIR / "plan-implementer-git-guard.py"
PLAN_IMPLEMENTER = AGENTS_DIR / "plan-implementer.md"
ADR_README = ADRS_DIR / "README.md"
ADR_0008 = ADRS_DIR / "0008-machine-enforced-agent-guardrails.md"


def read(path):
    try:
        return path.read_text()
    except OSError:
        return ""


PLAN_IMPLEMENTER_CONTENT = read(PLAN_IMPLEMENTER)
GUARD_SCRIPT_CONTENT = read(GUARD_SCRIPT)
ADR_README_CONTENT = read(ADR_README)


def load_guard_module():
    loader = SourceFileLoader("plan_implementer_git_guard", str(GUARD_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run_guard(stdin_text):
    """Run the guard script as a subprocess, return (exit_code, stderr)."""
    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stderr


def payload(command):
    return json.dumps({"tool_input": {"command": command}})


if __name__ == "__main__":
    h = Harness("GIT GUARD TEST SUITE (ADR-0008)")
    test_result = h.test_result

    # ============================================================================
    # SECTION 1: files exist / are wired
    # ============================================================================
    print("[Section 1] Files exist and are wired")

    test_result("guard script exists", GUARD_SCRIPT.exists(), "scripts/plan-implementer-git-guard.py not found")
    test_result(
        "guard script is executable",
        GUARD_SCRIPT.exists() and GUARD_SCRIPT.stat().st_mode & 0o111 != 0,
        "guard script is not executable",
    )
    test_result("plan-implementer.md exists", PLAN_IMPLEMENTER.exists(), "File not found")

    test_result(
        "plan-implementer.md frontmatter wires PreToolUse hook",
        "PreToolUse" in PLAN_IMPLEMENTER_CONTENT and "plan-implementer-git-guard.py" in PLAN_IMPLEMENTER_CONTENT,
        "PreToolUse hook wiring for the guard script not found in plan-implementer.md frontmatter",
    )
    test_result(
        "hook matcher targets Bash",
        'matcher: "Bash"' in PLAN_IMPLEMENTER_CONTENT,
        "Expected a Bash matcher for the PreToolUse hook",
    )

    test_result("ADR-0008 exists", ADR_0008.exists(), "docs/adr/0008-machine-enforced-agent-guardrails.md not found")
    test_result(
        "ADR-0008 listed in docs/adr/README.md",
        "0008-machine-enforced-agent-guardrails.md" in ADR_README_CONTENT,
        "ADR-0008 not indexed in docs/adr/README.md",
    )

    print()

    # ============================================================================
    # SECTION 2: single-source cross-check (script allowlist vs. prose)
    # ============================================================================
    print("[Section 2] Allowlist cross-check")

    guard_module = load_guard_module()
    script_allowlist = set(guard_module.ALLOWED_SUBCOMMANDS)

    # Pull the subcommand list out of plan-implementer.md's Constraints prose — it's the
    # backtick-delimited list of git subcommands between "are permitted" and "Everything else".
    prose_match = re.search(
        r"are permitted\s*[—-]*\s*(.*?)\s*\.\s*Everything else",
        PLAN_IMPLEMENTER_CONTENT,
        re.DOTALL,
    )
    prose_allowlist = set()
    if prose_match:
        prose_allowlist = set(re.findall(r"`([a-z-]+)`", prose_match.group(1)))

    test_result(
        "Constraints prose lists a subcommand allowlist",
        len(prose_allowlist) > 0,
        "Could not find a backtick-delimited subcommand allowlist in plan-implementer.md Constraints",
    )
    test_result(
        "Script allowlist matches Constraints prose allowlist",
        script_allowlist == prose_allowlist,
        f"script={sorted(script_allowlist)} prose={sorted(prose_allowlist)}",
    )

    print()

    # ============================================================================
    # SECTION 3: blocked commands (exit 2)
    # ============================================================================
    print("[Section 3] Blocked commands (exit 2)")

    blocked_commands = [
        "git reset --hard HEAD",
        "git reset",
        "git checkout HEAD -- file",
        "git stash",
        "git commit -m x",
        "git push",
        "git status && git reset",
        "git -C /tmp/wt reset",
        "git -c alias.x='reset --hard' x",
        "git RESET",
        "multi\nline\ngit reset",
        "git rm -f somefile",
        "git mv a b",
        "git --no-pager status",
    ]
    for cmd in blocked_commands:
        code, stderr = run_guard(payload(cmd))
        test_result(f"blocked: {cmd!r}", code == 2, f"expected exit 2, got {code} (stderr={stderr!r})")

    code, stderr = run_guard("not json")
    test_result("blocked: malformed JSON (fail-closed)", code == 2, f"expected exit 2, got {code}")

    code, stderr = run_guard("{}")
    test_result("blocked: missing tool_input (fail-closed)", code == 2, f"expected exit 2, got {code}")

    code, stderr = run_guard(json.dumps({"tool_input": {"command": 123}}))
    test_result("blocked: non-string command (fail-closed)", code == 2, f"expected exit 2, got {code}")

    code, stderr = run_guard(payload("git"))
    test_result(
        "blocked: git with no subcommand, does not render 'None'",
        code == 2 and "None" not in stderr,
        f"expected exit 2 and no 'None' in stderr, got code={code} stderr={stderr!r}",
    )

    print()

    # ============================================================================
    # SECTION 4: allowed commands (exit 0)
    # ============================================================================
    print("[Section 4] Allowed commands (exit 0)")

    allowed_commands = [
        "git add -A",
        "git status --porcelain",
        'date +%s > "$(git rev-parse --git-dir)/iwh-agent-start"',
        "cargo test",
        "git diff --staged",
        "git -C /tmp/wt add -A",
        "git -c core.pager=cat status",
        "echo done",
    ]
    for cmd in allowed_commands:
        code, stderr = run_guard(payload(cmd))
        test_result(f"allowed: {cmd!r}", code == 0, f"expected exit 0, got {code} (stderr={stderr!r})")

    print()

    # ============================================================================
    # SECTION 5: direct unit tests of the matching function
    # ============================================================================
    print("[Section 5] Direct unit tests of find_git_subcommands")

    test_result(
        "find_git_subcommands: simple reset",
        guard_module.find_git_subcommands("git reset --hard HEAD") == ["reset"],
    )
    test_result(
        "find_git_subcommands: compound command finds both",
        guard_module.find_git_subcommands("git status && git reset") == ["status", "reset"],
    )
    test_result(
        "find_git_subcommands: -C option skipped",
        guard_module.find_git_subcommands("git -C /tmp/wt add -A") == ["add"],
    )
    test_result(
        "find_git_subcommands: no git occurrence",
        guard_module.find_git_subcommands("cargo test") == [],
    )
    test_result(
        "find_git_subcommands: does not false-positive on --git-dir",
        guard_module.find_git_subcommands("git rev-parse --git-dir") == ["rev-parse"],
    )
    test_result(
        "find_git_subcommands: attached -C form",
        guard_module.find_git_subcommands("git -Cfoo add -A") == ["add"],
    )
    test_result(
        "find_git_subcommands: attached -c form",
        guard_module.find_git_subcommands("git -cuser.name=x status") == ["status"],
    )
    test_result(
        "find_git_subcommands: -c with separate value",
        guard_module.find_git_subcommands("git -c user.name=x status") == ["status"],
    )
    test_result(
        "find_git_subcommands: quoted git invocation",
        guard_module.find_git_subcommands("'git' add -A") == ["add"],
    )
    test_result(
        "find_git_subcommands: path-qualified git",
        guard_module.find_git_subcommands("/usr/bin/git status") == ["status"],
    )
    test_result(
        "find_git_subcommands: git with no subcommand renders <none>",
        guard_module.find_git_subcommands("git") == ["<none>"],
    )

    print()
    h.summarize_and_exit()
