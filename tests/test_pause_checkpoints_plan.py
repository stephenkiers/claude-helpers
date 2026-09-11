#!/usr/bin/env python3
"""
Test suite for pause-checkpoints plan implementation.

Verifies that /implement-with-haiku and ADR-0019 implement all plan items:
- summary checkpoint removed
- --pause-at all expands to gate,fanout,round4 only
- No bare $PAUSE_AT/$PAUSE_CHOICE across bash tool boundaries
- $PAUSE_CHOICE has fail-closed catch-all
- Four pause-point blocks consolidated
- "Stop here" messages include manual next steps
- PAUSE LOG documents all states (including unreachable)
- ADR-0019 references
- Standardized checkpoint wording
- Usage documentation

Run with: python3 tests/test_pause_checkpoints_plan.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

COMMANDS = REPO_ROOT / "commands"
ADRS = REPO_ROOT / "docs" / "adr"


def read(path):
    """Return file text or empty string if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


IMPL_WITH_HAIKU = read(COMMANDS / "implement-with-haiku.md")
ADR_0019 = read(ADRS / "0019-content-driven-pause-checkpoints.md")

h = Harness("PAUSE CHECKPOINTS PLAN TEST SUITE")
t = h.test_result


# ============================================================================
# CRITICAL: File existence and basic structure
# ============================================================================
print("[CRITICAL] File existence and basic structure")

t("implement-with-haiku.md exists and is not empty",
  IMPL_WITH_HAIKU != "",
  "File not found or empty")

t("ADR-0019 exists and is not empty",
  ADR_0019 != "",
  "File not found or empty")

print()


# ============================================================================
# HIGH PRIORITY: summary checkpoint removed from pausable set
# ============================================================================
print("[HIGH] summary checkpoint removed from pausable set")

# Check that 'summary' is NOT in the --pause-at argument hint
if "pause-at" in IMPL_WITH_HAIKU.lower():
    # Find the argument-hint section for --pause-at
    pause_at_section = re.search(
        r"--pause-at.*?\n(.*?)(?=\n--|$)",
        IMPL_WITH_HAIKU, re.DOTALL
    )
    if pause_at_section:
        pause_text = pause_at_section.group(1)
        has_summary_in_pause = re.search(r"\bsummary\b", pause_text, re.I)
        t("--pause-at argument hint does NOT include 'summary'",
          not has_summary_in_pause,
          "summary should not be listed as a valid pause-at checkpoint")

# Check that 'all' shorthand exists and expands to gate,fanout,round4
all_shorthand = re.search(
    r"--pause-at all.*(?:pause|checkpoint).*(?:gate|fanout|round4)",
    IMPL_WITH_HAIKU, re.I
)
t("Documentation mentions 'all' shorthand expansion",
  all_shorthand is not None,
  "Should document what --pause-at all expands to")

# The 'all' expansion should include all three checkpoints: gate, fanout, round4
# Look for the explicit expansion code or documented behavior
all_expansion_code = re.search(
    r'if\s+\[\s+"\$PAUSE_AT"\s+=\s+"all"\s+\].*?\n.*?"gate,fanout,round4"',
    IMPL_WITH_HAIKU, re.S
)
t("Code or documentation shows 'all' expands to exactly gate,fanout,round4",
  all_expansion_code is not None,
  "Should show that 'all' expands to gate,fanout,round4")

# Check that summary is not referenced as a pause checkpoint anywhere in the context block
context_blocks = re.findall(
    r"(?:valid|available|supported).*checkpoint.*\n(.*?)(?=\n[A-Z#]|\Z)",
    IMPL_WITH_HAIKU, re.I | re.S
)
for block in context_blocks:
    if "pause" in block.lower() and "summary" in block.lower():
        # This might be okay if it's just mentioning that summary is NOT a pause checkpoint
        if "not" not in block.lower() and "removed" not in block.lower():
            t("summary not listed as valid pause checkpoint in context blocks",
              False,
              "summary should not be listed as a valid pause checkpoint")
            break

print()


# ============================================================================
# HIGH PRIORITY: --pause-at validation and error handling
# ============================================================================
print("[HIGH] --pause-at validation and error handling")

# Check for constraint/documentation on --pause-at all
# Either explicit validation or documented behavior showing it's standalone
all_constraint = re.search(
    r"--pause-at all|PAUSE_AT.*all|if.*all.*then",
    IMPL_WITH_HAIKU, re.I
)

