#!/usr/bin/env python3
"""Tests for scripts/implement-ledger.py (run ledger and fail-closed finish for /implement-with-haiku).

Covers: the step-id and skip-reason vocabularies, the skip matrix per round-sizing class, the
ledger line grammar (run key, evidence, malformed rows), --pause and resume seeding, the summary
render, locate (session hit, repo-toplevel fallback, supersede, exit 3), and finish (success,
#259 incident replay recorded as steps_skipped, idempotent second finish, fail-closed on a missing
pointer or ledger).

Every CLI run (in-process via tests/_inproc.py, plus one subprocess end-to-end case) runs with
CLAUDE_CODE_SESSION_ID removed, IMPLEMENT_LEDGER_STATE_DIR and HOME
pointed at a temp dir, and finish --log/--state-dir under the same temp dir, so no real telemetry
or pointer state is touched.

Run with: python3 tests/test_implement_ledger.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT
from _inproc import run_python

SCRIPT = REPO_ROOT / "scripts" / "implement-ledger.py"
_spec = importlib.util.spec_from_file_location("implement_ledger", SCRIPT)
il = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(il)
STEP_IDS = il.STEP_IDS
SKIP_REASONS = il.SKIP_REASONS

# Plan Step 3 vocabulary, pinned here so a silent rename in the helper fails loudly.
EXPECTED_STEP_IDS = {
    "round1", "integration-gate", "round-sizing", "pause", "usage-check-round1-join",
    "fanout", "round2-apply", "partc-dedup", "partc-reference-check", "partc-mutation-a",
    "partc-mutation-b", "round3-followup", "round4", "usage-check-final", "full-check",
}
EXPECTED_SKIP_REASONS = {
    "mechanical", "test-only", "no-test-files-changed", "dirty-file",
    "no-comparison-operator", "no-new-tests", "pause-not-requested", "no-round1-units",
}
PAUSE_ROWS = {"pause", "usage-check-round1-join"}
MECHANICAL_REQUIRED = ["round1", "integration-gate", "round-sizing", "usage-check-final", "full-check"]
PARTC_ROWS = ["partc-dedup", "partc-reference-check", "partc-mutation-a", "partc-mutation-b"]

SESSION = "sess-ledger-test"
START_SHA = "0123456789abcdef0123456789abcdef01234567"
SHA_A = "1111111111111111111111111111111111111111"
SHA_B = "2222222222222222222222222222222222222222"


def _out(proc):
    return (proc.stdout or "") + (proc.stderr or "")


def _brief(proc):
    text = _out(proc).strip().replace("\n", " | ")
    return f"rc={proc.returncode} output={text[:400]!r}"


def _mentions(text, path):
    """True if the path (as given or resolved, for macOS /var -> /private/var) appears in text."""
    p = Path(path)
    return str(p) in text or str(p.resolve()) in text


class Sandbox:
    """Temp dirs for one test: state, home, a git repo (for toplevel), metrics state, and a log."""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.metrics_state = self.root / "metrics-state"
        self.log = self.root / "events.jsonl"
        for d in (self.state, self.home, self.repo, self.metrics_state):
            d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.repo_real = str(self.repo.resolve())

    def close(self):
        self._td.cleanup()

    def scratch(self, name):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def other_repo(self):
        """A second git repo under the same sandbox (for cross-repo resolution tests)."""
        d = self.root / "other-repo"
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        return d

    def run_e2e(self, args, session=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["IMPLEMENT_LEDGER_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.home)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(cwd or self.repo), env=env, capture_output=True, text=True, timeout=120,
        )

    def run(self, args, session=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["IMPLEMENT_LEDGER_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.home)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return run_python(
            [str(SCRIPT), *args],
            cwd=str(cwd or self.repo), env=env, replace_env=True,
        )

    def command_ends(self):
        if not self.log.exists():
            return []
        events = []
        for line in self.log.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
        return [e for e in events if e.get("event_type") == "command.end"]

    def all_events(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines() if ln.strip()]

    def pointers(self, run_key, suffix="pointer"):
        return sorted(self.state.rglob(f"{run_key}.{suffix}"))


class Run:
    """One /implement-with-haiku run: a run key, a scratch dir, and its ledger."""

    def __init__(self, sb, run_key, scratch_name="scratch"):
        self.sb = sb
        self.rk = run_key
        self.scratch = sb.scratch(scratch_name)
        self.ledger = self.scratch / "ledger.md"

    def init_e2e(self, *, session=SESSION, pause=False, carried=None, sha=START_SHA):
        args = ["init", "--scratch", str(self.scratch), "--start-sha", sha, "--run-key", self.rk]
        if session is not None:
            args += ["--session-id", session]
        if pause:
            args.append("--pause")
        if carried:
            args += ["--carried", carried]
        return self.sb.run_e2e(args)

    def init(self, *, session=SESSION, pause=False, carried=None, sha=START_SHA):
        args = ["init", "--scratch", str(self.scratch), "--start-sha", sha, "--run-key", self.rk]
        if session is not None:
            args += ["--session-id", session]
        if pause:
            args.append("--pause")
        if carried:
            args += ["--carried", carried]
        return self.sb.run(args)

    def evidence(self, name, text="ok\n"):
        path = self.scratch / f"{name}.txt"
        path.write_text(text)
        return str(path)

    def append(self, lines):
        with open(self.ledger, "a") as f:
            for line in lines:
                f.write(line + "\n")

    def check_e2e(self):
        return self.sb.run_e2e(["check", "--run-key", self.rk])

    def check(self):
        return self.sb.run(["check", "--run-key", self.rk])

    def render(self):
        return self.sb.run(["render", "--run-key", self.rk])

    def finish(self, session=None, with_key=True):
        args = ["finish"]
        if with_key:
            args += ["--run-key", self.rk]
        args += ["--log", str(self.sb.log), "--state-dir", str(self.sb.metrics_state)]
        return self.sb.run(args, session=session)


def _default_text(step):
    if step in PARTC_ROWS:
        return "result: 0 flagged\n"
    return f"{step}: ok\n"


def full_rows(run, *, cls="full", pause=False, drop=(), skip=None, text=None):
    """DONE rows for every row a class requires; `skip` maps step -> reason (SKIPPED rows)."""
    skip = skip or {}
    text = text or {}
    rows = []
    required = [s for s in STEP_IDS if pause or s not in PAUSE_ROWS]
    for step in required:
        if step in drop:
            continue
        if step in skip:
            rows.append(f"{run.rk} SKIPPED {step} {skip[step]}")
            continue
        content = f"class={cls}\n" if step == "round-sizing" else text.get(step, _default_text(step))
        rows.append(f"{run.rk} DONE {step} {run.evidence(step, content)}")
    return rows


def mechanical_rows(run, drop=()):
    rows = []
    for step in MECHANICAL_REQUIRED:
        if step in drop:
            continue
        content = "class=mechanical\n" if step == "round-sizing" else f"{step}: ok\n"
        rows.append(f"{run.rk} DONE {step} {run.evidence(step, content)}")
    return rows


def _check_rows(run):
    """Return (rc, combined output) for check."""
    r = run.check_e2e()
    return r.returncode, _out(r)


def test_vocabulary_pinned():
    """STEP_IDS and SKIP_REASONS equal the plan's closed vocabularies."""
    ok = set(STEP_IDS) == EXPECTED_STEP_IDS and set(SKIP_REASONS) == EXPECTED_SKIP_REASONS
    return ok, f"STEP_IDS extra={set(STEP_IDS) - EXPECTED_STEP_IDS} missing={EXPECTED_STEP_IDS - set(STEP_IDS)}; " \
               f"SKIP_REASONS extra={set(SKIP_REASONS) - EXPECTED_SKIP_REASONS} missing={EXPECTED_SKIP_REASONS - set(SKIP_REASONS)}"


def test_init_writes_ledger_and_pointer():
    """init creates <scratch>/ledger.md and a <run-key>.pointer under the state dir."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-init")
        r = run.init_e2e()
        ok = r.returncode == 0 and run.ledger.exists() and len(sb.pointers(run.rk)) == 1
        return ok, _brief(r)
    finally:
        sb.close()


def test_full_run_complete_finishes_success():
    """All rows DONE with evidence: check 0, finish 0, one command.end success, pointer gone."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-full-ok")
        run.init()
        run.append(full_rows(run))
        c = run.check()
        f = run.finish()
        ends = sb.command_ends()
        ok = (c.returncode == 0 and f.returncode == 0 and len(ends) == 1
              and ends[0].get("outcome") == {"status": "success"}
              and not sb.pointers(run.rk))
        return ok, f"check {_brief(c)}; finish {_brief(f)}; command.end={[e.get('outcome') for e in ends]}; " \
                   f"pointer_left={[str(p) for p in sb.pointers(run.rk)]}"
    finally:
        sb.close()


def test_incident_replay_records_steps_skipped():
    """#259 replay: rows missing round3-followup, partc-*, round4 -> check 1, NOT RUN, steps_skipped."""
    sb = Sandbox()
    try:
        missing = ["round3-followup", *PARTC_ROWS, "round4"]
        run = Run(sb, "run-incident")
        run.init()
        run.append(full_rows(run, drop=missing))
        c = run.check()
        rendered = _out(run.render())
        f = run.finish()
        ends = sb.command_ends()
        problems = []
        if c.returncode != 1:
            problems.append(f"check rc expected 1, got {c.returncode}")
        for step in missing:
            if step not in _out(c):
                problems.append(f"check output does not name {step}")
            if not any(step in line and "NOT RUN" in line for line in rendered.splitlines()):
                problems.append(f"render has no NOT RUN line for {step}")
        if f.returncode == 0:
            problems.append("finish exited 0 on an incomplete run")
        if len(ends) != 1 or ends[0].get("outcome") != {"status": "failure", "class": "steps_skipped"}:
            problems.append(f"command.end outcomes={[e.get('outcome') for e in ends]}")
        if not sb.pointers(run.rk):
            problems.append("pointer was removed on a failed finish")
        bad_schema = [e for e in sb.all_events() if telemetry_schema.validate_event(e)]
        if bad_schema:
            problems.append(f"log events fail telemetry validation: {bad_schema}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_mechanical_run_passes():
    """Mechanical class: only round1, integration-gate, round-sizing, usage-check-final required."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-mech")
        run.init()
        run.append(mechanical_rows(run))
        rc, out = _check_rows(run)
        return rc == 0, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_test_only_skip_round2_apply_only_for_test_only():
    """round2-apply SKIPPED test-only passes on a test-only run and fails on a full run."""
    sb = Sandbox()
    try:
        t_run = Run(sb, "run-testonly", "scratch-t")
        t_run.init()
        t_run.append(full_rows(t_run, cls="test-only", skip={"round2-apply": "test-only"}))
        rc_t, out_t = _check_rows(t_run)

        f_run = Run(sb, "run-full-skip", "scratch-f")
        f_run.init()
        f_run.append(full_rows(f_run, cls="full", skip={"round2-apply": "test-only"}))
        rc_f, out_f = _check_rows(f_run)

        ok = (rc_t == 0 and rc_f == 1
              and "SKIPPED round2-apply test-only is not permitted for class full" in out_f)
        return ok, f"test-only check rc={rc_t} ({out_t[:200]!r}); full check rc={rc_f} output={out_f[:200]!r}"
    finally:
        sb.close()


def test_round4_no_test_files_skip_passes():
    """round4 SKIPPED no-test-files-changed passes on a full run."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-r4")
        run.init()
        run.append(full_rows(run, skip={"round4": "no-test-files-changed"}))
        rc, out = _check_rows(run)
        return rc == 0, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_mutation_dirty_file_skip_passes():
    """partc-mutation-a SKIPPED dirty-file passes; partc-mutation-b SKIPPED dirty-file passes."""
    sb = Sandbox()
    try:
        results = []
        for name, step in (("run-mut-a", "partc-mutation-a"), ("run-mut-b", "partc-mutation-b")):
            run = Run(sb, name, name)
            run.init()
            run.append(full_rows(run, skip={step: "dirty-file"}))
            rc, out = _check_rows(run)
            results.append((step, rc, out[:200]))
        bad = [f"{s} rc={rc} {o!r}" for s, rc, o in results if rc != 0]
        return not bad, f"failed: {bad}" if bad else "ok"
    finally:
        sb.close()


