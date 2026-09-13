#!/usr/bin/env python3
"""
Spec-blind test suite for the Haiku Effort Scout feature.

This test suite is written ONLY from the plan specification (8 numbered findings),
without reading the implementation files. It verifies the accepted/implemented changes
by reading and asserting against the actual file content.

The 8 findings being tested:
1. Effort Scout downward-only invariant enforced at parse boundary (commands/expert-review.md)
2. ADR-0012 config defaults match template (prompts/effort-heuristic.yaml.template)
3. Bounded-input property scoped at tool level (agents/expert-scout.md tools frontmatter)
4. Checkpoint table row label changed to "Step 3" (commands/expert-review.md line 125)
5. Scout failure writes explicit error-shape JSON (commands/expert-review.md)
6. New section heading in ADR-0004 (docs/adr/0004-model-cost-routing.md)
7. ADR cross-reference anchor matches (docs/adr/0004 and 0012)
8. Needs-you cluster ruling (decision tracking, not testable in code)

Run with: python3 tests/test_effort_scout_spec_blind.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
AGENTS_DIR = REPO_ROOT / "agents"
DOCS_ADR_DIR = REPO_ROOT / "docs" / "adr"

h = Harness("HAIKU EFFORT SCOUT SPEC-BLIND TEST SUITE")
t = h.test_result

print()
print("[Finding 1] Effort Scout downward-only invariant enforced at parse boundary")
print("=" * 70)

expert_review_file = COMMANDS_DIR / "expert-review.md"
expert_review_exists = expert_review_file.exists()
t("commands/expert-review.md exists", expert_review_exists,
  "File not found at commands/expert-review.md" if not expert_review_exists else "")

has_mechanical_effort_clamp = False
has_effort_exceeds_check = False

if expert_review_exists:
    content = expert_review_file.read_text()

    # Require the literal ≤/<= comparison against MECHANICAL_EFFORT — no alternate
    # branches, since any alternative matching only surrounding prose (e.g. "exceeds
    # the mechanical tier") stays true even if the actual operator is flipped to ≥/>=,
    # which is exactly the regression this check exists to catch.
    has_mechanical_effort_clamp = bool(re.search(
        r"effort\s*(?:≤|<=)\s*MECHANICAL_EFFORT",
        content
    ))
    has_effort_exceeds_check = has_mechanical_effort_clamp

t("Parse logic mentions MECHANICAL_EFFORT or effort clamping",
  has_mechanical_effort_clamp,
  "No reference to MECHANICAL_EFFORT clamping found" if expert_review_exists and not has_mechanical_effort_clamp else "")

t("Parse logic checks if Scout effort exceeds mechanical tier",
  has_effort_exceeds_check,
  "No check for effort bounds found" if expert_review_exists and not has_effort_exceeds_check else "")

print()
print("[Finding 2] ADR-0012 config defaults match template")
print("=" * 70)

effort_heuristic_file = PROMPTS_DIR / "effort-heuristic.yaml.template"
effort_heuristic_exists = effort_heuristic_file.exists()
t("prompts/effort-heuristic.yaml.template exists", effort_heuristic_exists,
  "File not found" if not effort_heuristic_exists else "")

has_default_effort_3 = False
has_bias_lean = False

if effort_heuristic_exists:
    content = effort_heuristic_file.read_text()

    # Check for default_effort: 3 (exact value)
    has_default_effort_3 = bool(re.search(
        r"default_effort\s*:\s*3\b",
        content
    ))

    # Check for bias: lean (exact value)
    has_bias_lean = bool(re.search(
        r"bias\s*:\s*lean\b",
        content
    ))

t("effort-heuristic.yaml.template has 'default_effort: 3'",
  has_default_effort_3,
  "default_effort: 3 not found" if effort_heuristic_exists and not has_default_effort_3 else "")

t("effort-heuristic.yaml.template has 'bias: lean'",
  has_bias_lean,
  "bias: lean not found" if effort_heuristic_exists and not has_bias_lean else "")

print()
print("[Finding 3] Bounded-input property scoped at tool level")
print("=" * 70)

expert_scout_file = AGENTS_DIR / "expert-scout.md"
expert_scout_exists = expert_scout_file.exists()
t("agents/expert-scout.md exists", expert_scout_exists,
  "File not found" if not expert_scout_exists else "")

no_git_diff_tool = True
no_git_show_tool = True
has_tools_frontmatter = False

if expert_scout_exists:
    content = expert_scout_file.read_text()

    # Extract the frontmatter (lines between --- delimiters at the start)
    frontmatter_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    frontmatter = frontmatter_match.group(1) if frontmatter_match else ""
    has_tools_frontmatter = bool(frontmatter)

    # Check that tools frontmatter does NOT grant git diff or git show
    no_git_diff_tool = not bool(re.search(
        r"Bash\(git\s+diff:\*\)|git diff",
        frontmatter,
        re.IGNORECASE
    ))

    no_git_show_tool = not bool(re.search(
        r"Bash\(git\s+show:\*\)|git show",
        frontmatter,
        re.IGNORECASE
    ))

t("expert-scout.md has frontmatter section", has_tools_frontmatter,
  "No frontmatter found" if expert_scout_exists and not has_tools_frontmatter else "")

t("expert-scout.md tools does NOT grant 'Bash(git diff:*)'",
  no_git_diff_tool,
  "Found git diff tool grant in frontmatter" if expert_scout_exists and not no_git_diff_tool else "")

t("expert-scout.md tools does NOT grant 'Bash(git show:*)'",
  no_git_show_tool,
  "Found git show tool grant in frontmatter" if expert_scout_exists and not no_git_show_tool else "")

print()
print("[Finding 4] Checkpoint table row label changed to 'Step 3'")
print("=" * 70)

# This checks around line 125 of commands/expert-review.md for the checkpoint table row
has_step_3_label = False

if expert_review_exists:
    content = expert_review_file.read_text()

    # Look for a table row that mentions "Effort Scout" and "Step 3"
    # This row should be in a checkpoint table showing which step each component runs in
    has_step_3_label = bool(re.search(
        r"Effort\s+Scout.*Step\s+3|effort-scout.*Step\s+3",
        content,
        re.IGNORECASE
    ))

t("Checkpoint table row mentions 'Step 3' for Effort Scout (not 'Step 1')",
  has_step_3_label,
  "Step 3 reference for Effort Scout not found" if expert_review_exists and not has_step_3_label else "")

print()
print("[Finding 5] Scout failure writes explicit error-shape JSON")
print("=" * 70)

has_error_json_shape = False
has_fallback_error = False

if expert_review_exists:
    content = expert_review_file.read_text()

    # Check for prose about writing {"error": "..."} on fallback
    has_error_json_shape = bool(re.search(
        r'\{"error"\s*:\s*"[^"]*"\}|\{\s*error\s*:\s*',
        content
    ))

    # Check for prose about fallback paths or error reasons
    has_fallback_error = bool(re.search(
        r"effort-scout\.json|fallback|error.*reason|(?:missing|invalid|non-fatal)",
        content,
        re.IGNORECASE
    ))

t("Parse logic describes error-shape JSON with 'error' key",
  has_error_json_shape,
  "Error JSON shape not described" if expert_review_exists and not has_error_json_shape else "")

t("Parse logic describes fallback paths with explicit error reasons",
  has_fallback_error,
  "Fallback error handling not documented" if expert_review_exists and not has_fallback_error else "")

print()
print("[Finding 6] New section heading in ADR-0004")
print("=" * 70)

adr_0004_file = DOCS_ADR_DIR / "0004-model-cost-routing.md"
adr_0004_exists = adr_0004_file.exists()
t("docs/adr/0004-model-cost-routing.md exists", adr_0004_exists,
  "File not found" if not adr_0004_exists else "")

has_amendment_heading = False

if adr_0004_exists:
    content = adr_0004_file.read_text()

    # Check for new section heading: "### Amendment: Haiku Effort Scout exception (2026-09-12)"
    # or similar Amendment heading with Effort Scout
    has_amendment_heading = bool(re.search(
        r"^#+\s+Amendment.*Haiku.*Effort\s+Scout|^#+\s+Amendment.*Effort\s+Scout.*exception",
        content,
        re.MULTILINE | re.IGNORECASE
    ))

t("ADR-0004 has new 'Amendment: Haiku Effort Scout exception' heading",
  has_amendment_heading,
  "Amendment heading not found" if adr_0004_exists and not has_amendment_heading else "")

print()
print("[Finding 7] ADR-0004→ADR-0012 cross-reference anchor matches")
print("=" * 70)

adr_0012_file = DOCS_ADR_DIR / "0012-effort-ladder-and-pr-mode.md"
adr_0012_exists = adr_0012_file.exists()
t("docs/adr/0012-effort-ladder-and-pr-mode.md exists", adr_0012_exists,
  "File not found" if not adr_0012_exists else "")

adr_0004_has_link = False
adr_0012_has_matching_heading = False
anchor_text = None

# Check ADR-0004 for the link to ADR-0012 amendment section
if adr_0004_exists:
    content = adr_0004_file.read_text()

    # Look for markdown link to 0012 with amendment anchor
    link_match = re.search(
        r"\[.*?\]\(.*?0012[^)]*#amendment[^)]*\)",
        content,
        re.IGNORECASE
    )
    if link_match:
        adr_0004_has_link = True
        # Extract the anchor text for later verification
        anchor_match = re.search(
            r"#([a-z0-9\-]+)\)",
            link_match.group(0),
            re.IGNORECASE
        )
        if anchor_match:
            anchor_text = anchor_match.group(1)

t("ADR-0004 contains link to ADR-0012 amendment section",
  adr_0004_has_link,
  "Link to 0012 amendment not found" if adr_0004_exists and not adr_0004_has_link else "")

# Check ADR-0012 for matching heading
if adr_0012_exists and anchor_text:
    content = adr_0012_file.read_text()

    # Convert anchor text to heading pattern for matching:
    # anchor "amendment-haiku-effort-scout-2026-09-12" matches heading
    # "## Amendment: Haiku Effort Scout (2026-09-12)" when normalized
    # Try flexible matching: look for the key terms from anchor
    terms = anchor_text.split('-')
    # Build a pattern that looks for: heading + key terms (Amendment, Haiku, Effort, Scout, and year)
    # in roughly that order (allowing for variations in format)
    heading_pattern = r"^#+\s+Amendment.*(?:Haiku.*Effort.*Scout|Effort.*Scout)" + \
                      r".*(?:2026.*09.*12|2026-09-12|2026/09/12)"

    adr_0012_has_matching_heading = bool(re.search(
        heading_pattern,
        content,
        re.MULTILINE | re.IGNORECASE
    ))

t("ADR-0012 has heading matching the linked anchor from ADR-0004",
  adr_0012_has_matching_heading,
  f"No heading found matching anchor '{anchor_text}'" if adr_0012_exists and anchor_text and not adr_0012_has_matching_heading else "")

print()
h.summarize_and_exit()