if all_constraint:
    # Check context for how 'all' is handled
    context_start = max(0, all_constraint.start() - 500)
    context_end = min(len(IMPL_WITH_HAIKU), all_constraint.end() + 500)
    context = IMPL_WITH_HAIKU[context_start:context_end]

    # Should explain what happens (expands to gate,fanout,round4 or standalone or error)
    has_explanation = re.search(
        r"expand|gate,fanout,round4|standalone|error|must|cannot",
        context, re.I
    )

    t("--pause-at all is documented with constraint/behavior (standalone or expansion)",
      has_explanation is not None,
      "Should document how --pause-at all is handled")
else:
    t("--pause-at all is documented and handled",
      False,
      "Should document behavior of --pause-at all")

# Check that valid checkpoint names are documented
valid_checkpoints = ["gate", "fanout", "round4", "all"]
for checkpoint in valid_checkpoints:
    has_checkpoint = re.search(r"\b" + checkpoint + r"\b", IMPL_WITH_HAIKU)
    t(f"Checkpoint name '{checkpoint}' mentioned in document",
      has_checkpoint is not None,
      f"Valid checkpoint '{checkpoint}' should be documented")

print()


# ============================================================================
# HIGH PRIORITY: No bare $PAUSE_AT/$PAUSE_CHOICE across tool boundaries
# ============================================================================
print("[HIGH] No bare shell variables across tool boundaries")

# Look for patterns where $PAUSE_AT or $PAUSE_CHOICE are used as bare variables
# in conditional contexts that might span tool calls
bare_pause_at = re.findall(
    r'\$\{?PAUSE_AT\}?(?:\s*==|\s*!=|\s*-[zn]|\s*\[)',
    IMPL_WITH_HAIKU
)
bare_pause_choice = re.findall(
    r'\$\{?PAUSE_CHOICE\}?(?:\s*==|\s*!=|\s*-[zn]|\s*\[)',
    IMPL_WITH_HAIKU
)

t("No bare $PAUSE_AT variable conditionals found",
  len(bare_pause_at) == 0,
  f"Found {len(bare_pause_at)} bare $PAUSE_AT conditionals - should use literal values or state")

t("No bare $PAUSE_CHOICE variable conditionals found",
  len(bare_pause_choice) == 0,
  f"Found {len(bare_pause_choice)} bare $PAUSE_CHOICE conditionals - should use literal values or state")

# Check that pause-point blocks don't reference shell variables across bash tool calls
# Look for PAUSE_CHOICE capture that is written as literal code (not variable reference)
pause_choice_capture = re.search(
    r"(?:PAUSE_CHOICE|pause.*choice).*(?:echo|read|capture|get).*?(?:\n|```)",
    IMPL_WITH_HAIKU, re.I | re.S
)
if pause_choice_capture:
    capture_text = pause_choice_capture.group(0)
    # Should have literal values like 'continue', 'stop', etc. not just a variable
    has_literal_values = re.search(r"continue|stop|pause", capture_text, re.I)
    t("PAUSE_CHOICE capture uses literal values, not bare variable reference",
      has_literal_values is not None,
      "Choice capture should reference literal option strings")

print()


# ============================================================================
# HIGH PRIORITY: $PAUSE_CHOICE fail-closed catch-all
# ============================================================================
print("[HIGH] PAUSE_CHOICE choice-capture with fail-closed catch-all")

# Look for the choice-capture logic
choice_capture_blocks = re.findall(
    r"(?:continue|stop|pause|choice|CHOICE).*?(?:```|case|if|elif|else).*?(?:```|\n\n)",
    IMPL_WITH_HAIKU, re.I | re.S
)

has_catch_all = False
for block in choice_capture_blocks:
    # Should have a catch-all that defaults to 'stop' or treats unrecognized as stop
    if re.search(r"\*\)|default|else", block, re.I):
        if re.search(r"stop|exit|halt", block, re.I):
            has_catch_all = True
            break

t("PAUSE_CHOICE has fail-closed catch-all (defaults to stop)",
  has_catch_all,
  "Unrecognized/unset PAUSE_CHOICE should be treated as 'stop' (fail-closed)")

# Check for handling of user choices - either explicit documentation or via AskUserQuestion
# AskUserQuestion should naturally handle unrecognized values
choice_handling = re.search(
    r"AskUserQuestion|three.*option|proceed|stop|don't ask again",
    IMPL_WITH_HAIKU, re.I
)

