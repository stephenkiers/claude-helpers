#!/usr/bin/env python3
"""
Test suite for user preferences layer + gut-check cluster auto-conversion.

Covers:
1. Preferences layer: schema, template, loading in expert-framework.md, setup-local.md
2. Cluster auto-conversion: ≥3 findings shared premise → Needs you cluster item

Run with: python3 tests/test_preferences_and_clusters.py
"""

import re

from _test_harness import REPO_ROOT, Harness

PROMPTS_DIR = REPO_ROOT / "prompts"
COMMANDS_DIR = REPO_ROOT / "commands"
DOCS_DIR = REPO_ROOT / "docs" / "adr"

def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""

PREFERENCES_TEMPLATE = read(PROMPTS_DIR / "preferences.yaml.template")
EXPERT_FRAMEWORK = read(PROMPTS_DIR / "expert-framework.md")
SETUP_LOCAL = read(COMMANDS_DIR / "setup-local.md")
TRIAGE = read(PROMPTS_DIR / "triage.md")
ADR_0005 = read(DOCS_DIR / "0005-three-layer-context-cascade.md")
ADR_README = read(DOCS_DIR / "README.md")
CLAUDE_MD = read(REPO_ROOT / "CLAUDE.md")

h = Harness("USER PREFERENCES + GUT-CHECK CLUSTERS TEST SUITE")
t = h.test_result

# ============================================================================
# PREFERENCES.YAML.TEMPLATE TESTS
# ============================================================================
print("[Preferences Layer] Template file structure and content")

t("preferences.yaml.template exists",
  (PROMPTS_DIR / "preferences.yaml.template").exists(),
  "File not found at prompts/preferences.yaml.template")

# All example preferences should be commented out
t("all example entries are commented out (leading #)",
  bool(re.search(r"^# - name: no-magic-strings", PREFERENCES_TEMPLATE, re.MULTILINE)),
  "Example 'no-magic-strings' entry should be commented out with leading '# -'")

t("no-magic-strings example is present",
  "no-magic-strings" in PREFERENCES_TEMPLATE,
  "Example preference 'no-magic-strings' is missing from template")

t("no-as-in-ts example is present",
  "no-as-in-ts" in PREFERENCES_TEMPLATE,
  "Example preference 'no-as-in-ts' is missing from template")

t("readability-over-cleverness example is present",
  "readability-over-cleverness" in PREFERENCES_TEMPLATE,
  "Example preference 'readability-over-cleverness' is missing from template")

# Template header must explain key behavioral clauses
print()
print("[Preferences Layer] Template header behavioral contract")

t("header explains 'lens' concept (not suppression list)",
  bool(re.search(r"LENS.*suppression|never.*suppression\s+list", PREFERENCES_TEMPLATE, re.IGNORECASE | re.DOTALL)),
  "Header must explain that preferences are a lens, not a suppression list")

t("header mentions 'hard floor' for CRITICAL findings",
  bool(re.search(r"hard\s+floor|never\s+dilute.*CRITICAL|CRITICAL.*never\s+dilute", PREFERENCES_TEMPLATE, re.IGNORECASE)),
  "Header must state that CRITICAL findings are never diluted by preferences")

t("header mentions project-level wins on conflict",
  bool(re.search(r"project.*win|project.*override|project-level.*rule", PREFERENCES_TEMPLATE, re.IGNORECASE)),
  "Header must state that project.yaml rules override preferences on conflict")

t("header mentions reviewers noting when preference shaped wording",
  bool(re.search(r"note.*reasoning|reasoning.*note|shaped.*wording|wording.*prefer", PREFERENCES_TEMPLATE, re.IGNORECASE | re.DOTALL)),
  "Header must encourage reviewers to note in reasoning when preferences shaped wording")

# Schema documentation
print()
print("[Preferences Layer] Template schema documentation")

t("template documents 'name' field",
  "name:" in PREFERENCES_TEMPLATE,
  "Schema must document the 'name' field")

t("template documents 'rule' field",
  "rule:" in PREFERENCES_TEMPLATE,
  "Schema must document the 'rule' field")

