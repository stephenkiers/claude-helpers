#!/usr/bin/env python3
"""Tests for scripts/precedent-log.py (#295): record Pat's prediction against the actual pick.

Testing strategy:
- Call the real helper as a subprocess with a temp HOME and --log-root under a temp directory.
- The validated-file fixtures come from the real producer: the round-trip case runs
  scripts/precedent-render.py validate on a fixture precedent.md, and the shape pin asserts that
  make_validated() mirrors that output key for key.
- Idempotency per (run, question_id, command): repeated calls skip; a new --run appends.
- Missing or malformed inputs log nothing, exit 0, and name the specific reason on stderr.
- All-or-nothing: one block that cannot be built drops the whole run (zero rows).
- Unwritable log root: exit 0 with log-unwritable on stderr.
- Answer normalization: exact label, trailing (Recommended) stripped, free text collapsed.
- Stats: exact numbers on a fixture log, empty or absent log, corrupt lines counted as malformed.
- Round trip: rows the helper writes are read back by --stats with malformed_lines == 0.
- --match isolation: the digest --match path never opens predictions.jsonl (checked with an
  audit hook; a --stats run in the same harness is the positive control).
- Row contract: PINNED_KEYS order, hit/actual_source invariant, citations, anchored.
- Argv errors exit 2.

Run with: python3 tests/test_precedent_log.py
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
DIGEST = REPO_ROOT / "scripts" / "precedent-digest.py"
RENDER = REPO_ROOT / "scripts" / "precedent-render.py"


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


def _env(home):
    """Child environment: temp HOME, fixed TZ, no ambient session id."""
    full_env = {**os.environ, "HOME": str(home), "TZ": "UTC"}
    full_env.pop("CLAUDE_CODE_SESSION_ID", None)
    return full_env


def run_helper(home, log_root, args, trace_content):
    """Run the helper as subprocess with temp HOME and log-root. Returns (returncode, stdout, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)

    trace_file = home / "precedent-trace.txt"
    trace_file.write_text(trace_content, encoding="utf-8")

    cmd = [sys.executable, str(HELPER), "append", "--log-root", str(log_root), *args,
           "--trace-file", str(trace_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))
    return proc.returncode, proc.stdout, proc.stderr


def install_log_in_home(home, log_root):
    """Copy the log root into HOME/.claude/precedent, where the digest reads it."""
    home_prec = home / ".claude" / "precedent"
    home_prec.parent.mkdir(parents=True, exist_ok=True)
    if home_prec.exists():
        shutil.rmtree(home_prec)
    if log_root.exists():
        shutil.copytree(log_root, home_prec)


def run_digest_stats(home, log_root, repo_key):
    """Run digest --stats with log_root configured. Returns (returncode, stats_obj, stderr)."""
    assert_temp_home(home)
    assert_temp_home(log_root)
    install_log_in_home(home, log_root)

    cmd = [sys.executable, str(DIGEST), repo_key, "--stats"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))

    stats = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            stats = json.loads(proc.stdout)
        except json.JSONDecodeError:
            stats = None

    return proc.returncode, stats, proc.stderr


# Audit-hook wrapper for the --match isolation check. It records every open() of a path that
# names predictions.jsonl (open() and os.open() both raise the "open" audit event), then runs the
# digest script in-process with the given argv. Written into the temp HOME at test time.
AUDIT_WRAPPER = '''\
import atexit
import runpy
import sys

OPENED = []


def _record(event, args):
    if event == "open" and args and isinstance(args[0], str) and "predictions.jsonl" in args[0]:
        OPENED.append(args[0])


def _dump():
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write("\\n".join(OPENED))


OUT_PATH = sys.argv[1]
SCRIPT = sys.argv[2]
sys.addaudithook(_record)
atexit.register(_dump)
sys.argv = [SCRIPT] + sys.argv[3:]
runpy.run_path(SCRIPT, run_name="__main__")
'''


def run_digest_audited(home, log_root, repo_key, extra_args):
    """Run the digest under the audit wrapper. Returns (returncode, stdout, stderr, opened_paths).

    opened_paths is None if the wrapper never wrote its record (the run is then unusable).
    """
    install_log_in_home(home, log_root)
    wrapper = home / "audit_digest.py"
    wrapper.write_text(AUDIT_WRAPPER, encoding="utf-8")
    opened_file = home / "opened.txt"
    cmd = [sys.executable, str(wrapper), str(opened_file), str(DIGEST), repo_key, *extra_args]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))
    opened = None
    if opened_file.exists():
        opened = [line for line in opened_file.read_text(encoding="utf-8").splitlines() if line]
    return proc.returncode, proc.stdout, proc.stderr, opened


