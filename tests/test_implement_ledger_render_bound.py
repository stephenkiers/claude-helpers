#!/usr/bin/env python3
"""
Black-box tests for scripts/implement-ledger.py `render` (the bare render and `render --flow-dir --step`).

Driven only through the CLI (subprocess). Every case runs under a throwaway HOME so the ledger's
state lives under a temp .claude/state tree, never the real ~/.claude. Flow directories are
created with log.md files containing begin/retry lines.

Covers:
1. Happy path: --repo matches, created_epoch is in-window, render succeeds (exit 0).
2. Foreign repo: record's repo differs from --repo, exits 3 with named reason.
3. Boundary: created_epoch exactly at since - 5 passes; since - 6 fails with before-attempt reason.
4. Non-finite created_epoch: NaN, Infinity, bool, or missing exits 3 with named reason.
5. Tombstone (finished run): a complete run finishes, renders with binding checks.
6. Superseded key: not found (exit 3).
7. Argument validation: --flow-dir without --step exits 2; --step without --flow-dir exits 2.
8. No begin/retry line: log.md with no matching line exits 3 with named reason.
9. Invalid key shape: rejected before any path join (exit 2).
10. Bare render: exact summary lines for a fresh run, and the bound render prints the same lines.
11. Documented render rows: DONE evidence first line, class from round-sizing, permitted and
    forbidden SKIPPED rows.
12. --repo compared by realpath: a symlink to the repo matches.
13. String or non-numeric created_epoch ("abc", "1700000000") is not a number: exit 3, never a crash.
14. Finish-event cross-check (bound render): no finish event warns and passes; the named run being the
    one that finished last passes; another run finishing last in the same attempt fails; a superseded
    run's finish event counts.

Each test runs in its own temp directory (no shared HOME), so one case's finished run cannot be in
another case's attempt window.

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
MECHANICAL_DONE_STEPS = ("round1", "integration-gate", "round-sizing", "full-check", "usage-check-final")

# The bare render of a fresh run (no ledger rows): class defaults to full, pause rows are not required.
BARE_EXPECTED_LINES = [
    "RUN_KEY: bare-run",
    "CLASS: full PAUSE: no",
    "round1: NOT RUN",
    "integration-gate: NOT RUN",
    "round-sizing: NOT RUN",
    "pause: skipped (full)",
    "usage-check-round1-join: skipped (full)",
    "fanout: NOT RUN",
    "round2-apply: NOT RUN",
    "partc-dedup: NOT RUN",
    "partc-reference-check: NOT RUN",
    "partc-mutation-a: NOT RUN",
    "partc-mutation-b: NOT RUN",
    "round3-followup: NOT RUN",
    "round4: NOT RUN",
    "full-check: NOT RUN",
    "usage-check-final: NOT RUN",
]


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
        """Run `finish` for a run (success tombstones it; an incomplete run records steps_skipped)."""
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

    def append_ledger(self, run_key, lines):
        """Append raw lines to the run's ledger.md (the scratch file init created)."""
        with open(self.scratch_root / run_key / "ledger.md", "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))

    def complete_mechanical_run(self, run_key):
        """Give the run a complete mechanical ledger: a non-empty evidence file and a DONE row per required step."""
        scratch = self.scratch_root / run_key
        rows = []
        for step in MECHANICAL_DONE_STEPS:
            evidence = scratch / f"{step}.txt"
            body = "class=mechanical\n" if step == "round-sizing" else f"{step} evidence\n"
            evidence.write_text(body, encoding="utf-8")
            rows.append(f"{run_key} DONE {step} {evidence}")
        self.append_ledger(run_key, rows)


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


def test_string_epoch_fails(h, tmp):
    """A string created_epoch is not a number: "abc" and "1700000000" both exit 3, never a crash."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()
    for value in ("abc", "1700000000"):
        key = f"str-{value}"
        rc = fx.init_run(key)
        h.test_result(f"string-epoch {value}: init succeeds", rc == 0)

        record = fx.read_pointer(key)
        record["created_epoch"] = value
        fx.write_pointer(key, record)

        rc, out, err = fx.render_bound(key)
        h.test_result(f"string-epoch {value}: render exits 3 (not a crash)", rc == 3, f"rc={rc} stderr={err.strip()}")
        h.test_result(f"string-epoch {value}: stderr names non-finite epoch",
                      "has no finite created_epoch" in err,
                      f"stderr={err.strip()}")


def test_tombstone_renders(h, tmp):
    """Tombstone (finished run): a complete run finishes into a tombstone and renders with binding checks."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("tombstone-run")
    h.test_result("tombstone: init succeeds", rc == 0)
    fx.complete_mechanical_run("tombstone-run")

    fx.create_log_with_begin()
    rc, _, err = fx.finish_run("tombstone-run")
    h.test_result("tombstone: finish exits 0 on a complete run", rc == 0, f"stderr={err.strip()}")
    h.test_result("tombstone: tombstone file exists",
                  (fx.state_dir / "tombstone-run.finished").is_file())

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


def test_bare_render_exact_lines(h, tmp):
    """Bare render of a fresh run prints exactly the documented summary; the bound render prints the same lines."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("bare-run")
    h.test_result("bare: init succeeds", rc == 0)
    expected = "\n".join(BARE_EXPECTED_LINES) + "\n"

    rc_bare, out_bare, err_bare = fx.render_bare("bare-run")
    h.test_result("bare: render exits 0", rc_bare == 0, f"stderr={err_bare.strip()}")
    h.test_result("bare: stdout is exactly the documented summary", out_bare == expected,
                  f"stdout={out_bare!r}")
    h.test_result("bare: stderr is empty", err_bare == "", f"stderr={err_bare.strip()}")

    fx.create_log_with_begin()
    rc_bound, out_bound, err_bound = fx.render_bound("bare-run")
    h.test_result("bare: bound render exits 0", rc_bound == 0, f"stderr={err_bound.strip()}")
    h.test_result("bare: bound render stdout matches the bare render", out_bound == out_bare,
                  f"stdout={out_bound!r}")


def test_documented_rows_render(h, tmp):
    """DONE rows show the evidence's first line; round-sizing sets CLASS; permitted and forbidden SKIPPED rows."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("rows-run")
    h.test_result("rows: init succeeds", rc == 0)
    scratch = fx.scratch_root / "rows-run"
    sizing = scratch / "round-sizing.txt"
    sizing.write_text("class=mechanical\nsized by hand\n", encoding="utf-8")
    usage = scratch / "usage-check-final.txt"
    usage.write_text("\nUSAGE-GATE: ok\n", encoding="utf-8")
    fx.append_ledger("rows-run", [
        f"rows-run DONE round-sizing {sizing}",
        f"rows-run DONE usage-check-final {usage}",
        "rows-run SKIPPED pause pause-not-requested",
        "rows-run SKIPPED partc-dedup no-new-tests",
    ])

    rc, out, err = fx.render_bare("rows-run")
    lines = out.splitlines()
    h.test_result("rows: render exits 0", rc == 0, f"stderr={err.strip()}")
    h.test_result("rows: CLASS comes from the round-sizing evidence",
                  "CLASS: mechanical PAUSE: no" in lines, f"stdout={out.strip()}")
    h.test_result("rows: DONE shows the first non-blank evidence line",
                  "round-sizing: class=mechanical" in lines and "usage-check-final: USAGE-GATE: ok" in lines,
                  f"stdout={out.strip()}")
    h.test_result("rows: a permitted SKIPPED row renders as skipped with its reason",
                  "pause: skipped (pause-not-requested)" in lines, f"stdout={out.strip()}")
    h.test_result("rows: a forbidden SKIPPED row renders as not permitted",
                  "partc-dedup: NOT PERMITTED (SKIPPED no-new-tests)" in lines, f"stdout={out.strip()}")
    h.test_result("rows: a required step with no row is NOT RUN",
                  "round1: NOT RUN" in lines, f"stdout={out.strip()}")


def test_repo_symlink_matches(h, tmp):
    """--repo is compared by realpath: a symlink to the recorded repo matches and renders (exit 0)."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("symlink-run")
    h.test_result("symlink: init succeeds", rc == 0)
    link = fx.root / "repo-link"
    os.symlink(str(fx.repo), str(link))

    fx.create_log_with_begin()
    rc, out, err = run_ledger(
        ["render", "--run-key", "symlink-run", "--repo", str(link), "--flow-dir", str(fx.flow_dir), "--step", "implement"],
        fx.home,
        fx.repo,
    )
    h.test_result("symlink: render via a symlinked --repo exits 0", rc == 0, f"rc={rc} stderr={err.strip()}")
    h.test_result("symlink: output is present", out.strip() != "", f"stdout={out.strip()}")


def test_superseded_key_not_found(h, tmp):
    """Superseded key: a run replaced by a newer init is not found by its key (exit 3)."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("old-run")
    h.test_result("superseded-key: first init succeeds", rc == 0)
    rc = fx.init_run("new-run")
    h.test_result("superseded-key: second init succeeds", rc == 0)
    h.test_result("superseded-key: old pointer was superseded",
                  (fx.state_dir / "old-run.superseded").is_file())

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("old-run")
    h.test_result("superseded-key: render exits 3", rc == 3, f"rc={rc}")
    h.test_result("superseded-key: stderr says not found", "not found" in err, f"stderr={err.strip()}")


def test_no_finish_event_warns(h, tmp):
    """Cross-check, no finish event: a bound render of an unfinished run passes (exit 0) with a warning."""
    fx = RenderBoundFixture(tmp)
    rc = fx.init_run("unfinished-run")
    h.test_result("no-event: init succeeds", rc == 0)

    fx.create_log_with_begin()
    rc, out, err = fx.render_bound("unfinished-run")
    h.test_result("no-event: render exits 0", rc == 0, f"rc={rc} stderr={err.strip()}")
    h.test_result("no-event: stderr warns that no finish event exists",
                  "warning: no finish event recorded" in err, f"stderr={err.strip()}")
    h.test_result("no-event: stdout still carries the summary", "RUN_KEY: unfinished-run" in out,
                  f"stdout={out.strip()}")


def test_named_run_finished_last_passes(h, tmp):
    """Cross-check: the named run is the one that finished last in the attempt, so the render passes silently."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()
    rc = fx.init_run("clean-run")
    h.test_result("clean-last: init succeeds", rc == 0)
    fx.complete_mechanical_run("clean-run")
    rc, _, err = fx.finish_run("clean-run")
    h.test_result("clean-last: finish exits 0", rc == 0, f"stderr={err.strip()}")

    rc, out, err = fx.render_bound("clean-run")
    h.test_result("clean-last: render exits 0", rc == 0, f"stderr={err.strip()}")
    h.test_result("clean-last: no cross-check warning or refusal",
                  "warning" not in err and "NOT FOUND" not in err, f"stderr={err.strip()}")


def test_steps_skipped_named_run_passes(h, tmp):
    """Cross-check: an incomplete run that finished (steps_skipped) and is named renders (exit 0)."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()
    rc = fx.init_run("skipped-run")
    h.test_result("skipped-named: init succeeds", rc == 0)
    rc, _, err = fx.finish_run("skipped-run")
    h.test_result("skipped-named: finish exits 1 on an incomplete run", rc == 1, f"rc={rc} stderr={err.strip()}")

    rc, out, err = fx.render_bound("skipped-run")
    h.test_result("skipped-named: render exits 0", rc == 0, f"rc={rc} stderr={err.strip()}")
    h.test_result("skipped-named: the incomplete summary is printed", "round1: NOT RUN" in out,
                  f"stdout={out.strip()}")


def test_other_run_finished_last_fails(h, tmp):
    """Cross-check: a clean run is named, but an incomplete run finished last in the attempt: refused (exit 3)."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()
    rc = fx.init_run("named-clean")
    h.test_result("other-last: init clean run succeeds", rc == 0)
    fx.complete_mechanical_run("named-clean")
    rc, _, err = fx.finish_run("named-clean")
    h.test_result("other-last: clean run finishes", rc == 0, f"stderr={err.strip()}")

    rc = fx.init_run("dirty-run")
    h.test_result("other-last: dirty run init succeeds", rc == 0)
    rc, _, _ = fx.finish_run("dirty-run")
    h.test_result("other-last: dirty run finish records steps_skipped", rc == 1)

    rc, out, err = fx.render_bound("named-clean")
    h.test_result("other-last: render of the clean key exits 3", rc == 3, f"rc={rc} stderr={err.strip()}")
    h.test_result("other-last: stderr says the key is not the finished run and names the other run",
                  "is not the run this step recorded finishing" in err and "dirty-run" in err,
                  f"stderr={err.strip()}")
    h.test_result("other-last: no summary on stdout", out.strip() == "", f"stdout={out.strip()}")


def test_superseded_finish_event_counts(h, tmp):
    """Cross-check: a run superseded in the attempt still counts; a newer unfinished run cannot be named."""
    fx = RenderBoundFixture(tmp)
    fx.create_log_with_begin()
    rc = fx.init_run("first-run")
    h.test_result("superseded-event: first init succeeds", rc == 0)
    rc, _, _ = fx.finish_run("first-run")
    h.test_result("superseded-event: first run finish records steps_skipped", rc == 1)

    rc = fx.init_run("second-run")
    h.test_result("superseded-event: second init succeeds", rc == 0)
    h.test_result("superseded-event: first pointer superseded by the second init",
                  (fx.state_dir / "first-run.superseded").is_file())

    rc, out, err = fx.render_bound("second-run")
    h.test_result("superseded-event: naming the unfinished second run exits 3", rc == 3,
                  f"rc={rc} stderr={err.strip()}")
    h.test_result("superseded-event: stderr names the superseded first run",
                  "first-run finished last" in err, f"stderr={err.strip()}")


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

    # Each case gets its own temp directory (and HOME), so no case sees another case's runs.
    cases = [
        test_happy_path_in_window,
        test_foreign_repo_fails,
        test_boundary_epoch_passes,
        test_boundary_epoch_fails,
        test_nan_epoch_fails,
        test_infinity_epoch_fails,
        test_bool_epoch_fails,
        test_missing_created_epoch_fails,
        test_string_epoch_fails,
        test_tombstone_renders,
        test_flow_dir_without_step_fails,
        test_step_without_flow_dir_fails,
        test_no_begin_line_fails,
        test_invalid_key_shape_fails,
        test_bare_render_exact_lines,
        test_documented_rows_render,
        test_repo_symlink_matches,
        test_superseded_key_not_found,
        test_no_finish_event_warns,
        test_named_run_finished_last_passes,
        test_steps_skipped_named_run_passes,
        test_other_run_finished_last_fails,
        test_superseded_finish_event_counts,
        test_render_uses_given_repo_not_cwd,
    ]
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            case(h, tmp)

    h.summarize_and_exit()
