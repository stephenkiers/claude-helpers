#!/usr/bin/env python3
"""Tests for the #295 review-hardening pass on scripts/precedent-log.py and the --stats predictions
section of scripts/precedent-digest.py.

Covers only behaviour not already pinned by tests/test_precedent_log.py:
- --stats surfaces read_error when predictions.jsonl cannot be read, and reports null (not 0) when
  the log is unreadable.
- Undefined rates are null, never 0.0000 (JSON and markdown), including the baseline subsets.
- Pat's hit rate is also computed on the baseline subset (rows with a recommended option).
- --stats carries an anchoring caveat (markdown line and JSON key).
- --repo-key is allowlisted; hostile keys log nothing and write nothing outside the log root.
- Row-build failures name the question id and the field on stderr.
- Non-string answers are treated as unanswered; unanswered count is on stderr.
- Wrong-shaped validated input is refused without a traceback.
- Unreadable --flow-answers round files are skipped with a named reason.
- Dedup key includes --command (same run, different command appends).
- Row timestamps are timezone-aware UTC and shared by the rows of one run.
- Torn trailing line in the log does not glue the next row onto it.
- An unhashable field in an existing log row does not block append or crash --stats.

Every case runs the real scripts as subprocesses (helper and digest) against temp HOME and log root.

Run with: python3 tests/test_precedent_log_review_hardening.py
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness  # noqa: E402
from test_precedent_log import (  # noqa: E402
    DIGEST,
    Fixture,
    _env,
    _predictions,
    assert_temp_home,
    install_log_in_home,
    make_answers,
    make_flow_answers,
    make_questions,
    make_validated,
    run_digest_stats,
    run_helper,
)


def run_digest_md(home, log_root, repo_key):
    """Run digest --stats --format md with log_root configured. Returns (returncode, stdout, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)
    install_log_in_home(home, log_root)
    cmd = [sys.executable, str(DIGEST), repo_key, "--stats", "--format", "md"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))
    return proc.returncode, proc.stdout, proc.stderr


def _find_keys(obj, *parts):
    """Keys (anywhere in a dict) whose name contains every part, as (key, value) pairs."""
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if all(part in key for part in parts):
                found.append((key, value))
            found.extend(_find_keys(value, *parts))
    return found


def _row(qid, *, run, predicted, recommended, actual, hit, confidence="high",
         actual_source="option", command="expert-plan"):
    """One predictions.jsonl row in the writer's shape (fixture data only)."""
    return {
        "ts": "2026-10-25T12:00:00+00:00", "repo_key": "owner-name", "command": command,
        "run": run, "question_id": qid, "header": qid, "recommended": recommended,
        "predicted": predicted, "confidence": confidence, "actual": actual, "hit": hit,
        "citations": [], "actual_source": actual_source, "anchored": True,
    }


def _write_rows(fx, rows):
    fx.write_log_file("owner-name", "\n".join(json.dumps(r) for r in rows) + "\n")


def _two_question_inputs(fx, validated=None):
    """Put validated/questions files for two spoken questions; returns (validated_file, questions_file)."""
    if validated is None:
        validated = make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "medium", [2]),
        ])
    questions = make_questions([
        ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
        ("Q2", "Second", ["A. Option A", "B. Option B"], "B. Option B"),
    ])
    return fx.put_json("validated.json", validated), fx.put_json("questions.json", questions)


def _helper_args(validated_file, questions_file, answers_file, run, command="expert-plan",
                 repo_key="owner-name"):
    return ["--validated", str(validated_file), "--questions", str(questions_file),
            "--answers", str(answers_file), "--repo-key", repo_key, "--command", command,
            "--run", run]


# ================================================================ Test cases