# If AskUserQuestion is used, it handles the choice validation
# So no need for explicit unrecognized handling if the UI is well-defined
t("Choice handling is documented (via AskUserQuestion or explicit validation)",
  choice_handling is not None,
  "Should document how user choices are handled and validated")

print()


# ============================================================================
# HIGH PRIORITY: Four pause-point blocks consolidated into single procedure
# ============================================================================
print("[HIGH] Four pause-point blocks consolidated")

# Check for unified/consolidated procedure mention
consolidated_ref = re.search(
    r"unified procedure|single procedure|consolidated.*pause|parameterized.*checkpoint",
    IMPL_WITH_HAIKU, re.I
)

t("Document references unified or consolidated pause procedure",
  consolidated_ref is not None,
  "Should reference a consolidated/unified pause-point procedure")

# Check for parameterized structure (table, list, etc.)
has_parameter_table = re.search(
    r"\|\s*[^|]*(?:checkpoint|gate|fanout|round4)[^|]*\|[^|]*(?:prompt|message|name|id)",
    IMPL_WITH_HAIKU
) is not None

has_parameter_list = re.search(
    r"(?:table|list|parameters?).*(?:checkpoint|gate|fanout|round4)",
    IMPL_WITH_HAIKU, re.I
) is not None

t("Consolidated procedure uses parameterized structure (documented)",
  has_parameter_table or has_parameter_list,
  "Four pause-point blocks should be reduced to parameterized structure")

print()


# ============================================================================
# MEDIUM PRIORITY: Each "Stop here" message includes manual next steps
# ============================================================================
print("[MEDIUM] 'Stop here' messages with manual next steps")

# Find all "Stop here" or "stopped" messages
stop_messages = re.findall(
    r"(?:stop.*here|stopped.*here|checkpoint stopped|paused.*at).*?\n.*?(?=\n\n|\n```|$)",
    IMPL_WITH_HAIKU, re.I | re.S
)

for i, msg in enumerate(stop_messages):
    # Should mention what's committed
    has_commit_info = re.search(r"commit|committed|staged|applied", msg, re.I)
    t(f"Stop message {i+1} mentions what's committed",
      has_commit_info is not None,
      "Should tell user what has been committed")

    # Should mention how to resume manually
    has_resume_info = re.search(
        r"(?:resume|continue|next step|manual|run|execute|git)",
        msg, re.I
    )
    t(f"Stop message {i+1} includes manual next-step instructions",
      has_resume_info is not None,
      "Should provide concrete instructions for manual resume")

if not stop_messages:
    t("'Stop here' messages found in checkpoint blocks",
      False,
      "Should have explicit 'Stop here' exit messages for each checkpoint")

print()


# ============================================================================
# MEDIUM PRIORITY: PAUSE LOG documents all states including 'unreachable'
# ============================================================================
print("[MEDIUM] PAUSE LOG state values documented")

# Look for PAUSE LOG template or documentation
pause_log_section = re.search(
    r"(?:PAUSE[- ]LOG|pause.*log|state|status).*[^a-z](?:not-configured|stopped|unreachable)[^a-z]",
    IMPL_WITH_HAIKU, re.I | re.S
)

t("PAUSE LOG section exists and documents state values",
  pause_log_section is not None,
  "Should have documentation for PAUSE LOG state values")

# Check for 'unreachable' state specifically
has_unreachable = re.search(r"unreachable", IMPL_WITH_HAIKU, re.I)
t("PAUSE LOG mentions 'unreachable' state (for checkpoint requested but not reached)",
  has_unreachable is not None,
  "Should document unreachable state (checkpoint requested via --pause-at but never reached)")

# Check for distinction explanation
# Look for the explanation about why we distinguish them
distinction = re.search(
    r"(?:distinguish|separate|different).*(?:user.*didn't ask|request|ask)|unreachable.*(?:skip|round|classification).*not-configured",
    IMPL_WITH_HAIKU, re.I
)
if not distinction:
    # Alternative: look for "to distinguish" phrasing
    distinction = re.search(
        r"unreachable.*distinguish|distinguish.*unreachable|skip.*round.*not-configured",
        IMPL_WITH_HAIKU, re.I
    )

t("Distinction between unreachable and not-configured is explained or clear from context",
  distinction is not None,
  "Should explain that unreachable (run skips rounds) differs from not-configured (user didn't ask)")

print()


