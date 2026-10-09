#!/usr/bin/env python3
"""Tests for scripts/spike-manifest.py (spike.json state, resume derivation, spike listing).

Covers init/load/mark/add_command_id, fail-closed validation, artifact sentinels,
resume_point, list_spikes, resolve_spike, atomic writes, and the CLI exit-code contract.
All filesystem access goes through temp dirs.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "spike-manifest.py"
_spec = importlib.util.spec_from_file_location("spike_manifest", SCRIPT)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

EXPECTED_STAGES = [
    "gather-context", "decompose", "codebase-survey", "refine-questions",
    "expert-questions", "checkpoint", "research", "gap-check", "research-wave-2",
    "expert-assessment", "synthesize", "audit", "present",
]
EXPECTED_STATUSES = {"pending", "running", "done", "failed", "skipped"}

# Artifacts per the PR A contract. Sentinel stages write one file with a sentinel line;
# plain stages write the listed files.
SENTINEL_STAGES = {
    "codebase-survey": ("survey/q1.md", "<!-- survey-end -->"),
    "expert-questions": ("experts/alice-questions.md", "<!-- spike-questions-end -->"),
    "research": ("research/q1-1.md", "<!-- research-end -->"),
    "research-wave-2": ("research/wave-2/q2-1.md", "<!-- research-wave-2-end -->"),
    "expert-assessment": ("experts/alice-assessment.md", "<!-- spike-assessment-end -->"),
}
PLAIN_STAGES = {
    "gather-context": ["question.md", "README.md"],
    "decompose": ["questions.md"],
    "refine-questions": ["questions.md", "knowledge/findings.md"],
    "checkpoint": ["decisions.md"],
    "gap-check": ["knowledge/findings.md", "knowledge/sources.md"],
    "synthesize": ["synthesis.md"],
    "audit": ["audit.md"],
    "present": ["README.md"],
}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_artifacts(d, stage):
    d = Path(d)
    if stage in SENTINEL_STAGES:
        rel, sentinel = SENTINEL_STAGES[stage]
        _write(d / rel, "body text\n%s\n" % sentinel)
    else:
        for rel in PLAIN_STAGES[stage]:
            _write(d / rel, "content\n")


def _new_spike(root, name="alpha-run1", question="What is the best way?", slug="alpha", effort=2, **kw):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    sm.init_manifest(d, question, slug, effort, **kw)
    return d


def _edit_manifest(d, mutate):
    p = Path(d) / "spike.json"
    m = json.loads(p.read_text())
    mutate(m)
    p.write_text(json.dumps(m))


def _find_key(obj, key):
    """Find key at top level or one level nested (show may wrap the manifest). Returns None if absent."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            if isinstance(v, dict) and key in v:
                return v[key]
    return None


def _cli(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------- constants

def t_stages_exact_order():
    assert list(sm.STAGES) == EXPECTED_STAGES, "STAGES: %r" % (list(sm.STAGES),)


def t_statuses_exact_set():
    assert set(sm.STATUSES) == EXPECTED_STATUSES, "STATUSES: %r" % (sm.STATUSES,)


def t_stage_artifacts_cover_every_stage():
    assert set(sm.STAGE_ARTIFACTS) == set(EXPECTED_STAGES), \
        "STAGE_ARTIFACTS keys: %r" % (sorted(sm.STAGE_ARTIFACTS),)


# ---------------------------------------------------------------- init

def t_init_shape():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, question="Q?", slug="alpha", effort=3, models="opus",
                       experts=("alice", "bob"))
        m = sm.load_manifest(d)
    assert m["schema_version"] == 1, "schema_version %r" % m["schema_version"]
    assert m["question"] == "Q?", "question %r" % m["question"]
    assert m["slug"] == "alpha", "slug %r" % m["slug"]
    assert m["effort"] == 3, "effort %r" % m["effort"]
    assert m["models"] == "opus", "models %r" % m["models"]
    assert m["experts"] == ["alice", "bob"], "experts %r" % m["experts"]
    assert isinstance(m["created"], str) and m["created"], "created missing"
    assert isinstance(m["updated"], str) and m["updated"], "updated missing"
    assert isinstance(m["command_ids"], list), "command_ids not list"
    assert set(m["stages"]) == set(EXPECTED_STAGES), "stage keys: %r" % sorted(m["stages"])
    not_pending = {k: v for k, v in m["stages"].items() if v != "pending"}
    assert not not_pending, "fresh stages not pending: %r" % not_pending


def t_init_defaults_models_experts_commands():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        m = sm.load_manifest(d)
    assert m["models"] == "balanced", "default models %r" % m["models"]
    assert m["experts"] == [], "default experts %r" % m["experts"]
    assert m["command_ids"] == [], "default command_ids %r" % m["command_ids"]


def t_init_command_id_seeds_command_ids():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, command_id="cmd-1")
        m = sm.load_manifest(d)
    assert m["command_ids"] == ["cmd-1"], "command_ids %r" % m["command_ids"]


def t_init_refuses_reinit():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, question="original")
        before = (d / "spike.json").read_text()
        try:
            sm.init_manifest(d, "replacement", "alpha", 2)
        except sm.ManifestError:
            pass
        else:
            raise AssertionError("re-init of existing spike.json did not raise ManifestError")
        after = (d / "spike.json").read_text()
    assert before == after, "re-init modified existing spike.json"


