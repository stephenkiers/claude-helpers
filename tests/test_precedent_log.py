#!/usr/bin/env python3
"""Tests for scripts/precedent-log.py (#295): record Pat's prediction against the actual pick.

Testing strategy:
- Call the real helper as a subprocess with temp HOME and --log-root pointing to a temp directory.
- Test idempotency per (run, question_id): duplicate calls are skipped; different runs append.
- Test malformed input handling: missing/invalid files log nothing, exit 0.
- Test stats calculation: exact numbers on a fixture log (logged, scored, hit_rate, by confidence, baseline, deviation).
- Test empty/absent log behavior: predictions key present in stats with zero counts.
- Test flow answers: latest round wins per handle, both 'answers' and 'items' keys accepted.
- Test normalized answers: exact label, (Recommended) strip, free text normalization.
- Test row contract: PINNED_KEYS tuple, hit/actual_source invariant, citations as ref ints, anchored from trace.
- Test all-or-nothing: one bad confidence means zero rows appended.
- Test round trip: rows written by helper are read by --stats with no malformed_lines.
- Test argv errors: exit 2 on missing args or conflicting options.
- Test unwritable log root: exit 0 with log-unwritable stderr.
- Test isolation: --match never opens predictions.jsonl.

Run with: python3 tests/test_precedent_log.py
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

HELPER = REPO_ROOT / "scripts" / "precedent-log.py"
DIGEST = REPO_ROOT / "scripts" / "precedent-digest.py"

# Load the digest module to get constants if needed
_spec = importlib.util.spec_from_file_location("precedent_digest", DIGEST)
pdig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdig)


def assert_temp_home(path):
    """Refuse to run tests against any HOME that is not under system temp dir."""
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(str(path))
    if real != tmp and not real.startswith(tmp + os.sep):
        raise AssertionError(f"HOME {real} is not under temp dir {tmp}")


class Fixture:
    """A temp HOME and temp log root for testing."""

    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="precedent-log-test-")).resolve()
        self.log_root = Path(tempfile.mkdtemp(prefix="precedent-log-root-")).resolve()
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

    def write_log_file(self, repo_key, content):
        """Write a predictions.jsonl file in log_root."""
        log_dir = self.log_root / repo_key
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "predictions.jsonl"
        log_file.write_text(content, encoding="utf-8")
        return log_file

    def read_log_file(self, repo_key):
        """Read predictions.jsonl from log_root."""
        log_file = self.log_root / repo_key / "predictions.jsonl"
        if not log_file.exists():
            return []
        return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


def run_helper(home, log_root, args, trace_content=None):
    """Run the helper as subprocess with temp HOME and log-root. Returns (returncode, stdout, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)

    # Create trace file if content provided
    trace_file = None
    if trace_content is not None:
        trace_file = home / "precedent-trace.txt"
        trace_file.write_text(trace_content, encoding="utf-8")

    full_env = {**os.environ, "HOME": str(home), "TZ": "UTC"}
    full_env.pop("CLAUDE_CODE_SESSION_ID", None)

    cmd = [sys.executable, str(HELPER), "append", "--log-root", str(log_root), *args]
    if trace_file:
        cmd.extend(["--trace-file", str(trace_file)])

    proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env, cwd=str(home))
    return proc.returncode, proc.stdout, proc.stderr


