#!/usr/bin/env python3
"""Unit tests for scripts/wait-files.py, the bounded foreground child wait (#532).

Run with: python3 tests/test_wait_files.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT  # noqa: E402

_spec = importlib.util.spec_from_file_location("_wait_files", REPO_ROOT / "scripts" / "wait-files.py")
assert _spec is not None and _spec.loader is not None
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)

SENT = "<!-- report-end -->"


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start
        self.sleeps = []

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


def write(path: Path, text: str, mtime: float) -> None:
    path.write_text(text)
    os.utime(str(path), (mtime, mtime))


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = wf.main(argv)
    return code, out.getvalue(), err.getvalue()


def main() -> None:
    h = Harness("WAIT-FILES TEST SUITE")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        since = 1_000_000

        # mark
        sd = d / "state-mark"
        e1 = wf.mark("implement:round1", sd, now=since)
        e2 = wf.mark("implement:fanout", sd, now=since + 7)
        data = json.loads((sd / wf.SINCE_FILE).read_text())
        h.test_result("mark returns the epoch", e1 == since and e2 == since + 7)
        h.test_result("mark keeps other phases", data == {"implement:round1": since, "implement:fanout": since + 7})
        h.test_result("recorded_since reads the mark", wf.recorded_since("implement:fanout", sd) == since + 7)
        h.test_result("recorded_since None for unknown phase", wf.recorded_since("x", sd) is None)

        # file_state
        f = d / "r.md"
        h.test_result("missing file", wf.file_state(f, since, SENT) == "missing")
        write(f, "body\n" + SENT + "\n", since - 10)
        h.test_result("stale file (mtime before since)", wf.file_state(f, since, SENT) == "stale")
        write(f, "body still writing\n", since + 1)
        h.test_result("fresh file without sentinel is writing", wf.file_state(f, since, SENT) == "writing")
        write(f, "body\n" + SENT + "\n\n  \n", since + 1)
        h.test_result("sentinel on last non-empty line is ok", wf.file_state(f, since, SENT) == "ok")
        write(f, SENT + "\ntrailing text\n", since + 1)
        h.test_result("sentinel not last is writing", wf.file_state(f, since, SENT) == "writing")
        write(f, "7\n", since + 1)
        h.test_result("--exists mode: fresh file is ok", wf.file_state(f, since, None) == "ok")
        write(f, "7\n", since - 1)
        h.test_result("--exists mode: stale file is stale", wf.file_state(f, since, None) == "stale")

        # wait_once: closed after a file appears mid-slice
        sd = d / "state-closed"
        a, b = d / "a.md", d / "b.md"
        write(a, "x\n" + SENT + "\n", since + 5)
        fc = FakeClock(since + 10)
        orig_sleep = fc.sleep

        def sleep_then_write(secs: float) -> None:
            orig_sleep(secs)
            if fc.now >= since + 30 and not b.exists():
                write(b, "y\n" + SENT + "\n", fc.now)

        res = wf.wait_once("p:closed", sd, [("a", a), ("b", b)], since, 1800, 540, sentinel=SENT,
                           clock=fc.clock, sleep=sleep_then_write)
        h.test_result("wait_once closes when every id is ok", res.status == "closed" and res.outstanding == [])
        h.test_result("wait_once polls in 5 s steps", all(s <= 5.0 for s in fc.sleeps) and len(fc.sleeps) >= 1)
        rows = [json.loads(x) for x in (sd / wf.TELEMETRY_FILE).read_text().splitlines()]
        h.test_result("closed writes one telemetry row", len(rows) == 1 and rows[0]["outcome"] == "closed")
        h.test_result("closed resets attempts to 0",
                      json.loads((sd / wf.ATTEMPTS_FILE).read_text()) == {"p:closed": 0})

        # wait_once: pending at slice end, no telemetry
        sd = d / "state-pending"
        c = d / "c.md"
        fc = FakeClock(since)
        res = wf.wait_once("p:pending", sd, [("c", c)], since, 3600, 300, sentinel=SENT,
                           clock=fc.clock, sleep=fc.sleep)
        h.test_result("slice end before deadline is pending", res.status == "pending" and res.outstanding == ["c"])
        h.test_result("slice lasts the timeout", fc.now - since == 300)
        h.test_result("pending writes no telemetry", not (sd / wf.TELEMETRY_FILE).exists())

        # wait_once: deadline, attempts increment, then reset on closed
        sd = d / "state-deadline"
        fc = FakeClock(since + 1790)
        res = wf.wait_once("p:dl", sd, [("c", c)], since, 1800, 540, sentinel=SENT,
                           clock=fc.clock, sleep=fc.sleep)
        h.test_result("deadline status at since+deadline", res.status == "deadline" and res.attempt == 1)
        h.test_result("deadline sleep never overshoots", fc.now == since + 1800)
        fc = FakeClock(since + 5000)
        res2 = wf.wait_once("p:dl", sd, [("c", c)], since, 1800, 540, sentinel=SENT,
                            clock=fc.clock, sleep=fc.sleep)
        h.test_result("second deadline is attempt 2", res2.attempt == 2)
        rows = [json.loads(x) for x in (sd / wf.TELEMETRY_FILE).read_text().splitlines()]
        h.test_result("deadline rows record outstanding ids",
                      [r["outcome"] for r in rows] == ["deadline", "deadline"] and rows[0]["outstanding"] == ["c"])
        write(c, SENT + "\n", since + 1)
        wf.wait_once("p:dl", sd, [("c", c)], since, 1800, 540, sentinel=SENT,
                     clock=FakeClock(since + 6000).clock, sleep=lambda s: None)
        h.test_result("closed resets the deadline counter",
                      json.loads((sd / wf.ATTEMPTS_FILE).read_text())["p:dl"] == 0)

        # wait_once never touches watched files
        before = c.read_text(), os.stat(str(c)).st_mtime
        h.test_result("watched file untouched", (c.read_text(), os.stat(str(c)).st_mtime) == before)

        # CLI
        sd = d / "state-cli"
        code, out, _ = run_cli(["mark", "--phase", "q:x", "--state-dir", str(sd)])
        h.test_result("cli mark exits 0 and prints epoch", code == 0 and out.strip().isdigit())
        ok = d / "ok.md"
        write(ok, "z\n" + SENT + "\n", int(out.strip()) + 1)
        code, out, _ = run_cli(["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60",
                                "--sentinel", SENT, "ok=" + str(ok)])
        h.test_result("cli wait closed exits 0", code == 0 and out.strip() == "closed: 1 ids")
        code, out, _ = run_cli(["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "1",
                                "--since", "1", "--exists", "gone=" + str(d / "gone")])
        h.test_result("cli wait past deadline exits 4", code == 4 and out.startswith("deadline: outstanding gone"))
        code, out, _ = run_cli(["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "1",
                                "--since", "1", "--exists", "gone=" + str(d / "gone")])
        h.test_result("cli repeated deadline prints loop line", code == 4 and "loop: repeated child-wait deadline" in out)

        usage_cases = {
            "no subcommand": [],
            "timeout below floor": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60",
                                    "--timeout", "60", "--exists", "a=/x"],
            "timeout above cap": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60",
                                  "--timeout", "600", "--exists", "a=/x"],
            "no mark and no since": ["wait", "--phase", "never", "--state-dir", str(sd), "--deadline", "60",
                                     "--exists", "a=/x"],
            "both modes": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60",
                           "--exists", "--sentinel", "s", "a=/x"],
            "neither mode": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60", "a=/x"],
            "bad pair": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60", "--exists", "nopath"],
            "duplicate id": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60", "--exists",
                             "a=/x", "a=/y"],
            "blank sentinel": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "60",
                               "--sentinel", "  ", "a=/x"],
            "zero deadline": ["wait", "--phase", "q:x", "--state-dir", str(sd), "--deadline", "0", "--exists", "a=/x"],
        }
        for name, argv in usage_cases.items():
            code, _, err = run_cli(argv)
            h.test_result("cli usage error exits 2: " + name, code == 2 and "usage error" in err)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