# ---------------------------------------------------------------- fail closed on load

def t_load_missing_file_fails_closed():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "empty"
        d.mkdir()
        try:
            sm.load_manifest(d)
        except sm.ManifestError:
            return
        raise AssertionError("missing spike.json did not raise ManifestError")


def t_load_malformed_manifests_fail_closed():
    cases = {
        "invalid JSON": lambda d: (d / "spike.json").write_text("{not json"),
        "non-object JSON": lambda d: (d / "spike.json").write_text("[]"),
        "bad schema_version": lambda d: _edit_manifest(d, lambda m: m.update(schema_version=2)),
        "missing stage key": lambda d: _edit_manifest(d, lambda m: m["stages"].pop("present")),
        "extra stage key": lambda d: _edit_manifest(d, lambda m: m["stages"].update({"bogus-stage": "pending"})),
        "bad stage status": lambda d: _edit_manifest(d, lambda m: m["stages"].update({"decompose": "finished"})),
        "question wrong type": lambda d: _edit_manifest(d, lambda m: m.update(question=42)),
        "command_ids not a list": lambda d: _edit_manifest(d, lambda m: m.update(command_ids="abc")),
        "stages not an object": lambda d: _edit_manifest(d, lambda m: m.update(stages=list(EXPECTED_STAGES))),
        "effort out of 1-5": lambda d: _edit_manifest(d, lambda m: m.update(effort=9)),
    }
    failures = []
    for label, corrupt in cases.items():
        with tempfile.TemporaryDirectory() as root:
            d = _new_spike(root)
            corrupt(d)
            try:
                sm.load_manifest(d)
            except sm.ManifestError:
                continue
            except Exception as e:
                failures.append("%s raised %s instead of ManifestError" % (label, type(e).__name__))
                continue
            failures.append("%s loaded without error" % label)
    assert not failures, "; ".join(failures)


def t_load_valid_manifest_returns_dict():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        m = sm.load_manifest(d)
    assert isinstance(m, dict), "load_manifest returned %s" % type(m).__name__


# ---------------------------------------------------------------- mark

def t_mark_transitions_all_statuses():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for status in ["running", "done", "failed", "skipped"]:
            sm.mark_stage(d, "gather-context", status)
            got = sm.load_manifest(d)["stages"]["gather-context"]
            assert got == status, "after marking %s, stage reads %r" % (status, got)


def t_mark_only_touches_named_stage():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        sm.mark_stage(d, "decompose", "running")
        stages = sm.load_manifest(d)["stages"]
    assert stages["decompose"] == "running", "decompose %r" % stages["decompose"]
    others = {k: v for k, v in stages.items() if k != "decompose" and v != "pending"}
    assert not others, "other stages changed: %r" % others


def t_mark_invalid_stage_rejected():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        before = (d / "spike.json").read_text()
        try:
            sm.mark_stage(d, "not-a-stage", "done")
        except sm.ManifestError:
            pass
        else:
            raise AssertionError("unknown stage name accepted")
        after = (d / "spike.json").read_text()
    assert before == after, "rejected mark modified spike.json"


def t_mark_invalid_status_rejected():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        try:
            sm.mark_stage(d, "gather-context", "finished")
        except sm.ManifestError:
            pass
        else:
            raise AssertionError("unknown status accepted")
        got = sm.load_manifest(d)["stages"]["gather-context"]
    assert got == "pending", "stage changed despite rejected status: %r" % got


def t_mark_on_malformed_manifest_fails_closed():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        (d / "spike.json").write_text("{")
        try:
            sm.mark_stage(d, "gather-context", "done")
        except sm.ManifestError:
            return
        raise AssertionError("mark_stage on malformed spike.json did not raise ManifestError")


# ---------------------------------------------------------------- command ids

def t_add_command_id_appends():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        sm.add_command_id(d, "a")
        sm.add_command_id(d, "b")
        got = sm.load_manifest(d)["command_ids"]
    assert got == ["a", "b"], "command_ids %r" % got


def t_add_command_id_appends_after_seed():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, command_id="x")
        sm.add_command_id(d, "y")
        got = sm.load_manifest(d)["command_ids"]
    assert got == ["x", "y"], "command_ids %r" % got


def t_add_command_id_skips_duplicate_of_last():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        sm.add_command_id(d, "a")
        sm.add_command_id(d, "a")
        got = sm.load_manifest(d)["command_ids"]
    assert got == ["a"], "duplicate-last not skipped: %r" % got


def t_add_command_id_keeps_non_adjacent_duplicate():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for cid in ["a", "b", "a"]:
            sm.add_command_id(d, cid)
        got = sm.load_manifest(d)["command_ids"]
    assert got == ["a", "b", "a"], "non-adjacent duplicate handled wrongly: %r" % got


def t_add_command_id_skips_empty():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        sm.add_command_id(d, "")
        got = sm.load_manifest(d)["command_ids"]
    assert got == [], "empty id appended: %r" % got


# ---------------------------------------------------------------- artifacts and sentinels

def t_missing_artifacts_fresh_dir_every_stage():
    failures = []
    for stage in EXPECTED_STAGES:
        with tempfile.TemporaryDirectory() as root:
            d = _new_spike(root)
            missing = sm.missing_artifacts(d, stage)
            if not isinstance(missing, list) or not missing:
                failures.append(stage)
    assert not failures, "stages with no missing artifacts on fresh dir: %r" % failures


