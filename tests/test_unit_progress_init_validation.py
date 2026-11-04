#!/usr/bin/env python3
"""Input validation for `init` in scripts/unit-progress.py, exercised through its CLI only.

Covers: unit ids must be unique non-empty strings, every unit needs an owned_files list, anchor and
base must be full hex SHAs, min_units must be at least 1, a rejected init writes no record, and a
rejected `--supersede` init leaves the existing record untouched (validation runs before archiving).

Run with: python3 tests/test_unit_progress_init_validation.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

PROGRESS = REPO_ROOT / "scripts" / "unit-progress.py"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def sizing(min_units=1):
    return {
        "schema_version": 1,
        "kind": "plan",
        "steps": 2,
        "files": 2,
        "findings": None,
        "ceilings": {"steps": 8, "files": 12, "findings": 16},
        "min_units": min_units,
        "status": "ok",
        "reason": "",
    }


def good_units():
    return [
        {"unit_id": "u1", "sub_task": "one", "owned_files": ["a.py"]},
        {"unit_id": "u2", "sub_task": "two", "owned_files": ["b.py"]},
    ]


def run(args, scratch, plan, stdin_obj=None, raw_stdin=None):
    text = raw_stdin if raw_stdin is not None else json.dumps(stdin_obj)
    cmd = [sys.executable, str(PROGRESS), "--step", "implement", "--scratch", str(scratch), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, input=text)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def init_args(plan, anchor=SHA, base=SHA, supersede=False):
    args = ["init", "--anchor", anchor, "--base", base, "--plan", str(plan)]
    if supersede:
        args.append("--supersede")
    return args


def payload(units=None, min_units=1, basis="sequential"):
    return {"units": good_units() if units is None else units, "sizing": sizing(min_units),
            "basis": basis, "single_reason": None}


def units_file(scratch):
    return Path(scratch) / "units" / "implement" / "units.json"


def main():
    h = Harness("UNIT PROGRESS INIT VALIDATION")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plan = root / "plan.md"
        plan.write_text("# plan\n")

        print("[Control] a valid init writes a record")
        s_ok = root / "ok"
        code, _, err = run(init_args(plan), s_ok, plan, payload())
        h.test_result("valid init succeeds and writes units.json", code == 0 and units_file(s_ok).exists(), err)

        print("[Unit ids] must be unique and non-empty strings")
        s_dup = root / "dup"
        dup_units = [
            {"unit_id": "u1", "sub_task": "one", "owned_files": ["a.py"]},
            {"unit_id": "u1", "sub_task": "two", "owned_files": ["b.py"]},
        ]
        code, _, err = run(init_args(plan), s_dup, plan, payload(units=dup_units))
        h.test_result("duplicate unit ids rejected", code != 0 and "unique" in err, f"rc={code} err={err!r}")
        h.test_result("duplicate-id rejection writes no record", not units_file(s_dup).exists())

        s_int = root / "int"
        int_units = [{"unit_id": 7, "sub_task": "one", "owned_files": ["a.py"]}]
        code, _, err = run(init_args(plan), s_int, plan, payload(units=int_units))
        h.test_result("non-string unit_id rejected", code != 0, f"rc={code} err={err!r}")
        h.test_result("non-string-id rejection writes no record", not units_file(s_int).exists())

        s_empty = root / "empty"
        empty_units = [{"unit_id": "", "sub_task": "one", "owned_files": ["a.py"]}]
        code, _, err = run(init_args(plan), s_empty, plan, payload(units=empty_units))
        h.test_result("empty unit_id rejected", code != 0, f"rc={code} err={err!r}")

        print("[Unit shape] owned_files must be a list")
        s_nofiles = root / "nofiles"
        code, _, err = run(init_args(plan), s_nofiles, plan,
                           payload(units=[{"unit_id": "u1", "sub_task": "one"}]))
        h.test_result("unit without owned_files rejected", code != 0, f"rc={code} err={err!r}")

        s_strfiles = root / "strfiles"
        code, _, err = run(init_args(plan), s_strfiles, plan,
                           payload(units=[{"unit_id": "u1", "sub_task": "one", "owned_files": "a.py"}]))
        h.test_result("owned_files given as a bare string rejected", code != 0, f"rc={code} err={err!r}")

        print("[Anchor and base] must be full hex SHAs")
        s_anchor = root / "anchor"
        code, _, err = run(init_args(plan, anchor="not-a-sha"), s_anchor, plan, payload())
        h.test_result("non-hex --anchor rejected", code != 0, f"rc={code} err={err!r}")
        h.test_result("bad-anchor rejection writes no record", not units_file(s_anchor).exists())

        s_base = root / "base"
        code, _, err = run(init_args(plan, base="xyz"), s_base, plan, payload())
        h.test_result("non-hex --base rejected", code != 0, f"rc={code} err={err!r}")

        print("[min_units] must be at least 1")
        s_min = root / "min"
        code, _, err = run(init_args(plan), s_min, plan, payload(min_units=0))
        h.test_result("min_units of 0 rejected by init", code != 0, f"rc={code} err={err!r}")
        h.test_result("min_units rejection writes no record", not units_file(s_min).exists())

        print("[Supersede] validation runs before the existing record is archived")
        s_sup = root / "sup"
        code, _, _ = run(init_args(plan), s_sup, plan, payload())
        h.test_result("setup: initial valid record", code == 0)
        before = units_file(s_sup).read_bytes()
        code, _, err = run(init_args(plan, supersede=True), s_sup, plan, payload(units=dup_units))
        h.test_result("invalid --supersede init is rejected", code != 0, f"rc={code} err={err!r}")
        h.test_result("rejected --supersede leaves the existing units.json byte-identical",
                      units_file(s_sup).exists() and units_file(s_sup).read_bytes() == before)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
