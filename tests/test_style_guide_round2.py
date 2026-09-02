#!/usr/bin/env python3
"""
Additional test suite for style-guide.json cascade (Round 2).

Covers edge cases and detailed requirements not tested in test_style_guide.py:
1. Schema validation edges: all examples/toneNotes are strings
2. Example counts and structure
3. Template structure validation
4. PR comment guide loading from examples array
5. generate-style-guide detailed requirements
6. peer-merge.md tone section wording
7. ADR-0017 specific documentation points
8. CLAUDE.md section placement
9. setup-local.md auto-seed divergence specificity

Run with: python3 tests/test_style_guide_round2.py
"""

import json
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

# Read all files needed for tests
STYLE_GUIDE_JSON = read(PROMPTS_DIR / "style-guide.json")
STYLE_GUIDE_TEMPLATE = read(PROMPTS_DIR / "style-guide.json.template")
PR_COMMENT_GUIDE = read(PROMPTS_DIR / "pr-comment-guide.md")
GENERATE_STYLE_GUIDE = read(COMMANDS_DIR / "generate-style-guide.md")
PEER_MERGE = read(PROMPTS_DIR / "peer-merge.md")
SETUP_LOCAL = read(COMMANDS_DIR / "setup-local.md")
CLAUDE_MD = read(REPO_ROOT / "CLAUDE.md")
ADR_0017 = read(DOCS_DIR / "0017-style-guide-cascade.md")

h = Harness("STYLE-GUIDE.JSON ROUND 2 EDGE CASES TEST SUITE")
t = h.test_result

# ============================================================================
# STYLE-GUIDE.JSON SCHEMA EDGES
# ============================================================================
print("[Schema Edges] All array elements are strings")

# Parse JSON
try:
    style_guide = json.loads(STYLE_GUIDE_JSON)
except json.JSONDecodeError:
    style_guide = {}

# Check all examples are strings
examples = style_guide.get("examples", [])
all_examples_strings = all(isinstance(ex, str) for ex in examples)
t("all examples are strings",
  all_examples_strings,
  f"Found non-string example(s) in examples array")

# Check all toneNotes are strings
tone_notes = style_guide.get("toneNotes", [])
all_tone_notes_strings = all(isinstance(note, str) for note in tone_notes)
t("all toneNotes are strings",
  all_tone_notes_strings,
  f"Found non-string toneNote(s) in toneNotes array")

# Check example count (plan says 6 in the default)
example_count = len(examples)
t("default style-guide.json has exactly 6 examples",
  example_count == 6,
  f"Default should have 6 examples, found {example_count}")

# ============================================================================
# TEMPLATE STRUCTURE VALIDATION
# ============================================================================
print()
print("[Template Structure] Template fields match default structure")

try:
    template = json.loads(STYLE_GUIDE_TEMPLATE)
except json.JSONDecodeError:
    template = {}

# Template should have examples and toneNotes fields
t("template has 'examples' field",
  "examples" in template,
  "Template should have examples field for users to fill in")

t("template 'examples' is a list",
  isinstance(template.get("examples"), list),
  "Template examples should be a list")

t("template has 'toneNotes' field",
  "toneNotes" in template,
  "Template should have toneNotes field for users to fill in")

t("template 'toneNotes' is a list",
  isinstance(template.get("toneNotes"), list),
  "Template toneNotes should be a list")

# Template should have version and source fields
t("template has 'version' field set to 1",
  template.get("version") == 1,
  "Template version should be 1")

# ============================================================================
# PR-COMMENT-GUIDE.MD LOADING INSTRUCTIONS
# ============================================================================
print()
print("[Tone Loading] pr-comment-guide.md mentions loading from examples array")

t("pr-comment-guide.md references 'examples' loading",
  bool(re.search(r"examples|load.*examples|examples.*from", PR_COMMENT_GUIDE, re.IGNORECASE)),
  "Tone section should describe loading from examples array")

t("pr-comment-guide.md does not have hardcoded bullet list of comments",
  not bool(re.search(
    r'- ".{10,}.{10,}"\s*\n- ".{10,}.{10,}"',
    PR_COMMENT_GUIDE
  )),
  "Should not have six hardcoded example bullets — must load from cascade")

