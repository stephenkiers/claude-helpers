#!/usr/bin/env python3
"""
Test suite for style-guide.json cascade: portable PR comment tone guide.

Covers:
1. Regression guard: no hardcoded personal path in prompts/commands
2. style-guide.json schema and defaults (shipped)
3. style-guide.json.template schema and instructions
4. pr-comment-guide.md tone section rewrite
5. generate-style-guide.md command (frontmatter, allowed-tools, confirmation gate)
6. ADR-0017 and README index entry
7. CLAUDE.md updates (command list, new section)
8. setup-local.md updates
9. peer-merge.md exclusion note

Run with: python3 tests/test_style_guide.py
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
ADR_README = read(DOCS_DIR / "README.md")

h = Harness("STYLE-GUIDE.JSON CASCADE TEST SUITE")
t = h.test_result

# ============================================================================
# REGRESSION GUARD: NO HARDCODED PERSONAL PATH
# ============================================================================
print("[Regression Guard] No hardcoded personal path in repo files")

t("no hardcoded path in prompts/",
  not re.search(r"~/Repositories/me/main/voice/style-guide\.md", PR_COMMENT_GUIDE),
  "Found hardcoded personal path '~/Repositories/me/main/voice/style-guide.md' in pr-comment-guide.md — must be removed")

t("no hardcoded path in commands/",
  not bool(re.search(r"~/Repositories/me/main/voice/style-guide\.md", GENERATE_STYLE_GUIDE)),
  "Found hardcoded personal path in generate-style-guide.md — must be removed")

# ============================================================================
# STYLE-GUIDE.JSON (SHIPPED DEFAULT)
# ============================================================================
print()
print("[Shipped Default] prompts/style-guide.json exists and is valid")

t("style-guide.json exists",
  (PROMPTS_DIR / "style-guide.json").exists(),
  "File not found at prompts/style-guide.json")

# Parse JSON
try:
    style_guide = json.loads(STYLE_GUIDE_JSON)
    t("style-guide.json is valid JSON",
      True,
      "")
except json.JSONDecodeError as e:
    t("style-guide.json is valid JSON",
      False,
      f"JSON parse error: {e}")
    style_guide = {}

t("style-guide.json has 'version' field",
  "version" in style_guide,
  "Missing required 'version' field")

t("style-guide.json version is 1",
  style_guide.get("version") == 1,
  f"version should be 1, got {style_guide.get('version')}")

t("style-guide.json has 'source' field = 'default'",
  style_guide.get("source") == "default",
  f"source should be 'default', got {style_guide.get('source')}")

t("style-guide.json 'generatedAt' is null",
  style_guide.get("generatedAt") is None,
  f"generatedAt should be null for default file, got {style_guide.get('generatedAt')}")

t("style-guide.json 'scope' is null",
  style_guide.get("scope") is None,
  f"scope should be null for default file, got {style_guide.get('scope')}")

t("style-guide.json has 'examples' array",
  isinstance(style_guide.get("examples"), list),
  "examples should be a list")

t("style-guide.json examples is non-empty",
  len(style_guide.get("examples", [])) > 0,
  "examples array is empty")

t("style-guide.json has 'toneNotes' array",
  isinstance(style_guide.get("toneNotes"), list),
  "toneNotes should be a list")

t("style-guide.json toneNotes is non-empty",
  len(style_guide.get("toneNotes", [])) > 0,
  "toneNotes array is empty")

# Check for "instacart" (employer name) in examples or toneNotes
examples_text = " ".join(style_guide.get("examples", []))
toneNotes_text = " ".join(style_guide.get("toneNotes", []))
t("no 'instacart' in examples",
  "instacart" not in examples_text.lower(),
  "Found employer name 'instacart' in examples — must be genericized")

t("no 'instacart' in toneNotes",
  "instacart" not in toneNotes_text.lower(),
  "Found employer name 'instacart' in toneNotes — must be genericized")

# ============================================================================
# STYLE-GUIDE.JSON.TEMPLATE
# ============================================================================
print()
print("[Template] prompts/style-guide.json.template exists and is valid")

t("style-guide.json.template exists",
  (PROMPTS_DIR / "style-guide.json.template").exists(),
  "File not found at prompts/style-guide.json.template")

# Parse template JSON
try:
    template = json.loads(STYLE_GUIDE_TEMPLATE)
    t("style-guide.json.template is valid JSON",
      True,
      "")
except json.JSONDecodeError as e:
    t("style-guide.json.template is valid JSON",
      False,
      f"JSON parse error: {e}")
    template = {}

t("template has '_instructions' field",
  "_instructions" in template,
  "Template should have an '_instructions' field documenting the schema")

t("_instructions is non-empty",
  bool(template.get("_instructions", "").strip()),
  "_instructions field should contain documentation")

t("_instructions mentions /generate-style-guide",
  "/generate-style-guide" in template.get("_instructions", ""),
  "_instructions should mention /generate-style-guide as an alternative")

t("_instructions mentions manual copying",
  bool(re.search(r"manual|copy|template", template.get("_instructions", ""), re.IGNORECASE)),
  "_instructions should mention manual copying or editing")

t("template source is 'manual'",
  template.get("source") == "manual",
  "Template source should be 'manual'")

# ============================================================================
# PR-COMMENT-GUIDE.MD TONE SECTION
# ============================================================================
print()
print("[Tone Section] pr-comment-guide.md cascade loading instructions")

t("pr-comment-guide.md Tone section mentions ~/.claude/style-guide.json",
  "~/.claude/style-guide.json" in PR_COMMENT_GUIDE,
  "Tone section should mention the personal style-guide.json path")

t("pr-comment-guide.md mentions ~/.claude/prompts/style-guide.json",
  "~/.claude/prompts/style-guide.json" in PR_COMMENT_GUIDE,
  "Tone section should mention the default cascade path")

t("pr-comment-guide.md 'Tone' section still has 'Rules of thumb'",
  "Rules of thumb" in PR_COMMENT_GUIDE,
  "The Tone section should retain the 'Rules of thumb' prose")

t("pr-comment-guide.md still has CRITICAL exception sentence",
  bool(re.search(r"CRITICAL.*one sentence|one sentence.*CRITICAL", PR_COMMENT_GUIDE, re.IGNORECASE)),
  "The CRITICAL exception clause should remain in Tone section")

t("pr-comment-guide.md Prompt Injection Guard section is intact",
  "Prompt Injection Guard" in PR_COMMENT_GUIDE,
  "The Prompt Injection Guard section outside Tone should be unchanged")

t("no hardcoded examples in Tone section",
  not bool(re.search(
    r'- "This has no test coverage\? Can we add coverage for it\?"',
    PR_COMMENT_GUIDE
  )),
  "Hardcoded example bullets should be removed and replaced with cascade loading instructions")

# ============================================================================
# GENERATE-STYLE-GUIDE.MD COMMAND
# ============================================================================
print()
print("[Command] commands/generate-style-guide.md frontmatter and content")

t("generate-style-guide.md exists",
  (COMMANDS_DIR / "generate-style-guide.md").exists(),
  "File not found at commands/generate-style-guide.md")

# Check frontmatter
t("generate-style-guide.md has YAML frontmatter",
  GENERATE_STYLE_GUIDE.startswith("---"),
  "Command should start with YAML frontmatter (---)")

t("frontmatter has 'description' field",
  bool(re.search(r"^description:", GENERATE_STYLE_GUIDE, re.MULTILINE)),
  "Frontmatter must include 'description' field")

t("frontmatter has 'allowed-tools' field",
  bool(re.search(r"^allowed-tools:", GENERATE_STYLE_GUIDE, re.MULTILINE)),
  "Frontmatter must include 'allowed-tools' field")

# Extract allowed-tools
allowed_tools_match = re.search(r"^allowed-tools:\s*(.+)$", GENERATE_STYLE_GUIDE, re.MULTILINE)
allowed_tools = allowed_tools_match.group(1) if allowed_tools_match else ""

# Check that allowed-tools does not grant unscoped destructive capabilities
# Reject: Bash(gh:*), Bash(gh api:*), Bash(gh pr:*) without read-specific scope
unscoped_destructive = [
    r"Bash\(gh:\*\)",           # Unscoped gh
    r"Bash\(gh\s+api:\*\)",      # All gh api calls (includes destructive mutations)
    r"Bash\(gh\s+pr:\*\)",       # All gh pr calls (includes merge, close, etc.)
]
has_unscoped_destructive = any(re.search(pattern, allowed_tools) for pattern in unscoped_destructive)
t("allowed-tools restricts gh to read-only subcommands",
  not has_unscoped_destructive,
  "Frontmatter should not grant unscoped Bash(gh api:*), Bash(gh pr:*), or Bash(gh:*) — only specific read operations like Bash(gh api user*), Bash(gh search prs*), Bash(gh api graphql*)")

# Additionally verify command body doesn't contain destructive operations
destructive_patterns = [
    r"gh\s+pr\s+merge",
    r"gh\s+api\s+graphql\s+-f\s+query=.*mutation",
    r"--method\s+DELETE",
    r"--method\s+PATCH",
    r"--method\s+POST"
]
has_destructive = any(re.search(pattern, GENERATE_STYLE_GUIDE, re.IGNORECASE) for pattern in destructive_patterns)
t("command body contains no destructive operations",
  not has_destructive,
  "Command should not include destructive operations like 'gh pr merge' or 'gh api graphql' mutations")

# Check for confirmation step
t("command body mentions confirmation/confirm step",
  bool(re.search(r"confirm|confirmation", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should describe a confirmation gate before writing")

# Check for scope bounding
t("command bounds scope (not all of GitHub)",
  bool(re.search(r"never crawl|bound|scope|current repo", GENERATE_STYLE_GUIDE, re.IGNORECASE)),
  "Command should explicitly state it never crawls all of GitHub")

# ============================================================================
# PEER-MERGE.MD EXCLUSION NOTE
# ============================================================================
print()
print("[Peer Merge] style-guide.json cascade exclusion note")

t("peer-merge.md 'Apply Peer Tone' section mentions intentional exclusion",
  bool(re.search(r"style-guide\.json.*intentional|intentional.*style-guide|not wired|deliberately.*not|omission.*deliberate", PEER_MERGE, re.IGNORECASE)),
  "peer-merge.md should note that style-guide.json cascade is intentionally excluded (fast path)")

# ============================================================================
# ADR-0017 AND README INDEX
# ============================================================================
print()
print("[ADR] docs/adr/0017-style-guide-cascade.md exists and is well-formed")

t("ADR-0017 exists",
  (DOCS_DIR / "0017-style-guide-cascade.md").exists(),
  "File not found at docs/adr/0017-style-guide-cascade.md")

t("ADR-0017 title matches expected format",
  bool(re.search(r"^# ADR-0017", ADR_0017)),
  "ADR should start with '# ADR-0017: ...'")

t("ADR-0017 has **Status:** line",
  bool(re.search(r"\*\*Status:\*\*", ADR_0017)),
  "ADR must include a **Status:** line")

t("ADR-0017 has ## Context section",
  "## Context" in ADR_0017,
  "ADR must include a ## Context section")

t("ADR-0017 has ## Decision section",
  "## Decision" in ADR_0017,
  "ADR must include a ## Decision section")

t("ADR-0017 has ## Consequences section",
  "## Consequences" in ADR_0017,
  "ADR must include a ## Consequences section")

t("ADR-0017 references ADR-0005",
  bool(re.search(r"ADR-0005|0005-three-layer-context-cascade", ADR_0017, re.IGNORECASE)),
  "ADR-0017 should reference ADR-0005 (reviewer-context cascade)")

t("ADR-0017 references ADR-0007",
  bool(re.search(r"ADR-0007|0007-triage-and-decision-memory", ADR_0017, re.IGNORECASE)),
  "ADR-0017 should reference ADR-0007 (decision memory and confirm-before-write precedent)")

# Check ADR README entry
print()
print("[ADR Index] docs/adr/README.md includes ADR-0017")

t("ADR README has ADR-0017 entry",
  bool(re.search(r"ADR-0017.*style-guide|style-guide.*ADR-0017", ADR_README, re.IGNORECASE)),
  "docs/adr/README.md index must include an entry for ADR-0017")

# ============================================================================
# CLAUDE.MD UPDATES
# ============================================================================
print()
print("[CLAUDE.md] command list and new section")

t("CLAUDE.md mentions /generate-style-guide command",
  "/generate-style-guide" in CLAUDE_MD,
  "CLAUDE.md Commands section must list /generate-style-guide")

t("CLAUDE.md has 'PR comment tone' section",
  bool(re.search(r"## PR comment tone|## .*style-guide", CLAUDE_MD, re.IGNORECASE)),
  "CLAUDE.md should have a dedicated section about PR comment tone / style-guide.json")

t("CLAUDE.md mentions style-guide.json",
  "style-guide.json" in CLAUDE_MD,
  "CLAUDE.md should mention style-guide.json in the new section")

# ============================================================================
# SETUP-LOCAL.MD UPDATES
# ============================================================================
print()
print("[Setup] commands/setup-local.md documents style-guide.json symlink")

t("setup-local.md mentions style-guide.json",
  "style-guide.json" in SETUP_LOCAL,
  "setup-local.md should document that prompts/style-guide.json is symlinked")

t("setup-local.md notes personal style-guide is never auto-created",
  bool(re.search(r"style-guide\.json.*never|never.*style-guide", SETUP_LOCAL, re.IGNORECASE | re.DOTALL)),
  "setup-local.md should clarify that the personal ~/.claude/style-guide.json is never auto-seeded")

h.summarize_and_exit()
