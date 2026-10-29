#!/usr/bin/env python3
"""Spec-blind tests for Precedent Pat holdout behavior (#296).

Tests validate the plan specifications:
1. Deterministic hold-out: roughly 1 in 8 question ids are held out (stable hash, not Python hash)
2. `holdout <question_id>...` subcommand prints a JSON list of held-out ids
3. Every logged JSONL prediction row carries a boolean `displayed` field:
   - displayed = false for held-out questions
   - displayed = true for non-held-out questions
4. classify() strips "(recommended)" marker case-insensitively
5. Free text is reported as source "free_text"
6. Step 12 withholds Precedent line for held-out questions

Testing strategy:
- Call the real helper as a subprocess with a temp HOME and --log-root
- Test the `holdout` subcommand returns JSON list of question IDs
- Test displayed field matches holdout status (false for held-out, true otherwise)
- Test holdout is deterministic across runs (stable hash)
- Test displayed/holdout ratio is approximately 1/8 held-out, 7/8 displayed
- Test classify() behavior with recommended markers
- Test free_text source handling

Run with: python3 tests/test_precedent_holdout_spec_blind.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

HELPER = REPO_ROOT / "scripts" / "precedent-log.py"


def assert_temp_home(path):
    """Refuse to run tests against any HOME that is not under system temp dir."""
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(str(path))
    if real != tmp and not real.startswith(tmp + os.sep):
        raise AssertionError(f"HOME {real} is not under temp dir {tmp}")


class Fixture:
    """A temp HOME and temp log root for testing."""

    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="holdout-test-")).resolve()
        self.log_root = Path(tempfile.mkdtemp(prefix="holdout-log-")).resolve()
        assert_temp_home(self.home)
        assert_temp_home(self.log_root)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.log_root, ignore_errors=True)

    def put_json(self, path_str, obj):
        """Write a JSON file in the home directory."""
        path = self.home / path_str
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        return path

    def read_log_file(self, repo_key):
        """Read predictions.jsonl from log_root."""
        log_file = self.log_root / repo_key / "predictions.jsonl"
        if not log_file.exists():
            return []
        return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


def _env(home):
    """Child environment: temp HOME, fixed TZ, no ambient session id."""
    full_env = {**os.environ, "HOME": str(home), "TZ": "UTC"}
    full_env.pop("CLAUDE_CODE_SESSION_ID", None)
    return full_env


def run_helper(home, log_root, args, trace_content="shown 1 of 1"):
    """Run the helper as subprocess with temp HOME and log-root. Returns (returncode, stdout, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)

    trace_file = home / "precedent-trace.txt"
    trace_file.write_text(trace_content, encoding="utf-8")

    cmd = [sys.executable, str(HELPER), "append", "--log-root", str(log_root), *args,
           "--trace-file", str(trace_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))
    return proc.returncode, proc.stdout, proc.stderr


def run_holdout_subcommand(question_ids):
    """Run the holdout subcommand with a list of question IDs. Returns (returncode, list_of_held_out_ids, stderr)."""
    cmd = [sys.executable, str(HELPER), "holdout"] + list(question_ids)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    held_out = []
    try:
        if proc.stdout.strip():
            held_out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass
    return proc.returncode, held_out, proc.stderr


def make_validated(blocks):
    """Build a precedent-validated.json object shaped like `precedent-render.py validate` output."""
    spoken = {}
    for qid, prediction, confidence, refs in blocks:
        lines = [
            {
                "date": "2026-10-01",
                "ref": ref,
                "source": f"plan-{ref}.md",
                "answer": "A. Option A",
                "recommended": None,
                "reason": None,
            }
            for ref in refs
        ]
        spoken[qid] = {
            "precedent_lines": lines,
            "prediction": prediction,
            "confidence": confidence,
            "why": "Earlier plan faced the same choice.",
            "distinguish": "This ticket differs in scope.",
        }
    return {
        "status": "ok",
        "reason": "",
        "blocks": spoken,
        "shown": len(spoken),
        "asked": len(spoken),
    }


def make_questions(questions_list):
    """Build a precedent-questions.json object. questions = list of (id, header, options, recommended)."""
    return {
        "questions": [
            {
                "id": qid,
                "header": header,
                "options": options,
                "recommended": recommended,
            }
            for qid, header, options, recommended in questions_list
        ]
    }


def make_answers(answer_dict):
    """Build a normalized answers.json object."""
    return {"answers": answer_dict}


# ================================================================ Test cases


def case_holdout_subcommand_returns_json(h):
    """Test: holdout subcommand returns valid JSON list of question IDs."""
    question_ids = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    rc, held_out, stderr = run_holdout_subcommand(question_ids)

    h.test_result("holdout subcommand: exits 0", rc == 0, f"rc={rc}, stderr={stderr}")
    h.test_result("holdout subcommand: returns list", isinstance(held_out, list),
                  f"type={type(held_out)}, output={held_out!r}")
    h.test_result("holdout subcommand: all items are strings",
                  all(isinstance(qid, str) for qid in held_out),
                  f"items={held_out}")
    h.test_result("holdout subcommand: all held-out ids are in input",
                  all(qid in question_ids for qid in held_out),
                  f"held_out={held_out}, input={question_ids}")


