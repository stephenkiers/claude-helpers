#!/usr/bin/env python3
"""
Black-box tests for scripts/implement-ledger.py `locate --since` (the bounded, repo-wide scan).

Driven only through the CLI (subprocess). Every case runs under a throwaway HOME so the ledger's
state lives under a temp .claude/state tree, never the real ~/.claude.

Covers:
1. Happy path: a readable pointer inside the window is found (exit 0).
2. Fail closed: an unreadable pointer anywhere in the scan exits 2 with an 'unreadable' message on
   stderr, and nothing is reported on stdout.
3. Fail closed: a non-finite created_epoch (NaN, Infinity) exits 2 the same way.
4. Distinct hint: a bounded locate with no record in the window exits 3 and names the pruning
   window on stderr; the no-pointer run-key lookup does not.
5. Tie-break: two records with equal created_epoch resolve to the same run whichever order they
   were created in.

Run with: python3 tests/test_implement_ledger_locate_bounded.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

LEDGER = REPO_ROOT / "scripts" / "implement-ledger.py"
WINDOW = "2020-01-01T00:00:00Z"
FUTURE_WINDOW = "2099-01-01T00:00:00Z"
START_SHA = "0000000000000000000000000000000000000001"


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


class LedgerFixture:
    """A throwaway HOME, repo directory, and scratch root for one locate scenario."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.scratch_root = self.root / "scratch"
        for d in (self.home, self.repo, self.scratch_root):
            d.mkdir(parents=True)

    @property
    def state_dir(self):
        return self.home / ".claude" / "state" / "implement-with-haiku"

    def init_run(self, run_key):
        """Create a real run via `init`; the pointer lands in the state dir."""
        scratch = self.scratch_root / run_key
        scratch.mkdir()
        return run_ledger(
            ["init", "--scratch", str(scratch), "--start-sha", START_SHA, "--run-key", run_key],
            self.home,
            self.repo,
        )

    def read_pointer(self, run_key):
        return json.loads((self.state_dir / (run_key + ".pointer")).read_text())

    def write_pointer(self, run_key, record):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / (run_key + ".pointer")).write_text(json.dumps(record))

    def locate_since(self, since=WINDOW):
        return run_ledger(["locate", "--repo", str(self.repo), "--since", since], self.home, self.repo)


def test_readable_pointer_in_window_is_found(h, tmp):
    fx = LedgerFixture(tmp)
    rc, _, err = fx.init_run("happy-run")
    h.test_result("happy: init writes a readable pointer", rc == 0, err.strip())
    rc, out, err = fx.locate_since()
    h.test_result("happy: bounded locate finds the in-window run (exit 0)",
                  rc == 0 and "RUN_KEY: happy-run" in out, f"rc={rc} stderr={err.strip()}")


def test_unreadable_pointer_fails_closed(h, tmp):
    fx = LedgerFixture(tmp)
    fx.init_run("good-run")
    fx.state_dir.mkdir(parents=True, exist_ok=True)
    (fx.state_dir / "corrupt.pointer").write_text("not json{")
    rc, out, err = fx.locate_since()
    h.test_result("unreadable pointer: bounded locate exits 2 (fail closed)",
                  rc == 2, f"rc={rc} stdout={out.strip()}")
    h.test_result("unreadable pointer: stderr says the record is unreadable",
                  "unreadable" in err, f"stderr={err.strip()}")
    h.test_result("unreadable pointer: no run is reported on stdout",
                  "RUN_KEY:" not in out, f"stdout={out.strip()}")


def test_non_finite_created_epoch_fails_closed(h, tmp):
    for label, value in (("NaN", float("nan")), ("Infinity", float("inf"))):
        fx = LedgerFixture(Path(tmp) / label)
        fx.init_run("seed-" + label.lower())
        record = fx.read_pointer("seed-" + label.lower())
        record["run_key"] = "bad-" + label.lower()
        record["created_epoch"] = value
        fx.write_pointer("bad-" + label.lower(), record)
        rc, out, err = fx.locate_since()
        h.test_result(f"non-finite created_epoch ({label}): bounded locate exits 2",
                      rc == 2, f"rc={rc} stdout={out.strip()} stderr={err.strip()}")


def test_empty_window_emits_distinct_hint(h, tmp):
    fx = LedgerFixture(Path(tmp) / "empty")
    rc_nokey, _, err_nokey = run_ledger(["locate", "--repo", str(fx.repo), "--run-key", "missing"],
                                        fx.home, fx.repo)
    rc_since, out_since, err_since = fx.locate_since()
    h.test_result("empty window (no records): bounded locate exits 3",
                  rc_since == 3, f"rc={rc_since} stdout={out_since.strip()}")
    h.test_result("empty window: stderr names the pruning window",
                  "pruned" in err_since and "14 days" in err_since, f"stderr={err_since.strip()}")
    h.test_result("empty window: hint differs from the plain no-pointer message",
                  rc_nokey == 3 and err_since.strip() != err_nokey.strip(),
                  f"since={err_since.strip()} nokey={err_nokey.strip()}")

    fx2 = LedgerFixture(Path(tmp) / "future")
    fx2.init_run("present-run")
    rc_future, _, err_future = fx2.locate_since(FUTURE_WINDOW)
    h.test_result("empty window (records older than since): bounded locate exits 3",
                  rc_future == 3, f"rc={rc_future} stderr={err_future.strip()}")
    h.test_result("empty window (records older than since): stderr names the pruning window",
                  "pruned" in err_future, f"stderr={err_future.strip()}")


def test_equal_created_epoch_tie_break_is_order_independent(h, tmp):
    # One init supplies a real pointer shape; a second init in the same HOME would supersede the first,
    # so the two tied records are written directly from that template, in the order under test.
    epoch = int(time.time()) - 600
    results = []
    for label, order in (("ab", ("tie-a", "tie-b")), ("ba", ("tie-b", "tie-a"))):
        fx = LedgerFixture(Path(tmp) / label)
        rc, _, err = fx.init_run("template")
        if rc != 0:
            h.test_result(f"tie-break setup ({label}): init template", False, err.strip())
            return
        template = fx.read_pointer("template")
        (fx.state_dir / "template.pointer").unlink()
        for key in order:
            record = dict(template)
            record["run_key"] = key
            record["created_epoch"] = epoch
            fx.write_pointer(key, record)
        rc, out, err = fx.locate_since()
        run_lines = [line for line in out.splitlines() if line.startswith("RUN_KEY:")]
        results.append((rc, run_lines))
    h.test_result("tie-break: equal created_epoch locate exits 0 in both creation orders",
                  all(rc == 0 for rc, _ in results), f"results={results}")
    h.test_result("tie-break: same run is chosen regardless of creation order",
                  len(results) == 2 and results[0][1] == results[1][1] and len(results[0][1]) == 1,
                  f"results={results}")


def main():
    h = Harness("implement-ledger.py locate --since (bounded scan)")
    cases = (
        test_readable_pointer_in_window_is_found,
        test_unreadable_pointer_fails_closed,
        test_non_finite_created_epoch_fails_closed,
        test_empty_window_emits_distinct_hint,
        test_equal_created_epoch_tie_break_is_order_independent,
    )
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            case(h, tmp)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