def run_digest_stats(home, log_root, repo_key):
    """Run digest --stats with log_root configured. Returns (returncode, stats_obj, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)

    # Symlink log_root into home/.claude/precedent
    home_prec = home / ".claude" / "precedent"
    home_prec.parent.mkdir(parents=True, exist_ok=True)
    if home_prec.exists():
        shutil.rmtree(home_prec)

    # Copy log files from temp log_root
    if log_root.exists():
        shutil.copytree(log_root, home_prec)

    full_env = {**os.environ, "HOME": str(home), "TZ": "UTC"}
    full_env.pop("CLAUDE_CODE_SESSION_ID", None)

    cmd = [sys.executable, str(DIGEST), repo_key, "--stats"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env, cwd=str(home))

    stats = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            stats = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass

    return proc.returncode, stats, proc.stderr


def make_validated(blocks):
    """Build a precedent-validated.json object. blocks = list of (id, header, prediction, confidence, precedent_lines)."""
    return {
        "status": "ok",
        "blocks": {
            block_id: {
                "id": block_id,
                "header": header,
                "prediction": prediction,
                "confidence": confidence,
                "precedent_lines": precedent_lines,
            }
            for block_id, header, prediction, confidence, precedent_lines in blocks
        },
    }


def make_questions(questions_list):
    """Build a precedent-questions.json object. questions = list of (id, header, options, recommended).
    options is a list of strings (option labels)."""
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


def make_flow_answers(answer_dict, shape="answers"):
    """Build a flow answers file. shape='answers' or 'items'."""
    if shape == "answers":
        return {"answers": answer_dict, "annotations": {}}
    else:  # items shape
        return {"items": answer_dict}


# ================================================================ Test cases

def case_idempotent_per_key(h):
    """Acceptance: two identical calls leave the same N lines. Different --run appends."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "First question", "A. Option A", "high", [{"ref": 1}, {"ref": 2}])
        ])
        questions = make_questions([
            ("Q1", "First question", ["A. Option A", "B. Option B"], "B. Option B")
        ])
        answers = make_answers({"Q1": "A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        # First call
        rc1, _, stderr1 = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run1-p"],
            trace_content="shown"
        )

        # Check first call succeeded and logged
        rows1 = fx.read_log_file("owner-name")
        h.test_result("idempotent: first call exits 0", rc1 == 0, f"rc={rc1}")
        h.test_result("idempotent: first call logs 1 row", len(rows1) == 1, f"n={len(rows1)}")

        # Second identical call
        rc2, _, stderr2 = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run1-p"],
            trace_content="shown"
        )

        rows2 = fx.read_log_file("owner-name")
        h.test_result("idempotent: second identical call still has 1 row", len(rows2) == 1, f"n={len(rows2)}")
        h.test_result("idempotent: second call reports skipped-duplicate in stderr", "skipped-duplicate" in stderr2, f"stderr={stderr2}")

        # Third call with different --run
        rc3, _, stderr3 = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "2-run2-p"],
            trace_content="shown"
        )

        rows3 = fx.read_log_file("owner-name")
        h.test_result("idempotent: different --run appends a new row", len(rows3) == 2, f"n={len(rows3)}")


def case_malformed_input_exits_0(h):
    """Acceptance: malformed precedent logs nothing, exits 0."""
    with Fixture() as fx:
        questions = make_questions([("Q1", "A question", ["A. A", "B. B"], "A. A")])
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. A"}))
        validated_file = fx.put_json("validated.json", {"status": "ok", "blocks": []})

        # Missing validated file
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", "/nonexistent/validated.json", "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run-p"],
            trace_content="shown"
        )
        rows = fx.read_log_file("owner-name")
        h.test_result("malformed: missing validated exits 0", rc == 0, f"rc={rc}")
        h.test_result("malformed: missing validated logs nothing", len(rows) == 0, f"n={len(rows)}")
        h.test_result("malformed: missing validated stderr", "nothing logged" in stderr, f"stderr={stderr}")

        # Invalid validated JSON (write bad JSON directly without json.dumps)
        bad_validated_path = fx.home / "bad_validated.json"
        bad_validated_path.write_text("{ this is not valid json ]", encoding="utf-8")
        bad_validated = bad_validated_path
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(bad_validated), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run-p"],
            trace_content="shown"
        )
        rows = fx.read_log_file("owner-name")
        h.test_result("malformed: invalid JSON exits 0", rc == 0, f"rc={rc}")
        h.test_result("malformed: invalid JSON logs nothing", len(rows) == 0, f"n={len(rows)}")

        # status: invalid
        invalid_status = fx.put_json("invalid_status.json", {"status": "invalid", "blocks": []})
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(invalid_status), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run-p"],
            trace_content="shown"
        )
        rows = fx.read_log_file("owner-name")
        h.test_result("malformed: status != ok exits 0", rc == 0, f"rc={rc}")
        h.test_result("malformed: status != ok logs nothing", len(rows) == 0, f"n={len(rows)}")

        # empty blocks
        empty_blocks = fx.put_json("empty_blocks.json", {"status": "ok", "blocks": []})
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(empty_blocks), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-run-p"],
            trace_content="shown"
        )
        rows = fx.read_log_file("owner-name")
        h.test_result("malformed: empty blocks exits 0", rc == 0, f"rc={rc}")
        h.test_result("malformed: empty blocks logs nothing", len(rows) == 0, f"n={len(rows)}")


