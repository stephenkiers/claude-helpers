#!/usr/bin/env python3
"""
Test suite for ruling capture mechanism (Step 12 in expert-review.md and triage.md).

These are text invariants over two Markdown files, not behavioral tests: no
action-plan.md is produced or read here, and AskUserQuestion is never invoked.
They check that "Needs you" escalations carry a placeholder line that triage.md
emits and that expert-review.md Step 12 targets, and that the Step 13 red line's
allow-list and deny-list stay on their own sides of the line as the file changes.

On the classification of the two prior regressions (issue #18, twice): neither
was the orchestrator failing to execute a present instruction. Both occurred
against the version of commands/expert-review.md at f397320, which had no
`- **Ruling**:` placeholder and no Step 12 edit instruction at all — Step 12
ended at "ask in successive calls rather than dropping any." The mechanism was
absent, not un-executed.

That makes text-drift a live failure mode rather than a hypothetical one: the
mechanism now exists, so deleting it returns the system to exactly the state
that produced both regressions, and that is the deletion these invariants catch.
The mode this suite is structurally blind to — instruction present, orchestrator
does not execute it — has not yet occurred, and is guarded instead by Step 12's
own pre-Step-13 post-condition (re-read action-plan.md, confirm no placeholder
remains). The two guards cover different halves; neither replaces the other.

Run with: python3 tests/test_ruling_capture.py
"""

import re

from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"
PROMPTS = REPO_ROOT / "prompts"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


EXPERT_REVIEW = read(COMMANDS / "expert-review.md")
TRIAGE = read(PROMPTS / "triage.md")

h = Harness("RULING CAPTURE MECHANISM TEST SUITE")
t = h.test_result

# ============================================================================
# Shared extraction, done once, so no invariant below depends on another
# invariant's block having matched first.
# ============================================================================

step12_match = re.search(
    r"### Step 12:.*?\n(.*?)(?=\n### Step|\Z)",
    EXPERT_REVIEW,
    re.DOTALL | re.IGNORECASE,
)
step12_text = step12_match.group(1) if step12_match else ""

step13_match = re.search(
    r"### Step 13:.*?\n(.*?)(?=\n### Step|\n## |\Z)",
    EXPERT_REVIEW,
    re.DOTALL,
)
step13_text = step13_match.group(1) if step13_match else ""

# ============================================================================
# INVARIANT 1: The exact placeholder line exists in triage.md
# ============================================================================
print("[Invariant 1] Exact placeholder line present in triage.md")

# The exact placeholder line as specified in the plan
PLACEHOLDER_LINE = "- **Ruling**: _(pending your call — recorded here after you decide)_"

t("exact placeholder line is present in triage.md", PLACEHOLDER_LINE in TRIAGE,
  f"expected: {PLACEHOLDER_LINE!r}\nnot found in triage.md")

# Edge case: verify the em dash is present (not a regular hyphen)
t("placeholder uses em dash (—) not regular hyphen (-)",
  "— recorded here after you decide)_" in TRIAGE,
  "the em dash character was replaced with a regular hyphen")

# Edge case: verify underscores surround the pending state
t("pending state is wrapped in underscores (_pending...)",
  "_(pending your call" in TRIAGE,
  "underscores around the pending state are missing or malformed")

# ============================================================================
# INVARIANT 2: Placeholder position in the "Needs you" template
# ============================================================================
print("\n[Invariant 2] Placeholder is in the right position in the template")

# The placeholder should appear AFTER a "- **Recommendation**:" line
# and BEFORE a "- **Proposed decision**:" line. find() returns -1 on a miss,
# and -1 compares as "before" everything — so each position must be confirmed
# present before its ordering is trusted, or a missing marker reads as a pass.
rec_pos = TRIAGE.find("- **Recommendation**")
ruling_pos = TRIAGE.find(PLACEHOLDER_LINE)
decision_pos = TRIAGE.find("- **Proposed decision**")

positions_found = rec_pos != -1 and ruling_pos != -1 and decision_pos != -1
t("Recommendation, Ruling, and Proposed decision markers are all present",
  positions_found,
  f"one or more markers missing: Recommendation={rec_pos}, Ruling={ruling_pos}, "
  f"Proposed decision={decision_pos}")