def test_mutation_b_no_comparison_operator_skip_passes():
    """partc-mutation-b SKIPPED no-comparison-operator passes on a full run."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-mut-cmp")
        run.init()
        run.append(full_rows(run, skip={"partc-mutation-b": "no-comparison-operator"}))
        rc, out = _check_rows(run)
        return rc == 0, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_malformed_skip_reasons_fail():
    """SKIPPED round4 context and SKIPPED partc-dedup effort are malformed and fail the gate."""
    sb = Sandbox()
    try:
        problems = []
        for name, step, reason in (("run-bad-r4", "round4", "context"),
                                   ("run-bad-pd", "partc-dedup", "effort")):
            run = Run(sb, name, name)
            run.init()
            run.append(full_rows(run, drop={step}) + [f"{run.rk} SKIPPED {step} {reason}"])
            rc, out = _check_rows(run)
            if rc != 1 or f"unknown skip reason {reason!r}" not in out:
                problems.append(f"SKIPPED {step} {reason} not rejected: rc={rc} output={out[:200]!r}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_done_with_missing_evidence_fails():
    """A DONE row whose evidence file does not exist fails the gate."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-missing-ev")
        run.init()
        rows = full_rows(run, drop={"round4"})
        rows.append(f"{run.rk} DONE round4 {run.scratch / 'does-not-exist.txt'}")
        run.append(rows)
        rc, out = _check_rows(run)
        ok = rc == 1 and "evidence file is missing or unreadable" in out
        return ok, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_done_with_empty_evidence_fails():
    """A DONE row whose evidence file is empty fails (partc-dedup must hold an explicit result line)."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-empty-ev")
        run.init()
        run.append(full_rows(run, text={"partc-dedup": ""}))
        rc, out = _check_rows(run)
        ok = rc == 1 and "evidence file is empty" in out and "line " in out
        return ok, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_foreign_run_key_does_not_satisfy_row():
    """A round4 line with a different run key does not satisfy round4 (gate fails, names round4).

    Control: the same ledger with the correct run key's round4 row passes, so the failure is
    attributable to the foreign key and not to the rest of the ledger.
    """
    sb = Sandbox()
    try:
        foreign = Run(sb, "run-foreign", "scratch-foreign")
        foreign.init()
        foreign.append(full_rows(foreign, drop={"round4"}))
        foreign.append([f"some-other-run DONE round4 {foreign.evidence('foreign-round4')}"])
        rc_foreign, out = _check_rows(foreign)

        control = Run(sb, "run-own-key", "scratch-own")
        control.init()
        control.append(full_rows(control))
        rc_control, _ = _check_rows(control)

        ok = (rc_foreign == 1 and "MISSING: round4" in out and "foreign run key" in out
              and rc_control == 0)
        return ok, f"foreign-key run rc={rc_foreign} output={out[:200]!r}; control rc={rc_control}"
    finally:
        sb.close()


def test_unknown_step_id_does_not_satisfy_required_row():
    """A row for an unknown step id (round-four) does not satisfy the required round4 row."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-unknown-step")
        run.init()
        rows = full_rows(run, drop={"round4"})
        rows.append(f"{run.rk} DONE round-four {run.evidence('bogus')}")
        run.append(rows)
        rc, out = _check_rows(run)
        ok = rc == 1 and "unknown step 'round-four'" in out and "MISSING: round4" in out
        return ok, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_pause_rows_required_under_pause():
    """init --pause makes pause and usage-check-round1-join required."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-pause")
        run.init(pause=True)
        run.append(full_rows(run, pause=True, drop=PAUSE_ROWS))
        rc_without, out = _check_rows(run)
        run.append([f"{run.rk} DONE {s} {run.evidence(s)}" for s in sorted(PAUSE_ROWS)])
        rc_with, _ = _check_rows(run)
        ok = rc_without == 1 and all(s in out for s in PAUSE_ROWS) and rc_with == 0
        return ok, f"missing pause rows rc={rc_without} output={out[:200]!r}; after adding rc={rc_with}"
    finally:
        sb.close()


def test_pause_rows_absent_without_pause_passes():
    """Without --pause, the pause rows are not expected and their absence passes."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-nopause")
        run.init(pause=False)
        run.append(full_rows(run, pause=False))
        rc, out = _check_rows(run)
        return rc == 0, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_resume_seed_satisfies_round1():
    """init --carried A..B seeds round1 so a resumed run needs no round1 row of its own."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-resume")
        r = run.init(carried=f"{SHA_A}..{SHA_B}")
        run.append(full_rows(run, drop={"round1"}))
        rc, out = _check_rows(run)
        return r.returncode == 0 and rc == 0, f"init {_brief(r)}; check rc={rc} output={out[:200]!r}"
    finally:
        sb.close()


def test_carried_token_on_other_step_is_malformed():
    """A carried: evidence token on round4 is malformed and does not satisfy round4."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-carried-other")
        run.init()
        rows = full_rows(run, drop={"round4"})
        rows.append(f"{run.rk} DONE round4 carried:{SHA_A}..{SHA_B}")
        run.append(rows)
        rc, out = _check_rows(run)
        ok = rc == 1 and "carried evidence is only valid for round1" in out and "MISSING: round4" in out
        return ok, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_missing_round_sizing_treated_as_full():
    """A mechanical-looking ledger with no round-sizing row fails closed (treated as full)."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-no-sizing")
        run.init()
        rows = [r for r in mechanical_rows(run, drop={"round-sizing"})]
        run.append(rows)
        rc, out = _check_rows(run)
        ok = rc == 1 and "MISSING: round-sizing" in out
        return ok, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def _non_required_for_mechanical():
    return [s for s in STEP_IDS if s not in MECHANICAL_REQUIRED and s not in PAUSE_ROWS]


def test_mechanical_non_required_rows_absent_or_skipped_both_pass():
    """Absent non-required rows and SKIPPED mechanical rows both pass; render shows skipped (mechanical)."""
    problems = []
    sb = Sandbox()
    try:
        absent = Run(sb, "run-mech-absent", "scratch-absent")
        absent.init()
        absent.append(mechanical_rows(absent))
        rc_a, out_a = _check_rows(absent)
        render_a = _out(absent.render())

        skipped = Run(sb, "run-mech-skipped", "scratch-skipped")
        skipped.init()
        skipped.append(mechanical_rows(skipped)
                       + [f"{skipped.rk} SKIPPED {s} mechanical" for s in _non_required_for_mechanical()])
        rc_s, out_s = _check_rows(skipped)
        render_s = _out(skipped.render())

        if rc_a != 0:
            problems.append(f"absent variant rc={rc_a} output={out_a[:200]!r}")
        if rc_s != 0:
            problems.append(f"SKIPPED-mechanical variant rc={rc_s} output={out_s[:200]!r}")
        for label, render in (("absent", render_a), ("skipped", render_s)):
            lines = render.splitlines()
            if not any("fanout" in ln and "skipped" in ln and "mechanical" in ln for ln in lines):
                problems.append(f"{label}: render lacks 'skipped (mechanical)' for fanout")
            if any(("NOT RUN" in ln) and any(s in ln for s in _non_required_for_mechanical()) for ln in lines):
                problems.append(f"{label}: render shows NOT RUN for a non-required row")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_required_absent_row_renders_not_run():
    """A required row absent from a mechanical run fails check and renders NOT RUN."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-mech-missing")
        run.init()
        run.append(mechanical_rows(run, drop={"integration-gate"}))
        rc, out = _check_rows(run)
        rendered = _out(run.render())
        ok = rc == 1 and "MISSING: integration-gate" in out and any(
            "integration-gate" in ln and "NOT RUN" in ln for ln in rendered.splitlines())
        return ok, f"check rc={rc}; render lines={[ln for ln in rendered.splitlines() if 'integration' in ln]}"
    finally:
        sb.close()


# (class, reason used for the SKIPPED full-check negative): full-check is required for every class
# (#338), and no skip reason is permitted for it.
FULL_CHECK_CLASSES = (("mechanical", "mechanical"), ("test-only", "test-only"), ("full", "dirty-file"))


