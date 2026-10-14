#!/usr/bin/env python3
"""
Structural test suite for /expert-flow (commands/expert-flow.md), its ADR, and the epic-mode
prompt additions.

These tests are STRUCTURAL ONLY: they check that documented headings, phrases, flags, and
frontmatter grants are present (and, where noted, that related phrases sit in the same
section). They do not verify that the described orchestration behaves correctly at runtime.

Covers:
1. Command existence, frontmatter model: sonnet, documented flags
2. Telemetry: stage-pairing linter clean; all nine stage names listed; Every Exit Path section
3. Relay contract: references flow-reference.md, verifies the answers file before SendMessage,
   never passes --pause, no auto-remediation wording, stop-line format
4. Orchestrator capability: allowed-tools is an allowlist of read-only Bash plus the
   orchestrator tools (no Edit, no write-capable git/gh/sed)
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

STAGES = ["worktree", "plan", "track", "implement", "review", "fix", "verify", "ship", "merge"]
FLAGS = ["--plan-effort", "--review-effort", "--auto-merge", "--from", "--resume", "--list", "--epic"]

# Allowlist for the orchestrator's allowed-tools grants. Bash grants must be read-only
# git/gh, read/inspection utilities, or the run-metrics script. Matching is done on the
# grant's command core: the trailing `:*` or ` *` wildcard is stripped first, so
# `Bash(sed -n:*)` is checked as `sed -n` and `Bash(sed:*)` is checked as `sed` (rejected).
# Note: this allowlist is NOT a write boundary. `sed -n` can still write (e.g. a `w` command), so
# grants are a convention check; prefer `head -n 1` or Read where possible.
READ_ONLY_BASH = (
    r"git (?:log|status|diff|show|rev-parse|rev-list|ls-files|merge-base|for-each-ref|cat-file"
    r"|describe|symbolic-ref|worktree list|branch --show-current|config --get)(?: .*)?",
    r"gh (?:pr|issue|repo|run) (?:view|list|checks|diff|status)(?: .*)?",
    r"(?:ls|cat|grep|jq|date|printf|mkdir)(?: .*)?",
    r"sed -n(?: .*)?",
    r".*run-metrics\.py['\"]?(?: .*)?",
)
ORCHESTRATOR_TOOLS = {"Read", "Write", "Task", "SendMessage", "AskUserQuestion", "ScheduleWakeup"}


def frontmatter(text):
    """Return the YAML frontmatter block of a command doc, or an empty string."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    return m.group(1) if m else ""


