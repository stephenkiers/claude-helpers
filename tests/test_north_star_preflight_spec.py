#!/usr/bin/env python3
"""
Spec-blind test suite for North Star Preflight (#273).

Written ONLY from the plan specification (lines 181-242 "Testing Strategy"),
without reading the implementation files. It verifies the accepted behavior by
running the actual scripts via subprocess with temporary directories and a
stubbed `gh` command.

Test categories:
- files phase: missing scripts, dangling symlinks, gh auth, --allow-degraded
- snapshot phase: empty/missing in-flight.md, partial snapshots, unavailable snapshot
- Grammar: NORTH STAR BLIND prefix, token vocabulary, de-duplication, unparseable handling
- Contract: in-flight.err key names and the preflight classification

Run with: python3 tests/test_north_star_preflight_spec.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

PREFLIGHT = REPO_ROOT / "scripts" / "north-star-preflight.py"
SNAPSHOT = REPO_ROOT / "scripts" / "in-flight-snapshot.py"

h = Harness("NORTH STAR PREFLIGHT SPEC-BLIND TEST SUITE")
t = h.test_result


def run_preflight(args, env=None):
    """Run north-star-preflight.py and return (returncode, stdout, stderr)."""
    if env is None:
        env = os.environ.copy()

    cmd = [sys.executable, str(PREFLIGHT)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr


def run_snapshot(script_path, args, env=None):
    """Run in-flight-snapshot.py and return (returncode, stdout, stderr)."""
    if env is None:
        env = os.environ.copy()

    cmd = [sys.executable, str(script_path)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout.strip(), result.stderr


def setup_claude_dir(tmpdir):
    """Create a minimal ~/.claude structure for testing."""
    claude_dir = tmpdir / ".claude"
    claude_dir.mkdir()

    # Create scripts and prompts directories
    scripts_dir = claude_dir / "scripts"
    scripts_dir.mkdir()
    prompts_dir = claude_dir / "prompts"
    prompts_dir.mkdir()

    # Copy actual preflight script
    (scripts_dir / "north-star-preflight.py").write_text(
        PREFLIGHT.read_text()
    )
    (scripts_dir / "north-star-preflight.py").chmod(0o755)

    # Copy actual snapshot script
    (scripts_dir / "in-flight-snapshot.py").write_text(
        SNAPSHOT.read_text()
    )
    (scripts_dir / "in-flight-snapshot.py").chmod(0o755)

    # Create scout prompt
    (prompts_dir / "north-star-scout.md").write_text("# Scout prompt\n")

    return claude_dir


def fake_gh_success(bin_dir):
    """Create a fake gh that succeeds."""
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    return bin_dir


def fake_gh_failure(bin_dir):
    """Create a fake gh that fails with exit 1."""
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    return bin_dir


def env_with_path(bin_dir):
    """Return an env dict with bin_dir added to PATH."""
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
    return env


# ============================================================================
# FILES PHASE TESTS
# ============================================================================

print()
print("[FILES PHASE] Happy path and error cases")
print("=" * 70)


def test_files_ok_when_present():
    """Both scripts present and gh succeeds -> OK, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        fake_gh_success(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        return (rc == 0 and out == "OK"), f"rc={rc}, out={out!r}"


t("files: both present and gh succeeds -> OK, exit 0",
  test_files_ok_when_present(),
  test_files_ok_when_present()[1])


def test_files_fail_missing_snapshot_script():
    """Missing snapshot script -> FAIL snapshot-script, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        # Delete the snapshot script
        (claude_dir / "scripts" / "in-flight-snapshot.py").unlink()
        fake_gh_success(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        has_fail = out.startswith("FAIL")
        has_token = "snapshot-script" in out
        has_setup_local = "/setup-local" in err

        return (rc == 3 and has_fail and has_token and has_setup_local), \
               f"rc={rc}, out={out!r}, '/setup-local' in err={has_setup_local}"


t("files: missing snapshot script -> FAIL snapshot-script, exit 3, /setup-local in stderr",
  test_files_fail_missing_snapshot_script(),
  test_files_fail_missing_snapshot_script()[1])


def test_files_fail_missing_scout_prompt():
    """Missing scout prompt -> FAIL scout-prompt, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        # Delete the scout prompt
        (claude_dir / "prompts" / "north-star-scout.md").unlink()
        fake_gh_success(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        has_fail = out.startswith("FAIL")
        has_token = "scout-prompt" in out

        return (rc == 3 and has_fail and has_token), \
               f"rc={rc}, out={out!r}"


t("files: missing scout prompt -> FAIL scout-prompt, exit 3",
  test_files_fail_missing_scout_prompt(),
  test_files_fail_missing_scout_prompt()[1])


def test_files_fail_dangling_symlink():
    """Dangling symlink counts as missing -> FAIL, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)

        # Replace snapshot script with a dangling symlink
        script_file = claude_dir / "scripts" / "in-flight-snapshot.py"
        script_file.unlink()
        os.symlink("/nonexistent/path", script_file)

        fake_gh_success(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        has_fail = out.startswith("FAIL")
        has_snapshot_token = "snapshot-script" in out

        return (rc == 3 and has_fail and has_snapshot_token), \
               f"rc={rc}, out={out!r}"


t("files: dangling symlink counts as missing -> FAIL snapshot-script, exit 3",
  test_files_fail_dangling_symlink(),
  test_files_fail_dangling_symlink()[1])


def test_files_fail_gh_auth():
    """gh auth fails -> FAIL gh-auth, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        fake_gh_failure(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        has_fail = out.startswith("FAIL")
        has_token = "gh-auth" in out

        return (rc == 3 and has_fail and has_token), \
               f"rc={rc}, out={out!r}"


t("files: gh auth failure -> FAIL gh-auth, exit 3",
  test_files_fail_gh_auth(),
  test_files_fail_gh_auth()[1])


def test_files_allow_degraded_downgrades():
    """--allow-degraded downgrades hard failure to BLIND, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        # Delete snapshot script AND gh fails
        (claude_dir / "scripts" / "in-flight-snapshot.py").unlink()
        fake_gh_failure(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(
            ["files", "--claude-dir", str(claude_dir), "--allow-degraded"],
            env
        )

        has_blind = out.startswith("BLIND")
        has_both_tokens = "snapshot-script" in out and "gh-auth" in out

        return (rc == 0 and has_blind and has_both_tokens), \
               f"rc={rc}, out={out!r}"


t("files: --allow-degraded downgrades to BLIND with same tokens, exit 0",
  test_files_allow_degraded_downgrades(),
  test_files_allow_degraded_downgrades()[1])


# ============================================================================
# SNAPSHOT PHASE TESTS
# ============================================================================

print()
print("[SNAPSHOT PHASE] Empty, partial, and unavailable snapshots")
print("=" * 70)


def test_snapshot_empty():
    """Empty in-flight.md -> FAIL snapshot-empty, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create empty in-flight.md
        (session_dir / "in-flight.md").write_text("")

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_fail = out.startswith("FAIL")
        has_token = "snapshot-empty" in out

        return (rc == 3 and has_fail and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: empty in-flight.md -> FAIL snapshot-empty, exit 3",
  test_snapshot_empty(),
  test_snapshot_empty()[1])


def test_snapshot_missing():
    """Missing in-flight.md -> FAIL snapshot-empty, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()
        # in-flight.md not created at all

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_fail = out.startswith("FAIL")
        has_token = "snapshot-empty" in out

        return (rc == 3 and has_fail and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: missing in-flight.md -> FAIL snapshot-empty, exit 3",
  test_snapshot_missing(),
  test_snapshot_missing()[1])


def test_snapshot_unavailable():
    """Both issues and prs in .err -> FAIL snapshot-unavailable, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content
        (session_dir / "in-flight.md").write_text("# In-flight\n")

        # Create in-flight.err with both issues and prs keys
        err_content = "issues: timeout\nprs: permission denied\n"
        (session_dir / "in-flight.err").write_text(err_content)

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_fail = out.startswith("FAIL")
        has_token = "snapshot-unavailable" in out

        return (rc == 3 and has_fail and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: both issues and prs in .err -> FAIL snapshot-unavailable, exit 3",
  test_snapshot_unavailable(),
  test_snapshot_unavailable()[1])


def test_snapshot_partial_issues():
    """Only issues failing -> BLIND snapshot-partial=issues, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content
        (session_dir / "in-flight.md").write_text("# In-flight\n")

        # Create in-flight.err with only issues key
        err_content = "issues: timeout\n"
        (session_dir / "in-flight.err").write_text(err_content)

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_blind = out.startswith("BLIND")
        has_token = "snapshot-partial=issues" in out

        return (rc == 0 and has_blind and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: only issues failing -> BLIND snapshot-partial=issues, exit 0",
  test_snapshot_partial_issues(),
  test_snapshot_partial_issues()[1])


def test_snapshot_partial_prs():
    """Only prs failing -> BLIND snapshot-partial=prs, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content
        (session_dir / "in-flight.md").write_text("# In-flight\n")

        # Create in-flight.err with only prs key
        err_content = "prs: timeout\n"
        (session_dir / "in-flight.err").write_text(err_content)

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_blind = out.startswith("BLIND")
        has_token = "snapshot-partial=prs" in out

        return (rc == 0 and has_blind and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: only prs failing -> BLIND snapshot-partial=prs, exit 0",
  test_snapshot_partial_prs(),
  test_snapshot_partial_prs()[1])


def test_snapshot_partial_epics():
    """Only epics failing -> BLIND snapshot-partial=epics, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content
        (session_dir / "in-flight.md").write_text("# In-flight\n")

        # Create in-flight.err with only epics key
        err_content = "epics: timeout\n"
        (session_dir / "in-flight.err").write_text(err_content)

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        has_blind = out.startswith("BLIND")
        has_token = "snapshot-partial=epics" in out

        return (rc == 0 and has_blind and has_token), \
               f"rc={rc}, out={out!r}"


t("snapshot: only epics failing -> BLIND snapshot-partial=epics, exit 0",
  test_snapshot_partial_epics(),
  test_snapshot_partial_epics()[1])


def test_snapshot_no_err_file():
    """No in-flight.err file -> OK, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content but no .err file
        (session_dir / "in-flight.md").write_text("# In-flight\n")

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        return (rc == 0 and out == "OK"), \
               f"rc={rc}, out={out!r}"


t("snapshot: no in-flight.err file -> OK, exit 0",
  test_snapshot_no_err_file(),
  test_snapshot_no_err_file()[1])


# ============================================================================
# BLIND-LINE GRAMMAR TESTS
# ============================================================================

print()
print("[BLIND-LINE] Grammar, prefix, de-duplication, and fallback")
print("=" * 70)


def test_blind_line_prefix():
    """blind-line output starts with 'NORTH STAR BLIND: ' exactly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("gh-auth\nsnapshot-empty\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        has_prefix = out.startswith("NORTH STAR BLIND: ")

        return (rc == 0 and has_prefix), \
               f"rc={rc}, out={out!r}"


t("blind-line: output starts with 'NORTH STAR BLIND: ' exactly",
  test_blind_line_prefix(),
  test_blind_line_prefix()[1])


def test_blind_line_dedup():
    """blind-line de-duplicates and preserves order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("gh-auth\nsnapshot-empty\ngh-auth\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        # Should have exactly one "gh-auth" in the output
        has_line = "NORTH STAR BLIND: " in out
        count_auth = out.count("gh-auth")
        count_empty = out.count("snapshot-empty")

        return (rc == 0 and has_line and count_auth == 1 and count_empty == 1), \
               f"rc={rc}, out={out!r}, counts: gh-auth={count_auth}, snapshot-empty={count_empty}"


t("blind-line: de-duplicates tokens, preserving first-seen order",
  test_blind_line_dedup(),
  test_blind_line_dedup()[1])


def test_blind_line_empty():
    """blind-line with no tokens -> no output, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        return (rc == 0 and out == ""), \
               f"rc={rc}, out={out!r}"


t("blind-line: empty tokens -> no output, exit 0",
  test_blind_line_empty(),
  test_blind_line_empty()[1])


def test_blind_line_unknown_token():
    """blind-line rejects unknown tokens with exit 2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("unknown-token-xyz\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        return (rc == 2), f"rc={rc}, out={out!r}"


t("blind-line: unknown token -> exit 2",
  test_blind_line_unknown_token(),
  test_blind_line_unknown_token()[1])


def test_blind_line_nick_with_comma_rejected():
    """blind-line rejects nick= tokens containing commas with exit 2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("gh-auth\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file), "nick=has,comma"]
        )

        return (rc == 2), f"rc={rc}, out={out!r}"


t("blind-line: nick= token with comma -> exit 2",
  test_blind_line_nick_with_comma_rejected(),
  test_blind_line_nick_with_comma_rejected()[1])


def test_blind_line_scout_failed_all():
    """blind-line accepts scout-failed=all token."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file), "scout-failed=all"]
        )

        has_scout = "scout-failed=all" in out

        return (rc == 0 and has_scout), \
               f"rc={rc}, out={out!r}"


t("blind-line: accepts scout-failed=all token",
  test_blind_line_scout_failed_all(),
  test_blind_line_scout_failed_all()[1])


def test_blind_line_nick_unparseable():
    """blind-line accepts nick=unparseable token (fallback)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file), "nick=unparseable"]
        )

        has_unparseable = "nick=unparseable" in out

        return (rc == 0 and has_unparseable), \
               f"rc={rc}, out={out!r}"


t("blind-line: accepts nick=unparseable fallback token",
  test_blind_line_nick_unparseable(),
  test_blind_line_nick_unparseable()[1])


# ============================================================================
# SIDE-EFFECT FREEDOM TEST
# ============================================================================

print()
print("[SIDE-EFFECTS] Preflight is read-only")
print("=" * 70)


def test_snapshot_phase_no_side_effects():
    """snapshot phase does not modify files on disk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        (session_dir / "in-flight.md").write_text("# In-flight\n")
        (session_dir / "in-flight.err").write_text("issues: timeout\n")

        # Snapshot the file state before
        files_before = set(session_dir.glob("*"))

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        # Snapshot after
        files_after = set(session_dir.glob("*"))

        # Files should be unchanged
        return (files_before == files_after), \
               f"Files changed: before={files_before}, after={files_after}"


t("snapshot phase: does not create or modify files",
  test_snapshot_phase_no_side_effects(),
  test_snapshot_phase_no_side_effects()[1])


print()
h.summarize_and_exit()