def t_missing_artifacts_empty_once_written_every_stage():
    failures = []
    for stage in EXPECTED_STAGES:
        with tempfile.TemporaryDirectory() as root:
            d = _new_spike(root)
            _write_artifacts(d, stage)
            missing = sm.missing_artifacts(d, stage)
            if missing:
                failures.append("%s -> %r" % (stage, missing))
    assert not failures, "artifacts written but still reported missing: " + "; ".join(failures)


def t_sentinel_absent_counts_as_missing():
    failures = []
    for stage, (rel, _sentinel) in SENTINEL_STAGES.items():
        with tempfile.TemporaryDirectory() as root:
            d = _new_spike(root)
            _write(d / rel, "body with no sentinel\n")
            if not sm.missing_artifacts(d, stage):
                failures.append(stage)
    assert not failures, "sentinel-less artifact not reported missing for: %r" % failures


def t_sentinel_must_be_last_nonblank_line():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write(d / "survey" / "q1.md", "body\n<!-- survey-end -->\nmore notes\n")
        missing = sm.missing_artifacts(d, "codebase-survey")
    assert missing, "sentinel followed by content was accepted as complete"


def t_trailing_blank_lines_after_sentinel_ok():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write(d / "survey" / "q1.md", "body\n<!-- survey-end -->\n\n\n")
        missing = sm.missing_artifacts(d, "codebase-survey")
    assert not missing, "trailing blank lines after sentinel rejected: %r" % missing


def t_glob_needs_at_least_one_match():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        (d / "survey").mkdir()
        missing = sm.missing_artifacts(d, "codebase-survey")
    assert missing, "empty survey/ directory treated as complete"


def t_glob_every_matched_file_needs_sentinel():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write(d / "survey" / "q1.md", "body\n<!-- survey-end -->\n")
        _write(d / "survey" / "q2.md", "body with no sentinel\n")
        missing = sm.missing_artifacts(d, "codebase-survey")
    assert missing, "one sentinel-less survey file went unnoticed"


# ---------------------------------------------------------------- resume_point

def t_resume_fresh_is_gather_context():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        rp = sm.resume_point(d)
    assert rp == "gather-context", "fresh resume_point %r" % (rp,)


def t_resume_done_with_artifacts_advances():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        rp = sm.resume_point(d)
    assert rp == "decompose", "resume_point %r" % (rp,)


def t_resume_done_without_artifacts_is_not_complete():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        sm.mark_stage(d, "decompose", "done")  # questions.md deliberately absent
        rp = sm.resume_point(d)
    assert rp == "decompose", "done-without-artifacts treated as complete: %r" % (rp,)


def t_resume_sentinel_missing_reruns_stage():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for s in ["gather-context", "decompose"]:
            _write_artifacts(d, s)
            sm.mark_stage(d, s, "done")
        _write(d / "survey" / "q1.md", "no sentinel here\n")
        sm.mark_stage(d, "codebase-survey", "done")
        rp = sm.resume_point(d)
    assert rp == "codebase-survey", "sentinel-less survey not re-run: %r" % (rp,)


def t_resume_sentinel_present_completes_stage():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for s in ["gather-context", "decompose", "codebase-survey"]:
            _write_artifacts(d, s)
            sm.mark_stage(d, s, "done")
        rp = sm.resume_point(d)
    assert rp == "refine-questions", "resume_point %r" % (rp,)


def t_resume_running_is_rerun_even_with_artifacts():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        _write_artifacts(d, "decompose")
        sm.mark_stage(d, "decompose", "running")
        rp = sm.resume_point(d)
    assert rp == "decompose", "running stage not re-run: %r" % (rp,)


def t_resume_failed_is_rerun_even_with_artifacts():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        _write_artifacts(d, "decompose")
        sm.mark_stage(d, "decompose", "failed")
        rp = sm.resume_point(d)
    assert rp == "decompose", "failed stage not re-run: %r" % (rp,)


def t_resume_skipped_counts_as_complete():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for s in ["gather-context", "decompose", "codebase-survey", "refine-questions"]:
            _write_artifacts(d, s)
            sm.mark_stage(d, s, "done")
        sm.mark_stage(d, "expert-questions", "skipped")  # no artifacts on purpose
        rp = sm.resume_point(d)
    assert rp == "checkpoint", "skipped stage not treated as complete: %r" % (rp,)


def t_resume_earliest_incomplete_wins():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        _write_artifacts(d, "checkpoint")
        sm.mark_stage(d, "checkpoint", "done")
        rp = sm.resume_point(d)
    assert rp == "decompose", "later done stage masked earlier incomplete one: %r" % (rp,)


def t_resume_all_complete_is_none():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for s in EXPECTED_STAGES:
            _write_artifacts(d, s)
            sm.mark_stage(d, s, "done")
        rp = sm.resume_point(d)
    assert rp is None, "all-complete resume_point %r, expected None" % (rp,)


def t_resume_all_complete_with_skips_is_none():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for s in EXPECTED_STAGES:
            if s in ("expert-questions", "expert-assessment", "audit"):
                sm.mark_stage(d, s, "skipped")
            else:
                _write_artifacts(d, s)
                sm.mark_stage(d, s, "done")
        rp = sm.resume_point(d)
    assert rp is None, "all complete-or-skipped resume_point %r, expected None" % (rp,)