def case_helper_predictions_file_unwritable(h):
    """A predictions.jsonl that cannot be read or appended (a directory) logs nothing: exit 0, log-unwritable."""
    with Fixture() as fx:
        (fx.log_root / "owner-name" / "predictions.jsonl").mkdir(parents=True)
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(validated_file, questions_file, answers_file, "1-dir-p"),
                                   "shown")
        h.test_result("unreadable predictions file: helper exits 0", rc == 0, f"rc={rc}")
        h.test_result("unreadable predictions file: reason is log-unwritable",
                      "nothing logged (log-unwritable" in stderr, f"stderr={stderr}")
        h.test_result("unreadable predictions file: no traceback", "Traceback" not in stderr, f"stderr={stderr}")


def case_stats_surfaces_read_error(h):
    """--stats reports read_error (not an empty log) when predictions.jsonl is unreadable; null when readable."""
    with Fixture() as fx:
        (fx.log_root / "owner-name" / "predictions.jsonl").mkdir(parents=True)
        rc, stats, stderr = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("stats read_error: exit 0", rc == 0, f"rc={rc}, stderr={stderr[-200:]}")
        h.test_result("stats read_error: predictions.read_error is a non-empty string",
                      isinstance(pred.get("read_error"), str) and pred.get("read_error") != "",
                      f"read_error={pred.get('read_error')!r}")
        h.test_result("stats read_error: unreadable log reports logged 0 with rate null",
                      pred.get("logged") == 0 and pred.get("hit_rate") is None,
                      f"logged={pred.get('logged')}, hit_rate={pred.get('hit_rate')}")

    with Fixture() as fx:
        _write_rows(fx, [_row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True)])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("stats read_error control: readable log exits 0", rc == 0, f"rc={rc}")
        h.test_result("stats read_error control: readable log has read_error null",
                      "read_error" in pred and pred.get("read_error") is None,
                      f"read_error={pred.get('read_error')!r}")


def case_stats_undefined_rates_are_null(h):
    """Rates with a zero denominator are null in JSON and never printed as 0.0000 in markdown."""
    with Fixture() as fx:
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        baseline_rates = [v for k, v in _find_keys(pred, "baseline") if "rate" in k]
        pat_baseline = [v for k, v in _find_keys(pred, "pat", "baseline") if "rate" in k]
        h.test_result("undefined rates empty log: exit 0", rc == 0, f"rc={rc}")
        h.test_result("undefined rates empty log: a Pat-on-baseline rate key exists", len(pat_baseline) >= 1,
                      f"predictions keys={sorted(pred.keys())}")
        h.test_result("undefined rates empty log: every baseline rate is null",
                      len(baseline_rates) >= 1 and all(v is None for v in baseline_rates),
                      f"baseline rates={baseline_rates}")
        h.test_result("undefined rates empty log: Pat-on-baseline rate is null",
                      len(pat_baseline) >= 1 and all(v is None for v in pat_baseline),
                      f"pat baseline={pat_baseline}")

        rc_md, out_md, _ = run_digest_md(fx.home, fx.log_root, "owner-name")
        h.test_result("undefined rates empty log md: exit 0", rc_md == 0, f"rc={rc_md}")
        h.test_result("undefined rates empty log md: no 0.0000 printed", "0.0000" not in out_md,
                      f"out has 0.0000: {'0.0000' in out_md}")

    with Fixture() as fx:
        # Scored by Pat, but no row has a recommended option, so the baseline subset is empty.
        _write_rows(fx, [_row("Q1", run="r1", predicted="B", recommended=None, actual="B", hit=True,
                              confidence="medium")])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        medium = (pred.get("by_confidence") or {}).get("medium", {})
        h.test_result("undefined baseline: empty baseline subset gives baseline_n 0",
                      pred.get("baseline_n") == 0, f"baseline_n={pred.get('baseline_n')}")
        h.test_result("undefined baseline: baseline rate is null, not 0",
                      pred.get("baseline_recommended_hit_rate") is None
                      and medium.get("baseline_hit_rate") is None,
                      f"overall={pred.get('baseline_recommended_hit_rate')}, medium={medium.get('baseline_hit_rate')}")
        rc_md, out_md, _ = run_digest_md(fx.home, fx.log_root, "owner-name")
        h.test_result("undefined baseline md: no 0.0000 printed", rc_md == 0 and "0.0000" not in out_md,
                      f"rc={rc_md}")


