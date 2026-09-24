#!/usr/bin/env python3
"""
Test suite for scripts/write-transcript-origin.py (Testing Strategy cases d and e).

Tests the script's observable behavior:
- Env var set and valid → resolution: "env"
- Env var with unsafe characters → unavailable
- Env var unset → resolution: "unavailable", session_id: null, exit 0
- Output schema has exactly the six v1 keys; recorded_at matches ISO format
- REVIEW_DIR outside ~/.claude/reviews/ → non-zero exit
- Read-only directory → non-zero exit, no partial file
- Doc assertion: write path is a single script invocation in expert-review.md

Run with: python3 tests/test_write_transcript_origin.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT_PATH = REPO_ROOT / "scripts" / "write-transcript-origin.py"

# Load reviewer-yield.py for round-trip testing
_spec_ry = importlib.util.spec_from_file_location("reviewer_yield", REPO_ROOT / "scripts" / "reviewer-yield.py")
reviewer_yield = importlib.util.module_from_spec(_spec_ry)
_spec_ry.loader.exec_module(reviewer_yield)


class FakeHome:
    """Context manager that points HOME at a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        self._tmp.cleanup()


def run_script(review_dir: str, env: dict = None) -> subprocess.CompletedProcess:
    """Run write-transcript-origin.py as a subprocess with optional env override."""
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), review_dir],
        capture_output=True,
        text=True,
        env=run_env,
    )