def test_full_check_done_passes_and_missing_fails_for_every_class():
    """full-check DONE passes for mechanical, test-only and full; an absent full-check row fails each,
    and the render shows it NOT RUN."""
    problems = []
    sb = Sandbox()
    try:
        for cls, _reason in FULL_CHECK_CLASSES:
            ok_run = Run(sb, f"run-fc-ok-{cls}", f"scratch-fc-ok-{cls}")
            ok_run.init()
            if cls == "mechanical":
                ok_run.append(mechanical_rows(ok_run))
            else:
                ok_run.append(full_rows(ok_run, cls=cls))
            rc_ok, out_ok = _check_rows(ok_run)
            if rc_ok != 0:
                problems.append(f"{cls}: full-check DONE rejected rc={rc_ok} output={out_ok[:200]!r}")

            miss_run = Run(sb, f"run-fc-miss-{cls}", f"scratch-fc-miss-{cls}")
            miss_run.init()
            if cls == "mechanical":
                miss_run.append(mechanical_rows(miss_run, drop={"full-check"}))
            else:
                miss_run.append(full_rows(miss_run, cls=cls, drop={"full-check"}))
            rc_miss, out_miss = _check_rows(miss_run)
            rendered = _out(miss_run.render())
            if rc_miss != 1 or "MISSING: full-check" not in out_miss:
                problems.append(f"{cls}: absent full-check not rejected rc={rc_miss} output={out_miss[:200]!r}")
            if not any("full-check" in ln and "NOT RUN" in ln for ln in rendered.splitlines()):
                problems.append(f"{cls}: render has no NOT RUN line for full-check")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_full_check_skipped_is_rejected_for_every_class():
    """SKIPPED full-check is rejected for mechanical, test-only and full, whatever the reason."""
    problems = []
    sb = Sandbox()
    try:
        for cls, reason in FULL_CHECK_CLASSES:
            run = Run(sb, f"run-fc-skip-{cls}", f"scratch-fc-skip-{cls}")
            run.init()
            if cls == "mechanical":
                rows = mechanical_rows(run, drop={"full-check"})
            else:
                rows = full_rows(run, cls=cls, drop={"full-check"})
            run.append(rows + [f"{run.rk} SKIPPED full-check {reason}"])
            rc, out = _check_rows(run)
            if rc != 1 or "full-check" not in out:
                problems.append(f"{cls}: SKIPPED full-check {reason} not rejected rc={rc} output={out[:200]!r}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_locate_session_hit_and_run_key():
    """locate picks the current session's pointer; --run-key picks exactly that run."""
    sb = Sandbox()
    try:
        a = Run(sb, "run-loc-a", "scratch-loc-a")
        b = Run(sb, "run-loc-b", "scratch-loc-b")
        a.init(session="sess-A")
        b.init(session="sess-B")
        la = sb.run(["locate", "--repo", sb.repo_real], session="sess-A")
        lb = sb.run(["locate", "--repo", sb.repo_real], session="sess-B")
        lk = sb.run(["locate", "--run-key", a.rk], session="sess-B")
        problems = []
        if la.returncode != 0 or not _mentions(_out(la), a.scratch) or _mentions(_out(la), b.scratch):
            problems.append(f"session sess-A locate: {_brief(la)}")
        if lb.returncode != 0 or not _mentions(_out(lb), b.scratch):
            problems.append(f"session sess-B locate: {_brief(lb)}")
        if lk.returncode != 0 or not _mentions(_out(lk), a.scratch):
            problems.append(f"--run-key {a.rk} locate: {_brief(lk)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_locate_falls_back_to_repo_toplevel():
    """With a different session id, locate falls back to the unfinished pointer for this repo."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-loc-fallback")
        run.init(session="sess-original")
        r = sb.run(["locate", "--repo", sb.repo_real], session="sess-other")
        return r.returncode == 0 and _mentions(_out(r), run.scratch), _brief(r)
    finally:
        sb.close()


def test_locate_exits_3_when_nothing_found():
    """locate with no pointers at all exits 3."""
    sb = Sandbox()
    try:
        r = sb.run(["locate", "--repo", sb.repo_real], session="sess-none")
        return r.returncode == 3, _brief(r)
    finally:
        sb.close()


def test_init_supersedes_same_session_pointer():
    """A second init in the same session supersedes the first; locate and finish pick the new run."""
    sb = Sandbox()
    try:
        old = Run(sb, "run-old", "scratch-old")
        new = Run(sb, "run-new", "scratch-new")
        old.init(session=SESSION)
        new.init(session=SESSION)
        problems = []
        if sb.pointers(old.rk) or not sb.pointers(old.rk, "superseded"):
            problems.append("old pointer was not renamed to .superseded")
        loc = sb.run(["locate"], session=SESSION)
        if loc.returncode != 0 or not _mentions(_out(loc), new.scratch) or _mentions(_out(loc), old.scratch):
            problems.append(f"locate without --run-key did not pick the new run: {_brief(loc)}")
        new.append(full_rows(new))
        fin = sb.run(["finish", "--log", str(sb.log), "--state-dir", str(sb.metrics_state)], session=SESSION)
        ends = sb.command_ends()
        if fin.returncode != 0 or len(ends) != 1 or ends[0].get("outcome") != {"status": "success"}:
            problems.append(f"finish without --run-key: {_brief(fin)}; command.end={[e.get('outcome') for e in ends]}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_finish_twice_records_one_command_end():
    """A second finish after success is a no-op: still one command.end, exit 0, 'already finished'."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-twice")
        run.init()
        run.append(full_rows(run))
        first = run.finish()
        second = run.finish()
        ends = sb.command_ends()
        problems = []
        if first.returncode != 0:
            problems.append(f"first finish {_brief(first)}")
        if second.returncode != 0:
            problems.append(f"second finish {_brief(second)}")
        if len(ends) != 1:
            problems.append(f"command.end count={len(ends)}")
        if "already finished" not in _out(second).lower():
            problems.append("second finish does not say 'already finished'")
        if not sb.pointers(run.rk, "finished"):
            problems.append("no .finished tombstone after success")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_finish_missing_ledger_records_steps_skipped():
    """Pointer present but ledger deleted: finish fails closed with steps_skipped."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-no-ledger")
        run.init()
        run.append(full_rows(run))
        run.ledger.unlink()
        f = run.finish()
        ends = sb.command_ends()
        ok = (f.returncode != 0 and len(ends) == 1
              and ends[0].get("outcome") == {"status": "failure", "class": "steps_skipped"})
        return ok, f"finish {_brief(f)}; command.end={[e.get('outcome') for e in ends]}"
    finally:
        sb.close()


def test_finish_missing_pointer_records_steps_skipped():
    """No pointer and no tombstone for the run key: finish fails closed with steps_skipped."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-never-init")
        f = run.finish()
        ends = sb.command_ends()
        ok = (f.returncode != 0 and len(ends) == 1
              and ends[0].get("outcome") == {"status": "failure", "class": "steps_skipped"})
        return ok, f"finish {_brief(f)}; command.end={[e.get('outcome') for e in ends]}"
    finally:
        sb.close()


def test_test_only_partc_no_new_tests_skip_passes():
    """Test-only run: round2-apply SKIPPED test-only and partc-* SKIPPED no-new-tests passes."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-testonly-partc")
        run.init()
        skip = {"round2-apply": "test-only", **{s: "no-new-tests" for s in PARTC_ROWS}}
        run.append(full_rows(run, cls="test-only", skip=skip))
        rc, out = _check_rows(run)
        return rc == 0, f"check rc={rc} output={out[:300]!r}"
    finally:
        sb.close()


def test_steps_skipped_round_trips_through_schema():
    """steps_skipped builds a command.end that validates, and survives a JSON round trip."""
    event = telemetry_schema.build_event(
        "command.end",
        session_id="123",
        timestamp="2026-10-09T12:00:00Z",
        command_id="implement-with-haiku",
        outcome=telemetry_schema.outcome_failure("steps_skipped"),
    )
    errors = telemetry_schema.validate_event(event)
    reloaded = json.loads(json.dumps(event))
    ok = (not errors and telemetry_schema.validate_event(reloaded) == []
          and reloaded["outcome"] == {"status": "failure", "class": "steps_skipped"})
    return ok, f"errors={errors}"


def test_hand_appended_finished_does_not_bypass_gate():
    """A FINISHED line on an incomplete ledger (no tombstone) does not make finish succeed."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-hand-finished")
        run.init()
        run.append(full_rows(run, drop={"round4"}) + [f"{run.rk} FINISHED"])
        c = run.check()
        f = run.finish()
        ends = sb.command_ends()
        problems = []
        if c.returncode != 1 or "MISSING: round4" not in _out(c):
            problems.append(f"check {_brief(c)}")
        if f.returncode != 1:
            problems.append(f"finish on incomplete ledger with FINISHED line: {_brief(f)}")
        if [e.get("outcome") for e in ends] != [{"status": "failure", "class": "steps_skipped"}]:
            problems.append(f"command.end outcomes={[e.get('outcome') for e in ends]}")
        if sb.pointers(run.rk, "finished"):
            problems.append("tombstone written for an incomplete run")
        if not sb.pointers(run.rk):
            problems.append("live pointer removed on a failed finish")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_unknown_session_finish_does_not_resolve_other_repo():
    """Session unset, pointer in repo A; finish and locate from repo B must not resolve or tombstone it."""
    sb = Sandbox()
    try:
        other = sb.other_repo()
        run = Run(sb, "run-xrepo-unknown")
        run.init(session=None)
        run.append(full_rows(run))
        loc = sb.run(["locate"], cwd=other)
        f = sb.run(["finish", "--run-key", "run-xrepo-unknown", "--log", str(sb.log),
                    "--state-dir", str(sb.metrics_state)], cwd=other)
        ends = sb.command_ends()
        problems = []
        if loc.returncode != 3:
            problems.append(f"locate from other repo: {_brief(loc)}")
        if f.returncode != 3:
            problems.append(f"finish from other repo: {_brief(f)}")
        if any(e.get("outcome") == {"status": "success"} for e in ends):
            problems.append("success recorded for another repo's run")
        if not sb.pointers(run.rk) or sb.pointers(run.rk, "finished"):
            problems.append("repo A pointer was tombstoned by a finish from repo B")
        if "FINISHED" in run.ledger.read_text():
            problems.append("FINISHED written by a finish from another repo")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_foreign_tombstone_is_not_already_finished():
    """Run finished in repo A; finish with the same session from repo B exits 3, not 'already finished'."""
    sb = Sandbox()
    try:
        other = sb.other_repo()
        run = Run(sb, "run-xrepo-tomb")
        run.init(session=SESSION)
        run.append(full_rows(run))
        first = run.finish(session=SESSION)
        second = sb.run(["finish", "--run-key", run.rk, "--log", str(sb.log),
                         "--state-dir", str(sb.metrics_state)], session=SESSION, cwd=other)
        ends = [e.get("outcome") for e in sb.command_ends()]
        problems = []
        if first.returncode != 0:
            problems.append(f"first finish {_brief(first)}")
        if second.returncode != 3:
            problems.append(f"finish from other repo: {_brief(second)}")
        if "already finished" in _out(second).lower():
            problems.append("foreign tombstone reported as already finished")
        if ends != [{"status": "success"}, {"status": "failure", "class": "steps_skipped"}]:
            problems.append(f"command.end outcomes={ends}")
        if not sb.pointers(run.rk, "finished"):
            problems.append("repo A tombstone missing")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_success_event_failure_leaves_run_open():
    """command-end emission fails: finish exits 1, writes no FINISHED, keeps the pointer; a retry then succeeds."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-emit-fail")
        run.init()
        run.append(full_rows(run))
        blocker = sb.root / "blocker"
        blocker.write_text("not a directory\n")
        bad_log = blocker / "events.jsonl"
        f = sb.run(["finish", "--run-key", run.rk, "--log", str(bad_log),
                    "--state-dir", str(sb.metrics_state)])
        problems = []
        if f.returncode != 1 or "success event not recorded" not in _out(f):
            problems.append(f"finish with failing emission: {_brief(f)}")
        if "FINISHED" in run.ledger.read_text():
            problems.append("FINISHED written although success was not recorded")
        if sb.pointers(run.rk, "finished") or not sb.pointers(run.rk):
            problems.append("run tombstoned or pointer removed although success was not recorded")
        retry = run.finish()
        if retry.returncode != 0 or [e.get("outcome") for e in sb.command_ends()] != [{"status": "success"}]:
            problems.append(f"retry with working log: {_brief(retry)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


# --- bounded cross-session locate (#316): --flow-dir/--step derives the attempt start from log.md ---

def _now():
    return int(time.time())


def _iso(epoch):
    """The shape `date -u +%Y-%m-%dT%H:%M:%SZ` writes into log.md."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _flow(sb, name="flow-316"):
    """A flow dir under the sandbox HOME (the only place --flow-dir is accepted), with an empty log.md."""
    d = sb.home / ".claude" / "flows" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.md").write_text("")
    return d


def _log(flow, lines):
    (flow / "log.md").write_text("".join(f"{line}\n" for line in lines))


def _rewrite(paths, **fields):
    """Set each record field in every pointer/tombstone file (JSON); a None value deletes the field."""
    for p in paths:
        data = json.loads(p.read_text())
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        p.write_text(json.dumps(data))


def _start(sb, run, session, cwd=None):
    args = ["init", "--scratch", str(run.scratch), "--start-sha", START_SHA, "--run-key", run.rk,
            "--session-id", session]
    return sb.run(args, cwd=cwd)


def _finished(sb, rk, scratch, session, cwd=None, created=None):
    """Init, complete every required row, and finish a run (leaves a tombstone); created sets its created_epoch."""
    run = Run(sb, rk, scratch)
    init = _start(sb, run, session, cwd)
    if init.returncode != 0:
        raise AssertionError(f"init {rk}: {_brief(init)}")
    run.append(full_rows(run))
    fin = sb.run(["finish", "--run-key", rk, "--log", str(sb.log), "--state-dir", str(sb.metrics_state)],
                 session=session, cwd=cwd)
    if fin.returncode != 0:
        raise AssertionError(f"finish {rk}: {_brief(fin)}")
    if created is not None:
        _rewrite(sb.pointers(rk, "finished"), created_epoch=created)
    return run


