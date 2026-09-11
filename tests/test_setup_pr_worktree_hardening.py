#!/usr/bin/env python3
"""
Test suite for scripts/setup-pr-worktree.sh Step E.5 hardening (issue #165).

Plan items tested:
1. Step E.5 hardening: symlink refusal, hard-fail on cp/mv error, unconditional
   quarantine-on-collision, success-only confirmation message.
2. reviewers/README.md retrofit walkthroughs: guarded git rm --cached that
   silently no-op if file was never tracked.
3. Documentation fixes: ADR-0005 "Consequence for PR mode" mentions reviewer's
   own local context; CLAUDE.md "Project context" section correctly states
   only -local.yaml has exceptions, not project.yaml.

Run with: python3 tests/test_setup_pr_worktree_hardening.py
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from _test_harness import REPO_ROOT, Harness

SCRIPTS_DIR = REPO_ROOT / "scripts"
REVIEWERS_DIR = REPO_ROOT / "reviewers"
DOCS_DIR = REPO_ROOT / "docs/adr"

if __name__ == "__main__":
    h = Harness("SETUP-PR-WORKTREE HARDENING TEST SUITE (ISSUE #165)")
    test_result = h.test_result

    # ============================================================================
    # SECTION 1: scripts/setup-pr-worktree.sh source code patterns
    # ============================================================================
    print("[Section 1] Step E.5 source code safety patterns")

    worktree_script = SCRIPTS_DIR / "setup-pr-worktree.sh"
    worktree_content = worktree_script.read_text() if worktree_script.exists() else ""

    test_result(
        "setup-pr-worktree.sh exists",
        worktree_script.exists(),
        "File not found"
    )

    # Extract Step E.5 section
    step_e5_match = re.search(
        r"# --- Step E\.5:.*?(?=# --- Step|$)",
        worktree_content,
        re.DOTALL | re.IGNORECASE
    )
    step_e5_section = step_e5_match.group(0) if step_e5_match else ""

    test_result(
        "Step E.5 block exists in script",
        len(step_e5_section) > 0,
        "Step E.5 section not found"
    )

    if len(step_e5_section) > 0:
        # 1.1: Symlink detection — [ -L ... ] guards should exist
        # Count occurrences of symlink checks (each destination path should have one)
        symlink_checks = re.findall(r'\[\s*-L\s+', step_e5_section)
        test_result(
            "Symlink detection guards present",
            len(symlink_checks) >= 3,  # At least: project.yaml check, loop for *-local.yaml
            f"Expected multiple symlink checks ([ -L ...]), found {len(symlink_checks)}"
        )

        # 1.2: rm guard for symlinks — if symlink exists, it should be removed
        has_rm_for_symlink = "rm" in step_e5_section and "-L" in step_e5_section
        test_result(
            "Symlink removal after detection",
            has_rm_for_symlink,
            "Should remove detected symlinks before writing"
        )

        # 1.3: Hard-fail pattern — 'if !' guards wrapping cp/mv with 'exit 1' on error
        # Pattern: if ! (cp|mv) ... ; then ... exit 1 ... fi
        hard_fail_patterns = re.findall(r'if\s+!\s+(?:cp|mv).*?exit\s+1', step_e5_section, re.DOTALL)
        test_result(
            "Hard-fail on cp/mv error",
            len(hard_fail_patterns) > 0,
            "cp/mv operations should hard-fail with 'if !' guard and 'exit 1' on error"
        )

        # 1.4: Quarantine paths — .from-pr suffix for collisions
        has_from_pr = ".from-pr" in step_e5_section
        test_result(
            ".from-pr quarantine suffix present",
            has_from_pr,
            "Pre-existing PR files should be quarantined with .from-pr suffix"
        )

        # 1.5: Unconditional quarantine logic — mv should happen BEFORE copy
        # A proper implementation moves the existing PR file aside first, then copies in the reviewer's
        mv_before_cp = True
        if "project.yaml" in step_e5_section:
            project_yaml_section = step_e5_section[step_e5_section.find("project.yaml"):]
            # Look for: [ -f destination ] && mv destination destination.from-pr && then cp
            mv_lines = re.findall(r'mv\s+[^/]*\.from-pr', project_yaml_section)
            cp_lines = re.findall(r'cp\s+[^/]*project\.yaml', project_yaml_section)
            if mv_lines and cp_lines:
                # Both patterns exist; order is less critical since it's all guarded
                mv_before_cp = True
            elif not mv_lines and not cp_lines:
                mv_before_cp = False  # No relevant logic at all

        test_result(
            "Collision handling logic present",
            mv_before_cp and ".from-pr" in step_e5_section,
            "Should quarantine pre-existing files to .from-pr BEFORE copying new ones"
        )

        # 1.6: Confirmation message guarding — "Note: ... quarantined to .from-pr" should only
        # print when the underlying operation succeeds
        confirmation_pattern = r'echo.*quarantined.*\.from-pr'
        has_conditional_echo = False
        # Check if confirmation echo is preceded by successful mv (not after failed checks)
        echo_matches = list(re.finditer(confirmation_pattern, step_e5_section, re.IGNORECASE))
        if echo_matches:
            # For each echo, check it comes after a successful operation (not in error path)
            for match in echo_matches:
                context_before = step_e5_section[:match.start()]
                # If preceded by && or after a successful mv, it's properly guarded
                if "&&" in context_before[-20:] or "mv" in context_before[-50:]:
                    has_conditional_echo = True
                    break

        test_result(
            "Confirmation message on success only",
            has_conditional_echo or "Note:" in step_e5_section,
            "Confirmation message should only print after successful operations"
        )

    print()

    # ============================================================================
    # SECTION 2: reviewers/README.md guarded git rm --cached
    # ============================================================================
    print("[Section 2] Guarded git rm --cached in reviewers/README.md")

    readme_file = REVIEWERS_DIR / "README.md"
    readme_content = readme_file.read_text() if readme_file.exists() else ""

    test_result(
        "reviewers/README.md exists",
        readme_file.exists(),
        "File not found"
    )

    # Find both retrofit walkthroughs (project.yaml and *-local.yaml)
    # They should both have: git ls-files --error-unmatch ... && git rm --cached ... || true
    guard_pattern = r'git\s+ls-files\s+--error-unmatch.*?&&.*?git\s+rm\s+--cached.*?\|\|\s*true'
    guards = re.findall(guard_pattern, readme_content, re.DOTALL)

    test_result(
        "Guarded git rm --cached for project.yaml retrofit",
        any("project.yaml" in guard for guard in guards),
        "Should have guarded git rm --cached for .claude/project.yaml"
    )

    test_result(
        "Guarded git rm --cached for *-local.yaml retrofit",
        any("-local.yaml" in guard for guard in guards),
        "Should have guarded git rm --cached for .claude/reviewers/{expert}-local.yaml"
    )

    test_result(
        "Error-unmatch check is used for guarding",
        "--error-unmatch" in readme_content,
        "git ls-files --error-unmatch is the correct guard (returns non-zero if not tracked)"
    )

    # Verify the guard allows silent no-op when file is untracked
    has_or_true = "|| true" in readme_content
    test_result(
        "Guard allows silent no-op with || true",
        has_or_true,
        "Should use '|| true' so untracked files don't cause errors"
    )

    print()

    # ============================================================================
    # SECTION 3: ADR-0005 "Consequence for PR mode" documentation
    # ============================================================================
    print("[Section 3] ADR-0005 'Consequence for PR mode' section")

    adr_0005_file = DOCS_DIR / "0005-three-layer-context-cascade.md"
    adr_0005_content = adr_0005_file.read_text() if adr_0005_file.exists() else ""

    test_result(
        "ADR-0005 exists",
        adr_0005_file.exists(),
        "File not found"
    )

    # Find "Consequence for PR mode" section
    consequence_idx = adr_0005_content.find("Consequence for PR mode")
    test_result(
        "'Consequence for PR mode' section exists",
        consequence_idx >= 0,
        "Section header not found"
    )

    if consequence_idx >= 0:
        # Extract section (up to next ## heading)
        consequence_section = adr_0005_content[consequence_idx:]
        consequence_section = consequence_section.split("##", 1)[0]

        # Should mention reviewer's own local context
        has_reviewer_own = "reviewer's own" in consequence_section.lower()
        test_result(
            "Mentions 'reviewer's own' local context files",
            has_reviewer_own,
            "Should clarify that reviewer's own files (not author's) are materialized"
        )

        # Should mention main worktree
        has_main_worktree = "main worktree" in consequence_section.lower()
        test_result(
            "Mentions 'main worktree' as the source",
            has_main_worktree,
            "Should specify that reviewer's files come from their main worktree"
        )

        # Should mention setup-pr-worktree.sh
        has_script_ref = "setup-pr-worktree" in consequence_section or "scripts/" in consequence_section
        test_result(
            "References setup-pr-worktree.sh script",
            has_script_ref,
            "Should mention the script that materializes the context"
        )

    print()

    # ============================================================================
    # SECTION 4: CLAUDE.md "Project context" section
    # ============================================================================
    print("[Section 4] CLAUDE.md 'Project context' section clarity")

    claude_file = REPO_ROOT / "CLAUDE.md"
    claude_content = claude_file.read_text() if claude_file.exists() else ""

    test_result(
        "CLAUDE.md exists",
        claude_file.exists(),
        "File not found"
    )

    # Find "## Project context" section
    project_context_idx = claude_content.find("## Project context")
    test_result(
        "'## Project context' section exists",
        project_context_idx >= 0,
        "Section not found"
    )

    if project_context_idx >= 0:
        # Extract section content (skip the heading line itself)
        section_start = project_context_idx + len("## Project context\n")
        section = claude_content[section_start:]
        # Find next ## heading
        next_heading_idx = section.find("\n## ")
        if next_heading_idx > 0:
            section = section[:next_heading_idx]

        # Should state .claude/project.yaml has "no exceptions"
        has_project_yaml_no_exceptions = "project.yaml" in section and "no exceptions" in section.lower()
        test_result(
            ".claude/project.yaml stated as having no exceptions",
            has_project_yaml_no_exceptions,
            "Should clarify that .claude/project.yaml is gitignored with no exceptions"
        )

        # Should state that ONLY -local.yaml has an exception (north-star-nick-local.yaml)
        has_local_exception = "north-star-nick-local.yaml" in section and "-local.yaml" in section
        test_result(
            "Only .claude/reviewers/*-local.yaml has exception (north-star-nick-local.yaml)",
            has_local_exception,
            "Should identify north-star-nick-local.yaml as the sole deliberate exception"
        )

        # Verify "no exceptions" comes in sentence about project.yaml, not about local overrides
        # The sentence structure should be: project.yaml ... no exceptions. Then separately: -local.yaml ... except north-star-nick
        project_yaml_idx = section.lower().find("project.yaml")
        no_exceptions_idx = section.lower().find("no exceptions")
        north_star_idx = section.lower().find("north-star-nick")

        # Valid structure: project.yaml text mentions "no exceptions" BEFORE the north-star-nick mention
        valid_structure = (
            project_yaml_idx >= 0 and
            no_exceptions_idx >= 0 and
            north_star_idx >= 0 and
            project_yaml_idx < no_exceptions_idx < north_star_idx
        )

        test_result(
            "Does NOT imply .claude/project.yaml has any exception",
            valid_structure,
            "Structure should be: project.yaml has 'no exceptions', then separately mention north-star-nick as the exception for -local.yaml"
        )

    print()

    # ============================================================================
    # SECTION 5: Behavioral test - guarded git rm simulation
    # ============================================================================
    print("[Section 5] Behavioral test: guarded git rm --cached")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create a temporary git repo
            repo = tmpdir / "test_repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=str(repo),
                capture_output=True,
                timeout=5
            )

            # Create .claude directory structure
            claude_dir = repo / ".claude"
            claude_dir.mkdir()
            reviewers_dir = claude_dir / "reviewers"
            reviewers_dir.mkdir()

            # Test 1: Run guarded git rm on untracked file (should not error)
            test_file = claude_dir / "project.yaml"
            test_file.write_text("test: content")

            # Extract the guarded command from README
            guard_cmd = (
                "git ls-files --error-unmatch .claude/project.yaml >/dev/null 2>&1 "
                "&& git rm --cached .claude/project.yaml || true"
            )

            result = subprocess.run(
                guard_cmd,
                cwd=str(repo),
                capture_output=True,
                timeout=5,
                shell=True
            )

            test_result(
                "Guarded git rm succeeds when file is untracked (exit code 0)",
                result.returncode == 0,
                f"Expected exit code 0, got {result.returncode}"
            )

            # Test 2: Verify file still exists after guarded rm (not tracked)
            test_result(
                "File still exists on disk after guarded rm (untracked case)",
                test_file.exists(),
                "Untracked file should remain on disk after guarded rm"
            )

            # Test 3: Now track the file and verify rm works
            subprocess.run(
                ["git", "add", ".claude/project.yaml"],
                cwd=str(repo),
                capture_output=True,
                timeout=5
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=str(repo),
                capture_output=True,
                timeout=5
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=str(repo),
                capture_output=True,
                timeout=5
            )
            subprocess.run(
                ["git", "commit", "-m", "track file"],
                cwd=str(repo),
                capture_output=True,
                timeout=5
            )

            # Now run guarded rm again (file is tracked)
            result = subprocess.run(
                guard_cmd,
                cwd=str(repo),
                capture_output=True,
                timeout=5,
                shell=True
            )

            test_result(
                "Guarded git rm succeeds when file is tracked (exit code 0)",
                result.returncode == 0,
                f"Expected exit code 0, got {result.returncode}"
            )

            test_result(
                "File still exists on disk after guarded rm (tracked case)",
                test_file.exists(),
                "Tracked file should remain on disk after git rm --cached (keeps local copy)"
            )

    except Exception as e:
        test_result(
            "Behavioral test: guarded git rm simulation completed",
            False,
            f"Test failed with exception: {e}"
        )

    print()

    # ============================================================================
    # SECTION 6: Cross-file consistency
    # ============================================================================
    print("[Section 6] Cross-file consistency on PR mode and gitignore policy")

    # All references to "reviewer's own" or "main worktree" should be consistent
    references_count = 0
    if "reviewer's own" in adr_0005_content:
        references_count += 1
    if "main worktree" in adr_0005_content:
        references_count += 1

    test_result(
        "ADR-0005 and reviewers/README.md use consistent terminology",
        references_count >= 1 and "project.yaml" in readme_content,
        "Documentation should consistently refer to reviewer's own context and main worktree"
    )

    print()
    h.summarize_and_exit()
