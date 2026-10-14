#!/usr/bin/env python3
"""
Test suite for worktree cache seeding functionality (issue #275).

Tests seed_worktree_cache function, apply_track seeding, check resolution,
and doc consistency.

Run with: python3 tests/test_seed_worktree_cache.py
"""

import sys
import os
import json
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.cache import seed_worktree_cache, SEEDED_FILES
from workflow.checks import resolve_check_command
from workflow.track import (
    apply_track, plan_track, STEP_SEED_CACHE,
    TrackApplyResult
)
from workflow.providers.local import LocalProvider
from _test_harness import Harness
from _git_fixture import GitFixture


def _track_claude_dir(fixture):
    """Commit a tracked .claude/.gitkeep so new worktrees get a .claude dir."""
    claude = fixture.main_worktree / ".claude"
    claude.mkdir(exist_ok=True)
    (claude / ".gitkeep").write_text("")
    fixture._run_git(["add", ".claude/.gitkeep"], cwd=fixture.main_worktree)
    fixture._run_git(["commit", "-m", "track .claude"], cwd=fixture.main_worktree)


if __name__ == "__main__":
    h = Harness("SEED WORKTREE CACHE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] SEEDED_FILES constant")

    test_result(
        "SEEDED_FILES is a tuple",
        isinstance(SEEDED_FILES, tuple)
    )

    test_result(
        "SEEDED_FILES contains repo-cache.json",
        "repo-cache.json" in SEEDED_FILES
    )

    test_result(
        "SEEDED_FILES contains project.yaml",
        "project.yaml" in SEEDED_FILES
    )

    test_result(
        "SEEDED_FILES has exactly 2 items",
        len(SEEDED_FILES) == 2
    )

    print()
    print("[Section 2] seed_worktree_cache: basic copy")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        # Create repo-cache.json in main
        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)
        repo_cache_content = {"commands": {"check": "just check"}}
        (main_claude / "repo-cache.json").write_text(json.dumps(repo_cache_content))

        # Call seed_worktree_cache
        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: returns tuple of (seeded, skipped, error)",
            isinstance(seeded, list) and isinstance(skipped, list),
            f"seeded={type(seeded)}, skipped={type(skipped)}"
        )

        test_result(
            "seed_worktree_cache: repo-cache.json is seeded",
            "repo-cache.json" in seeded,
            f"seeded={seeded}"
        )

        test_result(
            "seed_worktree_cache: file was copied",
            (new_wt / ".claude" / "repo-cache.json").exists()
        )

        test_result(
            "seed_worktree_cache: content is identical",
            (new_wt / ".claude" / "repo-cache.json").read_text() == json.dumps(repo_cache_content)
        )

        test_result(
            "seed_worktree_cache: error is None",
            error is None
        )

    print()
    print("[Section 3] seed_worktree_cache: both files seeded")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Create both files in main
        repo_cache = {"commands": {"check": "just check"}}
        project_yaml = "commands:\n  check: custom-check\n"
        (main_claude / "repo-cache.json").write_text(json.dumps(repo_cache))
        (main_claude / "project.yaml").write_text(project_yaml)

        # Seed
        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: both files are seeded",
            len(seeded) == 2 and "repo-cache.json" in seeded and "project.yaml" in seeded,
            f"seeded={seeded}"
        )

        test_result(
            "seed_worktree_cache: project.yaml copied correctly",
            (new_wt / ".claude" / "project.yaml").read_text() == project_yaml
        )

    print()
    print("[Section 4] seed_worktree_cache: skips missing source files")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Don't create any files

        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: success=True when no source files",
            error is None
        )

        test_result(
            "seed_worktree_cache: nothing seeded for missing sources",
            len(seeded) == 0,
            f"seeded={seeded}, skipped={skipped}"
        )

        test_result(
            "seed_worktree_cache: no files created if nothing to seed",
            not any((new_wt / ".claude").glob("*")) if (new_wt / ".claude").exists() else True
        )

    print()
    print("[Section 5] seed_worktree_cache: does not overwrite existing destination")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Create source file
        original_content = {"commands": {"check": "original"}}
        (main_claude / "repo-cache.json").write_text(json.dumps(original_content))

        # Create pre-existing destination file
        new_claude = new_wt / ".claude"
        new_claude.mkdir(exist_ok=True)
        existing_content = {"commands": {"check": "existing"}}
        (new_claude / "repo-cache.json").write_text(json.dumps(existing_content))

        # Try to seed
        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: skips existing destination files",
            "repo-cache.json" in skipped,
            f"seeded={seeded}, skipped={skipped}"
        )

        test_result(
            "seed_worktree_cache: preserves existing content",
            (new_wt / ".claude" / "repo-cache.json").read_text() == json.dumps(existing_content)
        )

        test_result(
            "seed_worktree_cache: success=True even when skipping",
            error is None
        )

    print()
    print("[Section 6] seed_worktree_cache: byte-identical copy")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Create a cache file with specific content
        original_bytes = b'{"commands": {"check": "just check", "test": null}}'
        (main_claude / "repo-cache.json").write_bytes(original_bytes)

        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: copies bytes exactly",
            (new_wt / ".claude" / "repo-cache.json").read_bytes() == original_bytes
        )

    print()
    print("[Section 7] seed_worktree_cache: symlink source outside .claude refused")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"
        external = tmppath / "external"

        main_wt.mkdir()
        new_wt.mkdir()
        external.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Create an external file and symlink to it
        external_cache = external / "repo-cache.json"
        external_cache.write_text(json.dumps({"commands": {"check": "external"}}))

        # Create symlink in .claude pointing outside
        (main_claude / "repo-cache.json").symlink_to(external_cache)

        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: refuses symlink escaping .claude",
            error is not None,
            f"error={error}"
        )

        test_result(
            "seed_worktree_cache: error message mentions symlink or escape",
            error is not None and ("symlink" in str(error).lower() or "escape" in str(error).lower()),
            f"error={error}"
        )

    print()
    print("[Section 8] seed_worktree_cache: skips non-regular files")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        # Create a directory instead of a file
        (main_claude / "repo-cache.json").mkdir()

        seeded, skipped, error = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: skips non-regular files (directories)",
            "repo-cache.json" in skipped,
            f"skipped={skipped}"
        )

        test_result(
            "seed_worktree_cache: success=True when skipping non-regular",
            error is None
        )

    print()
    print("[Section 9] seed_worktree_cache: idempotent re-run")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)

        cache_content = {"commands": {"check": "just check"}}
        (main_claude / "repo-cache.json").write_text(json.dumps(cache_content))

        # First run
        seeded1, skipped1, error1 = seed_worktree_cache(main_wt, new_wt)

        # Second run (file now exists)
        seeded2, skipped2, error2 = seed_worktree_cache(main_wt, new_wt)

        test_result(
            "seed_worktree_cache: second run skips existing file",
            "repo-cache.json" in skipped2,
            f"skipped2={skipped2}"
        )

        test_result(
            "seed_worktree_cache: both runs succeed",
            error1 is None and error2 is None
        )

    print()
    print("[Section 10] apply_track: records STEP_SEED_CACHE")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        tmpdir = Path(tempfile.mkdtemp())
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        provider = LocalProvider(tracker_path=tracker_path, plans_dir=plans_dir)

        # Create a plan
        plan, err = plan_track(
            provider=provider,
            plan_content="Implementation plan",
            title="Test seeding",
            mode="local",
            cwd=fixture.repo_root
        )

        test_result(
            "apply_track: plan created",
            plan is not None
        )

        # Ensure worktree parent exists
        fixture.worktree_parent.mkdir(parents=True, exist_ok=True)

        plan_json = json.dumps(plan.to_dict())

        # Apply the plan
        result, err = apply_track(provider, plan_json, cwd=fixture.repo_root)

        test_result(
            "apply_track: result is TrackApplyResult",
            isinstance(result, TrackApplyResult)
        )

        test_result(
            "apply_track: has steps_completed",
            hasattr(result, 'steps_completed') and isinstance(result.steps_completed, list)
        )

        test_result(
            "apply_track: STEP_SEED_CACHE in steps_completed",
            STEP_SEED_CACHE in result.steps_completed,
            f"steps_completed={result.steps_completed}"
        )

        import shutil
        shutil.rmtree(tmpdir)

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 11] apply_track: seeding with repo-cache.json in main")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create repo-cache.json in main's .claude
        main_claude = fixture.main_worktree / ".claude"
        main_claude.mkdir(exist_ok=True)
        repo_cache = {"commands": {"check": "just check"}}
        (main_claude / "repo-cache.json").write_text(json.dumps(repo_cache))

        tmpdir = Path(tempfile.mkdtemp())
        tracker_path = tmpdir / "issues.json"
        plans_dir = tmpdir / "plans"

        provider = LocalProvider(tracker_path=tracker_path, plans_dir=plans_dir)

        # Create and apply plan
        plan, err = plan_track(
            provider=provider,
            plan_content="Implementation plan",
            title="Test cache seeding",
            mode="local",
            cwd=fixture.repo_root
        )

        fixture.worktree_parent.mkdir(parents=True, exist_ok=True)
        plan_json = json.dumps(plan.to_dict())
        result, err = apply_track(provider, plan_json, cwd=fixture.repo_root)

        test_result(
            "apply_track: worktree created",
            result.success and result.worktree_path is not None
        )

        new_wt_path = Path(result.worktree_path)
        new_cache_file = new_wt_path / ".claude" / "repo-cache.json"

        test_result(
            "apply_track: repo-cache.json seeded to new worktree",
            new_cache_file.exists(),
            f"new_cache_file exists: {new_cache_file.exists()}"
        )

        test_result(
            "apply_track: seeded cache is byte-identical",
            new_cache_file.read_text() == json.dumps(repo_cache)
        )

        test_result(
            "apply_track: main's repo-cache.json unchanged",
            (main_claude / "repo-cache.json").read_text() == json.dumps(repo_cache)
        )

        import shutil
        shutil.rmtree(tmpdir)

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 12] resolve_check_command: returns command and source")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create repo-cache.json in main
        main_claude = fixture.main_worktree / ".claude"
        main_claude.mkdir(exist_ok=True)
        repo_cache = {"commands": {"check": "just check"}}
        (main_claude / "repo-cache.json").write_text(json.dumps(repo_cache))

        # Create a worktree with seeded cache
        fixture.create_branch("feature/test")
        wt_path = fixture.create_worktree("feature/test")

        # Seed the cache
        new_claude = wt_path / ".claude"
        new_claude.mkdir(exist_ok=True)
        (new_claude / "repo-cache.json").write_text(json.dumps(repo_cache))

        # Resolve check command
        command, source, failure_reason = resolve_check_command(wt_path, fixture.main_worktree)

        test_result(
            "resolve_check_command: returns tuple",
            isinstance(command, str) or command is None,
            f"command type: {type(command)}"
        )

        test_result(
            "resolve_check_command: finds command from repo-cache.json",
            command == "just check",
            f"command={command}"
        )

        test_result(
            "resolve_check_command: source mentions repo-cache",
            "repo-cache" in source,
            f"source={source}"
        )

        test_result(
            "resolve_check_command: failure_reason is None on success",
            failure_reason is None
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 13] resolve_check_command: fallback to project.yaml")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create a worktree with project.yaml but no repo-cache.json
        fixture.create_branch("feature/test2")
        wt_path = fixture.create_worktree("feature/test2")

        new_claude = wt_path / ".claude"
        new_claude.mkdir(exist_ok=True)

        project_yaml = "commands:\n  check: custom-check\n"
        (new_claude / "project.yaml").write_text(project_yaml)

        command, source, failure_reason = resolve_check_command(wt_path)

        test_result(
            "resolve_check_command: falls back to project.yaml",
            command == "custom-check",
            f"command={command}"
        )

        test_result(
            "resolve_check_command: source is 'project.yaml' on fallback",
            source == "project.yaml",
            f"source={source}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 14] resolve_check_command: failure when no checks configured")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create a worktree with no check configuration
        fixture.create_branch("feature/test3")
        wt_path = fixture.create_worktree("feature/test3")

        new_claude = wt_path / ".claude"
        new_claude.mkdir(exist_ok=True)

        command, source, failure_reason = resolve_check_command(wt_path)

        test_result(
            "resolve_check_command: command is None when not found",
            command is None,
            f"command={command}"
        )

        test_result(
            "resolve_check_command: failure_reason contains expected text",
            failure_reason is not None and "no check command configured" in failure_reason,
            f"failure_reason={failure_reason}"
        )

        test_result(
            "resolve_check_command: failure_reason mentions tried sources",
            failure_reason is not None and "repo-cache.json" in failure_reason and "project.yaml" in failure_reason,
            f"failure_reason={failure_reason}"
        )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 15] CLI: track seed-cache --from --to")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        main_wt = tmppath / "main"
        new_wt = tmppath / "new"

        main_wt.mkdir()
        new_wt.mkdir()

        main_claude = main_wt / ".claude"
        main_claude.mkdir(exist_ok=True)
        (main_claude / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))

        repo_root = Path(__file__).parent.parent

        # Run the CLI command
        result = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "track", "seed-cache",
             "--from", str(main_wt), "--to", str(new_wt)],
            capture_output=True,
            text=True,
            cwd=repo_root
        )

        test_result(
            "CLI track seed-cache: exits 0 on success",
            result.returncode == 0,
            f"returncode={result.returncode}, stderr={result.stderr}"
        )

        try:
            output = json.loads(result.stdout)
            test_result(
                "CLI track seed-cache: outputs valid JSON",
                isinstance(output, dict)
            )

            test_result(
                "CLI track seed-cache: has 'seeded' key",
                "seeded" in output,
                f"output keys: {output.keys()}"
            )

            test_result(
                "CLI track seed-cache: has 'skipped' key",
                "skipped" in output
            )

            test_result(
                "CLI track seed-cache: has 'error' key",
                "error" in output
            )

            test_result(
                "CLI track seed-cache: repo-cache.json in seeded list",
                "repo-cache.json" in output.get("seeded", []),
                f"seeded={output.get('seeded')}"
            )

        except json.JSONDecodeError as e:
            test_result(
                "CLI track seed-cache: outputs valid JSON",
                False,
                f"JSON error: {e}"
            )

    print()
    print("[Section 16] CLI: checks resolve-check --worktree")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create repo-cache.json in main
        main_claude = fixture.main_worktree / ".claude"
        main_claude.mkdir(exist_ok=True)
        (main_claude / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))

        # Create a worktree with seeded cache
        fixture.create_branch("feature/test-cli")
        wt_path = fixture.create_worktree("feature/test-cli")

        new_claude = wt_path / ".claude"
        new_claude.mkdir(exist_ok=True)
        (new_claude / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))

        repo_root = Path(__file__).parent.parent

        # Run the CLI command
        result = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "resolve-check",
             "--worktree", str(wt_path)],
            capture_output=True,
            text=True,
            cwd=repo_root
        )

        test_result(
            "CLI checks resolve-check: exits 0 when check found",
            result.returncode == 0,
            f"returncode={result.returncode}, stderr={result.stderr}"
        )

        try:
            output = json.loads(result.stdout)
            test_result(
                "CLI checks resolve-check: outputs valid JSON",
                isinstance(output, dict)
            )

            test_result(
                "CLI checks resolve-check: has 'command' key",
                "command" in output,
                f"output keys: {output.keys()}"
            )

            test_result(
                "CLI checks resolve-check: has 'source' key",
                "source" in output
            )

            test_result(
                "CLI checks resolve-check: has 'failure_reason' key",
                "failure_reason" in output
            )

            test_result(
                "CLI checks resolve-check: command is 'just check'",
                output.get("command") == "just check",
                f"command={output.get('command')}"
            )

            test_result(
                "CLI checks resolve-check: source mentions repo-cache",
                "repo-cache" in (output.get("source") or ""),
                f"source={output.get('source')}"
            )

        except json.JSONDecodeError as e:
            test_result(
                "CLI checks resolve-check: outputs valid JSON",
                False,
                f"JSON error: {e}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    print("[Section 17] CLI: checks resolve-check fails when no check configured")

    fixture = GitFixture()
    old_cwd = os.getcwd()
    try:
        os.chdir(fixture.repo_root)
        fixture.create_initial_commit("Initial commit")
        _track_claude_dir(fixture)

        # Create a worktree with no cache
        fixture.create_branch("feature/test-no-check")
        wt_path = fixture.create_worktree("feature/test-no-check")

        new_claude = wt_path / ".claude"
        new_claude.mkdir(exist_ok=True)
        # Don't create any cache files

        repo_root = Path(__file__).parent.parent

        # Run the CLI command
        result = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", "resolve-check",
             "--worktree", str(wt_path)],
            capture_output=True,
            text=True,
            cwd=repo_root
        )

        test_result(
            "CLI checks resolve-check: exits 1 when no check found",
            result.returncode == 1,
            f"returncode={result.returncode}"
        )

        try:
            output = json.loads(result.stdout)
            test_result(
                "CLI checks resolve-check: outputs valid JSON on failure",
                isinstance(output, dict)
            )

            test_result(
                "CLI checks resolve-check: has failure_reason on failure",
                output.get("failure_reason") is not None,
                f"failure_reason={output.get('failure_reason')}"
            )

            test_result(
                "CLI checks resolve-check: failure_reason mentions config attempt",
                "no check command configured" in output.get("failure_reason", ""),
                f"failure_reason={output.get('failure_reason')}"
            )

        except json.JSONDecodeError as e:
            test_result(
                "CLI checks resolve-check: outputs valid JSON on failure",
                False,
                f"JSON error: {e}"
            )

    finally:
        os.chdir(old_cwd)
        fixture.cleanup()

    print()
    h.summarize_and_exit()
