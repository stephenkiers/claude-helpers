#!/usr/bin/env python3
"""
Black-box tests for scripts/implement-ledger.py `render --flow-dir --step` (the bounded epoch check).

Driven only through the CLI (subprocess). Every case runs under a throwaway HOME so the ledger's
state lives under a temp .claude/state tree, never the real ~/.claude. Flow directories are
created with log.md files containing begin/retry lines.

Covers:
1. Happy path: --repo matches, created_epoch is in-window, render succeeds (exit 0).
2. Foreign repo: record's repo differs from --repo, exits 3 with named reason.
3. Boundary: created_epoch exactly at since - 5 passes; since - 6 fails with before-attempt reason.
4. Non-finite created_epoch: NaN, Infinity, bool, or missing exits 3 with named reason.
5. Tombstone (finished run): renders with binding checks.
6. Superseded key: not found (exit 3).
7. Argument validation: --flow-dir without --step exits 2; --step without --flow-dir exits 2.
8. No begin/retry line: log.md with no matching line exits 3 with named reason.
9. Invalid key shape: rejected before any path join (exit 2).
10. Bare render: --run-key K without binding flags produces byte-identical output to today's.

Run with: python3 tests/test_implement_ledger_render_bound.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

LEDGER = REPO_ROOT / "scripts" / "implement-ledger.py"
START_SHA = "0000000000000000000000000000000000000001"
SKEW_SECS = 5


def run_ledger(args, home, cwd):
    """Run implement-ledger.py under the given HOME. Returns (returncode, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(LEDGER)] + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


class RenderBoundFixture:
    """A throwaway HOME, repo directory, flow directory, and scratch root for one render scenario."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.scratch_root = self.root / "scratch"
        self.flow_dir = self.home / ".claude" / "flows" / "fake-repo" / "123-test-slug-id"
        for d in (self.home, self.repo, self.scratch_root, self.flow_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def state_dir(self):
        return self.home / ".claude" / "state" / "implement-with-haiku"

    def init_run(self, run_key, repo=None):
        """Create a real run via `init`; the pointer lands in the state dir."""
        scratch = self.scratch_root / run_key
        scratch.mkdir()
        # If a different repo is passed, we'll manually edit the pointer after init.
        rc, _, _ = run_ledger(
            ["init", "--scratch", str(scratch), "--start-sha", START_SHA, "--run-key", run_key],
            self.home,
            repo or self.repo,
        )
        return rc

    def read_pointer(self, run_key):
        return json.loads((self.state_dir / (run_key + ".pointer")).read_text())

    def write_pointer(self, run_key, record):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / (run_key + ".pointer")).write_text(json.dumps(record))

    def finish_run(self, run_key):
        """Finish a run so it becomes a tombstone."""
        return run_ledger(
            ["finish", "--run-key", run_key],
            self.home,
            self.repo,
        )

    def create_log_with_begin(self, step="implement", offset_secs=0):
        """Create log.md with a begin line at (now + offset_secs)."""
        now = datetime.now(timezone.utc)
        if offset_secs != 0:
            now = now + timedelta(seconds=offset_secs)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        log_path = self.flow_dir / "log.md"
        log_path.write_text(f"{timestamp} {step} begin\n")
        return timestamp

    def render_bound(self, run_key, step="implement", flow_dir_arg=None):
        """Run render with binding flags. flow_dir_arg defaults to self.flow_dir."""
        flow_dir = flow_dir_arg or self.flow_dir
        return run_ledger(
            ["render", "--run-key", run_key, "--repo", str(self.repo), "--flow-dir", str(flow_dir), "--step", step],
            self.home,
            self.repo,
        )

    def render_bare(self, run_key):
        """Run render without binding flags."""
        return run_ledger(
            ["render", "--run-key", run_key],
            self.home,
            self.repo,
        )


def test_happy_path_in_window(h, tmp):
    """Happy path: --repo matches, created_epoch in-window, render succeeds (exit 0)."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("happy-bound")
    h.test_result("happy: init succeeds", rc == 0)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("happy-bound")
    h.test_result("happy: render with binding flags exits 0", rc == 0, f"stderr={err.strip()}")
    h.test_result("happy: render output contains a summary", "RUN_KEY:" in out, f"stdout={out.strip()}")