# ---------------------------------------------------------------- list_spikes

def _complete_spike(root, name, question):
    d = _new_spike(root, name=name, question=question)
    for s in EXPECTED_STAGES:
        _write_artifacts(d, s)
        sm.mark_stage(d, s, "done")
    return d


def t_list_empty_root():
    with tempfile.TemporaryDirectory() as root:
        got = sm.list_spikes(root)
    assert got == [], "empty root listed %r" % got


def t_list_statuses_and_malformed_does_not_abort():
    with tempfile.TemporaryDirectory() as root:
        _complete_spike(root, "done-one", "Finished question?")
        partial = _new_spike(root, name="partial-two", question="Half-done question?")
        _write_artifacts(partial, "gather-context")
        sm.mark_stage(partial, "gather-context", "done")
        broken = Path(root) / "broken-three"
        broken.mkdir()
        (broken / "spike.json").write_text("{garbage")
        got = sm.list_spikes(root)
    by_name = {e["name"]: e for e in got}
    assert len(got) == 3, "expected 3 entries (malformed must not abort listing), got %d: %r" % (
        len(got), sorted(by_name))
    assert by_name["done-one"]["status"] == "complete", "done-one status %r" % by_name["done-one"]["status"]
    assert by_name["done-one"]["resume_point"] is None, "complete spike resume_point %r" % by_name["done-one"]["resume_point"]
    assert by_name["partial-two"]["status"] == "in-progress", "partial status %r" % by_name["partial-two"]["status"]
    assert by_name["partial-two"]["resume_point"] == "decompose", \
        "partial resume_point %r" % by_name["partial-two"]["resume_point"]
    assert by_name["broken-three"]["status"] == "malformed", "broken status %r" % by_name["broken-three"]["status"]


def t_list_entry_fields():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, name="field-check", question="Which field?")
        got = sm.list_spikes(root)
    assert len(got) == 1, "entries %d" % len(got)
    e = got[0]
    for key in ["dir", "name", "question", "status", "last_stage", "resume_point", "updated"]:
        assert key in e, "entry missing %r: %r" % (key, sorted(e))
    assert e["name"] == "field-check", "name %r" % e["name"]
    assert Path(e["dir"]).resolve() == d.resolve(), "dir %r" % e["dir"]
    assert e["question"] == "Which field?", "question %r" % e["question"]


def t_list_sorted_updated_desc():
    with tempfile.TemporaryDirectory() as root:
        old = _new_spike(root, name="older-one", question="old")
        new = _new_spike(root, name="newer-two", question="new")
        _edit_manifest(old, lambda m: m.update(updated="2026-01-01T00:00:00+00:00"))
        _edit_manifest(new, lambda m: m.update(updated="2026-06-01T00:00:00+00:00"))
        got = sm.list_spikes(root)
    names = [e["name"] for e in got]
    assert names == ["newer-two", "older-one"], "order %r (expected updated desc)" % names


# ---------------------------------------------------------------- resolve_spike

def t_resolve_exact_name():
    with tempfile.TemporaryDirectory() as root:
        target = _new_spike(root, name="alpha-run1")
        _new_spike(root, name="beta-run2")
        got = sm.resolve_spike(root, "alpha-run1")
    assert Path(got).resolve() == target.resolve(), "exact resolved to %r" % got


def t_resolve_unique_prefix():
    with tempfile.TemporaryDirectory() as root:
        _new_spike(root, name="alpha-run1")
        target = _new_spike(root, name="beta-run2")
        got = sm.resolve_spike(root, "beta")
    assert Path(got).resolve() == target.resolve(), "prefix resolved to %r" % got


def t_resolve_exact_beats_prefix():
    with tempfile.TemporaryDirectory() as root:
        target = _new_spike(root, name="alpha")
        _new_spike(root, name="alpha-two")
        got = sm.resolve_spike(root, "alpha")
    assert Path(got).resolve() == target.resolve(), "exact name lost to longer prefix match: %r" % got


def t_resolve_absolute_path_ok():
    with tempfile.TemporaryDirectory() as root:
        target = _new_spike(root, name="beta-run2")
        got = sm.resolve_spike(root, str(target))
    assert Path(got).resolve() == target.resolve(), "absolute path resolved to %r" % got


def t_resolve_returns_absolute_path():
    with tempfile.TemporaryDirectory() as root:
        _new_spike(root, name="gamma-solo")
        got = sm.resolve_spike(root, "gamma")
    assert Path(got).is_absolute(), "returned relative path %r" % got


def t_resolve_ambiguous_prefix_raises():
    with tempfile.TemporaryDirectory() as root:
        _new_spike(root, name="gamma-1")
        _new_spike(root, name="gamma-2")
        try:
            sm.resolve_spike(root, "gamma")
        except sm.ManifestError:
            return
        raise AssertionError("ambiguous prefix did not raise ManifestError")


def t_resolve_no_match_raises():
    with tempfile.TemporaryDirectory() as root:
        _new_spike(root, name="alpha-run1")
        try:
            sm.resolve_spike(root, "zzz")
        except sm.ManifestError:
            return
        raise AssertionError("no-match ref did not raise ManifestError")


# ---------------------------------------------------------------- atomic writes

