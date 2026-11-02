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

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

# Number of push forms in the ADR-0008 flow-bash-push-allowlist block (Forms 1-4; Form 4 has two
# variants). Update together with that block.
EXPECTED_PUSH_FORMS = 5

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


# ALLOWED: test cases for commands that should be allowed by the guard.
# SANCTIONED in test_flow_bash_corpus.py is extracted from actual closure docs (different curated list)
ALLOWED = [
    "git push --force-with-lease --force-if-includes origin HEAD",
    "git -C /tmp/wt push --force-with-lease",
    "git push -u origin feature/x",
    f"git push --force-with-lease=feature/x:{SHA_A} origin {SHA_A}:refs/heads/feature/x",
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
    "git -C /tmp/wt push origin main --force",  # Extra flag
    "git --no-pager push --force origin main",  # Global option before push with --force
    f"git push --force-with-lease=feature/x:tooshort origin {SHA_A}:refs/heads/feature/x",  # Short SHA
    f"git push --force-with-lease=feature/x:{SHA_A} origin {SHA_B}:refs/heads/other",  # Branch mismatch
]


def main():
    h = Harness("flow-ship / flow-merge guard")

    # Import the guard to test explicit agent rows
    def _load(modname, path):
        sp = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(mod)
        return mod

    agent_guard = _load("flow_agent_guard", SCRIPTS / "flow-agent-guard.py")
    skill_guard = _load("flow_skill_guard", SCRIPTS / "flow-skill-guard.py")
    spec = importlib.util.spec_from_file_location("flow_bash_guard", BASH)
    flow_bash_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flow_bash_guard)

    for agent in AGENTS:
        for cmd in ALLOWED:
            rc, err = bash(cmd, agent)
            h.test_result(f"{agent}: allows {cmd[:55]}", rc == 0, err.strip())
        for cmd in DENIED_BOTH:
            rc, _ = bash(cmd, agent)
            h.test_result(f"{agent}: denies {cmd[:55]}", rc == 2, f"rc={rc}")
        rc, err = bash("git push --weird origin x", agent)
        h.test_result(f"{agent}: unknown push form names rule id and escalation hint",
                      rc == 2 and "rule push-form" in err and "Ask block" in err, err.strip())
        rc, err = run(BASH, {"tool_name": "Bash", "tool_input": {"command": "git push --weird"},
                             "agent_type": agent}, "auto")
        h.test_result(f"{agent}: auto mode resolves its own row", rc == 2 and agent in err, err.strip())

    # Test flow-ship denies ALL gh pr merge
    rc, err = bash("gh pr merge 1 --squash", "flow-ship")
    h.test_result("flow-ship denies gh pr merge (any flags) and includes escalation hint",
                  rc == 2 and "gh-pr-merge-ship" in err and "Ask block" in err, err.strip())
    rc, err = bash("gh pr merge 1", "flow-ship")
    h.test_result("flow-ship denies gh pr merge without flags", rc == 2, err.strip())

    # Test flow-merge gh pr merge --squash: behavior depends on merge-queue config
    # If merge-queue exists (deny), if it doesn't exist (allow)
    rc, err = bash("gh pr merge 1 --squash", "flow-merge")
    has_merge_queue = (REPO_ROOT.parent.parent / "merge-queue.json").exists()
    if has_merge_queue:
        h.test_result("flow-merge denies gh pr merge --squash (merge-queue exists)", rc == 2 and "gh-pr-merge-queue" in err, err.strip())
    else:
        h.test_result("flow-merge allows gh pr merge --squash (no merge-queue)", rc == 0, f"rc={rc}")

    rc, _ = bash("git status", "flow-bogus")
    h.test_result("unknown flow-* agent fails closed", rc == 2, f"rc={rc}")
    for at in (None, "general-purpose"):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
        if at:
            payload["agent_type"] = at
        rc, _ = run(BASH, payload, "auto")
        h.test_result(f"auto mode passes agent_type={at}", rc == 0, f"rc={rc}")

    # Test escalation hint appears in denial messages for specific rules
    escalation_in_rules = ["push-form", "gh-pr-merge-admin", "gh-pr-merge-ship"]
    for rule_id in escalation_in_rules:
        # Use the guard module to check that these rules include the escalation hint
        if rule_id == "push-form":
            rc, err = bash("git push --unknown origin x", "flow-ship")
        elif rule_id == "gh-pr-merge-admin":
            rc, err = bash("gh pr merge 1 --admin", "flow-merge")
        elif rule_id == "gh-pr-merge-ship":
            rc, err = bash("gh pr merge 1", "flow-ship")
        h.test_result(f"Rule {rule_id} includes escalation hint in message",
                      "Ask block" in err, f"{rule_id}: {err.strip()}")

    for f in sorted((REPO_ROOT / "agents").glob("flow-*.md")):
        name = re.search(r"^name:\s*(\S+)", f.read_text(), re.M).group(1)
        if name == "flow-assessor":
            continue  # tool-restricted (no Write/Edit, git read-only); not hook-guarded
        # Verify agent is in KNOWN_FLOW_AGENTS constant or is not a flow-* agent
        if name.startswith("flow-"):
            h.test_result(f"flow-bash-guard has explicit handling for {name}",
                          name in flow_bash_guard.KNOWN_FLOW_AGENTS, f"agent {name} not in KNOWN_FLOW_AGENTS")
        for label, table in (("flow-agent-guard.py ALLOWED_SUBAGENT_TYPES", agent_guard.ALLOWED_SUBAGENT_TYPES),
                             ("flow-skill-guard.py ALLOWED_SKILLS", skill_guard.ALLOWED_SKILLS)):
            h.test_result(f"{label} has a table key for {name}", name in table,
                          f"{name} is not a key of {label}")
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
                  forms(adr) == forms(BASH.read_text()) and len(forms(adr)) == EXPECTED_PUSH_FORMS,
                  f"adr={forms(adr)} guard={forms(BASH.read_text())}")
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
