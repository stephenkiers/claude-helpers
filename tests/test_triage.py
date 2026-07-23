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


def read(path):
    """Return a file's text, or '' if it is missing — so a moved/renamed file turns into a
    failing assertion, never a suite-crashing exception (a crash skips every later invariant)."""
    try:
        return path.read_text()
    except OSError:
        return ""


EXPERT_REVIEW = read(COMMANDS / "expert-review.md")
TRIAGE = read(PROMPTS / "triage.md")
AMALGAMATOR = read(PROMPTS / "amalgamator.md")
FRAMEWORK = read(PROMPTS / "expert-framework.md")
NICK = read(REVIEWERS / "north-star-nick.yaml")
REVIEW_STATS = read(COMMANDS / "review-stats.md")
TEMPLATE = read(PROMPTS / "decisions.yaml.template")
ADR7 = read(ADRS / "0007-triage-and-decision-memory.md")
ADR6 = read(ADRS / "0006-reviewer-output-format-carve-outs.md")
ADR5 = read(ADRS / "0005-three-layer-context-cascade.md")
ADR4 = read(ADRS / "0004-model-cost-routing.md")
ADR_INDEX = read(ADRS / "README.md")

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

for required in ("triage.md", "amalgamator.md"):
    t(f"expert-review.md references {required}", required in EXPERT_REVIEW,
      "the new step is unreachable without it")

# ============================================================================
# INVARIANT 2: Templates were EXTRACTED, not duplicated. Two copies of a template
# is the classic refactor failure: they drift, and the stale one wins because it is
# the one the reader sees first. Guard BOTH templates — report and action-plan.
# ============================================================================
print("\n[Invariant 2] Report and action-plan templates each live in exactly one place")

t("amalgamator.md carries the report template", "# Code Review Report" in AMALGAMATOR)
t("expert-review.md no longer inlines the report template",
  "# Code Review Report" not in EXPERT_REVIEW,
  "template is duplicated — it will drift")
t("triage.md carries the action-plan template", "# Action Plan" in TRIAGE)
t("expert-review.md does not inline the action-plan template",
  "# Action Plan" not in EXPERT_REVIEW,
  "same single-source-of-truth claim the diff makes for the report, made good for the plan")
t("the dead Sign-off Checklist stub is gone from expert-review.md",
  "| Item | Severity | Recommendation | Decision |" not in EXPERT_REVIEW,
  "the never-filled-in table ADR-0007 replaces")
t("amalgamator.md does not resurrect the Sign-off Checklist",
  "Sign-off Checklist" not in AMALGAMATOR)

# ============================================================================
# INVARIANT 3: Step ordering and receipts. Triage must run AFTER the Amalgamator
# (it reads final-report.md), and rulings must come after triage. Collect (num, title)
# PAIRS first so a duplicate step number is visible — a dict would silently collapse it,
# in the very PR that renumbered every step.
# ============================================================================
print("\n[Invariant 3] Pipeline order: Amalgamator -> Triage -> Rulings (decisions.yaml/ledger removed)")

pairs = [(int(m.group(1)), m.group(2))
         for m in re.finditer(r"^### Step (\d+): (.+)$", EXPERT_REVIEW, re.M)]
nums = [n for n, _ in pairs]

t("no step number repeats", len(nums) == len(set(nums)),
  f"found step numbers {sorted(nums)} — a repeat means a bad renumber")
t("steps are numbered contiguously with no gaps",
  sorted(nums) == list(range(min(nums), max(nums) + 1)) if nums else False,
  f"found steps {sorted(nums)}")

steps = dict(pairs)

def step_of(substr):
    return next((n for n, title in steps.items() if substr.lower() in title.lower()), None)

amalg, triage_step, rulings = (
    step_of("Amalgamator"), step_of("Triage"), step_of("Rulings"))

t("an Amalgamator step exists", amalg is not None)
t("a Triage step exists", triage_step is not None)
t("a Rulings step exists", rulings is not None)
if all(x is not None for x in (amalg, triage_step, rulings)):
    t("Triage runs after the Amalgamator", triage_step > amalg,
      "triage reads final-report.md — it cannot run first")
    t("Rulings run after Triage", rulings > triage_step)

