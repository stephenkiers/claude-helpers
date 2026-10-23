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
7. Human ledger (#287): the flow-ledger.py grant, the HUMAN_LEDGER line after every step spawn's
   ATTEMPT line, the three writer sites, gate_note removal with the legacy read-fallback, the
   --resume print, the flow-reference Ledger block, and the three worker-delivery sites

Run with: python3 tests/test_expert_flow.py
"""

import json
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
REVIEW_CMD = REPO_ROOT / "commands" / "expert-review.md"
IMPL_CMD = REPO_ROOT / "commands" / "implement-with-haiku.md"
LEDGER_GRANT_APPEND = 'Bash(python3 "$HOME/.claude/scripts/flow-ledger.py" append:*)'
LEDGER_GRANT_LIST = 'Bash(python3 "$HOME/.claude/scripts/flow-ledger.py" list:*)'
LEDGER_GRANTS = (LEDGER_GRANT_APPEND, LEDGER_GRANT_LIST)
PANEL_PROMPT = REPO_ROOT / "prompts" / "expert-review-panel.md"
LEDGER_SPAWN_LINE = "HUMAN_LEDGER: human.jsonl"
REDO_GRANT = 'Bash(python3 "$HOME/.claude/scripts/flow-redo.py":*)'

STAGES = ["worktree", "plan", "track", "implement", "review", "fix", "verify", "ship", "merge"]
FLAGS = ["--plan-effort", "--review-effort", "--auto-merge", "--gates", "--from", "--resume", "--list", "--epic"]

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
    # Named exceptions: mutating git grants that exist for the confirmation-gated Rebase action
    # in the Hard stop only (verify base-stale), narrowed per ADR-0023's gated-Rebase amendment.
    r"git fetch origin",
    r"git rebase (?:origin/main|origin/master|--abort)",
    # Named exceptions for Start fresh: resolve the helpers dir, then delete the old worktree
    # through the /cleanup helper (ADR-0023 Start-fresh amendment); human-gated.
    r"source \"\$HOME/\.claude/scripts/resolve-claude-helpers-dir\.sh\"(?::\*)?",
    r"python3 -m scripts\.workflow\.cli (?:cleanup plan|cleanup apply -|worktree detect-parent)(?::\*)?",
    r"realpath(?: .*)?",
    r"sed -n(?: .*)?",
    r".*run-metrics\.py['\"]?(?: .*)?",
    r".*implement-ledger\.py['\"]? (?:locate|render)(?: .*)?",
    r".*flow-verify-receipt\.py['\"]?(?: .*)?",
    r".*flow-ledger\.py['\"]?(?: .*)?",
    r"python3 \"\$HOME/\.claude/scripts/flow-redo\.py['\"]?(?: .*)?",
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


def subsection(text, heading):
    """Return the `### ` section whose heading line starts with `heading`, bounded by the next `##` or `###` heading."""
    m = re.search(rf"^{re.escape(heading)}\b.*$", text, re.MULTILINE)
    if not m:
        return ""
    nxt = re.search(r"^#{2,3} ", text[m.end():], re.MULTILINE)
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


def sub_section(doc, heading):
    """Return the body of the section whose heading line is exactly `heading` (up to the next ## or ###)."""
    start = doc.find(heading + "\n")
    if start < 0:
        return ""
    rest = doc[start + len(heading) + 1:]
    nxt = re.search(r"^#{2,3} ", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def paragraphs_with(doc, *needles):
    """Paragraphs (blank-line separated, whitespace-normalised) that contain every needle."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", doc)]
    return [p for p in paras if all(n in p for n in needles)]


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
    hard_sec = enclosing_section(text, "Then offer the standard menu:")
    h.test_result("hard stop offers Retry / Take over / Abort in the menu section",
                  all(s in hard_sec for s in ("**Retry**", "**Take over**", "**Abort**")))
    h.test_result("attempts capped at two per step", "two attempts per step" in text)
    stop_sec = enclosing_section(text, "paused at: <STEP>")
    h.test_result("stop line format present in the stop-line section",
                  "▶️ expert-flow #<ISSUE> — paused at: <STEP>" in stop_sec
                  and "resume: /expert-flow --resume <ISSUE>" in stop_sec)
    rebase_sec = enclosing_section(text, "/expert-rebase")
    h.test_result("/expert-rebase is suggested, invoked only by the base-stale auto-rebase worker",
                  bool(re.search(r"never\s+invoked outside", rebase_sec)) and "/expert-rebase" in rebase_sec)
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
    print("[Section 8] Worktree-first invocation and no-argument run")
    h.test_result("step table has a 00 | worktree row using --worktree-only",
                  "worktree" in rows and rows["worktree"].startswith("| 00 |") and "--worktree-only" in rows["worktree"],
                  rows.get("worktree", "missing"))
    h.test_result("plan row runs in the ticket worktree", "plan" in rows and "ticket worktree" in rows["plan"])
    # Updated: check for the new quoted template hand-off line instead of old two-command form
    h.test_result("hand-off uses quoted template cd <QUOTED_WORKTREE> && claude \"/expert-flow\"",
                  "worktree ready: <WORKTREE>" in text and "next:  cd <QUOTED_WORKTREE> && claude \"/expert-flow\"" in text,
                  "missing new template or old form still present")
    h.test_result("old 'then run: /expert-flow <ISSUE>' form is gone",
                  "then run: /expert-flow <ISSUE>" not in text)
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

    # New assertions for no-argument run feature
    print()
    print("[Section 8a] No-argument run (bare /expert-flow)")
    noarg_sec = subsection(text, "### No-argument run")
    h.test_result("No-argument run subsection exists", bool(noarg_sec))
    h.test_result("argument-hint shows issue as optional [<issue-url|number>]",
                  "[<issue-url|number>]" in fm, "issue should be optional in argument-hint")
    h.test_result("'Required unless' sentence names the bare run in a ticket worktree",
                  "run bare inside a ticket worktree" in text,
                  "Required unless sentence should say 'run bare inside a ticket worktree'")
    h.test_result("parser message about bare form and flags is present",
                  "(a bare /expert-flow takes no flags)" in text)
    h.test_result("ADR-0023 has the 2026-10-15 amendment heading",
                  "## Amendment (2026-10-15): bare /expert-flow in the ticket worktree" in adr)
    h.test_result("CLAUDE.md states silent resume only in the exact hand-off state, otherwise asks",
                  "A bare `cd <worktree> && claude \"/expert-flow\"` auto-detects the issue; in the exact hand-off state it resumes silently, otherwise it asks as usual." in claude)
    fresh_sec = subsection(text, "### Fresh run: resolve the issue")
    h.test_result("Fresh run section: Existing flow uses EXISTING_DIR when the no-arg route set it",
                  "When the no-arg route set `EXISTING_DIR`, use it" in fresh_sec)
    h.test_result("Fresh run section: Resume continues as --resume \"$EXISTING_DIR\"",
                  "--resume \"$EXISTING_DIR\"" in fresh_sec)
    h.test_result("Per-step details Hand-off paragraph pins the bare resume sentence",
                  "Running bare `/expert-flow` inside the worktree resumes this flow at `plan` without a question"
                  in subsection(text, "### Per-step details"))
    h.test_result("trail line contains 'from the main worktree to choose Start fresh'",
                  "from the main worktree to choose Start fresh" in text)
    h.test_result("Every Exit Path has bare run rows",
                  all(s in exit_sec for s in (
                      "Bare `/expert-flow` in ticket worktree, exact hand-off state (silently resumed)",
                      "No-issue question answered Abort, invalid input, or unanswered (no-arg route `ask`)",
                      "Bare `/expert-flow` with other flags")))
    h.test_result("--from exception line for preserve start point",
                  "/expert-flow <ISSUE> --from <FROM_STEP>[ --plan-file <QUOTED_PLAN_FILE>]" in text)
    h.test_result("non-GitHub worktree guard message is present",
                  "this worktree is not for a GitHub issue — run /expert-flow <issue> from the main worktree" in text)
    h.test_result("No-argument run section states the exact hand-off predicate (step: plan, status: paused)",
                  "`step==plan`, `status==paused`, `live==null`, `attempts.plan==0`" in noarg_sec)

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
    print("[Section 10] Stale/abandoned flows, resume re-asks, receipt-first (B2)")
    def sub(heading):
        return text.split(heading + "\n", 1)[1].split("\n### ", 1)[0] if heading + "\n" in text else ""
    flow_state = heading_section(text, "## Flow state")
    h.test_result("abandoned is in the status enum",
                  re.search(r"\| `status` \| enum \|[^\n]*`abandoned`", flow_state) is not None)
    h.test_result("abandoned is in the log-event list", "`aborted`, `abandoned`, `handoff`" in " ".join(flow_state.split()))
    args_sec = heading_section(text, "## Arguments")
    h.test_result("Arguments documents --abandon", "--abandon <flow-dir|issue#>" in args_sec)
    h.test_result("--abandon with another flag is a guard error", "--abandon takes no other flags" in text)
    h.test_result("--abandon is exempt from the location check", "`--list` and `--abandon` are exempt" in text)
    h.test_result("--abandon exit row in Every Exit Path", re.search(r"^\| `--abandon`", exit_sec, re.M) is not None)
    h.test_result("abandon section sets abandoned and live null",
                  "status: abandoned" in text and "live: null" in sub("### `--abandon`"))
    h.test_result("resume rule asks about an abandoned flow",
                  "Resume it?" in text and "Yes — resume (Recommended)" in text)
    list_sec = sub("### `--list`")
    h.test_result("--list marks stale flows", "stale" in list_sec and ("10800" in list_sec or "3 hours" in list_sec))
    h.test_result("--list jq uses 10800 seconds", "10800" in list_sec)
    h.test_result("resume relays the pending question before re-running",
                  "relay that question file now" in text and "PRIOR_ANSWERS" in text and "**not** step 6" in text and
                  "For report-kind files, never add a `PRIOR_ANSWERS` line" in text)
    ask_sec = heading_section(FLOW_REF.read_text(), "## Ask")
    h.test_result("flow-reference Ask has Prior answers rule",
                  "Prior answers" in ask_sec and "reused-answers" in ask_sec and "PRIOR_ANSWERS" in ask_sec)
    h.test_result("receipt read is the first action after a hand-back",
                  "first action" in text and "leaves the flow `running`" in " ".join(text.split()))

    print()
    print("[Section 11] Anti-shortcut rules, fidelity audit, retro (B3)")
    loop = sub("### The step loop")
    tmpl = loop.split("```\nFLOW_DIR=", 1)[1].split("\n```", 1)[0] if "```\nFLOW_DIR=" in loop else ""
    h.test_result("step prompt template says a reduced run is a failure",
                  "reduced run" in tmpl and "Reason: reduced run:" in tmpl)
    h.test_result("receipt rule 7 checks Panel, ROUND NOT RUN, and direct implementation",
                  "7. For `Decision: OK`" in loop and "`Panel:`" in loop and "NOT dispatched" in loop
                  and "NOT RUN" in loop and "implemented directly" in loop and "without dispatching" in loop)
    audit = sub("### Fidelity audit")
    h.test_result("Fidelity audit section exists", bool(audit))
    for needle in ("expert-scout", ".audit.md", "FIDELITY: ok", "FIDELITY: reduced", "<!-- audit-end -->",
                   "reduced run (audit)", "--failure-class reduced-run", "audits.jsonl", "audit failure"):
        h.test_result(f"audit section mentions {needle}", needle in audit)
    h.test_result("missing audit file is an audit failure, not treated as ok",
                  "audit failure" in audit and "fidelity audit failed:" in audit
                  and "treated as `FIDELITY: ok`" not in audit)
    h.test_result("audit covers plan, implement, review, fix and excludes the rest",
                  "`plan`, `implement`, `review`, and `fix`" in audit
                  and "never\nworktree, track, verify, ship, or merge" in audit)
    h.test_result("Retry after a reduced run adds the extra line",
                  "Previous attempt was rejected as a reduced run" in audit.replace("\n", " ")
                  and "<<<REASON" in audit)
    h.test_result("ledger locate passes --flow-dir and --step; render uses --run-key (render has no --repo)",
                  'locate --repo "<WORKTREE>" --flow-dir "$FLOW_DIR" --step <STEP>' in audit
                  and "render --run-key" in audit)
    checklist_lines = text.splitlines()
    checklist = " ".join(" ".join(checklist_lines[i:i + 4])
                         for i, ln in enumerate(checklist_lines) if "implement, fix:" in ln)
    h.test_result("implement, fix checklist: DECISION: ask done row is non-blocking, not NOT RUN (D2)",
                  "DECISION: ask" in checklist and "NOT RUN" in checklist and "reduced" in checklist
                  and "over-threshold-unreported" in checklist, checklist[:200])
    h.test_result("--list prints the reduced-runs tally from audits.jsonl",
                  "audits.jsonl" in list_sec and "reduced runs:" in list_sec)
    h.test_result("Arguments documents --retro", "--retro <flow-dir|issue#>" in args_sec)
    h.test_result("--retro parsed and takes no other flags",
                  "RETRO_REF" in text and "--retro takes no other flags" in text)
    retro_sec = sub("### `--retro`")
    h.test_result("--retro spawns one Opus general-purpose agent writing retro.md",
                  '"opus"' in retro_sec and "general-purpose" in retro_sec and "retro.md" in retro_sec)
    h.test_result("--retro exit row in Every Exit Path", re.search(r"^\| `--retro`", exit_sec, re.M) is not None)

    print()
    print("[Section 12] Verify check fallback, base-stale class, Rebase action (B4)")
    verify_sec = text.split("**verify.**", 1)[1].split("**ship.**", 1)[0] if "**verify.**" in text else ""
    vflat = " ".join(verify_sec.split())
    h.test_result("verify resolves the check via resolve-check with the main worktree fallback",
                  "checks resolve-check" in verify_sec and '--main-worktree "<MAIN_WORKTREE>"' in verify_sec)
    h.test_result("verify records CHECK_SOURCE", "CHECK_SOURCE" in verify_sec)
    h.test_result("verify classifies with CHECK_CLASS base-stale|other",
                  "CHECK_CLASS: base-stale" in verify_sec and "CHECK_CLASS: other" in verify_sec)
    for pat in ("behind origin/", "Branch is stale", "needs rebase", "base-fresh"):
        h.test_result(f"verify stale pattern {pat}", pat in vflat)
    h.test_result("verify reason says base stale", "base stale" in vflat)
    hard = sub("### Hard stop")
    h.test_result("Hard stop auto-rebases only for base-stale verify",
                  "Auto-rebase" in hard and "CHECK_CLASS: base-stale" in " ".join(hard.replace("`", "").split()))
    h.test_result("Auto-rebase falls back to an /expert-rebase worker without pushing",
                  "Worker fallback" in hard and "NEEDS_HUMAN" in hard and "do **not** push" in hard)
    h.test_result("Rebase aborts on conflicts", "git rebase --abort" in hard)
    h.test_result("Rebase is guarded by STACK_IS_STACKED", "STACK_IS_STACKED" in hard and "/expert-rebase" in hard)
    h.test_result("rebased is a log event", "`rebased`" in flow_state)
    h.test_result("Every Exit Path has both Rebase rows",
                  re.search(r"^\| Hard stop → auto-Rebase[^\n]*clean", exit_sec, re.M) is not None
                  and re.search(r"^\| Hard stop → auto-Rebase[^\n]*conflicts", exit_sec, re.M) is not None)
    fm_tools = frontmatter(text)
    h.test_result("allowed-tools grants only the exact git fetch origin / rebase grants for Rebase",
                  all(f"Bash({g})" in fm_tools for g in
                      ("git fetch origin", "git rebase --abort", "git rebase origin/main", "git rebase origin/master"))
                  and "Bash(git fetch origin:*)" not in fm_tools and "Bash(git rebase origin/*:*)" not in fm_tools
                  and "Bash(git rebase --abort:*)" not in fm_tools
                  and "Bash(git fetch:*)" not in fm_tools and "Bash(git rebase:*)" not in fm_tools)

    print("[Section 13] Receipt verification (#276)")

    # Template test
    template_match = re.search(r"Invoke the step's command with the Skill tool — `Skill\(skill='<SKILL>', args='<ARGS>'\)`", text)
    h.test_result("step prompt template names Skill(skill='<SKILL>', args='<ARGS>')",
                  bool(template_match), "Skill tool wording missing")
    h.test_result("step prompt forbids reading the command file",
                  "Do not read the command file" in text)
    h.test_result("step prompt forbids substitution",
                  "never substitute" in text)

    # Rule 7 tests
    h.test_result("validation rule 8 exists", "8. For `Decision: OK` only" in text)
    h.test_result("rule 8 mentions receipt admits deviation", "receipt admits deviation" in text)
    h.test_result("rule 8 mentions artifacts missing", "artifacts missing" in text)
    h.test_result("rule 8 mentions artifact check could not run", "artifact check could not run" in text)
    h.test_result("rule 8 runs after rules 1-7", "after rules 1–7" in text or "after rules 1-7" in text)
    h.test_result("rule 8 runs before OK branch clears live",
                  "before the `OK` branch clears `live`" in text)
    h.test_result("rule 8 runs only on Decision: OK",
                  "Decision: OK` only" in text or "For `Decision: OK` only" in text)

    # Per-step artifact checks
    steps_with_checks = [
        ("plan", "session dir"),
        ("track", "none needed"),
        ("implement", "new commit"),
        ("review", "checkpoints"),
        ("fix", "new commit"),
        ("verify", "none needed"),
        ("ship", "OPEN"),
        ("merge", "MERGED"),
    ]
    for step, keyword in steps_with_checks:
        h.test_result(f"{step} step has Artifact check line",
                      f"**{step}.**" in text and
                      re.search(rf"\*\*{step}\.\*\*.*?Artifact check:", text, re.DOTALL) is not None,
                      f"missing artifact check for {step}")

    # Allowlist test for the new grant
    h.test_result("allowed-tools grants flow-verify-receipt.py",
                  "flow-verify-receipt.py" in tools, tools)
    h.test_result("READ_ONLY_BASH allowlist updated for flow-verify-receipt",
                  any("flow-verify-receipt" in pat for pat in READ_ONLY_BASH))
    h.test_result("READ_ONLY_BASH allowlist updated for flow-ledger",
                  any("flow-ledger" in pat for pat in READ_ONLY_BASH))
    h.test_result("flow-ledger grants pass the read-only allowlist check",
                  all(disallowed_grants(g) == [] for g in LEDGER_GRANTS),
                  str([disallowed_grants(g) for g in LEDGER_GRANTS]))

    # ADR amendment test
    h.test_result("ADR-0023 has section 2.2", "### 2.2" in adr and "#276" in adr)
    h.test_result("ADR 2.2 says receipts are verified against artifacts",
                  "Receipts are verified against artifacts" in adr or "verified against on-disk artifacts" in adr)
    h.test_result("ADR 2.2 uses the word 'not' and 'prove'",
                  ("not proof" in adr or "does not prove" in adr) and "#276" in adr)
    h.test_result("ADR Receipt bullet points to 2.2",
                  "verified against on-disk artifacts — see §2.2" in adr)

    # flow-reference contract tests
    h.test_result("flow-reference lists Deviation: under Forbidden trailer shapes",
                  "a line beginning `Deviation:`" in flow_ref or "Deviation:" in flow_ref)
    h.test_result("flow-reference has no PANEL: in receipt grammar",
                  "PANEL:" not in flow_ref or "PANEL:" not in re.search(r"## Receipt.*?---", flow_ref, re.DOTALL).group(0) if re.search(r"## Receipt", flow_ref) else True)
    h.test_result("flow-reference documents Deviation: is reserved",
                  "Deviation:" in flow_ref and "reserved" in flow_ref)
    h.test_result("flow-reference has Artifact verification paragraph",
                  "Artifact verification" in flow_ref)

    # Naming and format pins
    h.test_result("expert-plan.md pins SESSION_DIR format",
                  "SESSION_DIR=\"$HOME/.claude/plan-sessions/${REPO_KEY}/${SLUG}-${INVOCATION_ID}\"" in plan)
    h.test_result("expert-plan.md pins FINAL_PLAN_PATH format",
                  "FINAL_PLAN_PATH=\"$HOME/.claude/plans/${SLUG}-${INVOCATION_ID}.md\"" in plan)
    h.test_result("expert-plan.md pins INVOCATION_ID format",
                  "INVOCATION_ID=" in plan and "%Y%m%dT%H%M%S" in plan and "%05d" in plan and "$RANDOM" in plan)
    h.test_result("expert-plan.md roster example has Expert header",
                  "| Expert | Concern | Model | Reason |" in plan)
    h.test_result("expert-plan.md Step 1 sentence pins roster format",
                  "roster must be a markdown table" in plan or "table whose header's first cell is `Expert`" in plan)

    join_barrier = (REPO_ROOT / "prompts" / "join-barrier-pattern.md").read_text()
    h.test_result("join-barrier-pattern.md stand-in has ## Decision / FAILED",
                  "## Decision" in join_barrier and re.search(r"## Decision.*?FAILED", join_barrier, re.DOTALL) is not None)
    h.test_result("expert-plan.md stand-in sentence references join-barrier-pattern",
                  "join-barrier-pattern.md" in plan and "Stand-In File" in plan)

    print()
    print("[Section 14] More human in the loop: context-carrying relay, full checkpoint relay, step gates")
    review_cmd = (REPO_ROOT / "commands" / "expert-review.md").read_text()
    ask_block = heading_section(flow_ref, "## Ask")
    h.test_result("flow-reference Ask block defines top-level `summary` and per-question `brief`",
                  '"summary"' in ask_block and '"brief"' in ask_block)
    h.test_result("flow-reference requires summary/brief for expert-plan and expert-review",
                  "**Required**" in ask_block and "/expert-plan" in ask_block and "/expert-review" in ask_block)
    relay = sub("### Relaying a question")
    h.test_result("relay reads `summary` and `brief`", "`summary`" in relay and "`brief`" in relay)
    h.test_result("relay prints the context before the mandatory call",
                  "Print the context, then call the form" in relay and "`AskUserQuestion` call is mandatory" in relay)
    h.test_result("relay falls back to printing the whole context_path file",
                  "whole `context_path` file" in relay)
    h.test_result("relay exception names both gates",
                  "merge gate and the step gates, which are the orchestrator's own decision points" in relay)
    gate = sub("### Step gate")
    h.test_result("Step gate section exists", gate != "")
    h.test_result("Step gate offers Continue / Pause / Abort",
                  "**Continue**" in gate and "**Pause**" in gate and "**Abort**" in gate)
    h.test_result("Step gate runs after plan, implement, review, fix, verify and never ship/merge",
                  "`plan`,\n`implement`, `review`, `fix`, and `verify`" in gate and "never after `worktree`" in gate)
    h.test_result("Step gate is opt-in: skipped unless flags.step_gates, or on a recorded continue",
                  "`flags.step_gates` is false (the default)" in gate and "`gates[STEP]` is already `continue`" in gate
                  and "**It is opt-in** (`--gates`" in gate)
    h.test_result("Step gate prints each step's digest",
                  "`decisions.md`" in gate and "claude-action-plan.md" in gate and "git log --oneline <bases[STEP]>..HEAD" in gate)
    h.test_result("Step gate writes the digest to gates/<NN>-<STEP>.md",
                  "gates/<NN>-<STEP>.md" in gate)
    h.test_result("Step gate prints the delta when a summary was relayed",
                  "only what the relay did not" in gate)
    h.test_result("Step gate Pause closes the command as interrupted and resume re-asks",
                  "command-end --outcome interrupted" in gate and "resume rule 2" in gate)
    h.test_result("free text at a gate is recorded as a gate_note ledger append", "--kind gate_note" in gate)
    h.test_result("OK branch runs the Step gate before STEP advances",
                  'hand off to the "Step gate"' in text and "Continue advances `STEP`" in text)
    h.test_result("resume rule 2 re-asks a gate the human paused at", "never re-run — whatever `status` says" in text)
    # Section-scoped: the resume rules sit under the `--resume` heading, up to the next `### ` heading.
    # Whitespace is normalized so the assertions do not depend on how the markdown is hard-wrapped.
    resume_m = re.search(r"^### `--resume`.*?(?=^### )", text, re.M | re.S)
    resume = " ".join(resume_m.group(0).split()) if resume_m else ""
    h.test_result("resume rule 2 checks how a taken-over OK was reached before its gate",
                  "Check how the `OK` was reached before the gate." in resume
                  and 'grep -E "^[^ ]+ <STEP> (begin|retry|ok|failed|taken-over)$" "$FLOW_DIR/log.md" | tail -n 1' in resume)
    h.test_result("resume: newest ok line goes to the gate as-is",
                  "**Newest line is `ok`** → the loop validated this receipt; go to the gate as-is." in resume)
    h.test_result("resume: hand-finished is keyed to the newest taken-over line, not to failed",
                  "**Newest line is `taken-over`** → **hand-finished**" in resume
                  and "Skip receipt validation entirely (rules 1–8, including the `ATTEMPT:` check) and the Fidelity audit." in resume
                  and "**Newest line is `failed`** → **hand-finished**" not in resume)
    h.test_result("resume: a failed, begin, or retry newest line (or none) is validated, never hand-finished",
                  "**Newest line is `failed`, `begin`, or `retry`, or there is none** → not hand-finished" in resume
                  and "never passed unchecked" in resume)
    h.test_result("resume: hand-finished gate digest says not audited",
                  "digest prefixed by the line `hand-finished, not audited`" in resume)
    h.test_result("resume: hand-finished logs hand-finished, and the taken-over line keeps the marker",
                  "Log `hand-finished` (the newest line stays `taken-over`" in resume
                  and "`hand-finished`, `awaiting`" in text)
    h.test_result("resume: no outcome line is validated, never passed unchecked",
                  "or there is none** → not hand-finished" in resume and "never passed unchecked" in resume)
    # Take over is the only writer of taken-over, so the event must be written at both Take over sites.
    h.test_result("event list names taken-over beside failed and hand-finished",
                  "`failed`, `taken-over`, `hand-finished`" in " ".join(text.split()))
    hard_stop = " ".join(subsection(text, "### Hard stop").split())
    take_over_hs = re.search(r"\*\*Take over\*\* — leave everything as is.*?(?=\*\*Abort\*\*)", hard_stop)
    h.test_result("Hard stop Take over logs taken-over (section-scoped, whitespace-normalized)",
                  bool(take_over_hs) and "log `taken-over` for `<STEP>`" in take_over_hs.group(0))
    gate_block = " ".join(text.split())
    take_over_gate = re.search(r"\*\*Take over\*\* — same as Hold.*?(?=\*\*merge\.\*\*)", gate_block)
    h.test_result("merge gate Take over logs taken-over (section-scoped, whitespace-normalized)",
                  bool(take_over_gate) and "also log `taken-over` for `merge`" in take_over_gate.group(0))
    h.test_result("ADR-0023 second 2026-10-16 amendment keys hand-finished to taken-over",
                  "`taken-over`" in " ".join(adr.split()).split("## Amendment (2026-10-16, second)")[-1])
    h.test_result("resume: rule 8 derives --since with --since-from-log, failing validation if no begin or retry line exists",
                  "--since-from-log" in resume and "no begin or retry line for <STEP>" in resume)
    h.test_result("resume: a resume-time validation failure does not increment failures[STEP]",
                  "**Do not increment `failures[STEP]`** — no attempt ran on resume" in resume)
    h.test_result("ADR-0023 records the 2026-10-16 second amendment (hand-finished is human-attested)",
                  "## Amendment (2026-10-16, second): a hand-finished `OK` is human-attested, not audited" in adr
                  and "**without incrementing `failures[STEP]`**" in " ".join(adr.split()))
    h.test_result("gate Abort resumes to re-ask, never re-run",
                  "never re-run — whatever `status` says" in text and "`--resume`" in enclosing_section(text, "**Abort** — set `status: failed`"))
    h.test_result("gate writes status: paused before the Ask",
                  "Before asking: set `status: paused`, log `paused`, `WRITE-STATE`" in gate)
    h.test_result("flags.step_gates is restored on resume; auto_merge still forced false",
                  "`flags.step_gates` is restored from\n   `state.json`" in text
                  and "`flags.auto_merge` is forced to `false` on resume" in text)
    h.test_result("state documents gates and flags.step_gates",
                  re.search(r"^\| `gates` \|", flow_state, re.M) is not None
                  and re.search(r"^\| `flags\.step_gates` \| boolean \| `--gates` was passed; stored and restored", flow_state, re.M) is not None)
    h.test_result("--gates is off by default and stored in Arguments",
                  "--gates" in args_sec and "**Off by default:**" in args_sec and "restored on `--resume`" in args_sec)
    h.test_result("--gates is rejected with --resume/--abandon/--retro/--list",
                  text.count('|| [ "$GATES" = yes ]') == 4)
    h.test_result("--no-gates is gone from the command",
                  "no-gates" not in text and "no_gates" not in text and "NO_GATES" not in text)
    h.test_result("epic children inherit flags.step_gates",
                  "inherit the parent's `flags.step_gates`" in text)
    h.test_result("Every Exit Path has Pause and Abort at a step gate rows",
                  re.search(r"^\| Pause at a step gate", exit_sec, re.M) is not None
                  and re.search(r"^\| Abort at a step gate", exit_sec, re.M) is not None)
    h.test_result("verifier is invoked with --flow-dir", '--flow-dir "$FLOW_DIR"' in text)
    h.test_result("verifier failure reasons for incomplete checkpoints are documented",
                  "plan checkpoint incomplete" in text and "review rulings incomplete" in text)
    # Rule 8 is the verifier's failure contract. Whitespace is normalized so the assertions do not
    # depend on how the markdown is hard-wrapped.
    rule8_m = re.search(r"^8\. For `Decision: OK` only.*?(?=^Then branch on the validated)", text, re.M | re.S)
    rule8 = " ".join(rule8_m.group(0).split()) if rule8_m else ""
    h.test_result("rule 8 lists the question-files-without-answers checkpoint failure reason",
                  "plan checkpoint incomplete (question files without an answers file" in rule8)
    h.test_result("rule 8 lists the ledger-handles-without-validated-report checkpoint failure reason",
                  "plan checkpoint incomplete (ledger has D/W/Q handles but no report file was validated: " in rule8
                  and "; handles: <handles>)" in rule8)
    h.test_result("rule 8 lists both plan-artifact failure reasons",
                  "plan artifacts missing (FINAL_PLAN_PATH file does not exist)" in rule8
                  and "plan artifacts missing (plan.md)" in rule8)
    audit = sub("### Fidelity audit")
    # Wording guard: the audit assertions below pin exact prose. A deliberate reword must update them in the same change.
    h.test_result("audit preamble names rule 8 (verifier exit 0) as preceding the audit",
                  "rule 8's" in audit.lower() and "flow-verify-receipt.py" in audit and "exits 0" in audit)
    h.test_result("audit prompt text says verifier has enforced counts and marker paths are relative to FLOW_DIR",
                  "verifier" in audit and "already enforced" in audit and "relative to `FLOW_DIR`" in audit)
    h.test_result("plan and review audit lines each contain <FLOW_DIR>/questions/ and <FLOW_DIR>/answers/ paths",
                  audit.count("<FLOW_DIR>/questions/") >= 2 and audit.count("<FLOW_DIR>/answers/") >= 2)
    h.test_result("audit section no longer contains mechanical counting clauses",
                  "count the `#### Q`" not in audit and "count the `### N.`" not in audit and
                  "only those marker-named files whose answers" not in audit and "a marker without its answers file" not in audit)
    h.test_result("audit keeps judgment checks (push back twice, evidence, excerpts)",
                  "did any expert push back twice" in audit and "evidence actually show no protest" in audit)
    h.test_result("audit keeps wording checks (not asked, defaults adopted, reduced run, a ruling was made for the human)",
                  "not asked" in audit and "defaults adopted" in audit and "reduced run" in audit and
                  "a ruling was made for the human" in audit)
    h.test_result("fidelity audit has zero-brief carve-out",
                  "zero `#### Q` headings" in audit and "carries no D/W/Q handles" in audit)
    h.test_result("plan audit line names <FLOW_DIR> before the plan-sessions glob (glob must be present)",
                  "~/.claude/plan-sessions/" in audit and "<FLOW_DIR>" in audit
                  and audit.find("<FLOW_DIR>") < audit.find("~/.claude/plan-sessions/"))
    plan_step5 = plan.split("### Step 5", 1)[1].split("### Step 6", 1)[0] if "### Step 5" in plan else ""
    h.test_result("expert-plan Step 5 fails a run that drops checkpoint questions",
                  "reduced run: checkpoint questions dropped" in plan_step5)
    h.test_result("expert-plan Step 5 Flow branch defines the checkpoint ledger",
                  "## Checkpoint ledger" in plan_step5 and "`seated`" in plan_step5 and "`expected_reactions`" in plan_step5)
    h.test_result("expert-plan Step 5 Flow branch references the reaction contract and tier script",
                  "plan-checkpoint-reaction.md" in plan_step5 and "plan-checkpoint-tiers.py" in plan_step5 and
                  "`accepted-on-continue`" in plan_step5)
    non_flow_section = (plan_step5.split("**Non-flow branch:**", 1)[1].split("**Flow branch:**", 1)[0]
                        if "**Non-flow branch:**" in plan_step5 and "**Flow branch:**" in plan_step5 else "")
    h.test_result("the non-flow Step 5 text in expert-plan.md is unchanged",
                  ("**Every decision brief is asked; none is answered for the human.**" in non_flow_section and
                   "FLOW_DIR" not in non_flow_section and
                   "AskUserQuestion" in non_flow_section and
                   "Open-ended questions" in non_flow_section and
                   "Effort-2 escalation trigger" in non_flow_section))
    h.test_result("expert-review Step 12 carries summary/brief and fails dropped rulings",
                  "reduced run:\nrulings dropped" in review_cmd and "`summary`" in review_cmd and "`review-01`, `review-02`" in review_cmd)
    h.test_result("ADR has the more-human-in-the-loop amendment",
                  "## Amendment (2026-10-14): more human in the loop" in adr and "`--no-gates`" in adr)
    h.test_result("ADR has the step-gates-opt-in amendment",
                  "## Amendment (2026-10-16, third): step gates are opt-in" in adr and "`flags.step_gates`" in adr)
    h.test_result("CLAUDE.md mentions step gates and --gates as opt-in",
                  "step gate" in CLAUDE_MD.read_text() and "`--gates`" in CLAUDE_MD.read_text()
                  and "--no-gates" not in CLAUDE_MD.read_text())

    print()
    print("[F1] Answers-file jq gate fixture tests")
    print("=" * 70)

    # F1: Test the answers-file jq gate with valid and invalid fixtures
    try:
        # Valid fixture: all items answered or accepted, loop_end is "continue"
        valid_fixture = {
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "answers": {
                "D1": {"disposition": "answered", "reply_excerpt": "approved"},
                "Q1": {"disposition": "answered", "reply_excerpt": "yes"}
            },
            "loop_end": "continue"
        }

        # Invalid fixture: unresolved item
        invalid_fixture_unresolved = {
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "answers": {
                "D1": {"disposition": "answered", "reply_excerpt": "approved"},
                "Q1": {"disposition": "unresolved"}
            },
            "loop_end": None
        }

        # Invalid fixture: accepted-on-continue without loop_end being "continue"
        invalid_fixture_bad_continue = {
            "kind": "report",
            "round": 1,
            "attempt": 1,
            "answers": {
                "D1": {"disposition": "answered", "reply_excerpt": "approved"},
                "Q1": {"disposition": "accepted-on-continue", "reply_excerpt": "continue"}
            },
            "loop_end": None
        }

        jq_gate = '(.answers|type=="object") and ([.answers[]|select(.disposition=="unresolved")]|length==0) and (.loop_end=="continue" or ([.answers[]|select(.disposition=="accepted-on-continue")]|length==0))'

        # Test valid fixture
        valid_result = subprocess.run(
            ['jq', '-e', jq_gate],
            input=json.dumps(valid_fixture),
            text=True,
            capture_output=True
        )
        h.test_result("F1: jq gate passes on valid answers file (all answered, loop_end=continue)",
                      valid_result.returncode == 0)

        # Test invalid fixture: unresolved items
        invalid_result = subprocess.run(
            ['jq', '-e', jq_gate],
            input=json.dumps(invalid_fixture_unresolved),
            text=True,
            capture_output=True
        )
        h.test_result("F1: jq gate fails on unresolved items",
                      invalid_result.returncode != 0)

        # Test invalid fixture: accepted-on-continue without loop_end=continue
        bad_continue_result = subprocess.run(
            ['jq', '-e', jq_gate],
            input=json.dumps(invalid_fixture_bad_continue),
            text=True,
            capture_output=True
        )
        h.test_result("F1: jq gate fails on accepted-on-continue without loop_end=continue",
                      bad_continue_result.returncode != 0)

    except (ImportError, subprocess.CalledProcessError, FileNotFoundError):
        h.test_result("F1: jq gate fixture tests skipped (jq not available)", True)
    except Exception as e:
        h.test_result(f"F1: jq gate fixture tests error: {e}", False)

    print()
    print("[Section 15] Human ledger (human.jsonl, #287)")
    flow_md = COMMAND.read_text()
    flat = " ".join(flow_md.split())
    fm = frontmatter(flow_md)
    h.test_result("allowed-tools grants flow-ledger.py append, list and mark (mark: Redo marks its direction record addressed, #286)",
                  all(g in fm for g in LEDGER_GRANTS) and "flow-ledger.py\" mark" in fm
                  and "flow-ledger.py\":*" not in fm,
                  fm)

    # Spawn line: every fenced step-spawn block with an ATTEMPT=<k> line carries the ledger line
    # directly after it. The fidelity-audit block is the one exemption.
    fenced = re.findall(r"^```[^\n]*\n(.*?)^```", flow_md, re.MULTILINE | re.DOTALL)
    spawn_blocks = [b for b in fenced if "ATTEMPT=<k>" in b]
    non_audit = [b for b in spawn_blocks if "Audit whether this step ran the full command" not in b]
    h.test_result("found the step spawn template blocks carrying ATTEMPT=<k>", len(non_audit) >= 1,
                  f"{len(spawn_blocks)} blocks")
    bad_next = []
    for block in non_audit:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            if "ATTEMPT=<k>" in line:
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt != LEDGER_SPAWN_LINE:
                    bad_next.append(nxt)
    h.test_result("HUMAN_LEDGER: human.jsonl is the line directly after ATTEMPT=<k> in every spawn block",
                  not bad_next, str(bad_next))
    h.test_result("the fidelity-audit block is exempt and identified by its Audit line",
                  any("Audit whether this step ran the full command" in b for b in fenced))

    h.test_result("placement prose: resume rule 5, the template note and the fidelity retry all say 'after the HUMAN_LEDGER line'",
                  flat.count("after the `HUMAN_LEDGER` line") >= 3,
                  str(flat.count("after the `HUMAN_LEDGER` line")))
    h.test_result("stale placement sentence 'right after the ATTEMPT=<k> line' is gone",
                  "right after the `ATTEMPT=<k>` line" not in flat)
    h.test_result("stale fidelity-retry sentence 'directly after ATTEMPT=<k>' is gone",
                  "one extra line directly after `ATTEMPT=<k>`" not in flat)
    h.test_result("template data sentence carves out human.jsonl under the Ledger block (one sentence)",
                  "is read under the Ledger block of `flow-reference.md`, which documents a reserved-but-inactive exception"
                  in flat)

    relay_sec = sub_section(flow_md, "### Relaying a question")
    gate_sec = sub_section(flow_md, "### Step gate")
    ledger_sec = sub_section(flow_md, "### Human ledger")
    h.test_result("Human ledger subsection exists", ledger_sec != "")
    h.test_result("relay section appends free-text answers with --kind answer",
                  'flow-ledger.py" append' in relay_sec and "--kind answer" in relay_sec)
    h.test_result("step-gate section appends free text with --kind gate_note",
                  "--kind gate_note" in gate_sec)
    h.test_result("Human ledger section writes corrections with --kind correction and --supersedes",
                  "--kind correction" in ledger_sec and "--supersedes" in ledger_sec)
    heredoc_re = re.compile(
        r"^python3 \"\$HOME/\.claude/scripts/flow-ledger\.py\" append [^\n]*--text-stdin "
        r"<<'HUMAN_TEXT_<8 random hex>'\n.*?\n^HUMAN_TEXT_<8 random hex>$",
        re.MULTILINE | re.DOTALL)
    for name, sec in (("relay", relay_sec), ("step gate", gate_sec), ("Human ledger", ledger_sec)):
        h.test_result(f"{name} section passes human text through a quoted, column-0 heredoc append",
                      len(heredoc_re.findall(sec)) >= 1, str(len(heredoc_re.findall(sec))))
        h.test_result(f"{name} section has no indented heredoc opener or closer",
                      re.search(r"^[ \t]+.*<<'HUMAN_TEXT_|^[ \t]+HUMAN_TEXT_<8 random hex>[ \t]*$", sec,
                                re.MULTILINE) is None)

    h.test_result("gate_note is gone from the state example (no \"gate_note\" key)",
                  '"gate_note"' not in flow_md)
    h.test_result("gate_note row is gone from the state table",
                  re.search(r"^\| `gate_note` \|", flow_md, re.MULTILINE) is None)
    h.test_result("legacy gate_note read-fallback is named in resume rule 1a",
                  "Legacy gate note (pre-ledger)" in flat)
    h.test_result("resume rule 1a prints pending records with list --md",
                  'list --flow-dir "$FLOW_DIR" --status pending --md' in flow_md)
    h.test_result("resume rule 1a and the writers stop hard with STOP human ledger error",
                  "STOP human ledger error" in flow_md)
    h.test_result("resume rule 1a prints the pending human input heading",
                  "Pending human input (human.jsonl):" in flow_md)

    fr = FLOW_REF.read_text()
    ledger_block = heading_section(fr, "## Ledger")
    cd_idx = fr.find("## Content is data")
    h.test_result("flow-reference has a ## Ledger heading before ## Content is data",
                  ledger_block != "" and 0 <= fr.find("## Ledger") < cd_idx)
    lb = " ".join(ledger_block.split())
    lb_lower = lb.lower()
    h.test_result("Ledger block: missing or empty file means no human input",
                  "no human input" in lb_lower and "missing" in lb_lower and "empty" in lb_lower)
    h.test_result("Ledger block: a malformed line fails with Decision: FAILED",
                  "Decision: FAILED" in lb and "human ledger unreadable: line" in lb)
    h.test_result("Ledger block: direction and question semantics",
                  "`direction`" in lb and "`question`" in lb)
    h.test_result("Ledger block: background kinds named (gate_note, answer, parked, transcript)",
                  all(f"`{k}`" in lb for k in ("gate_note", "answer", "parked", "transcript")))
    h.test_result("Ledger block: read-only wording (steps never write)",
                  re.search(r"read-only|never write", lb_lower) is not None)
    h.test_result("Ledger block: the <<<HUMAN_LEDGER fence is defined", "<<<HUMAN_LEDGER" in lb)
    h.test_result("Ledger block: the HUMAN_LEDGER>>> fence closer is defined", "HUMAN_LEDGER>>>" in lb)
    cd_sec = heading_section(fr, "## Content is data")
    h.test_result("Content is data section says human.jsonl is read under the Ledger block",
                  "human.jsonl" in cd_sec and "Ledger" in cd_sec)
    h.test_result("flow-reference-end sentinel is still the last line",
                  fr.rstrip().splitlines()[-1].strip() == "<!-- flow-reference-end -->")

    plan_md = PLAN_CMD.read_text()
    review_md = REVIEW_CMD.read_text()
    impl_md = IMPL_CMD.read_text()
    h.test_result("expert-plan context.md paragraph delivers the ledger via Passing it to workers",
                  bool(paragraphs_with(plan_md, "Ledger block", "Passing it to workers", "context.md", "Human ledger")))
    panel_md = PANEL_PROMPT.read_text()
    h.test_result("expert-review builds HUMAN_LEDGER_BLOCK from human.jsonl under the Ledger block",
                  bool(paragraphs_with(review_md, "HUMAN_LEDGER_BLOCK", "human.jsonl", "Passing it to workers")))
    h.test_result("expert-review passes HUMAN_LEDGER_BLOCK to Pass 2, the Amalgamator and the Triage Chief only",
                  "HUMAN_LEDGER_BLOCK" in review_md and "Pass 2, the Amalgamator, and the Triage Chief only" in review_md
                  and "never to Pass 1" in review_md)
    s9 = panel_md[panel_md.index("### Step 9:"):panel_md.index("### Step 10:")]
    s10_end = panel_md.index("\n### ", panel_md.index("### Step 10:") + 1)
    s10 = panel_md[panel_md.index("### Step 10:"):s10_end]
    s6 = panel_md[panel_md.index("### Step 6:"):panel_md.index("### Step 7:")]
    h.test_result("panel Step 9 (Pass 2 spawn) supplies HUMAN_LEDGER_BLOCK",
                  "HUMAN_LEDGER_BLOCK" in s9 and "Human ledger (from /expert-flow)" in s9)
    h.test_result("panel Step 10 (Amalgamator spawn) supplies HUMAN_LEDGER_BLOCK",
                  "HUMAN_LEDGER_BLOCK" in s10 and "Human ledger (from /expert-flow)" in s10)
    h.test_result("panel Step 6 (blind Pass 1 spawn) never mentions HUMAN_LEDGER_BLOCK",
                  "HUMAN_LEDGER" not in s6)
    s7_8 = panel_md[panel_md.index("### Step 7:"):panel_md.index("### Step 9:")]
    h.test_result("panel Steps 4-8 (Summarizer, Router, Carl, Haiku Q&A) never mention HUMAN_LEDGER_BLOCK",
                  "HUMAN_LEDGER" not in panel_md[:panel_md.index("### Step 9:")])
    h.test_result("panel Step 7/8 spawns do not carry the ledger (checked explicitly)",
                  "HUMAN_LEDGER" not in s7_8)
    h.test_result("panel pod and swarm paths never mention HUMAN_LEDGER_BLOCK",
                  "HUMAN_LEDGER" not in panel_md[panel_md.index("### Effort 2"):])
    pos = impl_md.find("Passing it to workers")
    inc = impl_md.rfind("Include", 0, pos) if pos >= 0 else -1
    between = impl_md[inc:pos] if inc >= 0 else ""
    window = " ".join(impl_md[max(0, pos - 400):pos + 120].split())
    h.test_result("implement-with-haiku unit-prompt Include list carries the ledger bullet",
                  pos >= 0 and inc >= 0 and "\n## " not in between and "\n### " not in between
                  and "FLOW_DIR" in window and "Ledger block" in window,
                  window[:200])

    adr_text = ADR.read_text()
    h.test_result("ADR-0023 has the per-flow human-input ledger amendment",
                  "## Amendment (2026-10-14): per-flow human-input ledger (#287)" in adr_text)

    print()
    print("[Section 16] Redo with notes (#286)")
    flow_md = COMMAND.read_text()
    plan_md = PLAN_CMD.read_text()
    review_md = REVIEW_CMD.read_text()
    adr_text = ADR.read_text()
    claude_text = CLAUDE_MD.read_text()

    # Every assertion below reads only the section it names (scoped by heading or by an anchor
    # paragraph), so a phrase elsewhere in the doc cannot satisfy it. Anchors are checked first so a
    # missing anchor fails loudly instead of slicing the wrong text.
    steps_sec = heading_section(flow_md, "## Steps")
    state_sec = heading_section(flow_md, "## Flow state")
    gate_sec = sub_section(flow_md, "### Step gate")
    redo_note_sec = sub_section(flow_md, "### Redo note")
    resume_sec = sub_section(flow_md, "### `--resume`")
    hard_sec = sub_section(flow_md, "### Hard stop")
    amendment = heading_section(adr_text, "## Amendment (2026-10-20): Redo with notes")
    exit_start = flow_md.find("**Exit Path table updates.**")
    exit_end = flow_md.find("### Stop lines")
    exit_tbl_sec = flow_md[exit_start:exit_end] if exit_start >= 0 and exit_end > exit_start else ""
    spawn_start = flow_md.find("7. **Spawn:**")
    spawn_end = flow_md.find("8. **After OK:**")
    spawn_step = flow_md[spawn_start:spawn_end] if spawn_start >= 0 and spawn_end > spawn_start else ""
    worked_start = flow_md.find("**Worked example:**")
    worked_end = flow_md.find("The preamble line is defined", worked_start)
    worked = flow_md[worked_start:worked_end] if worked_start >= 0 and worked_end > worked_start else ""

    for label, sec in (("Steps", steps_sec), ("Flow state", state_sec), ("Step gate", gate_sec),
                       ("Redo note", redo_note_sec), ("--resume", resume_sec), ("Hard stop", hard_sec),
                       ("ADR 2026-10-20 amendment", amendment), ("Exit Path table updates", exit_tbl_sec),
                       ("redo spawn step 7", spawn_step), ("worked example", worked)):
        h.test_result(f"section anchor found: {label}", bool(sec.strip()), "anchor missing")

    h.test_result("Redo-able steps line names exactly plan and review",
                  "Redo-able steps: plan, review." in steps_sec)

    gate_order = [gate_sec.find(f"   - **{opt}**") for opt in ("Continue", "Redo with notes", "Pause", "Abort")]
    h.test_result("Step gate lists Continue, Redo with notes, Pause, Abort as option bullets in order",
                  all(i >= 0 for i in gate_order) and gate_order == sorted(gate_order), f"positions={gate_order}")
    h.test_result("Step gate Redo with notes option names plan and review re-run costs",
                  'plan: "re-runs the full /expert-plan panel' in gate_sec
                  and 'review: "re-runs the full /expert-review panel' in gate_sec)
    h.test_result("Step gate: free text (\"Other\") is a Pause with a note",
                  'Free text ("Other") is a Pause with a note' in gate_sec)

    h.test_result("Redo note question offers the Cancel redo option",
                  "Options: `Cancel redo` and `Pause`." in redo_note_sec)
    h.test_result("Redo note gate entry: Cancel redo re-asks the gate",
                  "On the gate entry, **Cancel redo** re-asks the gate" in redo_note_sec)

    h.test_result("state table has a redos row",
                  "| `redos` |" in state_sec)
    h.test_result("state example shows \"redos\": {}",
                  '"redos": {}' in state_sec)
    h.test_result("--resume TSV reads redos with the absent-means-empty fallback",
                  "(.redos // {}|tostring)" in resume_sec)
    h.test_result("redo log event format is documented in the Flow state section",
                  "is logged as `<STEP> redo attempt=<k> redos=<n> note_chars=<kept_len>`" in state_sec)

    h.test_result("worked example fences the note in <<<NOTE and NOTE>>> lines",
                  "<<<NOTE\n" in worked and "\nNOTE>>>\n" in worked)
    i_ledger = spawn_step.find("`HUMAN_LEDGER` line")
    i_note = spawn_step.find("`HUMAN_NOTE` block")
    h.test_result("redo spawn places the HUMAN_NOTE block after the HUMAN_LEDGER line",
                  i_ledger >= 0 and i_note >= 0 and i_ledger < i_note, f"ledger={i_ledger}, note={i_note}")
    h.test_result("redo spawn carries no PRIOR_ANSWERS lines (initial spawn)",
                  "Initial redo spawns carry no `PRIOR_ANSWERS` lines" in spawn_step)

    h.test_result("Hard stop offers Keep previous attempt (Recommended) first",
                  "**Keep previous attempt (Recommended) — offered first.**" in hard_sec)

    h.test_result("Exit Path table has a Redo launched row",
                  re.search(r"^\| Redo launched \(at any point after spawn\)", exit_tbl_sec, re.M) is not None)
    h.test_result("Exit Path table has a Redo cancelled or paused row for the gate entry",
                  re.search(r"^\| Redo cancelled or paused at the Redo note question, gate entry", exit_tbl_sec, re.M)
                  is not None)
    h.test_result("Exit Path table has a Keep previous attempt row",
                  re.search(r"^\| Keep previous attempt \(Hard stop or resume\)", exit_tbl_sec, re.M) is not None)

    h.test_result("allowed-tools grants flow-redo.py",
                  REDO_GRANT in frontmatter(flow_md))

    h.test_result("expert-plan reads HUMAN_NOTE into context.md under the redo heading",
                  bool(paragraphs_with(plan_md, "`HUMAN_NOTE` block", "`context.md`"))
                  and "## Human direction for this redo" in plan_md)
    h.test_result("expert-review keeps HUMAN_NOTE out of PROJECT_CONTEXT (blind Pass 1, ADR-0002)",
                  bool(paragraphs_with(review_md, "`HUMAN_NOTE` block", "do NOT add it to `PROJECT_CONTEXT`", "blind Pass 1"))
                  and "## Human direction for this redo (from /expert-flow)" not in review_md)

    h.test_result("ADR amendment states the six-attempt ceiling",
                  "six attempts" in amendment)
    h.test_result("ADR amendment states the decision-point count stays six",
                  "decision-point" in amendment and "six" in amendment)

    bullet_start = claude_text.find("- `/expert-flow`")
    bullet_end = claude_text.find("\n- ", bullet_start + 1)
    flow_bullet = claude_text[bullet_start:bullet_end] if bullet_start >= 0 and bullet_end > bullet_start else ""
    h.test_result("CLAUDE.md /expert-flow bullet mentions Redo with notes",
                  "Redo with notes" in flow_bullet)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
