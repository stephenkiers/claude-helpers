#!/usr/bin/env python3
"""
Black-box tests for flow-verify-receipt.py split-location behavior (#318).

Tests verify that receipt marker paths (questions/plan-NN.json, questions/review-NN.json, etc.)
are resolved against FLOW_DIR, not the plan-session directory. Decoy files in the plan-session
or review directory must never satisfy a missing FLOW_DIR file.

This suite is deliberately separate from tests/test_flow_verify_receipt.py. It exists to pin
split-location resolution (FLOW_DIR versus the plan-session or review directory), which the main
suite covers only incidentally. Its fixtures use the real answers shape written by the flow relay,
`{"step", "seq", "answers": [{"id", "response"}]}`, which is the shape the main suite uses too.
Keep the two fixture shapes in step; do not merge the suites without re-checking both.

This test suite validates the fix for #318: the verifier is the sole owner of mechanical checks
(question counts, answers files, disposition, excerpts, ledger, reactions, contributions,
artifacts), and receipt marker paths are relative to FLOW_DIR with absolute paths given for
evidence reading.

Run with: python3 tests/test_flow_verify_receipt_split_location.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

NEEDS_YOU_PLAN_ONE = (
    "# Action Plan\n\n## Needs you\n\n### 1. Pick a thing\n\nWhere: a.py\n\n"
    "## Doing it\n\n### 1. Fix a\n"
)

NEEDS_YOU_PLAN_TWO = (
    "# Action Plan\n\n## Needs you\n\n### 1. Pick a thing\n\nWhere: a.py\n\n"
    "### 2. Pick another thing\n\nWhere: b.py\n\n## Doing it\n\n### 1. Fix a\n"
)


def since_timestamp():
    """ISO-8601 UTC an hour ago, so every fixture file is fresher than --since."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_reviewer_index(home: str) -> None:
    """Create a minimal reviewers/index.yaml fixture."""
    index_path = Path(home) / ".claude" / "reviewers" / "index.yaml"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("""reviewers:
  - name: Danielle the Designer
    file: danielle-designer.yaml
""")


def create_plan_session(home: str, invocation_id: str, briefs: int = 0) -> str:
    """Create a plan session directory with decisions.md.

    Returns the session directory path.
    """
    session_dir = Path(home) / ".claude" / "plan-sessions" / "repo_key" / f"plan-{invocation_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # Create decisions.md with briefs
    body = "# Decisions\n\n## Decision briefs\n\n"
    for n in range(1, briefs + 1):
        body += f"#### Q{n}. Question {n}\n\nWhy it matters: because.\n\n"

    (session_dir / "decisions.md").write_text(body)
    (session_dir / "plan.md").write_text("# Plan\n")
    (session_dir / "context.md").write_text("# Context\n")
    (session_dir / "selected-experts.md").write_text("| Expert |\n|--------|\n| Danielle the Designer |")
    (session_dir / "danielle-designer-contribution.md").write_text("# Contribution\n<!-- contribution-end -->")

    return str(session_dir)


def create_plan_path(home: str, invocation_id: str) -> str:
    """Create a plan file and return its path."""
    plan_path = Path(home) / ".claude" / "plans" / f"issue-slug-{invocation_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Plan\n")
    return str(plan_path)


def create_review_dir(home: str, action_plan: str = "# Action Plan\n\n## Doing it\n- item 1\n") -> str:
    """Create a review directory with required files. Returns the review directory path."""
    review_dir = Path(home) / ".claude" / "reviews" / "repo_key" / "12-review-2026"
    review_dir.mkdir(parents=True, exist_ok=True)

    # Create required files
    (review_dir / "claude-action-plan.md").write_text(action_plan)
    (review_dir / "final-report.md").write_text("# Final Report\n")
    (review_dir / "findings.json").write_text('{"findings": []}')
    (review_dir / "ariadne-pass1.md").write_text("# Pass 1\n")
    (review_dir / "nigel-pass1.md").write_text("# Pass 1 #2\n")

    return str(review_dir)


def write_question_file(qdir: Path, step: str, seq: int, count: int) -> None:
    """Write questions/<step>-NN.json holding `count` questions, in the relay's real shape."""
    qdir.mkdir(parents=True, exist_ok=True)
    questions = [
        {"id": f"q{i}", "question": f"Q{i}?", "header": "Q",
         "options": [{"label": "A", "description": "a"}], "multiSelect": False}
        for i in range(1, count + 1)
    ]
    (qdir / f"{step}-{seq:02d}.json").write_text(json.dumps({
        "step": step, "attempt": 1, "command": f"expert-{step}", "context_path": None, "summary": None,
        "questions": questions,
    }))


def write_answer_file(adir: Path, step: str, seq: int, count: int) -> None:
    """Write answers/<step>-NN.json answering `count` questions, in the relay's real shape."""
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{step}-{seq:02d}.json").write_text(json.dumps({
        "step": step, "seq": seq,
        "answers": [{"id": f"q{i}", "response": f"A{i}"} for i in range(1, count + 1)],
    }))