def case_stats_pat_rate_on_baseline_subset(h):
    """Pat's hit rate is also reported on the baseline subset (rows with a recommended option)."""
    with Fixture() as fx:
        _write_rows(fx, [
            # A: recommended A, Pat predicted A, actual A -> Pat hit, baseline hit.
            _row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True),
            # B: no recommended option -> outside the baseline subset; Pat hit.
            _row("Q2", run="r2", predicted="B", recommended=None, actual="B", hit=True),
            # C: recommended A, Pat predicted B, actual A -> Pat miss, baseline hit.
            _row("Q3", run="r3", predicted="B", recommended="A", actual="A", hit=False),
        ])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("pat baseline subset: exit 0", rc == 0, f"rc={rc}")
        h.test_result("pat baseline subset: overall Pat hit_rate is 2/3 over all scored rows",
                      pred.get("hit_rate") is not None and abs(pred.get("hit_rate") - round(2 / 3, 4)) < 1e-9,
                      f"hit_rate={pred.get('hit_rate')}")
        h.test_result("pat baseline subset: baseline_n counts only rows with a recommended option",
                      pred.get("baseline_n") == 2, f"baseline_n={pred.get('baseline_n')}")
        h.test_result("pat baseline subset: baseline recommended rate is 2/2",
                      pred.get("baseline_recommended_hit_rate") == 1.0,
                      f"baseline={pred.get('baseline_recommended_hit_rate')}")
        pat_baseline = [v for k, v in _find_keys(pred, "pat", "baseline") if "rate" in k]
        h.test_result("pat baseline subset: Pat rate on the baseline subset is 1/2",
                      len(pat_baseline) == 1 and pat_baseline[0] is not None and abs(pat_baseline[0] - 0.5) < 1e-9,
                      f"pat baseline={pat_baseline}")


def case_stats_anchoring_caveat(h):
    """--stats says every row is anchored, as a markdown line and a JSON key (upper-bound caveat)."""
    with Fixture() as fx:
        _write_rows(fx, [_row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True)])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        caveats = [v for k, v in _find_keys(pred, "anchor")]
        h.test_result("anchoring caveat: JSON predictions has an anchoring key", len(caveats) >= 1,
                      f"keys={sorted(pred.keys())}")
        h.test_result("anchoring caveat: JSON anchoring value says rows are anchored",
                      any(isinstance(v, str) and "anchored" in v.lower() for v in caveats),
                      f"values={caveats}")

        rc_md, out_md, _ = run_digest_md(fx.home, fx.log_root, "owner-name")
        h.test_result("anchoring caveat md: exit 0", rc_md == 0, f"rc={rc_md}")
        h.test_result("anchoring caveat md: markdown line says rows are anchored",
                      bool(re.search(r"anchored", out_md, re.IGNORECASE)), f"out={out_md[-300:]!r}")


def case_repo_key_allowlist(h):
    """Hostile --repo-key values log nothing, name bad-repo-key, and never create a file at the escaped path."""
    hostile = ["../escape", "a/b", "..", "", "Owner_Name", "owner name", "x\\y"]
    for key in hostile:
        with Fixture() as fx:
            validated_file, questions_file = _two_question_inputs(fx)
            answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
            args = _helper_args(validated_file, questions_file, answers_file, "1-key-p", repo_key=key)
            rc, _, stderr = run_helper(fx.home, fx.log_root, args, "shown")
            target = (fx.log_root / key / "predictions.jsonl").resolve()
            wrote = target.exists() or any(fx.log_root.rglob("predictions.jsonl"))
            h.test_result(f"repo-key allowlist {key!r}: exit is 0 or 2 (no crash)", rc in (0, 2), f"rc={rc}")
            h.test_result(f"repo-key allowlist {key!r}: no traceback", "Traceback" not in stderr, f"stderr={stderr}")
            h.test_result(f"repo-key allowlist {key!r}: bad-repo-key reason on stderr",
                          "bad-repo-key" in stderr or rc == 2, f"stderr={stderr}")
            h.test_result(f"repo-key allowlist {key!r}: no predictions.jsonl written", not wrote,
                          f"target={target}")