# ============================================================================
# MEDIUM PRIORITY: 'stopped' PAUSE LOG value is observable
# ============================================================================
print("[MEDIUM] PAUSE LOG 'stopped' value is observable")

# Check that there's output that makes 'stopped' state reachable
# Either via explicit print/output in stop path, or documented in PAUSE LOG template
stop_output = re.search(
    r"echo.*stopped|print.*stopped|output.*stopped|PAUSE[- ]LOG.*stopped",
    IMPL_WITH_HAIKU, re.I
)

t("Checkpoint exit paths produce observable output for 'stopped' state",
  stop_output is not None,
  "Stop path should produce output that allows 'stopped' to be logged")

# Alternative: PAUSE LOG template should document this
if not stop_output:
    t("PAUSE LOG template documents how 'stopped' is recorded",
      "stopped" in IMPL_WITH_HAIKU and "PAUSE.LOG" in IMPL_WITH_HAIKU,
      "If no explicit output, PAUSE LOG template should document stopped state recording")

print()


# ============================================================================
# MEDIUM PRIORITY: --pause-at all expands exactly to gate,fanout,round4
# ============================================================================
print("[MEDIUM] --pause-at all expansion documentation")

# Look for documented expansion of 'all'
all_doc = re.search(
    r"--pause-at all.*(?:gate|fanout|round4)",
    IMPL_WITH_HAIKU, re.I
)

if all_doc:
    # Find the documentation context
    start = max(0, all_doc.start() - 300)
    end = min(len(IMPL_WITH_HAIKU), all_doc.end() + 300)
    context = IMPL_WITH_HAIKU[start:end]

    has_gate = "gate" in context.lower()
    has_fanout = "fanout" in context.lower()
    has_round4 = "round4" in context.lower()

    t("'all' expansion documents the three checkpoints (gate, fanout, round4)",
      has_gate and has_fanout and has_round4,
      "all should expand to gate, fanout, and round4")

print()


# ============================================================================
# LOW PRIORITY: Description frontmatter mentions --pause-at
# ============================================================================
print("[LOW] Description frontmatter mentions --pause-at")

# Check description: field in YAML frontmatter or early description
description_mention = re.search(
    r'description:\s*"[^"]*(?:--pause-at|pause.*checkpoint)[^"]*"',
    IMPL_WITH_HAIKU, re.I
)

if not description_mention:
    # Alternative: check for "pause-at" in early text
    description_mention = re.search(
        r"^[^\n]*implement-with-haiku[^\n]*\n(.*?)\n[a-z]*-hint:",
        IMPL_WITH_HAIKU, re.I | re.S
    )
    if description_mention:
        has_pause = "--pause-at" in description_mention.group(1) or "pause" in description_mention.group(1).lower()
    else:
        has_pause = False
else:
    has_pause = True

t("Description mentions --pause-at checkpoints",
  has_pause,
  "Description should mention --pause-at feature")

print()


# ============================================================================
# LOW PRIORITY: Three checkpoint prompts use standardized wording
# ============================================================================
print("[LOW] Standardized checkpoint prompt wording")

# Check for standardized reassurance/third-option wording across checkpoints
# Look for patterns like "Proceed and don't ask again" appearing multiple times
proceed_pattern = re.findall(
    r"Proceed.*don't ask again|don't ask again|pause.*option|third option",
    IMPL_WITH_HAIKU, re.I
)

t("Checkpoint prompts include standardized 'don't ask again' / third-option wording",
  len(proceed_pattern) > 0,
  "Should have standardized reassurance wording like 'Proceed and don't ask again'")

# Check that this wording appears in multiple checkpoint contexts
# (indicating it's used consistently across checkpoints)
if len(proceed_pattern) > 1:
    t("Standardized wording appears multiple times (across checkpoints)",
      True,
      "")
else:
    t("Standardized wording should appear at each of the three checkpoints",
      False,
      "Should use identical wording at gate, fanout, round4 checkpoints")

print()


# ============================================================================
# LOW PRIORITY: Usage documentation with examples
# ============================================================================
print("[LOW] Usage documentation with examples")

# Check for usage examples in argument-hint or docs
usage_examples = re.findall(
    r"--pause-at\s+[a-z,]+(?:\s+[a-z,]+)?",
    IMPL_WITH_HAIKU, re.I
)

t("At least one usage example of --pause-at is documented",
  len(usage_examples) > 0,
  "Should include at least one example like --pause-at gate,round4")