def run_real_validator(home, precedent_text, questions_obj, digest_refs):
    """Run precedent-render.py validate on fixture inputs. Returns (rc, validated_obj, validated_path).

    The fixture precedent.md, questions.json and digest.jsonl are written into home. validated_obj is
    the parsed --out file, or {} if the validator wrote nothing.
    """
    precedent_path = home / "precedent.md"
    precedent_path.write_text(precedent_text, encoding="utf-8")
    questions_path = home / "validator-questions.json"
    questions_path.write_text(json.dumps(questions_obj, indent=2), encoding="utf-8")
    digest_path = home / "digest.jsonl"
    digest_path.write_text(
        "".join(json.dumps({"ref": ref, "plan": f"plan-{ref}.md"}) + "\n" for ref in digest_refs),
        encoding="utf-8",
    )
    validated_path = home / "validated.json"
    cmd = [sys.executable, str(RENDER), "validate",
           "--precedent", str(precedent_path),
           "--questions", str(questions_path),
           "--digest", str(digest_path),
           "--out", str(validated_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_env(home), cwd=str(home))
    validated = {}
    if validated_path.exists():
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
    return proc.returncode, validated, validated_path


def make_validated(blocks):
    """Build a precedent-validated.json object shaped like `precedent-render.py validate` output.

    blocks = list of (question_id, prediction, confidence, refs). The block key is the question id;
    the header lives in the questions file, not in the block (the real validator emits neither
    id nor header inside a block). refs become precedent_lines with the validator's six keys.
    """
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


def _row_for(rows, qid):
    """The single row for question qid, or {} so the field asserts that read it fail loudly."""
    matches = [r for r in rows if r.get("question_id") == qid]
    return matches[0] if len(matches) == 1 else {}


def _only(rows):
    """The single row, or {} so the field asserts that read it fail loudly."""
    return rows[0] if len(rows) == 1 else {}


def _predictions(stats):
    """The predictions section of a --stats object, or {} so the asserts that read it fail loudly."""
    if isinstance(stats, dict) and isinstance(stats.get("predictions"), dict):
        return stats["predictions"]
    return {}


# Fixture for the real-producer round trip: two spoken blocks, valid per precedent-render.py.
ROUND_TRIP_QUESTIONS = [
    ("auth-scope", "Which auth scope?", ["A. Option A", "B. Option B"], "A. Option A"),
    ("cache-ttl", "Cache TTL?", ["A. 30 seconds", "B. 5 minutes"], "A. 30 seconds"),
]

ROUND_TRIP_PRECEDENT = """# Precedent

precedent-pat | questions: 2 | spoke-on: 2 | citations: 2 | confidence: high=1 medium=1

## Q1 [auth-scope] Which auth scope?
**Precedent**: 2026-10-01 · #1 · plan-a.md · chose "A. Option A" · reason: "small blast radius"
**Prediction**: A. Option A
**Confidence**: high
**Why**: The earlier plan faced the same scope choice.
**Distinguish**: This ticket also touches sessions.

## Q2 [cache-ttl] Cache TTL?
**Precedent**: 2026-10-02 · #2 · plan-b.md · chose "B. 5 minutes" (recommended was "A. 30 seconds")
**Prediction**: B. 5 minutes
**Confidence**: medium
**Why**: Earlier cache work chose the longer TTL.
**Distinguish**: No invalidation hook exists here.
"""

ROUND_TRIP_DIGEST_REFS = [1, 2]


# ================================================================ Test cases

def case_idempotent_per_key(h):
    """Acceptance: two identical calls leave the same N lines. Different --run appends."""
    with Fixture() as fx:
        validated = make_validated([("Q1", "A. Option A", "high", [1, 2])])
        questions = make_questions([("Q1", "First question", ["A. Option A", "B. Option B"], "B. Option B")])
        answers = make_answers({"Q1": "A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)
        common = ["--validated", str(validated_file), "--questions", str(questions_file),
                  "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan"]

        rc1, _, stderr1 = run_helper(fx.home, fx.log_root, [*common, "--run", "1-run1-p"], "shown")
        rows1 = fx.read_log_file("owner-name")
        h.test_result("idempotent: first call exits 0", rc1 == 0, f"rc={rc1}")
        h.test_result("idempotent: first call logs 1 row", len(rows1) == 1, f"n={len(rows1)}")
        h.test_result("idempotent: first call reports logged 1, skipped-duplicate 0",
                      "logged 1, skipped-duplicate 0" in stderr1, f"stderr={stderr1}")

        rc2, _, stderr2 = run_helper(fx.home, fx.log_root, [*common, "--run", "1-run1-p"], "shown")
        rows2 = fx.read_log_file("owner-name")
        h.test_result("idempotent: second identical call exits 0", rc2 == 0, f"rc={rc2}")
        h.test_result("idempotent: second identical call still has 1 row", len(rows2) == 1, f"n={len(rows2)}")
        h.test_result("idempotent: second call reports logged 0, skipped-duplicate 1",
                      "logged 0, skipped-duplicate 1" in stderr2, f"stderr={stderr2}")

        rc3, _, _ = run_helper(fx.home, fx.log_root, [*common, "--run", "2-run2-p"], "shown")
        rows3 = fx.read_log_file("owner-name")
        h.test_result("idempotent: different --run exits 0", rc3 == 0, f"rc={rc3}")
        h.test_result("idempotent: different --run appends a new row", len(rows3) == 2, f"n={len(rows3)}")


def case_malformed_input_logs_nothing(h):
    """Acceptance: malformed precedent logs nothing, exits 0, and names the reason on stderr."""
    with Fixture() as fx:
        questions = make_questions([("Q1", "A question", ["A. A", "B. B"], "A. A")])
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. A"}))
        common = ["--questions", str(questions_file), "--answers", str(answers_file),
                  "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-run-p"]

        cases = []

        cases.append(("missing validated", ["--validated", "/nonexistent/validated.json"]))

        bad_validated_path = fx.home / "bad_validated.json"
        bad_validated_path.write_text("{ this is not valid json ]", encoding="utf-8")
        cases.append(("invalid JSON", ["--validated", str(bad_validated_path)]))

        invalid_status = fx.put_json("invalid_status.json", {"status": "invalid", "blocks": {}})
        cases.append(("status != ok", ["--validated", str(invalid_status)]))

        empty_blocks = fx.put_json("empty_blocks.json", {"status": "ok", "blocks": {}})
        cases.append(("empty blocks", ["--validated", str(empty_blocks)]))

        for label, extra in cases:
            rc, _, stderr = run_helper(fx.home, fx.log_root, [*extra, *common], "shown")
            rows = fx.read_log_file("owner-name")
            h.test_result(f"malformed: {label} exits 0", rc == 0, f"rc={rc}")
            h.test_result(f"malformed: {label} logs nothing", len(rows) == 0, f"n={len(rows)}")
            h.test_result(f"malformed: {label} reason is validated-invalid",
                          "nothing logged (validated-invalid" in stderr, f"stderr={stderr}")


def case_all_or_nothing_block_drop(h):
    """One block that cannot be built drops the whole run; the same run with valid blocks logs both."""
    with Fixture() as fx:
        questions = make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "B. Option B"),
        ])
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))

        bad = make_validated([("Q1", "A. Option A", "high", [1]), ("Q2", "B. Option B", "low", [2])])
        bad_file = fx.put_json("bad_validated.json", bad)
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(bad_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-bad-p"],
            "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("all-or-nothing: bad block exits 0", rc == 0, f"rc={rc}")
        h.test_result("all-or-nothing: bad block drops every row (zero rows)", len(rows) == 0, f"n={len(rows)}")
        h.test_result("all-or-nothing: reason is row-build-failed naming Q2",
                      "row-build-failed" in stderr and "Q2" in stderr, f"stderr={stderr}")

        good = make_validated([("Q1", "A. Option A", "high", [1]), ("Q2", "B. Option B", "medium", [2])])
        good_file = fx.put_json("good_validated.json", good)
        rc_c, _, stderr_c = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(good_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "2-good-p"],
            "shown")
        rows_c = fx.read_log_file("owner-name")
        h.test_result("all-or-nothing control: valid blocks exit 0", rc_c == 0, f"rc={rc_c}")
        h.test_result("all-or-nothing control: valid blocks log both rows",
                      len(rows_c) == 2 and "logged 2, skipped-duplicate 0" in stderr_c,
                      f"n={len(rows_c)}, stderr={stderr_c}")


