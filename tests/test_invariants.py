#!/usr/bin/env python3
"""
Test suite for reviewer-persona scaffolding trim.
Tests invariants 1-7 from the plan without using the yaml module.

Run with: python3 tests/test_invariants.py
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path("/Users/stephenkiers/Repositories/claude-helpers-open-source/.claude/worktrees/1-trim-reviewer-persona-scaffolding")
REVIEWERS_DIR = REPO_ROOT / "reviewers"
PROMPTS_DIR = REPO_ROOT / "prompts"

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

print("=" * 60)
print("REVIEWER-PERSONA SCAFFOLDING TEST SUITE")
print("=" * 60)
print()

# ============================================================================
# INVARIANT 1: No dead field investigationAreas
# ============================================================================
print("[Invariant 1] No dead field 'codeReview.investigationAreas'")

investigation_areas_files = []
for file in REVIEWERS_DIR.glob("*.yaml"):
    content = file.read_text()
    if "investigationAreas" in content:
        investigation_areas_files.append(file.name)

test_result(
    "investigationAreas field removed from all files",
    len(investigation_areas_files) == 0,
    f"Found in {len(investigation_areas_files)} files: {investigation_areas_files}"
)

# ============================================================================
# INVARIANT 2: OUTPUT FORMAT only in carve-out reviewers
# ============================================================================
print()
print("[Invariant 2] OUTPUT FORMAT blocks only in carve-out reviewers")

carve_outs = {
    "code-rot-cody.yaml",
    "contrarian-carl.yaml",
    "consistency-checker.yaml"
}
special_cases = carve_outs | {"sam-system.yaml"}

# Check that the three carve-out files DO have OUTPUT FORMAT
carve_out_violations = []
for file_name in carve_outs:
    file_path = REVIEWERS_DIR / file_name
    if file_path.exists():
        content = file_path.read_text()
        has_output = "OUTPUT" in content
        if not has_output:
            carve_out_violations.append(f"{file_name} (missing OUTPUT)")
    else:
        carve_out_violations.append(f"{file_name} (file not found)")

test_result(
    "Carve-out reviewers have OUTPUT FORMAT",
    len(carve_out_violations) == 0,
    ", ".join(carve_out_violations) if carve_out_violations else ""
)

# Check that sam-system.yaml has NO OUTPUT FORMAT
sam_system = REVIEWERS_DIR / "sam-system.yaml"
sam_has_output = False
if sam_system.exists():
    content = sam_system.read_text()
    sam_has_output = "OUTPUT" in content

test_result(
    "sam-system.yaml has no OUTPUT FORMAT",
    not sam_has_output,
    "Found OUTPUT in sam-system.yaml" if sam_has_output else ""
)

# Check all other reviewers don't have OUTPUT FORMAT
other_output_files = []
for file in REVIEWERS_DIR.glob("*.yaml"):
    if file.name in special_cases | {"index.yaml", "README.md"}:
        continue
    content = file.read_text()
    if "OUTPUT" in content:
        other_output_files.append(file.name)

test_result(
    "No OUTPUT FORMAT in non-carve-out reviewers",
    len(other_output_files) == 0,
    f"Found in {len(other_output_files)}: {other_output_files}"
)

# ============================================================================
# INVARIANT 3: YAML validity
# ============================================================================
print()
print("[Invariant 3] YAML validity of all reviewer files")

yaml_issues = []
for file in REVIEWERS_DIR.glob("*.yaml"):
    if file.name == "index.yaml":
        continue

    content = file.read_text()

    # Check for tabs in indentation (YAML uses spaces only)
    if "\t" in content:
        yaml_issues.append(f"{file.name}: contains tabs")

test_result(
    "All reviewer YAML files are valid",
    len(yaml_issues) == 0,
    "; ".join(yaml_issues) if yaml_issues else ""
)

# ============================================================================
# INVARIANT 4: Index completeness (27 reviewers)
# ============================================================================
print()
print("[Invariant 4] Index completeness (27 reviewers + 6 required personas)")

# Count YAML files (excluding index.yaml)
yaml_files = [f.name for f in REVIEWERS_DIR.glob("*.yaml") if f.name != "index.yaml"]
yaml_file_count = len(yaml_files)

test_result(
    "Exactly 27 reviewer YAML files",
    yaml_file_count == 27,
    f"Found {yaml_file_count} files (expected 27)"
)

# Read and parse index.yaml entries
index_file = REVIEWERS_DIR / "index.yaml"
index_entries = set()
if index_file.exists():
    index_content = index_file.read_text()
    # Extract entries: look for "file: <name>.yaml" patterns
    for match in re.finditer(r'file:\s+([a-z\-]+)\.yaml', index_content, re.MULTILINE):
        index_entries.add(match.group(1))

test_result(
    "Index contains 27 entries",
    len(index_entries) == 27,
    f"Found {len(index_entries)} entries (expected 27). Entries: {sorted(index_entries)}"
)

# Check the 6 required personas
required_personas = {
    "ariadne",
    "consistency-checker",
    "contrarian-carl",
    "editor-audience",
    "editor-cadence",
    "editor-signal"
}
missing_personas = required_personas - {f.stem for f in REVIEWERS_DIR.glob("*.yaml")}

test_result(
    "All 6 required personas present",
    len(missing_personas) == 0,
    f"Missing: {missing_personas}" if missing_personas else ""
)

# Check bidirectional mapping (comparing file names to index entries)
yaml_basenames = {f.stem for f in REVIEWERS_DIR.glob("*.yaml") if f.name != "index.yaml"}
files_not_in_index = yaml_basenames - index_entries
entries_not_in_files = index_entries - yaml_basenames

# Debug: show what we found
# print(f"Debug: Found {len(yaml_basenames)} yaml files, {len(index_entries)} index entries")

test_result(
    "All files in index",
    len(files_not_in_index) == 0,
    f"Missing from index: {files_not_in_index}" if files_not_in_index else ""
)

test_result(
    "No orphan index entries",
    len(entries_not_in_files) == 0,
    f"Orphan entries: {entries_not_in_files}" if entries_not_in_files else ""
)

# ============================================================================
# INVARIANT 5: Preserved persona content (regression guard)
# ============================================================================
print()
print("[Invariant 5] Core persona fields preserved (no empty trim)")

missing_fields = {}
for file in REVIEWERS_DIR.glob("*.yaml"):
    if file.name in {"index.yaml", "README.md"}:
        continue

    content = file.read_text()
    issues = []

    # Check for summary field (must exist and have content).
    # `summary:` is a YAML parent key — its value is nested, indented children
    # on following lines (character:/voice:), so the key line itself is bare.
    # "Empty" means the key has no indented child block beneath it.
    summary_match = re.search(r"^summary:[^\n]*\n", content, re.MULTILINE)
    if not summary_match:
        issues.append("missing summary")
    else:
        rest = content[summary_match.end():]
        # First non-blank line after `summary:` must be indented (a child).
        has_child = re.match(r"(?:[ \t]*\n)*[ \t]+\S", rest)
        if not has_child:
            issues.append("empty summary")

    # Check for principles field (must exist and have content)
    if not re.search(r"^principles:", content, re.MULTILINE):
        issues.append("missing principles")

    # Check the persona carries a review body. Two shapes are supported:
    #   - code-review personas: codeReview.prompt (an indented `prompt:`)
    #   - editor personas (editor-*): editReview with focusAreas
    has_code_prompt = bool(re.search(r"^\s+prompt:", content, re.MULTILINE))
    has_edit_review = bool(re.search(r"^editReview:", content, re.MULTILINE))
    if not (has_code_prompt or has_edit_review):
        issues.append("missing review body (codeReview.prompt or editReview)")

    if issues:
        missing_fields[file.name] = issues

test_result(
    "Core persona fields present and non-empty",
    len(missing_fields) == 0,
    f"Issues in {len(missing_fields)} files"
)

if missing_fields:
    for fname, issues in sorted(missing_fields.items()):
        print(f"    {fname}: {', '.join(issues)}")

# ============================================================================
# INVARIANT 6: Project-context centralization
# ============================================================================
print()
print("[Invariant 6] Project-context centralization in expert-framework.md")

expert_framework_file = PROMPTS_DIR / "expert-framework.md"
has_file = expert_framework_file.exists()
has_section = False

if has_file:
    content = expert_framework_file.read_text()
    has_section = "Load Project Context" in content

test_result(
    "expert-framework.md exists",
    has_file,
    "File not found" if not has_file else ""
)

test_result(
    "expert-framework.md has 'Load Project Context' section",
    has_section,
    "Section not found" if has_file and not has_section else ""
)

# ============================================================================
# INVARIANT 7: Novelty guardrail docs (reviewers/README.md)
# ============================================================================
print()
print("[Invariant 7] Novelty guardrail docs in reviewers/README.md")

reviewers_readme = REVIEWERS_DIR / "README.md"
readme_exists = reviewers_readme.exists()
has_schema_doc = False
has_checklist = False

if readme_exists:
    content = reviewers_readme.read_text()
    has_schema_doc = bool(re.search(r"(schema|structure|format)", content, re.IGNORECASE))
    has_checklist = bool(re.search(r"(adding|new).*reviewer", content, re.IGNORECASE))

test_result(
    "reviewers/README.md exists",
    readme_exists,
    "File not found" if not readme_exists else ""
)

test_result(
    "reviewers/README.md documents persona schemas",
    has_schema_doc,
    "Schema documentation not found" if readme_exists and not has_schema_doc else ""
)

test_result(
    "reviewers/README.md has guidance on creating/adding reviewers",
    has_checklist,
    "Checklist/guidance not found" if readme_exists and not has_checklist else ""
)

# ============================================================================
# Summary
# ============================================================================
print()
print("=" * 60)
print("TEST SUMMARY")
print("=" * 60)
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
