#!/usr/bin/env python3
"""
Subprocess tests for the flow-ship / flow-merge Bash guard rows (#359, plan Step 11).

Runs the real scripts/flow-bash-guard.py: sanctioned push forms and `gh stack sync` are
allowed; force variants, admin merges and wrapper indirection are denied; an unknown push
form carries the rule id and the Ask-block escalation text; unknown flow-* agents fail
closed; auto mode resolves each agent to its own row; every agents/flow-*.md has an
explicit row in all three guards.

Run with: python3 tests/test_flow_ship_merge_guard.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
BASH = SCRIPTS / "flow-bash-guard.py"
AGENTS = ("flow-ship", "flow-merge")
SHA_A, SHA_B = "a" * 40, "b" * 40


def run(guard, payload, agent):
    r = subprocess.run([sys.executable, str(guard), "--agent", agent], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    return r.returncode, r.stderr


def bash(command, agent):
    return run(BASH, {"tool_name": "Bash", "tool_input": {"command": command}}, agent)


ALLOWED = [
    "git push --force-with-lease --force-if-includes origin HEAD",
    "git -C /tmp/wt push --force-with-lease",
    "git push -u origin feature/x",
    f"git push --force-with-lease=feature/x:{SHA_A} origin {SHA_B}:refs/heads/feature/x",
    "gh stack sync",
    "gh pr create --title t --body b",
    'eval "$CHECK_CMD"',
]
DENIED_BOTH = [
    "git push --force origin main", "git push -f origin main", "git push -fu origin main",
    "git push --forc origin main", "git push origin +main", "git push origin :main",
    "git push --mirror", "git push --delete origin x", "git push",
    "git push --force-with-lease --force origin HEAD",
    "gh pr merge 1 --admin", "gh api -X POST repos/o/r/issues",
    'eval "$OTHER"',
]


def main():
    h = Harness("flow-ship / flow-merge guard")
    for agent in AGENTS:
        for cmd in ALLOWED:
            rc, err = bash(cmd, agent)
            h.test_result(f"{agent}: allows {cmd[:55]}", rc == 0, err.strip())
        for cmd in DENIED_BOTH:
            rc, _ = bash(cmd, agent)
            h.test_result(f"{agent}: denies {cmd[:55]}", rc == 2, f"rc={rc}")
        rc, err = bash("git push --weird origin x", agent)
        h.test_result(f"{agent}: unknown push form names rule id", rc == 2 and "rule push-form" in err, err.strip())
        rc, err = run(BASH, {"tool_name": "Bash", "tool_input": {"command": "git push --weird"},
                             "agent_type": agent}, "auto")
        h.test_result(f"{agent}: auto mode resolves its own row", rc == 2 and agent in err, err.strip())
    rc, _ = bash("gh pr merge 1 --squash", "flow-ship")
    h.test_result("flow-ship denies gh pr merge --squash", rc == 2, f"rc={rc}")
    rc, _ = bash("gh pr merge 1 --squash", "flow-merge")
    h.test_result("flow-merge allows gh pr merge --squash", rc == 0, f"rc={rc}")

    rc, _ = bash("git status", "flow-bogus")
    h.test_result("unknown flow-* agent fails closed", rc == 2, f"rc={rc}")
    for at in (None, "general-purpose"):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
        if at:
            payload["agent_type"] = at
        rc, _ = run(BASH, payload, "auto")
        h.test_result(f"auto mode passes agent_type={at}", rc == 0, f"rc={rc}")

    for f in sorted((REPO_ROOT / "agents").glob("flow-*.md")):
        name = re.search(r"^name:\s*(\S+)", f.read_text(), re.M).group(1)
        if name == "flow-assessor":
            continue  # tool-restricted (no Write/Edit, git read-only); not hook-guarded
        for guard in ("flow-bash-guard.py", "flow-agent-guard.py", "flow-skill-guard.py"):
            h.test_result(f"{guard} has an explicit row for {name}",
                          f'"{name}"' in (SCRIPTS / guard).read_text() or f"'{name}'" in (SCRIPTS / guard).read_text(),
                          "no explicit mention")
    def forms(text):
        out = []
        for ln in re.search(r"^(?:# |<!-- )flow-bash-push-allowlist:begin.*?\n(.*?)^(?:# |<!-- )flow-bash-push-allowlist:end",
                            text, re.S | re.M).group(1).splitlines():
            ln = re.sub(r"^(#\s*)?(-\s*)?", "", ln.strip())
            if re.match(r"(git|gh) ", ln):
                out.append(ln)
        return sorted(out)

    adr = next((REPO_ROOT / "docs" / "adr").glob("0008-*.md")).read_text()
    h.test_result("ADR-0008 push allow-list block matches the guard's marked block",
                  forms(adr) == forms(BASH.read_text()) and len(forms(adr)) == 5,
                  f"adr={forms(adr)} guard={forms(BASH.read_text())}")
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
