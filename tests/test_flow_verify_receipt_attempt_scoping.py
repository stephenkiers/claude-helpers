#!/usr/bin/env python3
"""
Tests for attempt-scoped disk counting and first-attempt identity floor.

Per ADR-0023 amendment (decision #310):
- Relayed questions are counted from disk by attempt, scoped to ATTEMPT field.
- Plan/review freshness uses a first-attempt identity floor from log.md <STEP> begin lines.
- Field-less question files fail closed (not counted, listed as malformed).
- Prior-attempt files count only via reused-answers marker.

Run with: python3 tests/test_flow_verify_receipt_attempt_scoping.py
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness
from test_flow_verify_receipt import create_contribution, create_reviewer_index, create_session


def iso_timestamp(timestamp: float) -> str:
    """Convert a float timestamp to ISO8601 format with Z suffix."""
    dt = datetime.datetime.utcfromtimestamp(timestamp)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_verifier(step: str, receipt_text: str, flow_dir: Path, since: str) -> tuple:
    """Run flow-verify-receipt.py and return (exit_code, stdout, stderr).

    Args:
        step: 'plan' or 'review'
        receipt_text: Receipt content (plain text, not a file path)
        flow_dir: Path to flow directory
        since: ISO8601 timestamp string (e.g., "2026-10-14T00:00:00Z")
    """
    # Write receipt to a temp file since the script expects a path
    receipt_file = flow_dir.parent / "receipt.md"
    receipt_file.write_text(receipt_text, encoding="utf-8")

    cmd = [
        sys.executable,
        str(Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"),
        "--step", step,
        "--receipt", str(receipt_file),
        "--flow-dir", str(flow_dir),
        "--since", since,
    ]
    env = dict(os.environ)
    if step == "plan":
        # Plan artifacts resolve under $HOME/.claude; build a complete session fixture there.
        home = str(flow_dir.parent)
        env["HOME"] = home
        create_reviewer_index(home)
        session_dir = create_session(home, "20261013T222013-30924", briefs=receipt_text.count("**Brief B"))
        Path(f"{session_dir}/context.md").write_text("# Context")
        # Freshness tests stage the wanted mtime on flow_dir/context.md; carry it to the session artifact.
        staged = flow_dir / "context.md"
        if staged.exists():
            m = staged.stat().st_mtime
            os.utime(f"{session_dir}/context.md", (m, m))
        Path(f"{session_dir}/selected-experts.md").write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def create_plan_question_file(flow_dir: Path, seq: int, attempt: int = 1, num_questions: int = 1, include_attempt: bool = True) -> Path:
    """Create a question file questions/plan-NN.json."""
    flow_dir.mkdir(exist_ok=True, parents=True)
    questions_dir = flow_dir / "questions"
    questions_dir.mkdir(exist_ok=True)

    filename = f"plan-{seq:02d}.json"
    questions = [
        {"id": f"q{i+1}", "question": f"Question {i+1}"}
        for i in range(num_questions)
    ]

    data = {
        "step": "plan",
        "questions": questions,
    }
    if include_attempt:
        data["attempt"] = attempt

    question_file = questions_dir / filename
    question_file.write_text(json.dumps(data), encoding="utf-8")
    return question_file


def create_review_question_file(flow_dir: Path, seq: int, attempt: int = 1, num_questions: int = 1, include_attempt: bool = True) -> Path:
    """Create a question file questions/review-NN.json."""
    flow_dir.mkdir(exist_ok=True, parents=True)
    questions_dir = flow_dir / "questions"
    questions_dir.mkdir(exist_ok=True)

    filename = f"review-{seq:02d}.json"
    questions = [
        {"id": f"q{i+1}", "question": f"Question {i+1}"}
        for i in range(num_questions)
    ]

    data = {
        "step": "review",
        "questions": questions,
    }
    if include_attempt:
        data["attempt"] = attempt

    question_file = questions_dir / filename
    question_file.write_text(json.dumps(data), encoding="utf-8")
    return question_file


def create_answers_file(flow_dir: Path, step: str, seq: int) -> Path:
    """Create an answers file to mark questions as answered."""
    flow_dir.mkdir(exist_ok=True, parents=True)
    answers_dir = flow_dir / "answers"
    answers_dir.mkdir(exist_ok=True)

    filename = f"{step}-{seq:02d}.json"
    answers_file = answers_dir / filename
    answers_file.write_text(json.dumps({"answered": True}), encoding="utf-8")
    return answers_file


def create_log_file(flow_dir: Path, step: str, timestamp: float) -> Path:
    """Create log.md with a <STEP> begin line at the given timestamp."""
    flow_dir.mkdir(exist_ok=True, parents=True)
    log_file = flow_dir / "log.md"

    iso_time = iso_timestamp(timestamp)
    content = f"{iso_time} {step} begin\n"
    log_file.write_text(content, encoding="utf-8")
    return log_file


def main():
    h = Harness("FLOW-VERIFY-RECEIPT ATTEMPT-SCOPING AND IDENTITY FLOOR TESTS")

    # Test A1: #1650 regression
    print("[Test A1] #1650 regression: 20 briefs, zero markers, all attempt:1 files answered")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        for i in range(1, 21):
            create_plan_question_file(flow_dir, i, attempt=1, num_questions=1)
            create_answers_file(flow_dir, "plan", i)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        briefs = "\n".join([f"**Brief B{i}**" for i in range(1, 21)])
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n{briefs}"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A1", exit_code == 0,
                     f"exit={exit_code}, expected 0. stdout={stdout[:200] if stdout else ''}")

    # Test A2: Field-less answered file
    print("[Test A2] Field-less answered file: not counted, malformed suffix")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        flow_dir.mkdir(parents=True)
        questions_dir = flow_dir / "questions"
        questions_dir.mkdir()
        question_file = questions_dir / "plan-01.json"
        question_file.write_text(json.dumps({
            "step": "plan",
            "questions": [{"id": "q1", "question": "Q1"}]
        }), encoding="utf-8")

        create_answers_file(flow_dir, "plan", 1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A2", exit_code == 1 and "malformed" in stdout,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A3: ATTEMPT:2, attempt:1 files without reused marker
    print("[Test A3] ATTEMPT:2, attempt:1 files, no reused marker")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=3)
        create_answers_file(flow_dir, "plan", 1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        briefs = "\n".join([f"**Brief B{i}**" for i in range(1, 4)])
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2\n{briefs}"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A3", exit_code == 1 and "no answered" in stdout.lower(),
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A4: ATTEMPT:2 with reused-answers marker
    print("[Test A4] ATTEMPT:2, attempt:1 files with reused-answers marker")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=3)
        create_answers_file(flow_dir, "plan", 1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        briefs = "\n".join([f"**Brief B{i}**" for i in range(1, 4)])
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2\n<!-- reused-answers: questions/plan-01.json -->\n{briefs}"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A4", exit_code == 0,
                     f"exit={exit_code}, expected 0. stdout={stdout[:200]}")

    # Test A5: Multiple ATTEMPT lines, last wins
    print("[Test A5] Receipt with two ATTEMPT: lines, last one wins")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=2, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\nATTEMPT: 2\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A5", exit_code == 0,
                     f"exit={exit_code}, expected 0. stdout={stdout[:200]}")

    # Test A6: awaiting-answers marker without answers file
    print("[Test A6] awaiting-answers marker without answers file")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        # NO answers file

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n<!-- awaiting-answers: questions/plan-01.json -->\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A6", exit_code == 1 and "unanswered" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A7: Identity floor - artifact between begin and --since passes
    print("[Test A7] Identity floor: artifact between begin and --since")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        begin_time = now - 100
        since_time = iso_timestamp(now - 10)

        create_log_file(flow_dir, "plan", begin_time)

        context_file = flow_dir / "context.md"
        context_file.write_text("# Plan context", encoding="utf-8")
        context_mtime = begin_time + 50
        os.utime(context_file, (context_mtime, context_mtime))

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1"

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since_time)
        h.test_result("Test A7", exit_code == 0,
                     f"exit={exit_code}, expected 0. stdout={stdout[:200]}")

    # Test A8: Identity floor - artifact older than begin
    print("[Test A8] Identity floor: artifact older than begin")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        begin_time = now - 50
        since_time = iso_timestamp(now - 100)

        create_log_file(flow_dir, "plan", begin_time)

        context_file = flow_dir / "context.md"
        context_file.write_text("# Old context", encoding="utf-8")
        context_mtime = begin_time - 100
        os.utime(context_file, (context_mtime, context_mtime))

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1"

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since_time)
        h.test_result("Test A8", exit_code == 1 and ("first attempt" in stdout or "older" in stdout),
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A9: Multiple begin lines, use newest
    print("[Test A9] Multiple begin lines: use newest")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        begin1_time = now - 200
        begin2_time = now - 100

        flow_dir.mkdir(parents=True)
        log_file = flow_dir / "log.md"
        iso1 = iso_timestamp(begin1_time)
        iso2 = iso_timestamp(begin2_time)
        log_file.write_text(f"{iso1} plan begin\n{iso2} plan begin\n", encoding="utf-8")

        context_file = flow_dir / "context.md"
        context_file.write_text("# Plan context", encoding="utf-8")
        context_mtime = begin1_time + 50
        os.utime(context_file, (context_mtime, context_mtime))

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1"
        since_time = iso_timestamp(now - 50)

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since_time)
        h.test_result("Test A9", exit_code == 1,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A10: No log.md fallback to --since
    print("[Test A10] No log.md: fallback to --since")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        since_time = iso_timestamp(now - 30)

        flow_dir.mkdir(parents=True)
        context_file = flow_dir / "context.md"
        context_file.write_text("# Plan context", encoding="utf-8")
        context_mtime = now - 40
        os.utime(context_file, (context_mtime, context_mtime))

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1"

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since_time)
        h.test_result("Test A10", exit_code == 1,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A11: Review with attempt scoping
    print("[Test A11] Review: ATTEMPT:2, attempt:1 files, no reused marker")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_review_question_file(flow_dir, 1, attempt=1, num_questions=2)
        create_answers_file(flow_dir, "review", 1)

        review_dir = tmp_path / "reviews"
        review_dir.mkdir(parents=True)
        review_file = review_dir / "review-20261013T222013-30924.md"
        review_file.write_text("# Review", encoding="utf-8")

        receipt = f"REVIEW_DIR: {review_dir}\nARTIFACTS: ok\nATTEMPT: 2\n## Needs you\n- **Item N1**\n- **Item N2**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("review", receipt, flow_dir, since)
        h.test_result("Test A11", exit_code == 1,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A12: Review identity floor
    print("[Test A12] Review: identity floor using log.md")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        begin_time = now - 100
        since_time = iso_timestamp(now - 10)

        create_log_file(flow_dir, "review", begin_time)

        action_plan = flow_dir / "claude-action-plan.md"
        action_plan.write_text("# Action Plan", encoding="utf-8")
        action_plan_mtime = begin_time - 50
        os.utime(action_plan, (action_plan_mtime, action_plan_mtime))

        review_dir = tmp_path / "reviews"
        review_dir.mkdir(parents=True)
        review_file = review_dir / "review-20261013T222013-30924.md"
        review_file.write_text("# Review", encoding="utf-8")

        receipt = f"REVIEW_DIR: {review_dir}\nARTIFACTS: ok\nATTEMPT: 1"

        exit_code, stdout, stderr = run_verifier("review", receipt, flow_dir, since_time)
        h.test_result("Test A12", exit_code == 1,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A13: Current-attempt unanswered file
    print("[Test A13] Current-attempt question file with no answers")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A13", exit_code == 1 and "unanswered" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A14: out-of-attempt marker
    print("[Test A14] awaiting-answers marker naming attempt:1 file under ATTEMPT:2")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2\n<!-- awaiting-answers: questions/plan-01.json -->\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A14", exit_code == 1 and "out-of-attempt" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A15: Mix of answered and unanswered
    print("[Test A15] Same attempt: mix of answered and unanswered")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)
        create_plan_question_file(flow_dir, 2, attempt=1, num_questions=1)

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n<!-- awaiting-answers: questions/plan-01.json -->\n<!-- awaiting-answers: questions/plan-02.json -->\n**Brief B1**\n**Brief B2**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        h.test_result("Test A15", exit_code == 1 and "unanswered" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A16: awaiting-answers marker on unanswered out-of-attempt file
    # (Finding 2 regression: should be classified as unanswered, not out-of-attempt)
    print("[Test A16] awaiting-answers on unanswered attempt:1 file under ATTEMPT:2")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        # NO answers file for this attempt:1 file

        plan_dir = tmp_path / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")

        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2\n<!-- awaiting-answers: questions/plan-01.json -->\n**Brief B1**"
        since = iso_timestamp(time.time())

        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since)
        # Should fail with unanswered, not out-of-attempt (because the file has no answers)
        h.test_result("Test A16", exit_code == 1 and "unanswered" in stdout and "out-of-attempt" not in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
