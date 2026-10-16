#!/usr/bin/env python3
"""
Tests for scripts/flow-ledger.py, the single owner of an expert-flow run's human.jsonl ledger (#287).

Every case runs the real script as a subprocess against a temporary flow dir holding a stub
state.json. Covered:
1. append: seq assignment, the ten keys in ticket order, UTC ts, --text / --text-stdin handling,
   argument errors, transcript path rules, flow-dir validation
2. supersede: `append --supersedes N` from pending, addressed and parked; refused from superseded
   or a missing seq; --kind correction and --supersedes must come together
3. mark: pending -> addressed/parked/superseded only; --by rules; non-pending sources refused
4. list: missing file is a pure no-op; filters; pinned --json and --md shapes; never writes
5. fail loud: torn, invalid, and invariant-violating files make list/append/mark exit 1 with the
   line number on stderr and leave the file byte-identical
6. lock: append, append --supersedes and mark block while another holder has the sidecar lock,
   and land only after release; list takes no lock

Run with: python3 tests/test_flow_ledger.py
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"
KEYS = ["seq", "ts", "step", "attempt", "kind", "text", "transcript", "status", "addressed_by", "supersedes"]
ALLOWED_DIR_ENTRIES = {"state.json", "human.jsonl", "human.jsonl.lock"}
ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)")


def flow_dir(root, name):
    """Create a flow dir with a stub state.json and return its path."""
    flow = Path(root) / name
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "state.json").write_text("{}\n")
    return flow


def run(sub, flow, *args, stdin=None):
    """Run `flow-ledger.py <sub> --flow-dir <flow> ...`. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(LEDGER), sub, "--flow-dir", str(flow), *args]
    kwargs = {"capture_output": True, "timeout": 60}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    result = subprocess.run(cmd, **kwargs)
    return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")


def append(flow, kind="answer", text="a response", step="plan", attempt=1, extra=(), stdin=None):
    """Run `append` with --text (or --text-stdin when stdin is given)."""
    args = ["--kind", kind, "--step", step, "--attempt", str(attempt), *extra]
    if stdin is None:
        args += ["--text", text]
        return run("append", flow, *args)
    args += ["--text-stdin"]
    return run("append", flow, *args, stdin=stdin)


def mark(flow, seq, status, by=None):
    """Run `mark` for one record."""
    args = ["--seq", str(seq), "--status", status]
    if by is not None:
        args += ["--by", by]
    return run("mark", flow, *args)


def supersede(flow, seq, text="corrected text"):
    """Run the correction form of append that supersedes `seq`."""
    return append(flow, kind="correction", text=text, extra=("--supersedes", str(seq)))


def ledger_path(flow):
    return Path(flow) / "human.jsonl"


def ledger_bytes(flow):
    """Raw bytes of human.jsonl, or None when the file does not exist."""
    path = ledger_path(flow)
    return path.read_bytes() if path.exists() else None


def raw_lines(flow):
    data = ledger_bytes(flow)
    return data.splitlines(keepends=True) if data else []


def load_records(flow):
    """Parse human.jsonl into dicts (key order preserved)."""
    data = ledger_bytes(flow) or b""
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def rec(seq, **over):
    """A well-formed record dict in ticket key order, with overrides."""
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
    """Write raw lines (already newline-terminated strings) as the ledger."""
    ledger_path(flow).write_bytes("".join(lines).encode("utf-8"))


def ordered_pairs(pairs):
    return [k for k, _ in pairs]


def parse_list_json(out):
    """Parse `list --json` output keeping key order."""
    return json.loads(out, object_pairs_hook=lambda pairs: pairs)