def case_unwritable_log_root(h):
    """A log root that cannot hold a directory: exit 0 with log-unwritable on stderr."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([("Q1", "A. Option A", "high", [1])]))
        questions_file = fx.put_json("questions.json", make_questions([("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A")]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))

        blocked_root = fx.home / "log-root-is-a-file"
        blocked_root.write_text("not a directory", encoding="utf-8")
        rc, _, stderr = run_helper(
            fx.home, blocked_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-unwritable-p"],
            "shown")
        h.test_result("unwritable root: exits 0", rc == 0, f"rc={rc}")
        h.test_result("unwritable root: reason is log-unwritable",
                      "nothing logged (log-unwritable" in stderr, f"stderr={stderr}")


def case_row_contract(h):
    """Test the row schema: PINNED_KEYS, hit/actual_source invariant, citations, anchored."""
    with Fixture() as fx:
        validated = make_validated([("Q1", "A. Option A", "high", [10, 20])])
        questions = make_questions([("Q1", "Test question", ["A. Option A", "B. Option B"], "B. Option B")])
        answers = make_answers({"Q1": "A. Option A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)
        common = ["--validated", str(validated_file), "--questions", str(questions_file),
                  "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan"]

        rc, _, _ = run_helper(fx.home, fx.log_root, [*common, "--run", "1-test-p"], "shown K of N")

        rows = fx.read_log_file("owner-name")
        h.test_result("row contract: one row written", rc == 0 and len(rows) == 1, f"rc={rc}, n={len(rows)}")

        row = _only(rows)
        expected_keys = ("ts", "repo_key", "command", "run", "question_id", "header", "recommended",
                         "predicted", "confidence", "actual", "hit", "citations", "actual_source", "anchored", "displayed")
        h.test_result("row contract: keys match PINNED_KEYS", tuple(row.keys()) == expected_keys,
                      f"expected={expected_keys}, got={tuple(row.keys())}")
        h.test_result("row contract: repo_key is lowercase", row.get("repo_key") == "owner-name",
                      f"repo_key={row.get('repo_key')}")
        h.test_result("row contract: command is correct", row.get("command") == "expert-plan",
                      f"command={row.get('command')}")
        h.test_result("row contract: question_id is Q1", row.get("question_id") == "Q1",
                      f"question_id={row.get('question_id')}")

        hit_val = row.get("hit")
        source_val = row.get("actual_source")
        h.test_result("row contract: hit/actual_source invariant (hit is None iff source != option)",
                      "hit" in row and "actual_source" in row and (hit_val is None) == (source_val != "option"),
                      f"hit={hit_val}, actual_source={source_val}")

        citations = row.get("citations")
        h.test_result("row contract: citations are the ref ints in order",
                      citations == [10, 20], f"citations={citations}")
        h.test_result("row contract: anchored is True for shown trace", row.get("anchored") is True,
                      f"anchored={row.get('anchored')}")

        # A trace that does not start with "shown" logs nothing, with that specific reason.
        rc2, _, stderr2 = run_helper(fx.home, fx.log_root, [*common, "--run", "2-test-p"], "other content")
        h.test_result("row contract: non-shown trace exits 0", rc2 == 0, f"rc={rc2}")
        h.test_result("row contract: non-shown trace reason is trace-not-shown",
                      "nothing logged (trace-not-shown" in stderr2, f"stderr={stderr2}")
        rows2_all = fx.read_log_file("owner-name")
        h.test_result("row contract: only first row in log", len(rows2_all) == 1, f"n={len(rows2_all)}")


def case_stats_hit_rate(h):
    """Acceptance: stats computes hit rate from fixtures."""
    with Fixture() as fx:
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
            # Row 6: deviation pat_right (predicted=actual, recommended!=actual); the writer
            # sets hit = (predicted == actual) for option answers, so hit is True here.
            {
                "ts": "2026-10-25T12:05:00", "repo_key": "owner-name", "command": "expert-plan",
                "run": "run6", "question_id": "Q6", "header": "Q6. Sixth", "recommended": "A",
                "predicted": "B", "confidence": "high", "actual": "B", "hit": True,
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
        ]

        log_content = "\n".join(json.dumps(row) for row in fixture_log) + "\n"
        fx.write_log_file("owner-name", log_content)

        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)

        h.test_result("stats: exit 0", rc == 0, f"rc={rc}")
        h.test_result("stats: has predictions section", "predictions" in pred or bool(pred),
                      f"keys={list(stats.keys()) if isinstance(stats, dict) else stats}")

        h.test_result("stats: logged count", pred.get("logged") == 9, f"logged={pred.get('logged')}")
        h.test_result("stats: runs count (distinct run values)", pred.get("runs") == 9, f"runs={pred.get('runs')}")
        h.test_result("stats: scored count (hit not null)", pred.get("scored") == 6, f"scored={pred.get('scored')}")

        null_obj = pred.get("null", {})
        h.test_result("stats: null.free_text", null_obj.get("free_text") == 2, f"free_text={null_obj.get('free_text')}")
        h.test_result("stats: null.accepted-on-continue", null_obj.get("accepted-on-continue") == 1,
                      f"accepted-on-continue={null_obj.get('accepted-on-continue')}")

        # 3 hits (rows 1, 3, 6) out of 6 scored.
        expected_hit_rate = round(3 / 6, 4)
        h.test_result("stats: hit_rate", pred.get("hit_rate") == expected_hit_rate,
                      f"expected={expected_hit_rate}, got={pred.get('hit_rate')}")

        by_conf = pred.get("by_confidence", {})
        high = by_conf.get("high", {})
        medium = by_conf.get("medium", {})
        h.test_result("stats: high.logged", high.get("logged") == 7, f"logged={high.get('logged')}")
        h.test_result("stats: high.scored", high.get("scored") == 5, f"scored={high.get('scored')}")
        h.test_result("stats: high.hits (rows 1 and 6)", high.get("hits") == 2, f"hits={high.get('hits')}")
        h.test_result("stats: medium.logged", medium.get("logged") == 2, f"logged={medium.get('logged')}")
        h.test_result("stats: medium.scored", medium.get("scored") == 1, f"scored={medium.get('scored')}")
        h.test_result("stats: medium.hits", medium.get("hits") == 1, f"hits={medium.get('hits')}")

        # Baseline: 3 of the 6 scored rows picked the recommended option.
        h.test_result("stats: baseline_n", pred.get("baseline_n") == 6, f"baseline_n={pred.get('baseline_n')}")
        expected_baseline = round(3 / 6, 4)
        h.test_result("stats: baseline_recommended_hit_rate",
                      pred.get("baseline_recommended_hit_rate") == expected_baseline,
                      f"baseline_rate={pred.get('baseline_recommended_hit_rate')}, expected={expected_baseline}")

        dev = pred.get("deviation", {})
        h.test_result("stats: deviation.n", dev.get("n") == 3, f"n={dev.get('n')}")
        h.test_result("stats: deviation.excluded_null", dev.get("excluded_null") == 1, f"excluded_null={dev.get('excluded_null')}")
        h.test_result("stats: deviation.pat_right", dev.get("pat_right") == 1, f"pat_right={dev.get('pat_right')}")
        h.test_result("stats: deviation.panel_right", dev.get("panel_right") == 1, f"panel_right={dev.get('panel_right')}")
        h.test_result("stats: deviation.neither", dev.get("neither") == 1, f"neither={dev.get('neither')}")


def case_stats_corrupt_lines(h):
    """Corrupt lines (not JSON, and JSON that is not an object) are counted as malformed; valid rows still count."""
    with Fixture() as fx:
        valid = [
            {"ts": "2026-10-25T12:00:00", "repo_key": "owner-name", "command": "expert-plan",
             "run": "run1", "question_id": "Q1", "header": "Q1. First", "recommended": "A",
             "predicted": "A", "confidence": "high", "actual": "A", "hit": True,
             "citations": [1], "actual_source": "option", "anchored": True},
            {"ts": "2026-10-25T12:01:00", "repo_key": "owner-name", "command": "expert-plan",
             "run": "run2", "question_id": "Q2", "header": "Q2. Second", "recommended": "B",
             "predicted": "B", "confidence": "high", "actual": "A", "hit": False,
             "citations": [], "actual_source": "option", "anchored": True},
        ]
        lines = [json.dumps(valid[0]), "{this is not json", json.dumps(valid[1]), "[1, 2]"]
        fx.write_log_file("owner-name", "\n".join(lines) + "\n")

        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("corrupt lines: stats exit 0", rc == 0, f"rc={rc}")
        h.test_result("corrupt lines: two malformed lines counted", pred.get("malformed_lines") == 2,
                      f"malformed_lines={pred.get('malformed_lines')}")
        h.test_result("corrupt lines: valid rows still logged", pred.get("logged") == 2,
                      f"logged={pred.get('logged')}")
        h.test_result("corrupt lines: valid rows still scored", pred.get("scored") == 2,
                      f"scored={pred.get('scored')}")
        h.test_result("corrupt lines: hit_rate from valid rows", pred.get("hit_rate") == 0.5,
                      f"hit_rate={pred.get('hit_rate')}")


def case_empty_or_absent_log(h):
    """Test: empty or absent log results in predictions section with zero counts."""
    with Fixture() as fx:
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)

        h.test_result("empty log: exit 0", rc == 0, f"rc={rc}")
        h.test_result("empty log: logged is 0", pred.get("logged") == 0, f"logged={pred.get('logged')}")
        h.test_result("empty log: runs is 0", pred.get("runs") == 0, f"runs={pred.get('runs')}")
        h.test_result("empty log: hit_rate is null", "hit_rate" in pred and pred.get("hit_rate") is None,
                      f"hit_rate={pred.get('hit_rate')}")


def case_argv_errors_exit_2(h):
    """Test: argv errors exit 2."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([]))
        questions_file = fx.put_json("questions.json", make_questions([]))
        answers_file = fx.put_json("answers.json", make_answers({}))

        # Missing --run
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan"],
            "shown")
        h.test_result("argv errors: missing --run exits 2", rc == 2, f"rc={rc}")

        # Both --answers and --flow-answers
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--flow-answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")
        h.test_result("argv errors: both --answers and --flow-answers exits 2", rc == 2, f"rc={rc}")

        # Unknown --command
        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "unknown-command",
             "--run", "1-test-p"],
            "shown")
        h.test_result("argv errors: unknown --command exits 2", rc == 2, f"rc={rc}")