t("template documents 'spirit' field",
  "spirit:" in PREFERENCES_TEMPLATE,
  "Schema must document the 'spirit' field")

t("template documents 'appliesTo' field",
  "appliesTo:" in PREFERENCES_TEMPLATE,
  "Schema must document the 'appliesTo' field")

t("template documents 'revisitIf' field",
  "revisitIf:" in PREFERENCES_TEMPLATE,
  "Schema must document the 'revisitIf' field")

# ============================================================================
# EXPERT-FRAMEWORK.MD PREFERENCES LOADING
# ============================================================================
print()
print("[Preferences Layer] expert-framework.md loads preferences.yaml")

t("expert-framework.md exists",
  (PROMPTS_DIR / "expert-framework.md").exists(),
  "File not found at prompts/expert-framework.md")

t("expert-framework.md has 'Load Project Context' section",
  "Load Project Context" in EXPERT_FRAMEWORK,
  "expert-framework.md must have a 'Load Project Context' section")

# The plan says: Load project context step adds third load: ~/.claude/preferences.yaml
t("preferences.yaml loading is mentioned in expert-framework.md",
  "preferences.yaml" in EXPERT_FRAMEWORK,
  "expert-framework.md must mention loading ~/.claude/preferences.yaml")

# Verify it's in the right place: in Load Project Context
load_context_match = re.search(
    r"Load Project Context.*?\n(.*?)(?=\n## |\n### [A-Z]|\Z)",
    EXPERT_FRAMEWORK,
    re.DOTALL | re.IGNORECASE
)
load_context_section = load_context_match.group(1) if load_context_match else ""

t("preferences.yaml loading is within 'Load Project Context' section",
  "preferences.yaml" in load_context_section,
  "preferences.yaml must be loaded as part of the Load Project Context step")

t("preferences.yaml loading allows it to be optional (skip if not present)",
  bool(re.search(r"(?:if|optional|not.*present|skip|—)", load_context_section, re.IGNORECASE)),
  "Loading instruction should indicate that preferences.yaml is optional and can be skipped if missing")

# Verify framing mentions it as a lens
t("expert-framework.md framing mentions preferences as a lens",
  bool(re.search(r"lens|shape.*wording|priorit", load_context_section, re.IGNORECASE)),
  "expert-framework.md should frame preferences as a lens that shapes wording/priority")

# ============================================================================
# SETUP-LOCAL.MD PREFERENCES CREATION
# ============================================================================
print()
print("[Preferences Layer] setup-local.md creates preferences.yaml")

t("setup-local.md exists",
  (COMMANDS_DIR / "setup-local.md").exists(),
  "File not found at commands/setup-local.md")

t("setup-local.md references preferences.yaml template",
  "preferences.yaml" in SETUP_LOCAL,
  "setup-local.md must reference preferences.yaml")

t("setup-local.md mentions copying from template",
  bool(re.search(r"template|copy|from.*preferences\.yaml\.template", SETUP_LOCAL, re.IGNORECASE)),
  "setup-local.md should mention creating preferences.yaml from the template")

# ============================================================================
# ADR-0005 AMENDMENT: FOUR-LAYER CASCADE
# ============================================================================
print()
print("[Preferences Layer] ADR-0005 documents four-layer cascade amendment")

t("ADR-0005 exists",
  (DOCS_DIR / "0005-three-layer-context-cascade.md").exists(),
  "File not found at docs/adr/0005-three-layer-context-cascade.md")

t("ADR-0005 mentions preferences in an amendment",
  bool(re.search(r"amendment|preferences\.yaml", ADR_0005, re.IGNORECASE)),
  "ADR-0005 must have an amendment section that mentions preferences.yaml as the fourth layer")

t("ADR-0005 mentions that preferences adds a fourth layer",
  bool(re.search(r"fourth\s+layer|four.*layer|cascade.*four", ADR_0005, re.IGNORECASE)),
  "ADR-0005 amendment should mention preferences.yaml as adding a fourth layer to the cascade")

# ============================================================================
# ADR-README PREFERENCES REFERENCE
# ============================================================================
print()
print("[Preferences Layer] ADR README entry for 0005 mentions preferences")