# ============================================================================
# GENERATE-STYLE-GUIDE DETAILED REQUIREMENTS
# ============================================================================
print()
print("[Generate Command] Detailed implementation requirements")

# User identification
t("command uses 'gh api user' for user identification",
  "gh api user" in GENERATE_STYLE_GUIDE,
  "Step 1 should use 'gh api user' to fetch invoking user's login")

# Scope resolution
t("command documents scope resolution logic (args or current repo)",
  bool(re.search(r"ARGUMENTS|current repo|scope", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Should document how scope is resolved from args or defaults to current repo")

# GraphQL for fetching comments
t("command uses GraphQL for fetching review comments",
  "graphql" in GENERATE_STYLE_GUIDE.lower(),
  "Should use GraphQL API to fetch review-thread comments")

# Filtering to user's own comments
t("command filters to author.login == $login",
  "author.login" in GENERATE_STYLE_GUIDE,
  "Must filter review comments to only the invoking user's own comments")

# Pre-filtering trivial comments
t("command mentions pre-filtering trivial/short comments",
  bool(re.search(r"trivial|short|fewer|20 words|bot|noise", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Step 5 should describe filtering out trivial/short comments")

# Examples count: 6-12
t("command specifies 6-12 examples range",
  bool(re.search(r"6.*12|6-12|6 to 12", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should specify picking 6-12 representative examples")

# Tone notes count: 3-6
t("command specifies 3-6 toneNotes range",
  bool(re.search(r"3.*6|3-6|3 to 6.*tone", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should specify drafting 3-6 toneNotes")

# Verbatim examples (not rewritten)
t("command specifies examples are verbatim or lightly trimmed",
  bool(re.search(r"verbatim|lightly.*trim|never.*rewritten?|different register", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should require examples to be verbatim or lightly trimmed, never rewritten into different register")

# Flag and genericize sensitive content
t("command explicitly addresses genericizing sensitive content",
  bool(re.search(r"genericiz|sensitive|employer|flag.*generic|explicit", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should explicitly describe flagging and genericizing sensitive content")

# Generated file fields
t("command writes source='generated'",
  bool(re.search(r'"source":\s*"generated"|source.*generated', GENERATE_STYLE_GUIDE)),
  "Generated file should have source: 'generated'")

t("command writes real generatedAt timestamp",
  bool(re.search(r"generatedAt|ISO-8601|timestamp", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Generated file should have generatedAt set to ISO-8601 timestamp")

t("command writes scope with repos list",
  bool(re.search(r'"scope".*repos|scope.*repos|repos.*owner/repo', GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Generated file should have scope.repos listing searched repositories")

# JSON validation before writing
t("command validates JSON before writing",
  bool(re.search(r"json|validate", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should validate generated JSON before writing to disk")

# ============================================================================
# PEER-MERGE.MD TONE SECTION WORDING
# ============================================================================
print()
print("[Peer Merge] Tone section is condensed prose-only restatement")

t("peer-merge.md Step 4 title mentions 'Apply Peer Tone'",
  bool(re.search(r"## Step 4.*Apply.*Tone|Apply.*Peer.*Tone", PEER_MERGE)),
  "Step 4 should be titled 'Apply Peer Tone' or similar")

t("peer-merge.md explicitly says prose-only and intentionally not wired",
  bool(re.search(
    r"condensed.*prose-only|prose.*condensed|intentionally.*NOT.*wired|NOT wired.*style-guide|style-guide.*omission.*deliberate",
    PEER_MERGE,
    re.IGNORECASE
  )),
  "Should explicitly state this is a prose-only restatement intentionally not wired into style-guide.json")

# ============================================================================
# ADR-0017 DOCUMENTATION SPECIFICITY
# ============================================================================
print()
print("[ADR-0017] Specific design documentation")

# Install behavior divergence
t("ADR-0017 documents install.sh auto-seed divergence from preferences.yaml",
  bool(re.search(
    r"never auto-seeded|auto-seed.*divergence|unlike.*preferences|preferences.*auto-.*seed",
    ADR_0017,
    re.IGNORECASE
  )),
  "Decision section should document why style-guide.json is never auto-seeded unlike preferences.yaml")

# Four-layer cascade is separate
t("ADR-0017 explicitly states tone cascade is separate from four-layer reviewer-context cascade",
  bool(re.search(
    r"separate.*cascade|four-layer|four layer|reviewer.*context.*cascade|separate.*from.*ADR-0005",
    ADR_0017,
    re.IGNORECASE
  )),
  "Should explain why tone is a separate two-layer cascade, not part of the four-layer reviewer-context cascade")

# Confirm-before-write lineage from ADR-0007
t("ADR-0017 traces confirm-before-write design to ADR-0007 precedent",
  bool(re.search(
    r"ADR-0007.*confirm|confirm.*ADR-0007|decisions\.yaml|ADR-0007.*human-reviewed|precedent.*ADR-0007",
    ADR_0017,
    re.IGNORECASE
  )),
  "Decision section should trace the confirm-before-write design to ADR-0007's lesson about decision memory")

# peer-merge.md exclusion reason documented
t("ADR-0017 explains peer-merge.md exclusion reason",
  bool(re.search(
    r"peer-merge|fast.?path|latency.*optimized|performance.*tradeoff|omission.*deliberate|deliberately",
    ADR_0017,
    re.IGNORECASE
  )),
  "Should document why peer-merge.md omits the style-guide.json cascade (latency optimization)")

# ============================================================================
# CLAUDE.MD SECTION PLACEMENT AND CONTENT
# ============================================================================
print()
print("[CLAUDE.md] Section placement and cross-reference")

# Find both sections
user_prefs_idx = CLAUDE_MD.find("## User preferences")
pr_tone_idx = CLAUDE_MD.find("## PR comment tone")

t("CLAUDE.md has both 'User preferences' and 'PR comment tone' sections",
  user_prefs_idx > 0 and pr_tone_idx > 0,
  "Both sections should be present")

# Check order (User preferences should come before PR comment tone)
if user_prefs_idx > 0 and pr_tone_idx > 0:
    t("'PR comment tone' section appears after 'User preferences' section",
      pr_tone_idx > user_prefs_idx,
      "PR comment tone section should logically follow User preferences section")

# Check ADR reference in PR comment tone section
pr_tone_section_start = CLAUDE_MD.find("## PR comment tone")
if pr_tone_section_start > 0:
    # Extract text until next major section (##)
    next_section_idx = CLAUDE_MD.find("\n## ", pr_tone_section_start + 1)
    if next_section_idx < 0:
        pr_tone_text = CLAUDE_MD[pr_tone_section_start:]
    else:
        pr_tone_text = CLAUDE_MD[pr_tone_section_start:next_section_idx]

    t("'PR comment tone' section mentions ADR-0017",
      "ADR-0017" in pr_tone_text or "0017" in pr_tone_text,
      "PR comment tone section should reference ADR-0017 for design rationale")

# ============================================================================
# SETUP-LOCAL.MD AUTO-SEED DIVERGENCE SPECIFICITY
# ============================================================================
print()
print("[Setup Local] Auto-seed divergence is specifically documented")

# Check that it mentions the divergence, not just mentioning the file
t("setup-local.md explicitly documents auto-seed divergence from preferences.yaml",
  bool(re.search(
    r"never auto-created|never auto-seeded|unlike.*preferences|preferences.*auto.*seed",
    SETUP_LOCAL,
    re.IGNORECASE
  )),
  "Should specifically state that personal style-guide.json is never auto-created (unlike preferences.yaml), not just mention the file")

# Check that it explains the rationale (personal file shouldn't be auto-seeded with someone else's voice)
t("setup-local.md explains why personal style-guide.json isn't auto-seeded",
  bool(re.search(
    r"personal|generate|manual.*copy|template|confirmation",
    SETUP_LOCAL,
    re.IGNORECASE
  )),
  "Should explain that users get it via /generate-style-guide or manual template copy")

h.summarize_and_exit()