def case_flow_answers_latest_wins(h):
    """Test: flow answers, latest round wins per handle, both answers and items shapes."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "high", [2]),
        ])
        questions = make_questions([
            ("Q1", "Question 1", ["A. Option A", "B. Option B"], "B. Option B"),
            ("Q2", "Question 2", ["A. Option A", "B. Option B"], "A. Option A"),
        ])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)

        # Round 1 uses the answers shape; round 2 uses the items shape and overrides Q1.
        flow_file_1 = fx.put_json("flow_answers_1.json", make_flow_answers(
            {"Q1": "A. Option A", "Q2": "B. Option B"}, shape="answers"))
        flow_file_2 = fx.put_json("flow_answers_2.json", make_flow_answers({
            "Q1": {"value": "B. Option B", "disposition": "overridden"},
            "Q2": {"value": "A. Option A", "disposition": "answered"},
        }, shape="items"))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--flow-answers", str(flow_file_1), "--flow-answers", str(flow_file_2),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-flow-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("flow answers: exits 0", rc == 0, f"rc={rc}")
        h.test_result("flow answers: got 2 rows", len(rows) == 2, f"n={len(rows)}")

        q1_row = _row_for(rows, "Q1")
        q2_row = _row_for(rows, "Q2")
        h.test_result("flow answers: Q1 latest round wins ('B. Option B')",
                      q1_row.get("actual") == "B. Option B", f"actual={q1_row.get('actual')}")
        h.test_result("flow answers: Q2 from round 2 ('A. Option A')",
                      q2_row.get("actual") == "A. Option A", f"actual={q2_row.get('actual')}")
        h.test_result("flow answers: Q1 actual_source is 'option'",
                      q1_row.get("actual_source") == "option", f"actual_source={q1_row.get('actual_source')}")
        h.test_result("flow answers: Q2 actual_source is 'option'",
                      q2_row.get("actual_source") == "option", f"actual_source={q2_row.get('actual_source')}")


def case_flow_accepted_on_continue(h):
    """Test: flow answers with accepted-on-continue disposition yields actual_source='accepted-on-continue'."""
    with Fixture() as fx:
        validated = make_validated([("Q1", "A. Yes", "high", [1])])
        questions = make_questions([("Q1", "Should we accept?", ["A. Yes", "B. No"], "A. Yes")])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        flow_file = fx.put_json("flow_answers.json", make_flow_answers({
            "Q1": {"value": "User accepted the recommendation", "disposition": "accepted-on-continue"},
        }, shape="items"))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--flow-answers", str(flow_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("accepted-on-continue: exits 0", rc == 0, f"rc={rc}")
        h.test_result("accepted-on-continue: got 1 row", len(rows) == 1, f"n={len(rows)}")

        row = _only(rows)
        h.test_result("accepted-on-continue: actual_source='accepted-on-continue'",
                      row.get("actual_source") == "accepted-on-continue", f"actual_source={row.get('actual_source')}")
        h.test_result("accepted-on-continue: hit is null", "hit" in row and row.get("hit") is None,
                      f"hit={row.get('hit')}")
        h.test_result("accepted-on-continue: actual is the value",
                      row.get("actual") == "User accepted the recommendation", f"actual={row.get('actual')}")


def case_flow_unresolved_accepted_no_row(h):
    """Test: flow answers with unresolved/accepted dispositions give no row, with the answers-invalid reason."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "high", [2]),
        ])
        questions = make_questions([
            ("Q1", "Question 1", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Question 2", ["A. Option A", "B. Option B"], "B. Option B"),
        ])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        flow_file = fx.put_json("flow_answers.json", make_flow_answers({
            "Q1": {"value": "ignored", "disposition": "unresolved"},
            "Q2": {"value": "ignored", "disposition": "accepted"},
        }, shape="items"))

        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--flow-answers", str(flow_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("unresolved/accepted: exits 0", rc == 0, f"rc={rc}")
        h.test_result("unresolved/accepted: no rows logged", len(rows) == 0, f"n={len(rows)}")
        h.test_result("unresolved/accepted: reason is answers-invalid",
                      "nothing logged (answers-invalid" in stderr, f"stderr={stderr}")


def case_letter_to_label_mapping(h):
    """Test: bare letter classification maps correctly to label and hit is computed correctly."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "B. Option B", "high", [1]),
            ("Q2", "A. Option A", "high", [2]),
        ])
        questions = make_questions([
            ("Q1", "Question with B", ["A. Option A", "B. Option B"], "B. Option B"),
            ("Q2", "Question with A", ["A. Option A", "B. Option B"], "A. Option A"),
        ])
        answers = make_answers({"Q1": "B", "Q2": "A"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("letter mapping: exits 0", rc == 0, f"rc={rc}")
        h.test_result("letter mapping: got 2 rows", len(rows) == 2, f"n={len(rows)}")

        q1_row = _row_for(rows, "Q1")
        q2_row = _row_for(rows, "Q2")
        h.test_result("letter mapping: Q1 'B' maps to 'B. Option B'",
                      q1_row.get("actual") == "B. Option B", f"actual={q1_row.get('actual')}")
        h.test_result("letter mapping: Q1 hit is True", q1_row.get("hit") is True, f"hit={q1_row.get('hit')}")
        h.test_result("letter mapping: Q2 'A' maps to 'A. Option A'",
                      q2_row.get("actual") == "A. Option A", f"actual={q2_row.get('actual')}")
        h.test_result("letter mapping: Q2 hit is True", q2_row.get("hit") is True, f"hit={q2_row.get('hit')}")


def case_ambiguous_letter_free_text(h):
    """Test: unknown letters are classified as free_text."""
    with Fixture() as fx:
        validated = make_validated([("Q1", "A. Option A", "high", [1])])
        questions = make_questions([("Q1", "Question", ["A. Option A", "B. Option B"], "A. Option A")])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "C"}))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("ambiguous letter: exits 0", rc == 0, f"rc={rc}")
        h.test_result("ambiguous letter: got 1 row", len(rows) == 1, f"n={len(rows)}")

        row = _only(rows)
        h.test_result("ambiguous letter: actual_source is 'free_text'",
                      row.get("actual_source") == "free_text", f"actual_source={row.get('actual_source')}")
        h.test_result("ambiguous letter: actual is the normalized text 'C'",
                      row.get("actual") == "C", f"actual={row.get('actual')}")


def case_answer_normalization(h):
    """Test: a trailing (Recommended) is stripped before the exact-label match; free text is whitespace-collapsed."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "medium", [2]),
        ])
        questions = make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "B. Option B"),
        ])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({
            "Q1": "A. Option A (Recommended)",
            "Q2": "  multi word\n  answer  ",
        }))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("normalization: exits 0", rc == 0, f"rc={rc}")
        h.test_result("normalization: got 2 rows", len(rows) == 2, f"n={len(rows)}")

        q1_row = _row_for(rows, "Q1")
        q2_row = _row_for(rows, "Q2")
        h.test_result("normalization: '(Recommended)' stripped to exact label",
                      q1_row.get("actual") == "A. Option A" and q1_row.get("actual_source") == "option",
                      f"actual={q1_row.get('actual')}, source={q1_row.get('actual_source')}")
        h.test_result("normalization: exact label predicted correctly (hit True)",
                      q1_row.get("hit") is True, f"hit={q1_row.get('hit')}")
        h.test_result("normalization: free text collapsed to single spaces",
                      q2_row.get("actual") == "multi word answer" and q2_row.get("actual_source") == "free_text",
                      f"actual={q2_row.get('actual')}, source={q2_row.get('actual_source')}")
        h.test_result("normalization: free text has null hit", "hit" in q2_row and q2_row.get("hit") is None,
                      f"hit={q2_row.get('hit')}")


