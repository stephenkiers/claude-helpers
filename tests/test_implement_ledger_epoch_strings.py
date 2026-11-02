#!/usr/bin/env python3
"""
Black-box tests for the created_epoch finiteness check in scripts/implement-ledger.py (#346).

The check that rejects a non-finite created_epoch must not raise when the stored value is a string
(numeric-looking or not). Each case goes through the CLI (subprocess) under a throwaway HOME, once through
`render --flow-dir --step` (the bound render) and once through the bounded `locate --since` scan. A
traceback on stderr, or exit 1 (Python's uncaught-exception code), is a failure.

Covers:
1. Bound render: string created_epoch values "", "nan", "1e400", and " 1700000000 " exit 3 with no traceback.
2. Bounded locate: a non-numeric string created_epoch ("abc", "") does not crash the scan: no traceback,
   exit is not 0 for the bad record's run, and the bad run key is never named on stdout.

Run with: python3 tests/test_implement_ledger_epoch_strings.py
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
START_SHA = "0000000000000000000000000000000000000001"
WINDOW = "2020-01-01T00:00:00Z"
RENDER_STRING_EPOCHS = ("", "nan", "1e400", " 1700000000 ")
LOCATE_STRING_EPOCHS = ("abc", "")


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


class EpochFixture:
    """A throwaway HOME, repo, scratch root, and flow directory for one scenario."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.scratch_root = self.root / "scratch"
        self.flow_dir = self.home / ".claude" / "flows" / "fake-repo" / "epoch-flow"
        for d in (self.home, self.repo, self.scratch_root, self.flow_dir):
            d.mkdir(parents=True, exist_ok=True)

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

    def set_epoch(self, run_key, value):
        """Replace the run's created_epoch with a raw value (a string here) in its pointer."""
        record = self.read_pointer(run_key)
        record["created_epoch"] = value
        self.write_pointer(run_key, record)

    def write_begin_line(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.flow_dir / "log.md").write_text(f"{now} implement begin\n")

    def render_bound(self, run_key):
        return run_ledger(
            ["render", "--run-key", run_key, "--repo", str(self.repo), "--flow-dir", str(self.flow_dir),
             "--step", "implement"],
            self.home,
            self.repo,
        )

    def locate_since(self):
        return run_ledger(["locate", "--repo", str(self.repo), "--since", WINDOW], self.home, self.repo)


def test_bound_render_string_epoch_exits_3_without_traceback(h, tmp):
    """Bound render of a run whose created_epoch is a string exits 3 with no traceback, for each value."""
    fx = EpochFixture(Path(tmp) / "render")
    fx.write_begin_line()
    for index, value in enumerate(RENDER_STRING_EPOCHS):
        label = repr(value)
        key = f"render-{index}"
        rc, _, err = fx.init_run(key)
        h.test_result(f"render string epoch {label}: init succeeds", rc == 0, err.strip())
        fx.set_epoch(key, value)

        rc, out, err = fx.render_bound(key)
        h.test_result(f"render string epoch {label}: exits 3 (refused, not crashed)",
                      rc == 3, f"rc={rc} stdout={out.strip()} stderr={err.strip()}")
        h.test_result(f"render string epoch {label}: no traceback and no summary on stdout",
                      "Traceback" not in err and out.strip() == "",
                      f"stdout={out.strip()} stderr={err.strip()[-300:]}")


def test_bounded_locate_non_numeric_string_epoch_does_not_crash(h, tmp):
    """Bounded locate with a non-numeric string created_epoch: no traceback, and the bad run is never named."""
    for index, value in enumerate(LOCATE_STRING_EPOCHS):
        label = repr(value)
        fx = EpochFixture(Path(tmp) / f"locate-{index}")
        rc, _, err = fx.init_run("seed-good")
        h.test_result(f"locate string epoch {label}: seed init succeeds", rc == 0, err.strip())
        rc, _, err = fx.init_run("bad-str")
        h.test_result(f"locate string epoch {label}: bad-record init succeeds", rc == 0, err.strip())
        fx.set_epoch("bad-str", value)

        rc, out, err = fx.locate_since()
        h.test_result(f"locate string epoch {label}: no traceback",
                      "Traceback" not in err, f"rc={rc} stderr={err.strip()[-300:]}")
        h.test_result(f"locate string epoch {label}: exit is not the uncaught-exception code 1",
                      rc != 1, f"rc={rc} stdout={out.strip()} stderr={err.strip()[-300:]}")
        h.test_result(f"locate string epoch {label}: the bad run is never named on stdout",
                      "RUN_KEY: bad-str" not in out, f"stdout={out.strip()}")


def main():
    h = Harness("implement-ledger.py created_epoch string values (#346)")
    cases = (
        test_bound_render_string_epoch_exits_3_without_traceback,
        test_bounded_locate_non_numeric_string_epoch_does_not_crash,
    )
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            case(h, tmp)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