def t_atomic_writes_leave_valid_json_each_step():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        cycle = ["running", "done", "failed", "skipped", "pending"]
        for i in range(20):
            sm.mark_stage(d, EXPECTED_STAGES[i % len(EXPECTED_STAGES)], cycle[i % len(cycle)])
            json.loads((d / "spike.json").read_text())  # must always parse
        sm.load_manifest(d)


def t_failed_replace_keeps_previous_manifest():
    real_replace = os.replace
    calls = []

    def boom(src, dst, *a, **kw):
        calls.append((str(src), str(dst)))
        raise OSError("simulated crash during rename")

    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        os.replace = boom
        try:
            try:
                sm.mark_stage(d, "decompose", "done")
            except Exception:
                pass
        finally:
            os.replace = real_replace
        assert calls, "mark_stage did not write via os.replace; atomicity not exercised"
        raw = (d / "spike.json").read_text()
        json.loads(raw)  # previous manifest must still be valid JSON
        status = sm.load_manifest(d)["stages"]["decompose"]
    assert status == "pending", "interrupted write left partial state: decompose=%r" % status


# ---------------------------------------------------------------- CLI

def t_cli_init_show_mark_add_list_resolve_flow():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "cli-spike"
        d.mkdir()
        r = _cli(["init", "--dir", str(d), "--question", "Q?", "--slug", "cli", "--effort", "2",
                  "--expert", "alice", "--expert", "bob", "--models", "opus", "--command-id", "cmd-9"])
        assert r.returncode == 0, "init rc=%d stderr=%s" % (r.returncode, r.stderr)
        json.loads(r.stdout)
        m = sm.load_manifest(d)
        assert m["experts"] == ["alice", "bob"], "experts %r" % m["experts"]
        assert m["models"] == "opus", "models %r" % m["models"]
        assert m["command_ids"] == ["cmd-9"], "command_ids %r" % m["command_ids"]

        r = _cli(["mark", "--dir", str(d), "--stage", "gather-context", "--status", "done"])
        assert r.returncode == 0, "mark rc=%d stderr=%s" % (r.returncode, r.stderr)
        assert sm.load_manifest(d)["stages"]["gather-context"] == "done", "mark not persisted"

        r = _cli(["show", "--dir", str(d)])
        assert r.returncode == 0, "show rc=%d stderr=%s" % (r.returncode, r.stderr)
        shown = json.loads(r.stdout)
        rp = _find_key(shown, "resume_point")
        # gather-context is marked done but its artifacts are absent, so it is not complete.
        assert rp == "gather-context", "show resume_point %r" % (rp,)

        r = _cli(["add-command-id", "--dir", str(d), "--command-id", "cmd-10"])
        assert r.returncode == 0, "add-command-id rc=%d stderr=%s" % (r.returncode, r.stderr)
        assert sm.load_manifest(d)["command_ids"] == ["cmd-9", "cmd-10"], "add-command-id not persisted"

        r = _cli(["list", "--root", root])
        assert r.returncode == 0, "list rc=%d stderr=%s" % (r.returncode, r.stderr)
        listed = json.loads(r.stdout)
        assert isinstance(listed, list) and len(listed) == 1, "list output %r" % (listed,)

        r = _cli(["resolve", "--root", root, "--ref", "cli"])
        assert r.returncode == 0, "resolve rc=%d stderr=%s" % (r.returncode, r.stderr)
        assert "cli-spike" in r.stdout, "resolve stdout %r" % r.stdout


def t_cli_init_reinit_exits_2():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "dup"
        d.mkdir()
        first = _cli(["init", "--dir", str(d), "--question", "Q", "--slug", "dup", "--effort", "2"])
        assert first.returncode == 0, "first init rc=%d" % first.returncode
        second = _cli(["init", "--dir", str(d), "--question", "Q", "--slug", "dup", "--effort", "2"])
    assert second.returncode == 2, "re-init rc=%d (expected 2)" % second.returncode
    assert "error:" in second.stderr, "stderr %r" % second.stderr


def t_cli_mark_invalid_stage_exits_2():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        r = _cli(["mark", "--dir", str(d), "--stage", "not-a-stage", "--status", "done"])
    assert r.returncode == 2, "rc=%d (expected 2)" % r.returncode
    assert "error:" in r.stderr, "stderr %r" % r.stderr


def t_cli_show_malformed_exits_2():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "bad"
        d.mkdir()
        (d / "spike.json").write_text("{nope")
        r = _cli(["show", "--dir", str(d)])
    assert r.returncode == 2, "rc=%d (expected 2)" % r.returncode
    assert "error:" in r.stderr, "stderr %r" % r.stderr


def t_cli_resolve_no_match_exits_2():
    with tempfile.TemporaryDirectory() as root:
        _new_spike(root, name="alpha-run1")
        r = _cli(["resolve", "--root", root, "--ref", "zzz"])
    assert r.returncode == 2, "rc=%d (expected 2)" % r.returncode
    assert "error:" in r.stderr, "stderr %r" % r.stderr


def t_cli_list_empty_root_is_empty_array():
    with tempfile.TemporaryDirectory() as root:
        r = _cli(["list", "--root", root])
    assert r.returncode == 0, "rc=%d" % r.returncode
    assert json.loads(r.stdout) == [], "stdout %r" % r.stdout


# ---------------------------------------------------------------- expected artifacts / symlinks