def case_lowercase_recommended_marker(h):
    """Test: lowercase (recommended) marker is stripped case-insensitively (D1 casing fix)."""
    with Fixture() as fx:
        validated = make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "B. Option B", "high", [2]),
        ])
        questions = make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "B. Option B"),
        ])

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", make_answers({
            "Q1": "A. Option A (recommended)",  # lowercase (recommended)
            "Q2": "B. Option B (RECOMMENDED)",  # uppercase (RECOMMENDED)
        }))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-test-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("lowercase recommended: exits 0", rc == 0, f"rc={rc}")
        h.test_result("lowercase recommended: got 2 rows", len(rows) == 2, f"n={len(rows)}")

        q1_row = _row_for(rows, "Q1")
        q2_row = _row_for(rows, "Q2")
        h.test_result("lowercase recommended: '(recommended)' stripped to exact label",
                      q1_row.get("actual") == "A. Option A" and q1_row.get("actual_source") == "option",
                      f"actual={q1_row.get('actual')}, source={q1_row.get('actual_source')}")
        h.test_result("lowercase recommended: lowercase predicted correctly (hit True)",
                      q1_row.get("hit") is True, f"hit={q1_row.get('hit')}")
        h.test_result("lowercase recommended: '(RECOMMENDED)' uppercase also stripped",
                      q2_row.get("actual") == "B. Option B" and q2_row.get("actual_source") == "option",
                      f"actual={q2_row.get('actual')}, source={q2_row.get('actual_source')}")
        h.test_result("lowercase recommended: uppercase predicted correctly (hit True)",
                      q2_row.get("hit") is True, f"hit={q2_row.get('hit')}")


def case_real_ask_label_with_recommended(h):
    """Test: end-to-end append with real Ask label shape 'A - Not a divergence (recommended)' logs correctly (D1 end-to-end)."""
    with Fixture() as fx:
        # Real Ask label format: "A - Option text (recommended)"
        validated = make_validated([
            ("N1", "A - Not a divergence", "high", [100]),
        ])
        questions = make_questions([
            ("N1", "Is this a divergence from past scope?",
             ["A - Not a divergence", "B - Yes, new scope", "C - Partial divergence"],
             "A - Not a divergence"),
        ])
        answers = make_answers({"N1": "A - Not a divergence (recommended)"})

        validated_file = fx.put_json("validated.json", validated)
        questions_file = fx.put_json("questions.json", questions)
        answers_file = fx.put_json("answers.json", answers)

        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file),
             "--repo-key", "owner-name", "--command", "expert-review", "--run", "1-review-p"],
            "shown")

        rows = fx.read_log_file("owner-name")
        h.test_result("ask label with recommended: exits 0", rc == 0, f"rc={rc}")
        h.test_result("ask label with recommended: logs 1 row", len(rows) == 1, f"n={len(rows)}")

        row = _only(rows)
        h.test_result("ask label with recommended: actual_source is 'option' not 'free_text'",
                      row.get("actual_source") == "option",
                      f"actual_source={row.get('actual_source')}")
        h.test_result("ask label with recommended: actual matches the option label (without marker)",
                      row.get("actual") == "A - Not a divergence",
                      f"actual={row.get('actual')}")
        h.test_result("ask label with recommended: prediction matched correctly (hit True)",
                      row.get("hit") is True, f"hit={row.get('hit')}")
        h.test_result("ask label with recommended: stderr reports logged 1",
                      "logged 1, skipped-duplicate 0" in stderr, f"stderr={stderr}")


