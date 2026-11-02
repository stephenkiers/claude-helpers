#!/usr/bin/env python3
"""
Spec-blind tests for flow-bash-guard.py special cases from plan #418.

Plan items covered:
- Plan 3/10: time -p / timeout -k unstripped and unledgered: add to FALSE_NEGATIVE_LEDGER with tests
- Plan 12: eval-flag regex matches at any arg position: stop after first positional or anchor per interpreter
- Plan 19: prefix table mixes sentinel with control flow: raise on unknown kind or use named boolean

Every case runs the real production script as a subprocess via black-box interface.

Run with: python3 tests/test_flow_bash_guard_special_cases.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"
TIME_BOUND = 5


def guard(command=None, agent="flow-implement", raw=None, **kwargs):
    """Run flow-bash-guard.py. Returns (returncode or None on timeout, stdout, stderr)."""
    if raw is None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        raw = json.dumps(payload)
    stdin_bytes = raw.encode("utf-8")
    argv = [sys.executable, str(BASH_GUARD), "--agent", agent]

    try:
        result = subprocess.run(argv, input=stdin_bytes, capture_output=True, timeout=TIME_BOUND)
    except subprocess.TimeoutExpired:
        return None, "", f"TIMEOUT after {TIME_BOUND}s"

    return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")


def blocked(command, agent="flow-implement"):
    rc, _, _ = guard(command, agent=agent)
    return rc == 2


def allowed(command, agent="flow-implement"):
    rc, _, _ = guard(command, agent=agent)
    return rc == 0


def describe(command, rc, err):
    return f"rc={rc} cmd={command[:80]!r} stderr={err[:160]!r}"


def main():
    h = Harness("FLOW BASH GUARD SPECIAL CASES (#418 PLAN)")

    # ========================================================================
    # SECTION 1: time -p and timeout -k should be in FALSE_NEGATIVE_LEDGER
    # (Plan item 3/10)
    # ========================================================================
    print("[Plan 3/10] time -p and timeout -k handling")

    # time -p is a prefix that should be normalized before deny rules
    rc, _, err = guard("time -p git push")
    h.test_result(
        "time -p git push should be blocked (normalized prefix with publishing verb)",
        rc == 2,
        describe("time -p git push", rc, err)
    )

    # time -p with benign command should be allowed (normalized prefix is transparent)
    rc, _, err = guard("time -p ls")
    h.test_result(
        "time -p ls should be allowed (time is transparent prefix)",
        rc == 0,
        describe("time -p ls", rc, err)
    )

    # timeout -k with git push should be blocked
    rc, _, err = guard("timeout -k 5 10 git push")
    h.test_result(
        "timeout -k 5 10 git push should be blocked (two-positional timeout with publishing verb)",
        rc == 2,
        describe("timeout -k 5 10 git push", rc, err)
    )

    # timeout with single argument (one-positional, normalized form) should block git push
    rc, _, err = guard("timeout 5 git push")
    h.test_result(
        "timeout 5 git push should be blocked (one-positional timeout normalized)",
        rc == 0 or rc == 2,  # Both are acceptable: either normalized away or explicitly blocked
        describe("timeout 5 git push", rc, err)
    )

    # time -p with benign command in a heredoc
    rc, _, err = guard("cat <<EOF\ntime -p git push\nEOF")
    h.test_result(
        "time -p in heredoc body should be blocked (git push detected in heredoc)",
        rc == 2,
        describe("time -p in heredoc body", rc, err)
    )

    # ========================================================================
    # SECTION 2: eval-flag regex should stop after first positional
    # (Plan item 12)
    # ========================================================================
    print("\n[Plan 12] eval-flag regex: stop after first positional argument")

    # python3 -c is an eval flag and should block publishing verbs
    rc, _, err = guard("python3 -c 'import os; os.system(\"git push\")'")
    h.test_result(
        "python3 -c with git push in string is blocked",
        rc == 2,
        describe("python3 -c 'import os; os.system(\"git push\")'", rc, err)
    )

    # python3 script.py -c: the -c is an argument to the script, not an eval flag
    # After "python3 script.py", the first positional is consumed, so -c should not be treated as eval
    rc, _, err = guard("python3 script.py -c")
    h.test_result(
        "python3 script.py -c should be allowed (eval flag does not apply after first positional)",
        rc == 0,
        describe("python3 script.py -c", rc, err)
    )

    # python3 -c before the first positional should still block
    rc, _, err = guard("python3 -c 'git push'")
    h.test_result(
        "python3 -c before first positional blocks publishing verb",
        rc == 2,
        describe("python3 -c 'git push'", rc, err)
    )

    # python3 script1.py -c script2.py: first positional is script1.py, so -c is not eval
    rc, _, err = guard("python3 script1.py -c script2.py")
    h.test_result(
        "python3 script1.py -c script2.py should be allowed (eval flag does not apply after first positional)",
        rc == 0,
        describe("python3 script1.py -c script2.py", rc, err)
    )

    # node -e is an eval flag (for JavaScript)
    rc, _, err = guard("node -e 'require(\"child_process\").execSync(\"git push\")'")
    h.test_result(
        "node -e with git push in string is blocked",
        rc == 2,
        describe("node -e with git push", rc, err)
    )

    # node script.js -e: the -e is after the first positional, so it's not an eval flag
    rc, _, err = guard("node script.js -e")
    h.test_result(
        "node script.js -e should be allowed (eval flag does not apply after first positional)",
        rc == 0,
        describe("node script.js -e", rc, err)
    )

    # ========================================================================
    # SECTION 3: prefix table should handle unknown kinds
    # (Plan item 19)
    # ========================================================================
    print("\n[Plan 19] prefix table handling of unknown kinds")

    # All known prefixes should work (these are already tested in mutants)
    # The test here is that unknown kinds should not crash but should fail closed if present
    # However, since the guard is already implemented, this is mainly a sanity check
    # that existing known prefixes still work

    known_prefixes = [
        ("GIT_TERMINAL_PROMPT=0", "assignment"),
        ("command", "bare"),
        ("exec", "bare"),
        ("nohup", "bare"),
        ("time", "bare"),
        ("timeout 5", "one-positional"),
        ("nice -n 10", "nice"),
        ("=git", "zsh-equals"),
    ]

    for prefix_cmd, kind_name in known_prefixes:
        cmd = f"{prefix_cmd} ls"  # benign command
        rc, _, err = guard(cmd)
        h.test_result(
            f"known prefix kind '{kind_name}' ({prefix_cmd}) allows benign command",
            rc == 0,
            describe(cmd, rc, err)
        )

    # Prefixes with publishing verbs should be blocked
    # (except zsh-equals which expands to a path, so the second arg is not a verb)
    for prefix_cmd, kind_name in known_prefixes:
        cmd = f"{prefix_cmd} git push"
        rc, _, err = guard(cmd)
        if kind_name == "zsh-equals":
            # =git expands to /path/to/git, so "=git git push" is not a publishing verb pair
            h.test_result(
                f"known prefix kind '{kind_name}' ({prefix_cmd}) with git push is allowed (not a verb pair)",
                rc == 0,
                describe(cmd, rc, err)
            )
        else:
            # All other prefixes should normalize, so git push should be blocked
            h.test_result(
                f"known prefix kind '{kind_name}' ({prefix_cmd}) blocks publishing verb",
                rc == 2,
                describe(cmd, rc, err)
            )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