if positions_found:
    t("triage.md contains Recommendation before Ruling",
      rec_pos < ruling_pos,
      "Recommendation line should come before the Ruling placeholder in triage.md")

    t("triage.md contains Ruling before Proposed decision",
      ruling_pos < decision_pos,
      "Ruling placeholder should come before Proposed decision in triage.md")

    # These should be within roughly 1000 characters of each other (same template item)
    span = decision_pos - rec_pos
    t("Recommendation, Ruling, and Proposed decision are close together (same item)",
      span < 1500,
      f"items are {span} chars apart; should be in same template item")

# ============================================================================
# INVARIANT 3: Step 12 in expert-review.md targets action-plan.md edits
# ============================================================================
print("\n[Invariant 3] Step 12 instructs in-place Edit of action-plan.md")

t("Step 12 exists in expert-review.md", step12_match is not None,
  "no '### Step 12:' heading found")

# Step 12 should mention editing action-plan.md
t("Step 12 mentions action-plan.md", "action-plan.md" in step12_text,
  "Step 12 does not reference action-plan.md")

# Step 12 should mention using Edit (the in-place edit operation)
t("Step 12 instructs using Edit operation", "`Edit`" in step12_text,
  "Step 12 does not mention the Edit operation")

# Step 12 should target the Ruling placeholder
t("Step 12 targets the Ruling placeholder",
  "Ruling" in step12_text and "placeholder" in step12_text.lower(),
  "Step 12 does not mention targeting the Ruling placeholder")

# Edge case: Step 12 should run when needs-you > 0
t("Step 12 runs when needs-you > 0",
  "needs-you" in step12_text,
  "Step 12 does not condition on needs-you count")

# ============================================================================
# INVARIANT 4: Shared contract - token consistency between triage and expert-review
# ============================================================================
print("\n[Invariant 4] Shared contract: placeholder token appears in both files")

# The orchestrator (expert-review.md Step 12) must target the exact same
# placeholder token that triage.md emits. This is a silent failure point:
# if they drift, the ruling never gets recorded.

# The shared contract element is the "- **Ruling**:" pattern
# Both files must reference this same pattern to target the right line
t("expert-review.md Step 12 searches for '- **Ruling**:' pattern",
  "- **Ruling**" in step12_text,
  "Step 12 must search for the same '- **Ruling**:' pattern that triage.md emits")

t("both files reference the '- **Ruling**:' token",
  "- **Ruling**:" in TRIAGE and "- **Ruling**" in step12_text,
  "the shared contract token '- **Ruling**:' is missing from one of the files")

# ============================================================================
# INVARIANT 5: Step 13 red line allows action-plan.md
# ============================================================================
print("\n[Invariant 5] Step 13 red line permits action-plan.md as Edit target")

t("Step 13 exists in expert-review.md", step13_match is not None,
  "no '### Step 13:' heading found")

has_red_line = bool(re.search(r"red.?line|forbidden|must not", step13_text, re.IGNORECASE))
t("Step 13 contains a red line constraint", has_red_line,
  "no red line section found in Step 13")

t("Step 13 red line names action-plan.md as an allowed Edit target",
  "action-plan.md" in step13_text,
  "action-plan.md is not listed as an allowed Edit target in the red line")

# ============================================================================
# INVARIANT 6/7: The red line's allow-list and deny-list stay on their own sides
# ============================================================================
print("\n[Invariant 6/7] Red line allow-list and deny-list are on opposite sides "
      "of the 'must never touch' marker")

# Invariants 6 and 7 used to check only substring *presence* across the whole
# red-line paragraph — a paragraph that holds both the allow-list and the
# deny-list. That means moving a filename from the deny side to the allow
# side left every check green. Split on the in-paragraph marker (allowing for
# Markdown bold around "never") and check each side independently.
MARKER_RE = re.compile(r"must\s*\*{0,2}never\*{0,2}\s+touch", re.IGNORECASE)
marker_match = MARKER_RE.search(step13_text)