def case_round_trip_real_validator(h):
    """Real producer end to end: precedent-render.py validate -> precedent-log.py append -> digest --stats."""
    with Fixture() as fx:
        questions_obj = make_questions(ROUND_TRIP_QUESTIONS)
        rc_v, validated, validated_path = run_real_validator(
            fx.home, ROUND_TRIP_PRECEDENT, questions_obj, ROUND_TRIP_DIGEST_REFS)
        h.test_result("round trip: validator exits 0", rc_v == 0, f"rc={rc_v}")
        h.test_result("round trip: validator status is ok", validated.get("status") == "ok",
                      f"validated={validated}")
        h.test_result("round trip: validator blocks keyed by question id",
                      set(validated.get("blocks", {})) == {"auth-scope", "cache-ttl"},
                      f"keys={sorted(validated.get('blocks', {}))}")

        questions_file = fx.put_json("questions.json", questions_obj)
        answers_file = fx.put_json("answers.json", make_answers({
            "auth-scope": "A. Option A",
            "cache-ttl": "custom: 10 minutes",
        }))
        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_path), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "round-trip-1"],
            "shown")
        h.test_result("round trip: helper exits 0", rc == 0, f"rc={rc}")
        h.test_result("round trip: helper logs both rows",
                      "logged 2, skipped-duplicate 0" in stderr, f"stderr={stderr}")

        rows = fx.read_log_file("owner-name")
        auth = _row_for(rows, "auth-scope")
        cache = _row_for(rows, "cache-ttl")
        h.test_result("round trip: auth-scope header comes from questions metadata",
                      auth.get("header") == "Which auth scope?", f"header={auth.get('header')}")
        h.test_result("round trip: auth-scope recommended comes from questions metadata",
                      auth.get("recommended") == "A. Option A", f"recommended={auth.get('recommended')}")
        h.test_result("round trip: auth-scope predicted/actual/hit",
                      auth.get("predicted") == "A. Option A" and auth.get("actual") == "A. Option A"
                      and auth.get("hit") is True and auth.get("actual_source") == "option",
                      f"row={auth}")
        h.test_result("round trip: auth-scope citations come from the validated refs",
                      auth.get("citations") == [1], f"citations={auth.get('citations')}")
        h.test_result("round trip: cache-ttl header and recommended from questions metadata",
                      cache.get("header") == "Cache TTL?" and cache.get("recommended") == "A. 30 seconds",
                      f"header={cache.get('header')}, recommended={cache.get('recommended')}")
        h.test_result("round trip: cache-ttl free text, null hit, confidence medium",
                      cache.get("actual_source") == "free_text" and cache.get("hit") is None
                      and cache.get("confidence") == "medium" and cache.get("predicted") == "B. 5 minutes",
                      f"row={cache}")

        rc_s, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        h.test_result("round trip: stats exit 0", rc_s == 0, f"rc={rc_s}")
        h.test_result("round trip: stats reads both rows with no malformed lines",
                      pred.get("logged") == 2 and pred.get("malformed_lines") == 0,
                      f"logged={pred.get('logged')}, malformed_lines={pred.get('malformed_lines')}")
        h.test_result("round trip: stats scores the one option answer", pred.get("scored") == 1,
                      f"scored={pred.get('scored')}")


def case_make_validated_matches_real_validator(h):
    """Shape pin: make_validated() has the same keys as real validator output, at every level."""
    with Fixture() as fx:
        rc, real, _ = run_real_validator(
            fx.home, ROUND_TRIP_PRECEDENT, make_questions(ROUND_TRIP_QUESTIONS), ROUND_TRIP_DIGEST_REFS)
        expected = make_validated([
            ("auth-scope", "A. Option A", "high", [1]),
            ("cache-ttl", "B. 5 minutes", "medium", [2]),
        ])
        h.test_result("shape: validator exits 0", rc == 0, f"rc={rc}")
        h.test_result("shape: top-level keys match validator output",
                      set(real.keys()) == set(expected.keys()),
                      f"real={sorted(real.keys())}, helper={sorted(expected.keys())}")

        real_block = real.get("blocks", {}).get("auth-scope", {})
        expected_block = expected["blocks"]["auth-scope"]
        h.test_result("shape: block keys match validator output",
                      set(real_block.keys()) == set(expected_block.keys()),
                      f"real={sorted(real_block.keys())}, helper={sorted(expected_block.keys())}")

        real_line = (real_block.get("precedent_lines") or [{}])[0]
        expected_line = expected_block["precedent_lines"][0]
        h.test_result("shape: precedent_line keys match validator output",
                      set(real_line.keys()) == set(expected_line.keys()),
                      f"real={sorted(real_line.keys())}, helper={sorted(expected_line.keys())}")


def case_match_never_opens_predictions(h):
    """--match isolation: the digest --match path never opens predictions.jsonl; --stats does (control)."""
    with Fixture() as fx:
        log_rows = [
            {"ts": "2026-10-25T12:00:00", "repo_key": "owner-name", "command": "expert-plan",
             "run": "run1", "question_id": "Q1", "header": "Which auth scope?", "recommended": "A",
             "predicted": "A", "confidence": "high", "actual": "A", "hit": True,
             "citations": [1], "actual_source": "option", "anchored": True},
        ]
        fx.write_log_file("owner-name", "\n".join(json.dumps(r) for r in log_rows) + "\n")
        questions_file = fx.put_json("match-questions.json", make_questions([
            ("Q1", "Which auth scope?", ["A. Option A", "B. Option B"], "A. Option A"),
        ]))

        rc_m, _, _, opened_m = run_digest_audited(
            fx.home, fx.log_root, "owner-name", ["--match", str(questions_file)])
        h.test_result("match isolation: --match exits 0", rc_m == 0, f"rc={rc_m}")
        h.test_result("match isolation: --match opened no predictions.jsonl",
                      opened_m == [], f"opened={opened_m}")

        rc_s, _, _, opened_s = run_digest_audited(fx.home, fx.log_root, "owner-name", ["--stats"])
        h.test_result("match isolation control: --stats exits 0", rc_s == 0, f"rc={rc_s}")
        h.test_result("match isolation control: --stats does open predictions.jsonl",
                      bool(opened_s) and all("predictions.jsonl" in p for p in opened_s),
                      f"opened={opened_s}")


def run_digest_raw(home, args):
    """Run precedent-digest.py with the given argv and the temp HOME. Returns (rc, stdout, stderr)."""
    assert_temp_home(home)
    proc = subprocess.run([sys.executable, str(DIGEST), *args], capture_output=True, text=True,
                          env=_env(home), cwd=str(home))
    return proc.returncode, proc.stdout, proc.stderr


def _stats_row(run, qid, predicted, actual, hit, recommended="A. Option A", command="expert-plan"):
    """A minimal prediction-log row with every field the digest stats read."""
    return json.dumps({
        "ts": "2026-10-01T00:00:00+00:00", "repo_key": "owner-name", "command": command,
        "run": run, "question_id": qid, "header": "H", "recommended": recommended,
        "predicted": predicted, "confidence": "high", "actual": actual, "hit": hit,
        "citations": [1], "actual_source": "option", "anchored": True,
    })