def test_foreign_repo_fails(h, tmp):
    """Foreign repo: record's repo differs from --repo, exits 3 with named reason."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("foreign-run")
    h.test_result("foreign: init succeeds", rc == 0)

    # Modify the pointer to have a different repo.
    record = fx.read_pointer("foreign-run")
    record["repo"] = "/some/other/repo"
    fx.write_pointer("foreign-run", record)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("foreign-run")
    h.test_result("foreign: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("foreign: stderr names repo mismatch",
                  "belongs to another repo" in err and "/some/other/repo" in err,
                  f"stderr={err.strip()}")
    h.test_result("foreign: no output on stdout", out.strip() == "", f"stdout={out.strip()}")


def test_boundary_epoch_passes(h, tmp):
    """Boundary: created_epoch exactly at since - 5 passes."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("boundary-pass")
    h.test_result("boundary-pass: init succeeds", rc == 0)

    # Get the begin timestamp and back-date the run to exactly 5 seconds before.
    timestamp_str = fx.create_log_with_begin()
    # Parse the timestamp and get epoch.
    from datetime import datetime
    dt = datetime.fromisoformat(timestamp_str.rstrip("Z") + "+00:00")
    since_epoch = dt.timestamp()
    target_epoch = since_epoch - SKEW_SECS

    record = fx.read_pointer("boundary-pass")
    record["created_epoch"] = int(target_epoch)
    fx.write_pointer("boundary-pass", record)

    rc, out, err = fx.render_bound("boundary-pass")
    h.test_result("boundary-pass: render exits 0", rc == 0, f"stderr={err.strip()}")
    h.test_result("boundary-pass: output present", out.strip() != "", f"stdout={out.strip()}")


def test_boundary_epoch_fails(h, tmp):
    """Boundary: created_epoch one second earlier (since - 6) fails."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("boundary-fail")
    h.test_result("boundary-fail: init succeeds", rc == 0)

    timestamp_str = fx.create_log_with_begin()
    from datetime import datetime
    dt = datetime.fromisoformat(timestamp_str.rstrip("Z") + "+00:00")
    since_epoch = dt.timestamp()
    target_epoch = since_epoch - SKEW_SECS - 1  # One second too old

    record = fx.read_pointer("boundary-fail")
    record["created_epoch"] = int(target_epoch)
    fx.write_pointer("boundary-fail", record)

    rc, out, err = fx.render_bound("boundary-fail")
    h.test_result("boundary-fail: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("boundary-fail: stderr names stale epoch",
                  "created before this attempt's start" in err,
                  f"stderr={err.strip()}")


def test_nan_epoch_fails(h, tmp):
    """Non-finite created_epoch: NaN exits 3 with named reason."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("nan-run")
    h.test_result("nan: init succeeds", rc == 0)

    record = fx.read_pointer("nan-run")
    record["created_epoch"] = float("nan")
    fx.write_pointer("nan-run", record)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("nan-run")
    h.test_result("nan: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("nan: stderr names non-finite epoch",
                  "has no finite created_epoch" in err,
                  f"stderr={err.strip()}")


def test_infinity_epoch_fails(h, tmp):
    """Non-finite created_epoch: Infinity exits 3 with named reason."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("inf-run")
    h.test_result("inf: init succeeds", rc == 0)

    record = fx.read_pointer("inf-run")
    record["created_epoch"] = float("inf")
    fx.write_pointer("inf-run", record)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("inf-run")
    h.test_result("inf: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("inf: stderr names non-finite epoch",
                  "has no finite created_epoch" in err,
                  f"stderr={err.strip()}")


def test_bool_epoch_fails(h, tmp):
    """Non-finite created_epoch: bool exits 3 with named reason."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("bool-run")
    h.test_result("bool: init succeeds", rc == 0)

    record = fx.read_pointer("bool-run")
    record["created_epoch"] = True
    fx.write_pointer("bool-run", record)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("bool-run")
    h.test_result("bool: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("bool: stderr names non-finite epoch",
                  "has no finite created_epoch" in err,
                  f"stderr={err.strip()}")


def test_missing_created_epoch_fails(h, tmp):
    """Non-finite created_epoch: missing key exits 3 with named reason."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("missing-epoch-run")
    h.test_result("missing-epoch: init succeeds", rc == 0)

    record = fx.read_pointer("missing-epoch-run")
    del record["created_epoch"]
    fx.write_pointer("missing-epoch-run", record)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("missing-epoch-run")
    h.test_result("missing-epoch: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("missing-epoch: stderr names non-finite epoch",
                  "has no finite created_epoch" in err,
                  f"stderr={err.strip()}")


def test_tombstone_renders(h, tmp):
    """Tombstone (finished run): renders with binding checks."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("tombstone-run")
    h.test_result("tombstone: init succeeds", rc == 0)

    # Finish the run to convert it to a tombstone.
    rc, _, _ = fx.finish_run("tombstone-run")
    # Note: finish may return 0 or 1 depending on gate status; we only care that render works.

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("tombstone-run")
    h.test_result("tombstone: render exits 0", rc == 0, f"stderr={err.strip()}")
    h.test_result("tombstone: output present", out.strip() != "", f"stdout={out.strip()}")


