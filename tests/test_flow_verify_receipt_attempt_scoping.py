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
        # Coupling: the session's decisions.md gets one brief per "**Brief B" marker in the receipt
        # text, so a test states its expected brief count by writing that many markers into it.
        session_dir = create_session(home, "20261013T222013-30924", briefs=receipt_text.count("**Brief B"))
        Path(f"{session_dir}/context.md").write_text("# Context")
        # Freshness tests stage the wanted mtime on flow_dir/context.md; carry it to the session artifact.
        staged = flow_dir / "context.md"
        if staged.exists():
            m = staged.stat().st_mtime
            os.utime(f"{session_dir}/context.md", (m, m))
        Path(f"{session_dir}/selected-experts.md").write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")
    else:
        # Review artifacts must live under $HOME/.claude/reviews; make_review_fixture builds them
        # under the same HOME (flow_dir.parent), so the review check reaches freshness and rulings.
        env["HOME"] = str(flow_dir.parent)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def make_review_fixture(tmp_path: Path, needs_you: int, action_plan_mtime: float = None) -> Path:
    """Build a complete review dir under <tmp_path>/.claude/reviews (the HOME run_verifier uses).

    The action plan has `needs_you` *Needs you* items, so the only thing a test can trip on is the
    behaviour it sets up (attempt scoping or the freshness floor).
    """
    review_dir = tmp_path / ".claude" / "reviews" / "repo" / "review-20261013T222013-30924"
    review_dir.mkdir(parents=True)
    items = "".join(f"### {i}. Item {i}\n- **Why**: x\n\n" for i in range(1, needs_you + 1))
    action_plan = review_dir / "claude-action-plan.md"
    action_plan.write_text(f"# Action Plan\n\n## Needs you: {needs_you}\n\n{items}## Doing it: 0\n", encoding="utf-8")
    if action_plan_mtime is not None:
        os.utime(action_plan, (action_plan_mtime, action_plan_mtime))
    (review_dir / "final-report.md").write_text("# Report", encoding="utf-8")
    (review_dir / "findings.json").write_text("[]", encoding="utf-8")
    (review_dir / "alice-pass1.md").write_text("x", encoding="utf-8")
    (review_dir / "bob-pass1.md").write_text("x", encoding="utf-8")
    return review_dir


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
        h.test_result("Test A9", exit_code == 1 and "older than this step's first attempt" in stdout,
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
        h.test_result("Test A10", exit_code == 1 and "older than this attempt" in stdout,
                     f"exit={exit_code}, expected 1. stdout={stdout[:300]}")

    # Test A11: Review with attempt scoping
    print("[Test A11] Review: ATTEMPT:2, attempt:1 files, no reused marker")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        create_review_question_file(flow_dir, 1, attempt=1, num_questions=2)
        create_answers_file(flow_dir, "review", 1)
        review_dir = make_review_fixture(tmp_path, needs_you=2)

        receipt = f"REVIEW_DIR: {review_dir}\nARTIFACTS: ok\nATTEMPT: 2\n"
        since = iso_timestamp(time.time() - 3600)

        exit_code, stdout, stderr = run_verifier("review", receipt, flow_dir, since)
        h.test_result("Test A11", exit_code == 1 and "review rulings incomplete (needs-you: 2, asked: 0" in stdout
                      and "no answered review-* question files for attempt 2" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A12: Review identity floor
    print("[Test A12] Review: identity floor using log.md")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        tmp_path = Path(tmp)

        now = time.time()
        begin_time = now - 100
        since_time = iso_timestamp(now - 10)

        create_log_file(flow_dir, "review", begin_time)
        review_dir = make_review_fixture(tmp_path, needs_you=0, action_plan_mtime=begin_time - 50)

        receipt = f"REVIEW_DIR: {review_dir}\nARTIFACTS: ok\nATTEMPT: 1"

        exit_code, stdout, stderr = run_verifier("review", receipt, flow_dir, since_time)
        h.test_result("Test A12", exit_code == 1 and "review artifacts missing (action plan older than this step's first attempt)" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

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

    # Test A17: floor is min(begin, since): a since OLDER than begin lowers the floor to since
    print("[Test A17] Identity floor direction: artifact newer than since but older than begin passes (min, not max)")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        now = time.time()
        begin_time = now - 10
        since_time = iso_timestamp(now - 100)
        create_log_file(flow_dir, "plan", begin_time)
        context_file = flow_dir / "context.md"
        context_file.write_text("# Plan context", encoding="utf-8")
        os.utime(context_file, (now - 50, now - 50))
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1"
        # min(begin=now-10, since=now-100) = now-100 passes; max would be now-10 and fail.
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, since_time)
        h.test_result("Test A17", exit_code == 0, f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A18: a redo line supersedes the first-attempt begin line as the floor
    print("[Test A18] Redo: artifact from before the redo line fails even though it postdates begin")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        flow_dir.mkdir(parents=True)
        now = time.time()
        begin_time, redo_time = now - 300, now - 100
        (flow_dir / "log.md").write_text(
            f"{iso_timestamp(begin_time)} plan begin\n{iso_timestamp(redo_time)} plan redo attempt=2 redos=1 note_chars=0\n",
            encoding="utf-8")
        context_file = flow_dir / "context.md"
        context_file.write_text("# Old plan context", encoding="utf-8")
        os.utime(context_file, (now - 200, now - 200))
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2"
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, iso_timestamp(now - 90))
        h.test_result("Test A18", exit_code == 1 and "older than this step's first attempt" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A19: malformed file fails closed even when enough valid files satisfy the count
    print("[Test A19] Fail closed: one malformed current-attempt file alongside enough valid ones")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)
        (flow_dir / "questions" / "plan-02.json").write_text("{not json", encoding="utf-8")
        create_answers_file(flow_dir, "plan", 2)
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 1\n**Brief B1**"
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, iso_timestamp(time.time() - 3600))
        h.test_result("Test A19", exit_code == 1 and "malformed: plan-02.json (not json)" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A20: out-of-attempt marker-named file fails closed despite enough valid current files
    print("[Test A20] Fail closed: out-of-attempt marker-named file alongside enough valid ones")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)
        create_plan_question_file(flow_dir, 2, attempt=2, num_questions=1)
        create_answers_file(flow_dir, "plan", 2)
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = (f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: 2\n"
                   "<!-- awaiting-answers: questions/plan-01.json -->\n**Brief B1**")
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, iso_timestamp(time.time() - 3600))
        h.test_result("Test A20", exit_code == 1 and "out-of-attempt: plan-01.json" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A21: unanswered current-attempt report-kind file is unanswered
    print("[Test A21] Report-kind current-attempt file without answers is unanswered")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        qdir = flow_dir / "questions"
        qdir.mkdir(parents=True)
        (qdir / "plan-01.json").write_text(json.dumps({
            "kind": "report", "step": "plan", "attempt": 1, "round": 1,
        }), encoding="utf-8")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "flow_verify_receipt_a21", Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc = mod.count_relayed_questions(flow_dir, "plan", "", 1)
        h.test_result("Test A21", rc.unanswered == ["plan-01.json"] and rc.asked == 0 and not rc.malformed,
                     f"unanswered={rc.unanswered}, asked={rc.asked}, malformed={rc.malformed}")

    # Test A22: a missing ATTEMPT line fails closed when a question file is attempt > 1
    print("[Test A22] Missing ATTEMPT line with an attempt-2 question file fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        create_plan_question_file(flow_dir, 1, attempt=2, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\n**Brief B1**"
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, iso_timestamp(time.time() - 3600))
        h.test_result("Test A22", exit_code == 1 and "no ATTEMPT: line" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    # Test A23: an invalid ATTEMPT value fails closed instead of defaulting to 1
    print("[Test A23] Invalid ATTEMPT value fails closed")
    with tempfile.TemporaryDirectory() as tmp:
        flow_dir = Path(tmp) / "flow"
        create_plan_question_file(flow_dir, 1, attempt=1, num_questions=1)
        create_answers_file(flow_dir, "plan", 1)
        plan_dir = Path(tmp) / ".claude" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "issue-slug-20261013T222013-30924.md"
        plan_file.write_text("# Plan", encoding="utf-8")
        receipt = f"FINAL_PLAN_PATH: {plan_file}\nARTIFACTS: ok\nATTEMPT: two\n**Brief B1**"
        exit_code, stdout, stderr = run_verifier("plan", receipt, flow_dir, iso_timestamp(time.time() - 3600))
        h.test_result("Test A23", exit_code == 1 and "invalid ATTEMPT" in stdout,
                     f"exit={exit_code}, stdout={stdout[:300]}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