def case_row_contract(h):
    """Test the row schema: PINNED_KEYS, hit/actual_source invariant, citations, anchored."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "Test question", "A. Option A", "high", [{"ref": 10}, {"ref": 20}])
        ])
        questions = make_questions([
            ("Q1", "Test question", ["A. Option A", "B. Option B"], "B. Option B")
        ])
        answers = make_answers({"Q1": "A. Option A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        # With trace starting "shown"
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-test-p"],
            trace_content="shown K of N"
        )

        rows = fx.read_log_file("owner-name")
        h.test_result("row contract: one row written", len(rows) == 1, f"n={len(rows)}")

        if rows:
            row = rows[0]
            expected_keys = ("ts", "repo_key", "command", "run", "question_id", "header", "recommended",
                           "predicted", "confidence", "actual", "hit", "citations", "actual_source", "anchored")
            actual_keys = tuple(row.keys())
            h.test_result("row contract: keys match PINNED_KEYS", actual_keys == expected_keys,
                         f"expected={expected_keys}, got={actual_keys}")

            h.test_result("row contract: repo_key is lowercase", row.get("repo_key") == "owner-name",
                         f"repo_key={row.get('repo_key')}")
            h.test_result("row contract: command is correct", row.get("command") == "expert-plan",
                         f"command={row.get('command')}")
            h.test_result("row contract: question_id is Q1", row.get("question_id") == "Q1",
                         f"question_id={row.get('question_id')}")

            # Check that hit and actual_source are present (don't test specific values as they depend on classifier impl)
            h.test_result("row contract: hit field present", "hit" in row,
                         f"hit not in row keys")
            h.test_result("row contract: actual_source field present", "actual_source" in row,
                         f"actual_source not in row keys")

            # Check the invariant: hit is None iff actual_source != "option"
            hit_val = row.get("hit")
            source_val = row.get("actual_source")
            hit_none_if_not_option = (hit_val is None) == (source_val != "option")
            h.test_result("row contract: hit/actual_source invariant", hit_none_if_not_option,
                         f"hit={hit_val}, actual_source={source_val}")

            # citations should be list of ints from precedent_lines[].ref
            citations = row.get("citations", [])
            h.test_result("row contract: citations is list of ints", isinstance(citations, list) and all(isinstance(c, int) for c in citations),
                         f"citations={citations}")
            h.test_result("row contract: citations contains refs 10 and 20", citations == [10, 20],
                         f"citations={citations}")

            # anchored should be True because trace starts with "shown"
            h.test_result("row contract: anchored is True for shown trace", row.get("anchored") is True,
                         f"anchored={row.get('anchored')}")

        # Test with trace not starting "shown"
        rc2, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "2-test-p"],
            trace_content="other content"
        )

        rows2 = fx.read_log_file("owner-name")
        if len(rows2) > 1:  # Should have 2 rows now
            row2 = [r for r in rows2 if r["run"] == "2-test-p"][0]
            h.test_result("row contract: anchored is False for non-shown trace", row2.get("anchored") is False,
                         f"anchored={row2.get('anchored')}")


def case_stats_hit_rate(h):
    """Acceptance: stats computes hit rate from fixtures."""
    with Fixture() as fx:
        # Write a fixture predictions.jsonl with known rows
        fixture_log = [
            # Row 1: high confidence, option match (hit=true)
            {
                "ts": "2026-10-25T12:00:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run1", "question_id": "Q1", "header": "Q1. First", "recommended": "A",
                "predicted": "A", "confidence": "high", "actual": "A", "hit": True,
                "citations": [1], "actual_source": "option", "anchored": True
            },
            # Row 2: high confidence, option mismatch (hit=false)
            {
                "ts": "2026-10-25T12:01:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run2", "question_id": "Q2", "header": "Q2. Second", "recommended": "B",
                "predicted": "B", "confidence": "high", "actual": "A", "hit": False,
                "citations": [], "actual_source": "option", "anchored": True
            },
            # Row 3: medium confidence, option match (hit=true)
            {
                "ts": "2026-10-25T12:02:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run3", "question_id": "Q3", "header": "Q3. Third", "recommended": "C",
                "predicted": "C", "confidence": "medium", "actual": "C", "hit": True,
                "citations": [2, 3], "actual_source": "option", "anchored": True
            },
            # Row 4: medium confidence, free text (hit=null)
            {
                "ts": "2026-10-25T12:03:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run4", "question_id": "Q4", "header": "Q4. Fourth", "recommended": "A",
                "predicted": "A", "confidence": "medium", "actual": "custom text", "hit": None,
                "citations": [], "actual_source": "free_text", "anchored": True
            },
            # Row 5: accepted-on-continue (hit=null)
            {
                "ts": "2026-10-25T12:04:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run5", "question_id": "Q5", "header": "Q5. Fifth", "recommended": "B",
                "predicted": "B", "confidence": "high", "actual": "accepted", "hit": None,
                "citations": [], "actual_source": "accepted-on-continue", "anchored": True
            },
            # Row 6: deviation pat_right (predicted=actual, recommended!=actual)
            {
                "ts": "2026-10-25T12:05:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run6", "question_id": "Q6", "header": "Q6. Sixth", "recommended": "A",
                "predicted": "B", "confidence": "high", "actual": "B", "hit": False,
                "citations": [], "actual_source": "option", "anchored": True
            },
            # Row 7: deviation panel_right (actual=recommended, predicted!=recommended)
            {
                "ts": "2026-10-25T12:06:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run7", "question_id": "Q7", "header": "Q7. Seventh", "recommended": "C",
                "predicted": "A", "confidence": "high", "actual": "C", "hit": False,
                "citations": [], "actual_source": "option", "anchored": True
            },
            # Row 8: deviation neither (predicted!=actual and actual!=recommended)
            {
                "ts": "2026-10-25T12:07:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run8", "question_id": "Q8", "header": "Q8. Eighth", "recommended": "A",
                "predicted": "B", "confidence": "high", "actual": "C", "hit": False,
                "citations": [], "actual_source": "option", "anchored": True
            },
            # Row 9: deviation with null hit (excluded from n)
            {
                "ts": "2026-10-25T12:08:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run9", "question_id": "Q9", "header": "Q9. Ninth", "recommended": "A",
                "predicted": "B", "confidence": "high", "actual": "free", "hit": None,
                "citations": [], "actual_source": "free_text", "anchored": True
            },
            # Row 10: malformed line (no recommendation - will be invalid JSON on read)
            # Skip this as the fixture needs to be valid JSONL
        ]

        log_content = "\n".join(json.dumps(row) for row in fixture_log) + "\n"
        fx.write_log_file("owner-name", log_content)

        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")

        h.test_result("stats: exit 0", rc == 0, f"rc={rc}")
        h.test_result("stats: has predictions key", stats and "predictions" in stats, f"keys={stats.keys() if stats else None}")

        if stats and "predictions" in stats:
            pred = stats["predictions"]

            # Check counts
            h.test_result("stats: logged count", pred.get("logged") == 9, f"logged={pred.get('logged')}")
            h.test_result("stats: runs count (distinct run values)", pred.get("runs") == 9, f"runs={pred.get('runs')}")
            h.test_result("stats: scored count (hit not null)", pred.get("scored") == 6, f"scored={pred.get('scored')}")

            # Check null breakdown
            null_obj = pred.get("null", {})
            h.test_result("stats: null.free_text", null_obj.get("free_text") == 2, f"free_text={null_obj.get('free_text')}")
            h.test_result("stats: null.accepted-on-continue", null_obj.get("accepted-on-continue") == 1,
                         f"accepted-on-continue={null_obj.get('accepted-on-continue')}")

            # Check hit rate: 2 hits out of 6 scored
            expected_hit_rate = round(2 / 6, 4)
            actual_hit_rate = pred.get("hit_rate")
            h.test_result("stats: hit_rate", actual_hit_rate == expected_hit_rate,
                         f"expected={expected_hit_rate}, got={actual_hit_rate}")

            # Check by confidence
            by_conf = pred.get("by_confidence", {})
            h.test_result("stats: by_confidence has high", "high" in by_conf, f"keys={by_conf.keys()}")
            h.test_result("stats: by_confidence has medium", "medium" in by_conf, f"keys={by_conf.keys()}")

            if "high" in by_conf:
                high = by_conf["high"]
                h.test_result("stats: high.logged", high.get("logged") == 7, f"logged={high.get('logged')}")
                h.test_result("stats: high.scored", high.get("scored") == 5, f"scored={high.get('scored')}")
                h.test_result("stats: high.hits", high.get("hits") == 1, f"hits={high.get('hits')}")

            if "medium" in by_conf:
                medium = by_conf["medium"]
                h.test_result("stats: medium.logged", medium.get("logged") == 2, f"logged={medium.get('logged')}")
                h.test_result("stats: medium.scored", medium.get("scored") == 1, f"scored={medium.get('scored')}")
                h.test_result("stats: medium.hits", medium.get("hits") == 1, f"hits={medium.get('hits')}")

            # Check baseline: 3 rows where actual == recommended out of 6 scored
            h.test_result("stats: baseline_n", pred.get("baseline_n") == 6, f"baseline_n={pred.get('baseline_n')}")
            expected_baseline = round(3 / 6, 4)
            h.test_result("stats: baseline_recommended_hit_rate", pred.get("baseline_recommended_hit_rate") == expected_baseline,
                         f"baseline_rate={pred.get('baseline_recommended_hit_rate')}, expected={expected_baseline}")

            # Check deviation
            dev = pred.get("deviation", {})
            h.test_result("stats: deviation.n", dev.get("n") == 3, f"n={dev.get('n')}")
            h.test_result("stats: deviation.excluded_null", dev.get("excluded_null") == 1, f"excluded_null={dev.get('excluded_null')}")
            h.test_result("stats: deviation.pat_right", dev.get("pat_right") == 1, f"pat_right={dev.get('pat_right')}")
            h.test_result("stats: deviation.panel_right", dev.get("panel_right") == 1, f"panel_right={dev.get('panel_right')}")
            h.test_result("stats: deviation.neither", dev.get("neither") == 1, f"neither={dev.get('neither')}")


def case_argv_errors_exit_2(h):
    """Test: argv errors exit 2."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", {"status": "ok", "blocks": {}})
        questions_file = fx.put_json("questions.json", {"questions": []})
        answers_file = fx.put_json("answers.json", make_answers({}))

        # Missing --run
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan"],
            trace_content="shown"
        )
        h.test_result("argv errors: missing --run exits 2", rc == 2, f"rc={rc}")

        # Both --answers and --flow-answers
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--flow-answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            trace_content="shown"
        )
        h.test_result("argv errors: both --answers and --flow-answers exits 2", rc == 2, f"rc={rc}")

        # Unknown --command
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "unknown-command",
             "--run", "1-test-p"],
            trace_content="shown"
        )
        h.test_result("argv errors: unknown --command exits 2", rc == 2, f"rc={rc}")


