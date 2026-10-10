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
import subprocess
import sys
import tempfile
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

    def run(self, args, session=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["IMPLEMENT_LEDGER_STATE_DIR"] = str(self.state)
        env["HOME"] = str(self.home)
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(self.repo), env=env, capture_output=True, text=True, timeout=120,
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
        args = ["init", "--scratch", str(self.scratch), "--start-sha", sha,
                "--session-id", session, "--run-key", self.rk]
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
        rc_f, _ = _check_rows(f_run)

        ok = rc_t == 0 and rc_f != 0
        return ok, f"test-only check rc={rc_t} ({out_t[:200]!r}); full check rc={rc_f} (expected non-zero)"
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
            rc, _ = _check_rows(run)
            if rc == 0:
                problems.append(f"SKIPPED {step} {reason} passed the gate")
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
        return rc != 0 and "round4" in out, f"check rc={rc} output={out[:300]!r}"
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
        return rc != 0 and "partc-dedup" in out, f"check rc={rc} output={out[:300]!r}"
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

        ok = rc_foreign != 0 and "round4" in out and rc_control == 0
        return ok, f"foreign-key run rc={rc_foreign}; control rc={rc_control}"
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
        return rc != 0 and "round4" in out, f"check rc={rc} output={out[:300]!r}"
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
        ok = rc_without != 0 and all(s in out for s in PAUSE_ROWS) and rc_with == 0
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
        return rc != 0 and "round4" in out, f"check rc={rc} output={out[:300]!r}"
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
        return rc != 0 and "round-sizing" in out, f"check rc={rc} output={out[:300]!r}"
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
        ok = rc != 0 and "integration-gate" in out and any(
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