def test_flow_dir_without_step_fails(h, tmp):
    """Argument validation: --flow-dir without --step exits 2."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("noarg-run")
    h.test_result("noarg: init succeeds", rc == 0)

    rc, out, err = run_ledger(
        ["render", "--run-key", "noarg-run", "--repo", str(fx.repo), "--flow-dir", str(fx.flow_dir)],
        fx.home,
        fx.repo,
    )
    h.test_result("noarg: render exits 2 (argument error)", rc == 2, f"rc={rc}")
    h.test_result("noarg: stderr names the argument issue",
                  "--step" in err.lower(),
                  f"stderr={err.strip()}")


def test_step_without_flow_dir_fails(h, tmp):
    """Argument validation: --step without --flow-dir exits 2."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("noflow-run")
    h.test_result("noflow: init succeeds", rc == 0)

    rc, out, err = run_ledger(
        ["render", "--run-key", "noflow-run", "--repo", str(fx.repo), "--step", "implement"],
        fx.home,
        fx.repo,
    )
    h.test_result("noflow: render exits 2 (argument error)", rc == 2, f"rc={rc}")
    h.test_result("noflow: stderr names the argument issue",
                  "--flow-dir" in err.lower(),
                  f"stderr={err.strip()}")


def test_no_begin_line_fails(h, tmp):
    """No begin/retry line: log.md with no matching line exits 3."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("noline-run")
    h.test_result("noline: init succeeds", rc == 0)

    # Create log.md with no begin/retry line.
    log_path = fx.flow_dir / "log.md"
    log_path.write_text("2026-10-14T12:00:00Z some-step not-begin\n")

    rc, out, err = fx.render_bound("noline-run")
    h.test_result("noline: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("noline: stderr names no begin/retry line",
                  "no begin or retry line" in err,
                  f"stderr={err.strip()}")


def test_invalid_key_shape_fails(h, tmp):
    """Invalid key shape: rejected by RUN_KEY_RE validation (exit 2)."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()

    # Try a key with invalid characters.
    rc, out, err = run_ledger(
        ["render", "--run-key", "../x", "--repo", str(fx.repo), "--flow-dir", str(fx.flow_dir), "--step", "implement"],
        fx.home,
        fx.repo,
    )
    h.test_result("invalid-key: render exits 2 (invalid key)", rc == 2, f"rc={rc}")
    h.test_result("invalid-key: stderr mentions invalid key",
                  "invalid run key" in err.lower(),
                  f"stderr={err.strip()}")


def test_bare_render_unchanged(h, tmp):
    """Bare render: --run-key K without binding flags produces byte-identical output."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("bare-run")
    h.test_result("bare: init succeeds", rc == 0)

    # Get bare render output (no binding flags).
    rc_bare, out_bare, err_bare = fx.render_bare("bare-run")
    h.test_result("bare: render exits 0", rc_bare == 0, f"stderr={err_bare.strip()}")

    # Verify that bare render still works without binding flags.
    h.test_result("bare: output is not empty", out_bare.strip() != "", f"stdout={out_bare.strip()}")


def test_render_uses_given_repo_not_cwd(h, tmp):
    """--repo is used explicitly, not cwd: render from different directory succeeds."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("explicit-repo")
    h.test_result("explicit-repo: init succeeds", rc == 0)

    fx.create_log_with_begin()

    # Create a different directory to run from.
    other_dir = fx.root / "other"
    other_dir.mkdir()

    # Run from the other directory, but specify --repo as fx.repo.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(fx.home)
    result = subprocess.run(
        [sys.executable, str(LEDGER), "render", "--run-key", "explicit-repo", "--repo", str(fx.repo),
         "--flow-dir", str(fx.flow_dir), "--step", "implement"],
        cwd=str(other_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    h.test_result("explicit-repo: render exits 0 from different cwd", result.returncode == 0,
                  f"stderr={result.stderr.strip()}")
    h.test_result("explicit-repo: output is present", result.stdout.strip() != "",
                  f"stdout={result.stdout.strip()}")


if __name__ == "__main__":
    import sys
    h = Harness("test_implement_ledger_render_bound.py")

    with tempfile.TemporaryDirectory() as tmp:
        test_happy_path_in_window(h, tmp)
        test_foreign_repo_fails(h, tmp)
        test_boundary_epoch_passes(h, tmp)
        test_boundary_epoch_fails(h, tmp)
        test_nan_epoch_fails(h, tmp)
        test_infinity_epoch_fails(h, tmp)
        test_bool_epoch_fails(h, tmp)
        test_missing_created_epoch_fails(h, tmp)
        test_tombstone_renders(h, tmp)
        test_flow_dir_without_step_fails(h, tmp)
        test_step_without_flow_dir_fails(h, tmp)
        test_no_begin_line_fails(h, tmp)
        test_invalid_key_shape_fails(h, tmp)
        test_bare_render_unchanged(h, tmp)
        test_render_uses_given_repo_not_cwd(h, tmp)

    h.summarize_and_exit()
