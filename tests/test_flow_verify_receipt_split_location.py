#!/usr/bin/env python3
"""
Black-box tests for flow-verify-receipt.py split-location behavior (#318).

Tests verify that receipt marker paths (questions/plan-NN.json, questions/review-NN.json, etc.)
are resolved against FLOW_DIR, not the plan-session directory. Decoy files in the plan-session
directory must never satisfy a missing FLOW_DIR file.

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


def create_review_dir(home: str) -> str:
    """Create a review directory with required files. Returns the review directory path."""
    review_dir = Path(home) / ".claude" / "reviews" / "repo_key" / "12-review-2026"
    review_dir.mkdir(parents=True, exist_ok=True)

    # Create required files
    (review_dir / "claude-action-plan.md").write_text("# Action Plan\n\n## Doing it\n- item 1\n")
    (review_dir / "final-report.md").write_text("# Final Report\n")
    (review_dir / "findings.json").write_text('{"findings": []}')
    (review_dir / "ariadne-pass1.md").write_text("# Pass 1\n")
    (review_dir / "nigel-pass1.md").write_text("# Pass 1 #2\n")

    return str(review_dir)


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

        # Create real marker files in FLOW_DIR (the live location)
        questions_dir = flow_dir / "questions"
        questions_dir.mkdir()
        answers_dir = flow_dir / "answers"
        answers_dir.mkdir()

        for seq in range(1, 5):
            (questions_dir / f"plan-{seq:02d}.json").write_text(json.dumps({
                "step": "plan", "command": "expert-plan", "context_path": None,
                "summary": None, "questions": [{"id": f"q{seq}", "question": f"Q{seq}?"}]
            }))
            (answers_dir / f"plan-{seq:02d}.json").write_text(json.dumps({
                "step": "plan", "answers": {f"q{seq}": f"answer{seq}"}
            }))

        # Create decoy files in session dir (should NOT be used)
        (Path(session_dir) / "pending-questions").mkdir(parents=True, exist_ok=True)
        (Path(session_dir) / "pending-questions" / "plan-01.json").write_text('{}')

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

        # Create ONLY decoy files in session, NOT in FLOW_DIR
        (Path(session_dir) / "pending-questions").mkdir(parents=True, exist_ok=True)
        (Path(session_dir) / "pending-questions" / "plan-01.json").write_text(json.dumps({
            "step": "plan", "questions": [{"id": "q1", "question": "Q1?"}]
        }))
        (Path(session_dir) / "pending-answers").mkdir(parents=True, exist_ok=True)
        (Path(session_dir) / "pending-answers" / "plan-01.json").write_text(json.dumps({
            "answers": {"q1": "a1"}
        }))

        # Create receipt with markers pointing to FLOW_DIR locations
        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""FINAL_PLAN_PATH: {plan_path}
<!-- awaiting-answers: questions/plan-01.json -->
Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("plan", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "L2: No files in FLOW_DIR (only in session decoy) → exit 1, unanswered",
            rc == 1 and "unanswered: plan-01.json" in out,
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
            "E1: Missing session plan.md → exit 1, plan artifacts missing",
            rc == 1 and ("plan artifacts missing" in out or "plan.md" in out),
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test E2: Missing FINAL_PLAN_PATH file → plan artifacts missing")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261018T120000-11114"
        session_dir = create_plan_session(tmp_home, invocation_id, briefs=0)
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
            "E2: Missing FINAL_PLAN_PATH file → exit 1, FINAL_PLAN_PATH",
            rc == 1 and "FINAL_PLAN_PATH" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    # ============================================================================
    # REVIEW SPLIT-LOCATION TESTS
    # ============================================================================

    print("\n[REVIEW SPLIT-LOCATION TESTS]")
    print("Test RV1: Review split-location positive (questions in FLOW_DIR, decoy in session)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Create real marker files in FLOW_DIR
        questions_dir = flow_dir / "questions"
        questions_dir.mkdir()
        answers_dir = flow_dir / "answers"
        answers_dir.mkdir()

        (questions_dir / "review-01.json").write_text(json.dumps({
            "step": "review", "command": "expert-review", "kind": "report",
            "questions": [{"id": "q1", "question": "Q1?"}]
        }))
        (answers_dir / "review-01.json").write_text(json.dumps({
            "answers": {"q1": "answer1"}
        }))

        # Create decoy in review dir (should NOT be used)
        (Path(review_dir) / "pending-questions").mkdir(parents=True, exist_ok=True)
        (Path(review_dir) / "pending-questions" / "review-01.json").write_text('{}')

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""REVIEW_DIR: {review_dir}
<!-- awaiting-answers: questions/review-01.json -->
## Needs you
### 1. Pick a thing
- **Why**: a human call

Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV1: Review questions in FLOW_DIR with answers → exit 0, ARTIFACTS: ok",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    print("Test RV2: Review split-location negative (no FLOW_DIR files, only session decoy)")

    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        review_dir = create_review_dir(tmp_home)
        flow_dir = Path(tmp_home) / "flow"
        flow_dir.mkdir()

        # Create decoy files in review dir, NOT in FLOW_DIR
        (Path(review_dir) / "pending-questions").mkdir(parents=True, exist_ok=True)
        (Path(review_dir) / "pending-questions" / "review-01.json").write_text(json.dumps({
            "step": "review", "questions": [{"id": "q1", "question": "Q1?"}]
        }))

        receipt_path = Path(tmp_home) / "receipt.md"
        receipt_content = f"""REVIEW_DIR: {review_dir}
<!-- awaiting-answers: questions/review-01.json -->
## Needs you
### 1. Pick a thing

Decision: OK
<!-- step-end -->"""
        receipt_path.write_text(receipt_content)

        rc, out = run_verifier("review", str(receipt_path), str(flow_dir), tmp_home)
        h.test_result(
            "RV2: No files in FLOW_DIR (only in review dir decoy) → exit 1, unanswered",
            rc == 1 and "unanswered: review-01.json" in out,
            f"rc={rc}\nout={out[-500:]}"
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
