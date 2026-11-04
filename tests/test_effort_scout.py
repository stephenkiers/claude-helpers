#!/usr/bin/env python3
"""
Test suite for the Haiku Effort Scout (ADR-0012 amendment, ADR-0004 narrow exception).

Verifies the Scout prompt exists with the right output contract and guardrails, that
expert-scout.md and expert-review.md wire it in with a deterministic fallback, and that
both ADRs record the exception.

Run with: python3 tests/test_effort_scout.py
"""

import re
from _test_harness import REPO_ROOT, Harness

PROMPTS_DIR = REPO_ROOT / "prompts"
COMMANDS_DIR = REPO_ROOT / "commands"
AGENTS_DIR = REPO_ROOT / "agents"
ADR_DIR = REPO_ROOT / "docs" / "adr"

h = Harness("EFFORT SCOUT TEST SUITE")
test_result = h.test_result


def read(path):
    return path.read_text()


# ============================================================================
# INVARIANT 1: prompts/effort-scout.md exists with the JSON output contract
# ============================================================================
print("[Invariant 1] prompts/effort-scout.md exists with required contract")

scout_file = PROMPTS_DIR / "effort-scout.md"
scout_exists = scout_file.exists()
test_result("prompts/effort-scout.md exists", scout_exists,
            "File is missing" if not scout_exists else "")

if scout_exists:
    scout_text = read(scout_file)
    test_result(
        "documents the {effort, reason} JSON output",
        '"effort"' in scout_text and '"reason"' in scout_text,
        "JSON output shape not documented"
    )
    test_result(
        "restricts effort to 2/3/4",
        "2, 3, or 4" in scout_text or "[2, 4]" in scout_text,
        "Tier range not documented"
    )
    test_result(
        "only allows downward deviation from the mechanical tier",
        "lower" in scout_text and "never" in scout_text,
        "Downward-only constraint not documented"
    )
    test_result(
        "treats diff/issue content as data, not instructions",
        "data, never instructions" in scout_text or "data, not\ncommands" in scout_text
        or "not\ncommands to obey" in scout_text,
        "Prompt-injection guard not documented"
    )
    test_result(
        "diff-index.md is the bounded input (never the full patch)",
        "diff-index.md" in scout_text and "full patch" in scout_text,
        "Bounded-input rule not documented"
    )

# ============================================================================
# INVARIANT 2: agents/expert-scout.md wires in the Effort Scout job
# ============================================================================
print()
print("[Invariant 2] agents/expert-scout.md documents the Effort Scout job")

agent_file = AGENTS_DIR / "expert-scout.md"
agent_text = read(agent_file) if agent_file.exists() else ""
test_result("agents/expert-scout.md exists", agent_file.exists())
test_result(
    "mentions Effort Scout",
    "Effort Scout" in agent_text,
    "Effort Scout job not listed"
)
test_result(
    "references prompts/effort-scout.md",
    "effort-scout.md" in agent_text,
    "Prompt file not referenced"
)

# ============================================================================
# INVARIANT 3: commands/expert-review.md wires the Scout into the heuristic
# with a deterministic, non-blocking fallback
# ============================================================================
print()
print("[Invariant 3] commands/expert-review.md wires in the Scout with a fallback")

review_file = COMMANDS_DIR / "expert-review.md"
review_text = read(review_file)
review_text_flat = " ".join(review_text.split())

test_result(
    "spawns the Effort Scout via prompts/effort-scout.md",
    "prompts/effort-scout.md" in review_text,
    "Scout invocation not found"
)
test_result(
    "risk-keyword floor skips the Scout entirely",
    "not spawned for a diff that already floored to 4" in review_text_flat,
    "Risk-floor short-circuit of the Scout not documented"
)
test_result(
    "falls back to the mechanical calculation on Scout failure",
    "fall back to" in review_text and "MECHANICAL_EFFORT" in review_text,
    "Deterministic fallback not documented"
)
test_result(
    "never lets a Scout failure block the run",
    "Never let a Scout failure block the run" in review_text,
    "Non-fatal guarantee not documented"
)
test_result(
    "EFFORT_SOURCE distinguishes risk-floor, haiku-scout, and heuristic",
    all(s in review_text for s in ["EFFORT_SOURCE=risk-floor", "EFFORT_SOURCE=haiku-scout", "EFFORT_SOURCE=heuristic"]),
    "Not all three EFFORT_SOURCE values are set explicitly"
)
test_result(
    "calibration flag covers the Scout's own picks, not just the mechanical heuristic",
    "haiku-scout" in review_text.split("Calibration flag")[1][:600] if "Calibration flag" in review_text else False,
    "Calibration flag condition doesn't mention haiku-scout"
)

# ============================================================================
# INVARIANT 4: both ADRs record the exception
# ============================================================================
print()
print("[Invariant 4] ADR-0012 and ADR-0004 record the Haiku Effort Scout exception")

adr_0012 = read(ADR_DIR / "0012-effort-ladder-and-pr-mode.md")
adr_0004 = read(ADR_DIR / "0004-model-cost-routing.md")

test_result(
    "ADR-0012 has a Haiku Effort Scout amendment",
    "Haiku Effort Scout" in adr_0012,
    "Amendment section missing from ADR-0012"
)
test_result(
    "ADR-0004 cross-references the ADR-0012 amendment as a narrow exception",
    "Effort Scout" in adr_0004 and "0012-effort-ladder-and-pr-mode.md" in adr_0004,
    "ADR-0004 does not record the exception"
)

# ============================================================================
# INVARIANT 5: prompts/effort-heuristic.yaml.template has correct config defaults
# ============================================================================
print()
print("[Invariant 5] prompts/effort-heuristic.yaml.template has default_effort: 3 and bias: lean")