def case_holdout_subcommand_deterministic_stable_hash(h):
    """Test: holdout determination is deterministic and stable (not Python hash())."""
    question_ids = [f"Q{i:04d}" for i in range(100)]

    # Run holdout subcommand three times
    results = []
    for _ in range(3):
        rc, held_out, _ = run_holdout_subcommand(question_ids)
        if rc == 0:
            results.append(held_out)

    h.test_result("holdout stability: got 3 successful runs", len(results) == 3,
                  f"runs={len(results)}")
    if len(results) == 3:
        h.test_result("holdout stability: run 1 == run 2", results[0] == results[1],
                      f"run1={sorted(results[0])}, run2={sorted(results[1])}")
        h.test_result("holdout stability: run 2 == run 3", results[1] == results[2],
                      f"run2={sorted(results[1])}, run3={sorted(results[2])}")

        # Verify it's not empty and not everything
        h.test_result("holdout stability: some held-out (0 < count < 100)",
                      0 < len(results[0]) < 100,
                      f"count={len(results[0])}")


def case_holdout_approximately_one_eighth(h):
    """Test: roughly 1/8 of questions are held out (100±50 of 800)."""
    question_ids = [f"Q{i:04d}" for i in range(800)]
    rc, held_out, stderr = run_holdout_subcommand(question_ids)

    h.test_result("holdout ratio: command exits 0", rc == 0, f"rc={rc}, stderr={stderr}")
    count = len(held_out) if isinstance(held_out, list) else 0
    h.test_result("holdout ratio: roughly 1/8 held out (100±50 of 800)",
                  50 <= count <= 150, f"count={count}, expected ~100")