def _pointer_only(sb, rk, scratch, session, created=None):
    """Init only (an unfinished pointer, as a session that never finished leaves it); created sets created_epoch."""
    run = Run(sb, rk, scratch)
    init = _start(sb, run, session)
    if init.returncode != 0:
        raise AssertionError(f"init {rk}: {_brief(init)}")
    if created is not None:
        _rewrite(sb.pointers(rk), created_epoch=created)
    return run


def _locate(sb, flow, step="implement", session="sess-S2", cwd=None):
    args = ["locate", "--repo", sb.repo_real, "--flow-dir", str(flow), "--step", step]
    return sb.run(args, session=session, cwd=cwd)


def test_cross_session_finished_run_found_with_bound():
    """Acceptance: S1 finished its run; S2 locates it via --flow-dir/--step (bounded scan reaches the tombstone)."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 30)} implement begin"])
        s1 = _finished(sb, "run-x-s1", "scratch-x-s1", "sess-S1")
        r = _locate(sb, flow, "implement", session="sess-S2")
        out = _out(r)
        problems = []
        if r.returncode != 0:
            problems.append(f"locate: {_brief(r)}")
        if not re.search(rf"RUN_KEY:\s*{re.escape(s1.rk)}\b", out):
            problems.append(f"S1 RUN_KEY not printed: {_brief(r)}")
        if "already finished" not in out:
            problems.append("tombstone not reported as already finished")
        if "SINCE:" not in out:
            problems.append("SINCE line not printed")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_same_tombstone_without_bound_is_not_found():
    """The same cross-session tombstone with no --flow-dir/--step (unbounded) still exits 3."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 30)} implement begin"])
        _finished(sb, "run-x-unbounded", "scratch-x-unb", "sess-S1")
        r = sb.run(["locate", "--repo", sb.repo_real], session="sess-S2")
        return r.returncode == 3, _brief(r)
    finally:
        sb.close()


def test_retry_with_leftover_pointer_returns_newer_tombstone():
    """D1: S1's unfinished pointer predates the newest retry line; S2's tombstone postdates it. S2's run wins.

    Fixture floors: the retry bound is now-55 (retry at now-50, less the 5 s skew) and the begin bound
    would be now-205. The leftover pointer's created_epoch (now-100) sits between the two floors, so it is
    eligible only under the begin bound. Scenario 1 (S2 present) checks that the newest eligible record
    wins. Scenario 2 (S2 absent) is the discriminator: with no newer record to win, the leftover pointer
    must not be returned, so the locate exits 3. A begin-bound implementation returns it and fails here.
    """
    problems = []
    now = _now()

    sb = Sandbox()
    try:
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 200)} implement begin", f"{_iso(now - 50)} implement retry"])
        old = _pointer_only(sb, "run-d1-old", "scratch-d1-old", "sess-S1", created=now - 100)
        new = _finished(sb, "run-d1-new", "scratch-d1-new", "sess-S2")
        r = _locate(sb, flow, "implement", session="sess-S2")
        if r.returncode != 0 or not _mentions(_out(r), new.scratch):
            problems.append(f"expected S2's run: {_brief(r)}")
        if _mentions(_out(r), old.scratch):
            problems.append("S1's leftover pointer returned ahead of the retry-era tombstone")
    finally:
        sb.close()

    sb = Sandbox()
    try:
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 200)} implement begin", f"{_iso(now - 50)} implement retry"])
        _pointer_only(sb, "run-d1-only", "scratch-d1-only", "sess-S1", created=now - 100)
        r = _locate(sb, flow, "implement", session="sess-S2")
        if r.returncode != 3:
            problems.append(f"leftover pointer before the retry floor was not excluded (want exit 3): {_brief(r)}")
        if _mentions(_out(r), "scratch-d1-only"):
            problems.append("leftover pointer older than the retry floor was returned (begin bound used)")
    finally:
        sb.close()
    return not problems, "; ".join(problems) or "ok"


def test_stale_implement_run_does_not_pass_fix_audit():
    """A tombstone created before the newest fix begin line is outside the bound: fix locate exits 3."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _finished(sb, "run-stale-impl", "scratch-stale-impl", "sess-S1", created=now - 300)
        _log(flow, [f"{_iso(now - 100)} fix begin"])
        r = _locate(sb, flow, "fix", session="sess-S2")
        return r.returncode == 3, _brief(r)
    finally:
        sb.close()


def test_bounded_locate_filters_by_repo():
    """F12: a tombstone from another repo inside the window is not returned; exit 3."""
    sb = Sandbox()
    try:
        now = _now()
        other = sb.other_repo()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        _finished(sb, "run-other-repo", "scratch-other-repo", "sess-S1", cwd=other)
        landed = sb.pointers("run-other-repo", "finished")
        r = _locate(sb, flow, "implement", session="sess-S2")
        problems = []
        if not landed:
            problems.append("other-repo tombstone was not written (fixture problem)")
        if r.returncode != 3:
            problems.append(f"another repo's tombstone was returned: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_missing_begin_or_retry_line_exits_3_with_reason():
    """No fix begin|retry line in log.md: exit 3 and stderr names the missing line."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        _finished(sb, "run-no-fix-line", "scratch-no-fix", "sess-S1")
        r = _locate(sb, flow, "fix", session="sess-S2")
        ok = r.returncode == 3 and "no begin or retry line for fix" in r.stderr
        return ok, _brief(r)
    finally:
        sb.close()


def test_forged_begin_lines_are_ignored_or_fail_closed():
    """Non-matching lines are ignored (older valid line used); a newest garbage-prefixed line fails closed."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        t0 = _iso(now - 100)
        _finished(sb, "run-forge", "scratch-forge", "sess-S1")
        _log(flow, [f"{t0} implement begin",
                    f"{_iso(now - 1)} implement begin extra",
                    f" {_iso(now - 1)} implement begin",
                    f"{_iso(now - 1)} implementx begin",
                    f"{_iso(now - 1)} fix begin"])
        ignored = _locate(sb, flow, "implement", session="sess-S2")
        problems = []
        if ignored.returncode != 0 or f"SINCE: {t0}" not in _out(ignored):
            problems.append(f"non-matching lines moved the bound: {_brief(ignored)}")
        _log(flow, [f"{t0} implement begin", "garbage implement begin"])
        garbage = _locate(sb, flow, "implement", session="sess-S2")
        if garbage.returncode != 3:
            problems.append(f"newest garbage line did not fail closed: {_brief(garbage)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_malformed_timestamp_on_newest_line_fails_closed():
    """2026-13-45T99:00:00Z as the newest matching line: exit 3, no fallback to the valid older line."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _finished(sb, "run-malformed-ts", "scratch-malformed", "sess-S1")
        _log(flow, [f"{_iso(now - 100)} implement begin", "2026-13-45T99:00:00Z implement begin"])
        r = _locate(sb, flow, "implement", session="sess-S2")
        return r.returncode == 3, _brief(r)
    finally:
        sb.close()


def test_naive_timestamp_handling():
    """A naive newest log line exits 3; --since without Z is an argparse error (2); --since with Z is accepted."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _finished(sb, "run-naive", "scratch-naive", "sess-S1")
        problems = []
        _log(flow, [f"{_iso(now - 100)} implement begin", "2026-10-19T15:58:14 implement begin"])
        naive_log = _locate(sb, flow, "implement", session="sess-S2")
        if naive_log.returncode != 3:
            problems.append(f"naive newest log line: {_brief(naive_log)}")
        _log(flow, [f"{_iso(now - 100)} implement begin"])
        naive_since = sb.run(["locate", "--repo", sb.repo_real, "--since", "2026-10-19T15:58:14"], session="sess-S2")
        if naive_since.returncode != 2:
            problems.append(f"naive --since not an argparse error: {_brief(naive_since)}")
        z_since = sb.run(["locate", "--repo", sb.repo_real, "--since", _iso(now - 60)], session="sess-S2")
        if z_since.returncode != 0:
            problems.append(f"Z --since rejected or not found: {_brief(z_since)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_skew_boundary_is_five_seconds():
    """created_epoch == since - 5 is eligible; since - 6 is not."""
    now = _now()
    b = now - 100
    sb_in = Sandbox()
    try:
        flow = _flow(sb_in)
        _log(flow, [f"{_iso(b)} implement begin"])
        _finished(sb_in, "run-skew-in", "scratch-skew-in", "sess-S1", created=b - 5)
        r_in = _locate(sb_in, flow, "implement", session="sess-S2")
        in_ok = r_in.returncode == 0 and re.search(r"RUN_KEY:\s*run-skew-in\b", _out(r_in)) is not None
    finally:
        sb_in.close()
    sb_out = Sandbox()
    try:
        flow = _flow(sb_out)
        _log(flow, [f"{_iso(b)} implement begin"])
        _finished(sb_out, "run-skew-out", "scratch-skew-out", "sess-S1", created=b - 6)
        r_out = _locate(sb_out, flow, "implement", session="sess-S2")
        out_ok = r_out.returncode == 3
    finally:
        sb_out.close()
    return in_ok and out_ok, f"since-5 eligible={in_ok} (rc={r_in.returncode}); since-6 excluded={out_ok} (rc={r_out.returncode})"


def test_run_created_in_begin_second_is_eligible():
    """Clock truncation: a run created in the same second as the begin line is eligible (5 s skew)."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now)} implement begin"])
        _finished(sb, "run-same-second", "scratch-same-second", "sess-S1")
        r = _locate(sb, flow, "implement", session="sess-S2")
        return r.returncode == 0, _brief(r)
    finally:
        sb.close()


def test_missing_created_epoch_is_not_eligible():
    """A pointer with no created_epoch is ineligible even with a fresh mtime; stderr counts it."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        run = _pointer_only(sb, "run-no-created", "scratch-no-created", "sess-S1")
        _rewrite(sb.pointers(run.rk), created_epoch=None)
        r = _locate(sb, flow, "implement", session="sess-S2")
        ok = r.returncode == 3 and "1 without created_epoch" in r.stderr
        return ok, _brief(r)
    finally:
        sb.close()


def test_unreadable_tombstone_fails_closed_with_exit_2():
    """A corrupt newest tombstone fails the bounded locate closed: exit 2, stderr names it, no run printed."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        _finished(sb, "run-unread-bad", "scratch-unread-bad", "sess-S1", created=now - 5)
        for p in sb.pointers("run-unread-bad", "finished"):
            p.write_text("{not json")
        _finished(sb, "run-unread-good", "scratch-unread-good", "sess-S3", created=now - 10)
        r = _locate(sb, flow, "implement", session="sess-S2")
        problems = []
        if r.returncode != 2:
            problems.append(f"unreadable record did not fail closed with exit 2: {_brief(r)}")
        if "unreadable" not in r.stderr or "run-unread-bad.finished" not in r.stderr:
            problems.append(f"stderr does not name the unreadable tombstone: {r.stderr.strip()[:200]!r}")
        if "RUN_KEY:" in _out(r):
            problems.append("a run was printed although the scan failed closed")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_non_finite_created_epoch_fails_closed_with_exit_2():
    """created_epoch NaN and Infinity parse as JSON but cannot be ordered: a bounded locate exits 2, not admits them."""
    problems = []
    for label, value in (("NaN", float("nan")), ("Infinity", float("inf"))):
        sb = Sandbox()
        try:
            now = _now()
            flow = _flow(sb)
            _log(flow, [f"{_iso(now - 60)} implement begin"])
            _finished(sb, f"run-nonfinite-{label.lower()}", f"scratch-nonfinite-{label.lower()}", "sess-S1",
                      created=value)
            _finished(sb, "run-nonfinite-ok", "scratch-nonfinite-ok", "sess-S3", created=now - 10)
            r = _locate(sb, flow, "implement", session="sess-S2")
            if r.returncode != 2 or "unreadable" not in r.stderr:
                problems.append(f"created_epoch {label}: want exit 2 with 'unreadable': {_brief(r)}")
            if "RUN_KEY:" in _out(r):
                problems.append(f"created_epoch {label}: a run was printed although the scan failed closed")
        finally:
            sb.close()
    return not problems, "; ".join(problems) or "ok"