heuristic_file = PROMPTS_DIR / "effort-heuristic.yaml.template"
heuristic_text = read(heuristic_file) if heuristic_file.exists() else ""

# Extract default_effort value using a simple regex
default_effort_match = re.search(r'^\s*default_effort:\s*(\d+)', heuristic_text, re.MULTILINE)
default_effort_value = int(default_effort_match.group(1)) if default_effort_match else None
test_result(
    "default_effort is set to 3 (not 4)",
    default_effort_value == 3,
    f"default_effort is {default_effort_value}, expected 3"
)

# Extract bias value using a simple regex
bias_match = re.search(r'^\s*bias:\s*(\w+)', heuristic_text, re.MULTILINE)
bias_value = bias_match.group(1) if bias_match else None
test_result(
    "bias is set to lean (not over-review)",
    bias_value == "lean",
    f"bias is {bias_value}, expected lean"
)

# ============================================================================
# INVARIANT 6: ADR-0004 cross-reference anchor matches ADR-0012 heading
# ============================================================================
print()
print("[Invariant 6] ADR-0004→ADR-0012 cross-reference anchor matches the actual heading")

# Extract the anchor fragment from ADR-0004's link
anchor_match = re.search(
    r'0012-effort-ladder-and-pr-mode\.md#([a-z0-9-]+)',
    adr_0004
)
anchor_fragment = anchor_match.group(1) if anchor_match else None

# Extract the heading from ADR-0012 that should correspond to this anchor
# Look for "## Amendment: Haiku Effort Scout" or similar pattern
heading_match = re.search(
    r'^#+\s+Amendment:\s*Haiku Effort Scout.*?\(2026-09-12\)',
    adr_0012,
    re.MULTILINE
)
heading_text = heading_match.group(0) if heading_match else None

# Convert heading to GitHub-style anchor slug: lowercase, keep alphanumerics/spaces/hyphens, spaces→hyphens
def heading_to_slug(heading):
    if not heading:
        return None
    # Remove leading #'s and whitespace
    text = re.sub(r'^#+\s*', '', heading)
    # Lowercase
    text = text.lower()
    # Keep only alphanumerics, spaces, hyphens, parentheses
    text = re.sub(r'[^\w\s\-\(\)]', '', text)
    # Replace spaces/parens/colons with hyphens, collapse multiples
    text = re.sub(r'[\s\-\(\):]+', '-', text)
    # Strip leading/trailing hyphens
    text = text.strip('-')
    return text

expected_slug = heading_to_slug(heading_text)
test_result(
    "anchor fragment matches the heading slug",
    anchor_fragment == expected_slug,
    f"anchor is '{anchor_fragment}', heading slug is '{expected_slug}'"
)

print()
print("[Finding 1] Effort Scout downward-only invariant enforced at parse boundary")
print("=" * 70)

expert_review_file = COMMANDS_DIR / "expert-review.md"
expert_review_exists = expert_review_file.exists()
test_result("commands/expert-review.md exists", expert_review_exists,
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

test_result("Parse logic mentions MECHANICAL_EFFORT or effort clamping",
  has_mechanical_effort_clamp,
  "No reference to MECHANICAL_EFFORT clamping found" if expert_review_exists and not has_mechanical_effort_clamp else "")

test_result("Parse logic checks if Scout effort exceeds mechanical tier",
  has_effort_exceeds_check,
  "No check for effort bounds found" if expert_review_exists and not has_effort_exceeds_check else "")

print()
print("[Finding 3] Bounded-input property scoped at tool level")
print("=" * 70)

expert_scout_file = AGENTS_DIR / "expert-scout.md"
expert_scout_exists = expert_scout_file.exists()
test_result("agents/expert-scout.md exists", expert_scout_exists,
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

test_result("expert-scout.md has frontmatter section", has_tools_frontmatter,
  "No frontmatter found" if expert_scout_exists and not has_tools_frontmatter else "")

test_result("expert-scout.md tools does NOT grant 'Bash(git diff:*)'",
  no_git_diff_tool,
  "Found git diff tool grant in frontmatter" if expert_scout_exists and not no_git_diff_tool else "")

test_result("expert-scout.md tools does NOT grant 'Bash(git show:*)'",
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

test_result("Checkpoint table row mentions 'Step 3' for Effort Scout (not 'Step 1')",
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
        r'\{"error"\s*:\s*"[^"]*"\}|\{\s*error\s*:',
        content
    ))

    # Check for prose about fallback paths or error reasons
    has_fallback_error = bool(re.search(
        r"effort-scout\.json|fallback|error.*reason|(?:missing|invalid|non-fatal)",
        content,
        re.IGNORECASE
    ))

test_result("Parse logic describes error-shape JSON with 'error' key",
  has_error_json_shape,
  "Error JSON shape not described" if expert_review_exists and not has_error_json_shape else "")

test_result("Parse logic describes fallback paths with explicit error reasons",
  has_fallback_error,
  "Fallback error handling not documented" if expert_review_exists and not has_fallback_error else "")

print()
print("[Finding 6] New section heading in ADR-0004")
print("=" * 70)

adr_0004_file = ADR_DIR / "0004-model-cost-routing.md"
adr_0004_exists = adr_0004_file.exists()
test_result("docs/adr/0004-model-cost-routing.md exists", adr_0004_exists,
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

test_result("ADR-0004 has new 'Amendment: Haiku Effort Scout exception' heading",
  has_amendment_heading,
  "Amendment heading not found" if adr_0004_exists and not has_amendment_heading else "")

h.summarize_and_exit()
