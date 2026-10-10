#!/usr/bin/env python3
"""
Structural test suite for /expert-flow (commands/expert-flow.md), its ADR, and the epic-mode
prompt additions.

Covers:
1. Command existence, frontmatter model: sonnet, documented flags
2. Telemetry: stage-pairing linter clean; all eight stage names listed; Every Exit Path section
3. Relay contract: references flow-reference.md, verifies the answers file before SendMessage,
   never passes --pause, no auto-remediation wording, stop-line format
4. Orchestrator capability: no Edit tool, no write-capable git/gh in allowed-tools
5. ADR-0023 exists, is indexed in docs/adr/README.md, CLAUDE.md lists /expert-flow and ADR-0023,
   the stale /expert-implement-with-haiku-and-ship entry is gone
6. Epic mode: --epic documented in the command; both planning prompts gate Sub-tickets on
   EPIC_MODE: true; /expert-plan accepts --epic and writes the marker line

Run with: python3 tests/test_expert_flow.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

COMMAND = REPO_ROOT / "commands" / "expert-flow.md"
ADR = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
ADR_INDEX = REPO_ROOT / "docs" / "adr" / "README.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
FLOW_REF = REPO_ROOT / "prompts" / "flow-reference.md"
PLAN_CMD = REPO_ROOT / "commands" / "expert-plan.md"
CONTRACT = REPO_ROOT / "prompts" / "plan-contribution-contract.md"
SYNTH = REPO_ROOT / "prompts" / "plan-synthesize-and-check.md"
STAGE_LINT = REPO_ROOT / "scripts" / "check_stage_pairing.py"

STAGES = ["plan", "track", "implement", "review", "fix", "verify", "ship", "merge"]
FLAGS = ["--plan-effort", "--review-effort", "--auto-merge", "--from", "--resume", "--list", "--epic"]


def frontmatter(text):
    """Return the YAML frontmatter block of a command doc, or an empty string."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    return m.group(1) if m else ""


