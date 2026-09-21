#!/usr/bin/env python3
"""
Test suite for requirement 1f: reviewer contexts validation.

Validates the Phase 1 routing v2 schema and structural constraints:
- Every reviewer in reviewers/index.yaml has a valid `contexts` map
- Contexts keys are limited to review|plan|write; values limited to primary|secondary|named-only
- No reviewer carries a `gate` field inside `contexts`
- Write-tagged reviewers are exactly {editor-audience, editor-cadence, editor-signal} + Fiona's write: secondary
- Code Rot Cody and Consistency Checker have no plan or write context
- Neither planning command carries an inline `| Reviewer | File | Use When |` table
- commands/expert-write.md does not contain the editor-*.yaml glob
- The literal effort-3 pre-seat row "| uncle-bob | Yes | Pre-seated at effort 3 |" does not appear
- All effort-1 scouts and effort-2 pod lenses are review-tagged in the index
- Fail-closed wording is present in selector consumers

Run with: python3 tests/test_reviewer_contexts.py
"""

import re
import yaml
from pathlib import Path
from _test_harness import REPO_ROOT, Harness

REVIEWERS_DIR = REPO_ROOT / "reviewers"
COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"

h = Harness("REVIEWER CONTEXTS TEST SUITE")
test_result = h.test_result

# ============================================================================
# ASSERTION 1: Every reviewer has a non-empty `contexts` map
# ============================================================================
print("[Assertion 1] Every reviewer has a non-empty contexts map")

index_file = REVIEWERS_DIR / "index.yaml"
index_content = index_file.read_text()

try:
    index_data = yaml.safe_load(index_content)
    reviewers = index_data.get("reviewers", [])

    missing_contexts = []
    for reviewer in reviewers:
        name = reviewer.get("name", "UNKNOWN")
        contexts = reviewer.get("contexts")
        if contexts is None:
            missing_contexts.append(f"{name} (missing contexts key)")
        elif not isinstance(contexts, dict) or len(contexts) == 0:
            missing_contexts.append(f"{name} (contexts is empty or not a dict)")

    test_result(
        "All reviewers have non-empty contexts map",
        len(missing_contexts) == 0,
        f"Missing in {len(missing_contexts)}: {missing_contexts}" if missing_contexts else ""
    )
except Exception as e:
    test_result(
        "All reviewers have non-empty contexts map",
        False,
        f"Failed to parse index.yaml: {e}"
    )
    reviewers = []

# ============================================================================
# ASSERTION 2: Every contexts key is in {review, plan, write} and value in {primary, secondary, named-only}
# ============================================================================
print()
print("[Assertion 2] All contexts keys and values are valid")

valid_keys = {"review", "plan", "write"}
valid_values = {"primary", "secondary", "named-only"}
invalid_context_specs = []

for reviewer in reviewers:
    name = reviewer.get("name", "UNKNOWN")
    contexts = reviewer.get("contexts", {})
    if not isinstance(contexts, dict):
        continue

    for key, value in contexts.items():
        if key not in valid_keys:
            invalid_context_specs.append(f"{name}: invalid key '{key}'")
        if value not in valid_values:
            invalid_context_specs.append(f"{name}: invalid value for {key}: '{value}'")

test_result(
    "All context keys and values are valid",
    len(invalid_context_specs) == 0,
    f"Found {len(invalid_context_specs)}: {invalid_context_specs}" if invalid_context_specs else ""
)

# ============================================================================
# ASSERTION 3: No contexts entry carries a gate-style key (re-introduction guard)
# ============================================================================
print()
print("[Assertion 3] No gate-style keys inside contexts")

gate_keys = {"gate", "condition", "precondition", "gated"}
gate_violations = []

for reviewer in reviewers:
    name = reviewer.get("name", "UNKNOWN")
    contexts = reviewer.get("contexts", {})
    if not isinstance(contexts, dict):
        continue

    for key in contexts.keys():
        if key in gate_keys:
            gate_violations.append(f"{name}: found gate key '{key}' in contexts")

test_result(
    "No gate-style keys in contexts",
    len(gate_violations) == 0,
    f"Found {len(gate_violations)}: {gate_violations}" if gate_violations else ""
)

# ============================================================================
# ASSERTION 4: write-tagged reviewers are exactly editors + Fiona's write: secondary
# ============================================================================
print()
print("[Assertion 4] write-tagged reviewers are exactly {editors} + Fiona's write: secondary")

