#!/usr/bin/env python3
"""Simple inline test to verify implementation against invariants."""

from pathlib import Path
import re

REPO = Path("/Users/stephenkiers/Repositories/claude-helpers-open-source/.claude/worktrees/1-trim-reviewer-persona-scaffolding")
REVIEWERS = REPO / "reviewers"
PROMPTS = REPO / "prompts"

print("Quick Invariant Check")
print("=" * 60)

# 1. No investigationAreas
inv1 = sum(1 for f in REVIEWERS.glob("*.yaml") if "investigationAreas" in f.read_text())
print(f"1. investigationAreas occurrences: {inv1} (expect 0) - {'PASS' if inv1 == 0 else 'FAIL'}")

# 2. OUTPUT blocks only in carve-outs
carve_outs = {"code-rot-cody.yaml", "contrarian-carl.yaml", "consistency-checker.yaml"}
has_output = {f.name for f in REVIEWERS.glob("*.yaml") if "OUTPUT" in f.read_text()}
expected_output = carve_outs
unexpected_output = has_output - carve_outs
inv2 = len(unexpected_output) == 0 and carve_outs.issubset(has_output)
print(f"2. OUTPUT in {has_output}, unexpected: {unexpected_output} - {'PASS' if inv2 else 'FAIL'}")

# 3. YAML valid (no tabs)
tabs = sum(1 for f in REVIEWERS.glob("*.yaml") if "\t" in f.read_text())
inv3 = tabs == 0
print(f"3. Files with tabs: {tabs} (expect 0) - {'PASS' if inv3 else 'FAIL'}")

# 4. Index completeness
yaml_count = len([f for f in REVIEWERS.glob("*.yaml") if f.name != "index.yaml"])
index_content = (REVIEWERS / "index.yaml").read_text()
index_entries = set(m.group(1) for m in re.finditer(r'file:\s+([a-z\-]+)\.yaml', index_content))
yaml_names = {f.stem for f in REVIEWERS.glob("*.yaml") if f.name != "index.yaml"}
inv4 = yaml_count == 27 and len(index_entries) == 27 and yaml_names == index_entries
print(f"4. Files: {yaml_count}, Index entries: {len(index_entries)}, Match: {yaml_names == index_entries} - {'PASS' if inv4 else 'FAIL'}")

# 4b. Required personas
required = {"ariadne", "consistency-checker", "contrarian-carl", "editor-audience", "editor-cadence", "editor-signal"}
missing = required - yaml_names
inv4b = len(missing) == 0
print(f"   Required personas: {required}, Missing: {missing} - {'PASS' if inv4b else 'FAIL'}")

# 5. Core fields preserved
missing_fields = []
for f in REVIEWERS.glob("*.yaml"):
    if f.name in {"index.yaml"}:
        continue
    content = f.read_text()
    if not re.search(r"^summary:", content, re.MULTILINE):
        missing_fields.append(f"{f.name}: no summary")
    if not re.search(r"^principles:", content, re.MULTILINE):
        missing_fields.append(f"{f.name}: no principles")
    if not re.search(r"^\s+prompt:", content, re.MULTILINE):
        missing_fields.append(f"{f.name}: no codeReview.prompt")
inv5 = len(missing_fields) == 0
print(f"5. Missing core fields: {len(missing_fields)} - {'PASS' if inv5 else 'FAIL'}")
if missing_fields:
    for issue in missing_fields[:3]:
        print(f"   - {issue}")

# 6. expert-framework.md has Load Project Context
ef = PROMPTS / "expert-framework.md"
inv6 = ef.exists() and "Load Project Context" in ef.read_text()
print(f"6. expert-framework.md Load Project Context: {inv6} - {'PASS' if inv6 else 'FAIL'}")

# 7. reviewers/README.md docs
readme = REVIEWERS / "README.md"
has_readme = readme.exists()
has_schema = has_readme and re.search(r"schema|structure|format", readme.read_text(), re.I)
has_guide = has_readme and re.search(r"(adding|new|creating).*reviewer", readme.read_text(), re.I)
inv7 = has_readme and has_schema and has_guide
print(f"7. README.md (exists={has_readme}, schema={bool(has_schema)}, guide={bool(has_guide)}): - {'PASS' if inv7 else 'FAIL'}")

print()
print("=" * 60)
all_pass = all([inv1 == 0, inv2, inv3, inv4, inv4b, inv5, inv6, inv7])
print(f"Overall: {'ALL PASS ✓' if all_pass else 'SOME FAIL ✗'}")
