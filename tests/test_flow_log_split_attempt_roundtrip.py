#!/usr/bin/env python3
"""SPLIT_ATTEMPT writer/reader contract for scripts/flow_log.py.

The writer (`format_split_attempt_line`) and the reader (`parse_split_attempt_lines`) must agree:
a record written is read back unchanged, booleans are JSON booleans on the wire, and the reader
rejects lines whose fields are the wrong type or whose JSON is not a versioned object, reporting
them as malformed without raising. Prose and unrelated lines are ignored.

flow_log.py is a library module with no CLI, so it is loaded the same way tests/test_flow_log.py
loads it, and only its public functions are called.

Run with: python3 tests/test_flow_log_split_attempt_roundtrip.py
"""
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

FLOW_LOG = REPO_ROOT / "scripts" / "flow_log.py"
_spec = importlib.util.spec_from_file_location("flow_log", FLOW_LOG)
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z implement SPLIT_ATTEMPT \{.*\}$")


def record(**overrides):
    fields = dict(
        schema_version=1,
        step="implement",
        attempt=2,
        trigger="retry",
        prior_split_basis="parallel",
        split_basis="parallel",
        min_units="2",
        units=3,
        partial_work="keep",
        decision="retry",
        failure_class=None,
        turns_exhausted=True,
        committed_units=["u1"],
        pending_units=["u2", "u3"],
    )
    fields.update(overrides)
    return fl.SplitAttemptRecord(**fields)


def write_log(lines):
    path = Path(tempfile.mkdtemp()) / "log.md"
    path.write_text("# flow log\n" + "\n".join(lines) + "\n")
    return path


def main():
    h = Harness("FLOW LOG SPLIT_ATTEMPT ROUND TRIP")

    print("[Round trip] a written record is read back unchanged")
    original = record()
    line = fl.format_split_attempt_line(original)
    h.test_result("writer emits the '<ISO Z> <step> SPLIT_ATTEMPT {json}' shape",
                  LINE_RE.match(line) is not None, repr(line))
    parsed = fl.parse_split_attempt_lines(write_log([line]))
    h.test_result("reader returns exactly one record for one written line",
                  len(parsed.records) == 1 and not parsed.malformed_lines,
                  f"records={parsed.records} malformed={parsed.malformed_lines}")
    h.test_result("round-tripped record equals the written record",
                  parsed.records and parsed.records[0] == original,
                  f"got={parsed.records[:1]}")

    print("[Wire format] booleans are JSON booleans, nulls are JSON nulls")
    payload = json.loads(line.split(" SPLIT_ATTEMPT ", 1)[1])
    h.test_result("turns_exhausted is written as JSON true", payload.get("turns_exhausted") is True,
                  repr(payload.get("turns_exhausted")))
    h.test_result("failure_class None is written as JSON null", "failure_class" in payload and payload["failure_class"] is None,
                  repr(payload.get("failure_class")))
    no_turns = fl.format_split_attempt_line(record(turns_exhausted=False, failure_class="turn-limit"))
    no_turns_payload = json.loads(no_turns.split(" SPLIT_ATTEMPT ", 1)[1])
    h.test_result("turns_exhausted False is written as JSON false",
                  no_turns_payload.get("turns_exhausted") is False, repr(no_turns_payload.get("turns_exhausted")))
    parsed_nt = fl.parse_split_attempt_lines(write_log([no_turns]))
    h.test_result("a false turns_exhausted round-trips as a boolean False",
                  parsed_nt.records and parsed_nt.records[0].turns_exhausted is False and parsed_nt.records[0].failure_class == "turn-limit",
                  f"records={parsed_nt.records} malformed={parsed_nt.malformed_lines}")

    print("[Validation] the reader rejects wrong-typed and unversioned lines without raising")
    stringy = line.replace('"turns_exhausted": true', '"turns_exhausted": "true"')
    bad_bool = fl.parse_split_attempt_lines(write_log([stringy]))
    h.test_result("string turns_exhausted is not returned as a record",
                  len(bad_bool.records) == 0, f"records={bad_bool.records}")
    h.test_result("string turns_exhausted is reported as malformed",
                  len(bad_bool.malformed_lines) == 1, f"malformed={bad_bool.malformed_lines}")

    no_version = line.replace('"schema_version": 1, ', "")
    bad_version = fl.parse_split_attempt_lines(write_log([no_version]))
    h.test_result("line without schema_version is reported as malformed, not parsed",
                  len(bad_version.records) == 0 and len(bad_version.malformed_lines) == 1,
                  f"records={bad_version.records} malformed={bad_version.malformed_lines}")

    non_object = "2026-11-02T10:00:00Z implement SPLIT_ATTEMPT [1, 2]"
    bad_shape = fl.parse_split_attempt_lines(write_log([non_object]))
    h.test_result("non-object JSON is reported as malformed", len(bad_shape.malformed_lines) == 1,
                  f"malformed={bad_shape.malformed_lines}")

    print("[Tolerance] good lines survive alongside malformed ones; prose is ignored")
    mixed = fl.parse_split_attempt_lines(write_log([
        "2026-11-02T10:00:00Z the verifier said SPLIT_ATTEMPT is interesting",
        non_object,
        line,
        stringy,
    ]))
    h.test_result("the one valid record is still returned beside malformed lines",
                  len(mixed.records) == 1 and mixed.records[0] == original,
                  f"records={mixed.records}")
    h.test_result("prose mentioning SPLIT_ATTEMPT is not reported as malformed",
                  len(mixed.malformed_lines) == 2, f"malformed={mixed.malformed_lines}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
