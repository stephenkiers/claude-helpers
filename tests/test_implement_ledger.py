#!/usr/bin/env python3
"""Tests for scripts/implement-ledger.py (run ledger and fail-closed finish for /implement-with-haiku).

Covers: the step-id and skip-reason vocabularies, the skip matrix per round-sizing class, the
ledger line grammar (run key, evidence, malformed rows), --pause and resume seeding, the summary
render, locate (session hit, repo-toplevel fallback, supersede, exit 3), and finish (success,
#259 incident replay recorded as steps_skipped, idempotent second finish, fail-closed on a missing
pointer or ledger).

Every subprocess runs with CLAUDE_CODE_SESSION_ID removed, IMPLEMENT_LEDGER_STATE_DIR and HOME
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT

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
    "partc-mutation-b", "round3-followup", "round4", "usage-check-final",
}
EXPECTED_SKIP_REASONS = {
    "mechanical", "test-only", "no-test-files-changed", "dirty-file",
    "no-comparison-operator", "no-new-tests", "pause-not-requested",
}
PAUSE_ROWS = {"pause", "usage-check-round1-join"}
MECHANICAL_REQUIRED = ["round1", "integration-gate", "round-sizing", "usage-check-final"]
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

    def run(self, args, session=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["IMPLEMENT_LEDGER_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.home)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(cwd or self.repo), env=env, capture_output=True, text=True, timeout=120,
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
    r = run.check()
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
        r = run.init()
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
    would be now-205. The leftover pointer's created_epoch (now-100) sits between the two floors. Newest
    eligible record wins, so S2's newer tombstone is returned under either bound; the assertion guards
    against the leftover pointer being returned, not against the bound's exact value.
    """
    sb = Sandbox()
    try:
        now = _now()
        flow = _flow(sb)
        _log(flow, [f"{_iso(now - 200)} implement begin", f"{_iso(now - 50)} implement retry"])
        old = _pointer_only(sb, "run-d1-old", "scratch-d1-old", "sess-S1", created=now - 100)
        new = _finished(sb, "run-d1-new", "scratch-d1-new", "sess-S2")
        r = _locate(sb, flow, "implement", session="sess-S2")
        problems = []
        if r.returncode != 0 or not _mentions(_out(r), new.scratch):
            problems.append(f"expected S2's run: {_brief(r)}")
        if _mentions(_out(r), old.scratch):
            problems.append("S1's leftover pointer returned ahead of the retry-era tombstone")
        return not problems, "; ".join(problems) or "ok"
    finally:
        sb.close()


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
