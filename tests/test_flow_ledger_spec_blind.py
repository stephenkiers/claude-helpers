#!/usr/bin/env python3
"""
Spec-blind tests for scripts/flow-ledger.py (#287), written from the round-2 review action plan and
the shared contracts (C1-C11), not from the implementation. Every case runs the real script as a
subprocess against a temporary flow dir holding a stub state.json.

Covered (plan item -> section):
1. F1  argument errors exit 1 with "flow-ledger: append: " / "flow-ledger: mark: " and never say
       "human.jsonl line N"; argparse usage errors exit 2; corruption of an existing line uses the
       "flow-ledger: human.jsonl line N: " prefix; a torn last line says "missing trailing newline"
2. F2  eight concurrent appenders behind a held sidecar lock: all still blocked after 1.5s (positive
       evidence of blocking), no record written while held, then seq 1..8 land in file order
3. F4  a failed plain append (RLIMIT_FSIZE forces a write error) rolls the file back to its prior
       bytes, and the ledger stays usable afterwards
4. F12 human.jsonl and human.jsonl.lock are created 0o600 even under umask 0
5. F27 list (default and --json) on a missing file prints []; --md prints nothing
6. F6  list takes a shared lock: an exclusive holder blocks it; a shared holder does not
7. F19 ts is ISO-8601 UTC with a Z suffix (control: a valid Z ts is accepted; non-Z or non-ISO ts in
       an existing file is refused with line N)
8. F14 mark --status accepts only addressed and parked; superseded is refused (argparse, exit 2)
9. F20 extra validation-matrix rows: step pattern, attempt, supersede of a pending correction,
       addressed_by on supersede and on parked/mark-parked
10. F29 a ledger larger than one read chunk lists in full and accepts appends
11. F7  --text-stdin decodes UTF-8 regardless of locale / PYTHONIOENCODING

Skipped (not observable from a black-box run, or structural/doc-only, or forbidden to read):
F21, F22, F23 (structural doc assertions), F8, F10, F11, F13, F24, F25, F26 (prose and prompt
wording), F28 doc alignment, F15-F18 (code structure), F7 fsync and temp-file leak on error (not
observable without tracing), F5 beyond the list-lock behaviour in section 6.

Run with: python3 tests/test_flow_ledger_spec_blind.py
"""

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"
LEDGER_NAME = "human.jsonl"
LOCK_NAME = "human.jsonl.lock"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
LINE_N = re.compile(r"\bline \d+\b")


# ---------------------------------------------------------------- helpers

def make_flow(root, name):
    """Flow dir with a stub state.json (the script requires it)."""
    flow = Path(root) / name
    flow.mkdir(parents=True)
    (flow / "state.json").write_text("{}\n")
    return flow


def ledger_path(flow):
    return Path(flow) / LEDGER_NAME


def lock_path(flow):
    return Path(flow) / LOCK_NAME


def ledger_bytes(flow):
    path = ledger_path(flow)
    return path.read_bytes() if path.exists() else None


def load(flow):
    data = ledger_bytes(flow) or b""
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def rec(seq, **over):
    """A well-formed record in ticket key order, with overrides."""
    base = {
        "seq": seq,
        "ts": "2026-10-14T19:00:00Z",
        "step": "plan",
        "attempt": 1,
        "kind": "answer",
        "text": f"text {seq}",
        "transcript": None,
        "status": "pending",
        "addressed_by": None,
        "supersedes": None,
    }
    base.update(over)
    return base


def rec_line(seq, **over):
    return json.dumps(rec(seq, **over)) + "\n"


def seed(flow, lines):
    ledger_path(flow).write_bytes("".join(lines).encode("utf-8"))


def run_cli(argv, stdin=None, env=None, preexec=None):
    """Run argv. Returns (returncode, stdout, stderr) decoded as UTF-8."""
    kwargs = {"capture_output": True, "timeout": 60, "env": env, "preexec_fn": preexec}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    res = subprocess.run(argv, **kwargs)
    return res.returncode, res.stdout.decode("utf-8", "replace"), res.stderr.decode("utf-8", "replace")