# Receipts must be IDENTICAL in the command and the prompt — a drifted field name means the
# orchestrator parses a receipt the agent never emits. Compare full lines, not an 11-char prefix.
def receipt(text, head):
    m = re.search(r"^(" + re.escape(head) + r".*)$", text, re.M)
    return m.group(1).strip() if m else None

tri_cmd, tri_prompt = receipt(EXPERT_REVIEW, "triage | doing:"), receipt(TRIAGE, "triage | doing:")
t("triage receipt is declared in both the command and the prompt",
  tri_cmd is not None and tri_prompt is not None)
t("triage receipt is identical in both", tri_cmd == tri_prompt,
  f"command: {tri_cmd!r}\n      prompt: {tri_prompt!r}")

amg_cmd, amg_prompt = receipt(EXPERT_REVIEW, "amalgamator |"), receipt(AMALGAMATOR, "amalgamator |")
t("amalgamator receipt is declared in both the command and the prompt",
  amg_cmd is not None and amg_prompt is not None)
t("amalgamator receipt is identical in both", amg_cmd == amg_prompt,
  f"command: {amg_cmd!r}\n      prompt: {amg_prompt!r}")

# ============================================================================
# INVARIANT 4: Escalation-test integrity. Over-escalation is the failure mode
# this step exists to prevent; the guard must trip on ABSOLUTE COUNT, not only a
# ratio (a ratio cries wolf on tiny reviews and sleeps through huge ones).
# ============================================================================
print("\n[Invariant 4] The escalation guard is present and count-based")

t("triage.md names over-escalation as the failure mode",
  re.search(r"over-escalat\w+ is (not the safe choice|the failure)", TRIAGE, re.I) is not None,
  "the guard that keeps 'Needs you' short")
t("triage.md resolves uncertainty toward NOT escalating",
  re.search(r"uncertain[^.\n]*?:\s*\*\*it does not", TRIAGE, re.I) is not None)
t("the over-escalation guard is absolute-count based, not ratio-only",
  "needs-you >= 5" in EXPERT_REVIEW and "needs-you >= 5" in TRIAGE,
  "a pure 20% ratio trips on tidy 3-finding reviews and misses 40-finding ones")
t("the guard defines its denominator (confirmed = doing + needs-you + deferred)",
  "confirmed = doing + needs-you + deferred" in EXPERT_REVIEW
  and "confirmed = doing + needs-you + deferred" in TRIAGE,
  "an unspecified denominator lets two agents disagree on the same run")

# Anchor the bucket checks to the bucket-DEFINITION section. Checking the whole file would pass
# even if the definitions were deleted, because the names survive downstream in the output template.
bucket_section = re.search(r"## The three finding buckets(.*?)\n## The escalation test", TRIAGE, re.S)
t("triage.md still has a bucket-definition section", bucket_section is not None)
if bucket_section:
    body = bucket_section.group(1)
    for i, bucket in enumerate(("Doing it", "Needs you", "Deferred"), start=1):
        t(f"bucket #{i} '{bucket}' is defined in the bucket section",
          re.search(rf"### {i}\. {re.escape(bucket)}", body) is not None,
          "deleting the definition must not still ship green")
    t("Already settled bucket is not present in the bucket section",
      "Already settled" not in body,
      "Already settled bucket should have been removed")
    t("Gut check is NOT numbered as a finding bucket",
      re.search(r"### \d+\. Gut check", body) is None,
      "it holds no findings — it is cross-cutting analysis")

for question in ("Shared premise", "Drift", "Panel disagreement"):
    t(f"gut check asks '{question}'", question in TRIAGE)

t("gut check does not ask 'Recurring'", "Recurring" not in TRIAGE,
  "Recurring question was removed when ledger machinery was removed")

# The load-reduction extras ADR-0007's amendment adds.
t("every footgun/scope escalation must offer 'Leave as-is'", "Leave as-is" in TRIAGE,
  "the human's real choice on a risky fix includes doing nothing")
t("triage records declined nominations (the under-escalation instrument)",
  "Declined nominations" in TRIAGE)

# ============================================================================
# INVARIANT 5: Decisions and ledger machinery was removed - no longer applicable
# ============================================================================
print("\n[Invariant 5] Decisions/ledger machinery has been removed from the pipeline")