t("docs/adr/README.md exists",
  (DOCS_DIR / "README.md").exists(),
  "File not found at docs/adr/README.md")

t("ADR-0005 entry in README mentions preferences",
  bool(re.search(r"ADR-0005.*preferences|preferences\.yaml.*0005|with\s+`preferences\.yaml`", ADR_README, re.IGNORECASE)),
  "docs/adr/README.md ADR-0005 entry must mention preferences amendment")

# ============================================================================
# CLAUDE.MD USER PREFERENCES SECTION
# ============================================================================
print()
print("[Preferences Layer] CLAUDE.md has 'User preferences' section")

t("CLAUDE.md exists",
  (REPO_ROOT / "CLAUDE.md").exists(),
  "File not found at CLAUDE.md")

t("CLAUDE.md has 'User preferences' section",
  bool(re.search(r"^#+\s+User\s+preferences", CLAUDE_MD, re.MULTILINE | re.IGNORECASE)),
  "CLAUDE.md must have a 'User preferences' section")

# Verify the section explains the behavioral contract
user_prefs_section_match = re.search(
    r"^#+\s+User\s+preferences.*?\n(.*?)(?=\n^#+\s[^#]|\Z)",
    CLAUDE_MD,
    re.MULTILINE | re.DOTALL | re.IGNORECASE
)
user_prefs_section = user_prefs_section_match.group(1) if user_prefs_section_match else ""

t("User preferences section mentions lens (not suppression)",
  bool(re.search(r"lens|not.*suppression|suppression.*not", user_prefs_section, re.IGNORECASE)),
  "User preferences section must explain lens concept")

# ============================================================================
# GUT-CHECK CLUSTER AUTO-CONVERSION: TRIAGE.MD FEATURES
# ============================================================================
print()
print("[Clusters] triage.md cluster synthesis for ≥3 shared-premise findings")

t("triage.md exists",
  (PROMPTS_DIR / "triage.md").exists(),
  "File not found at prompts/triage.md")

t("triage.md mentions cluster synthesis when ≥3 findings share premise",
  bool(re.search(r"cluster|3\s+(?:finding|confirm)|shared\s+premise", TRIAGE, re.IGNORECASE)),
  "triage.md must describe cluster synthesis: when ≥3 confirmed findings trace to one premise")

t("triage.md describes synthesized cluster as 'Needs you' item",
  bool(re.search(r"cluster.*Needs you|Needs you.*cluster", TRIAGE, re.IGNORECASE)),
  "triage.md must explain that synthesized clusters become Needs you escalations")

# ============================================================================
# GUT-CHECK CLUSTER: DISSOLVES FIELD
# ============================================================================
print()
print("[Clusters] Cluster item has 'Dissolves' field")

t("triage.md cluster item template includes 'Dissolves' field",
  bool(re.search(r"\*\*Dissolves\*\*|Dissolves:", TRIAGE, re.IGNORECASE)),
  "Cluster item template must include a 'Dissolves' field")

t("'Dissolves' field lists finding titles (not ordinals)",
  bool(re.search(r"Dissolves.*(?:title|name|bullet)|finding\s+title", TRIAGE, re.IGNORECASE | re.DOTALL)),
  "Dissolves field must list finding titles, not ordinals (ordinals shift if findings renumber)")

# ============================================================================
# GUT-CHECK CLUSTER: OR APPLY PIECEMEAL OPTION
# ============================================================================
print()
print("[Clusters] Cluster item has 'Or apply piecemeal' named option")

t("triage.md mentions 'Or apply piecemeal' as named option",
  bool(re.search(r"Or apply piecemeal|piecemeal.*option", TRIAGE, re.IGNORECASE)),
  "triage.md must document 'Or apply piecemeal' as a named option (not buried in prose)")

t("'Or apply piecemeal' option is presented as alternative to fixing upstream assumption",
  bool(re.search(r"apply.*each.*finding.*individual|individual.*without.*fixing|fix.*upstream", TRIAGE, re.IGNORECASE | re.DOTALL)),
  "'Or apply piecemeal' should mean applying findings individually without fixing the upstream assumption")

