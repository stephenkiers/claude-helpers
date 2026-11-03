#!/usr/bin/env python3
"""
Skill call-tree closure for the flow-ship and flow-merge step agents (#359, plan Step 3).

The Skill guard (scripts/flow-skill-guard.py) allows each restricted agent a fixed set of
skills. That set must equal what the agent's entry command can actually reach through Skill
invocations in the command docs, or a step fails at runtime (too narrow) or the allowlist
silently grants more than the flow needs (too wide). Also asserts no command in a closure
spawns a subagent, since flow-ship and flow-merge have no Agent/Task tool.

A Skill invocation is a mention of the Skill tool with another command named nearby as a
/name token or a `name` skill span.

Run with: python3 tests/test_flow_skill_closure.py
"""

import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

COMMANDS = REPO_ROOT / "commands"
ENTRY = {"flow-ship": "shipit", "flow-merge": "merge-and-cleanup"}
SKILL_MENTION = re.compile(r"\bSkill\b")
SLASH_NAME = re.compile(r"(?<![\w/.-])/([a-z][a-z0-9-]*)\b")
SPAWN = re.compile(r"subagent_type\s*:|\bTask tool\b|\bAgent tool\b")


def load_allowed():
    spec = importlib.util.spec_from_file_location(
        "flow_skill_guard", REPO_ROOT / "scripts" / "flow-skill-guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ALLOWED_SKILLS


# Number of characters to scan before and after each Skill mention when looking for
# invoked command names. This window captures a Skill call in context (/Skill and nearby
# /name or `name` tokens) while excluding passing prose mentions elsewhere in the doc.
WINDOW = 160


def direct_skills(name):
    """Commands a command doc invokes through the Skill tool.

    Only /name tokens and `name` skill spans within the WINDOW character range of a Skill mention
    count, so prose elsewhere in the doc ("complements /shipit") is not an invocation.
    """
    text = (COMMANDS / f"{name}.md").read_text()
    found = set()
    for hit in SKILL_MENTION.finditer(text):
        near = text[max(0, hit.start() - WINDOW):hit.end() + WINDOW]
        tokens = SLASH_NAME.findall(near)
        tokens += re.findall(r"`([a-z][a-z0-9-]*)` skill\b", near)
        for tok in tokens:
            if tok != name and (COMMANDS / f"{tok}.md").exists():
                found.add(tok)
    return found


def closure(entry):
    seen, todo = {entry}, [entry]
    while todo:
        for nxt in direct_skills(todo.pop()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def main():
    h = Harness("FLOW SKILL CLOSURE TEST SUITE (#359)")
    allowed = load_allowed()

    # Positive control: verify the detector finds a known Skill edge.
    # shipit calls stack-sync via Skill (one of the few restricted-agent cross-calls),
    # so the closure from shipit must include stack-sync.
    shipit_direct = direct_skills("shipit")
    h.test_result(
        "positive control: detector finds shipit → stack-sync Skill edge",
        "stack-sync" in shipit_direct,
        f"stack-sync not in direct_skills('shipit')={sorted(shipit_direct)}",
    )

    for agent, entry in ENTRY.items():
        reach = closure(entry)
        h.test_result(
            f"{agent}: ALLOWED_SKILLS equals the Skill closure from /{entry}",
            reach == allowed[agent],
            f"closure={sorted(reach)} allowed={sorted(allowed[agent])}",
        )
        for name in sorted(reach):
            spawns = SPAWN.findall((COMMANDS / f"{name}.md").read_text())
            h.test_result(
                f"{agent}: /{name} in the closure spawns no subagent",
                not spawns,
                f"found {spawns}",
            )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