def test_created_epoch_tie_is_deterministic_and_prefers_tombstone():
    """Equal created_epoch inside the window: a run key's tombstone beats its own pointer; across run keys the greater key wins."""
    problems = []
    now = _now()
    tie = now - 10

    sb = Sandbox()
    try:
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        _finished(sb, "run-tie-a", "scratch-tie-a", "sess-S1", created=tie)
        tomb = sb.pointers("run-tie-a", "finished")[0]
        (sb.state / "run-tie-a.pointer").write_text(tomb.read_text())
        r = _locate(sb, flow, "implement", session="sess-S2")
        if r.returncode != 0 or not re.search(r"RUN_KEY:\s*run-tie-a\b", _out(r)) or "already finished" not in _out(r):
            problems.append(f"same-key pointer and tombstone: tombstone not preferred: {_brief(r)}")
    finally:
        sb.close()

    # Create the tied records in both orders, so a directory-order coincidence cannot pass the check.
    for first, second in (("run-tie-a", "run-tie-b"), ("run-tie-b", "run-tie-a")):
        sb = Sandbox()
        try:
            flow = _flow(sb)
            _log(flow, [f"{_iso(now - 60)} implement begin"])
            _finished(sb, first, f"scratch-{first}", "sess-S1", created=tie)
            _finished(sb, second, f"scratch-{second}", "sess-S3", created=tie)
            r = _locate(sb, flow, "implement", session="sess-S2")
            if r.returncode != 0 or not re.search(r"RUN_KEY:\s*run-tie-b\b", _out(r)):
                problems.append(f"two run keys at one created_epoch (created {first} first): "
                                f"greater run key not chosen: {_brief(r)}")
        finally:
            sb.close()
    return not problems, "; ".join(problems) or "ok"


def test_bound_argument_validation():
    """--since with --flow-dir is 2; --flow-dir without --step is 2; --step without --flow-dir is 2;
    relative, outside-flows, and symlinked-log flow dirs are 3."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        _finished(sb, "run-validation", "scratch-validation", "sess-S1")
        outside = sb.home / "escape-target"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "log.md").write_text(f"{_iso(now - 60)} implement begin\n")
        linked = sb.home / ".claude" / "flows" / "flow-linked"
        linked.mkdir(parents=True, exist_ok=True)
        (linked / "log.md").symlink_to(flow / "log.md")
        since = _iso(now - 60)
        cases = [
            ("--since with --flow-dir", ["--since", since, "--flow-dir", str(flow), "--step", "implement"], None, 2),
            ("--flow-dir without --step", ["--flow-dir", str(flow)], None, 2),
            ("--step without --flow-dir", ["--step", "implement"], None, 2),
            ("relative --flow-dir", ["--flow-dir", ".claude/flows/flow-316", "--step", "implement"], sb.home, 3),
            ("flow dir outside HOME/.claude/flows", ["--flow-dir", str(outside), "--step", "implement"], None, 3),
            ("symlinked log.md", ["--flow-dir", str(linked), "--step", "implement"], None, 3),
        ]
        problems = []
        for label, extra, cwd, want in cases:
            r = sb.run(["locate", "--repo", sb.repo_real, *extra], session="sess-S2", cwd=cwd)
            if r.returncode != want:
                problems.append(f"{label}: want {want}, {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_attempt_start_is_newest_begin_or_retry_in_file_order():
    """Non-begin events never move the bound: the SINCE line is the newest begin|retry, not a later awaiting/resumed."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _finished(sb, "run-boundary", "scratch-boundary", "sess-S1")
        t1, t2, t3, t4, t5 = (_iso(now - d) for d in (500, 400, 300, 200, 100))
        problems = []
        _log(flow, [f"{t1} implement begin", f"{t2} implement failed", f"{t3} implement retry",
                    f"{t4} implement awaiting", f"{t5} implement resumed"])
        r = _locate(sb, flow, "implement", session="sess-S2")
        if r.returncode != 0 or f"SINCE: {t3}" not in _out(r):
            problems.append(f"begin/failed/retry/awaiting/resumed: want SINCE {t3}: {_brief(r)}")
        _log(flow, [f"{t1} implement begin", f"{t2} implement awaiting", f"{t3} implement resumed"])
        r = _locate(sb, flow, "implement", session="sess-S2")
        if r.returncode != 0 or f"SINCE: {t1}" not in _out(r):
            problems.append(f"begin/awaiting/resumed: want SINCE {t1}: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_hardening_cases_fail_closed():
    """CRLF, NUL, future-dated newest lines, a symlinked flow dir escaping HOME/.claude/flows, and a ../ path: all exit 3."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _finished(sb, "run-hardening", "scratch-hardening", "sess-S1")
        valid = f"{_iso(now - 100)} implement begin"
        cases = [
            ("CRLF newest line", [valid, f"{_iso(now - 50)} implement begin\r"]),
            ("NUL byte in newest candidate", [valid, f"{_iso(now - 50)}\x00 implement begin"]),
            ("trailing NUL on newest candidate", [valid, f"{_iso(now - 50)} implement begin\x00"]),
            ("future-dated newest line", [valid, f"{_iso(now + 3600)} implement begin"]),
        ]
        problems = []
        for label, lines in cases:
            _log(flow, lines)
            r = _locate(sb, flow, "implement", session="sess-S2")
            if r.returncode != 3:
                problems.append(f"{label}: {_brief(r)}")
        escape = sb.home / "escape-target"
        escape.mkdir(parents=True, exist_ok=True)
        (escape / "log.md").write_text(f"{valid}\n")
        symlinked = sb.home / ".claude" / "flows" / "flow-symlinked"
        symlinked.symlink_to(escape, target_is_directory=True)
        r = _locate(sb, symlinked, "implement", session="sess-S2")
        if r.returncode != 3:
            problems.append(f"symlinked flow dir escaping flows: {_brief(r)}")
        dotdot = sb.home / ".claude" / "flows" / ".." / ".." / "escape-target"
        r = _locate(sb, dotdot, "implement", session="sess-S2")
        if r.returncode != 3:
            problems.append(f"../ path escaping flows: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_resolve_for_check_render_finish_unchanged_by_bound():
    """_resolve (check, render, finish without a run key) still does not resolve another session's tombstone."""
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 60)} implement begin"])
        s1 = _finished(sb, "run-resolve", "scratch-resolve", "sess-S1")
        fin = sb.run(["finish", "--log", str(sb.log), "--state-dir", str(sb.metrics_state)], session="sess-S2")
        chk = sb.run(["check"], session="sess-S2")
        ren = sb.run(["render"], session="sess-S2")
        problems = []
        if fin.returncode != 3:
            problems.append(f"finish without key resolved another session's tombstone: {_brief(fin)}")
        for label, r in (("check", chk), ("render", ren)):
            if r.returncode == 0 or _mentions(_out(r), s1.scratch):
                problems.append(f"{label} without key resolved S1's run: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_zero_unit_skip_reason_matrix():
    """#526: no-round1-units is permitted for round1 in class full only, never mechanical or Part C rows."""
    full_r1 = il.permitted_skip_reasons("full", "round1", False)
    mech_r1 = il.permitted_skip_reasons("mechanical", "round1", False)
    ref = il.permitted_skip_reasons("full", "partc-reference-check", False)
    mut_a = il.permitted_skip_reasons("full", "partc-mutation-a", False)
    mut_b = il.permitted_skip_reasons("full", "partc-mutation-b", False)
    ok = ("no-round1-units" in full_r1 and "no-round1-units" not in mech_r1
          and "no-round1-units" not in ref and "no-round1-units" not in mut_a
          and "no-round1-units" not in mut_b)
    return ok, f"full/round1={full_r1} mechanical/round1={mech_r1} ref={ref} mutA={mut_a} mutB={mut_b}"


def test_no_round1_units_rejected_without_zero_units_basis():
    """#526: SKIPPED round1 no-round1-units is bound to a zero-units run; an ordinary full run is rejected."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-zu-unbound")
        run.init()
        run.append(full_rows(run, cls="full", skip={"round1": "no-round1-units"}))
        rc, out = _check_rows(run)
        return rc != 0, f"rc={rc} out={out[:300]!r}"
    finally:
        sb.close()


# --- implement-ledger.py append (#548) -------------------------------------

APPEND_RE = re.compile(r'implement-ledger\.py"?\s+append\b')
COMMAND_DOC = REPO_ROOT / "commands" / "implement-with-haiku.md"
PROMPT_DOC = REPO_ROOT / "prompts" / "test-cleanup-reference.md"
GUARD_PATH = REPO_ROOT / "scripts" / "flow-bash-guard.py"


def _al_ledger_bytes(run):
    return run.ledger.read_bytes() if run.ledger.exists() else None


def _al_ev_forms(ev):
    """Accept the evidence path as given or with symlinks resolved (macOS /var -> /private/var)."""
    return {ev, str(Path(ev).resolve())}


def _al_append(run, args, cwd=None):
    return run.sb.run(["append", "--run-key", run.rk, *args], cwd=cwd)


def _al_fenced_blocks(text):
    """(info string, lines) for each fenced code block in a markdown text."""
    blocks, cur, info = [], None, ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            if cur is None:
                cur, info = [], s[3:].strip().lower()
            else:
                blocks.append((info, cur))
                cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def _al_bash_blocks(text):
    return [lines for info, lines in _al_fenced_blocks(text)
            if info.split()[:1] and info.split()[0] in ("bash", "sh", "shell", "zsh")]


def test_append_done_happy_path():
    """append --done writes '<K> DONE <step> <evidence>' as the last ledger line, prints APPENDED, exits 0."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-done")
        run.init()
        ev = run.evidence("a")
        r = _al_append(run, ["--done", "round1", "--evidence", ev])
        last = run.ledger.read_text().splitlines()[-1] if run.ledger.exists() else ""
        last_ok = any(last == f"{run.rk} DONE round1 {form}" for form in _al_ev_forms(ev))
        out_ok = any(r.stdout.strip() == f"APPENDED: {run.rk} DONE round1 {form}" for form in _al_ev_forms(ev))
        ends_nl = run.ledger.read_bytes().endswith(b"\n")
        ok = r.returncode == 0 and last_ok and out_ok and ends_nl
        return ok, f"{_brief(r)}; last_line={last!r}"
    finally:
        sb.close()


