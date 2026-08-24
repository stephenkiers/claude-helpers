#!/usr/bin/env python3
"""
Test suite for project context fallback (issue #67).
Tests that all sites checking for .claude/project.yaml now also accept .claude/project-context.yaml.

Run with: python3 tests/test_project_context_fallback.py
"""

import re

from _test_harness import REPO_ROOT, Harness

PROMPTS_DIR = REPO_ROOT / "prompts"
COMMANDS_DIR = REPO_ROOT / "commands"
REVIEWERS_DIR = REPO_ROOT / "reviewers"
DOCS_DIR = REPO_ROOT / "docs"

h = Harness("PROJECT CONTEXT FALLBACK TEST SUITE")
test_result = h.test_result

# A paired site names ".claude/project.yaml" before ".claude/project-context.yaml", close enough
# together that they're clearly describing the same fallback, not two unrelated mentions. This
# catches both a reversed reading order and a mention of one filename with no partner nearby —
# things a plain "file contains substring X" check can't tell apart from a correct pairing.
FALLBACK_PAIR_RE = re.compile(
    r"\.claude/project\.yaml.{0,160}?\.claude/project-context\.yaml",
    re.DOTALL,
)


def assert_fallback_pairs(file_label, content, exists):
    """Every '.claude/project.yaml' mention in content must pair with a nearby, later
    '.claude/project-context.yaml' mention, and vice versa: no orphan mention of either."""
    project_count = content.count(".claude/project.yaml") if exists else 0
    context_count = content.count(".claude/project-context.yaml") if exists else 0
    pair_count = len(FALLBACK_PAIR_RE.findall(content)) if exists else 0

    ok = exists and pair_count > 0 and pair_count == project_count == context_count
    detail = (
        "File not found"
        if not exists
        else ""
        if ok
        else f"project.yaml mentions={project_count}, project-context.yaml mentions={context_count}, "
        f"ordered pairs={pair_count} (expected all three equal and > 0)"
    )

    test_result(
        f"{file_label} pairs '.claude/project.yaml' with '.claude/project-context.yaml' in order",
        ok,
        detail,
    )


# ============================================================================
# TEST 1: expert-framework.md contains both filenames, paired
# ============================================================================
print("[Test 1] expert-framework.md contains both project.yaml and project-context.yaml")

expert_framework_file = PROMPTS_DIR / "expert-framework.md"
framework_content = ""
framework_exists = expert_framework_file.exists()

if framework_exists:
    framework_content = expert_framework_file.read_text()

test_result(
    "expert-framework.md exists",
    framework_exists,
    "File not found" if not framework_exists else ""
)

assert_fallback_pairs("expert-framework.md", framework_content, framework_exists)

# ============================================================================
# TEST 2: 15 command/prompt files contain both filenames, paired
# ============================================================================
print()
print("[Test 2] Command/prompt files contain both project.yaml and project-context.yaml")

target_files = [
    ("commands/expert-review.md", COMMANDS_DIR / "expert-review.md"),
    ("prompts/triage.md", PROMPTS_DIR / "triage.md"),
    ("commands/expert-plan.md", COMMANDS_DIR / "expert-plan.md"),
    ("commands/shipit.md", COMMANDS_DIR / "shipit.md"),
    ("commands/expert-pre-mortem.md", COMMANDS_DIR / "expert-pre-mortem.md"),
    ("commands/expert-harden-types.md", COMMANDS_DIR / "expert-harden-types.md"),
    ("commands/expert-harden-contracts.md", COMMANDS_DIR / "expert-harden-contracts.md"),
    ("commands/expert-harden-tests.md", COMMANDS_DIR / "expert-harden-tests.md"),
    ("commands/expert-review-coworker.md", COMMANDS_DIR / "expert-review-coworker.md"),
    ("commands/implement-with-haiku.md", COMMANDS_DIR / "implement-with-haiku.md"),
    ("CLAUDE.md", REPO_ROOT / "CLAUDE.md"),
    ("README.md", REPO_ROOT / "README.md"),
    ("docs/adr/0005-three-layer-context-cascade.md", DOCS_DIR / "adr" / "0005-three-layer-context-cascade.md"),
    ("prompts/expert-review-panel.md", PROMPTS_DIR / "expert-review-panel.md"),
    ("reviewers/README.md", REVIEWERS_DIR / "README.md"),
]

for file_label, file_path in target_files:
    content = ""
    exists = file_path.exists()

    if exists:
        content = file_path.read_text()

    test_result(
        f"{file_label} exists",
        exists,
        "File not found" if not exists else ""
    )

    assert_fallback_pairs(file_label, content, exists)

# ============================================================================
# TEST 3: project.yaml.template contains project-context.yaml
# ============================================================================
print()
print("[Test 3] project.yaml.template contains project-context.yaml")

template_file = PROMPTS_DIR / "project.yaml.template"
template_content = ""
template_exists = template_file.exists()

if template_exists:
    template_content = template_file.read_text()

test_result(
    "project.yaml.template exists",
    template_exists,
    "File not found" if not template_exists else ""
)

test_result(
    "project.yaml.template contains '.claude/project-context.yaml'",
    ".claude/project-context.yaml" in template_content,
    "Substring not found" if template_exists else "File not found"
)

# ============================================================================
# TEST 4: contract-chris.yaml does NOT contain project.yaml
# ============================================================================
print()
print("[Test 4] contract-chris.yaml does NOT contain .claude/project.yaml (negative check)")

contract_chris_file = REVIEWERS_DIR / "contract-chris.yaml"
contract_chris_content = ""
contract_chris_exists = contract_chris_file.exists()

if contract_chris_exists:
    contract_chris_content = contract_chris_file.read_text()

test_result(
    "contract-chris.yaml exists",
    contract_chris_exists,
    "File not found" if not contract_chris_exists else ""
)

test_result(
    "contract-chris.yaml does NOT contain '.claude/project.yaml'",
    ".claude/project.yaml" not in contract_chris_content,
    "Substring was found (but should not be present)" if contract_chris_exists and ".claude/project.yaml" in contract_chris_content else ""
)

h.summarize_and_exit()
