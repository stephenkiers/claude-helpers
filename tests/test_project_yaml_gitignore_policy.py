#!/usr/bin/env python3
"""
Test suite for .claude/project.yaml and *-local.yaml gitignore policy (issue #165).

Plan: Lock the policy that .claude/project.yaml and .claude/reviewers/{name}-local.yaml
are local, gitignored state — with one deliberate exception, this repo's own north-star-nick-local.yaml.

Run with: python3 tests/test_project_yaml_gitignore_policy.py

IMPORTANT — escape hatch: if a new tracked .claude/ file is needed (not a local override),
add it to the exception list in this test, and justify it alongside north-star-nick-local.yaml
in the ADR-0005 amendment. The forbidden-set form catches policy violations without blocking
legitimate additions.
"""

import subprocess
from pathlib import Path
from _test_harness import REPO_ROOT, Harness

COMMANDS_DIR = REPO_ROOT / "commands"
PROMPTS_DIR = REPO_ROOT / "prompts"
REVIEWERS_DIR = REPO_ROOT / "reviewers"
DOCS_DIR = REPO_ROOT / "docs/adr"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CLAUDE_DIR = REPO_ROOT / ".claude"

if __name__ == "__main__":
    h = Harness("PROJECT.YAML / LOCAL OVERRIDE GITIGNORE POLICY TEST SUITE")
    test_result = h.test_result

    # ============================================================================
    # SECTION 1: .gitignore exists and has the right rules
    # ============================================================================
    print("[Section 1] .claude/.gitignore structure")

    gitignore_file = CLAUDE_DIR / ".gitignore"
    gitignore_content = gitignore_file.read_text() if gitignore_file.exists() else ""

    test_result(
        ".claude/.gitignore exists",
        gitignore_file.exists(),
        "File not found"
    )

    test_result(
        "project.yaml is in .gitignore",
        "project.yaml" in gitignore_content,
        "Found no project.yaml entry in .gitignore"
    )

    test_result(
        "reviewers/*-local.yaml is in .gitignore",
        "reviewers/*-local.yaml" in gitignore_content,
        "Found no reviewers/*-local.yaml entry in .gitignore"
    )

    # Negation pattern should appear AFTER the pattern it negates
    project_idx = gitignore_content.find("project.yaml")
    local_yaml_idx = gitignore_content.find("reviewers/*-local.yaml")
    negation_idx = gitignore_content.find("!reviewers/north-star-nick-local.yaml")

    test_result(
        "north-star-nick-local.yaml negation is present",
        negation_idx >= 0,
        "Found no negation pattern for north-star-nick-local.yaml"
    )

    test_result(
        "Negation pattern appears AFTER the pattern it negates",
        negation_idx > local_yaml_idx,
        "Negation pattern must appear after the pattern it negates"
    )

    print()

    # ============================================================================
    # SECTION 2: git ls-files enforcement (forbidden-set check, not allowlist)
    # ============================================================================
    print("[Section 2] Git tracked files (forbidden-set enforcement)")

    try:
        result = subprocess.run(
            ["git", "ls-files", ".claude"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10
        )
        tracked_files = result.stdout.strip().split("\n") if result.stdout.strip() else []
    except Exception as e:
        test_result(
            "git ls-files .claude command executed",
            False,
            f"Failed to run git ls-files: {e}"
        )
        tracked_files = []

    # Forbidden: any project.yaml
    project_yaml_tracked = any("project.yaml" in f for f in tracked_files)
    test_result(
        ".claude/project.yaml is NOT tracked in git",
        not project_yaml_tracked,
        "VIOLATION: Found .claude/project.yaml in git history. "
        "Fix: git rm --cached .claude/project.yaml, verify with git check-ignore --no-index. "
        "Policy: docs/adr/0005-three-layer-context-cascade.md (Amendment — gitignore section). "
        "See reviewers/README.md for full retrofit guidance. "
        "To add a deliberate exception: document it in ADR-0005 amendment and update this test."
    )

    # Forbidden: any *-local.yaml EXCEPT north-star-nick-local.yaml
    forbidden_local_files = [
        f for f in tracked_files
        if "-local.yaml" in f and "north-star-nick-local.yaml" not in f
    ]
    test_result(
        "No tracked .claude/reviewers/*-local.yaml except north-star-nick-local.yaml",
        len(forbidden_local_files) == 0,
        f"VIOLATION: Found tracked local overrides: {', '.join(forbidden_local_files)}. "
        "Policy: docs/adr/0005-three-layer-context-cascade.md (Amendment — gitignore section). "
        "Fix: git rm --cached {file}, verify with git check-ignore --no-index. "
        "See reviewers/README.md for full retrofit guidance. "
        "To add a deliberate exception: document it in ADR-0005 amendment and update this test."
    )

    # Deliberate exception: north-star-nick-local.yaml MUST be tracked (dogfooding)
    north_star_tracked = any("north-star-nick-local.yaml" in f for f in tracked_files)
    test_result(
        ".claude/reviewers/north-star-nick-local.yaml IS tracked (deliberate exception)",
        north_star_tracked,
        "north-star-nick-local.yaml should be tracked as the sole exception (dogfooding). "
        "Policy: docs/adr/0005-three-layer-context-cascade.md (Amendment — gitignore section)."
    )

    print()

    # ============================================================================
    # SECTION 3: ADR-0005 Amendment documents the policy
    # ============================================================================
    print("[Section 3] ADR-0005 amendment documents the policy")

    adr_0005_file = DOCS_DIR / "0005-three-layer-context-cascade.md"
    adr_0005_content = adr_0005_file.read_text() if adr_0005_file.exists() else ""

    test_result(
        "ADR-0005 exists",
        adr_0005_file.exists(),
        "File not found"
    )

    test_result(
        "ADR-0005 has amendment section mentioning gitignore",
        "Amendment — .claude/project.yaml and local overrides are gitignored" in adr_0005_content,
        "Missing amendment section on gitignore policy"
    )

    test_result(
        "ADR-0005 amendment names north-star-nick-local.yaml as the exception",
        "north-star-nick-local.yaml" in adr_0005_content and "deliberate" in adr_0005_content.lower(),
        "Amendment should identify north-star-nick-local.yaml as the deliberate exception"
    )

    test_result(
        "ADR-0005 amendment mentions git rm --cached",
        "git rm --cached" in adr_0005_content,
        "Amendment should mention the retrofit procedure"
    )

    print()

    # ============================================================================
    # SECTION 4: docs/adr/README.md cross-references the amendment
    # ============================================================================
    print("[Section 4] docs/adr/README.md index cross-reference")

    adr_readme_file = DOCS_DIR / "README.md"
    adr_readme_content = adr_readme_file.read_text() if adr_readme_file.exists() else ""

    test_result(
        "docs/adr/README.md exists",
        adr_readme_file.exists(),
        "File not found"
    )

    test_result(
        "ADR-0005 index entry mentions gitignore amendment",
        "gitignore" in adr_readme_content and "#165" in adr_readme_content,
        "Index entry for ADR-0005 should cross-reference the gitignore amendment (issue #165)"
    )

    print()

    # ============================================================================
    # SECTION 5: ADR-0009 carries the pointer amendment
    # ============================================================================
    print("[Section 5] ADR-0009 amendment on local context in PR worktrees")

    adr_0009_file = DOCS_DIR / "0009-peer-review-and-shared-panel.md"
    adr_0009_content = adr_0009_file.read_text() if adr_0009_file.exists() else ""

    test_result(
        "ADR-0009 exists",
        adr_0009_file.exists(),
        "File not found"
    )

    test_result(
        "ADR-0009 has amendment on local context files in PR worktrees",
        "Amendment — Local context files do not flow to peer worktrees" in adr_0009_content,
        "Missing amendment section on local context in PR worktrees"
    )

    test_result(
        "ADR-0009 amendment references ADR-0005's latest amendment",
        "ADR-0005" in adr_0009_content and "latest amendment" in adr_0009_content.lower(),
        "Amendment should cross-reference ADR-0005's latest amendment for policy rationale"
    )

    print()

    # ============================================================================
    # SECTION 6: reviewers/README.md documents the retrofit
    # ============================================================================
    print("[Section 6] reviewers/README.md retrofit guidance")

    reviewers_readme_file = REVIEWERS_DIR / "README.md"
    reviewers_readme_content = reviewers_readme_file.read_text() if reviewers_readme_file.exists() else ""

    test_result(
        "reviewers/README.md exists",
        reviewers_readme_file.exists(),
        "File not found"
    )

    test_result(
        "reviewers/README.md mentions git rm --cached",
        "git rm --cached" in reviewers_readme_content,
        "Should document the retrofit procedure"
    )

    test_result(
        ".claude/project.yaml ignore line documented",
        ".claude/project.yaml" in reviewers_readme_content,
        "Should document adding .claude/project.yaml to .gitignore"
    )

    test_result(
        "--no-index verification documented",
        "--no-index" in reviewers_readme_content,
        "Should document the verification command (git check-ignore --no-index)"
    )

    test_result(
        "Installed ~/.claude/ paths used for cross-references, not repo-relative",
        "~/.claude/reviewers/" in reviewers_readme_content and "~/.claude/prompts/" in reviewers_readme_content,
        "Cross-references should use installed ~/.claude/ paths, not repo-relative paths"
    )

    print()

    # ============================================================================
    # SECTION 7: commands/setup-repo.md mentions the procedure
    # ============================================================================
    print("[Section 7] commands/setup-repo.md references untrack procedure")

    setup_repo_file = COMMANDS_DIR / "setup-repo.md"
    setup_repo_content = setup_repo_file.read_text() if setup_repo_file.exists() else ""

    test_result(
        "commands/setup-repo.md exists",
        setup_repo_file.exists(),
        "File not found"
    )

    test_result(
        "setup-repo.md cites ~/.claude/reviewers/README.md for gitignore",
        "~/.claude/reviewers/README.md" in setup_repo_content,
        "Should cite the installed path, not a repo-relative path"
    )

    test_result(
        "setup-repo.md mentions git rm --cached",
        "git rm --cached" in setup_repo_content,
        "Should reference the untrack command"
    )

    print()

    # ============================================================================
    # SECTION 8: commands/setup-local.md mentions the procedure
    # ============================================================================
    print("[Section 8] commands/setup-local.md references local overrides")

    setup_local_file = COMMANDS_DIR / "setup-local.md"
    setup_local_content = setup_local_file.read_text() if setup_local_file.exists() else ""

    test_result(
        "commands/setup-local.md exists",
        setup_local_file.exists(),
        "File not found"
    )

    test_result(
        "setup-local.md mentions .claude/project.yaml gitignore",
        ".claude/project.yaml" in setup_local_content and "gitignore" in setup_local_content.lower(),
        "Should mention adding .claude/project.yaml to .gitignore"
    )

    test_result(
        "setup-local.md cites ~/.claude/reviewers/README.md for context",
        "~/.claude/reviewers/README.md" in setup_local_content,
        "Should cite the installed path, not a repo-relative path"
    )

    print()

    # ============================================================================
    # SECTION 9: prompts/project.yaml.template header mentions gitignore
    # ============================================================================
    print("[Section 9] prompts/project.yaml.template and *-local-example-*.yaml headers")

    template_file = PROMPTS_DIR / "project.yaml.template"
    template_content = template_file.read_text() if template_file.exists() else ""

    test_result(
        "prompts/project.yaml.template exists",
        template_file.exists(),
        "File not found"
    )

    test_result(
        "Template header mentions gitignore",
        "gitignore" in template_content.lower() or "gitignored" in template_content.lower(),
        "Template header should warn about gitignore"
    )

    test_result(
        "Template header mentions reviewers/README.md",
        "reviewers/README.md" in template_content or "README" in template_content,
        "Template should reference retrofit guidance"
    )

    # Check *-local-example-*.yaml files for consistent headers
    local_example_files = sorted(PROMPTS_DIR.glob("*-local-example-*.yaml"))
    test_result(
        "At least one *-local-example-*.yaml file exists",
        len(local_example_files) > 0,
        "Expected prompts/*-local-example-*.yaml files"
    )

    for example_file in local_example_files:
        example_content = example_file.read_text()
        test_result(
            f"{example_file.name} header mentions gitignore",
            "gitignore" in example_content.lower() or "gitignored" in example_content.lower(),
            f"{example_file.name} header should warn about gitignore"
        )
        test_result(
            f"{example_file.name} header mentions reviewers/README.md",
            "reviewers/README.md" in example_content,
            f"{example_file.name} should reference retrofit guidance"
        )

    print()

    # ============================================================================
    # SECTION 10: scripts/setup-pr-worktree.sh contains copy logic
    # ============================================================================
    print("[Section 10] scripts/setup-pr-worktree.sh materializes local context")

    worktree_script = SCRIPTS_DIR / "setup-pr-worktree.sh"
    worktree_content = worktree_script.read_text() if worktree_script.exists() else ""

    test_result(
        "scripts/setup-pr-worktree.sh exists",
        worktree_script.exists(),
        "File not found"
    )

    test_result(
        "Step E.5 (materializes local context) is present",
        "Step E.5" in worktree_content or "Materialize reviewer's local" in worktree_content,
        "Should document reviewer's local context materialization"
    )

    test_result(
        ".claude/project.yaml copy is present",
        '[ -f "${MAIN_WORKTREE}/.claude/project.yaml" ]' in worktree_content
        or ('project.yaml' in worktree_content and 'cp' in worktree_content),
        "Should copy reviewer's project.yaml into the PR worktree"
    )

    test_result(
        "reviewers/*-local.yaml copy is present",
        "*-local.yaml" in worktree_content and "for local_file" in worktree_content,
        "Should copy reviewer's *-local.yaml overrides into the PR worktree"
    )

    test_result(
        ".from-pr quarantine path is present",
        ".from-pr" in worktree_content,
        "PR-branch-resident files should be moved aside with .from-pr suffix"
    )

    test_result(
        "Copy failure warning is present",
        "WARNING:" in worktree_content and "Failed to copy" in worktree_content,
        "Should warn on copy failure (stderr)"
    )

    step_e5_section = worktree_content.split("Step E.5", 1)[1].split("# --- Step", 1)[0] if "Step E.5" in worktree_content else None
    test_result(
        "No copy-once guard was reintroduced",
        step_e5_section is not None and "[ ! -f" not in step_e5_section,
        "Should not have a conditional copy guard (always materialize reviewer's local context)" if step_e5_section is not None else "Step E.5 anchor not found in worktree_content — cannot verify"
    )

    print()

    # ============================================================================
    # SECTION 11: Cross-file integrity checks
    # ============================================================================
    print("[Section 11] Cross-file integrity")

    # Check that the policy is consistently referenced
    docs_mentioning_policy = 0
    if "north-star-nick-local.yaml" in gitignore_content:
        docs_mentioning_policy += 1
    if "north-star-nick-local.yaml" in adr_0005_content:
        docs_mentioning_policy += 1
    if "git rm --cached" in reviewers_readme_content:
        docs_mentioning_policy += 1

    test_result(
        "Policy is consistently documented across key files",
        docs_mentioning_policy >= 3,
        "Policy should be documented in .gitignore, ADR-0005, and reviewers/README.md"
    )

    print()
    h.summarize_and_exit()