write_tagged = []
for reviewer in reviewers:
    contexts = reviewer.get("contexts", {})
    if "write" in contexts:
        name = reviewer.get("name", "UNKNOWN")
        strength = contexts["write"]
        write_tagged.append((name, strength))

expected_write_reviewers = {
    ("Demosthenes", "primary"),
    ("Shakespeare", "primary"),
    ("Strunk", "primary"),
    ("Fact-Check Fiona", "secondary"),
}

actual_write_reviewers = set(write_tagged)

write_mismatch = actual_write_reviewers != expected_write_reviewers
write_extra = actual_write_reviewers - expected_write_reviewers
write_missing = expected_write_reviewers - actual_write_reviewers

test_result(
    "Write-tagged reviewers match expected set",
    not write_mismatch,
    f"Expected {expected_write_reviewers}, got {actual_write_reviewers}" if write_mismatch else ""
)

# ============================================================================
# ASSERTION 5: Code Rot Cody and Consistency Checker have no plan or write keys
# ============================================================================
print()
print("[Assertion 5] Code Rot Cody and Consistency Checker have no plan/write")

cody_issues = []
for reviewer in reviewers:
    name = reviewer.get("name", "UNKNOWN")
    if name in ["Code Rot Cody", "Consistency Checker"]:
        contexts = reviewer.get("contexts", {})
        if "plan" in contexts:
            cody_issues.append(f"{name}: has plan key (should not)")
        if "write" in contexts:
            cody_issues.append(f"{name}: has write key (should not)")

test_result(
    "Cody and Consistency Checker have no plan/write",
    len(cody_issues) == 0,
    f"Found {len(cody_issues)}: {cody_issues}" if cody_issues else ""
)

# ============================================================================
# ASSERTION 6: expert-plan.md and expert-review-plan.md have no inline table
# ============================================================================
print()
print("[Assertion 6] Planning commands have no inline reviewer tables")

# The structural regex from the plan: "| Reviewer | File | Use When |"
# This is a table header with exactly these column names in order
table_header_pattern = r'\|\s*Reviewer\s*\|\s*File\s*\|\s*Use When\s*\|'

planning_files = [
    COMMANDS_DIR / "expert-plan.md",
    COMMANDS_DIR / "expert-review-plan.md"
]

planning_table_violations = []
for file_path in planning_files:
    if file_path.exists():
        content = file_path.read_text()
        if re.search(table_header_pattern, content):
            planning_table_violations.append(file_path.name)

test_result(
    "No inline reviewer tables in planning commands",
    len(planning_table_violations) == 0,
    f"Found in {', '.join(planning_table_violations)}" if planning_table_violations else ""
)

# ============================================================================
# ASSERTION 7: expert-write.md does not contain the editor-*.yaml glob
# ============================================================================
print()
print("[Assertion 7] expert-write.md does not use editor-* glob")

expert_write_file = COMMANDS_DIR / "expert-write.md"
editor_glob_present = False
editor_glob_violations = []

if expert_write_file.exists():
    content = expert_write_file.read_text()
    # Look for patterns like "editor-*.yaml" or "editor-*" as a glob
    if re.search(r"editor-\*", content):
        editor_glob_present = True
        editor_glob_violations.append("Found editor-* glob pattern")

test_result(
    "expert-write.md does not use editor-* glob",
    not editor_glob_present,
    "; ".join(editor_glob_violations) if editor_glob_violations else ""
)

# ============================================================================
# ASSERTION 8: The effort-3 uncle-bob pre-seat row does not appear anywhere
# ============================================================================
print()
print("[Assertion 8] No effort-3 uncle-bob pre-seat row")

# The literal row from the plan: "| uncle-bob | Yes | Pre-seated at effort 3 |"
uncle_bob_preseat_pattern = r'\|\s*uncle-bob\s*\|\s*Yes\s*\|\s*Pre-seated at effort 3\s*\|'

preseat_violations = []
for file_path in (COMMANDS_DIR / "expert-review.md", PROMPTS_DIR / "expert-review-panel.md"):
    if file_path.exists():
        content = file_path.read_text()
        if re.search(uncle_bob_preseat_pattern, content):
            preseat_violations.append(file_path.relative_to(REPO_ROOT))

test_result(
    "Uncle Bob pre-seat row removed",
    len(preseat_violations) == 0,
    f"Found in {', '.join(str(p) for p in preseat_violations)}" if preseat_violations else ""
)

# ============================================================================
# ASSERTION 9: All effort-1 scouts and effort-2 pod lenses are review-tagged
# ============================================================================
print()
print("[Assertion 9] All effort-1 scouts and effort-2 pod lenses are review-tagged")

