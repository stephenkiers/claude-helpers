#!/usr/bin/env python3
"""
Test suite for ADR-0007: triage step + decision memory.

The invariants here are the ones that fail *silently* if broken — a dangling prompt path or a
dropped field doesn't error, it just quietly produces a worse review. Cosmetic wording is not
tested; wiring is.

Run with: python3 tests/test_triage.py
"""

import re

from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"
PROMPTS = REPO_ROOT / "prompts"
REVIEWERS = REPO_ROOT / "reviewers"
ADRS = REPO_ROOT / "docs" / "adr"

EXPERT_REVIEW = (COMMANDS / "expert-review.md").read_text()
TRIAGE = (PROMPTS / "triage.md").read_text()
AMALGAMATOR = (PROMPTS / "amalgamator.md").read_text()
FRAMEWORK = (PROMPTS / "expert-framework.md").read_text()
NICK = (REVIEWERS / "north-star-nick.yaml").read_text()
REVIEW_STATS = (COMMANDS / "review-stats.md").read_text()

h = Harness("TRIAGE + DECISION MEMORY TEST SUITE (ADR-0007)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Every prompt path the orchestrator names actually exists.
# A dangling path means the subagent silently reviews with no mandate.
# ============================================================================
print("[Invariant 1] Prompt paths referenced by expert-review.md resolve to real files")

referenced = {
    name.rstrip(".,")  # strip sentence punctuation, keep the extension
    for name in re.findall(r"~/\.claude/prompts/([A-Za-z0-9._-]+)", EXPERT_REVIEW)
}
t("expert-review.md references at least the 5 core prompts", len(referenced) >= 5,
  f"found {sorted(referenced)}")

for name in sorted(referenced):
    t(f"prompts/{name} exists in repo", (PROMPTS / name).exists(),
      f"expert-review.md names it but {PROMPTS / name} is missing")

for required in ("triage.md", "amalgamator.md", "decisions.yaml.template"):
    t(f"expert-review.md references {required}", required in EXPERT_REVIEW,
      "the new step is unreachable without it")

# ============================================================================
# INVARIANT 2: The Amalgamator's prompt was EXTRACTED, not duplicated.
# Two copies of the report template is the classic refactor failure: they drift,
# and the stale one wins because it is the one the reader sees first.
# ============================================================================
print("\n[Invariant 2] Amalgamator template lives in exactly one place")

t("amalgamator.md carries the report template", "# Code Review Report" in AMALGAMATOR)
t("expert-review.md no longer inlines the report template",
  "# Code Review Report" not in EXPERT_REVIEW,
  "template is duplicated — it will drift")
t("the dead Sign-off Checklist stub is gone from expert-review.md",
  "| Item | Severity | Recommendation | Decision |" not in EXPERT_REVIEW,
  "the never-filled-in table ADR-0007 replaces")
t("amalgamator.md does not resurrect the Sign-off Checklist",
  "Sign-off Checklist" not in AMALGAMATOR)

# ============================================================================
# INVARIANT 3: Step ordering and receipts. Triage must run AFTER the Amalgamator
# (it reads final-report.md), and rulings must come after triage.
# ============================================================================
print("\n[Invariant 3] Pipeline order: Amalgamator -> Triage -> Rulings -> Record")

steps = {}
for m in re.finditer(r"^### Step (\d+): (.+)$", EXPERT_REVIEW, re.M):
    steps[int(m.group(1))] = m.group(2)

t("steps are numbered contiguously with no gaps or repeats",
  sorted(steps) == list(range(min(steps), max(steps) + 1)) if steps else False,
  f"found steps {sorted(steps)}")

def step_of(substr):
    return next((n for n, title in steps.items() if substr.lower() in title.lower()), None)

amalg, triage_step, rulings, record = (
    step_of("Amalgamator"), step_of("Triage"), step_of("Rulings"), step_of("Record"))

t("an Amalgamator step exists", amalg is not None)
t("a Triage step exists", triage_step is not None)
t("a Rulings step exists", rulings is not None)
t("a Record step exists", record is not None)
if all(x is not None for x in (amalg, triage_step, rulings, record)):
    t("Triage runs after the Amalgamator", triage_step > amalg,
      "triage reads final-report.md — it cannot run first")
    t("Rulings run after Triage", rulings > triage_step)
    t("Record runs after Rulings", record > rulings,
      "you cannot record a decision the user has not made yet")

t("triage receipt schema is declared in both the command and the prompt",
  "triage | doing:" in EXPERT_REVIEW and "triage | doing:" in TRIAGE,
  "orchestrator and agent must agree on the receipt it parses")

# ============================================================================
# INVARIANT 4: Escalation-test integrity. Over-escalation is the failure mode
# this step exists to prevent; if that guard is ever edited out, triage silently
# degenerates back into "here are 30 findings, good luck".
# ============================================================================
print("\n[Invariant 4] The anti-over-escalation guard is present")

t("triage.md names over-escalation as the failure mode",
  re.search(r"over-escalat\w+ is (not the safe choice|the failure)", TRIAGE, re.I) is not None,
  "the guard that keeps 'Needs you' short")
t("triage.md resolves uncertainty toward NOT escalating",
  "it does not" in TRIAGE.lower() and "uncertain" in TRIAGE.lower())
t("expert-review.md warns when needs-you exceeds the threshold",
  "20%" in EXPERT_REVIEW and "needs-you" in EXPERT_REVIEW)

for bucket in ("Doing it", "Needs you", "Gut check", "Deferred"):
    t(f"triage.md defines the '{bucket}' bucket", bucket in TRIAGE)

for question in ("Shared premise", "Drift", "Panel disagreement", "Recurring"):
    t(f"gut check asks '{question}'", question in TRIAGE)

# ============================================================================
# INVARIANT 5: The learning loop is actually closed. decisions.yaml is worthless
# if reviewers never read it — that is the whole mechanism, and it is one line
# of prompt away from being a no-op.
# ============================================================================
print("\n[Invariant 5] Reviewers read and obey .claude/decisions.yaml")

# Must appear in the NUMBERED LOAD LIST, not merely somewhere in the file. Checking for the bare
# substring would pass even if the load instruction were deleted, because the prose below it also
# names the file — and a reviewer that never READS decisions.yaml makes the whole loop a no-op.
load_list = re.search(
    r"check for and read these files in order.*?(?=\n\n[A-Z]|\nRead them in that order)",
    FRAMEWORK, re.S)
t("expert-framework.md still has a numbered project-context load list", load_list is not None)
if load_list:
    t("the load list includes .claude/decisions.yaml",
      ".claude/decisions.yaml" in load_list.group(0),
      "without this the memory is write-only and nothing ever gets quieter")
    t("decisions.yaml loads before the per-reviewer local override",
      load_list.group(0).index("decisions.yaml")
      < load_list.group(0).index("-local.yaml"),
      "cascade order: project truth, then decided truth, then persona override")
t("expert-framework.md forbids re-raising settled findings",
  re.search(r"do not raise a finding that a\s+recorded decision already answers", FRAMEWORK, re.I)
  is not None)
t("expert-framework.md still permits challenging an OUTDATED decision",
  "no longer holds" in FRAMEWORK,
  "settled != unfalsifiable; a decision whose premise died must be challengeable")
t("expert-framework.md scopes decisions by appliesTo",
  "appliesTo" in FRAMEWORK and "Silence is not permission" in FRAMEWORK)

t("decisions template requires the load-bearing 'spirit' field",
  "spirit:" in (PROMPTS / "decisions.yaml.template").read_text())
t("decisions template states the patterns-only bar",
  "nit" in (PROMPTS / "decisions.yaml.template").read_text().lower())

# ============================================================================
# INVARIANT 6: The Human Call field survives end to end. A field the panel emits
# and the Amalgamator drops is worse than no field: the human never learns the
# reviewer wanted them.
# ============================================================================
print("\n[Invariant 6] **Human Call** survives panel -> amalgamator -> triage")

t("expert-framework.md defines the Human Call field", "**Human Call**" in FRAMEWORK)
t("expert-framework.md marks it a nomination, not a verdict",
  "nomination, not a verdict" in FRAMEWORK,
  "otherwise reviewers inflate their way onto the human's plate")
# The Amalgamator is the ONLY place Human Call can be silently dropped, so it is not enough that
# the string appears somewhere in its prompt (the report template mentions it too). The explicit
# carry-forward INSTRUCTION must exist, naming all three pass-through fields.
carry = re.search(r"Preserve them verbatim(.*?)(?=\n## )", AMALGAMATOR, re.S)
t("amalgamator.md has an explicit 'preserve verbatim' instruction", carry is not None,
  "the Amalgamator is the only place these fields can be silently dropped")
if carry:
    for field in ("**Human Call**", "**Category**", "**Panel Conflict**"):
        t(f"the carry-forward instruction names {field}", field in carry.group(1),
          "a field the panel emits and the Amalgamator drops is worse than no field")

t("amalgamator.md's report template emits Human Call", "**Human Call**: …" in AMALGAMATOR)
t("triage.md consumes Human Call", "**Human Call**" in TRIAGE)

t("amalgamator.md can mark a conflict unresolved rather than faking a verdict",
  "Panel Conflict" in AMALGAMATOR)
t("triage.md escalates unresolved panel conflicts", "Panel Conflict" in TRIAGE)

# ============================================================================
# INVARIANT 7: North Star Nick emits CANONICAL severities. He is not an ADR-0006
# carve-out, so non-canonical output silently breaks his receipt, his Pass 2
# eligibility, and /review-stats parsing.
# ============================================================================
print("\n[Invariant 7] North Star Nick is canonical-severity compliant")

t("Nick no longer replaces the standard severity levels",
  "instead of standard levels" not in NICK,
  "this instruction dropped him out of the pipeline's C/H/M/L plumbing")
t("Nick emits canonical severities", "CRITICAL / HIGH / MEDIUM / LOW" in NICK)
t("Nick's category rides alongside as a tag", "**Category**:" in NICK)
# ADR-0006 mentions Nick in a dogfooding note (he is the reviewer who *enforces* ADRs). What
# matters is that he is absent from the carve-out LIST — the three personas allowed to define
# their own output shape. If a fourth is ever added, this assertion should be updated deliberately.
carve_out_list = re.search(
    r"Self-formatting carve-outs\s*—\s*(.+?)\s*—?\s*define their own shape",
    FRAMEWORK, re.S)
t("expert-framework.md still names the carve-out list", carve_out_list is not None)
if carve_out_list:
    names = carve_out_list.group(1)
    t("the carve-out list is still exactly the three known personas",
      all(n in names for n in ("Code Rot Cody", "Contrarian Carl", "Consistency Checker")))
    t("North Star Nick is NOT a format carve-out",
      "North Star" not in names,
      "he must emit canonical severities or he falls out of the pipeline")
t("DRIFT and QUESTION are marked as escalating",
  NICK.count("escalates: true") == 2,
  "exactly DRIFT and QUESTION route to the human")

# ============================================================================
# INVARIANT 8: The ledger is append-only and read by /review-stats. A rewritten
# ledger loses history silently; concurrent reviews would clobber each other.
# ============================================================================
print("\n[Invariant 8] Ledger is append-only and consumed")

t("expert-review.md appends to the ledger with >>", ">> \"$HOME/.claude/reviews/" in EXPERT_REVIEW)
t("expert-review.md never truncates the ledger with >",
  not re.search(r"[^>]> \"?\$HOME/\.claude/reviews/[^\"]*ledger\.jsonl", EXPERT_REVIEW),
  "a single > would silently erase the project's history")
t("review-stats.md reads the ledger", "ledger.jsonl" in REVIEW_STATS)
t("review-stats.md draws the missing-decision conclusion",
  "missing decision" in REVIEW_STATS.lower())

# ============================================================================
# INVARIANT 9: ADR-0007 exists, is indexed, and ADR-0005 knows it was amended.
# ============================================================================
print("\n[Invariant 9] ADR-0007 is recorded and cross-linked")

adr7 = ADRS / "0007-triage-and-decision-memory.md"
t("ADR-0007 exists", adr7.exists())
if adr7.exists():
    t("ADR-0007 is Accepted", "**Status:** Accepted" in adr7.read_text())
    t("ADR-0007 has the required sections",
      all(s in adr7.read_text() for s in ("## Context", "## Decision", "## Consequences")))
t("ADR-0007 is in the index", "0007-triage-and-decision-memory.md" in (ADRS / "README.md").read_text())
t("ADR-0005 records that it was amended",
  "0007" in (ADRS / "0005-three-layer-context-cascade.md").read_text(),
  "a fourth context layer silently contradicts ADR-0005 otherwise")

h.summarize_and_exit()
