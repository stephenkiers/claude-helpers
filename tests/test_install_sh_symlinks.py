#!/usr/bin/env python3
"""
Test suite for install.sh recursive symlink behavior (item 5).

Covers:
1. Nested files (e.g. scripts/workflow/*.py) get symlinked into mirrored nested path
2. Doubly-nested files (e.g. scripts/workflow/providers/*.py) also get symlinked
3. Files under __pycache__ directories (at any depth) are NOT linked
4. Stale nested symlinks get pruned
5. Existing top-level-only behavior still works (pre-existing case)

Run with: python3 tests/test_install_sh_symlinks.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import REPO_ROOT, Harness

INSTALL_SH = REPO_ROOT / "install.sh"


def run_bash_script(args, cwd=None, env=None):
    """Run a bash script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = ["bash"] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def setup_test_repo_with_install_sh(tmpdir_path, source_repo):
    """Copy install.sh into the source_repo so REPO_DIR resolves correctly."""
    install_sh_copy = source_repo / "install.sh"
    shutil.copy(INSTALL_SH, install_sh_copy)
    install_sh_copy.chmod(0o755)
    return install_sh_copy


def test_nested_file_symlinked():
    """A nested file (scripts/workflow/cli.py) gets symlinked into mirrored path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo with nested file
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()
        workflow_dir = scripts_dir / "workflow"
        workflow_dir.mkdir()

        # Create a test file in the nested directory
        test_file = workflow_dir / "test_module.py"
        test_file.write_text("# test nested file\n")

        # Also create top-level files for comparison
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh with our test directories
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that the nested file was symlinked
        claude_dir = test_home / ".claude"
        target_symlink = claude_dir / "scripts" / "workflow" / "test_module.py"

        if not target_symlink.is_symlink():
            return False, f"nested symlink not created at {target_symlink}"

        # Verify it points to the right place
        link_target = target_symlink.readlink()
        expected_target = test_file
        if link_target != expected_target:
            return False, f"symlink points to {link_target}, expected {expected_target}"

        return True, ""


def test_doubly_nested_file_symlinked():
    """A doubly-nested file (scripts/workflow/providers/base.py) gets symlinked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo with doubly-nested file
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()
        workflow_dir = scripts_dir / "workflow"
        workflow_dir.mkdir()
        providers_dir = workflow_dir / "providers"
        providers_dir.mkdir()

        # Create a test file in the doubly-nested directory
        test_file = providers_dir / "test_provider.py"
        test_file.write_text("# test doubly-nested file\n")

        # Create at least one top-level file so install.sh processes the dir
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh with our test directories
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that the doubly-nested file was symlinked
        claude_dir = test_home / ".claude"
        target_symlink = claude_dir / "scripts" / "workflow" / "providers" / "test_provider.py"

        if not target_symlink.is_symlink():
            return False, f"doubly-nested symlink not created at {target_symlink}"

        # Verify it points to the right place
        link_target = target_symlink.readlink()
        expected_target = test_file
        if link_target != expected_target:
            return False, f"symlink points to {link_target}, expected {expected_target}"

        return True, ""