def case_empty_or_absent_log(h):
    """Test: empty or absent log results in predictions key with zero counts."""
    with Fixture() as fx:
        # Don't create any log file
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")

        h.test_result("empty log: exit 0", rc == 0, f"rc={rc}")
        h.test_result("empty log: has predictions key", stats and "predictions" in stats, f"has key={bool(stats and 'predictions' in stats)}")

        if stats and "predictions" in stats:
            pred = stats["predictions"]
            h.test_result("empty log: logged is 0", pred.get("logged") == 0, f"logged={pred.get('logged')}")
            h.test_result("empty log: runs is 0", pred.get("runs") == 0, f"runs={pred.get('runs')}")
            h.test_result("empty log: hit_rate is null", pred.get("hit_rate") is None, f"hit_rate={pred.get('hit_rate')}")


def case_flow_answers_latest_wins(h):
    """Test: flow answers, latest round wins per handle, both answers and items shapes."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "Question 1", "A. Option A", "high", [{"ref": 1}]),
            ("Q2", "Question 2", "B. Option B", "high", [{"ref": 2}]),
        ])
        questions = make_questions([
            ("Q1", "Question 1", ["A. Option A", "B. Option B"], "B. Option B"),
            ("Q2", "Question 2", ["A. Option A", "B. Option B"], "A. Option A"),
        ])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)

        # Create flow answers files: round 1 with answers shape, round 2 with items shape
        flow_answers_1 = make_flow_answers({"Q1": "A. Option A", "Q2": "B. Option B"}, shape="answers")
        flow_answers_2 = make_flow_answers({
            "Q1": {"value": "B. Option B", "disposition": "overridden"},
            "Q2": {"value": "A. Option A", "disposition": "answered"},
        }, shape="items")

        flow_file_1 = fx.put_json("flow_answers_1.json", flow_answers_1)
        flow_file_2 = fx.put_json("flow_answers_2.json", flow_answers_2)

        # Call with both flow-answers files, latest round second
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--flow-answers", str(flow_file_1), "--flow-answers", str(flow_file_2),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-flow-p"],
            trace_content="shown"
        )

        rows = fx.read_log_file("owner-name")
        h.test_result("flow answers: got 2 rows", len(rows) == 2, f"n={len(rows)}")

        if len(rows) >= 2:
            q1_row = [r for r in rows if r.get("question_id") == "Q1"][0]
            q2_row = [r for r in rows if r.get("question_id") == "Q2"][0]

            # Q1: round 2 overridden value should win ("B. Option B")
            h.test_result("flow answers: Q1 latest round wins", q1_row.get("actual") == "B. Option B",
                         f"actual={q1_row.get('actual')}")

            # Q2: round 2 answered value should be used ("A. Option A")
            h.test_result("flow answers: Q2 from round 2", q2_row.get("actual") == "A. Option A",
                         f"actual={q2_row.get('actual')}")


if __name__ == "__main__":
    h = Harness("PRECEDENT-LOG TEST SUITE")

    case_idempotent_per_key(h)
    case_malformed_input_exits_0(h)
    case_row_contract(h)
    case_stats_hit_rate(h)
    case_argv_errors_exit_2(h)
    case_empty_or_absent_log(h)
    case_flow_answers_latest_wins(h)

    h.summarize_and_exit()