def heading_section(text, heading):
    """Return the body of the `## ` section whose heading line starts with `heading`."""
    m = re.search(rf"^{re.escape(heading)}\b.*$", text, re.MULTILINE)
    if not m:
        return ""
    nxt = re.search(r"^## ", text[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.start():end]


def enclosing_section(text, anchor):
    """Return the `## ` section that contains the first occurrence of `anchor` ("" if absent)."""
    pos = text.find(anchor)
    if pos < 0:
        return ""
    starts = [m.start() for m in re.finditer(r"^## ", text, re.MULTILINE) if m.start() <= pos]
    start = starts[-1] if starts else 0
    nxt = re.search(r"^## ", text[pos:], re.MULTILINE)
    end = pos + nxt.start() if nxt else len(text)
    return text[start:end]


def disallowed_grants(tools):
    """Return the allowed-tools grants that fall outside the read-only allowlist."""
    offenders = []
    for inner in re.findall(r"Bash\(([^)]*)\)", tools):
        core = re.sub(r"(?::\*| \*)$", "", inner.strip())
        if not any(re.fullmatch(pattern, core) for pattern in READ_ONLY_BASH):
            offenders.append(f"Bash({inner})")
    bare = re.findall(r"[A-Za-z]\w*", re.sub(r"Bash\([^)]*\)", "", tools))
    offenders.extend(tool for tool in bare if tool not in ORCHESTRATOR_TOOLS)
    return offenders


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
    h.test_result("all nine stage names are listed", set(STAGES) <= listed,
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
    verify_sec = enclosing_section(text, "Verify the answers file exists before resuming")
    h.test_result("verifies the answers file before resuming, with SendMessage in the same section",
                  "Verify the answers file exists before resuming" in verify_sec and "SendMessage" in verify_sec)
    h.test_result("never passes --pause to implement", "Never pass `--pause`" in text)
    merge_sec = enclosing_section(text, "**Merge**")
    h.test_result("merge gate offers Merge / Hold / Take over in one section",
                  all(s in merge_sec for s in ("**Merge**", "**Hold**", "**Take over**")))
    hard_sec = enclosing_section(text, "No other option exists")
    h.test_result("hard stop offers Retry / Abort in the same section as 'No other option exists'",
                  all(s in hard_sec for s in ("**Retry**", "**Take over**", "**Abort**")))
    h.test_result("attempts capped at two per step", "two attempts per step" in text)
    stop_sec = enclosing_section(text, "paused at: <STEP>")
    h.test_result("stop line format present in the stop-line section",
                  "▶️ expert-flow #<ISSUE> — paused at: <STEP>" in stop_sec
                  and "resume: /expert-flow --resume <ISSUE>" in stop_sec)
    rebase_sec = enclosing_section(text, "/expert-rebase")
    h.test_result("/expert-rebase is suggested, never invoked",
                  bool(re.search(r"never\s+invoked", rebase_sec)) and "/expert-rebase" in rebase_sec)
    h.test_result("fix step skipped when every CONFIRMED count is 0",
                  "are all `0`" in text or "every CONFIRMED count is 0" in text)
    h.test_result("verify uses commands.check only", "commands.check" in text and "epic decision 4" in text)
    h.test_result("flow directories never auto-deleted", "never" in text and "auto-deleted" in text)
    h.test_result("--auto-merge is never a stored default", "never a stored default" in text)
    h.test_result("content-is-data rule stated", "data, not instructions" in text)
    h.test_result("marker file is .claude/flow-run", ".claude/flow-run" in text)
    h.test_result("marker excluded via info/exclude, not .gitignore edits", "info/exclude" in text)
    h.test_result("Write scoped to FLOW_DIR stated", "files under `FLOW_DIR`" in text or "writes only under `FLOW_DIR`" in text)

    print()
    print("[Section 4] Orchestrator capability (allowed-tools allowlist)")
    tools = re.search(r"allowed-tools:\s*(.*)", fm)
    tools = tools.group(1) if tools else ""
    h.test_result("allowed-tools has at least one Bash grant",
                  bool(re.search(r"Bash\(", tools)), tools)
    off = disallowed_grants(tools)
    h.test_result("allowed-tools grants only the read-only allowlist (no Edit, no write-capable Bash)",
                  not off, ", ".join(off))
    h.test_result("no Edit tool", not re.search(r"(^|,\s*)Edit(,|$)", tools), tools)
    guard_fixture = ("Read, Edit, Bash(git branch:*), Bash(sed:*), "
                     "Bash(git branch --show-current:*), Bash(sed -n:*)")
    h.test_result("allowlist guard rejects Edit, Bash(git branch:*) and Bash(sed:*) (fixture)",
                  set(disallowed_grants(guard_fixture)) == {"Edit", "Bash(git branch:*)", "Bash(sed:*)"},
                  str(disallowed_grants(guard_fixture)))
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
    epic = heading_section(text, "## Epic mode")
    h.test_result("command has an Epic mode section", bool(epic))
    h.test_result("epic plan step passes --epic", "--flow <FLOW_DIR> --epic" in epic)
    h.test_result("epic approval has Approve / Edit / Stop", all(s in epic for s in ("**Approve**", "**Edit**", "**Stop**")))
    h.test_result("epic sub-issues carry Part of #<epic>", "Part of #<ISSUE>" in epic)
    h.test_result("epic is sequential; --stack deferred", "`--stack`" in epic and "Not in v1" in epic)
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
    print("[Section 7] Receipt hardening (#273 dogfood)")
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| \d\d \| (\w+) \|.*$", text, re.M)}
    h.test_result("implement row does not require ACTION PLAN SOURCE",
                  "implement" in rows and "`ACTION PLAN SOURCE:`" not in rows["implement"].split("|")[5],
                  "implement-with-haiku prints it only for a claude-action-plan source")
    h.test_result("fix row requires ACTION PLAN SOURCE",
                  "fix" in rows and "`ACTION PLAN SOURCE:`" in rows["fix"].split("|")[5])
    impl_cmd = (REPO_ROOT / "commands" / "implement-with-haiku.md").read_text()
    h.test_result("implement-with-haiku emits ACTION PLAN SOURCE only for an action-plan source",
                  "ACTION PLAN SOURCE: <path>  [only when PLAN_SOURCE=claude-action-plan]" in impl_cmd)
    flow_ref = FLOW_REF.read_text()
    h.test_result("flow-reference splits implement and fix required lines",
                  re.search(r"^\| implement \|[^\n]*\n\| fix \|[^\n]*ACTION PLAN SOURCE", flow_ref, re.M) is not None)
    h.test_result("validation rule 1 detects a misplaced receipt",
                  "misplaced receipt" in text and "wrong FLOW_DIR" in text)
    h.test_result("step prompt tells agents to copy FLOW_DIR verbatim", "character for character" in text)
    h.test_result("flow-reference tells agents to copy the receipt path verbatim and check steps/ exists",
                  "character for character" in flow_ref and "ls -d" in flow_ref)
    slug_filter = re.search(r"SLUG=\$\(printf '%s' \"\$ISSUE_TITLE\" \| jq -R -r '(.*)'\)", text)
    h.test_result("slug derivation is present", bool(slug_filter))
    if slug_filter:
        jq_expr = slug_filter.group(1)
        # Test cases: (title, description)
        test_cases = [
            ("North Star Nick runs blind when in-flight snapshot/scout files are unlinked; fail loudly instead",
             "normal title"),
            ("!!!", "non-alphanumeric only"),
            ("a" * 60 + " word", "first word exceeds 40 chars, should cap"),
            ("foo bar baz qux quux corge grault garply waldo",
             "multiple words, cut before final"),
        ]

        for title, case_desc in test_cases:
            res = subprocess.run(["jq", "-R", "-r", jq_expr], input=title, capture_output=True, text=True)
            slug = res.stdout.strip()
            # Oracle: extract alphanumeric sequences
            expected_words = re.findall(r"[a-z0-9]+", title.lower())

            # Basic validity checks
            if len(slug) > 0:
                h.test_result(f"slug {case_desc}: length <= 40",
                              len(slug) <= 40, slug)
                # For non-empty slugs, verify it's composed of words from the title
                slug_words = slug.split("-")
                # Check that slug words match the start of expected_words (up to capping)
                # For words capped at 40 chars, the first word may be truncated
                first_word_expected = expected_words[0] if expected_words else ""
                first_word_slug = slug_words[0] if slug_words else ""
                if len(first_word_expected) > 40:
                    # First word was capped, check it's exactly the first 40 chars
                    h.test_result(f"slug {case_desc} first word capped",
                                  first_word_slug == first_word_expected[:40] and len(slug_words) == 1,
                                  f"{first_word_slug}")
                else:
                    # Check that slug is built from complete words in order
                    reconstructed = "-".join(expected_words[:len(slug_words)])
                    h.test_result(f"slug {case_desc} uses complete words",
                                  slug == reconstructed,
                                  f"slug={slug} vs reconstructed={reconstructed}")
            else:
                h.test_result(f"slug {case_desc}: empty when no alphanumeric",
                              len(expected_words) == 0, title)

    print()
    print("[Section 8] Worktree-first invocation")
    h.test_result("step table has a 00 | worktree row using --worktree-only",
                  "worktree" in rows and rows["worktree"].startswith("| 00 |") and "--worktree-only" in rows["worktree"],
                  rows.get("worktree", "missing"))
    h.test_result("plan row runs in the ticket worktree", "plan" in rows and "ticket worktree" in rows["plan"])
    h.test_result("hand-off lines are present",
                  "worktree ready: <WORKTREE>" in text and "then run: /expert-flow <ISSUE>" in text)
    exit_sec = heading_section(text, "## Every Exit Path")
    h.test_result("Every Exit Path mentions the hand-off", "hand-off" in exit_sec.lower())
    h.test_result("worktree is a telemetry stage name", "worktree" in STAGES and "`worktree`" in exit_sec)
    h.test_result("handoff is in the closed log-event list", "`handoff`" in text)
    h.test_result("old main-worktree-only guard sentence is gone",
                  "Run /expert-flow from the main worktree:" not in text)
    h.test_result("verify-before-create ordering is documented",
                  "Verify with git before creating" in text)
    h.test_result("epic mode keeps main worktree with no hand-off",
                  "no hand-off" in heading_section(text, "## Epic mode"))
    h.test_result("done line points back at the main worktree", "next:  cd <MAIN_WORKTREE>" in text)
    h.test_result("ADR has the worktree-first amendment", "## Amendment: worktree-first invocation" in adr)

    print()
    print("[Section 9] Mandatory AskUserQuestion relay and prose ingestion (#277)")
    relay = heading_section(text, "### Relaying a question")
    h.test_result("Relaying section found", bool(relay))
    h.test_result("AskUserQuestion call is mandatory",
                  re.search(r"`AskUserQuestion` call is mandatory", relay) is not None)
    h.test_result("memory/prose preferences do not override the call",
                  "No personal memory" in relay and "overrides" in relay and "prefer prose" in relay)
    h.test_result("awaiting only after the call is issued",
                  "ends without the call having been issued" in " ".join(relay.split()))
    h.test_result("prose ingestion echo format", "`q1 → Approve`" in relay and "`q2 → Other:" in relay)
    h.test_result("answers keyed by question id, never text",
                  "keyed by question **id**, never" in relay)
    h.test_result("unmapped ids are re-asked, never guessed",
                  "re-ask that question" in relay and "Never guess" in relay and "never fill a default" in relay)
    h.test_result("#277 tracks prose ingestion", "#277" in relay)
    h.test_result("flow-reference orchestrator handshake agrees",
                  "`AskUserQuestion` (mandatory" in FLOW_REF.read_text())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