def test_append_done_happy_path_subprocess():
    """The same DONE append run as a real subprocess (end-to-end CLI path) exits 0 and writes the row."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-e2e")
        run.init_e2e()
        ev = run.evidence("a")
        r = sb.run_e2e(["append", "--run-key", run.rk, "--done", "round1", "--evidence", ev])
        last = run.ledger.read_text().splitlines()[-1] if run.ledger.exists() else ""
        ok = r.returncode == 0 and any(last == f"{run.rk} DONE round1 {form}" for form in _al_ev_forms(ev))
        return ok, f"{_brief(r)}; last_line={last!r}"
    finally:
        sb.close()


def test_append_skip_happy_path():
    """append --skip with a valid reason writes '<K> SKIPPED <step> <reason>' and exits 0."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-skip")
        run.init()
        r = _al_append(run, ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator"])
        last = run.ledger.read_text().splitlines()[-1] if run.ledger.exists() else ""
        ok = r.returncode == 0 and last == f"{run.rk} SKIPPED partc-mutation-b no-comparison-operator"
        return ok, f"{_brief(r)}; last_line={last!r}"
    finally:
        sb.close()


def test_append_several_rows_per_step_in_order():
    """Two DONE round1 rows with different evidence, and DONE then SKIPPED for one step, all exit 0 in order."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-multi")
        run.init()
        e1 = run.evidence("first", "one\n")
        e2 = run.evidence("second", "two\n")
        r1 = _al_append(run, ["--done", "round1", "--evidence", e1])
        r2 = _al_append(run, ["--done", "round1", "--evidence", e2])
        r3 = _al_append(run, ["--done", "partc-mutation-a", "--evidence", e1])
        r4 = _al_append(run, ["--skip", "partc-mutation-a", "--reason", "dirty-file"])
        codes_ok = all(r.returncode == 0 for r in (r1, r2, r3, r4))
        lines = run.ledger.read_text().splitlines()
        tail = lines[-4:]
        order_ok = (len(tail) == 4
                    and tail[0] in {f"{run.rk} DONE round1 {f}" for f in _al_ev_forms(e1)}
                    and tail[1] in {f"{run.rk} DONE round1 {f}" for f in _al_ev_forms(e2)}
                    and tail[2] in {f"{run.rk} DONE partc-mutation-a {f}" for f in _al_ev_forms(e1)}
                    and tail[3] == f"{run.rk} SKIPPED partc-mutation-a dirty-file")
        ok = codes_ok and order_ok
        return ok, f"codes={[r.returncode for r in (r1, r2, r3, r4)]}; tail={tail!r}"
    finally:
        sb.close()


def test_append_end_to_end_check_complete():
    """Mechanical run written only through append: check exits 0 with CHECK: COMPLETE and no PROBLEM line."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-check")
        run.init()
        rcs = []
        for step in ("round1", "integration-gate", "round-sizing", "full-check", "usage-check-final"):
            content = "class=mechanical\n" if step == "round-sizing" else f"{step}: ok\n"
            rcs.append(_al_append(run, ["--done", step, "--evidence", run.evidence(step, content)]).returncode)
        c = run.check()
        out = _out(c)
        ok = all(rc == 0 for rc in rcs) and c.returncode == 0 and "CHECK: COMPLETE" in out and "PROBLEM:" not in out
        return ok, f"append rcs={rcs}; check {_brief(c)}"
    finally:
        sb.close()


def test_append_unknown_step_rejected():
    """--done with an unknown step id exits 1, stderr says 'append: rejected:', ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-unk-step")
        run.init()
        before = _al_ledger_bytes(run)
        r = _al_append(run, ["--done", "not-a-step", "--evidence", run.evidence("a")])
        ok = r.returncode == 1 and "append: rejected:" in _out(r) and _al_ledger_bytes(run) == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_unknown_skip_reason_rejected():
    """--skip with a reason outside the vocabulary exits 1 and leaves the ledger byte-identical."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-unk-reason")
        run.init()
        before = _al_ledger_bytes(run)
        r = _al_append(run, ["--skip", "round4", "--reason", "because"])
        ok = r.returncode == 1 and "append: rejected:" in _out(r) and _al_ledger_bytes(run) == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_evidence_rules_rejected():
    """Relative path, absolute path outside scratch, missing file, empty file, whitespace-only file: each exit 1, ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-evidence")
        run.init()
        outside = sb.root / "outside.txt"
        outside.write_text("outside\n")
        empty = run.scratch / "empty.txt"
        empty.write_text("")
        blank = run.scratch / "blank.txt"
        blank.write_text("  \n\t\n")
        cases = {
            "relative": "a.txt",
            "outside-scratch": str(outside),
            "missing": str(run.scratch / "missing.txt"),
            "empty": str(empty),
            "whitespace-only": str(blank),
        }
        problems = []
        for label, ev in cases.items():
            before = _al_ledger_bytes(run)
            r = _al_append(run, ["--done", "round1", "--evidence", ev])
            if r.returncode != 1 or "append: rejected:" not in _out(r) or _al_ledger_bytes(run) != before:
                problems.append(f"{label}: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_carried_evidence_rejected():
    """carried:<sha>..<sha> evidence is rejected for append (W1): exit 1, stderr names carried, ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-carried")
        run.init()
        before = _al_ledger_bytes(run)
        r = _al_append(run, ["--done", "round1", "--evidence", "carried:abcdef1..1234567"])
        ok = r.returncode == 1 and "carried" in _out(r) and _al_ledger_bytes(run) == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_injection_and_control_chars_rejected():
    """Embedded newline, CR, tab in --evidence; control char in --done or --reason; trailing space in --reason: all exit 1, ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-inject")
        run.init()
        ev = run.evidence("a")
        cases = [
            ("evidence newline injection", ["--done", "round1", "--evidence", f"{ev}\n{run.rk} FINISHED"]),
            ("evidence CR", ["--done", "round1", "--evidence", ev + "\r"]),
            ("evidence tab", ["--done", "round1", "--evidence", ev + "\t"]),
            ("done control char", ["--done", "round1\x01", "--evidence", ev]),
            ("reason newline", ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator\n"]),
            ("reason tab", ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator\t"]),
            ("reason trailing space", ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator "]),
        ]
        problems = []
        for label, args in cases:
            before = _al_ledger_bytes(run)
            r = _al_append(run, args)
            if r.returncode != 1 or _al_ledger_bytes(run) != before:
                problems.append(f"{label}: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_argument_shape_exit_2():
    """Bad argument shapes and an invalid run key exit 2 and leave the ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-shape")
        run.init()
        ev = run.evidence("a")
        K = run.rk
        cases = [
            ("no --run-key", ["--done", "round1", "--evidence", ev]),
            ("neither --done nor --skip", ["--run-key", K]),
            ("both --done and --skip", ["--run-key", K, "--done", "round1", "--skip", "partc-mutation-a",
                                        "--reason", "dirty-file", "--evidence", ev]),
            ("--done without --evidence", ["--run-key", K, "--done", "round1"]),
            ("--done with --reason", ["--run-key", K, "--done", "round1", "--evidence", ev, "--reason", "dirty-file"]),
            ("--skip without --reason", ["--run-key", K, "--skip", "dirty-file"]),
            ("--skip with --evidence", ["--run-key", K, "--skip", "dirty-file", "--reason", "dirty-file",
                                        "--evidence", ev]),
            ("invalid run key ../x", ["--run-key", "../x", "--done", "round1", "--evidence", ev]),
        ]
        problems = []
        before = _al_ledger_bytes(run)
        for label, args in cases:
            r = sb.run(["append", *args])
            if r.returncode != 2:
                problems.append(f"{label}: rc={r.returncode}")
        if _al_ledger_bytes(run) != before:
            problems.append("ledger changed")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_no_pointer_exit_3():
    """append for a run key that was never initialised exits 3 with the 'no pointer or tombstone' message."""
    sb = Sandbox()
    try:
        run = Run(sb, "never-initialised")
        ev = run.evidence("a")
        r = sb.run(["append", "--run-key", "never-initialised", "--done", "round1", "--evidence", ev])
        ok = r.returncode == 3 and "no pointer or tombstone found for run key never-initialised" in _out(r)
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_repo_guard_exit_4():
    """append run from a different git repo exits 4, names both the recorded and the current repo, ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-repo")
        run.init()
        before = _al_ledger_bytes(run)
        other = sb.other_repo()
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")], cwd=other)
        out = _out(r)
        ok = (r.returncode == 4 and _mentions(out, sb.repo) and _mentions(out, other)
              and _al_ledger_bytes(run) == before)
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_superseded_exit_5():
    """After a second init in the same session supersedes the first, append to the first exits 5 and leaves it unchanged."""
    sb = Sandbox()
    try:
        run1 = Run(sb, "run-app-sup-one", "s1-scratch")
        run1.init(session="s1", sha=SHA_A)
        run2 = Run(sb, "run-app-sup-two", "s2-scratch")
        run2.init(session="s1", sha=SHA_B)
        before = _al_ledger_bytes(run1)
        r = _al_append(run1, ["--done", "round1", "--evidence", run1.evidence("a")])
        ok = r.returncode == 5 and "superseded" in _out(r) and _al_ledger_bytes(run1) == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_finished_tombstone_exit_6():
    """After a successful finish, append exits 6 with 'already finished' and leaves the ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-tomb")
        run.init()
        run.append(mechanical_rows(run))
        f = run.finish()
        before = _al_ledger_bytes(run)
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("late")])
        ok = (f.returncode == 0 and r.returncode == 6 and "already finished" in _out(r)
              and _al_ledger_bytes(run) == before)
        return ok, f"finish {_brief(f)}; append {_brief(r)}"
    finally:
        sb.close()


def test_append_finished_row_without_tombstone_exit_6():
    """A '<K> FINISHED' row in the ledger with the pointer still present: append exits 6, ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-fin-row")
        run.init()
        run.append([f"{run.rk} FINISHED"])
        before = _al_ledger_bytes(run)
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")])
        ok = r.returncode == 6 and _al_ledger_bytes(run) == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_missing_ledger_exit_2():
    """With the ledger deleted, append exits 2 and does not create the ledger."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-noledger")
        run.init()
        run.ledger.unlink()
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")])
        ok = r.returncode == 2 and not run.ledger.exists()
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_symlinked_ledger_exit_2():
    """A ledger that is a symlink: append exits 2 and the symlink target is not written."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-symlink")
        run.init()
        target = sb.root / "symlink-target.md"
        target.write_text("target\n")
        run.ledger.unlink()
        os.symlink(target, run.ledger)
        before = target.read_bytes()
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")])
        ok = r.returncode == 2 and target.read_bytes() == before
        return ok, _brief(r)
    finally:
        sb.close()


def test_append_never_writes_finished_or_hash_lines():
    """Lines appended by successful calls are never '#' lines and never '<K> FINISHED'."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-noforbidden")
        run.init()
        before_lines = run.ledger.read_text().splitlines()
        e1 = run.evidence("a")
        rcs = [
            _al_append(run, ["--done", "round1", "--evidence", e1]).returncode,
            _al_append(run, ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator"]).returncode,
        ]
        new = run.ledger.read_text().splitlines()[len(before_lines):]
        bad = [ln for ln in new if ln.startswith("#") or ln == f"{run.rk} FINISHED"]
        ok = all(rc == 0 for rc in rcs) and len(new) == 2 and not bad
        return ok, f"rcs={rcs}; new={new!r}"
    finally:
        sb.close()


def test_append_write_short_exit_2():
    """When os.write returns fewer bytes than the row, append exits 2 and no APPENDED line."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-short-write")
        run.init()
        ev = run.evidence("a")

        # Patch os.write to return only 1 byte instead of the full write
        def short_write(fd, data):
            return 1

        with mock.patch('os.write', side_effect=short_write):
            r = _al_append(run, ["--done", "round1", "--evidence", ev])

        # Exit code should be 2
        # No APPENDED line in output
        # Error message should mention short write
        exit_ok = r.returncode == 2
        appended_ok = "APPENDED:" not in _out(r)
        error_ok = "short write" in _out(r)
        ok = exit_ok and appended_ok and error_ok
        return ok, f"rc={r.returncode} appended_in_output={'APPENDED:' in _out(r)} error_msg_ok={error_ok} {_brief(r)}"
    finally:
        sb.close()


def test_append_write_oserror_exit_2():
    """When os.write raises OSError, append exits 2 and no APPENDED line."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-write-error")
        run.init()
        ev = run.evidence("a")

        # Patch os.write to raise OSError
        def write_error(fd, data):
            raise OSError("Simulated write error")

        with mock.patch('os.write', side_effect=write_error):
            r = _al_append(run, ["--done", "round1", "--evidence", ev])

        # Exit code should be 2
        # No APPENDED line in output
        # Error message should mention the OSError
        exit_ok = r.returncode == 2
        appended_ok = "APPENDED:" not in _out(r)
        error_ok = "Simulated write error" in _out(r)
        ok = exit_ok and appended_ok and error_ok
        return ok, f"rc={r.returncode} appended_in_output={'APPENDED:' in _out(r)} error_msg_ok={error_ok} {_brief(r)}"
    finally:
        sb.close()