def _survey_spike(root):
    d = _new_spike(root)
    for s in EXPECTED_STAGES[:2]:
        _write_artifacts(d, s)
        sm.mark_stage(d, s, "done")
    for q in ("q1", "q2"):
        _write(d / "survey" / (q + ".md"), "notes\n<!-- survey-end -->\n")
    sm.mark_stage(d, "codebase-survey", "done")
    return d


def t_lost_expected_artifact_reopens_stage():
    with tempfile.TemporaryDirectory() as root:
        d = _survey_spike(root)
        sm.expect_artifacts(d, "codebase-survey", ["survey/q1.md", "survey/q2.md"])
        assert sm.resume_point(d) == "refine-questions", "intact survey resume %r" % sm.resume_point(d)
        (d / "survey" / "q2.md").unlink()
        rp = sm.resume_point(d)
        missing = sm.missing_artifacts(d, "codebase-survey", ["survey/q1.md", "survey/q2.md"])
    assert rp == "codebase-survey", "lost q2 resume_point %r, expected codebase-survey" % (rp,)
    assert missing == ["survey/q2.md"], "missing %r" % (missing,)


def t_expect_rejects_unsafe_paths():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        for bad in ("../x.md", "/etc/passwd", ""):
            try:
                sm.expect_artifacts(d, "codebase-survey", [bad])
            except sm.ManifestError:
                continue
            raise AssertionError("expect accepted unsafe path %r" % bad)
        try:
            sm.expect_artifacts(d, "nope", ["a.md"])
        except sm.ManifestError:
            return
    raise AssertionError("expect accepted unknown stage")


def t_symlinked_artifact_does_not_satisfy():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        outside = Path(root) / "outside.md"
        outside.write_text("content\n")
        (d / "synthesis.md").symlink_to(outside)
        missing = sm.missing_artifacts(d, "synthesize")
    assert missing == ["synthesis.md"], "symlink satisfied artifact: %r" % (missing,)


def t_cli_expect_roundtrip():
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        r = _cli(["expect", "--dir", str(d), "--stage", "codebase-survey",
                  "--path", "survey/q1.md", "--path", "survey/q2.md"])
        shown = json.loads((d / "spike.json").read_text())
    assert r.returncode == 0, "expect exit %d: %s" % (r.returncode, r.stderr)
    assert shown["expected_artifacts"] == {"codebase-survey": ["survey/q1.md", "survey/q2.md"]}, \
        "stored %r" % (shown.get("expected_artifacts"),)


# ---------------------------------------------------------------- F1: research vs wave-2 separation

def t_research_and_wave2_complete_independently():
    """Both research stages can be complete at once: their globs never overlap."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        _write(d / "research" / "q1.md", "body\n<!-- research-end -->\n")
        assert not sm.missing_artifacts(d, "research")
        assert sm.missing_artifacts(d, "research-wave-2"), "wave-2 satisfied by research files"
        _write(d / "research" / "wave-2" / "q2.md", "body\n<!-- research-wave-2-end -->\n")
        assert not sm.missing_artifacts(d, "research-wave-2")
        assert not sm.missing_artifacts(d, "research"), "wave-2 file broke the research stage"


def t_no_two_stages_share_sentinel_pattern():
    """Sentinel-bearing globs must be unique: a shared glob would let one stage's files
    fail (or satisfy) another stage's sentinel check."""
    seen = {}
    for stage, arts in sm.STAGE_ARTIFACTS.items():
        for pattern, sentinel in arts:
            if sentinel is None:
                continue
            assert pattern not in seen, "%s and %s share %s" % (stage, seen[pattern], pattern)
            seen[pattern] = stage


# ---------------------------------------------------------------- F3: roster-driven expert artifact tests

def t_missing_artifacts_expert_pattern_empty_roster():
    """Expert-specific artifacts require at least one expert in roster."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, experts=[])  # no experts
        # expert-questions stage needs experts/*-questions.md
        missing = sm.missing_artifacts(d, "expert-questions")
        assert missing, "expert stage incomplete with empty roster: %r" % missing


def t_missing_artifacts_expert_pattern_partial_roster():
    """Expert-specific artifacts require files for each expert in roster."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, experts=["alice", "bob"])
        # Write only alice's questions
        _write(d / "experts" / "alice-questions.md", "q\n<!-- spike-questions-end -->\n")
        missing = sm.missing_artifacts(d, "expert-questions")
        # Should report bob's missing
        assert any("bob" in m for m in missing), "bob-questions not reported missing: %r" % missing


def t_missing_artifacts_expert_pattern_complete():
    """Expert-specific artifacts complete when all roster experts have files."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, experts=["alice", "bob"])
        for name in ("alice", "bob"):
            _write(d / "experts" / f"{name}-questions.md", "q\n<!-- spike-questions-end -->\n")
        missing = sm.missing_artifacts(d, "expert-questions")
        assert not missing, "expert stage incomplete with all experts: %r" % missing


def t_missing_artifacts_stray_expert_files_ok():
    """Extra expert files beyond roster don't cause issues."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, experts=["alice"])
        # Write alice's questions AND a stray charlie's questions
        for name in ("alice", "charlie"):
            _write(d / "experts" / f"{name}-questions.md", "q\n<!-- spike-questions-end -->\n")
        missing = sm.missing_artifacts(d, "expert-questions")
        assert not missing, "stray expert file caused issues: %r" % missing


