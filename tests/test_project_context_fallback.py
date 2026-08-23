#!/usr/bin/env python3
"""
Test suite for project context fallback (issue #67).
Tests that all sites checking for .claude/project.yaml now also accept .claude/project-context.yaml.

Run with: python3 tests/test_project_context_fallback.py
"""

from _test_harness import REPO_ROOT, Harness

PROMPTS_DIR = REPO_ROOT / "prompts"
COMMANDS_DIR = REPO_ROOT / "commands"
REVIEWERS_DIR = REPO_ROOT / "reviewers"

h = Harness("PROJECT CONTEXT FALLBACK TEST SUITE")
test_result = h.test_result

# ============================================================================
# TEST 1: expert-framework.md contains both filenames
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

test_result(
    "expert-framework.md contains '.claude/project.yaml'",
    ".claude/project.yaml" in framework_content if framework_exists else False,
    "Substring not found" if framework_exists else ""
)

test_result(
    "expert-framework.md contains '.claude/project-context.yaml'",
    ".claude/project-context.yaml" in framework_content if framework_exists else False,
    "Substring not found" if framework_exists else ""
)

# ============================================================================
# TEST 2: 10 command/prompt files contain both filenames
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

    test_result(
        f"{file_label} contains '.claude/project.yaml'",
        ".claude/project.yaml" in content if exists else False,
        "Substring not found" if exists else ""
    )

    test_result(
        f"{file_label} contains '.claude/project-context.yaml'",
        ".claude/project-context.yaml" in content if exists else False,
        "Substring not found" if exists else ""
    )

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
    ".claude/project-context.yaml" in template_content if template_exists else False,
    "Substring not found" if template_exists else ""
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
    ".claude/project.yaml" not in contract_chris_content if contract_chris_exists else True,
    "Substring was found (but should not be present)" if contract_chris_exists and ".claude/project.yaml" in contract_chris_content else ""
)

h.summarize_and_exit()
