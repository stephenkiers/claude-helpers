#!/usr/bin/env python3
"""
Spec-driven tests for scripts/flow-verify-receipt.py DISCARDED_ADDED_FILES parsing.

Covers behaviours from feature #315 action plan:
- F2: DISCARDED_ADDED_FILES template shape and parser handling (both spaces and bare paths)
- F16: check_discarded_files git error handling and path validation
- F18: parse_receipt_field last-wins behavior and header-block anchoring

Run with: python3 tests/test_flow_verify_receipt_discarded_files.py
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def since_timestamp():
    """ISO-8601 UTC an hour ago."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ImplementFixture:
    """Minimal fixture for implement step with git repo."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.flow = self.root / "flow"
        self.receipt = self.flow / "steps" / "06-implement.md"

        self.home.mkdir(parents=True)
        for sub in ("steps", "questions", "answers"):
            (self.flow / sub).mkdir(parents=True)

        # Create minimal git repo
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=self.repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )
        # Initial commit
        (self.repo / "initial.txt").write_text("initial\n")
        subprocess.run(
            ["git", "add", "initial.txt"],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )

    def write_receipt(self, body_lines, path=None):
        """Write an implement receipt with given lines."""
        target = Path(path) if path else self.receipt
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(body_lines) + "\n"
        target.write_text(body, encoding="utf-8")

    def add_file(self, filename):
        """Add a file to the repo and commit."""
        (self.repo / filename).write_text(f"content of {filename}\n")
        subprocess.run(
            ["git", "add", filename], cwd=self.repo, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"add {filename}"],
            cwd=self.repo,
            capture_output=True,
            check=True,
        )


def run_verifier(fx, step="implement", receipt=None, **kwargs):
    """Run verifier. Returns (exit_code, stdout, stderr)."""
    args = [sys.executable, str(VERIFIER), "--step", step]
    if receipt:
        args += ["--receipt", str(receipt)]
    else:
        args += ["--receipt", str(fx.receipt)]
    args += ["--since", since_timestamp()]
    if hasattr(fx, "repo"):
        args += ["--repo", str(fx.repo)]
    if hasattr(fx, "flow"):
        args += ["--flow-dir", str(fx.flow)]

    for key, val in kwargs.items():
        if val is not None:
            arg_name = key.replace("_", "-")
            args.extend([f"--{arg_name}", str(val)])

    env = {**os.environ, "HOME": str(fx.home)}
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness(
        "flow-verify-receipt.py: DISCARDED_ADDED_FILES parsing (#315 F2, F16, F18)"
    )

    # ========================
    # F2: DISCARDED_ADDED_FILES template shape and parser
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        # Get current branch and base BEFORE adding files
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add a committed file (required for implement step to succeed)
        fx.add_file("kept.py")

        # Add files but don't commit them (so they're discarded)
        (fx.repo / "test.py").write_text("content of test.py\n")
        (fx.repo / "doc.md").write_text("content of doc.md\n")

        # F2a: DISCARDED_ADDED_FILES with template shape (two spaces + dash + path)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "DISCARDED_ADDED_FILES:",
                "  - test.py",
                "  - doc.md",
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        h.test_result(
            "F2a: Template shape (two spaces + dash + path) parses successfully",
            rc == 0,
            f"exit {rc}, stderr: {err[:100]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add a committed file (required for implement step to succeed)
        fx.add_file("kept.py")

        # Add file but don't commit it (so it's discarded)
        (fx.repo / "single.py").write_text("content of single.py\n")

        # F2b: DISCARDED_ADDED_FILES with bare shape (just path, no leading spaces/dash)
        # Note: the spec says parser accepts both shapes
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "DISCARDED_ADDED_FILES:",
                "single.py",
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        # Parser should accept both template and bare shapes
        h.test_result(
            "F2b: Bare shape (path only, no dash) also parses",
            rc == 0,
            f"exit {rc}, stderr: {err[:100]}",
        )

    # ========================
    # F16: check_discarded_files path validation
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add file after getting base (so we have commits to check)
        fx.add_file("good.py")

        # F16a: Absolute paths should be rejected
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "DISCARDED_ADDED_FILES:",
                "  - /absolute/path.py",
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        h.test_result(
            "F16a: Absolute paths are rejected",
            rc != 0,
            f"exit {rc} (should fail), stderr: {err[:100]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add file after getting base (so we have commits to check)
        fx.add_file("good.py")

        # F16b: Paths with .. should be rejected
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "DISCARDED_ADDED_FILES:",
                "  - ../escape.py",
                "Decision: OK",
                "<!-- step-end -->",
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        h.test_result(
            "F16b: Paths with .. are rejected",
            rc != 0,
            f"exit {rc} (should fail), stderr: {err[:100]}",
        )

    # ========================
    # F18: parse_receipt_field last-wins behavior
    # ========================

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        # Get base BEFORE adding files
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add file after getting base
        fx.add_file("file.py")

        # F18a: When a field appears multiple times, last occurrence wins
        # Test with Decision field appearing twice (last one should be used)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: WRONG",
                "Decision: OK",  # Last one should win
                "<!-- step-end -->",
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        h.test_result(
            "F18a: Last occurrence of Decision field wins",
            rc == 0,
            f"exit {rc} (OK should win), stderr: {err[:100]}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        fx = ImplementFixture(tmp)
        # Get base BEFORE adding files
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fx.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Add file after getting base
        fx.add_file("file.py")

        # F18b: Field parsing should be anchored to the header block
        # (before step-end marker, not after)
        fx.write_receipt(
            [
                "Implement summary",
                "FLOW_DIR: " + str(fx.flow),
                "Decision: OK",
                "<!-- step-end -->",
                "Decision: AFTER_END",  # This should not count
            ]
        )
        rc, out, err = run_verifier(fx, branch=branch, base=base)
        h.test_result(
            "F18b: Field parsing stops at step-end marker (header block anchoring)",
            rc == 0,
            f"exit {rc} (OK before marker should win), stderr: {err[:100]}",
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