def case_row_build_failure_names_field(h):
    """A block that cannot be built drops the run and names the question id and the field on stderr."""
    with Fixture() as fx:
        questions_file = fx.put_json("questions.json", make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "B. Option B"),
        ]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        bad_file = fx.put_json("bad.json", make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "bogus", [2]),
        ]))
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(bad_file, questions_file, answers_file, "1-field-p"),
                                   "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("row-build field: exit 0 and zero rows", rc == 0 and rows == [], f"rc={rc}, n={len(rows)}")
        h.test_result("row-build field: stderr names question Q2 and field confidence",
                      "row-build-failed" in stderr and "Q2" in stderr and "confidence" in stderr,
                      f"stderr={stderr}")


def case_unanswered_counted_on_stderr(h):
    """A spoken question with no answer is skipped and counted as unanswered on stderr."""
    with Fixture() as fx:
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(validated_file, questions_file, answers_file, "1-unans-p"),
                                   "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("unanswered: exit 0", rc == 0, f"rc={rc}")
        h.test_result("unanswered: only the answered question is logged",
                      len(rows) == 1 and rows[0].get("question_id") == "Q1", f"rows={rows}")
        h.test_result("unanswered: stderr counts one unanswered question",
                      re.search(r"unanswered\D*1\b", stderr) is not None, f"stderr={stderr}")


def case_non_string_answer_is_unanswered(h):
    """A non-string answer value is treated as unanswered, not a crash; the string answer still logs."""
    with Fixture() as fx:
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", {"answers": {"Q1": 42, "Q2": "B. Option B"}})
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(validated_file, questions_file, answers_file, "1-nonstr-p"),
                                   "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("non-string answer: exit 0", rc == 0, f"rc={rc}")
        h.test_result("non-string answer: no traceback", "Traceback" not in stderr, f"stderr={stderr}")
        h.test_result("non-string answer: Q1 not logged, Q2 logged",
                      [r.get("question_id") for r in rows] == ["Q2"], f"rows={rows}")
        h.test_result("non-string answer: Q1 counted as unanswered",
                      re.search(r"unanswered\D*1\b", stderr) is not None, f"stderr={stderr}")


def case_wrong_shape_validated_refused(h):
    """Validated input of the wrong JSON shape logs nothing, exits 0, and has no traceback."""
    shapes = [
        ("top-level list", [1, 2]),
        ("blocks not an object", {"status": "ok", "reason": "", "blocks": ["Q1"], "shown": 1, "asked": 1}),
    ]
    for label, shape in shapes:
        with Fixture() as fx:
            _, questions_file = _two_question_inputs(fx)
            validated_file = fx.put_json("shape.json", shape)
            answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))
            rc, _, stderr = run_helper(fx.home, fx.log_root,
                                       _helper_args(validated_file, questions_file, answers_file, "1-shape-p"),
                                       "shown")
            rows = fx.read_log_file("owner-name")
            h.test_result(f"wrong shape ({label}): exit 0", rc == 0, f"rc={rc}")
            h.test_result(f"wrong shape ({label}): no traceback", "Traceback" not in stderr, f"stderr={stderr}")
            h.test_result(f"wrong shape ({label}): logs nothing", rows == [], f"rows={rows}")
            h.test_result(f"wrong shape ({label}): reason on stderr",
                          "nothing logged (" in stderr, f"stderr={stderr}")


