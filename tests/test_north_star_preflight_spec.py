#!/usr/bin/env python3
"""
Spec-blind test suite for North Star Preflight (#273).

Written ONLY from the plan specification (lines 181-242 "Testing Strategy"),
without reading the implementation files. It verifies the accepted behavior by
running the actual scripts via subprocess with temporary directories and a
stubbed `gh` command.

Test categories:
- files phase: missing scripts, dangling symlinks, gh auth (hard), --allow-degraded
- snapshot phase: empty/missing in-flight.md, partial snapshots, unavailable snapshot
- Grammar: NORTH STAR BLIND prefix, token vocabulary, de-duplication, unparseable handling
- Nick's line: first-5-lines scan, length cap, printable-only, undecodable bytes
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


def snapshot_state(directory):
    """Bytes and mtime of every regular file in a directory, for before/after comparison."""
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in directory.iterdir() if p.is_file()}


def nick_line_result(tmpdir, payload):
    """Write `payload` (bytes) as Nick's output and run blind-line with no tokens file contents."""
    nick_file = tmpdir / "nick.md"
    nick_file.write_bytes(payload)
    return run_preflight(["blind-line", "--tokens-file", str(tmpdir / "missing"),
                          "--nick-file", str(nick_file)])


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


ok, detail = test_files_ok_when_present()
t("files: both present and gh succeeds -> OK, exit 0", ok, detail)


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


ok, detail = test_files_fail_missing_snapshot_script()
t("files: missing snapshot script -> FAIL snapshot-script, exit 3, /setup-local in stderr", ok, detail)


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


ok, detail = test_files_fail_missing_scout_prompt()
t("files: missing scout prompt -> FAIL scout-prompt, exit 3", ok, detail)


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


ok, detail = test_files_fail_dangling_symlink()
t("files: dangling symlink counts as missing -> FAIL snapshot-script, exit 3", ok, detail)


def test_files_gh_auth_hard():
    """gh auth fails with every required file present -> FAIL gh-auth, exit 3."""
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

        return (rc == 3 and out == "FAIL gh-auth"), f"rc={rc}, out={out!r}"


ok, detail = test_files_gh_auth_hard()
t("files: gh auth failure is a hard stop -> FAIL gh-auth, exit 3", ok, detail)


def test_files_fail_line_lists_all_hard_tokens():
    """Hard snapshot-script plus gh-auth -> FAIL line lists both hard tokens, exit 3."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        (claude_dir / "scripts" / "in-flight-snapshot.py").unlink()
        fake_gh_failure(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(["files", "--claude-dir", str(claude_dir)], env)

        return (rc == 3 and out == "FAIL snapshot-script gh-auth"), f"rc={rc}, out={out!r}"


ok, detail = test_files_fail_line_lists_all_hard_tokens()
t("files: FAIL line lists every hard token including gh-auth, exit 3", ok, detail)


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


ok, detail = test_files_allow_degraded_downgrades()
t("files: --allow-degraded downgrades to BLIND with same tokens, exit 0", ok, detail)


def test_files_allow_degraded_lists_hard_tokens_in_order():
    """Under --allow-degraded the BLIND line lists the hard tokens in order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        home = tmpdir / "home"
        home.mkdir()
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()

        claude_dir = setup_claude_dir(home)
        (claude_dir / "scripts" / "in-flight-snapshot.py").unlink()
        fake_gh_failure(bin_dir)

        env = env_with_path(bin_dir)
        env["HOME"] = str(home)

        rc, out, err = run_preflight(
            ["files", "--claude-dir", str(claude_dir), "--allow-degraded"],
            env
        )

        return (rc == 0 and out == "BLIND snapshot-script gh-auth"), f"rc={rc}, out={out!r}"


ok, detail = test_files_allow_degraded_lists_hard_tokens_in_order()
t("files: --allow-degraded BLIND line is exactly the hard tokens in order", ok, detail)


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