# ---------------------------------------------------------------- F6: init validation table-driven

def t_init_rejects_invalid_inputs():
    """Table-driven validation for init arguments."""
    cases = [
        ("empty question", {"question": ""}, sm.ManifestError),
        ("whitespace question", {"question": "   "}, sm.ManifestError),
        ("question not string", {"question": 42}, sm.ManifestError),
        ("invalid slug chars", {"slug": "A B"}, sm.ManifestError),
        ("slug too long", {"slug": "x"*51}, sm.ManifestError),
        ("empty slug", {"slug": ""}, sm.ManifestError),
        ("effort 0", {"effort": 0}, sm.ManifestError),
        ("effort 6", {"effort": 6}, sm.ManifestError),
        ("effort string", {"effort": "3"}, sm.ManifestError),
        ("invalid models", {"models": "haiku"}, sm.ManifestError),
        ("expert name with space", {"experts": ["alice smith"]}, sm.ManifestError),
        ("empty expert name", {"experts": [""]}, sm.ManifestError),
    ]

    failures = []
    for label, invalid_kw, exc_type in cases:
        with tempfile.TemporaryDirectory() as root:
            d = Path(root) / "test"
            d.mkdir()
            try:
                kw = {"question": "Q?", "slug": "slug", "effort": 2}
                kw.update(invalid_kw)
                sm.init_manifest(d, **kw)
            except exc_type:
                continue
            except Exception as e:
                failures.append("%s: raised %s instead of %s" % (label, type(e).__name__, exc_type.__name__))
                continue
            failures.append("%s: did not raise %s" % (label, exc_type.__name__))

    assert not failures, "; ".join(failures)


# ---------------------------------------------------------------- F14: atomic writes with exclusive flag

def t_write_atomic_exclusive_true_on_existing_fails():
    """_write_atomic with exclusive=True fails if file exists."""
    with tempfile.TemporaryDirectory() as root:
        d = Path(root)
        sm.init_manifest(d, "Q", "s", 2)  # creates spike.json
        try:
            sm.init_manifest(d, "Q2", "s2", 3)  # should fail on re-init
        except sm.ManifestError:
            return
        raise AssertionError("re-init with exclusive=True did not raise ManifestError")


def t_write_atomic_temp_cleanup_on_error():
    """_write_atomic cleans up temp file if replace fails."""
    real_replace = os.replace
    call_count = [0]

    def boom(src, dst, *a, **kw):
        call_count[0] += 1
        raise OSError("simulated error")

    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        temp_files_before = list(d.glob("*.tmp"))
        os.replace = boom
        try:
            try:
                sm.mark_stage(d, "decompose", "running")
            except Exception:
                pass
        finally:
            os.replace = real_replace

        temp_files_after = list(d.glob("*.tmp"))
        assert len(temp_files_before) == len(temp_files_after), \
            "temp file not cleaned up: before=%d after=%d" % (len(temp_files_before), len(temp_files_after))


# ---------------------------------------------------------------- F20: resume_point_from and show_spike