# Effort-1 scouts (from prompts/expert-review-panel.md Step S2)
effort_1_scouts = [
    "sam-system",
    "fragile-feynman",
    "contract-chris",
    "ariadne",
    "vera-verifier",
    "curious-casey"
]

# Effort-2 pod lenses (from prompts/expert-review-panel.md P2)
# Pod 1: tara-typesafe, contract-chris, know-it-all-nigel
# Pod 2: sam-system, mozart-eda, eric-evans, rachel, fragile-feynman, vera-verifier
effort_2_pod_lenses = [
    "tara-typesafe",
    "contract-chris",
    "know-it-all-nigel",
    "sam-system",
    "mozart-eda",
    "eric-evans",
    "rachel",
    "fragile-feynman",
    "vera-verifier"
]

all_roster_reviewers = set(effort_1_scouts + effort_2_pod_lenses)

# Build a map of reviewer slug -> contexts
reviewer_by_slug = {}
for reviewer in reviewers:
    file_name = reviewer.get("file", "")
    if file_name:
        slug = file_name.replace(".yaml", "")
        contexts = reviewer.get("contexts", {})
        reviewer_by_slug[slug] = contexts

roster_review_issues = []
for slug in all_roster_reviewers:
    if slug not in reviewer_by_slug:
        roster_review_issues.append(f"{slug}: not found in index")
    else:
        contexts = reviewer_by_slug[slug]
        if "review" not in contexts:
            roster_review_issues.append(f"{slug}: missing review key")

test_result(
    "All roster reviewers are review-tagged",
    len(roster_review_issues) == 0,
    f"Found {len(roster_review_issues)}: {roster_review_issues}" if roster_review_issues else ""
)

# ============================================================================
# ASSERTION 10: Fail-closed wording is present in selector consumers
# ============================================================================
print()
print("[Assertion 10] Fail-closed wording present in selector consumers")

# The fail-closed rule from Step 1: "if the resolved set for a context is empty, stop and report..."
# We look for wording that indicates the system will stop/fail on empty resolution
fail_closed_patterns = [
    r"(?:stop|fail|error|halt)\s+and\s+report",
    r"resolve.*empty",
    r"no\s+(?:reviewers|entries|writers|editors).*found",
    r"(?:resolved|resolve)\s+empty"
]

fail_closed_files = {
    "panel-effort-5": PROMPTS_DIR / "expert-review-panel.md",  # Lines 64-72
    "planning-consumers": [
        COMMANDS_DIR / "expert-plan.md",
        COMMANDS_DIR / "expert-review-plan.md",
        PROMPTS_DIR / "plan-router.md",
        COMMANDS_DIR / "expert-plan-v3.md"
    ],
    "expert-write": COMMANDS_DIR / "expert-write.md"
}

missing_fail_closed = []

# Check panel effort-5 clause
panel_file = fail_closed_files["panel-effort-5"]
if panel_file.exists():
    content = panel_file.read_text()
    # Look for fail-closed wording around the effort-5 clause
    # We check if any fail-closed pattern appears in the effort-5 section
    effort5_section = content[content.find("EFFORT=5"):content.find("EFFORT=5") + 2000] if "EFFORT=5" in content else ""
    has_fail_closed = any(re.search(pattern, effort5_section, re.IGNORECASE) for pattern in fail_closed_patterns)
    if not has_fail_closed:
        missing_fail_closed.append("panel effort-5 clause")

# Check planning consumers
for planning_file in fail_closed_files["planning-consumers"]:
    if planning_file.exists():
        content = planning_file.read_text()
        has_fail_closed = any(re.search(pattern, content, re.IGNORECASE) for pattern in fail_closed_patterns)
        if not has_fail_closed:
            missing_fail_closed.append(f"{planning_file.name}")

# Check expert-write
write_file = fail_closed_files["expert-write"]
if write_file.exists():
    content = write_file.read_text()
    has_fail_closed = any(re.search(pattern, content, re.IGNORECASE) for pattern in fail_closed_patterns)
    if not has_fail_closed:
        missing_fail_closed.append("expert-write.md")

test_result(
    "Fail-closed wording present in selector consumers",
    len(missing_fail_closed) == 0,
    f"Missing from {len(missing_fail_closed)}: {', '.join(missing_fail_closed)}" if missing_fail_closed else ""
)

# ============================================================================
# Summary and exit
# ============================================================================

h.summarize_and_exit()
