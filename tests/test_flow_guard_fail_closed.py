#!/usr/bin/env python3
"""
Fail-closed subprocess tests for the three /expert-flow hook guards (#405).

Every case runs the real production script as a subprocess, with the payload on stdin and the
--agent argument on argv, exactly as Claude Code's PreToolUse hook does. For each failure path
the test asserts returncode == 2 AND a path-specific stderr marker (D2): the exit code alone
would still pass if a dedicated isinstance or argv check were deleted and the catch-all
caught the error instead.

Covered:
1. Failure paths per guard: bad argv, unknown --agent (Bash guard), malformed or empty JSON,
   non-dict payload, missing tool_name, non-dict tool_input, invalid or missing field, and a
   blocked command.
2. Catch-all (`except Exception`): non-UTF-8 stdin and closed stdin, for all three guards.
3. Pinned pass-through (exit 0): auto mode with a missing or non-flow agent_type, other
   tool_name values, and `--agent flow-review` on the Bash guard.
4. Nesting cap (depth > 64 exits 2 with the depth reason) on the quote-aware path and on the
   heredoc fallback path.
5. Time bounds: wide shallow input on both paths, deep nesting inside a heredoc command, and a
   large heredoc, each under a fixed 5 s wall-clock bound.

Hook timeout source (for the time bounds): no hook `timeout` is configured anywhere in this
repo (install.sh, agents/flow-implement.md, agents/flow-review.md and ADR-0008 all leave it
unset), so Claude Code's default command-hook timeout applies. That default is far above the
5 s bound used here. The bound is fixed, not derived from the default at runtime, so the tests
do not flake under load. The sub-second runtime is the expected behaviour, and a slow run fails
the test by name.

Run with: python3 tests/test_flow_guard_fail_closed.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"
AGENT_GUARD = REPO_ROOT / "scripts" / "flow-agent-guard.py"
SKILL_GUARD = REPO_ROOT / "scripts" / "flow-skill-guard.py"

# Fixed wall-clock bound for each time-bound case (see module docstring for the source).
TIME_BOUND_SECS = 30.0

# Nesting cap of the guard (64 levels): MAX_NESTING constant in flow-bash-guard.py.
# Depth exactly 64 is ALLOWED; depth 65 must be refused on both splitter paths.
AT_CAP_DEPTH = 64
OVER_CAP_DEPTH = 65


def run_guard(guard, args, stdin_bytes=None, close_stdin=False, timeout=TIME_BOUND_SECS):
    """Run a guard as a subprocess. Returns (returncode or None on timeout, stderr, elapsed secs).

    stdin_bytes: bytes written to stdin (None means empty stdin via DEVNULL).
    close_stdin: close fd 0 in the child so sys.stdin is None (catch-all path).
    """
    cmd = [sys.executable, str(guard), *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8:strict"}
    kwargs = {
        "capture_output": True,
        "timeout": timeout,
        "env": env,
    }
    if close_stdin:
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["preexec_fn"] = lambda: os.close(0)
    elif stdin_bytes is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_bytes

    started = time.monotonic()
    try:
        result = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return None, "timeout after {}s".format(timeout), time.monotonic() - started
    elapsed = time.monotonic() - started
    return result.returncode, result.stderr.decode("utf-8", "replace"), elapsed


def payload_bytes(obj):
    """JSON-encode a payload for stdin."""
    return json.dumps(obj).encode("utf-8")


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def nested_groups(open_tok, close_tok, depth, body="ls"):
    """Return `depth` nested groups around `body`, e.g. ('$(', ')', 2, 'ls') -> '$($(ls))'."""
    return open_tok * depth + body + close_tok * depth


# Wide, shallow input: thousands of sibling $(a) groups at depth 1 on one line.
def wide_sibling_groups(count=33000):
    return "; ".join("$(a)" for _ in range(count))


def check_case(h, label, rc, stderr, want_rc, marker):
    """Record one case: exit code and, when given, a path-specific stderr marker."""
    if marker is None:
        ok = rc == want_rc
        detail = f"rc={rc} stderr={stderr.strip()!r}"
    else:
        ok = rc == want_rc and marker in stderr
        detail = f"rc={rc} want marker {marker!r} stderr={stderr.strip()!r}"
    h.test_result(label, ok, detail)


def main():
    h = Harness("FLOW GUARD FAIL-CLOSED TEST SUITE (#405)")

    # ========================================================================
    # SECTION 1: Bash guard failure paths
    # ========================================================================
    print("[Section 1] Bash guard failure paths (flow-bash-guard.py)")

    bash_cases = [
        (
            "bash guard: missing --agent argv exits 2 with Usage marker",
            [], None, "Usage: flow-bash-guard.py",
        ),
        (
            "bash guard: wrong argv flag exits 2 with Usage marker",
            ["--agnt", "flow-implement"], None, "Usage: flow-bash-guard.py",
        ),
        (
            "bash guard: unknown --agent value exits 2 with Unknown --agent marker (W2)",
            ["--agent", "flow-implment"], payload_bytes(bash_payload("ls")), "Unknown --agent value",
        ),
        (
            "bash guard: malformed JSON exits 2 with parse marker",
            ["--agent", "flow-implement"], b"{not json", "Failed to parse JSON payload",
        ),
        (
            "bash guard: empty stdin exits 2 with parse marker",
            ["--agent", "flow-implement"], b"", "Failed to parse JSON payload",
        ),
        (
            "bash guard: non-dict payload exits 2 with not-a-dict marker",
            ["--agent", "flow-implement"], b"[1, 2]", "Invalid payload: not a dict",
        ),
        (
            "bash guard: missing tool_name exits 2 with tool_name marker (W2 message)",
            ["--agent", "flow-implement"], b"{}", "tool_name",
        ),
        (
            "bash guard: non-dict tool_input exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Bash", "tool_input": "git push"}),
            "Invalid tool_input: not a dict",
        ),
        (
            "bash guard: empty command exits 2 with invalid-command marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Bash", "tool_input": {"command": ""}}),
            "Invalid command: not a non-empty string",
        ),
        (
            "bash guard: non-string command exits 2 with invalid-command marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Bash", "tool_input": {"command": 5}}),
            "Invalid command: not a non-empty string",
        ),
        (
            "bash guard: blocked wrapper exits 2 with BLOCKED wrapper marker",
            ["--agent", "flow-implement"],
            payload_bytes(bash_payload('bash -c "ls"')),
            "BLOCKED: shell wrappers",
        ),
        (
            "bash guard: blocked publishing verb exits 2 with BLOCKED verb marker",
            ["--agent", "flow-implement"],
            payload_bytes(bash_payload("git push origin x")),
            "BLOCKED: publishing commands",
        ),
        (
            "bash guard: unterminated quote exits 2 with unparseable marker",
            ["--agent", "flow-implement"],
            payload_bytes(bash_payload('git commit -m "abc')),
            "Unparseable",
        ),
        (
            "bash guard: unclosed command substitution exits 2 with unparseable marker",
            ["--agent", "flow-implement"],
            payload_bytes(bash_payload("$(git status")),
            # "parseable" matches both "Unparseable" and "unparseable" wording.
            "parseable",
        ),
    ]
    for label, args, stdin_bytes, marker in bash_cases:
        rc, stderr, _ = run_guard(BASH_GUARD, args, stdin_bytes)
        check_case(h, label, rc, stderr, 2, marker)

    # ========================================================================
    # SECTION 2: Agent guard failure paths
    # ========================================================================
    print("\n[Section 2] Agent guard failure paths (flow-agent-guard.py)")

    agent_cases = [
        (
            "agent guard: missing --agent argv exits 2 with Usage marker",
            [], None, "Usage: flow-agent-guard.py",
        ),
        (
            "agent guard: malformed JSON exits 2 with parse marker",
            ["--agent", "flow-implement"], b"{not json", "Failed to parse JSON payload",
        ),
        (
            "agent guard: empty stdin exits 2 with parse marker",
            ["--agent", "flow-implement"], b"", "Failed to parse JSON payload",
        ),
        (
            "agent guard: non-dict payload exits 2 with not-a-dict marker",
            ["--agent", "flow-implement"], b"[]", "Invalid payload: not a dict",
        ),
        (
            "agent guard: missing tool_name exits 2 with marker",
            ["--agent", "flow-implement"], b"{}", "Missing tool_name in payload",
        ),
        (
            "agent guard: non-dict tool_input exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Agent", "tool_input": "plan-implementer"}),
            "Invalid tool_input: not a dict",
        ),
        (
            "agent guard: missing subagent_type exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Agent", "tool_input": {}}),
            "Missing or empty subagent_type in tool_input",
        ),
        (
            "agent guard: empty subagent_type exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Task", "tool_input": {"subagent_type": ""}}),
            "Missing or empty subagent_type in tool_input",
        ),
        (
            "agent guard: disallowed subagent_type for flow-implement exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}}),
            "not allowed for flow-implement",
        ),
        (
            "agent guard: disallowed subagent_type for flow-review exits 2 with marker",
            ["--agent", "flow-review"],
            payload_bytes({"tool_name": "Task", "tool_input": {"subagent_type": "plan-implementer"}}),
            "not allowed for flow-review",
        ),
    ]
    for label, args, stdin_bytes, marker in agent_cases:
        rc, stderr, _ = run_guard(AGENT_GUARD, args, stdin_bytes)
        check_case(h, label, rc, stderr, 2, marker)

    # ========================================================================
    # SECTION 3: Skill guard failure paths
    # ========================================================================
    print("\n[Section 3] Skill guard failure paths (flow-skill-guard.py)")

    skill_cases = [
        (
            "skill guard: missing --agent argv exits 2 with Usage marker",
            [], None, "Usage: flow-skill-guard.py",
        ),
        (
            "skill guard: malformed JSON exits 2 with parse marker",
            ["--agent", "flow-implement"], b"{not json", "Failed to parse JSON payload",
        ),
        (
            "skill guard: empty stdin exits 2 with parse marker",
            ["--agent", "flow-implement"], b"", "Failed to parse JSON payload",
        ),
        (
            "skill guard: non-dict payload exits 2 with not-a-dict marker",
            ["--agent", "flow-implement"], b'"Skill"', "Invalid payload: not a dict",
        ),
        (
            "skill guard: missing tool_name exits 2 with marker",
            ["--agent", "flow-implement"], b"{}", "Missing tool_name in payload",
        ),
        (
            "skill guard: non-dict tool_input exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Skill", "tool_input": "implement-with-haiku"}),
            "Invalid tool_input: not a dict",
        ),
        (
            "skill guard: missing skill exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Skill", "tool_input": {}}),
            "Missing or empty skill in tool_input",
        ),
        (
            "skill guard: empty skill exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Skill", "tool_input": {"skill": ""}}),
            "Missing or empty skill in tool_input",
        ),
        (
            "skill guard: disallowed skill for flow-implement exits 2 with marker",
            ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Skill", "tool_input": {"skill": "expert-review"}}),
            "not allowed for flow-implement",
        ),
    ]
    for label, args, stdin_bytes, marker in skill_cases:
        rc, stderr, _ = run_guard(SKILL_GUARD, args, stdin_bytes)
        check_case(h, label, rc, stderr, 2, marker)

    # ========================================================================
    # SECTION 4: Catch-all (`except Exception`) for all three guards
    # ========================================================================
    print("\n[Section 4] Catch-all: non-UTF-8 stdin and closed stdin")

    invalid_utf8 = b"\xff\xfe\xfd{}"
    for guard_label, guard in (
        ("bash", BASH_GUARD),
        ("agent", AGENT_GUARD),
        ("skill", SKILL_GUARD),
    ):
        args = ["--agent", "flow-implement"]
        rc, stderr, _ = run_guard(guard, args, invalid_utf8)
        check_case(
            h,
            f"{guard_label} guard: non-UTF-8 stdin exits 2 with Unexpected error marker",
            rc, stderr, 2, "Unexpected error",
        )

        rc, stderr, _ = run_guard(guard, args, close_stdin=True)
        check_case(
            h,
            f"{guard_label} guard: closed stdin exits 2 with Unexpected error marker",
            rc, stderr, 2, "Unexpected error",
        )

    # ========================================================================
    # SECTION 5: Pinned pass-through (exit 0)
    # ========================================================================
    print("\n[Section 5] Pinned pass-through (exit 0)")

    pass_cases = [
        (
            "bash guard: auto mode with missing agent_type passes through",
            BASH_GUARD, ["--agent", "auto"], payload_bytes(bash_payload("git push origin x")),
        ),
        (
            "bash guard: auto mode with non-flow agent_type passes through",
            BASH_GUARD, ["--agent", "auto"],
            payload_bytes({"agent_type": "plan-implementer", **bash_payload("git push origin x")}),
        ),
        (
            "bash guard: --agent flow-review passes a blocked command through",
            BASH_GUARD, ["--agent", "flow-review"], payload_bytes(bash_payload("git push origin x")),
        ),
        (
            "bash guard: flow-implement passes a non-Bash tool_name through",
            BASH_GUARD, ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Read", "tool_input": {"file_path": "x"}}),
        ),
        (
            "agent guard: auto mode with missing agent_type passes through",
            AGENT_GUARD, ["--agent", "auto"],
            payload_bytes({"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}}),
        ),
        (
            "agent guard: auto mode with non-flow agent_type passes through",
            AGENT_GUARD, ["--agent", "auto"],
            payload_bytes({
                "agent_type": "plan-implementer",
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "general-purpose"},
            }),
        ),
        (
            "agent guard: flow-implement passes a non-Agent/Task tool_name through",
            AGENT_GUARD, ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Read", "tool_input": {"file_path": "x"}}),
        ),
        (
            "skill guard: auto mode with missing agent_type passes through",
            SKILL_GUARD, ["--agent", "auto"],
            payload_bytes({"tool_name": "Skill", "tool_input": {"skill": "expert-review"}}),
        ),
        (
            "skill guard: flow-implement passes a non-Skill tool_name through",
            SKILL_GUARD, ["--agent", "flow-implement"],
            payload_bytes({"tool_name": "Agent", "tool_input": {"subagent_type": "plan-implementer"}}),
        ),
    ]
    for label, guard, args, stdin_bytes in pass_cases:
        rc, stderr, _ = run_guard(guard, args, stdin_bytes)
        h.test_result(label, rc == 0, f"rc={rc} stderr={stderr.strip()!r}")

    # ========================================================================
    # SECTION 6: Nesting cap (depth > 64 exits 2 with the depth reason)
    # ========================================================================
    print("\n[Section 6] Nesting cap: quote-aware path and heredoc fallback path")

    # At-limit cases: depth 64 (MAX_NESTING) is allowed on both paths
    quote_aware_limit = [
        ("quote-aware path: $( nesting at depth 64 (limit) exits 0 allowed",
         "echo " + nested_groups("$(", ")", AT_CAP_DEPTH)),
        ("quote-aware path: ( nesting at depth 64 (limit) exits 0 allowed",
         nested_groups("(", ")", AT_CAP_DEPTH)),
    ]
    for label, command in quote_aware_limit:
        rc, stderr, _ = run_guard(BASH_GUARD, ["--agent", "flow-implement"],
                                  payload_bytes(bash_payload(command)))
        check_case(h, label, rc, stderr, 0, None)

    fallback_limit = (
        "fallback path (heredoc): $( nesting at depth 64 (limit) exits 0 allowed",
        "cat <<EOF ; echo " + nested_groups("$(", ")", AT_CAP_DEPTH) + "\nEOF",
    )
    rc, stderr, _ = run_guard(BASH_GUARD, ["--agent", "flow-implement"],
                              payload_bytes(bash_payload(fallback_limit[1])))
    check_case(h, fallback_limit[0], rc, stderr, 0, None)

    # Over-limit cases: depth 65 is rejected on both paths
    quote_aware_deep = [
        ("quote-aware path: $( nesting at depth 65 exits 2 with nesting marker",
         "echo " + nested_groups("$(", ")", OVER_CAP_DEPTH)),
        ("quote-aware path: ( nesting at depth 65 exits 2 with nesting marker",
         nested_groups("(", ")", OVER_CAP_DEPTH)),
    ]
    for label, command in quote_aware_deep:
        rc, stderr, _ = run_guard(BASH_GUARD, ["--agent", "flow-implement"],
                                  payload_bytes(bash_payload(command)))
        check_case(h, label, rc, stderr, 2, "nesting too deep")

    fallback_deep = (
        "fallback path (heredoc): $( nesting at depth 65 exits 2 with nesting marker",
        "cat <<EOF ; echo " + nested_groups("$(", ")", OVER_CAP_DEPTH) + "\nEOF",
    )
    rc, stderr, _ = run_guard(BASH_GUARD, ["--agent", "flow-implement"],
                              payload_bytes(bash_payload(fallback_deep[1])))
    check_case(h, fallback_deep[0], rc, stderr, 2, "nesting too deep")

    # ========================================================================
    # SECTION 7: Time bounds (each case under a fixed 5 s wall-clock bound)
    # ========================================================================
    print("\n[Section 7] Time bounds (fixed 5 s bound; see module docstring for the timeout source)")

    wide = wide_sibling_groups()
    # Quote-aware path: sibling groups plus a long run of `;` inside double quotes. No
    # unquoted << or #, so the single quote-aware pass runs. Nesting stays below the cap.
    quote_aware_wide = wide + '; echo "' + ";" * 20000 + '"'
    # Fallback path: the sibling groups inside a heredoc command, so the quote-blind fallback
    # runs. No quoted run here: the fallback is quote-blind, so a `"` run would split on `;`.
    fallback_wide = "cat <<EOF\n" + wide + "\nEOF"
    heredoc_deep = "cat <<EOF\n" + nested_groups("(", ")", 100000) + "\nEOF"
    large_heredoc = "cat <<EOF\n" + ("lorem ipsum dolor sit amet\n" * 9000) + "EOF"

    time_cases = [
        (
            "time: wide shallow quote-aware input exits 0 under 30 s",
            quote_aware_wide, 0, None,
        ),
        (
            "time: wide shallow fallback (heredoc) input exits 0 under 30 s",
            fallback_wide, 0, None,
        ),
        (
            "time: deep nesting inside a heredoc command exits 2 (nesting) under 30 s",
            heredoc_deep, 2, "nesting too deep",
        ),
        (
            "time: large (~256 KB) heredoc exits 0 under 30 s",
            large_heredoc, 0, None,
        ),
    ]
    for label, command, want_rc, marker in time_cases:
        rc, stderr, elapsed = run_guard(BASH_GUARD, ["--agent", "flow-implement"],
                                        payload_bytes(bash_payload(command)))
        within = rc is not None and elapsed < TIME_BOUND_SECS
        if marker is None:
            ok = within and rc == want_rc
        else:
            ok = within and rc == want_rc and marker in stderr
        h.test_result(
            label, ok,
            f"rc={rc} elapsed={elapsed:.2f}s bound={TIME_BOUND_SECS}s stderr={stderr.strip()[:120]!r}",
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