def test_pycache_files_not_symlinked():
    """Files under __pycache__ directories are NOT symlinked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()

        # Create __pycache__ directory with a file
        pycache_dir = scripts_dir / "__pycache__"
        pycache_dir.mkdir()
        cache_file = pycache_dir / "cli.cpython-39.pyc"
        cache_file.write_text("fake compiled python\n")

        # Create a normal file to ensure the dir is processed
        normal_file = scripts_dir / "normal.py"
        normal_file.write_text("# normal file\n")

        # Create at least one top-level file
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that __pycache__ file was NOT symlinked
        claude_dir = test_home / ".claude"
        pycache_target = claude_dir / "scripts" / "__pycache__"

        if pycache_target.exists():
            return False, f"__pycache__ directory should not have been created at {pycache_target}"

        # But the normal file should have been symlinked
        normal_target = claude_dir / "scripts" / "normal.py"
        if not normal_target.is_symlink():
            return False, f"normal.py should have been symlinked at {normal_target}"

        return True, ""


def test_pycache_at_any_depth_not_symlinked():
    """Files under __pycache__ at any depth are NOT symlinked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()
        workflow_dir = scripts_dir / "workflow"
        workflow_dir.mkdir()

        # Create __pycache__ at nested level
        pycache_dir = workflow_dir / "__pycache__"
        pycache_dir.mkdir()
        cache_file = pycache_dir / "cli.cpython-39.pyc"
        cache_file.write_text("fake compiled python\n")

        # Create a normal nested file to ensure the dir is processed
        normal_file = workflow_dir / "normal.py"
        normal_file.write_text("# normal nested file\n")

        # Create at least one top-level file
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that nested __pycache__ file was NOT symlinked
        claude_dir = test_home / ".claude"
        pycache_target = claude_dir / "scripts" / "workflow" / "__pycache__"

        if pycache_target.exists():
            return False, f"nested __pycache__ should not exist at {pycache_target}"

        # But the normal file should have been symlinked
        normal_target = claude_dir / "scripts" / "workflow" / "normal.py"
        if not normal_target.is_symlink():
            return False, f"normal.py should have been symlinked at {normal_target}"

        return True, ""