def case_displayed_field_presence(h):
    """Test: every logged row carries a displayed field."""
    with Fixture() as fx:
        # Create 1 simple question matching the existing test pattern
        validated = make_validated([("D1", "A", "high", [1])])
        questions = make_questions([("D1", "Display test", ["A", "B"], "A")])
        answers = make_answers({"D1": "A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(fx.home, fx.log_root,
                             ["--validated", str(validated_file),
                              "--questions", str(questions_file),
                              "--answers", str(answers_file),
                              "--repo-key", "test-repo",
                              "--command", "expert-plan",
                              "--run", "test-displayed-field"],
                             "shown 1 of 1")

        rows = fx.read_log_file("test-repo")
        h.test_result("displayed field: rows logged", rc == 0 and len(rows) == 1,
                      f"rc={rc}, rows={len(rows)}")

        for i, row in enumerate(rows):
            h.test_result(f"displayed field: row {i+1} has 'displayed' key",
                          "displayed" in row,
                          f"keys={list(row.keys())}")
            h.test_result(f"displayed field: row {i+1} is boolean",
                          isinstance(row.get("displayed"), bool),
                          f"value={row.get('displayed')!r}, type={type(row.get('displayed'))}")


def case_displayed_field_reflects_holdout_status(h):
    """Test: displayed field is false for held-out questions, true for others."""
    # Use a large set to reliably have some held-out and some non-held-out
    question_ids_set = [f"holdtest-Q{i:04d}" for i in range(100)]
    rc_holdout, held_out, _ = run_holdout_subcommand(question_ids_set)

    if rc_holdout != 0 or not held_out:
        h.test_result("displayed-holdout: can determine holdout status",
                      False, "holdout subcommand failed or returned empty list")
        return

    held_out_set = set(held_out)

    with Fixture() as fx:
        # Create questions matching the holdout test set
        validated_list = [
            (qid, "A. Option A", "high", [1])
            for qid in question_ids_set
        ]
        questions_list = [
            (qid, f"{qid} header", ["A. Option A", "B. Option B"], "A. Option A")
            for qid in question_ids_set
        ]
        answers_dict = {qid: "A" for qid in question_ids_set}

        validated = make_validated(validated_list)
        questions = make_questions(questions_list)
        answers = make_answers(answers_dict)

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(fx.home, fx.log_root,
                             ["--validated", str(validated_file),
                              "--questions", str(questions_file),
                              "--answers", str(answers_file),
                              "--repo-key", "test-holdout-status",
                              "--command", "expert-plan",
                              "--run", "test-holdout-status-run"],
                             "shown")

        rows = fx.read_log_file("test-holdout-status")
        h.test_result("displayed-holdout: rows logged",
                      rc == 0 and len(rows) == len(question_ids_set),
                      f"rc={rc}, rows={len(rows)}, expected={len(question_ids_set)}")

        # Check that displayed matches holdout status
        mismatch_count = 0
        for row in rows:
            qid = row.get("question_id")
            displayed = row.get("displayed")
            is_held_out = qid in held_out_set

            # displayed should be False if held out, True if not
            expected_displayed = not is_held_out
            if displayed != expected_displayed:
                mismatch_count += 1

        h.test_result("displayed-holdout: displayed field reflects holdout status",
                      mismatch_count == 0,
                      f"mismatches={mismatch_count}/{len(rows)}")


def case_displayed_field_ratio_approximately_seven_eighths(h):
    """Test: approximately 7/8 of rows have displayed=true, 1/8 have displayed=false."""
    with Fixture() as fx:
        question_ids_set = [f"ratio-test-Q{i:04d}" for i in range(800)]

        validated_list = [(qid, "A", "high", [1]) for qid in question_ids_set]
        questions_list = [(qid, f"{qid}", ["A", "B"], "A") for qid in question_ids_set]
        answers_dict = {qid: "A" for qid in question_ids_set}

        validated = make_validated(validated_list)
        questions = make_questions(questions_list)
        answers = make_answers(answers_dict)

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(fx.home, fx.log_root,
                             ["--validated", str(validated_file),
                              "--questions", str(questions_file),
                              "--answers", str(answers_file),
                              "--repo-key", "test-ratio",
                              "--command", "expert-plan",
                              "--run", "test-ratio-run"],
                             "shown")

        rows = fx.read_log_file("test-ratio")
        h.test_result("displayed ratio: all rows logged",
                      rc == 0 and len(rows) == len(question_ids_set),
                      f"rc={rc}, rows={len(rows)}, expected={len(question_ids_set)}")

        displayed_true = sum(1 for row in rows if row.get("displayed") is True)
        displayed_false = sum(1 for row in rows if row.get("displayed") is False)

        # Expect roughly 700 displayed=true (7/8) and 100 displayed=false (1/8)
        # With 800 total, allow ±50 margin
        h.test_result("displayed ratio: ~7/8 are true (700±50 of 800)",
                      650 <= displayed_true <= 750,
                      f"true={displayed_true}, false={displayed_false}")
        h.test_result("displayed ratio: ~1/8 are false (100±50 of 800)",
                      50 <= displayed_false <= 150,
                      f"false={displayed_false}, true={displayed_true}")


def case_classify_strips_recommended_via_logged_rows(h):
    """Test: answers with (recommended) suffix are classified correctly and logged with actual_source."""
    with Fixture() as fx:
        # Test that answers with (recommended) suffix are classified correctly
        validated = make_validated([
            ("rec-q1", "A", "high", [1]),
        ])
        questions = make_questions([
            ("rec-q1", "Recommended test", ["A", "B"], "A"),
        ])
        # Provide answer with (recommended) suffix - the classify function should strip this
        answers = make_answers({"rec-q1": "A (recommended)"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(fx.home, fx.log_root,
                             ["--validated", str(validated_file),
                              "--questions", str(questions_file),
                              "--answers", str(answers_file),
                              "--repo-key", "test-rec",
                              "--command", "expert-plan",
                              "--run", "test-rec-run"],
                             "shown")

        rows = fx.read_log_file("test-rec")
        h.test_result("classify-recommended: one row logged",
                      rc == 0 and len(rows) == 1,
                      f"rc={rc}, rows={len(rows)}")

        if rows:
            row = rows[0]
            # The answer "A (recommended)" should be classified as matching "A" with source "option"
            h.test_result("classify-recommended: actual matches predicted or has marker",
                          row.get("actual") in ["A", "A (recommended)"],
                          f"actual={row.get('actual')!r}")
            h.test_result("classify-recommended: actual_source is 'option'",
                          row.get("actual_source") == "option",
                          f"actual_source={row.get('actual_source')!r}")


def case_free_text_source_label(h):
    """Test: free text answers are reported with source 'free_text'."""
    with Fixture() as fx:
        validated = make_validated([
            ("free-q1", "some free text answer", "high", [1]),
            ("free-q2", "another free text", "medium", [1]),
        ])
        questions = make_questions([
            ("free-q1", "Free text Q1", ["Option A", "Option B"], None),
            ("free-q2", "Free text Q2", ["Option A", "Option B"], None),
        ])
        answers = make_answers({
            "free-q1": "some free text answer",
            "free-q2": "another free text",
        })

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(fx.home, fx.log_root,
                             ["--validated", str(validated_file),
                              "--questions", str(questions_file),
                              "--answers", str(answers_file),
                              "--repo-key", "test-free-text",
                              "--command", "expert-plan",
                              "--run", "test-free-text-run"],
                             "shown")

        rows = fx.read_log_file("test-free-text")
        h.test_result("free_text source: rows logged", rc == 0 and len(rows) == 2,
                      f"rc={rc}, rows={len(rows)}")

        for i, row in enumerate(rows):
            actual_source = row.get("actual_source")
            h.test_result(f"free_text source: row {i+1} has actual_source='free_text'",
                          actual_source == "free_text",
                          f"actual_source={actual_source!r}")


if __name__ == "__main__":
    h = Harness("PRECEDENT HOLDOUT SPEC-BLIND TEST SUITE")

    case_holdout_subcommand_returns_json(h)
    case_holdout_subcommand_deterministic_stable_hash(h)
    case_holdout_approximately_one_eighth(h)
    case_displayed_field_presence(h)
    case_displayed_field_reflects_holdout_status(h)
    case_displayed_field_ratio_approximately_seven_eighths(h)
    case_classify_strips_recommended_via_logged_rows(h)
    case_free_text_source_label(h)

    h.summarize_and_exit()