ok, detail = test_snapshot_empty()
t("snapshot: empty in-flight.md -> FAIL snapshot-empty, exit 3", ok, detail)


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


ok, detail = test_snapshot_missing()
t("snapshot: missing in-flight.md -> FAIL snapshot-empty, exit 3", ok, detail)


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


ok, detail = test_snapshot_unavailable()
t("snapshot: both issues and prs in .err -> FAIL snapshot-unavailable, exit 3", ok, detail)


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


ok, detail = test_snapshot_partial_issues()
t("snapshot: only issues failing -> BLIND snapshot-partial=issues, exit 0", ok, detail)


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


ok, detail = test_snapshot_partial_prs()
t("snapshot: only prs failing -> BLIND snapshot-partial=prs, exit 0", ok, detail)


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


ok, detail = test_snapshot_partial_epics()
t("snapshot: only epics failing -> BLIND snapshot-partial=epics, exit 0", ok, detail)


def test_snapshot_no_err_file():
    """No in-flight.err file -> OK, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        # Create in-flight.md with content and a non-empty issue-bodies file, but no .err file
        (session_dir / "in-flight.md").write_text("# In-flight\n")
        (session_dir / "in-flight-issues.md").write_text("## #1 body\n")

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        return (rc == 0 and out == "OK"), \
               f"rc={rc}, out={out!r}"


ok, detail = test_snapshot_no_err_file()
t("snapshot: no in-flight.err file -> OK, exit 0", ok, detail)


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


ok, detail = test_blind_line_prefix()
t("blind-line: output starts with 'NORTH STAR BLIND: ' exactly", ok, detail)


def test_blind_line_dedup():
    """blind-line de-duplicates tokens."""
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


ok, detail = test_blind_line_dedup()
t("blind-line: de-duplicates tokens", ok, detail)


def test_blind_line_first_seen_order():
    """De-duplication keeps first-seen order, which here is NOT alphabetical."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("snapshot-empty\ngh-auth\nsnapshot-empty\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        return (rc == 0 and out == "NORTH STAR BLIND: snapshot-empty, gh-auth"), \
               f"rc={rc}, out={out!r}"


ok, detail = test_blind_line_first_seen_order()
t("blind-line: de-duplicated output is exactly first-seen order (not alphabetical)", ok, detail)


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


ok, detail = test_blind_line_empty()
t("blind-line: empty tokens -> no output, exit 0", ok, detail)


def test_blind_line_unknown_token():
    """An unknown token becomes nick=unparseable, warned on stderr, exit 0 (never exit 2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("unknown-token-xyz\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file)]
        )

        return (rc == 0 and out == "NORTH STAR BLIND: nick=unparseable" and "invalid" in err), \
               f"rc={rc}, out={out!r}, err={err!r}"


ok, detail = test_blind_line_unknown_token()
t("blind-line: unknown token -> nick=unparseable, stderr warning, exit 0", ok, detail)


def test_blind_line_nick_with_comma_rejected():
    """A nick= extra token containing a comma becomes nick=unparseable, exit 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        tokens_file = tmpdir / "tokens.txt"
        tokens_file.write_text("gh-auth\n")

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens_file), "nick=has,comma"]
        )

        return (rc == 0 and out == "NORTH STAR BLIND: gh-auth, nick=unparseable"), \
               f"rc={rc}, out={out!r}"


ok, detail = test_blind_line_nick_with_comma_rejected()
t("blind-line: nick= extra token with comma -> nick=unparseable, exit 0", ok, detail)


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


ok, detail = test_blind_line_scout_failed_all()
t("blind-line: accepts scout-failed=all token", ok, detail)


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


ok, detail = test_blind_line_nick_unparseable()
t("blind-line: accepts nick=unparseable fallback token", ok, detail)


# ============================================================================
# NICK'S LINE (--nick-file) TESTS
# ============================================================================

print()
print("[NICK LINE] Position, length cap, printable-only, undecodable bytes")
print("=" * 70)