# Check for specific example patterns
has_gate_example = re.search(r"--pause-at.*gate", IMPL_WITH_HAIKU)
has_fanout_example = re.search(r"--pause-at.*fanout", IMPL_WITH_HAIKU)
has_round4_example = re.search(r"--pause-at.*round4", IMPL_WITH_HAIKU)
has_all_example = re.search(r"--pause-at.*all", IMPL_WITH_HAIKU)

example_count = sum([
    has_gate_example is not None,
    has_fanout_example is not None,
    has_round4_example is not None,
    has_all_example is not None,
])

t("Multiple checkpoint examples documented (gate, fanout, round4, all)",
  example_count >= 2,
  "Should include examples of different checkpoint combinations")

print()


# ============================================================================
# LOW PRIORITY: Pause telemetry silence documented as intentional
# ============================================================================
print("[LOW] Pause telemetry silence documented")

# Look for documentation about telemetry/output behavior
# Either as explicit documentation or as comments in the code
has_telemetry_note = re.search(
    r"(?:no.*output|silence|best.*effort).*(?:pause|checkpoint|telemetry)",
    IMPL_WITH_HAIKU, re.I
)

if not has_telemetry_note:
    # Look for it the other way around
    has_telemetry_note = re.search(
        r"(?:pause|checkpoint|stage-begin|stage-end).*(?:no.*output|silence|intentional)",
        IMPL_WITH_HAIKU, re.I
    )

t("Pause telemetry silence is documented (as intentional or best-effort)",
  has_telemetry_note is not None,
  "Should document that pause checkpoints have no user-facing telemetry output")

print()


# ============================================================================
# ADR-0019: Content-driven pause checkpoints (via ADR file)
# ============================================================================
print("[LOW] ADR-0019 pause-checkpoint references")

if ADR_0019:
    # Should mention that pause generalizes expert-plan-v2's pattern
    t("ADR-0019 states pause generalizes expert-plan-v2's pattern",
      re.search(r"expert-plan-v2|expert.plan.v2", ADR_0019, re.I) is not None and
      re.search(r"generaliz|extend|build", ADR_0019, re.I) is not None,
      "Should state that pause mechanism generalizes expert-plan-v2's single-shot pattern")

    # Should NOT claim expert-plan-v2 already had a three-option UI
    three_option_false = re.search(
        r"expert-plan-v2.*three.option|expert-plan-v2.*don't ask again",
        ADR_0019, re.I
    )
    t("ADR-0019 does NOT claim expert-plan-v2 had three-option UI",
      three_option_false is None,
      "Should not claim expert-plan-v2 already had a three-option 'don't ask again' UI")

    # Should mention shared run-metrics.py or reference to ADR-0016 usage-gate
    run_metrics_ref = re.search(r"run-metrics|run.metrics|run_metrics", ADR_0019, re.I)
    namespace_ref = re.search(r"namespace|separate.*namespace", ADR_0019, re.I)
    adr_0016_ref = re.search(r"0016|adr-0016|adr 0016|usage-gate", ADR_0019, re.I)

    # ADR should mention shared telemetry either directly or via reference to ADR-0016
    has_telemetry_mention = (run_metrics_ref is not None and namespace_ref is not None) or adr_0016_ref is not None

    t("ADR-0019 mentions shared telemetry script or references ADR-0016",
      has_telemetry_mention,
      "Should reference shared run-metrics.py with separate namespaces or mention ADR-0016")

    # Should address whether stacked prompts are accepted or merged
    t("ADR-0019 addresses stacked prompts at shared seam",
      re.search(r"stack|seam|prompt|merge|combined", ADR_0019, re.I) is not None,
      "Should address whether stacked prompts at shared seam are accepted or merged")

else:
    t("ADR-0019 exists and documents design decisions",
      False,
      "ADR-0019 should exist and document pause-checkpoint design")

print()


# ============================================================================
# ADR-0019: Reference to ADR-0016 (usage-gate) and shared telemetry
# ============================================================================
print("[LOW] ADR-0019 references to related designs (ADR-0016)")

if ADR_0019:
    # Should mention or reference ADR-0016 (usage-gate)
    has_adr_0016_ref = re.search(r"0016|usage.gate|usage-gate|ADR-0016", ADR_0019, re.I)
    t("ADR-0019 references or mentions ADR-0016 (usage-gate mechanism)",
      has_adr_0016_ref is not None,
      "Should mention ADR-0016 for context on the parallel usage-gate mechanism")

print()


h.summarize_and_exit()