def run_while_locked(flow, argv):
    """Hold the sidecar lock in this process, start the script with argv, release after 1.5s.

    Returns (blocked_while_held, bytes_unchanged_while_held, returncode, stdout, stderr).
    """
    before = ledger_bytes(flow)
    lock_file = open(flow / "human.jsonl.lock", "a")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    proc = subprocess.Popen(
        [sys.executable, str(LEDGER), *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1.5)
        blocked = proc.poll() is None
        unchanged = ledger_bytes(flow) == before
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return blocked, unchanged, proc.returncode, out.decode("utf-8"), err.decode("utf-8")


def main():
    h = Harness("FLOW LEDGER (flow-ledger.py) TEST SUITE")

    with tempfile.TemporaryDirectory() as root:
        root = Path(root)

        print("[Section 1] append")
        flow = flow_dir(root, "append-basic")
        rc, out, err = append(flow, text="first")
        h.test_result("first append exits 0", rc == 0, err)
        h.test_result("first append prints seq 1 on stdout", out.strip() == "1", repr(out))
        h.test_result("first append creates human.jsonl", ledger_path(flow).exists())
        recs = load_records(flow)
        first = recs[0] if recs else {}
        h.test_result("record has exactly the ten keys in ticket order",
                      list(first.keys()) == KEYS, str(list(first.keys())))
        h.test_result("first record has seq 1", first.get("seq") == 1)
        h.test_result("new record status is pending", first.get("status") == "pending")
        h.test_result("new record addressed_by is null", first.get("addressed_by") is None)
        h.test_result("new record supersedes is null", first.get("supersedes") is None)
        h.test_result("new record transcript is null when not given", first.get("transcript") is None)
        h.test_result("record carries the step, attempt, kind and text as given",
                      first.get("step") == "plan" and first.get("attempt") == 1
                      and first.get("kind") == "answer" and first.get("text") == "first",
                      str(first))
        h.test_result("ts is an ISO-8601 UTC timestamp",
                      isinstance(first.get("ts"), str) and ISO_UTC.fullmatch(first["ts"]) is not None,
                      str(first.get("ts")))
        h.test_result("human.jsonl ends with a newline", (ledger_bytes(flow) or b"").endswith(b"\n"))

        rc, out, err = append(flow, text="second")
        h.test_result("second append exits 0 and prints seq 2", rc == 0 and out.strip() == "2", err or repr(out))
        h.test_result("second append gets seq 2 in the file",
                      [r["seq"] for r in load_records(flow)] == [1, 2])

        flow = flow_dir(root, "append-dash")
        rc, _, err = run("append", flow, "--kind", "answer", "--step", "plan", "--attempt", "1", "--text=-x")
        h.test_result("--text=-x stores the dash text verbatim",
                      rc == 0 and load_records(flow)[-1]["text"] == "-x", err)

        flow = flow_dir(root, "append-stdin")
        rc, _, err = append(flow, stdin=b"  lead\n\nmid  \n\n\n")
        h.test_result("--text-stdin exits 0", rc == 0, err)
        h.test_result("--text-stdin strips exactly one trailing newline and keeps the rest",
                      load_records(flow)[0]["text"] == "  lead\n\nmid  \n\n",
                      repr(load_records(flow)[0]["text"]))
        rc, _, err = append(flow, stdin=b"-x --y")
        h.test_result("--text-stdin keeps a dash-leading text with no trailing newline",
                      rc == 0 and load_records(flow)[-1]["text"] == "-x --y", err)

        flow = flow_dir(root, "append-textarg")
        rc, _, _ = run("append", flow, "--kind", "answer", "--step", "plan", "--attempt", "1")
        h.test_result("neither --text nor --text-stdin is rejected", rc != 0)
        rc, _, _ = run("append", flow, "--kind", "answer", "--step", "plan", "--attempt", "1",
                       "--text", "x", "--text-stdin", stdin=b"y")
        h.test_result("--text together with --text-stdin is rejected", rc != 0)
        h.test_result("rejected text args leave no ledger", ledger_bytes(flow) is None)

        flow = flow_dir(root, "append-kinds")
        kinds = ["transcript", "answer", "direction", "question", "parked", "gate_note"]
        all_ok = True
        for kind in kinds:
            rc, _, _ = append(flow, kind=kind, text=f"kind {kind}")
            all_ok = all_ok and rc == 0
        h.test_result("every non-correction kind is accepted", all_ok)
        h.test_result("kinds are recorded in order",
                      [r["kind"] for r in load_records(flow)] == kinds)

        flow = flow_dir(root, "append-badkind")
        rc, _, _ = append(flow, kind="memo")
        h.test_result("unknown --kind exits 2", rc == 2, f"rc={rc}")
        rc, _, _ = run("append", flow, "--kind", "answer", "--step", "plan", "--attempt", "1",
                       "--seq", "5", "--text", "x")
        h.test_result("append has no --seq flag (exit 2)", rc == 2, f"rc={rc}")
        h.test_result("usage errors leave no ledger", ledger_bytes(flow) is None)

        flow = flow_dir(root, "append-seq-caller")
        append(flow, text="one")
        rc, _, _ = run("append", flow, "--kind", "answer", "--step", "plan", "--attempt", "1",
                       "--seq", "1", "--text", "two")
        h.test_result("a caller-supplied seq is refused", rc == 2 and len(load_records(flow)) == 1)

        flow = flow_dir(root, "append-transcript")
        rc, _, _ = append(flow, extra=("--transcript", "/etc/passwd"))
        h.test_result("absolute --transcript exits 1", rc == 1)
        rc, _, _ = append(flow, extra=("--transcript", "../secret.md"))
        h.test_result("--transcript with a leading .. exits 1", rc == 1)
        rc, _, _ = append(flow, extra=("--transcript", "steps/../../x.md"))
        h.test_result("--transcript with an interior .. exits 1", rc == 1)
        h.test_result("rejected transcripts leave no ledger", ledger_bytes(flow) is None)
        rc, _, err = append(flow, extra=("--transcript", "steps/01-plan.md"))
        h.test_result("relative --transcript is stored",
                      rc == 0 and load_records(flow)[0]["transcript"] == "steps/01-plan.md", err)

        flow = flow_dir(root, "append-empty-file")
        ledger_path(flow).write_bytes(b"")
        rc, out, err = append(flow, text="into empty")
        h.test_result("an existing empty ledger is appended to as seq 1",
                      rc == 0 and out.strip() == "1", err or repr(out))

        missing_state = Path(root) / "no-state"
        missing_state.mkdir()
        rc, _, _ = append(missing_state)
        h.test_result("append in a flow dir without state.json exits 1", rc == 1)
        h.test_result("append without state.json creates no ledger", ledger_bytes(missing_state) is None)
        rc, _, _ = append(root / "does-not-exist")
        h.test_result("append in a nonexistent flow dir exits 1", rc == 1)

        print()
        print("[Section 2] supersede (append --supersedes)")
        flow = flow_dir(root, "supersede-basic")
        append(flow, text="one")
        append(flow, text="two")
        before = raw_lines(flow)
        rc, out, err = supersede(flow, 1)
        h.test_result("supersede exits 0 and prints the new seq 3", rc == 0 and out.strip() == "3", err or repr(out))
        recs = load_records(flow)
        h.test_result("superseded record has status superseded", recs[0]["status"] == "superseded")
        h.test_result("correction record has supersedes 1, kind correction and status pending",
                      recs[2]["kind"] == "correction" and recs[2]["supersedes"] == 1
                      and recs[2]["status"] == "pending" and recs[2]["seq"] == 3,
                      str(recs[2]))
        after = raw_lines(flow)
        h.test_result("the untouched record 2 survives the supersede byte-for-byte",
                      len(after) >= 2 and after[1] == before[1])

        flow = flow_dir(root, "supersede-addressed")
        append(flow, text="one")
        rc, _, err = mark(flow, 1, "addressed", by="steps/03-implement.md")
        h.test_result("setup: mark record 1 addressed", rc == 0, err)
        rc, _, err = supersede(flow, 1)
        h.test_result("superseding an addressed record succeeds", rc == 0, err)
        recs = load_records(flow)
        h.test_result("superseding an addressed record marks it superseded",
                      recs[0]["status"] == "superseded")
        h.test_result("superseding an addressed record keeps its addressed_by",
                      recs[0]["addressed_by"] == "steps/03-implement.md", str(recs[0]))

        flow = flow_dir(root, "supersede-parked")
        append(flow, text="one")
        mark(flow, 1, "parked")
        rc, _, err = supersede(flow, 1)
        h.test_result("superseding a parked record succeeds", rc == 0 and load_records(flow)[0]["status"] == "superseded", err)

        flow = flow_dir(root, "supersede-refused")
        append(flow, text="one")
        supersede(flow, 1)
        before = ledger_bytes(flow)
        rc, _, _ = supersede(flow, 1, text="again")
        h.test_result("superseding an already superseded seq exits 1", rc == 1, f"rc={rc}")
        h.test_result("refused re-supersede leaves the file byte-identical", ledger_bytes(flow) == before)
        rc, _, _ = supersede(flow, 99)
        h.test_result("superseding a nonexistent seq exits 1", rc == 1, f"rc={rc}")
        h.test_result("refused supersede of a missing seq leaves the file byte-identical",
                      ledger_bytes(flow) == before)

        flow = flow_dir(root, "supersede-pairing")
        append(flow, text="one")
        before = ledger_bytes(flow)
        rc, _, _ = append(flow, kind="answer", text="x", extra=("--supersedes", "1"))
        h.test_result("--supersedes without --kind correction exits 1", rc == 1, f"rc={rc}")
        rc, _, _ = append(flow, kind="correction", text="x")
        h.test_result("--kind correction without --supersedes exits 1", rc == 1, f"rc={rc}")
        h.test_result("mis-paired supersede calls leave the file byte-identical", ledger_bytes(flow) == before)

        print()
        print("[Section 3] mark")
        flow = flow_dir(root, "mark-basic")
        for n in range(1, 4):
            append(flow, text=f"record {n}")
        before = raw_lines(flow)
        rc, _, err = mark(flow, 1, "addressed", by="steps/03-implement.md")
        h.test_result("pending -> addressed with --by exits 0", rc == 0, err)
        recs = load_records(flow)
        h.test_result("marked record is addressed with addressed_by set",
                      recs[0]["status"] == "addressed" and recs[0]["addressed_by"] == "steps/03-implement.md",
                      str(recs[0]))
        after = raw_lines(flow)
        h.test_result("the other records survive a mark byte-for-byte",
                      len(after) == 3 and after[1] == before[1] and after[2] == before[2])
        names = set(os.listdir(flow))
        h.test_result("mark leaves no temp files in the flow dir", names <= ALLOWED_DIR_ENTRIES, str(sorted(names)))

        rc, _, _ = mark(flow, 2, "addressed")
        h.test_result("addressed without --by exits 1", rc == 1, f"rc={rc}")
        rc, _, _ = mark(flow, 2, "parked", by="x")
        h.test_result("parked with --by exits 1", rc == 1, f"rc={rc}")
        rc, _, _ = mark(flow, 2, "addressed", by="/abs/path.md")
        h.test_result("absolute --by exits 1", rc == 1, f"rc={rc}")
        rc, _, _ = mark(flow, 2, "addressed", by="steps/../x.md")
        h.test_result("--by with a .. segment exits 1", rc == 1, f"rc={rc}")
        before = ledger_bytes(flow)
        rc, _, _ = mark(flow, 2, "pending")
        h.test_result("--status pending is rejected", rc != 0, f"rc={rc}")
        h.test_result("rejected marks leave the file byte-identical", ledger_bytes(flow) == before)
        rc, _, _ = mark(flow, 99, "parked")
        h.test_result("mark of a nonexistent seq exits 1", rc == 1, f"rc={rc}")
        h.test_result("mark of a nonexistent seq leaves the file byte-identical", ledger_bytes(flow) == before)

        rc, _, _ = mark(flow, 2, "parked")
        h.test_result("pending -> parked exits 0", rc == 0)
        rc, _, _ = mark(flow, 3, "superseded")
        h.test_result("pending -> superseded via mark exits 0", rc == 0)
        recs = load_records(flow)
        h.test_result("mark statuses land: 1 addressed, 2 parked, 3 superseded",
                      [r["status"] for r in recs] == ["addressed", "parked", "superseded"],
                      str([r["status"] for r in recs]))

        print()
        print("[Section 3b] mark refuses every non-pending source")
        flow = flow_dir(root, "mark-nonpending")
        for n in range(1, 4):
            append(flow, text=f"record {n}")
        mark(flow, 1, "addressed", by="steps/03-implement.md")
        mark(flow, 2, "parked")
        mark(flow, 3, "superseded")
        before = ledger_bytes(flow)
        for label, args in [
            ("addressed -> parked", (1, "parked", None)),
            ("addressed -> addressed", (1, "addressed", "steps/other.md")),
            ("addressed -> superseded", (1, "superseded", None)),
            ("parked -> addressed", (2, "addressed", "steps/03-implement.md")),
            ("parked -> parked", (2, "parked", None)),
            ("superseded -> addressed", (3, "addressed", "steps/03-implement.md")),
            ("superseded -> parked", (3, "parked", None)),
        ]:
            seq, status, by = args
            rc, _, _ = mark(flow, seq, status, by=by)
            h.test_result(f"mark refuses {label} (exit non-zero)", rc != 0, f"rc={rc}")
        h.test_result("refused non-pending marks leave the file byte-identical", ledger_bytes(flow) == before)

        print()
        print("[Section 4] list")
        flow = flow_dir(root, "list-missing")
        rc1, out1, _ = run("list", flow, "--json")
        rc2, out2, _ = run("list", flow, "--md")
        rc3, out3, _ = run("list", flow, "--status", "pending")
        h.test_result("list on a missing file exits 0 with no output",
                      rc1 == 0 and rc2 == 0 and rc3 == 0 and out1 == "" and out2 == "" and out3 == "",
                      f"rc={rc1},{rc2},{rc3} out={out1!r},{out2!r},{out3!r}")
        h.test_result("list on a missing file creates neither human.jsonl nor the lock",
                      not ledger_path(flow).exists() and not (flow / "human.jsonl.lock").exists(),
                      str(sorted(os.listdir(flow))))
        no_state = Path(root) / "list-no-state"
        no_state.mkdir()
        rc, _, _ = run("list", no_state)
        h.test_result("list in a flow dir without state.json exits 1", rc == 1, f"rc={rc}")

        flow = flow_dir(root, "list-empty-file")
        ledger_path(flow).write_bytes(b"")
        rc, out, _ = run("list", flow, "--md")
        h.test_result("list on an empty ledger exits 0 with no --md output", rc == 0 and out == "", repr(out))

        flow = flow_dir(root, "list-filters")
        append(flow, kind="answer", text="a1")
        append(flow, kind="gate_note", text="g2")
        append(flow, kind="answer", text="a3")
        append(flow, kind="parked", text="p4")
        mark(flow, 3, "addressed", by="steps/03-implement.md")
        mark(flow, 4, "parked")
        rc, out, _ = run("list", flow, "--status", "pending", "--json")
        seqs = [dict(r)["seq"] for r in parse_list_json(out)] if rc == 0 else []
        h.test_result("--status pending selects pending records only", seqs == [1, 2], str(seqs))
        rc, out, _ = run("list", flow, "--kind", "answer", "--json")
        seqs = [dict(r)["seq"] for r in parse_list_json(out)] if rc == 0 else []
        h.test_result("--kind answer selects answer records only", seqs == [1, 3], str(seqs))
        rc, out, _ = run("list", flow, "--status", "addressed", "--kind", "answer", "--json")
        seqs = [dict(r)["seq"] for r in parse_list_json(out)] if rc == 0 else []
        h.test_result("--status and --kind filters combine", seqs == [3], str(seqs))

        rc_default, out_default, _ = run("list", flow)
        rc_json, out_json, _ = run("list", flow, "--json")
        h.test_result("list defaults to the --json output", rc_default == 0 and out_default == out_json)
        entries = parse_list_json(out_json)
        h.test_result("--json prints a JSON array of records with the ten keys in order",
                      isinstance(entries, list) and len(entries) == 4
                      and all(ordered_pairs(e) == KEYS for e in entries),
                      out_json[:200])
        first = dict(entries[0])
        h.test_result("--json record values match the stored record",
                      first["seq"] == 1 and first["kind"] == "answer" and first["text"] == "a1"
                      and first["status"] == "pending", str(first))

        flow = flow_dir(root, "list-md")
        append(flow, kind="answer", text="multi\nline text", step="plan", attempt=2)
        append(flow, kind="gate_note", text="second", step="implement", attempt=1)
        mark(flow, 1, "addressed", by="steps/03-implement.md")
        supersede(flow, 2, text="corrected")
        rc, out, _ = run("list", flow, "--md")
        lines = out.splitlines()
        h.test_result("list --md exits 0", rc == 0)
        h.test_result("--md header line has the documented shape",
                      bool(lines) and re.match(r"- #1 \[answer, addressed\] plan attempt 2 \(\S[^)]*\)", lines[0]) is not None,
                      lines[0] if lines else "")
        h.test_result("--md shows the addressed_by path",
                      "by steps/03-implement.md" in out, out[:300])
        h.test_result("--md shows supersedes #N on the correction",
                      "supersedes #2" in out, out[:300])
        h.test_result("--md shows the superseded status",
                      re.search(r"^- #2 \[gate_note, superseded\] implement attempt 1 \(", out, re.M) is not None,
                      out[:300])
        h.test_result("--md indents a multi-line text line by line",
                      re.search(r"^\s+multi$", out, re.M) is not None and re.search(r"^\s+line text$", out, re.M) is not None,
                      out[:300])
        rc, _, _ = run("list", flow, "--json", "--md")
        h.test_result("--json and --md are mutually exclusive", rc != 0, f"rc={rc}")

        print()
        print("[Section 5] fail loud (invalid or torn ledger)")
        good1 = rec_line(1)
        good2 = rec_line(2, kind="gate_note")
        cases = [
            ("torn trailing line (no newline)", [good1, good2, '{"seq": 3, "ts": "2026-10'], 3),
            ("invalid JSON on a middle line", [good1, "{not json\n", good2], 2),
            ("unknown status value", [rec_line(1, status="adressed"), good2], 1),
            ("seq gap", [good1, rec_line(3)], 2),
            ("duplicate seq", [good1, rec_line(1, text="dup")], 2),
            ("addressed_by set on a pending record", [rec_line(1, addressed_by="steps/03.md"), good2], 1),
            ("addressed status without addressed_by", [rec_line(1, status="addressed"), good2], 1),
            ("supersedes set on a non-correction record", [good1, rec_line(2, supersedes=1)], 2),
            ("correction without supersedes", [good1, rec_line(2, kind="correction")], 2),
            ("unknown kind", [rec_line(1, kind="memo"), good2], 1),
            ("step outside the slug pattern", [rec_line(1, step="Plan Step"), good2], 1),
            ("empty text", [rec_line(1, text=""), good2], 1),
            ("absolute transcript path", [rec_line(1, transcript="/etc/passwd"), good2], 1),
            ("transcript containing ..", [rec_line(1, transcript="steps/../../x.md"), good2], 1),
            ("missing key (ts)", [json.dumps({k: v for k, v in rec(1).items() if k != "ts"}) + "\n", good2], 1),
            ("extra key", [json.dumps({**rec(1), "bonus": 1}) + "\n", good2], 1),
        ]
        commands = [
            ("list --json", lambda f: run("list", f, "--json")),
            ("append", lambda f: append(f, text="new")),
            ("mark", lambda f: mark(f, 2, "parked")),
        ]
        for label, lines, line_no in cases:
            for cmd_label, call in commands:
                flow = flow_dir(root, "bad-" + re.sub(r"\W+", "-", label)[:40] + "-" + cmd_label.split()[0])
                seed(flow, lines)
                before = ledger_bytes(flow)
                rc, _, err = call(flow)
                ok = rc == 1 and re.search(rf"\bline {line_no}\b", err) is not None
                h.test_result(f"{cmd_label} exits 1 with 'line {line_no}' on stderr: {label}",
                              ok, f"rc={rc} stderr={err.strip()[:160]!r}")
                h.test_result(f"{cmd_label} leaves the bad ledger byte-identical: {label}",
                              ledger_bytes(flow) == before)

        print()
        print("[Section 6] lock (deterministic, not a race test)")
        flow = flow_dir(root, "lock-append")
        (flow / "human.jsonl.lock").touch()
        blocked, unchanged, rc, out, err = run_while_locked(
            flow, ["append", "--flow-dir", str(flow), "--kind", "answer", "--step", "plan",
                   "--attempt", "1", "--text", "locked write"])
        h.test_result("append blocks while another holder has the sidecar lock", blocked)
        h.test_result("append leaves the ledger untouched while blocked", unchanged)
        h.test_result("blocked append completes after release with seq 1",
                      rc == 0 and out.strip() == "1", err or repr(out))

        flow = flow_dir(root, "lock-supersede")
        append(flow, text="one")
        append(flow, text="two")
        blocked, unchanged, rc, out, err = run_while_locked(
            flow, ["append", "--flow-dir", str(flow), "--kind", "correction", "--step", "plan",
                   "--attempt", "1", "--supersedes", "1", "--text", "fixed"])
        h.test_result("append --supersedes blocks while the lock is held", blocked)
        h.test_result("append --supersedes leaves the ledger untouched while blocked", unchanged)
        recs = load_records(flow)
        h.test_result("append --supersedes lands the new record and the superseded status together",
                      rc == 0 and len(recs) == 3 and recs[0]["status"] == "superseded"
                      and recs[2]["kind"] == "correction" and recs[2]["supersedes"] == 1,
                      err or str(recs))

        flow = flow_dir(root, "lock-mark")
        append(flow, text="one")
        append(flow, text="two")
        blocked, unchanged, rc, out, err = run_while_locked(
            flow, ["mark", "--flow-dir", str(flow), "--seq", "1", "--status", "parked"])
        h.test_result("mark blocks while the lock is held", blocked)
        h.test_result("mark leaves the ledger untouched while blocked", unchanged)
        recs = load_records(flow)
        h.test_result("mark lands after release", rc == 0 and recs[0]["status"] == "parked", err)

        flow = flow_dir(root, "lock-list")
        append(flow, text="one")
        held = open(flow / "human.jsonl.lock", "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            try:
                listed = subprocess.run(
                    [sys.executable, str(LEDGER), "list", "--flow-dir", str(flow), "--md"],
                    capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                )
                completed = True
                list_rc, list_out = listed.returncode, listed.stdout.decode("utf-8")
            except subprocess.TimeoutExpired:
                completed = False
                list_rc, list_out = None, ""
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
        h.test_result("list completes while the lock is held (pure read, no lock)",
                      completed and list_rc == 0 and "#1 [answer, pending]" in list_out)

        flow = flow_dir(root, "list-no-lock-file")
        append(flow, text="one")
        (flow / "human.jsonl.lock").unlink()
        run("list", flow, "--json")
        h.test_result("list does not recreate the lock file", not (flow / "human.jsonl.lock").exists())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