def test_append_readonly_ledger_exit_nonzero():
    """A read-only ledger (mode 0444) causes append to exit 2 with no APPENDED line."""
    sb = Sandbox()
    try:
        # Skip if running as root (root ignores file modes)
        if os.geteuid() == 0:
            return True, "skipped (running as root)"

        run = Run(sb, "run-app-readonly")
        run.init()
        ev = run.evidence("a")

        # Make ledger read-only
        run.ledger.chmod(0o444)

        r = _al_append(run, ["--done", "round1", "--evidence", ev])

        # Exit code should be 2 (write failure)
        # No APPENDED line in output
        # Error message should mention permission denied or similar
        exit_ok = r.returncode == 2
        appended_ok = "APPENDED:" not in _out(r)
        error_ok = "append: cannot write ledger" in _out(r)
        ok = exit_ok and appended_ok and error_ok
        return ok, f"rc={r.returncode} appended_in_output={'APPENDED:' in _out(r)} error_msg_ok={error_ok} {_brief(r)}"
    finally:
        sb.close()


def test_append_help_and_docstring():
    """--help lists append; the module docstring has append in Usage and an append entry in Exit codes naming 0-6."""
    sb = Sandbox()
    try:
        r = sb.run(["--help"])
        help_ok = r.returncode == 0 and "append" in _out(r)
        doc = il.__doc__ or ""
        usage_idx = doc.find("Usage")
        usage_ok = usage_idx >= 0 and "append" in doc[usage_idx:]
        exit_idx = doc.find("Exit codes")
        exit_ok = False
        missing = []
        if exit_idx >= 0:
            sub = doc[exit_idx:]
            a_idx = sub.find("append")
            if a_idx >= 0:
                # The append entry ends at the first blank line, so digits from a neighbouring entry or
                # paragraph cannot satisfy the check.
                block = sub[a_idx:].split("\n\n", 1)[0]
                missing = [d for d in "0123456" if not re.search(rf"\b{d}\b", block)]
                exit_ok = not missing
        ok = help_ok and usage_ok and exit_ok
        return ok, f"help_ok={help_ok} usage_ok={usage_ok} exit_ok={exit_ok} missing_codes={missing}"
    finally:
        sb.close()


def test_append_doc_no_printf_redirect():
    """No '>> \"<LEDGER_PATH>\"' redirect in the two target docs, and none under commands/ or prompts/."""
    problems = []
    pat = re.compile(r'>>\s*"?<LEDGER_PATH>')
    for path in (COMMAND_DOC, PROMPT_DOC):
        text = path.read_text()
        if '>> "<LEDGER_PATH>"' in text:
            problems.append(f"{path.name} contains the literal redirect")
        if any(pat.search(ln) for ln in text.splitlines()):
            problems.append(f"{path.name} has a redirect line")
    for path in sorted(list((REPO_ROOT / "commands").glob("*.md")) + list((REPO_ROOT / "prompts").glob("*.md"))):
        if '>> "<LEDGER_PATH>"' in path.read_text():
            problems.append(f"{path.relative_to(REPO_ROOT)} contains the literal redirect")
    return not problems, "; ".join(problems) or "ok"


# A floor, not an exact count. #548 converted every ledger row write in the command doc and left 26 append lines
# inside bash fences. Adding call sites is fine, but fewer means one was deleted or turned back into a redirect.
MIN_APPEND_CALL_SITES = 26


def test_append_doc_call_site_counts():
    """Command doc has >=MIN_APPEND_CALL_SITES append lines inside bash fences, each with --run-key \"<RUN_KEY>\"; prompt doc has none."""
    cmd_lines = [ln for block in _al_bash_blocks(COMMAND_DOC.read_text()) for ln in block if APPEND_RE.search(ln)]
    prompt_lines = [ln for block in _al_bash_blocks(PROMPT_DOC.read_text()) for ln in block if APPEND_RE.search(ln)]
    missing_key = [ln for ln in cmd_lines if '--run-key "<RUN_KEY>"' not in ln]
    ok = len(cmd_lines) >= MIN_APPEND_CALL_SITES and not prompt_lines and not missing_key
    return ok, f"command append lines={len(cmd_lines)} prompt={len(prompt_lines)} without --run-key={len(missing_key)}"


def test_append_doc_separation():
    """No bash fence holds both an append line and a scratch redirect; append lines carry no &&, ;, ||, 2>/dev/null."""
    problems = []
    redirect = re.compile(r'>>?\s*"?<SCRATCH_PATH>/')
    for path in (COMMAND_DOC, PROMPT_DOC):
        for block in _al_bash_blocks(path.read_text()):
            app = [ln for ln in block if APPEND_RE.search(ln)]
            if app and any(redirect.search(ln) for ln in block):
                problems.append(f"{path.name}: append and scratch redirect share a fence")
            for ln in app:
                for bad in ("&&", ";", "||", "2>/dev/null"):
                    if bad in ln:
                        problems.append(f"{path.name}: append line has {bad!r}: {ln.strip()[:120]}")
                # A single pipe chains the append into another command. Double-quoted text is blanked first, so a
                # pipe inside a quoted argument (usage text such as STEP|STEP) is not mistaken for a chain.
                unquoted = re.sub(r'"[^"]*"', "", ln)
                if "|" in unquoted:
                    problems.append(f"{path.name}: append line has a pipe: {ln.strip()[:120]}")
    return not problems, "; ".join(problems[:5]) or "ok"


def test_append_doc_contract_prose():
    """Command doc states 'A non-zero `append` exit is a stop' and names printf, >> and Write as forbidden fallbacks."""
    text = COMMAND_DOC.read_text()
    stop_sentence = "A non-zero `append` exit is a stop"
    stop_idx = text.find(stop_sentence)
    has_stop = stop_idx >= 0
    # Anchor the other two checks to the stop sentence's own paragraph, so a stray printf or Write elsewhere
    # in the doc cannot satisfy them.
    paragraph = text[stop_idx:].split("\n\n", 1)[0] if has_stop else ""
    names = all(tok in paragraph for tok in ("printf", ">>", "Write"))
    forbidden = re.search(r"(forbidden|fall\s+back)", paragraph, re.IGNORECASE) is not None
    ok = has_stop and names and forbidden
    return ok, f"stop_sentence={has_stop} names={names} forbidden_word={forbidden}"


def test_append_doc_grants_unchanged():
    """allowed-tools frontmatter keeps the scoped implement-ledger and printf grants, and no broad python3 grant."""
    lines = COMMAND_DOC.read_text().splitlines()
    front = []
    if lines and lines[0].strip() == "---":
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            front.append(ln)
    front_text = "\n".join(front)
    scoped = 'Bash(python3 "$HOME/.claude/scripts/implement-ledger.py":*)' in front_text
    printf_grant = "Bash(printf:*)" in front_text
    broad = "Bash(python3:*)" in front_text or "Bash(python3 *)" in front_text
    ok = scoped and printf_grant and not broad
    return ok, f"scoped={scoped} printf={printf_grant} broad_python3={broad}"


def test_append_guard_passes_append():
    """flow-bash-guard does not deny the append form for flow-implement."""
    spec = importlib.util.spec_from_file_location("_al_flow_bash_guard", GUARD_PATH)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    cmd = 'python3 "$HOME/.claude/scripts/implement-ledger.py" append --run-key "K" --done round1 --evidence "/tmp/s/a.txt"'
    blocked, reason, _rule = guard.check_command_for_agent(cmd, "flow-implement")
    return not blocked, f"blocked={blocked} reason={str(reason)[:200]!r}"


def _al_subprocess(sb, args, timeout):
    """Run the real CLI as a subprocess with a hard timeout (for inputs that could block the reader)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["IMPLEMENT_LEDGER_STATE_DIR"] = str(sb.state)
    env["HOME"] = str(sb.home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(sb.repo), env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_append_line_separators_rejected():
    """U+0085, U+2028 and U+2029 in --evidence, --done or --reason exit 1 with 'line separator', ledger unchanged."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-sep")
        run.init()
        ev = run.evidence("a")
        problems = []
        for label, sep in (("U+0085", "\x85"), ("U+2028", " "), ("U+2029", " ")):
            cases = [
                ("evidence", ["--done", "round1", "--evidence", ev + sep]),
                ("done", ["--done", "round1" + sep, "--evidence", ev]),
                ("reason", ["--skip", "partc-mutation-b", "--reason", "no-comparison-operator" + sep]),
            ]
            for field, args in cases:
                before = _al_ledger_bytes(run)
                r = _al_append(run, args)
                if r.returncode != 1 or "line separator" not in _out(r) or _al_ledger_bytes(run) != before:
                    problems.append(f"{label} in {field}: {_brief(r)}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_special_ledger_exits_2_promptly():
    """A ledger that is a FIFO, or a symlink to /dev/zero, exits 2 within 5s with 'not a regular file'."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-special")
        run.init()
        ev = run.evidence("a")
        setups = {
            "fifo": lambda: os.mkfifo(run.ledger),
            "dev-zero symlink": lambda: os.symlink("/dev/zero", run.ledger),
        }
        problems = []
        for label, setup in setups.items():
            run.ledger.unlink()
            setup()
            try:
                r = _al_subprocess(sb, ["append", "--run-key", run.rk, "--done", "round1", "--evidence", ev], 5)
            except subprocess.TimeoutExpired:
                problems.append(f"{label}: hung past 5s")
                continue
            if r.returncode != 2 or "not a regular file" not in r.stderr:
                problems.append(f"{label}: rc={r.returncode} stderr={r.stderr.strip()[:120]!r}")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_pointer_bad_keys_exit_2():
    """A pointer missing or mis-typing run_key/scratch exits 2 with no traceback; ledger unchanged, pointer restored."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-badptr")
        run.init()
        pointers = sb.pointers(run.rk)
        if len(pointers) != 1:
            return False, f"expected one pointer, found {len(pointers)}"
        ptr = pointers[0]
        original = ptr.read_text()
        data = json.loads(original)
        before = _al_ledger_bytes(run)
        variants = {
            "missing scratch": {k: v for k, v in data.items() if k != "scratch"},
            "missing run_key": {k: v for k, v in data.items() if k != "run_key"},
            "scratch not a string": {**data, "scratch": 7},
            "run_key null": {**data, "run_key": None},
        }
        problems = []
        try:
            for label, bad in variants.items():
                ptr.write_text(json.dumps(bad))
                r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")])
                if r.returncode != 2 or "Traceback" in r.stderr or _al_ledger_bytes(run) != before:
                    problems.append(f"{label}: {_brief(r)}")
        finally:
            ptr.write_text(original)
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