def main():
    h = Harness("EXPERT-FLOW STRUCTURAL TEST SUITE")

    print("[Section 1] Command doc and frontmatter")
    exists = COMMAND.is_file()
    h.test_result("commands/expert-flow.md exists", exists, str(COMMAND))
    text = COMMAND.read_text() if exists else ""
    fm = frontmatter(text)
    h.test_result("frontmatter pins model: sonnet", bool(re.search(r"model:\s*sonnet", fm)))
    for flag in FLAGS:
        h.test_result(f"argument-hint documents {flag}", flag in fm, "missing from frontmatter")
        h.test_result(f"arg parser handles {flag}", f"{flag})" in text or f"{flag}=*)" in text,
                      "no case arm for the flag")

    print()
    print("[Section 2] Telemetry")
    if exists:
        res = subprocess.run([sys.executable, str(STAGE_LINT), str(COMMAND)],
                             capture_output=True, text=True)
        h.test_result("check_stage_pairing.py is clean for expert-flow.md", res.returncode == 0,
                      (res.stdout + res.stderr).strip())
    begins = set(re.findall(r"run-metrics\.py.*stage-begin.*--stage\s+(\S+)", text))
    ends = set(re.findall(r"run-metrics\.py.*stage-end.*--stage\s+(\S+)", text))
    h.test_result("generic stage-begin and stage-end use the same <STEP> placeholder",
                  begins == ends == {"<STEP>"}, f"begins={sorted(begins)} ends={sorted(ends)}")
    stage_list = re.search(r"Stage names \(one `stage-begin`/`stage-end` pair each[^\n]*\):\s*([^\n]+(?:\n[^\n]+)?)", text)
    listed = set(re.findall(r"`([a-z]+)`", stage_list.group(1))) if stage_list else set()
    h.test_result("all eight stage names are listed", set(STAGES) <= listed,
                  f"missing: {sorted(set(STAGES) - listed)}")
    h.test_result("has an Every Exit Path section", "## Every Exit Path" in text)
    h.test_result("command-begin names expert-flow", "command-begin --command expert-flow" in text)
    for outcome in ("success", "interrupted", "failure"):
        h.test_result(f"command-end --outcome {outcome} appears",
                      f"command-end --command expert-flow --outcome {outcome}" in text)

    print()
    print("[Section 3] Relay contract")
    h.test_result("references prompts/flow-reference.md", "flow-reference.md" in text)
    h.test_result("step prompt names the receipt path", "steps/<NN>-<STEP>.md" in text)
    h.test_result("verifies the answers file exists before resuming",
                  "Verify the answers file exists before resuming" in text and "SendMessage" in text)
    h.test_result("never passes --pause to implement", "Never pass `--pause`" in text)
    h.test_result("merge gate offers Merge / Hold / Take over",
                  all(s in text for s in ("**Merge**", "**Hold**", "**Take over**")))
    h.test_result("hard stop offers Retry / Take over / Abort and nothing else",
                  all(s in text for s in ("**Retry**", "**Abort**", "No other option exists")))
    h.test_result("attempts capped at two per step", "two attempts per step" in text)
    h.test_result("stop line format present", "▶️ expert-flow #<ISSUE> — paused at: <STEP>" in text
                  and "resume: /expert-flow --resume <ISSUE>" in text)
    h.test_result("/expert-rebase is suggested, never invoked",
                  bool(re.search(r"never\s+invoked", text)) and "/expert-rebase" in text)
    h.test_result("fix step skipped when every CONFIRMED count is 0",
                  "are all `0`" in text or "every CONFIRMED count is 0" in text)
    h.test_result("verify uses commands.check only", "commands.check" in text and "epic decision 4" in text)
    h.test_result("flow directories never auto-deleted", "never" in text and "auto-deleted" in text)
    h.test_result("--auto-merge is never a stored default", "never a stored default" in text)
    h.test_result("content-is-data rule stated", "data, not instructions" in text)
    h.test_result("marker file is .claude/flow-run", ".claude/flow-run" in text)
    h.test_result("marker excluded via info/exclude, not .gitignore edits", "info/exclude" in text)

    print()
    print("[Section 4] Orchestrator capability (allowed-tools)")
    tools = re.search(r"allowed-tools:\s*(.*)", fm)
    tools = tools.group(1) if tools else ""
    h.test_result("no Edit tool", not re.search(r"(^|,\s*)Edit(,|$)", tools), tools)
    for bad in ("git push", "git commit", "git checkout", "git rebase", "git add", "git reset",
                "gh pr merge", "gh pr create", "gh issue create", "Bash(git:*)", "Bash(gh:*)", "Bash(*)"):
        h.test_result(f"allowed-tools does not grant {bad}", bad not in tools)
    for good in ("Task", "SendMessage", "AskUserQuestion", "Write", "Read"):
        h.test_result(f"allowed-tools grants {good}", good in tools)

    print()
    print("[Section 5] ADR, index, CLAUDE.md")
    h.test_result("docs/adr/0023-expert-flow.md exists", ADR.is_file())
    adr = ADR.read_text() if ADR.is_file() else ""
    h.test_result("ADR titled ADR-0023", adr.startswith("# ADR-0023:"))
    h.test_result("ADR cites the spike (#246) and subagent-per-step", "#246" in adr and "subagent-per-step" in adr)
    h.test_result("ADR records the four epic-level decisions",
                  all(s in adr for s in ("free-text", "--auto-merge", "sequential", "commands.check")))
    index = ADR_INDEX.read_text()
    h.test_result("README index links 0023-expert-flow.md", "(0023-expert-flow.md)" in index)
    claude = CLAUDE_MD.read_text()
    h.test_result("CLAUDE.md lists /expert-flow", "`/expert-flow`" in claude)
    h.test_result("CLAUDE.md lists ADR-0023", "0023-expert-flow.md" in claude)
    h.test_result("stale /expert-implement-with-haiku-and-ship entry removed",
                  "/expert-implement-with-haiku-and-ship" not in claude)
    h.test_result("no commands/expert-implement-with-haiku-and-ship.md exists",
                  not (REPO_ROOT / "commands" / "expert-implement-with-haiku-and-ship.md").exists())
    h.test_result("flow-reference.md points at ADR-0023", "0023-expert-flow.md" in FLOW_REF.read_text())

    print()
    print("[Section 6] Epic mode")
    h.test_result("command has an Epic mode section", "## Epic mode" in text)
    h.test_result("epic plan step passes --epic", "--flow <FLOW_DIR> --epic" in text)
    h.test_result("epic approval has Approve / Edit / Stop", all(s in text for s in ("**Approve**", "**Edit**", "**Stop**")))
    h.test_result("epic sub-issues carry Part of #<epic>", "Part of #<ISSUE>" in text)
    h.test_result("epic is sequential; --stack deferred", "`--stack`" in text and "Not in v1" in text)
    contract = CONTRACT.read_text()
    synth = SYNTH.read_text()
    for name, body in (("plan-contribution-contract.md", contract), ("plan-synthesize-and-check.md", synth)):
        h.test_result(f"{name} has a Sub-tickets section", "Sub-tickets" in body)
        h.test_result(f"{name} gates Sub-tickets on EPIC_MODE: true", "EPIC_MODE: true" in body)
        h.test_result(f"{name} requires depends-on to point backwards", "depends-on" in body and "backwards" in body)
    plan = PLAN_CMD.read_text()
    h.test_result("expert-plan.md parses --epic", "--epic)" in plan)
    h.test_result("expert-plan.md writes EPIC_MODE: true into context.md", "EPIC_MODE: true" in plan)
    h.test_result("expert-plan.md parses --flow", "--flow" in plan)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