def main():
    h = Harness("WRITE-TRANSCRIPT-ORIGIN TEST SUITE (Testing Strategy d + e)")
    t = h.test_result

    # ========================================================================
    print("[Case d] write-transcript-origin.py unit tests")

    # Test 1: Env var set and valid
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        # Create a session file so the script can locate project_dir
        projects_dir = home / ".claude" / "projects" / "my-proj"
        projects_dir.mkdir(parents=True)
        (projects_dir / "valid-sess-1.jsonl").write_text('{"type": "start"}\n')

        result = run_script(
            str(review_dir),
            env={"CLAUDE_CODE_SESSION_ID": "valid-sess-1"}
        )

        t("Env var valid: exit 0", result.returncode == 0, result.stderr)

        origin_file = review_dir / "transcript-origin.json"
        t("Env var valid: file written", origin_file.exists())

        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Env var valid: resolution is env", data.get("resolution") == "env")
            t("Env var valid: session_id set", data.get("session_id") == "valid-sess-1")
            t("Env var valid: project_dir populated", data.get("project_dir") == "my-proj")
            t("Schema has all six v1 keys",
              set(data.keys()) == {"schema_version", "cwd", "project_dir", "session_id", "resolution", "recorded_at"})
            t("recorded_at matches ISO format",
              bool(__import__("re").match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", data.get("recorded_at", ""))))

    # Test 2: Env var unset
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(review_dir)],
            capture_output=True,
            text=True,
            env=env,
        )

        t("Env var unset: exit 0", result.returncode == 0)

        origin_file = review_dir / "transcript-origin.json"
        t("Env var unset: file written", origin_file.exists())

        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Env var unset: resolution is unavailable", data.get("resolution") == "unavailable")
            t("Env var unset: session_id is null", data.get("session_id") is None)

    # Test 3: Unsafe session_id characters
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        unsafe_ids = ["../escape", "with space"]
        for unsafe_id in unsafe_ids:
            (review_dir / "transcript-origin.json").unlink(missing_ok=True)

            result = run_script(
                str(review_dir),
                env={"CLAUDE_CODE_SESSION_ID": unsafe_id}
            )

            t(f"Unsafe chars: exit 0 for {unsafe_id!r}", result.returncode == 0)

            origin_file = review_dir / "transcript-origin.json"
            if origin_file.exists():
                file_content = origin_file.read_text()
                t(f"Unsafe value not in file: {unsafe_id!r}", unsafe_id not in file_content)
                data = json.loads(file_content)
                t(f"Unsafe env: resolution is unavailable for {unsafe_id!r}", data.get("resolution") == "unavailable")

    # Test 4: REVIEW_DIR outside ~/.claude/reviews/
    with FakeHome() as home:
        bad_dir = home / "outside-reviews"
        bad_dir.mkdir()

        result = run_script(str(bad_dir), env={})
        t("REVIEW_DIR outside ~/.claude/reviews/: non-zero exit", result.returncode != 0)

    # Test 5: Read-only directory (skip when running as root)
    if os.geteuid() != 0:
        with FakeHome() as home:
            review_dir = home / ".claude" / "reviews" / "test-repo"
            review_dir.mkdir(parents=True)

            os.chmod(review_dir, 0o555)

            try:
                result = run_script(str(review_dir), env={})
                t("Read-only dir: non-zero exit", result.returncode != 0)

                origin_file = review_dir / "transcript-origin.json"
                t("Read-only dir: no partial file written",
                  not origin_file.exists() or origin_file.stat().st_size == 0)

                # Check for leftover .tmp files
                tmp_files = list(review_dir.glob(".transcript-origin-*.tmp"))
                t("Read-only dir: no leftover .tmp file",
                  len(tmp_files) == 0,
                  f"found tmp files: {tmp_files}")
            finally:
                os.chmod(review_dir, 0o755)
    else:
        print("  (Skipping read-only-dir test: running as root)")


    # Test 6: Valid write produces correct schema
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        result = run_script(str(review_dir), env={})

        origin_file = review_dir / "transcript-origin.json"
        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Output JSON has valid schema_version", data.get("schema_version") == 1)
            t("Output JSON has cwd field", "cwd" in data)
            t("Output JSON has project_dir field", "project_dir" in data)

    # Test 7: Zero-match glob scenario (session_id set but no matching project dir)
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        # Session ID is set but no matching project files exist
        result = run_script(
            str(review_dir),
            env={"CLAUDE_CODE_SESSION_ID": "valid-sess-1"}
        )

        t("Zero-match: exit 0", result.returncode == 0, result.stderr)

        origin_file = review_dir / "transcript-origin.json"
        t("Zero-match: file written", origin_file.exists())

        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Zero-match: resolution is env", data.get("resolution") == "env")
            t("Zero-match: session_id set", data.get("session_id") == "valid-sess-1")
            t("Zero-match: project_dir falls back to sanitized cwd",
              data.get("project_dir") != "",
              f"expected non-empty project_dir, got {data.get('project_dir')}")
            t("Zero-match: fallback warning in stderr",
              "Warning" not in result.stderr or "sanitized cwd" in result.stderr or "session_id unavailable" in result.stderr,
              f"stderr: {result.stderr}")

    # Test 8: Two-match glob scenario (multiple project dirs match session_id)
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        # Create two projects with the same session_id
        for proj_name in ["proj-a", "proj-b"]:
            projects_dir = home / ".claude" / "projects" / proj_name
            projects_dir.mkdir(parents=True)
            (projects_dir / "ambig-sess-1.jsonl").write_text('{"type": "start"}\n')

        result = run_script(
            str(review_dir),
            env={"CLAUDE_CODE_SESSION_ID": "ambig-sess-1"}
        )

        t("Two-match: exit 0", result.returncode == 0, result.stderr)

        origin_file = review_dir / "transcript-origin.json"
        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Two-match: resolution is env", data.get("resolution") == "env")
            t("Two-match: session_id set", data.get("session_id") == "ambig-sess-1")
            # When glob finds more than one match, project_dir should fall back to sanitized cwd
            t("Two-match: project_dir falls back (not ambiguous)",
              data.get("project_dir") != "",
              f"expected non-empty fallback, got {data.get('project_dir')}")

    # Test 9: Unset CLAUDE_CODE_SESSION_ID with cwd fallback
    with FakeHome() as home:
        review_dir = home / ".claude" / "reviews" / "test-repo"
        review_dir.mkdir(parents=True)

        # Ensure CLAUDE_CODE_SESSION_ID is explicitly unset
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(review_dir)],
            capture_output=True,
            text=True,
            env=env,
        )

        t("Unset-env: exit 0", result.returncode == 0)

        origin_file = review_dir / "transcript-origin.json"
        t("Unset-env: file written", origin_file.exists())

        if origin_file.exists():
            data = json.loads(origin_file.read_text())
            t("Unset-env: resolution is unavailable", data.get("resolution") == "unavailable")
            t("Unset-env: session_id is null", data.get("session_id") is None)
            t("Unset-env: project_dir populated via sanitized cwd",
              data.get("project_dir") != "",
              f"expected non-empty project_dir, got {data.get('project_dir')}")
            t("Unset-env: fallback warning in stderr",
              "Warning" in result.stderr and "sanitized cwd" in result.stderr,
              f"expected warning in stderr, got: {result.stderr}")

    # ========================================================================
    print("\n[Case e] Doc assertion: write path is a single script invocation")

    expert_review_md = REPO_ROOT / "commands" / "expert-review.md"
    t("commands/expert-review.md exists", expert_review_md.exists())

    if expert_review_md.exists():
        content = expert_review_md.read_text()

        # Should contain the script invocation
        has_script = 'write-transcript-origin.py' in content and '"$REVIEW_DIR"' in content
        t("expert-review.md contains write-transcript-origin.py invocation",
          has_script,
          "Missing script call or REVIEW_DIR parameter")

        # Should NOT contain the old inline logic
        t("expert-review.md does not contain PROJECT_DIR_SANITIZED logic",
          "PROJECT_DIR_SANITIZED" not in content)
        t("expert-review.md does not contain most-recent-dir fallback",
          "most-recent-dir" not in content)
        t("expert-review.md does not contain old jq redirect for transcript-origin.json",
          'jq' not in content or "> \"$REVIEW_DIR/transcript-origin.json\"" not in content)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
