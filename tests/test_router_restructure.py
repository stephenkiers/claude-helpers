#!/usr/bin/env python3
"""
Test suite for expert-review restructuring (Router & Amalgamator).
Verifies the plan to replace tagger/confirm-gate/cross-review with Router/Amalgamator.

Run with: python3 tests/test_router_restructure.py
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
COMMANDS_DIR = REPO_ROOT / "commands"
AGENTS_DIR = REPO_ROOT / "agents"

pass_count = 0
fail_count = 0
failures = []

def test_result(test_name, passed, message=""):
    global pass_count, fail_count, failures
    if passed:
        print(f"✓ {test_name}")
        pass_count += 1
    else:
        error_msg = f"✗ {test_name}"
        if message:
            error_msg += f": {message}"
        print(error_msg)
        failures.append(error_msg)
        fail_count += 1

print("=" * 70)
print("EXPERT-REVIEW ROUTER RESTRUCTURE TEST SUITE")
print("=" * 70)
print()

# ============================================================================
# INVARIANT 1: tagger.md is gone (renamed/replaced)
# ============================================================================
print("[Invariant 1] Old tagger.md file removed")

tagger_file = PROMPTS_DIR / "tagger.md"
tagger_exists = tagger_file.exists()

test_result(
    "prompts/tagger.md does NOT exist",
    not tagger_exists,
    "File still exists at prompts/tagger.md" if tagger_exists else ""
)

# ============================================================================
# INVARIANT 2: router.md exists with required structure
# ============================================================================
print()
print("[Invariant 2] New router.md exists with Panel Decision section")

router_file = PROMPTS_DIR / "router.md"
router_exists = router_file.exists()

test_result(
    "prompts/router.md exists",
    router_exists,
    "File not found at prompts/router.md" if not router_exists else ""
)

has_panel_decision = False
has_index_yaml_rule = False

if router_exists:
    router_content = router_file.read_text()

    # Check for Panel Decision section (various heading levels acceptable)
    has_panel_decision = bool(re.search(
        r"^#+\s+Panel\s+Decision\b",
        router_content,
        re.MULTILINE | re.IGNORECASE
    ))

    # Check for rule about loading only index.yaml, not persona files
    has_index_yaml_rule = bool(re.search(
        r"(index\.yaml|load.*index|only.*index)",
        router_content,
        re.IGNORECASE
    )) and bool(re.search(
        r"(not.*persona|don't.*persona|never.*persona|without.*persona|persona.*file)",
        router_content,
        re.IGNORECASE
    ))

test_result(
    "router.md has Panel Decision section",
    has_panel_decision,
    "Panel Decision heading not found" if router_exists and not has_panel_decision else ""
)

test_result(
    "router.md mentions loading only index.yaml (not persona files)",
    has_index_yaml_rule,
    "Index.yaml-only rule not found" if router_exists and not has_index_yaml_rule else ""
)

# ============================================================================
# INVARIANT 3: expert-review.md has no active confirm-gate/escalation/cross-review stages
# ============================================================================
print()
print("[Invariant 3] No live confirm-gate/escalation/cross-review mechanism in expert-review.md")

expert_review_file = COMMANDS_DIR / "expert-review.md"
expert_review_exists = expert_review_file.exists()

has_gate_stage = False
has_escalation_stage = False
has_crossreview_stage = False

if expert_review_exists:
    expert_review_content = expert_review_file.read_text()

    # Look for numbered steps or subsections that are active mechanisms (not historical mentions)
    # Pattern: ## Step N: ..., ### Step or numbered headers containing these terms

    # Check for confirm-gate or confirm step (active)
    gate_pattern = r"^#+\s+(?:Step\s+\d+|[0-9]\.)\s+.*(?:confirm|gate)\b"
    has_gate_stage = bool(re.search(gate_pattern, expert_review_content, re.MULTILINE | re.IGNORECASE))

    # Check for escalation step (active)
    escalation_pattern = r"^#+\s+(?:Step\s+\d+|[0-9]\.)\s+.*escalat"
    has_escalation_stage = bool(re.search(escalation_pattern, expert_review_content, re.MULTILINE | re.IGNORECASE))

    # Check for cross-review as an active stage (not just mentioned as "replaces cross-review")
    # Active: "## Step N: Cross-Review" or "### Cross-Review" as a heading
    # Inactive: "replaces the quadratic cross-review" in prose
    crossreview_pattern = r"^#+\s+(?:Step\s+\d+|[0-9]\.)\s+.*cross.review"
    has_crossreview_stage = bool(re.search(crossreview_pattern, expert_review_content, re.MULTILINE | re.IGNORECASE))

test_result(
    "No active confirm-gate step in expert-review.md",
    not has_gate_stage,
    "Found active confirm-gate stage" if expert_review_exists and has_gate_stage else ""
)

test_result(
    "No active escalation step in expert-review.md",
    not has_escalation_stage,
    "Found active escalation stage" if expert_review_exists and has_escalation_stage else ""
)

test_result(
    "No active cross-review stage in expert-review.md",
    not has_crossreview_stage,
    "Found active cross-review stage" if expert_review_exists and has_crossreview_stage else ""
)

# ============================================================================
# INVARIANT 4: expert-review.md mentions Amalgamator producing final-report.md
# ============================================================================
print()
print("[Invariant 4] Amalgamator step and final-report.md in expert-review.md")

has_amalgamator = False
has_final_report = False

if expert_review_exists:
    expert_review_content = expert_review_file.read_text()

    # Look for "Amalgamator" (case-insensitive is reasonable for heading searches)
    has_amalgamator = bool(re.search(r"\bamalgamator\b", expert_review_content, re.IGNORECASE))

    # Look for "final-report.md" or "final report"
    has_final_report = bool(re.search(r"final.report\.md|final\s+report", expert_review_content, re.IGNORECASE))

test_result(
    "expert-review.md mentions Amalgamator",
    has_amalgamator,
    "Amalgamator not mentioned" if expert_review_exists and not has_amalgamator else ""
)

test_result(
    "expert-review.md mentions final-report.md",
    has_final_report,
    "final-report.md not mentioned" if expert_review_exists and not has_final_report else ""
)

# ============================================================================
# INVARIANT 5: Router step uses expert-reviewer (not expert-scout) with sonnet
# ============================================================================
print()
print("[Invariant 5] Router spawns expert-reviewer (not expert-scout), with sonnet reference")

router_uses_correct_agent = False
router_avoids_scout = False
router_mentions_sonnet = False

if expert_review_exists:
    expert_review_content = expert_review_file.read_text()

    # Find the router step/section - look for "### Step 2.5" (the router step number)
    # Extract everything until the next step heading (### Step)
    router_section_pattern = r"### Step 2\.5.*?\n(.*?)(?=\n### Step|\Z)"
    router_section_match = re.search(router_section_pattern, expert_review_content, re.DOTALL | re.IGNORECASE)

    if router_section_match:
        router_section = router_section_match.group(1)

        # Check that expert-reviewer is mentioned in the router section
        router_uses_correct_agent = bool(re.search(r"expert.reviewer|expert-reviewer", router_section, re.IGNORECASE))

        # Check that expert-scout is NOT mentioned as the agent type for the router
        router_avoids_scout = not bool(re.search(r"expert.scout|expert-scout", router_section, re.IGNORECASE))

        # Check for sonnet reference (could be "sonnet", "model.*sonnet", "Sonnet", etc.)
        router_mentions_sonnet = bool(re.search(r"\bsonnet\b", router_section, re.IGNORECASE))

test_result(
    "Router step mentions expert-reviewer",
    router_uses_correct_agent,
    "expert-reviewer not found in router section" if expert_review_exists and not router_uses_correct_agent else ""
)

test_result(
    "Router step does NOT use expert-scout",
    router_avoids_scout,
    "expert-scout incorrectly used for router" if expert_review_exists and not router_avoids_scout else ""
)

test_result(
    "Router step mentions sonnet",
    router_mentions_sonnet,
    "sonnet reference not found in router section" if expert_review_exists and not router_mentions_sonnet else ""
)

# ============================================================================
# INVARIANT 6: expert-scout.md job list excludes tagger and confirm-gate/escalation
# ============================================================================
print()
print("[Invariant 6] expert-scout.md does not list tagger or confirm-gate/escalation")

expert_scout_file = AGENTS_DIR / "expert-scout.md"
expert_scout_exists = expert_scout_file.exists()

scout_mentions_tagger = False
scout_mentions_gate = False

if expert_scout_exists:
    expert_scout_content = expert_scout_file.read_text()

    # Look for "tagger" in the job list or description
    scout_mentions_tagger = bool(re.search(r"\btagger\b", expert_scout_content, re.IGNORECASE))

    # Look for "confirm-gate", "confirm gate", "escalation", "escalation-gate" in jobs
    scout_mentions_gate = bool(re.search(
        r"(?:confirm.gate|escalation.gate|escalation|confirm.*gate)",
        expert_scout_content,
        re.IGNORECASE
    ))

test_result(
    "expert-scout.md exists",
    expert_scout_exists,
    "File not found at agents/expert-scout.md" if not expert_scout_exists else ""
)

if expert_scout_exists:
    test_result(
        "expert-scout.md does NOT mention tagger",
        not scout_mentions_tagger,
        "Tagger mentioned in expert-scout" if scout_mentions_tagger else ""
    )

    test_result(
        "expert-scout.md does NOT mention confirm-gate or escalation",
        not scout_mentions_gate,
        "Confirm-gate/escalation mentioned in expert-scout" if scout_mentions_gate else ""
    )

# ============================================================================
# INVARIANT 7: expert-reviewer.md mentions Router among its jobs
# ============================================================================
print()
print("[Invariant 7] expert-reviewer.md mentions Router in job description")

expert_reviewer_file = AGENTS_DIR / "expert-reviewer.md"
expert_reviewer_exists = expert_reviewer_file.exists()

reviewer_mentions_router = False

if expert_reviewer_exists:
    expert_reviewer_content = expert_reviewer_file.read_text()
    reviewer_mentions_router = bool(re.search(r"\brouter\b", expert_reviewer_content, re.IGNORECASE))

test_result(
    "expert-reviewer.md exists",
    expert_reviewer_exists,
    "File not found at agents/expert-reviewer.md" if not expert_reviewer_exists else ""
)

if expert_reviewer_exists:
    test_result(
        "expert-reviewer.md mentions Router",
        reviewer_mentions_router,
        "Router not mentioned in expert-reviewer" if not reviewer_mentions_router else ""
    )

# ============================================================================
# INVARIANT 8: CLAUDE.md no longer describes tagger routing + haiku confirm-gate
# ============================================================================
print()
print("[Invariant 8] CLAUDE.md no longer describes tagger routing + Haiku confirm-gate")

claude_md_file = REPO_ROOT / "CLAUDE.md"
claude_md_exists = claude_md_file.exists()

mentions_tagger_routing_actively = False
mentions_haiku_gate_actively = False

if claude_md_exists:
    claude_md_content = claude_md_file.read_text()

    # Find the /expert-review bullet description
    expert_review_section_pattern = r"- `/expert-review`.*?\n(.*?)(?=\n- `/[a-z]|\nOptional|\Z)"
    expert_review_section_match = re.search(expert_review_section_pattern, claude_md_content, re.DOTALL | re.IGNORECASE)

    if expert_review_section_match:
        er_section = expert_review_section_match.group(1)

        # Check for "tagger routing" mentioned as an active mechanism
        mentions_tagger_routing_actively = bool(re.search(
            r"tagger\s+routing",
            er_section,
            re.IGNORECASE
        ))

        # Check for "confirm-gate" or "confirm gate" mentioned as active mechanism of the old design
        # But NOT in context of "pass" or review scoring - be specific about "gate" mechanism
        mentions_haiku_gate_actively = bool(re.search(
            r"confirm.gate|confirm\s+gate",
            er_section,
            re.IGNORECASE
        ))

test_result(
    "CLAUDE.md /expert-review does NOT mention tagger routing",
    not mentions_tagger_routing_actively,
    "Tagger routing still mentioned in CLAUDE.md /expert-review" if claude_md_exists and mentions_tagger_routing_actively else ""
)

test_result(
    "CLAUDE.md /expert-review does NOT mention Haiku confirm-gate",
    not mentions_haiku_gate_actively,
    "Haiku confirm-gate still mentioned in CLAUDE.md /expert-review" if claude_md_exists and mentions_haiku_gate_actively else ""
)

# ============================================================================
# INVARIANT 9: review-stats.md does not have active tagger-collapse parsing
# ============================================================================
print()
print("[Invariant 9] review-stats.md does not parse tagger-collapse as active metric")

review_stats_file = COMMANDS_DIR / "review-stats.md"
review_stats_exists = review_stats_file.exists()

has_active_tagger_collapse_parsing = False

if review_stats_exists:
    review_stats_content = review_stats_file.read_text()

    # Look for an active parsing rule that specifically counts "tagger collapse"
    # Pattern: something like "parses ... tagger collapse" or "checks for tagger collapse"
    # Historical mentions like "the old tagger collapse guard" are OK

    # Be conservative: look for a line that suggests active computation/parsing of tagger collapse
    has_active_tagger_collapse_parsing = bool(re.search(
        r"(?:parse|count|metric|check|compute|check).*tagger\s+collapse|tagger\s+collapse.*(?:parse|count|metric|check|compute)",
        review_stats_content,
        re.IGNORECASE
    ))

test_result(
    "review-stats.md exists",
    review_stats_exists,
    "File not found at commands/review-stats.md" if not review_stats_exists else ""
)

if review_stats_exists:
    test_result(
        "review-stats.md does NOT actively parse tagger-collapse metric",
        not has_active_tagger_collapse_parsing,
        "Active tagger-collapse parsing found in review-stats" if has_active_tagger_collapse_parsing else ""
    )

# ============================================================================
# Summary
# ============================================================================
print()
print("=" * 70)
print("TEST SUMMARY")
print("=" * 70)
print(f"Passed: {pass_count}")
print(f"Failed: {fail_count}")
print()

if failures:
    print("FAILURES:")
    for failure in failures:
        print(f"  {failure}")
    print()

if fail_count == 0:
    print("✓ All tests PASSED")
    exit(0)
else:
    print("✗ Some tests FAILED")
    exit(1)