t("Step 13 red line contains the 'must never touch' marker", marker_match is not None,
  "expected a 'must ... never ... touch' sentence separating the allow-list from the deny-list")

if marker_match:
    allow_side = step13_text[:marker_match.start()]
    deny_side = step13_text[marker_match.end():]

    # The allow-list: exactly the three permitted Edit targets.
    ALLOWED = {
        "action-plan.md": "action-plan.md",
        "$DECISIONS_FILE": "$DECISIONS_FILE",
        "docs/adr/": "an ADR under docs/adr/",
    }
    for pattern, label in ALLOWED.items():
        t(f"allow side names {label} as a permitted Edit target",
          pattern in allow_side,
          f"{label} is missing from the allow side of the red line")

    # Cardinality pin: exactly three allowed targets — not two (stale) and not
    # four (a silent widening). Matches ADR-0007's "exactly three targets."
    allowed_count = sum(1 for pattern in ALLOWED if pattern in allow_side)
    t("allow side names exactly three permitted Edit targets",
      allowed_count == 3,
      f"found {allowed_count} of the 3 expected allow-list targets on the allow side")

    # The deny-list must forbid these, and — this is the regression this
    # invariant exists to catch — must NOT appear on the allow side.
    PROHIBITED = {
        ".claude/settings.json": ".claude/settings.json",
        "CLAUDE.md": "CLAUDE.md",
        "under `agents/`": "agents/ directory",
        "`reviewers/`": "reviewers/ directory",
    }
    for pattern, label in PROHIBITED.items():
        t(f"deny side forbids edits to {label}",
          pattern in deny_side,
          f"the prohibition of {label} is missing from the deny side of the red line")
        t(f"{label} does not also appear on the allow side",
          pattern not in allow_side,
          f"{label} appears on the allow side — it would be reachable as an Edit target")

    # Edge case: source files should also be forbidden, on the deny side.
    t("deny side forbids edits to source files",
      bool(re.search(r"source\s+file", deny_side, re.IGNORECASE)),
      "prohibition of source files is missing or unclear on the deny side")

# ============================================================================
# INVARIANT 8: Edge case - Step 12 runs unconditionally when needs-you > 0
# ============================================================================
print("\n[Invariant 8] Step 12 runs unconditionally for any needs-you > 0")

# Step 12's edit must NOT be conditional on recording a decision to
# decisions.yaml. The literal phrases below are the ones the prose actually
# uses — an earlier version of this check also accepted a bare "any", which
# is satisfied by unrelated pre-existing prose (e.g. "any source file") and so
# passed regardless of whether Step 12 said anything about unconditionality.
t("Step 12 states the edit is unconditional",
  "unconditionally" in step12_text,
  "Step 12 should say the edit runs 'unconditionally' whenever needs-you > 0")

t("Step 12 states the edit is independent of decisions.yaml recording",
  "independent of" in step12_text,
  "Step 12 should say the edit is 'independent of' whether the ruling becomes a decision")

# ============================================================================
# INVARIANT 9: Sentinel tokens the Step 12 edit boundary depends on
# ============================================================================
print("\n[Invariant 9] Both optional trailing-field sentinels are present in triage.md")

# Step 12's edit boundary falls back to these two optional fields (and, failing
# both, to the Ruling line itself). Only "Proposed decision" was guarded before;
# "Rises to" was not, despite being just as load-bearing for the boundary.
for sentinel in ("- **Proposed decision**:", "- **Rises to**:"):
    t(f"triage.md template contains the sentinel token {sentinel!r}",
      sentinel in TRIAGE,
      f"{sentinel} is missing from triage.md — Step 12's boundary fallback depends on it")

# ============================================================================
# INVARIANT 10: Edge case - placeholder is not a duplicate
# ============================================================================
print("\n[Invariant 10] Placeholder line appears exactly once in triage.md")

placeholder_count = TRIAGE.count(PLACEHOLDER_LINE)
t("placeholder line appears in triage.md", placeholder_count > 0,
  "placeholder not found at all")

t("placeholder line appears exactly once (not duplicated)",
  placeholder_count == 1,
  f"found {placeholder_count} copies; only 1 expected (template templates should not be duplicated)")

h.summarize_and_exit()