def run_verifier(step: str, receipt_path: str, flow_dir: str, home: str) -> tuple:
    """Run the verifier and return (exit_code, stdout+stderr)."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
        "--since", since_timestamp(),
        "--flow-dir", flow_dir,
    ]
    env = {**os.environ, "HOME": home}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


def main():
    h = Harness("FLOW-VERIFY-RECEIPT SPLIT-LOCATION TEST SUITE (#318)")

    # ============================================================================
    # LEGACY PLAN TESTS (awaiting-answers markers, legacy brief-vs-asked count)
    # ============================================================================

    print("\n[LEGACY PLAN SPLIT-LOCATION TESTS]")
    print("Test L1: Legacy plan split-location positive (questions in FLOW_DIR, decoy in session)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11111"
        session_dir = create_plan_session(tmp_home, invocation_id, briefs=4)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Real marker files in FLOW_DIR (the live location), all answered
        for seq in range(1, 5):
            write_question_file(flow_dir / "questions", "plan", seq, 1)
            write_answer_file(flow_dir / "answers", "plan", seq, 1)

        # Decoy in the session dir: a complete question file named like a real marker, with NO
        # answer beside it. The decoy sits where the verifier never reads, so this pins the absence
        # of a session-dir read path. It does not catch a fallback unless that fallback reads pending-questions.
        write_question_file(Path(session_dir) / "pending-questions", "plan", 1, 1)

        # Create receipt with markers
        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
<!-- awaiting-answers: questions/plan-02.json -->
<!-- awaiting-answers: questions/plan-03.json -->
<!-- awaiting-answers: questions/plan-04.json -->
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "L1: Questions in FLOW_DIR with answers → exit 0, ARTIFACTS: ok",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test L2: Legacy plan split-location negative (no FLOW_DIR files, only session decoy)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11112"
        session_dir = create_plan_session(tmp_home, invocation_id, briefs=2)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Complete decoy question AND answer, in the legacy session layout, NOT in FLOW_DIR.
        # Pins that a session-dir answer does not satisfy the FLOW_DIR marker. Like the other decoys,
        # it only catches a fallback that reads pending-questions/pending-answers.
        write_question_file(Path(session_dir) / "pending-questions", "plan", 1, 1)
        write_answer_file(Path(session_dir) / "pending-answers", "plan", 1, 1)

        # Create receipt with markers pointing to FLOW_DIR locations
        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "L2: No files in FLOW_DIR (only in session decoy) → exit 1, exact unanswered reason",
            rc == 1 and "plan checkpoint incomplete (briefs: 2, asked: 0, no answered plan-* question files for attempt 1, unanswered: plan-01.json)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    # ============================================================================
    # PARTIAL-FAILURE TESTS (one case per gap: missing answers, fewer markers than
    # questions, non-question files in the dir)
    # ============================================================================

    print("\n[PARTIAL-FAILURE TESTS]")
    print("Test P1: Answers missing for one of two marked questions → exit 1, unanswered names it")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11115"
        create_plan_session(tmp_home, invocation_id, briefs=2)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        write_question_file(flow_dir / "questions", "plan", 1, 1)
        write_answer_file(flow_dir / "answers", "plan", 1, 1)
        write_question_file(flow_dir / "questions", "plan", 2, 1)
        # answers/plan-02.json deliberately absent

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_path.write_text(f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
<!-- awaiting-answers: questions/plan-02.json -->
Decision: OK
<!-- step-end -->""")

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "P1: Missing answers file for plan-02 → exit 1, exact reason (briefs: 2, asked: 1, unanswered: plan-02.json)",
            rc == 1 and "plan checkpoint incomplete (briefs: 2, asked: 1, unanswered: plan-02.json)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test P2: Fewer markers than questions on disk → exit 0, disk is authoritative (markers are only an index)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11116"
        create_plan_session(tmp_home, invocation_id, briefs=3)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Three answered question files exist, but the receipt marks only one of them.
        for seq in range(1, 4):
            write_question_file(flow_dir / "questions", "plan", seq, 1)
            write_answer_file(flow_dir / "answers", "plan", seq, 1)

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_path.write_text(f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
Decision: OK
<!-- step-end -->""")

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "P2: 3 briefs, 1 marker (3 answered current-attempt files on disk) → exit 0, all 3 counted",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test P3: Non-question files in FLOW_DIR (unmarked or wrong step) are ignored → exit 0")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11117"
        create_plan_session(tmp_home, invocation_id, briefs=1)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        write_question_file(flow_dir / "questions", "plan", 1, 1)
        write_answer_file(flow_dir / "answers", "plan", 1, 1)
        # Non-question files and an unmarked plan file: none of these is named by a marker.
        (flow_dir / "questions" / "notes.txt").write_text("scratch\n")
        (flow_dir / "questions" / "README.md").write_text("# not a question file\n")
        (flow_dir / "answers" / "notes.txt").write_text("scratch\n")
        write_question_file(flow_dir / "questions", "review", 1, 2)

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_path.write_text(f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
Decision: OK
<!-- step-end -->""")

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "P3: Extra non-marker files in FLOW_DIR do not change the count → exit 0, ARTIFACTS: ok",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    # ============================================================================
    # EXISTENCE GAP TESTS (from plan Step 1)
    # ============================================================================

    print("\n[EXISTENCE GAP TESTS]")
    print("Test E1: Missing session plan.md → plan artifacts missing")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11113"
        session_dir = create_plan_session(tmp_home, invocation_id, briefs=0)
        plan_path = create_plan_path(tmp_home, invocation_id)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Remove the plan.md from session
        (Path(session_dir) / "plan.md").unlink()

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""FINAL_PLAN_PATH: {plan_path}
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "E1: Missing session plan.md → exit 1, exact reason 'plan artifacts missing (plan.md)'",
            rc == 1 and "plan artifacts missing (plan.md)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test E2: Missing FINAL_PLAN_PATH file → plan artifacts missing")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11114"
        create_plan_session(tmp_home, invocation_id, briefs=0)
        # Create plan_path but don't write the file (missing)
        plan_path = str(Path(tmp_home) / ".claude" / "plans" / f"issue-slug-{invocation_id}.md")
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""FINAL_PLAN_PATH: {plan_path}
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "E2: Missing FINAL_PLAN_PATH file → exit 1, exact reason 'plan artifacts missing (FINAL_PLAN_PATH file does not exist)'",
            rc == 1 and "plan artifacts missing (FINAL_PLAN_PATH file does not exist)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    # ============================================================================
    # REVIEW SPLIT-LOCATION TESTS
    # ============================================================================

    print("\n[REVIEW SPLIT-LOCATION TESTS]")
    print("Test RV1: Review split-location positive (questions in FLOW_DIR, decoy in review dir)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home, action_plan=NEEDS_YOU_PLAN_ONE)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # One Needs-you item in the action plan, answered by one question in FLOW_DIR
        write_question_file(flow_dir / "questions", "review", 1, 1)
        write_answer_file(flow_dir / "answers", "review", 1, 1)

        # Decoy in the review dir: complete question file, NO answer beside it. The decoy sits where
        # the verifier never reads, so this pins the absence of a review-dir read path. It does not
        # catch a fallback unless that fallback reads pending-questions.
        write_question_file(Path(review_dir) / "pending-questions", "review", 1, 1)

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""REVIEW_DIR: {review_dir}
<!-- awaiting-answers: questions/review-01.json -->
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV1: Review questions in FLOW_DIR with answers (1 Needs-you, 1 asked) → exit 0, ARTIFACTS: ok",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test RV1b: Review variant: 2 Needs-you items, 1 question asked → rulings incomplete")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home, action_plan=NEEDS_YOU_PLAN_TWO)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        write_question_file(flow_dir / "questions", "review", 1, 1)
        write_answer_file(flow_dir / "answers", "review", 1, 1)

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_path.write_text(f"""REVIEW_DIR: {review_dir}
<!-- awaiting-answers: questions/review-01.json -->
Decision: OK
<!-- step-end -->""")

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV1b: 2 Needs-you items, 1 asked → exit 1, exact reason (needs-you: 2, asked: 1)",
            rc == 1 and "review rulings incomplete (needs-you: 2, asked: 1)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test RV1c: Review Needs-you present with no marker → rulings incomplete")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home, action_plan=NEEDS_YOU_PLAN_ONE)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # The action plan has a Needs-you item, but the receipt marks no question at all.
        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_path.write_text(f"""REVIEW_DIR: {review_dir}
Decision: OK
<!-- step-end -->""")

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV1c: Needs-you item with no marker → exit 1, exact reason (needs-you: 1, asked: 0, no answered review-* question files for attempt 1)",
            rc == 1 and "review rulings incomplete (needs-you: 1, asked: 0, no answered review-* question files for attempt 1)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test RV2: Review split-location negative (no FLOW_DIR files, only review-dir decoy)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home, action_plan=NEEDS_YOU_PLAN_ONE)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Complete decoy question file in the review dir, NOT in FLOW_DIR
        write_question_file(Path(review_dir) / "pending-questions", "review", 1, 1)

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""REVIEW_DIR: {review_dir}
<!-- awaiting-answers: questions/review-01.json -->
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV2: No files in FLOW_DIR (only in review dir decoy) → exit 1, exact reason (needs-you: 1, asked: 0, no answered review-* question files for attempt 1, unanswered: review-01.json)",
            rc == 1 and "review rulings incomplete (needs-you: 1, asked: 0, no answered review-* question files for attempt 1, unanswered: review-01.json)" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