def test_append_pointer_without_repo_reports_unknown():
    """A pointer with no repo field exits 4 and names the recorded repo as <unknown>, never 'None'."""
    sb = Sandbox()
    try:
        run = Run(sb, "run-app-norepo")
        run.init()
        pointers = sb.pointers(run.rk)
        if len(pointers) != 1:
            return False, f"expected one pointer, found {len(pointers)}"
        ptr = pointers[0]
        data = json.loads(ptr.read_text())
        data.pop("repo", None)
        ptr.write_text(json.dumps(data))
        other = sb.other_repo()
        r = _al_append(run, ["--done", "round1", "--evidence", run.evidence("a")], cwd=other)
        out = _out(r)
        ok = r.returncode == 4 and "belongs to repo <unknown>" in out and "None" not in out
        return ok, _brief(r)
    finally:
        sb.close()


TESTS = [
    ("STEP_IDS and SKIP_REASONS match the plan vocabulary", test_vocabulary_pinned),
    ("init writes ledger and pointer", test_init_writes_ledger_and_pointer),
    ("full run with all rows DONE finishes success", test_full_run_complete_finishes_success),
    ("#259 incident replay records steps_skipped and NOT RUN", test_incident_replay_records_steps_skipped),
    ("mechanical run with four required rows passes", test_mechanical_run_passes),
    ("round2-apply SKIPPED test-only: passes test-only, fails full", test_test_only_skip_round2_apply_only_for_test_only),
    ("round4 SKIPPED no-test-files-changed passes", test_round4_no_test_files_skip_passes),
    ("partc-mutation-a/b SKIPPED dirty-file passes", test_mutation_dirty_file_skip_passes),
    ("partc-mutation-b SKIPPED no-comparison-operator passes", test_mutation_b_no_comparison_operator_skip_passes),
    ("SKIPPED with out-of-class reason (context, effort) fails", test_malformed_skip_reasons_fail),
    ("DONE with missing evidence file fails", test_done_with_missing_evidence_fails),
    ("DONE with empty evidence file fails", test_done_with_empty_evidence_fails),
    ("row with a different run key does not satisfy", test_foreign_run_key_does_not_satisfy_row),
    ("row with an unknown step id does not satisfy the required row", test_unknown_step_id_does_not_satisfy_required_row),
    ("--pause makes pause rows required", test_pause_rows_required_under_pause),
    ("without --pause, pause rows may be absent", test_pause_rows_absent_without_pause_passes),
    ("init --carried satisfies round1", test_resume_seed_satisfies_round1),
    ("carried: token on another step is malformed", test_carried_token_on_other_step_is_malformed),
    ("missing round-sizing row is treated as full", test_missing_round_sizing_treated_as_full),
    ("mechanical: absent or SKIPPED non-required rows both pass", test_mechanical_non_required_rows_absent_or_skipped_both_pass),
    ("mechanical: required absent row fails and renders NOT RUN", test_required_absent_row_renders_not_run),
    ("full-check: DONE passes and absent fails for mechanical, test-only and full", test_full_check_done_passes_and_missing_fails_for_every_class),
    ("full-check: SKIPPED is rejected for mechanical, test-only and full", test_full_check_skipped_is_rejected_for_every_class),
    ("locate: session hit and --run-key", test_locate_session_hit_and_run_key),
    ("locate: falls back to repo toplevel when session differs", test_locate_falls_back_to_repo_toplevel),
    ("locate: exits 3 when nothing found", test_locate_exits_3_when_nothing_found),
    ("init supersedes same-session pointer; finish picks new run", test_init_supersedes_same_session_pointer),
    ("second finish is a no-op: one command.end", test_finish_twice_records_one_command_end),
    ("finish with missing ledger records steps_skipped", test_finish_missing_ledger_records_steps_skipped),
    ("finish with missing pointer records steps_skipped", test_finish_missing_pointer_records_steps_skipped),
    ("test-only: partc-* SKIPPED no-new-tests passes", test_test_only_partc_no_new_tests_skip_passes),
    ("steps_skipped round-trips through telemetry schema", test_steps_skipped_round_trips_through_schema),
    ("hand-appended FINISHED does not bypass the completeness gate", test_hand_appended_finished_does_not_bypass_gate),
    ("unknown session: finish and locate do not resolve another repo's pointer", test_unknown_session_finish_does_not_resolve_other_repo),
    ("foreign tombstone is not reported as already finished", test_foreign_tombstone_is_not_already_finished),
    ("failed success emission leaves the run open (no FINISHED, no tombstone)", test_success_event_failure_leaves_run_open),
    ("bounded locate: cross-session finished run is found with --flow-dir/--step", test_cross_session_finished_run_found_with_bound),
    ("unbounded locate: cross-session tombstone is still not found",test_same_tombstone_without_bound_is_not_found),
    ("bounded locate: retry with leftover pointer returns the newer tombstone (D1)", test_retry_with_leftover_pointer_returns_newer_tombstone),
    ("bounded locate: stale implement run does not pass the fix audit", test_stale_implement_run_does_not_pass_fix_audit),
    ("bounded locate: another repo's tombstone inside the window is not returned", test_bounded_locate_filters_by_repo),
    ("bounded locate: missing begin|retry line exits 3 with a reason", test_missing_begin_or_retry_line_exits_3_with_reason),
    ("bounded locate: forged begin lines ignored or fail closed", test_forged_begin_lines_are_ignored_or_fail_closed),
    ("bounded locate: malformed timestamp on newest line fails closed", test_malformed_timestamp_on_newest_line_fails_closed),
    ("bounded locate: naive timestamps (log line exit 3, --since argparse exit 2)", test_naive_timestamp_handling),
    ("bounded locate: created_epoch skew boundary is 5 s", test_skew_boundary_is_five_seconds),
    ("bounded locate: run created in the begin second is eligible", test_run_created_in_begin_second_is_eligible),
    ("bounded locate: pointer without created_epoch is not eligible and is counted", test_missing_created_epoch_is_not_eligible),
    ("bounded locate: unreadable tombstone fails closed with exit 2", test_unreadable_tombstone_fails_closed_with_exit_2),
    ("bounded locate: non-finite created_epoch fails closed with exit 2", test_non_finite_created_epoch_fails_closed_with_exit_2),
    ("bounded locate: created_epoch ties are deterministic and prefer the tombstone", test_created_epoch_tie_is_deterministic_and_prefers_tombstone),
    ("bounded locate: --flow-dir/--step/--since validation and flow-dir rules", test_bound_argument_validation),
    ("bounded locate: attempt start is the newest begin|retry in file order", test_attempt_start_is_newest_begin_or_retry_in_file_order),
    ("bounded locate: CRLF, NUL, future-dated, escaping symlink, and ../ fail closed", test_hardening_cases_fail_closed),
    ("check, render, finish without a key still do not resolve another session's tombstone", test_resolve_for_check_render_finish_unchanged_by_bound),
    ("zero-unit: no-round1-units permitted for round1 in class full only", test_zero_unit_skip_reason_matrix),
    ("zero-unit: SKIPPED round1 no-round1-units rejected without a zero-units basis", test_no_round1_units_rejected_without_zero_units_basis),
    ("append: DONE happy path (CLI, stdout, last ledger line)", test_append_done_happy_path),
    ("append: DONE happy path as a real subprocess", test_append_done_happy_path_subprocess),
    ("append: SKIPPED happy path", test_append_skip_happy_path),
    ("append: several rows per step, in order", test_append_several_rows_per_step_in_order),
    ("append: mechanical run written by append passes check", test_append_end_to_end_check_complete),
    ("append: unknown step rejected (exit 1)", test_append_unknown_step_rejected),
    ("append: unknown skip reason rejected (exit 1)", test_append_unknown_skip_reason_rejected),
    ("append: evidence rules (relative, outside, missing, empty, blank) exit 1", test_append_evidence_rules_rejected),
    ("append: carried evidence rejected (W1)", test_append_carried_evidence_rejected),
    ("append: injection and control characters rejected (exit 1)", test_append_injection_and_control_chars_rejected),
    ("append: argument shape and run-key validation exit 2", test_append_argument_shape_exit_2),
    ("append: no pointer exits 3", test_append_no_pointer_exit_3),
    ("append: repo guard exits 4", test_append_repo_guard_exit_4),
    ("append: superseded pointer exits 5", test_append_superseded_exit_5),
    ("append: finished tombstone exits 6", test_append_finished_tombstone_exit_6),
    ("append: FINISHED row without tombstone exits 6", test_append_finished_row_without_tombstone_exit_6),
    ("append: missing ledger exits 2 and is not created", test_append_missing_ledger_exit_2),
    ("append: symlinked ledger exits 2, target untouched", test_append_symlinked_ledger_exit_2),
    ("append: never writes '#' or FINISHED lines", test_append_never_writes_finished_or_hash_lines),
    ("append: write returns short count exits 2, no APPENDED", test_append_write_short_exit_2),
    ("append: write raises OSError exits 2, no APPENDED", test_append_write_oserror_exit_2),
    ("append: read-only ledger (0444) exits non-zero, no APPENDED", test_append_readonly_ledger_exit_nonzero),
    ("append: --help and module docstring document append and exit codes 0-6", test_append_help_and_docstring),
    ("append doc: no printf/>> ledger redirect in commands/ or prompts/", test_append_doc_no_printf_redirect),
    ("append doc: call-site counts and --run-key form", test_append_doc_call_site_counts),
    ("append doc: append never shares a fence with scratch redirects or chaining", test_append_doc_separation),
    ("append doc: non-zero exit is a stop; printf, >> and Write named as forbidden", test_append_doc_contract_prose),
    ("append doc: allowed-tools grants scoped and no broad python3 grant", test_append_doc_grants_unchanged),
    ("append guard: flow-implement does not deny the append form", test_append_guard_passes_append),
    ("append: U+0085, U+2028, U+2029 in any field rejected (exit 1)", test_append_line_separators_rejected),
    ("append: FIFO or /dev/zero symlink ledger exits 2 promptly", test_append_special_ledger_exits_2_promptly),
    ("append: pointer with missing or mistyped run_key/scratch exits 2", test_append_pointer_bad_keys_exit_2),
    ("append: pointer without repo reports <unknown> (exit 4)", test_append_pointer_without_repo_reports_unknown),
]


if __name__ == "__main__":
    h = Harness("IMPLEMENT-LEDGER TEST SUITE")

    for name, fn in TESTS:
        try:
            passed, msg = fn()
        except Exception:
            passed, msg = False, f"exception: {traceback.format_exc(limit=3)}"
        h.test_result(name, passed, msg if not passed else "")

    h.summarize_and_exit()