def case_unreadable_round_file_skipped(h):
    """An unreadable --flow-answers round file is skipped with its path named; the readable round still logs."""
    with Fixture() as fx:
        validated_file, questions_file = _two_question_inputs(fx)
        bad_path = fx.home / "bad-round.json"
        bad_path.write_text("{ not json", encoding="utf-8")
        good_file = fx.put_json("good-round.json", make_flow_answers({
            "Q1": {"value": "A. Option A", "disposition": "answered"},
            "Q2": {"value": "B. Option B", "disposition": "answered"},
        }, shape="items"))
        rc, _, stderr = run_helper(fx.home, fx.log_root, [
            "--validated", str(validated_file), "--questions", str(questions_file),
            "--flow-answers", str(bad_path), "--flow-answers", str(good_file),
            "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-round-p",
        ], "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("unreadable round: exit 0", rc == 0, f"rc={rc}")
        h.test_result("unreadable round: readable round still logs both rows", len(rows) == 2, f"n={len(rows)}")
        h.test_result("unreadable round: stderr names the skipped file",
                      "skipped unreadable round file" in stderr and "bad-round.json" in stderr,
                      f"stderr={stderr}")
        h.test_result("unreadable round: no traceback", "Traceback" not in stderr, f"stderr={stderr}")


def case_dedup_key_includes_command(h):
    """Same run and question under a different --command appends; the same command again is a duplicate."""
    with Fixture() as fx:
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        rc1, _, _ = run_helper(fx.home, fx.log_root,
                               _helper_args(validated_file, questions_file, answers_file, "same-run",
                                            command="expert-plan"),
                               "shown")
        rc2, _, stderr2 = run_helper(fx.home, fx.log_root,
                                     _helper_args(validated_file, questions_file, answers_file, "same-run",
                                                  command="expert-review"),
                                     "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("dedup command: both calls exit 0", rc1 == 0 and rc2 == 0, f"rc1={rc1}, rc2={rc2}")
        h.test_result("dedup command: different command appends both questions (4 rows)", len(rows) == 4,
                      f"n={len(rows)}")
        h.test_result("dedup command: different command logs 2, skips 0",
                      "logged 2, skipped-duplicate 0" in stderr2, f"stderr={stderr2}")
        rc3, _, stderr3 = run_helper(fx.home, fx.log_root,
                                     _helper_args(validated_file, questions_file, answers_file, "same-run",
                                                  command="expert-review"),
                                     "shown")
        h.test_result("dedup command: repeat of the same command is skipped",
                      rc3 == 0 and "logged 0, skipped-duplicate 2" in stderr3 and len(fx.read_log_file("owner-name")) == 4,
                      f"stderr={stderr3}")


def case_timestamp_utc_shared_per_run(h):
    """Row ts is timezone-aware UTC and identical for every row written by one run."""
    with Fixture() as fx:
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        rc, _, _ = run_helper(fx.home, fx.log_root,
                              _helper_args(validated_file, questions_file, answers_file, "ts-run"), "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("timestamp: exit 0 with two rows", rc == 0 and len(rows) == 2, f"rc={rc}, n={len(rows)}")
        stamps = [r.get("ts") for r in rows]
        parsed = []
        for stamp in stamps:
            try:
                parsed.append(datetime.fromisoformat(stamp))
            except (TypeError, ValueError):
                parsed.append(None)
        h.test_result("timestamp: every ts parses as ISO 8601",
                      all(p is not None for p in parsed), f"ts={stamps}")
        h.test_result("timestamp: every ts is timezone-aware UTC",
                      all(p is not None and p.utcoffset() == timedelta(0) for p in parsed), f"ts={stamps}")
        h.test_result("timestamp: rows of one run share one ts", len(set(stamps)) == 1, f"ts={stamps}")


def case_torn_trailing_line_not_glued(h):
    """A log whose last line is torn (no trailing newline) keeps the torn line apart from the new rows."""
    with Fixture() as fx:
        fx.write_log_file("owner-name", '{"ts": "2026-10-25T12:00:00+00:00", "partial":')
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(validated_file, questions_file, answers_file, "torn-run"),
                                   "shown")
        raw = (fx.log_root / "owner-name" / "predictions.jsonl").read_text(encoding="utf-8")
        parsed_rows = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("run") == "torn-run":
                parsed_rows.append(obj)
        h.test_result("torn line: exit 0", rc == 0, f"rc={rc}, stderr={stderr}")
        h.test_result("torn line: both new rows are parseable on their own lines", len(parsed_rows) == 2,
                      f"parseable new rows={len(parsed_rows)}, raw={raw[:200]!r}")

        rc_s, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("torn line: stats counts the torn line as malformed once",
                      rc_s == 0 and pred.get("malformed_lines") == 1, f"malformed={pred.get('malformed_lines')}")
        h.test_result("torn line: stats still reads the two new rows", pred.get("logged") == 2,
                      f"logged={pred.get('logged')}")


def case_unhashable_field_does_not_block_append(h):
    """An existing row whose run is a list does not block the helper; its new rows still append."""
    with Fixture() as fx:
        bad_row = _row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True)
        bad_row["run"] = ["not", "hashable"]
        fx.write_log_file("owner-name", json.dumps(bad_row) + "\n")
        validated_file, questions_file = _two_question_inputs(fx)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))
        rc, _, stderr = run_helper(fx.home, fx.log_root,
                                   _helper_args(validated_file, questions_file, answers_file, "hash-run"),
                                   "shown")
        h.test_result("unhashable append: exit 0", rc == 0, f"rc={rc}")
        h.test_result("unhashable append: no traceback", "Traceback" not in stderr, f"stderr={stderr}")
        h.test_result("unhashable append: both new rows logged",
                      "logged 2, skipped-duplicate 0" in stderr, f"stderr={stderr}")