def case_non_utf8_log_is_log_unwritable(h):
    """Finding 1: a log that is not valid UTF-8 exits 0 with log-unwritable, no traceback, bytes untouched."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([("Q1", "A. Option A", "high", [1])]))
        questions_file = fx.put_json("questions.json", make_questions([("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A")]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))

        log_dir = fx.log_root / "owner-name"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "predictions.jsonl"
        original = b"\xff\xfe not utf-8\n"
        log_file.write_bytes(original)

        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-nonutf8-p"],
            "shown")
        h.test_result("non-UTF-8 log: exits 0", rc == 0, f"rc={rc}")
        h.test_result("non-UTF-8 log: reason is log-unwritable (not valid UTF-8)",
                      "nothing logged (log-unwritable" in stderr and "not valid UTF-8" in stderr, f"stderr={stderr}")
        h.test_result("non-UTF-8 log: no Traceback on stderr",
                      "Traceback" not in stderr, f"stderr={stderr}")
        h.test_result("non-UTF-8 log: log bytes unchanged",
                      log_file.read_bytes() == original, "log was modified")


def case_flow_empty_value_keeps_earlier_answer(h):
    """Finding 2: a later round with no usable value keeps the earlier answer; a blank normalized answer is unanswered."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "A. Option A", "high", [2]),
        ]))
        questions_file = fx.put_json("questions.json", make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A"),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "A. Option A"),
        ]))
        round_1 = fx.put_json("flow_answers_1.json", make_flow_answers({
            "Q1": {"value": "B. Option B", "disposition": "answered"},
            "Q2": {"value": "A. Option A", "disposition": "answered"},
        }))
        round_2 = fx.put_json("flow_answers_2.json", make_flow_answers({
            "Q1": {"disposition": "answered"},
            "Q2": {"value": "   ", "disposition": "answered"},
        }))

        rc, _, _ = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--flow-answers", str(round_1), "--flow-answers", str(round_2),
             "--repo-key", "owner-name", "--command", "expert-plan", "--run", "1-emptyval-p"],
            "shown")
        rows = fx.read_log_file("owner-name")
        q1_row = _row_for(rows, "Q1")
        q2_row = _row_for(rows, "Q2")
        h.test_result("empty flow value: exits 0 with two rows", rc == 0 and len(rows) == 2, f"rc={rc} n={len(rows)}")
        h.test_result("empty flow value: Q1 keeps round-1 answer ('B. Option B')",
                      q1_row.get("actual") == "B. Option B", f"actual={q1_row.get('actual')!r}")
        h.test_result("empty flow value: Q1 is an option answer with hit False",
                      q1_row.get("actual_source") == "option" and q1_row.get("hit") is False,
                      f"source={q1_row.get('actual_source')} hit={q1_row.get('hit')}")
        h.test_result("empty flow value: whitespace-only Q2 keeps round-1 answer ('A. Option A', hit True)",
                      q2_row.get("actual") == "A. Option A" and q2_row.get("hit") is True,
                      f"actual={q2_row.get('actual')!r} hit={q2_row.get('hit')}")

        blank_file = fx.put_json("answers_blank.json", make_answers({"Q1": "   "}))
        rc_blank, _, stderr_blank = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(blank_file), "--repo-key", "owner-blank", "--command", "expert-plan",
             "--run", "1-blank-p"],
            "shown")
        h.test_result("blank normalized answer: exits 0 with answers-invalid",
                      rc_blank == 0 and "nothing logged (answers-invalid" in stderr_blank,
                      f"rc={rc_blank} stderr={stderr_blank}")


def case_repo_key_allowlist_widened(h):
    """Finding F2: owner-name may contain _ and . (ASCII); '..' and a leading '.' are rejected."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([("Q1", "A. Option A", "high", [1])]))
        questions_file = fx.put_json("questions.json", make_questions([("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A")]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))

        def log_key(key):
            return run_helper(
                fx.home, fx.log_root,
                ["--validated", str(validated_file), "--questions", str(questions_file),
                 "--answers", str(answers_file), "--repo-key", key, "--command", "expert-plan",
                 "--run", "1-allow-p"],
                "shown")

        rc_u, _, err_u = log_key("owner-my_repo")
        h.test_result("repo key allowlist: owner-my_repo is logged",
                      rc_u == 0 and "logged 1" in err_u, f"stderr={err_u}")
        rc_d, _, err_d = log_key("owner-my.repo")
        h.test_result("repo key allowlist: owner-my.repo is logged",
                      rc_d == 0 and "logged 1" in err_d, f"stderr={err_d}")
        rc_dd, _, err_dd = log_key("owner-a..b")
        h.test_result("repo key allowlist: owner-a..b is rejected (bad-repo-key)",
                      rc_dd == 0 and "bad-repo-key" in err_dd, f"stderr={err_dd}")
        rc_lead, _, err_lead = log_key(".owner-name")
        h.test_result("repo key allowlist: leading-dot key is rejected (bad-repo-key)",
                      rc_lead == 0 and "bad-repo-key" in err_lead, f"stderr={err_lead}")

        # The digest reads the same underscore spelling (same allowlist on its side)
        install_log_in_home(fx.home, fx.log_root)
        rc_stats, out_stats, err_stats = run_digest_raw(fx.home, ["owner/my_repo", "--stats"])
        stats_pred = _predictions(json.loads(out_stats)) if rc_stats == 0 and out_stats.strip() else {}
        h.test_result("repo key allowlist: digest owner/my_repo --stats reads the logged row",
                      rc_stats == 0 and stats_pred.get("logged") == 1 and stats_pred.get("read_error") is None,
                      f"rc={rc_stats} predictions={stats_pred} stderr={err_stats}")


def case_stats_nonstring_fields_malformed(h):
    """Finding 3: non-string run/question_id/command or non-bool hit is counted malformed, not an internal error."""
    with Fixture() as fx:
        content = "\n".join([
            _stats_row("r1", "Q1", "A. Option A", "A. Option A", True),
            _stats_row(["x"], "Q2", "A. Option A", "A. Option A", True),
            _stats_row("r2", "Q3", "A. Option A", "A. Option A", "yes"),
            _stats_row("r3", ["Q4"], "A. Option A", "A. Option A", True),
        ]) + "\n"
        fx.write_log_file("owner-name", content)

        rc, stats, stderr = run_digest_stats(fx.home, fx.log_root, "owner/name")
        pred = _predictions(stats)
        h.test_result("non-string fields: exits 0, no internal error",
                      rc == 0 and "internal error" not in stderr, f"rc={rc} stderr={stderr}")
        h.test_result("non-string fields: one valid row logged",
                      pred.get("logged") == 1, f"predictions={pred}")
        h.test_result("non-string fields: three rows counted malformed",
                      pred.get("malformed_lines") == 3, f"malformed={pred.get('malformed_lines')}")
        h.test_result("non-string fields: read_error stays null",
                      pred.get("read_error") is None, f"read_error={pred.get('read_error')}")


def case_symlinked_log_cannot_escape_root(h):
    """Finding 4: a dangling or directory symlink that points outside log_root is refused; no file is created outside."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([("Q1", "A. Option A", "high", [1])]))
        questions_file = fx.put_json("questions.json", make_questions([("Q1", "First", ["A. Option A", "B. Option B"], "A. Option A")]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A"}))

        # Writer: predictions.jsonl is a dangling link to a file outside the root
        dangling_dir = fx.log_root / "owner-dangle"
        dangling_dir.mkdir(parents=True)
        outside_file = fx.home / "outside-target.jsonl"
        (dangling_dir / "predictions.jsonl").symlink_to(outside_file)
        rc1, _, err1 = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-dangle", "--command", "expert-plan",
             "--run", "1-dangle-p"],
            "shown")
        h.test_result("dangling log link: exits 0 with nothing logged",
                      rc1 == 0 and "nothing logged (" in err1, f"rc={rc1} stderr={err1}")
        h.test_result("dangling log link: no file created outside the log root",
                      not outside_file.exists(), "target was created outside log_root")

        # Writer: the owner-name directory is a symlink to a directory outside the root
        elsewhere = fx.home / "elsewhere"
        (fx.log_root / "owner-subdir").symlink_to(elsewhere, target_is_directory=True)
        rc2, _, err2 = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-subdir", "--command", "expert-plan",
             "--run", "1-subdir-p"],
            "shown")
        h.test_result("subdir link outside root: exits 0 with nothing logged",
                      rc2 == 0 and "nothing logged (" in err2, f"rc={rc2} stderr={err2}")
        h.test_result("subdir link outside root: no file created outside the log root",
                      not (elsewhere / "predictions.jsonl").exists(), "file created outside log_root")

        # Digest: a dangling predictions.jsonl link is reported as read_error, not an empty log
        digest_dir = fx.home / ".claude" / "precedent" / "owner-dangle"
        digest_dir.mkdir(parents=True)
        (digest_dir / "predictions.jsonl").symlink_to(fx.home / "missing-target.jsonl")
        rc_d, out_d, _ = run_digest_raw(fx.home, ["owner/dangle", "--stats"])
        pred_d = _predictions(json.loads(out_d)) if rc_d == 0 and out_d.strip() else {}
        h.test_result("digest dangling link: read_error is set, logged 0",
                      pred_d.get("read_error") is not None and pred_d.get("logged") == 0,
                      f"rc={rc_d} predictions={pred_d}")