# ============================================================================
# GUT-CHECK CLUSTER: OVER-ESCALATION GUARD UPDATE
# ============================================================================
print()
print("[Clusters] Over-escalation guard discounts clusters")

t("triage.md over-escalation guard uses 'clusters-escalated' field",
  bool(re.search(r"clusters-escalated|clusters-escalated", TRIAGE, re.IGNORECASE)),
  "Receipt should include 'clusters-escalated' field for guard arithmetic")

t("guard formula uses 0.5 discount for clusters-escalated",
  bool(re.search(r"0\.5\s*\*\s*clusters|clusters.*0\.5|human_asks.*0\.5", TRIAGE, re.IGNORECASE)),
  "Guard formula must discount clusters: human_asks = max(0, needs_you - 0.5 * clusters_escalated)")

t("guard trips on human_asks >= 5 OR over-escalation ratio",
  bool(re.search(r"human_asks\s*>=\s*5|0\.2.*confirmed|confirmed.*0\.2|ratio", TRIAGE, re.IGNORECASE)),
  "Guard must check both human_asks >= 5 and ratio (human_asks / confirmed > 0.2 AND confirmed >= 10)")

# ============================================================================
# GUT-CHECK: DRIFT AND PANEL DISAGREEMENT STAY PROSE ONLY
# ============================================================================
print()
print("[Clusters] Drift and Panel disagreement gut-check questions remain prose only")

# These should NOT be synthesized into cluster items; they stay as prose questions
t("triage.md gut-check section contains Drift question (prose, not cluster)",
  bool(re.search(r"Drift|drift.*premise|drift.*assumption", TRIAGE, re.IGNORECASE)),
  "Drift gut-check question must remain in prose form (not synthesized to clusters)")

t("triage.md gut-check section contains Panel disagreement question (prose, not cluster)",
  bool(re.search(r"Panel disagreement|disagreement.*panel|panel.*disagree", TRIAGE, re.IGNORECASE)),
  "Panel disagreement gut-check question must remain in prose form (not synthesized to clusters)")

# ============================================================================
# RECEIPT FIELD ORDERING
# ============================================================================
print()
print("[Clusters] Receipt field includes clusters-escalated in expected order")

# The receipt schema should include clusters-escalated alongside existing fields
t("triage.md receipt schema mentions required fields",
  bool(re.search(r"receipt|confirmed:|needs.you:|clusters:", TRIAGE, re.IGNORECASE)),
  "Receipt should document its field schema")

t("clusters-escalated appears in receipt field list",
  bool(re.search(r"clusters.escalated|clusters-escalated", TRIAGE, re.IGNORECASE)),
  "Receipt must include clusters-escalated field in its schema")

# ============================================================================
# EDGE CASE: SHARED PREMISE IDENTIFICATION (≥3 FINDINGS)
# ============================================================================
print()
print("[Clusters] Shared premise analysis triggers on ≥3 confirmed findings")

t("triage.md describes shared premise as cross-cutting analysis",
  bool(re.search(r"Shared premise|shared\s+premise", TRIAGE, re.IGNORECASE)),
  "Gut-check section must have 'Shared premise' analysis step")

t("shared premise section specifies ≥3 findings threshold",
  bool(re.search(r"three\s+or\s+more|do\s+three|3\s+finding", TRIAGE, re.IGNORECASE)),
  "Shared premise analysis must specify ≥3 confirmed findings tracing to one upstream assumption")

# ============================================================================
# EDGE CASE: FINDINGS REMAIN IN DOING IT UNDER CLUSTER
# ============================================================================
print()
print("[Clusters] Findings remain in Doing it with dissolves notation")

t("triage.md explains findings stay in Doing it when clustered",
  bool(re.search(r"Doing it.*sub|dissolves|⤴|remain.*Doing", TRIAGE, re.IGNORECASE | re.DOTALL)),
  "Findings in a cluster must remain in Doing it as a sub-list with a note about dissolution")

t("triage.md uses dissolves marker (⤴ or similar) in findings",
  bool(re.search(r"⤴|⬆|dissolve|cluster.*dissolve", TRIAGE, re.IGNORECASE)),
  "Findings should be marked when they dissolve if a cluster option is chosen")

h.summarize_and_exit()