# No assertions needed here — the machinery is gone. These checks would have verified
# decision observability and cross-run memory, but decisions.yaml is no longer part
# of the pipeline as of this PR.

# ============================================================================
# INVARIANT 6: Additive fields survive end to end. A field the panel emits and the
# Amalgamator drops is worse than no field: the human never learns the reviewer wanted them.
# ============================================================================
print("\n[Invariant 6] **Human Call** and the additive fields survive panel -> amalgamator -> triage")

t("expert-framework.md defines the Human Call field", "**Human Call**" in FRAMEWORK)
t("expert-framework.md marks it a nomination, not a verdict",
  "nomination, not a verdict" in FRAMEWORK,
  "otherwise reviewers inflate their way onto the human's plate")
# The Amalgamator is the ONLY place these can be silently dropped, so the explicit carry-forward
# INSTRUCTION must exist and name every pass-through field. Lookahead tolerates end-of-file.
carry = re.search(r"Preserve them verbatim(.*?)(?=\n## |\Z)", AMALGAMATOR, re.S)
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
# INVARIANT 7: North Star Nick emits CANONICAL severities, and the escalation routing
# lives in exactly ONE place (triage.md), not duplicated into persona config.
# ============================================================================
print("\n[Invariant 7] Nick is canonical-severity compliant; routing has one source of truth")

t("Nick no longer replaces the standard severity levels",
  "instead of standard levels" not in NICK,
  "this instruction dropped him out of the pipeline's C/H/M/L plumbing")
t("Nick emits canonical severities", "CRITICAL / HIGH / MEDIUM / LOW" in NICK)
t("Nick's category rides alongside as a tag", "**Category**:" in NICK)

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

# Routing (which categories escalate) is defined ONLY in triage.md; the `escalates:` config key
# was removed from the persona because no consumer reads it. Assert both halves.
t("Nick carries no `escalates:` config key (no consumer reads it)",
  re.search(r"^\s*escalates:", NICK, re.M) is None,
  "two sources of truth for routing that agree only by luck")
esc_section = re.search(r"## The escalation test(.*?)\n## The gut check", TRIAGE, re.S)
t("triage.md is the single source for category escalation", esc_section is not None)
if esc_section:
    esc = esc_section.group(1)
    t("escalation test routes exactly DRIFT and QUESTION to the human",
      "DRIFT" in esc and "QUESTION" in esc)
    t("SCOPE/OVERLAP/INCONSISTENCY explicitly do NOT auto-escalate",
      all(c in esc for c in ("SCOPE", "OVERLAP", "INCONSISTENCY"))
      and "do **not** auto-escalate" in esc,
      "the reciprocal half — nothing today asserts these stay OFF the human's plate")

# ============================================================================
# INVARIANT 8: Ledger and decisions machinery has been removed
# ============================================================================
print("\n[Invariant 8] Ledger and decisions machinery removed from pipeline")

# Cross-run memory via ledger.jsonl and decisions.yaml was removed in this PR.
# No assertions needed — the machinery is gone and review-stats.md is stubbed as non-functional.

# ============================================================================
# INVARIANT 9: ADR-0007 exists, is indexed, and every ADR it amends knows it.
# ============================================================================
print("\n[Invariant 9] ADR-0007 is recorded and cross-linked to what it amends")

t("ADR-0007 exists", bool(ADR7))
t("ADR-0007 is Accepted", "**Status:** Accepted" in ADR7)
t("ADR-0007 has the required sections",
  all(s in ADR7 for s in ("## Context", "## Decision", "## Consequences")))
t("ADR-0007 records the dogfooded amendment", "## Amendment" in ADR7)
t("ADR-0007 is in the index", "0007-triage-and-decision-memory.md" in ADR_INDEX)

for adr_name, adr_text in (("0005", ADR5), ("0006", ADR6), ("0004", ADR4)):
    t(f"ADR-{adr_name} records that ADR-0007 amended it", "0007" in adr_text,
      f"ADR-{adr_name} silently contradicts ADR-0007 otherwise")
t("ADR-0006 carries the additive-fields amendment", "additive field" in ADR6.lower())
t("ADR-0004 lists the Triage Chief in the panel tier", "Triage Chief" in ADR4)

h.summarize_and_exit()