def case_digest_unresolved_repo_arg_not_read(h):
    """Finding 5: an unresolved repo argument is never joined into a log path; the log is not read."""
    with Fixture() as fx:
        decoy = fx.home / "x"
        decoy.mkdir(parents=True)
        (decoy / "predictions.jsonl").write_text(_stats_row("r1", "Q1", "A", "A", True) + "\n", encoding="utf-8")
        (fx.home / ".claude" / "precedent").mkdir(parents=True)

        rc, out, _ = run_digest_raw(fx.home, ["../../x", "--stats"])
        pred = _predictions(json.loads(out)) if rc == 0 and out.strip() else {}
        h.test_result("unresolved repo arg: decoy log is not counted",
                      pred.get("logged") == 0, f"rc={rc} predictions={pred}")
        h.test_result("unresolved repo arg: read_error explains the refusal",
                      pred.get("read_error") is not None and "not a valid owner-name" in pred.get("read_error", ""),
                      f"read_error={pred.get('read_error')}")


def case_md_hit_rate_subset_figure(h):
    """Finding 6: the markdown hit-rate line shows Pat on the baseline subset next to the baseline figure."""
    with Fixture() as fx:
        content = "\n".join([
            _stats_row("r1", "Q1", "A. Option A", "A. Option A", True, recommended="A. Option A"),
            _stats_row("r1", "Q2", "B. Option B", "A. Option A", False, recommended="A. Option A"),
            _stats_row("r1", "Q3", "B. Option B", "B. Option B", True, recommended=None),
        ]) + "\n"
        fx.write_log_file("owner-name", content)
        install_log_in_home(fx.home, fx.log_root)

        rc, out, _ = run_digest_raw(fx.home, ["owner/name", "--stats", "--format", "md"])
        h.test_result("md hit rates: exits 0", rc == 0, f"rc={rc}")
        h.test_result("md hit rates: overall Pat rate labelled with its own n",
                      "pat overall=(0.6667, n=3)" in out, f"line not found in: {out[:400]}")
        h.test_result("md hit rates: Pat on the baseline subset shown beside the baseline figure",
                      "pat=(0.5000)" in out and "baseline-recommended=(1.0000, n=2)" in out,
                      f"line not found in: {out[:400]}")


def case_recommended_type_and_options_guard(h):
    """Finding 7: a non-string recommended, or one not among the question's options, is written as null."""
    with Fixture() as fx:
        validated_file = fx.put_json("validated.json", make_validated([
            ("Q1", "A. Option A", "high", [1]),
            ("Q2", "A. Option A", "high", [2]),
        ]))
        questions_file = fx.put_json("questions.json", make_questions([
            ("Q1", "First", ["A. Option A", "B. Option B"], 2),
            ("Q2", "Second", ["A. Option A", "B. Option B"], "Z. Not an option"),
        ]))
        answers_file = fx.put_json("answers.json", make_answers({"Q1": "A. Option A", "Q2": "B. Option B"}))

        rc, _, stderr = run_helper(
            fx.home, fx.log_root,
            ["--validated", str(validated_file), "--questions", str(questions_file),
             "--answers", str(answers_file), "--repo-key", "owner-name", "--command", "expert-plan",
             "--run", "1-recommended-p"],
            "shown")
        rows = fx.read_log_file("owner-name")
        h.test_result("recommended guard: rows still logged (exit 0)", rc == 0 and len(rows) == 2,
                      f"rc={rc} n={len(rows)} stderr={stderr}")
        h.test_result("recommended guard: integer recommended becomes null",
                      _row_for(rows, "Q1").get("recommended", "missing") is None,
                      f"recommended={_row_for(rows, 'Q1').get('recommended', 'missing')!r}")
        h.test_result("recommended guard: recommended outside the options becomes null",
                      _row_for(rows, "Q2").get("recommended", "missing") is None,
                      f"recommended={_row_for(rows, 'Q2').get('recommended', 'missing')!r}")


if __name__ == "__main__":
    h = Harness("PRECEDENT-LOG TEST SUITE")

    case_idempotent_per_key(h)
    case_malformed_input_logs_nothing(h)
    case_all_or_nothing_block_drop(h)
    case_unwritable_log_root(h)
    case_row_contract(h)
    case_stats_hit_rate(h)
    case_stats_corrupt_lines(h)
    case_empty_or_absent_log(h)
    case_argv_errors_exit_2(h)
    case_flow_answers_latest_wins(h)
    case_flow_accepted_on_continue(h)
    case_flow_unresolved_accepted_no_row(h)
    case_letter_to_label_mapping(h)
    case_ambiguous_letter_free_text(h)
    case_answer_normalization(h)
    case_lowercase_recommended_marker(h)
    case_real_ask_label_with_recommended(h)
    case_round_trip_real_validator(h)
    case_make_validated_matches_real_validator(h)
    case_match_never_opens_predictions(h)
    case_non_utf8_log_is_log_unwritable(h)
    case_flow_empty_value_keeps_earlier_answer(h)
    case_repo_key_allowlist_widened(h)
    case_stats_nonstring_fields_malformed(h)
    case_symlinked_log_cannot_escape_root(h)
    case_digest_unresolved_repo_arg_not_read(h)
    case_md_hit_rate_subset_figure(h)
    case_recommended_type_and_options_guard(h)

    h.summarize_and_exit()