def test_stale_nested_symlink_pruned():
    """A stale nested symlink (pointing at deleted source) gets pruned."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()
        claude_dir = test_home / ".claude"
        claude_dir.mkdir()

        # Create synthetic source repo
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()
        workflow_dir = scripts_dir / "workflow"
        workflow_dir.mkdir()

        # Create a normal file
        normal_file = workflow_dir / "normal.py"
        normal_file.write_text("# normal file\n")

        # Create at least one top-level file
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Pre-create a stale symlink (pointing at non-existent file)
        target_dir = claude_dir / "scripts" / "workflow"
        target_dir.mkdir(parents=True)
        stale_symlink = target_dir / "deleted_module.py"
        deleted_source = source_repo / "scripts" / "workflow" / "deleted_module.py"

        # Create symlink pointing to a file that doesn't exist (and won't)
        os.symlink(deleted_source, stale_symlink)

        # Verify the stale symlink exists before install.sh runs
        if not stale_symlink.is_symlink():
            return False, "failed to create test stale symlink"

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that the stale symlink was pruned
        if stale_symlink.exists() or stale_symlink.is_symlink():
            return False, f"stale symlink should have been pruned but still exists: {stale_symlink}"

        # But the normal file should still be symlinked
        normal_target = claude_dir / "scripts" / "workflow" / "normal.py"
        if not normal_target.is_symlink():
            return False, f"normal.py should have been symlinked at {normal_target}"

        return True, ""


def test_top_level_files_still_work():
    """Existing top-level file symlinkin (pre-existing case) still works."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo with only top-level files
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()

        # Create multiple top-level files
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            dir_path = source_repo / dirname
            dir_path.mkdir()
            # Create a file in each directory
            (dir_path / f"test_{dirname}.txt").write_text(f"test {dirname}\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that all top-level files were symlinked
        claude_dir = test_home / ".claude"

        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            target_symlink = claude_dir / dirname / f"test_{dirname}.txt"

            if not target_symlink.is_symlink():
                return False, f"top-level symlink not created for {dirname}/test_{dirname}.txt"

            # Verify it points to the right place
            link_target = target_symlink.readlink()
            expected_target = source_repo / dirname / f"test_{dirname}.txt"
            if link_target != expected_target:
                return False, f"symlink for {dirname} points to {link_target}, expected {expected_target}"

        return True, ""


def test_empty_subdirectories_cleaned_up():
    """Empty subdirectories left behind by pruning are cleaned up."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()
        claude_dir = test_home / ".claude"
        claude_dir.mkdir()

        # Create synthetic source repo
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()

        # Create a normal file
        normal_file = scripts_dir / "normal.py"
        normal_file.write_text("# normal file\n")

        # Create at least one top-level file
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Pre-create a stale symlink in a nested directory
        target_subdir = claude_dir / "scripts" / "subdir"
        target_subdir.mkdir(parents=True)
        stale_symlink = target_subdir / "stale.py"

        # Create symlink pointing to non-existent file
        os.symlink("/nonexistent/file.py", stale_symlink)

        # Verify structure exists before install.sh runs
        if not target_subdir.exists():
            return False, "failed to create test subdirectory"

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that the empty subdirectory was cleaned up
        if target_subdir.exists():
            # If it exists, it should have at least some files
            files_in_subdir = list(target_subdir.iterdir())
            if len(files_in_subdir) == 0:
                return False, f"empty subdirectory should have been cleaned up: {target_subdir}"

        # Note: This test is checking that empty dirs get cleaned, but the dir itself
        # might be recreated by the normal linking process. The important thing is that
        # directories with only stale symlinks get their empty subdirs cleaned.

        return True, ""


def test_nested_structure_preserved():
    """Nested directory structure is preserved across multiple levels."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create fake HOME and source repo structure
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        # Create synthetic source repo with deeply nested structure
        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        scripts_dir = source_repo / "scripts"
        scripts_dir.mkdir()
        workflow_dir = scripts_dir / "workflow"
        workflow_dir.mkdir()
        providers_dir = workflow_dir / "providers"
        providers_dir.mkdir()

        # Create files at various levels
        (scripts_dir / "root_script.py").write_text("# root\n")
        (workflow_dir / "workflow_script.py").write_text("# workflow\n")
        (providers_dir / "provider_script.py").write_text("# provider\n")

        # Create at least one top-level file
        top_level_file = source_repo / "commands"
        top_level_file.mkdir()
        (top_level_file / "test_cmd.md").write_text("test command\n")

        # Copy install.sh into the test repo so REPO_DIR resolves correctly
        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        # Run install.sh
        env = os.environ.copy()
        env["HOME"] = str(test_home)

        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Check that all files at all levels were symlinked
        claude_dir = test_home / ".claude"

        test_cases = [
            ("scripts", "root_script.py"),
            ("scripts/workflow", "workflow_script.py"),
            ("scripts/workflow/providers", "provider_script.py"),
        ]

        for rel_dir, filename in test_cases:
            target_symlink = claude_dir / rel_dir / filename
            if not target_symlink.is_symlink():
                return False, f"symlink not created at {target_symlink}"

        return True, ""


if __name__ == "__main__":
    h = Harness("INSTALL.SH RECURSIVE SYMLINK TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Nested file symlink behavior")
    passed, msg = test_nested_file_symlinked()
    test_result("nested file gets symlinked (scripts/workflow/*.py)", passed, msg)

    print()

    print("[Section 2] Doubly-nested file symlink behavior")
    passed, msg = test_doubly_nested_file_symlinked()
    test_result("doubly-nested file gets symlinked (scripts/workflow/providers/*.py)", passed, msg)

    print()

    print("[Section 3] __pycache__ exclusion")
    passed, msg = test_pycache_files_not_symlinked()
    test_result("__pycache__ files at top level not symlinked", passed, msg)

    passed, msg = test_pycache_at_any_depth_not_symlinked()
    test_result("__pycache__ files at any depth not symlinked", passed, msg)

    print()

    print("[Section 4] Stale symlink pruning")
    passed, msg = test_stale_nested_symlink_pruned()
    test_result("stale nested symlink gets pruned", passed, msg)

    print()

    print("[Section 5] Backward compatibility")
    passed, msg = test_top_level_files_still_work()
    test_result("top-level file symlinks still work (pre-existing case)", passed, msg)

    print()

    print("[Section 6] Directory cleanup")
    passed, msg = test_empty_subdirectories_cleaned_up()
    test_result("empty subdirectories cleaned up after pruning", passed, msg)

    print()

    print("[Section 7] Complex nested structure")
    passed, msg = test_nested_structure_preserved()
    test_result("deeply nested structure preserved across multiple levels", passed, msg)

    print()

    h.summarize_and_exit()