def cli(sub, flow, *args, stdin=None, env=None, preexec=None):
    """Run `flow-ledger.py <sub> --flow-dir <flow> *args`."""
    argv = [sys.executable, str(LEDGER), sub, "--flow-dir", str(flow), *args]
    return run_cli(argv, stdin=stdin, env=env, preexec=preexec)


def append_args(kind="answer", step="plan", attempt=1, text="a response", extra=()):
    return [f"--kind={kind}", f"--step={step}", f"--attempt={attempt}", f"--text={text}", *extra]


# Runs the real script in-process after capping RLIMIT_FSIZE, so an oversized write raises EFBIG inside
# the script (the rollback path). A preexec_fn cap was observed not to bite on this macOS build even
# though the child reported the limit, so the cap is applied in-process instead.
LIMITED_RUNNER = (
    "import resource, runpy, signal, sys\n"
    "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
    "_, hard = resource.getrlimit(resource.RLIMIT_FSIZE)\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[1]), hard))\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(proc, timeout=60):
    """Wait for proc. Returns (returncode, stdout, stderr) decoded as UTF-8."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def no_line_number(err):
    """True when stderr carries no 'line N' locator (argument errors must not say human.jsonl line)."""
    return "human.jsonl line" not in err and LINE_N.search(err) is None


# ---------------------------------------------------------------- sections

def section_1_error_strings(h, root):
    print()
    print("[Section 1] F1 stderr prefixes, exit codes, torn/corrupt line locators")

    # Bad CLI arguments and invalid NEW records: exit 1, "flow-ledger: append: " prefix, no line N.
    append_cases = [
        ("step with uppercase", dict(step="Plan"), []),
        ("step with a space", dict(step="a b"), []),
        ("step empty", dict(step=""), []),
        ("step with underscore", dict(step="plan_1"), []),
        ("attempt zero", dict(attempt=0), []),
        ("attempt negative", dict(attempt=-1), []),
        ("absolute transcript", {}, ["--transcript=/etc/passwd"]),
        ("transcript with ..", {}, ["--transcript=steps/../x.md"]),
        ("correction without --supersedes", dict(kind="correction"), []),
        ("--supersedes without kind correction", {}, ["--supersedes=1"]),
        ("--supersedes a nonexistent seq", dict(kind="correction"), ["--supersedes=99"]),
        ("--supersedes 0", dict(kind="correction"), ["--supersedes=0"]),
    ]
    for label, kw, extra in append_cases:
        flow = make_flow(root, "append-arg-" + re.sub(r"\W+", "-", label))
        seed(flow, [rec_line(1)])
        before = ledger_bytes(flow)
        rc, _, err = cli("append", flow, *append_args(extra=extra, **kw))
        h.test_result(f"append rejects {label} with rc 1",
                      rc == 1, f"rc={rc} stderr={err.strip()[:160]!r}")
        h.test_result(f"append rejects {label} with 'flow-ledger: append: ' prefix",
                      err.startswith("flow-ledger: append: "), repr(err[:120]))
        h.test_result(f"append rejects {label} without a 'human.jsonl line N' locator",
                      no_line_number(err), repr(err[:160]))
        h.test_result(f"append rejects {label} and leaves the ledger byte-identical",
                      ledger_bytes(flow) == before)

    flow = Path(root) / "append-no-state"
    flow.mkdir()
    rc, _, err = run_cli([sys.executable, str(LEDGER), "append", "--flow-dir", str(flow),
                          *append_args()])
    h.test_result("append without state.json exits 1 with 'flow-ledger: append: ' prefix",
                  rc == 1 and err.startswith("flow-ledger: append: "), f"rc={rc} {err[:120]!r}")

    # Mark argument and state errors: exit 1, "flow-ledger: mark: " prefix, no line N.
    mark_cases = [
        ("addressed without --by", ["--seq=1", "--status=addressed"]),
        ("parked with --by", ["--seq=1", "--status=parked", "--by=steps/03.md"]),
        ("absolute --by", ["--seq=1", "--status=addressed", "--by=/etc/x.md"]),
        ("--by with a .. segment", ["--seq=1", "--status=addressed", "--by=steps/../x.md"]),
        ("nonexistent seq", ["--seq=99", "--status=parked"]),
        ("seq zero", ["--seq=0", "--status=parked"]),
        ("non-pending source (parked -> addressed)", ["--seq=2", "--status=addressed", "--by=steps/03.md"]),
    ]
    for label, args in mark_cases:
        flow = make_flow(root, "mark-arg-" + re.sub(r"\W+", "-", label))
        seed(flow, [rec_line(1), rec_line(2, status="parked")])
        before = ledger_bytes(flow)
        rc, _, err = cli("mark", flow, *args)
        h.test_result(f"mark rejects {label} with rc 1",
                      rc == 1, f"rc={rc} stderr={err.strip()[:160]!r}")
        h.test_result(f"mark rejects {label} with 'flow-ledger: mark: ' prefix",
                      err.startswith("flow-ledger: mark: "), repr(err[:120]))
        h.test_result(f"mark rejects {label} without a 'human.jsonl line N' locator",
                      no_line_number(err), repr(err[:160]))
        h.test_result(f"mark rejects {label} and leaves the ledger byte-identical",
                      ledger_bytes(flow) == before)

    # Argparse usage errors: exit 2, and no ledger locator.
    flow = make_flow(root, "usage-both-text")
    seed(flow, [rec_line(1)])
    before = ledger_bytes(flow)
    rc, _, err = cli("append", flow, "--kind=answer", "--step=plan", "--attempt=1", "--text=x",
                     "--text-stdin", stdin=b"y")
    h.test_result("append with both --text and --text-stdin exits 2", rc == 2, f"rc={rc}")
    h.test_result("usage error on append carries no 'human.jsonl line N' locator", no_line_number(err), repr(err[:160]))
    h.test_result("usage error leaves the ledger byte-identical", ledger_bytes(flow) == before)

    flow = make_flow(root, "usage-no-step")
    rc, _, err = cli("append", flow, "--kind=answer", "--attempt=1", "--text=x")
    h.test_result("append without --step exits 2", rc == 2, f"rc={rc}")
    h.test_result("usage error without --step carries no 'human.jsonl line N' locator",
                  no_line_number(err), repr(err[:160]))

    flow = make_flow(root, "usage-list-no-flowdir")
    rc, _, err = run_cli([sys.executable, str(LEDGER), "list"])
    h.test_result("list without --flow-dir exits 2", rc == 2, f"rc={rc}")

    # Corruption of an existing line: exit 1 with the exact locator prefix on every command.
    good1 = rec_line(1)
    good2 = rec_line(2, kind="gate_note")
    corrupt_cases = [
        ("invalid JSON on line 2", [good1, "{not json\n", good2], 2, None),
        ("torn trailing line 2 (no newline)", [good1, '{"seq": 2, "ts": "2026-10'], 2, "missing trailing newline"),
    ]
    for label, lines, line_no, needle in corrupt_cases:
        for cmd_label, call in [
            ("list", lambda f: cli("list", f, "--json")),
            ("append", lambda f: cli("append", f, *append_args(text="new"))),
            ("mark", lambda f: cli("mark", f, "--seq=1", "--status=parked")),
        ]:
            flow = make_flow(root, "corrupt-" + re.sub(r"\W+", "-", label)[:30] + "-" + cmd_label)
            seed(flow, lines)
            before = ledger_bytes(flow)
            rc, _, err = call(flow)
            prefix = f"flow-ledger: human.jsonl line {line_no}: "
            h.test_result(f"{cmd_label} on {label} exits 1 with exact locator prefix",
                          rc == 1 and err.startswith(prefix), f"rc={rc} stderr={err[:160]!r}")
            if needle:
                h.test_result(f"{cmd_label} on {label} names the torn-write reason",
                              needle in err and "torn write" in err, repr(err[:200]))
            h.test_result(f"{cmd_label} on {label} leaves the file byte-identical",
                          ledger_bytes(flow) == before)


def section_2_contention(h, root):
    print()
    print("[Section 2] F2 eight concurrent appenders behind a held lock")
    flow = make_flow(root, "contend-8")
    holder = open(lock_path(flow), "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    procs = []
    try:
        for i in range(8):
            procs.append(spawn([sys.executable, str(LEDGER), "append", "--flow-dir", str(flow),
                                *append_args(text=f"w{i}")]))
        time.sleep(1.5)
        all_blocked = all(p.poll() is None for p in procs)
        nothing_written = not ledger_path(flow).exists() or ledger_bytes(flow) == b""
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    results = [finish(p) for p in procs]
    h.test_result("all 8 appenders are still blocked after 1.5s behind the held lock (positive evidence)",
                  all_blocked, f"alive={[p.poll() is None for p in procs]}")
    h.test_result("no record is written while the lock is held", nothing_written)
    h.test_result("all 8 concurrent appends exit 0 after release",
                  all(rc == 0 for rc, _, _ in results), str([(rc, e[:80]) for rc, _, e in results]))
    seqs = sorted(int(out.strip()) for _, out, _ in results if out.strip().isdigit())
    h.test_result("the 8 appends print the distinct seqs 1..8", seqs == list(range(1, 9)), str(seqs))
    recs = load(flow)
    h.test_result("the ledger holds exactly 8 records", len(recs) == 8, str(len(recs)))
    h.test_result("record seqs are 1..8 in file order (seq assigned under the lock)",
                  [r["seq"] for r in recs] == list(range(1, 9)), str([r["seq"] for r in recs]))
    h.test_result("every concurrent text landed exactly once",
                  sorted(r["text"] for r in recs) == sorted(f"w{i}" for i in range(8)),
                  str(sorted(r["text"] for r in recs)))

    # Second contention shape: no holder, all 8 race for the lock at once.
    flow = make_flow(root, "race-8")
    procs = [spawn([sys.executable, str(LEDGER), "append", "--flow-dir", str(flow),
                    *append_args(text=f"r{i}")]) for i in range(8)]
    results = [finish(p) for p in procs]
    h.test_result("8 unheld concurrent appends all exit 0",
                  all(rc == 0 for rc, _, _ in results), str([(rc, e[:80]) for rc, _, e in results]))
    recs = load(flow)
    h.test_result("8 unheld concurrent appends leave seqs 1..8 in file order with no gaps or duplicates",
                  [r["seq"] for r in recs] == list(range(1, 9)), str([r["seq"] for r in recs]))


def section_3_rollback(h, root):
    print()
    print("[Section 3] F4 failed plain append rolls back under the lock")
    flow = make_flow(root, "rollback")
    seed(flow, [rec_line(1), rec_line(2)])
    before = ledger_bytes(flow)
    # Macos enforces the cap against the write length (checked before O_APPEND moves the offset),
    # so a cap below the appended record's size makes the write fail deterministically.
    limit = 64

    argv = [sys.executable, "-c", LIMITED_RUNNER, str(limit), str(LEDGER), "append",
            "--flow-dir", str(flow), *append_args(text="x" * 200)]
    rc, out, err = run_cli(argv)
    h.test_result("append that fails mid-write exits 1", rc == 1, f"rc={rc} stderr={err[:160]!r}")
    h.test_result("append failure names the write failure with the rollback message",
                  err.startswith("flow-ledger: append: ") and "write failed" in err
                  and "ledger rolled back" in err, repr(err[:220]))
    h.test_result("failed append prints no seq on stdout", out.strip() == "", repr(out))
    h.test_result("failed append leaves human.jsonl byte-identical to before (rollback)",
                  ledger_bytes(flow) == before,
                  f"before={len(before)} after={len(ledger_bytes(flow) or b'')}")

    rc, out, err = cli("append", flow, *append_args(text="after rollback"))
    h.test_result("ledger accepts a normal append after the rollback", rc == 0 and out.strip() == "3", err or repr(out))
    try:
        seqs_after = [r["seq"] for r in load(flow)]
    except ValueError as exc:
        seqs_after = f"unreadable: {exc}"
    h.test_result("after rollback the ledger holds seqs 1..3 as valid records",
                  seqs_after == [1, 2, 3], str(seqs_after))


def section_4_modes(h, root):
    print()
    print("[Section 4] F12 owner-only file modes regardless of umask")
    flow = make_flow(root, "modes")

    def umask_zero():
        os.umask(0)

    rc, _, err = cli("append", flow, *append_args(), preexec=umask_zero)
    h.test_result("append under umask 0 exits 0", rc == 0, err)
    if rc == 0:
        ledger_mode = stat.S_IMODE(os.stat(ledger_path(flow)).st_mode)
        lock_mode = stat.S_IMODE(os.stat(lock_path(flow)).st_mode)
        h.test_result("human.jsonl is created 0o600 under umask 0", ledger_mode == 0o600, oct(ledger_mode))
        h.test_result("human.jsonl.lock is created 0o600 under umask 0", lock_mode == 0o600, oct(lock_mode))


def section_5_list_missing(h, root):
    print()
    print("[Section 5] F27 list on a missing or empty ledger")
    flow = make_flow(root, "list-missing")
    rc, out, err = cli("list", flow)
    h.test_result("default list on a missing ledger exits 0", rc == 0, err)
    h.test_result("default list on a missing ledger prints []",
                  out.strip() == "[]" and json.loads(out) == [], repr(out))
    rc, out, err = cli("list", flow, "--json")
    h.test_result("list --json on a missing ledger exits 0 and prints []",
                  rc == 0 and json.loads(out) == [], f"rc={rc} out={out!r} err={err[:120]!r}")
    rc, out, err = cli("list", flow, "--md")
    h.test_result("list --md on a missing ledger exits 0 and prints nothing",
                  rc == 0 and out == "", f"rc={rc} out={out!r}")
    h.test_result("listing a missing ledger creates neither human.jsonl nor the lock",
                  ledger_bytes(flow) is None and not lock_path(flow).exists())

    flow = make_flow(root, "list-empty")
    seed(flow, [])
    rc, out, err = cli("list", flow, "--json")
    h.test_result("list --json on an existing empty ledger prints []",
                  rc == 0 and json.loads(out) == [], f"rc={rc} out={out!r} err={err[:120]!r}")


def section_6_list_lock(h, root):
    print()
    print("[Section 6] F6 list takes a shared lock when the file exists")
    flow = make_flow(root, "list-ex")
    seed(flow, [rec_line(1)])
    holder = open(lock_path(flow), "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        proc = spawn([sys.executable, str(LEDGER), "list", "--flow-dir", str(flow), "--json"])
        time.sleep(1.5)
        blocked = proc.poll() is None
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    rc, out, err = finish(proc)
    h.test_result("list --json blocks behind an exclusive holder (positive evidence)", blocked)
    h.test_result("list --json completes after release with the record",
                  rc == 0 and len(json.loads(out)) == 1, f"rc={rc} err={err[:120]!r}")

    flow = make_flow(root, "list-sh")
    seed(flow, [rec_line(1)])
    holder = open(lock_path(flow), "a")
    fcntl.flock(holder, fcntl.LOCK_SH)
    try:
        try:
            rc, out, err = run_cli([sys.executable, str(LEDGER), "list", "--flow-dir", str(flow), "--json"])
            completed = True
        except subprocess.TimeoutExpired:
            completed, rc, out = False, None, ""
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    h.test_result("list --json completes alongside a shared holder (LOCK_SH, not LOCK_EX)",
                  completed and rc == 0 and len(json.loads(out)) == 1, f"completed={completed} rc={rc}")


def section_7_ts(h, root):
    print()
    print("[Section 7] F19 ts is ISO-8601 UTC with a Z suffix")
    flow = make_flow(root, "ts-append")
    before_ns = datetime.now(timezone.utc).replace(tzinfo=None)
    rc, _, err = cli("append", flow, *append_args())
    after_ns = datetime.now(timezone.utc).replace(tzinfo=None)
    h.test_result("append for ts check exits 0", rc == 0, err)
    ts = load(flow)[0]["ts"] if rc == 0 else ""
    h.test_result("appended ts matches ISO-8601 with a Z suffix", ISO_Z.match(ts) is not None, repr(ts))
    h.test_result("appended ts is not a numeric-offset form", not ts.endswith("+00:00") and " " not in ts, repr(ts))
    try:
        parsed = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        slack = timedelta(minutes=1)
        in_window = before_ns.replace(microsecond=0) - slack <= parsed <= after_ns + slack
    except ValueError:
        in_window = False
    h.test_result("appended ts is the current UTC time (not local time)", in_window, repr(ts))

    flow = make_flow(root, "ts-control")
    seed(flow, [rec_line(1, ts="2026-10-14T19:00:00Z")])
    rc, out, err = cli("list", flow, "--json")
    h.test_result("control: a valid Z timestamp in an existing ledger is accepted",
                  rc == 0 and len(json.loads(out)) == 1, f"rc={rc} err={err[:120]!r}")

    bad_ts = [
        ("space separator", "2026-10-14 19:00:00Z"),
        ("numeric offset", "2026-10-14T19:00:00+00:00"),
        ("date only", "2026-10-14"),
        ("free text", "yesterday"),
        ("epoch integer", 1787000000),
    ]
    for label, value in bad_ts:
        flow = make_flow(root, "ts-bad-" + re.sub(r"\W+", "-", label))
        seed(flow, [rec_line(1, ts=value), rec_line(2)])
        rc, out, err = cli("list", flow, "--json")
        h.test_result(f"an existing record with ts ({label}) is refused with 'human.jsonl line 1:'",
                      rc == 1 and err.startswith("flow-ledger: human.jsonl line 1: "),
                      f"rc={rc} err={err[:160]!r}")


def section_8_mark_status(h, root):
    print()
    print("[Section 8] F14 mark accepts only addressed and parked")
    flow = make_flow(root, "mark-superseded")
    seed(flow, [rec_line(1)])
    before = ledger_bytes(flow)
    rc, _, err = cli("mark", flow, "--seq=1", "--status=superseded")
    h.test_result("mark --status superseded exits 2 (argparse choices)", rc == 2, f"rc={rc}")
    h.test_result("refused mark --status superseded leaves the ledger byte-identical",
                  ledger_bytes(flow) == before)
    h.test_result("refused mark --status superseded carries no 'human.jsonl line N' locator",
                  no_line_number(err), repr(err[:160]))

    rc, out, _ = run_cli([sys.executable, str(LEDGER), "mark", "--help"])
    h.test_result("mark --help lists exactly the choices {addressed,parked}",
                  rc == 0 and re.search(r"--status \{addressed,parked\}", out) is not None, repr(out[:300]))


def section_9_matrix(h, root):
    print()
    print("[Section 9] F20 extra validation-matrix rows and addressed_by rules")

    flow = make_flow(root, "supersede-pending-correction")
    rc, _, _ = cli("append", flow, *append_args(text="one"))
    rc2, _, _ = cli("append", flow, *append_args(kind="correction", text="fix one", extra=["--supersedes=1"]))
    h.test_result("setup: correction supersedes a pending answer", rc == 0 and rc2 == 0)
    rc3, out3, err3 = cli("append", flow, *append_args(kind="correction", text="fix the fix",
                                                       extra=["--supersedes=2"]))
    h.test_result("a pending correction may itself be superseded (C1)", rc3 == 0 and out3.strip() == "3",
                  err3 or repr(out3))
    recs = load(flow)
    h.test_result("the superseded correction keeps status superseded and addressed_by null",
                  len(recs) == 3 and recs[1]["status"] == "superseded" and recs[1]["addressed_by"] is None,
                  str(recs[1:2]))
    h.test_result("the first answer stays superseded with addressed_by null (pending source)",
                  recs[0]["status"] == "superseded" and recs[0]["addressed_by"] is None, str(recs[0]))
    h.test_result("the new correction records supersedes 2",
                  recs[2]["supersedes"] == 2 and recs[2]["kind"] == "correction", str(recs[2]))

    flow = make_flow(root, "park-keeps-null")
    seed(flow, [rec_line(1)])
    rc, _, err = cli("mark", flow, "--seq=1", "--status=parked")
    recs = load(flow)
    h.test_result("pending -> parked exits 0 with addressed_by null", rc == 0 and recs[0]["addressed_by"] is None
                  and recs[0]["status"] == "parked", err or str(recs))

    flow = make_flow(root, "supersede-zero-matrix")
    rc, _, _ = cli("append", flow, *append_args(text="one"))
    rc2, _, err2 = cli("append", flow, *append_args(kind="correction", extra=["--supersedes=0"]))
    h.test_result("supersede of seq 0 exits 1 with the append prefix",
                  rc2 == 1 and err2.startswith("flow-ledger: append: "), f"rc={rc2} err={err2[:120]!r}")


def section_10_large(h, root):
    print()
    print("[Section 10] F29 a ledger larger than one read chunk lists in full")
    flow = make_flow(root, "large")
    big = "".join(rec_line(i, text="x" * 600) for i in range(1, 301))
    seed(flow, [big])
    size = len(ledger_bytes(flow))
    h.test_result("setup: the seeded ledger is well past 64 KiB", size > 64 * 1024, str(size))
    rc, out, err = cli("list", flow, "--json")
    recs = json.loads(out) if rc == 0 else []
    h.test_result("list --json returns all 300 records of a large ledger",
                  rc == 0 and len(recs) == 300, f"rc={rc} n={len(recs)} err={err[:120]!r}")
    h.test_result("the large ledger's seqs come back as 1..300 in order",
                  [r["seq"] for r in recs] == list(range(1, 301)))
    rc, out, err = cli("append", flow, *append_args(text="after large"))
    h.test_result("append after a large ledger assigns seq 301", rc == 0 and out.strip() == "301", err or repr(out))


def section_11_utf8(h, root):
    print()
    print("[Section 11] F7 --text-stdin decodes UTF-8 regardless of locale or PYTHONIOENCODING")
    flow = make_flow(root, "utf8-stdin")
    env = dict(os.environ, PYTHONIOENCODING="ascii", LC_ALL="C", LANG="C")
    env.pop("PYTHONUTF8", None)
    rc, _, err = cli("append", flow, "--kind=answer", "--step=plan", "--attempt=1", "--text-stdin",
                     stdin="café ✓ naïve\n".encode("utf-8"), env=env)
    h.test_result("--text-stdin with UTF-8 bytes under an ascii stdio encoding exits 0", rc == 0, repr(err[:200]))
    recs = load(flow) if rc == 0 else []
    h.test_result("the UTF-8 text is stored intact", recs and recs[0]["text"] == "café ✓ naïve",
                  repr(recs[0]["text"]) if recs else "no record")


def main():
    h = Harness("FLOW LEDGER (flow-ledger.py) SPEC-BLIND TEST SUITE")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sections = [
            section_1_error_strings, section_2_contention, section_3_rollback,
            section_4_modes, section_5_list_missing, section_6_list_lock,
            section_7_ts, section_8_mark_status, section_9_matrix,
            section_10_large, section_11_utf8,
        ]
        for section in sections:
            # A section that raises (e.g. on an unexpected ledger state) is recorded as a failure
            # so the remaining sections still run.
            try:
                section(h, root)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                h.test_result(f"{section.__name__} ran to completion", False, f"{type(exc).__name__}: {exc}")
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
