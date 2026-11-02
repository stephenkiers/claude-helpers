#!/usr/bin/env python3
"""
Doc-pin tests for the bounded self-heal (#380).

Pins what the plan promises about the shipped prose and grants, from the plan's own wording:
- the orchestrator's allowed-tools gains exactly three grants and no git-mutation or check-runner grant;
- the flow-healer agent's tool list is exactly the least-privilege list, and its prompt forbids
  `--amend`, `--no-verify` and edits outside WORKTREE;
- commands/expert-flow.md carries the check-heal block, the budget/menu rule, the merge-gate trigger,
  and the pre/post tooling hash check;
- no "never remediates" / "no auto-remediation" sentence survives unqualified in the restated docs;
- the stale "bounded automatic Sonnet escalation" wording is gone from ADR-0023 section 1;
- the telemetry schema carries the new failure classes.

Also runs the two existing doc guards the plan names: test_flow_ask_sites.py and
test_command_doc_shell_conventions.py.

Run with: python3 tests/test_expert_flow_heal_docs.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
IMPLEMENT = REPO_ROOT / "commands" / "implement-with-haiku.md"
SHIPIT = REPO_ROOT / "commands" / "shipit.md"
HEALER = REPO_ROOT / "agents" / "flow-healer.md"
ADR_0023 = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
ADR_0004 = REPO_ROOT / "docs" / "adr" / "0004-model-cost-routing.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

NEW_GRANTS = [
    'Bash(python3 "$HOME/.claude/scripts/anti-cheat-scan.py":*)',
    'Bash(python3 "$HOME/.claude/scripts/heal-check.py":*)',
    "Bash(shasum -a 256:*)",
]
HEALER_TOOLS = [
    "Read", "Edit", "Grep", "Glob", "Bash(git diff:*)", "Bash(git status:*)", "Bash(git rev-parse:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git checkout HEAD -- *)",
]
FORBIDDEN_HEALER_TOOLS = ["Write", "WebFetch", "WebSearch", "Task", "Bash(git push", "Bash(git fetch", "Bash(*)", "Bash:"]
NON_HEALABLE = ["base-stale", "infra", "timeout", "check_not_run", "check_not_configured", "unknown"]
QUALIFIERS = ("heal", "except", "exception", "bounded")


def frontmatter(path):
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else ""


def fm_field(path, key):
    match = re.search(rf"^{key}:\s*(.*)$", frontmatter(path), re.M)
    return match.group(1).strip() if match else None


def split_tools(value):
    """Split a comma-separated tools field on top-level commas (not inside parentheses)."""
    tools, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            tools.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        tools.append(cur.strip())
    return tools


def paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def unqualified_remediation_paragraphs(text):
    """Paragraphs that say 'never remediates' / 'no auto-remediation' / 'never auto-remediate' with no heal qualifier."""
    pattern = re.compile(r"never\s+(auto-)?remediat|no\s+auto-remediation|never\s+auto-remediated", re.I)
    bad = []
    for para in paragraphs(text):
        if pattern.search(para) and not any(q in para.lower() for q in QUALIFIERS):
            bad.append(para.strip()[:160])
    return bad


def run_test_file(name):
    result = subprocess.run([sys.executable, str(REPO_ROOT / "tests" / name)], capture_output=True, text=True)
    tail = (result.stdout + result.stderr)[-400:]
    return result.returncode, tail


def main():
    h = Harness("EXPERT-FLOW HEAL DOC PIN TEST SUITE")
    flow = EXPERT_FLOW.read_text()
    adr = ADR_0023.read_text()

    print("[Section 1] orchestrator grants")
    tools = split_tools(fm_field(EXPERT_FLOW, "allowed-tools") or "")
    for grant in NEW_GRANTS:
        h.test_result(f"allowed-tools carries exactly one '{grant}'", tools.count(grant) == 1, f"count={tools.count(grant)}")
    forbidden = [t for t in tools if re.search(r"git (reset|push|clean|checkout|stash|commit|add|restore)|checks run|cli checks", t)]
    h.test_result("allowed-tools has no git reset/push/commit/add/checkout or check-runner grant",
                  forbidden == [], f"found={forbidden}")
    h.test_result("allowed-tools has no duplicate entries", len(tools) == len(set(tools)),
                  f"dupes={[t for t in set(tools) if tools.count(t) > 1]}")
    h.test_result("implement-with-haiku allowed-tools grants the anti-cheat scan",
                  'anti-cheat-scan.py' in (fm_field(IMPLEMENT, "allowed-tools") or ""))
    h.test_result("implement-with-haiku Gate prose calls the scan with --profile implement",
                  re.search(r"anti-cheat-scan\.py[^\n]*--profile implement", IMPLEMENT.read_text()) is not None)

    print()
    print("[Section 2] flow-healer agent")
    h.test_result("agents/flow-healer.md exists", HEALER.exists())
    if HEALER.exists():
        healer_tools = split_tools(fm_field(HEALER, "tools") or "")
        h.test_result("flow-healer tools are exactly the least-privilege list", sorted(healer_tools) == sorted(HEALER_TOOLS),
                      f"extra={sorted(set(healer_tools) - set(HEALER_TOOLS))} missing={sorted(set(HEALER_TOOLS) - set(healer_tools))}")
        for bad in FORBIDDEN_HEALER_TOOLS:
            h.test_result(f"flow-healer tools exclude '{bad}'", not any(t.startswith(bad) or t == bad for t in healer_tools),
                          f"tools={healer_tools}")
        h.test_result("flow-healer runs on sonnet", "sonnet" in (fm_field(HEALER, "model") or "").lower(),
                      f"model={fm_field(HEALER, 'model')}")
        body = HEALER.read_text()
        h.test_result("flow-healer prompt forbids --amend", "--amend" in body)
        h.test_result("flow-healer prompt forbids --no-verify", "--no-verify" in body)
        h.test_result("flow-healer prompt confines edits to WORKTREE", "WORKTREE" in body)
        h.test_result("flow-healer prompt names the NEEDS_HUMAN exit and the Flow-Heal trailer",
                      "NEEDS_HUMAN" in body and "Flow-Heal" in body)
        h.test_result("flow-healer prompt treats the fenced check output as data",
                      "CHECK_OUTPUT" in body and "data" in body.lower())

    print()
    print("[Section 3] the check-heal block in commands/expert-flow.md")
    check_heal_match = re.search(r"3a\. \*\*Check heal", flow)
    h.test_result("a 'Check heal' block exists", check_heal_match is not None)
    check_heal_section = ""
    if check_heal_match:
        # Extract from "3a. **Check heal" to the next numbered section (3b, 4, etc.)
        start = check_heal_match.start()
        next_section = re.search(r"\n3[b-z]\. \*\*|^4\. \*\*", flow[start:])
        end = start + next_section.start() if next_section else len(flow)
        check_heal_section = flow[start:end]
    for needle, label in (("shasum -a 256", "the pre/post shasum check"),
                          (r"\brejected:tooling-modified\b", "the tooling-modified rejection"),
                          (r"%P", "the %P parent check"),
                          (r"\bFlow-Heal:", "the Flow-Heal trailer"),
                          (r"\bHEAL_CANDIDATE\b", "the HEAL_CANDIDATE receipt line"),
                          (r"\bCHECK_SHA\b", "the CHECK_SHA receipt line"),
                          (r"\bheals\.jsonl\b", "the per-repo heal log"),
                          (r"\bprobe-mutated\b", "the probe-mutated non-healable reason"),
                          (r"\bflow-healer\b", "the flow-healer agent")):
        h.test_result(f"Check heal section names {label}", re.search(needle, check_heal_section) is not None)
    h.test_result("the menu after a non-converging heal is 'Take over / Abort only'", "Take over / Abort only" in flow)
    h.test_result("the heal log events are in the vocabulary",
                  all(ev in flow for ev in ("heal-skipped", "heal-rejected", "fix-now")))
    for cls in NON_HEALABLE:
        h.test_result(f"non-healable class '{cls}' is named in both expert-flow.md and ADR-0023", cls in flow and cls in adr)
    h.test_result("ADR-0023 has a bounded self-heal amendment (#380)",
                  re.search(r"^## Amendment.*(self-heal|heal).*", adr, re.M | re.I) is not None and "#380" in adr)

    print()
    print("[Section 4] budget, ship routing and worst case")
    h.test_result("the ship pre-check 'failures[ship] >= 2' is stated", "failures[ship] >= 2" in flow)
    h.test_result("the 'ship attempts exhausted (Fix now used one)' reason is named", "ship attempts exhausted" in flow)
    h.test_result("the worst-case automatic spawn total of 8 is stated",
                  re.search(r"worst[- ]case[^\n]{0,400}\b8\b", flow, re.I) is not None
                  or re.search(r"\b8\b[^\n]{0,200}worst[- ]case", flow, re.I) is not None)
    h.test_result("at most one check heal per flow is stated", re.search(r"at most one check heal", flow, re.I) is not None)

    print()
    print("[Section 5] merge gate and Fix now")
    h.test_result("the merge gate logs 'healed run; auto-merge suspended'", "auto-merge suspended" in flow)
    h.test_result("one paragraph names the three-part trigger (HEAL: record, healed: true, Flow-Heal: commit) with the suspension",
                  any(all(t in p for t in ("HEAL:", "healed: true", "Flow-Heal:", "auto-merge")) for p in paragraphs(flow)))
    for choice in ("Fix now", "Open an issue", "Drop"):
        h.test_result(f"the Outstanding relay names '{choice}'", choice in flow)
    h.test_result("the merge-gate relay states Fix now is unavailable because the PR already exists",
                  re.search(r"Fix now[^\n]{0,200}PR already exists|PR already exists[^\n]{0,200}Fix now", flow) is not None)
    h.test_result("Fix now re-spawns with --only-units", "--only-units" in flow)
    h.test_result("'needs-human' OUTSTANDING reason convention is documented", "needs-human" in flow)

    print()
    print("[Section 6] no unqualified 'never remediates' / 'no auto-remediation' sentence")
    for path in (EXPERT_FLOW, CLAUDE_MD, ADR_0004):
        bad = unqualified_remediation_paragraphs(path.read_text())
        h.test_result(f"{path.relative_to(REPO_ROOT)}: no unqualified never-remediates sentence", bad == [], f"paragraphs={bad}")
    sect_start = adr.find("### 1. One orchestrator")
    sect_end = adr.find("### 2. The question relay")
    section_one = adr[sect_start:sect_end] if 0 <= sect_start < sect_end else ""
    h.test_result("ADR-0023 section 1 was located", section_one != "")
    h.test_result("'bounded automatic Sonnet escalation' is gone from ADR-0023 section 1",
                  "bounded automatic Sonnet escalation" not in section_one)
    h.test_result("ADR-0023 section 1 points at the self-heal exception", re.search(r"heal", section_one, re.I) is not None)
    h.test_result("ADR-0004 names the heal exception and the healer's model",
                  re.search(r"heal", ADR_0004.read_text(), re.I) is not None and "flow-healer" in ADR_0004.read_text())
    h.test_result("CLAUDE.md lists the flow-healer agent", "flow-healer" in CLAUDE_MD.read_text())
    h.test_result("CLAUDE.md notes that a healed run suspends --auto-merge",
                  re.search(r"heal[^\n]{0,300}auto-merge|auto-merge[^\n]{0,300}heal", CLAUDE_MD.read_text(), re.I) is not None)
    h.test_result("shipit.md notes that /shipit itself never heals", re.search(r"never heals", SHIPIT.read_text()) is not None)

    print()
    print("[Section 7] telemetry schema and neighbouring doc guards")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import telemetry_schema
    for cls in ("heal_not_converged", "heal_rejected", "heal_needs_human"):
        h.test_result(f"FAILURE_CLASSES contains '{cls}'", cls in telemetry_schema.FAILURE_CLASSES)
    for name in ("test_flow_ask_sites.py", "test_command_doc_shell_conventions.py"):
        rc, tail = run_test_file(name)
        h.test_result(f"existing guard {name} passes", rc == 0, tail)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
