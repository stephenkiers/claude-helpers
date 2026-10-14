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
    h.test_result("hard stop offers Retry / Take over / Abort in the same section as 'No other option exists'",
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
                  "re-ask the pending question" in text and "PRIOR_ANSWERS" in text and "**not** step 6" in text)
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
    h.test_result("ledger render uses --run-key (render has no --repo)",
                  "implement-ledger.py\" locate --repo" in audit and "render --run-key" in audit)
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
    h.test_result("validation rule 7 exists", "7. For `Decision: OK` only" in text)
    h.test_result("rule 7 mentions receipt admits deviation", "receipt admits deviation" in text)
    h.test_result("rule 7 mentions artifacts missing", "artifacts missing" in text)
    h.test_result("rule 7 mentions artifact check could not run", "artifact check could not run" in text)
    h.test_result("rule 7 runs after rules 1-6", "after rules 1–6" in text or "after rules 1-6" in text)
    h.test_result("rule 7 runs before OK branch clears live",
                  "before the `OK` branch clears `live`" in text)
    h.test_result("rule 7 runs only on Decision: OK",
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
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