def t_resume_point_from_with_data():
    """resume_point_from derives point from manifest data and disk artifacts."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root)
        data = sm.load_manifest(d)
        # Fresh manifest should point to gather-context
        rp = sm.resume_point_from(data, d)
        assert rp == "gather-context", "fresh resume_point_from %r" % (rp,)

        # Mark gather-context done with artifacts
        _write_artifacts(d, "gather-context")
        sm.mark_stage(d, "gather-context", "done")
        data = sm.load_manifest(d)
        rp = sm.resume_point_from(data, d)
        assert rp == "decompose", "resume_point_from after first stage %r" % (rp,)


def t_show_spike_outputs_manifest():
    """show_spike outputs manifest as JSON."""
    with tempfile.TemporaryDirectory() as root:
        d = _new_spike(root, question="Test?", slug="test")
        shown = sm.show_spike(d)
        assert isinstance(shown, dict), "show_spike returned %s" % type(shown).__name__
        assert shown["question"] == "Test?", "shown question %r" % shown.get("question")
        assert shown["slug"] == "test", "shown slug %r" % shown.get("slug")


TESTS = [
    ("STAGES exact order", t_stages_exact_order),
    ("STATUSES exact set", t_statuses_exact_set),
    ("STAGE_ARTIFACTS covers every stage", t_stage_artifacts_cover_every_stage),
    ("init writes expected shape", t_init_shape),
    ("init defaults models/experts/command_ids", t_init_defaults_models_experts_commands),
    ("init command_id seeds command_ids", t_init_command_id_seeds_command_ids),
    ("init refuses to re-init existing spike", t_init_refuses_reinit),
    ("load fails closed on missing file", t_load_missing_file_fails_closed),
    ("load fails closed on malformed manifests", t_load_malformed_manifests_fail_closed),
    ("load returns dict for valid manifest", t_load_valid_manifest_returns_dict),
    ("mark transitions through every status", t_mark_transitions_all_statuses),
    ("mark touches only the named stage", t_mark_only_touches_named_stage),
    ("mark rejects unknown stage name", t_mark_invalid_stage_rejected),
    ("mark rejects unknown status", t_mark_invalid_status_rejected),
    ("mark on malformed manifest fails closed", t_mark_on_malformed_manifest_fails_closed),
    ("add_command_id appends", t_add_command_id_appends),
    ("add_command_id appends after init seed", t_add_command_id_appends_after_seed),
    ("add_command_id skips duplicate of last", t_add_command_id_skips_duplicate_of_last),
    ("add_command_id keeps non-adjacent duplicate", t_add_command_id_keeps_non_adjacent_duplicate),
    ("add_command_id skips empty id", t_add_command_id_skips_empty),
    ("missing_artifacts non-empty on fresh dir for every stage", t_missing_artifacts_fresh_dir_every_stage),
    ("missing_artifacts empty once written for every stage", t_missing_artifacts_empty_once_written_every_stage),
    ("sentinel-less artifact counts as missing", t_sentinel_absent_counts_as_missing),
    ("sentinel must be the last non-blank line", t_sentinel_must_be_last_nonblank_line),
    ("trailing blank lines after sentinel are fine", t_trailing_blank_lines_after_sentinel_ok),
    ("glob needs at least one match", t_glob_needs_at_least_one_match),
    ("every globbed file needs its sentinel", t_glob_every_matched_file_needs_sentinel),
    ("resume fresh spike is gather-context", t_resume_fresh_is_gather_context),
    ("resume advances past done stage with artifacts", t_resume_done_with_artifacts_advances),
    ("done without artifacts is not complete", t_resume_done_without_artifacts_is_not_complete),
    ("sentinel-less output re-runs stage", t_resume_sentinel_missing_reruns_stage),
    ("sentinel-bearing output completes stage", t_resume_sentinel_present_completes_stage),
    ("running stage is re-run", t_resume_running_is_rerun_even_with_artifacts),
    ("failed stage is re-run", t_resume_failed_is_rerun_even_with_artifacts),
    ("skipped stage counts as complete", t_resume_skipped_counts_as_complete),
    ("earliest incomplete stage wins", t_resume_earliest_incomplete_wins),
    ("all stages complete -> None", t_resume_all_complete_is_none),
    ("complete-or-skipped everywhere -> None", t_resume_all_complete_with_skips_is_none),
    ("list_spikes empty root -> []", t_list_empty_root),
    ("list_spikes reports statuses; malformed does not abort", t_list_statuses_and_malformed_does_not_abort),
    ("list_spikes entry has required fields", t_list_entry_fields),
    ("list_spikes sorted by updated desc", t_list_sorted_updated_desc),
    ("resolve exact name", t_resolve_exact_name),
    ("resolve unique prefix", t_resolve_unique_prefix),
    ("resolve exact name beats longer prefix", t_resolve_exact_beats_prefix),
    ("resolve accepts existing absolute path", t_resolve_absolute_path_ok),
    ("resolve returns absolute path", t_resolve_returns_absolute_path),
    ("resolve ambiguous prefix raises", t_resolve_ambiguous_prefix_raises),
    ("resolve no match raises", t_resolve_no_match_raises),
    ("atomic writes keep JSON valid across many marks", t_atomic_writes_leave_valid_json_each_step),
    ("failed rename leaves previous manifest intact", t_failed_replace_keeps_previous_manifest),
    ("CLI init/show/mark/add-command-id/list/resolve flow", t_cli_init_show_mark_add_list_resolve_flow),
    ("CLI re-init exits 2 with error", t_cli_init_reinit_exits_2),
    ("CLI invalid mark stage exits 2", t_cli_mark_invalid_stage_exits_2),
    ("CLI show on malformed manifest exits 2", t_cli_show_malformed_exits_2),
    ("CLI resolve no match exits 2", t_cli_resolve_no_match_exits_2),
    ("CLI list on empty root prints []", t_cli_list_empty_root_is_empty_array),
    ("lost expected artifact reopens stage", t_lost_expected_artifact_reopens_stage),
    ("expect rejects unsafe paths and stages", t_expect_rejects_unsafe_paths),
    ("symlinked artifact does not satisfy stage", t_symlinked_artifact_does_not_satisfy),
    ("CLI expect stores paths", t_cli_expect_roundtrip),
    ("research and wave-2 complete independently", t_research_and_wave2_complete_independently),
    ("no two stages share artifact/sentinel pair", t_no_two_stages_share_sentinel_pattern),
    ("expert pattern with empty roster", t_missing_artifacts_expert_pattern_empty_roster),
    ("expert pattern with partial roster", t_missing_artifacts_expert_pattern_partial_roster),
    ("expert pattern with complete roster", t_missing_artifacts_expert_pattern_complete),
    ("stray expert files don't cause issues", t_missing_artifacts_stray_expert_files_ok),
    ("init rejects invalid inputs", t_init_rejects_invalid_inputs),
    ("write_atomic exclusive=True on existing fails", t_write_atomic_exclusive_true_on_existing_fails),
    ("write_atomic cleans up temp file on error", t_write_atomic_temp_cleanup_on_error),
    ("resume_point_from with data", t_resume_point_from_with_data),
    ("show_spike outputs manifest", t_show_spike_outputs_manifest),
]


def main():
    h = Harness("spike-manifest")
    for name, fn in TESTS:
        try:
            fn()
            h.test_result(name, True)
        except AssertionError as e:
            h.test_result(name, False, str(e) or "assertion failed")
        except Exception as e:  # a crash is a failure of this test, not of the suite
            h.test_result(name, False, "raised %s: %s" % (type(e).__name__, e))
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