def case_stats_unhashable_field_counted_malformed(h):
    """--stats does not crash on a row whose run is a list; it counts the row as malformed."""
    with Fixture() as fx:
        bad_row = _row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True)
        bad_row["run"] = ["not", "hashable"]
        good_row = _row("Q2", run="r2", predicted="B", recommended="B", actual="B", hit=True)
        fx.write_log_file("owner-name",
                          json.dumps(bad_row) + "\n" + json.dumps(good_row) + "\n")
        rc, stats, stderr = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("unhashable stats: exit 0", rc == 0, f"rc={rc}, stderr={stderr[-200:]}")
        h.test_result("unhashable stats: no internal error", "internal error" not in stderr and "Traceback" not in stderr,
                      f"stderr={stderr[-300:]}")
        h.test_result("unhashable stats: the bad row is counted as malformed",
                      pred.get("malformed_lines") == 1, f"malformed={pred.get('malformed_lines')}")
        h.test_result("unhashable stats: the valid row is still logged", pred.get("logged") == 1,
                      f"logged={pred.get('logged')}")


if __name__ == "__main__":
    h = Harness("PRECEDENT-LOG REVIEW HARDENING TEST SUITE")

    case_helper_predictions_file_unwritable(h)
    case_stats_surfaces_read_error(h)
    case_stats_undefined_rates_are_null(h)
    case_stats_pat_rate_on_baseline_subset(h)
    case_stats_anchoring_caveat(h)
    case_repo_key_allowlist(h)
    case_row_build_failure_names_field(h)
    case_unanswered_counted_on_stderr(h)
    case_non_string_answer_is_unanswered(h)
    case_wrong_shape_validated_refused(h)
    case_unreadable_round_file_skipped(h)
    case_dedup_key_includes_command(h)
    case_timestamp_utc_shared_per_run(h)
    case_torn_trailing_line_not_glued(h)
    case_unhashable_field_does_not_block_append(h)
    case_stats_unhashable_field_counted_malformed(h)

    h.summarize_and_exit()