def test_nick_line_within_first_five_lines():
    """The blind line is found on line 5. From line 6 on it is not parsed, but never silently dropped:
    it is reported as nick=unparseable (round-3 finding #1, which replaced the earlier silent drop)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        found_rc, found_out, _ = nick_line_result(
            tmpdir, b"one\ntwo\nthree\nfour\nNORTH STAR BLIND: nick=on line five\nbody\n")
        missed_rc, missed_out, _ = nick_line_result(
            tmpdir, b"one\ntwo\nthree\nfour\nfive\nNORTH STAR BLIND: nick=on line six\n")

        ok = (found_rc == 0 and found_out == "NORTH STAR BLIND: nick=on line five"
              and missed_rc == 0 and missed_out == "NORTH STAR BLIND: nick=unparseable")
        return ok, f"found={found_out!r}, missed={missed_out!r}"


ok, detail = test_nick_line_within_first_five_lines()
t("nick line: found within the first 5 lines; from line 6 reported as nick=unparseable", ok, detail)


def test_nick_text_length_cap():
    """Nick text of exactly 200 characters is kept; 201 collapses to nick=unparseable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        at_cap = "x" * 200
        over_cap = "x" * 201
        rc1, out1, _ = nick_line_result(tmpdir, ("NORTH STAR BLIND: nick=" + at_cap + "\n").encode())
        rc2, out2, _ = nick_line_result(tmpdir, ("NORTH STAR BLIND: nick=" + over_cap + "\n").encode())

        ok = (rc1 == 0 and out1 == "NORTH STAR BLIND: nick=" + at_cap
              and rc2 == 0 and out2 == "NORTH STAR BLIND: nick=unparseable")
        return ok, f"at_cap_out_len={len(out1)}, over_cap_out={out2!r}"


ok, detail = test_nick_text_length_cap()
t("nick line: nick text of 200 chars is kept, 201 chars collapses to nick=unparseable", ok, detail)


def test_nick_text_non_printable():
    """A non-printable character in nick text collapses the line to one nick=unparseable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        rc, out, _ = nick_line_result(tmpdir, b"NORTH STAR BLIND: nick=bad\x07bell\n")
        return (rc == 0 and out == "NORTH STAR BLIND: nick=unparseable"), f"rc={rc}, out={out!r}"


ok, detail = test_nick_text_non_printable()
t("nick line: non-printable character in nick text -> one nick=unparseable, exit 0", ok, detail)


def test_nick_line_undecodable_bytes():
    """Undecodable bytes in Nick's output do not raise; the line is still handled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        rc, out, err = nick_line_result(tmpdir, b"NORTH STAR BLIND: nick=caf\xff\n")
        ok = rc == 0 and "Traceback" not in err and out.startswith("NORTH STAR BLIND: nick=caf")
        return ok, f"rc={rc}, out={out!r}, err={err[-200:]!r}"


ok, detail = test_nick_line_undecodable_bytes()
t("nick line: undecodable bytes do not raise (exit 0, no traceback)", ok, detail)


# ============================================================================
# SIDE-EFFECT FREEDOM TEST
# ============================================================================

print()
print("[SIDE-EFFECTS] Preflight is read-only")
print("=" * 70)


def test_snapshot_phase_no_side_effects():
    """snapshot phase does not modify files on disk (names, bytes and mtimes unchanged)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        session_dir = tmpdir / "session"
        session_dir.mkdir()

        (session_dir / "in-flight.md").write_text("# In-flight\n")
        (session_dir / "in-flight.err").write_text("issues: timeout\n")

        before = snapshot_state(session_dir)

        rc, out, err = run_preflight(["snapshot", "--dir", str(session_dir)])

        after = snapshot_state(session_dir)

        return (before == after), \
               f"Files changed: before={sorted(before)}, after={sorted(after)}"


ok, detail = test_snapshot_phase_no_side_effects()
t("snapshot phase: does not create or modify files (names, bytes, mtimes)", ok, detail)


print()
h.summarize_and_exit()
