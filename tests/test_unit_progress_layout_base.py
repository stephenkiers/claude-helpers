#!/usr/bin/env python3
"""layout_base_sha contract for scripts/unit-progress.py, exercised through its CLI only.

Covers: the `layout-base` subcommand's error paths (absent record, null value, invalid SHA,
legacy record with the key missing, each exit 3) and its success output; a fresh init with
--no-layout-base leaves the value null; a copy-forward init never re-pins a legacy record to
the new anchor; a scoped init with no main record does not pin.

Run with: python3 tests/test_unit_progress_layout_base.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

PROGRESS = REPO_ROOT / "scripts" / "unit-progress.py"
ANCHOR = "a" * 40
OTHER_ANCHOR = "b" * 40
SIZING = {
    "schema_version": 1,
    "kind": "plan",
    "steps": 2,
    "files": 2,
    "findings": None,
    "ceilings": {"steps": 8, "files": 12, "findings": 16},
    "min_units": 1,
    "status": "ok",
    "reason": "",
}


def run(args, scratch, stdin=None):
    cmd = [sys.executable, str(PROGRESS), "--step", "implement", "--scratch", str(scratch), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def init_payload():
    units = [{"unit_id": "u1", "sub_task": "one", "owned_files": ["a.py"]}]
    return json.dumps({"units": units, "sizing": SIZING, "basis": "single", "single_reason": "test"})


def init(scratch, anchor=ANCHOR, extra=()):
    return run(["init", "--anchor", anchor, "--base", anchor, *extra], scratch, stdin=init_payload())


def units_path(scratch):
    return Path(scratch) / "units" / "implement" / "units.json"


def rewrite_units(scratch, mutate):
    path = units_path(scratch)
    assert path.exists(), f"units.json missing: {path}"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def main():
    h = Harness("UNIT PROGRESS LAYOUT BASE")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("[layout-base] absent record exits 3")
        s_absent = root / "absent"
        s_absent.mkdir()
        code, out, err = run(["layout-base"], s_absent)
        h.test_result("absent record: exit 3", code == 3, f"rc={code} err={err!r}")
        h.test_result("absent record: nothing on stdout", out == "", f"out={out!r}")
        h.test_result("absent record: stderr says no units.json", "no units.json file found" in err, f"err={err!r}")

        print("[layout-base] null value exits 3")
        s_null = root / "null"
        code, _, err = init(s_null, extra=["--no-layout-base"])
        h.test_result("fresh init with --no-layout-base succeeds", code == 0, f"rc={code} err={err!r}")
        assert units_path(s_null).exists(), "units.json not written by init"
        code, out, err = run(["layout-base"], s_null)
        h.test_result("null layout_base_sha: exit 3", code == 3, f"rc={code} err={err!r}")
        h.test_result("null layout_base_sha: nothing on stdout", out == "", f"out={out!r}")
        h.test_result("null layout_base_sha: stderr says absent or null",
                      err == "unit-progress: layout-base: layout_base_sha field is absent or null", f"err={err!r}")

        print("[layout-base] invalid SHA value exits 3")
        s_bad = root / "bad"
        code, _, err = init(s_bad)
        h.test_result("setup: fresh init pins a valid anchor", code == 0, f"rc={code} err={err!r}")
        rewrite_units(s_bad, lambda d: d.__setitem__("layout_base_sha", "not-a-sha"))
        code, out, err = run(["layout-base"], s_bad)
        h.test_result("invalid SHA value: exit 3", code == 3, f"rc={code} err={err!r}")
        h.test_result("invalid SHA value: nothing on stdout", out == "", f"out={out!r}")
        h.test_result("invalid SHA value: stderr says not a valid SHA",
                      err == "unit-progress: layout-base: layout_base_sha is not a valid SHA", f"err={err!r}")

        print("[layout-base] legacy record with the key missing exits 3")
        s_legacy = root / "legacy"
        code, _, err = init(s_legacy)
        h.test_result("setup: fresh init for legacy record", code == 0, f"rc={code} err={err!r}")
        rewrite_units(s_legacy, lambda d: d.pop("layout_base_sha"))
        code, out, err = run(["layout-base"], s_legacy)
        h.test_result("legacy record (key missing): exit 3", code == 3, f"rc={code} err={err!r}")
        h.test_result("legacy record (key missing): stderr says absent or null",
                      err == "unit-progress: layout-base: layout_base_sha field is absent or null", f"err={err!r}")

        print("[layout-base] valid value prints exactly the anchor")
        s_ok = root / "ok"
        code, _, err = init(s_ok)
        h.test_result("setup: fresh init for valid record", code == 0, f"rc={code} err={err!r}")
        code, out, err = run(["layout-base"], s_ok)
        h.test_result("valid record: exit 0 and stdout is exactly the anchor",
                      code == 0 and out == ANCHOR, f"rc={code} out={out!r} err={err!r}")

        print("[Pinning] fresh init with --no-layout-base stores null")
        s_nopin = root / "nopin"
        code, _, err = init(s_nopin, extra=["--no-layout-base"])
        h.test_result("fresh --no-layout-base init succeeds", code == 0, f"rc={code} err={err!r}")
        assert units_path(s_nopin).exists(), "units.json not written"
        data = json.loads(units_path(s_nopin).read_text())
        h.test_result("fresh --no-layout-base init: key present and null",
                      "layout_base_sha" in data and data["layout_base_sha"] is None, f"data keys: {sorted(data)}")

        print("[Pinning] supersede of a legacy record does not re-pin to the new anchor")
        rewrite_units(s_legacy, lambda d: d.pop("layout_base_sha", None))
        code, _, err = run(["init", "--supersede", "--anchor", OTHER_ANCHOR, "--base", OTHER_ANCHOR],
                           s_legacy, stdin=init_payload())
        h.test_result("supersede of legacy record succeeds", code == 0, f"rc={code} err={err!r}")
        data = json.loads(units_path(s_legacy).read_text())
        h.test_result("supersede of legacy record: anchor updated, layout_base_sha still absent or null",
                      data.get("anchor_sha") == OTHER_ANCHOR and data.get("layout_base_sha") is None,
                      f"anchor_sha={data.get('anchor_sha')!r} layout_base_sha={data.get('layout_base_sha')!r}")
        code, _, _ = run(["layout-base"], s_legacy)
        h.test_result("supersede of legacy record: layout-base still exits 3", code == 3, f"rc={code}")

        print("[Pinning] scoped init with no main record does not pin")
        s_scoped = root / "scoped"
        s_scoped.mkdir()
        code, _, err = run(["--scope", "scoped-a1", "init", "--anchor", ANCHOR, "--base", ANCHOR],
                           s_scoped, stdin=init_payload())
        h.test_result("scoped init with no main record succeeds", code == 0, f"rc={code} err={err!r}")
        scoped_file = s_scoped / "units" / "implement" / "scoped-a1" / "units.json"
        assert scoped_file.exists(), f"scoped units.json not written: {scoped_file}"
        data = json.loads(scoped_file.read_text())
        h.test_result("scoped init with no main record: layout_base_sha stays null",
                      data.get("layout_base_sha") is None, f"layout_base_sha={data.get('layout_base_sha')!r}")

    check_layout_base_flow_dir(h)
    check_no_pin_without_source(h)

    h.summarize_and_exit()


def check_layout_base_flow_dir(h):
    """layout-base also works when the run is addressed by --flow-dir (the global flags come before
    the subcommand, as the caller passes them): a fresh init pins the anchor, and the value prints."""
    with tempfile.TemporaryDirectory() as tmp:
        # A flow dir must sit under $HOME/.claude/flows, so run under a temp HOME.
        home = Path(tmp) / "home"
        flow = home / ".claude" / "flows" / "repo" / "1-layout-base-abc123"
        flow.mkdir(parents=True)
        (flow / "log.md").write_text("")
        env = {**os.environ, "HOME": str(home)}
        init_cmd = [sys.executable, str(PROGRESS), "--step", "implement", "--flow-dir", str(flow),
                    "init", "--anchor", ANCHOR, "--base", ANCHOR]
        init_res = subprocess.run(init_cmd, capture_output=True, text=True, input=init_payload(), env=env)
        show_cmd = [sys.executable, str(PROGRESS), "--step", "implement", "--flow-dir", str(flow), "layout-base"]
        show_res = subprocess.run(show_cmd, capture_output=True, text=True, env=env)
        h.test_result("layout-base with --flow-dir prints the anchor pinned by a fresh init",
                      init_res.returncode == 0 and show_res.returncode == 0
                      and show_res.stdout.strip() == ANCHOR,
                      f"init rc={init_res.returncode} err={init_res.stderr.strip()!r} "
                      f"layout-base rc={show_res.returncode} out={show_res.stdout!r} "
                      f"err={show_res.stderr.strip()!r}")


def check_no_pin_without_source(h):
    """--split-finer and --supersede with no copy-forward source never pin: null plus a warning."""
    for flag in ("--split-finer", "--supersede"):
        with tempfile.TemporaryDirectory() as scratch:
            code, _out, err = init(scratch, extra=(flag,))
            data = json.loads(units_path(scratch).read_text()) if units_path(scratch).exists() else {}
            h.test_result(f"{flag} with no source record: layout_base_sha stays null and a warning is printed",
                          code == 0 and data.get("layout_base_sha") is None and "warning" in err
                          and "leaving layout_base_sha null" in err,
                          f"rc={code} sha={data.get('layout_base_sha')!r} err={err!r}")


if __name__ == "__main__":
    main()
