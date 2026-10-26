#!/usr/bin/env python3
"""
Characterization tests for flow-verify-receipt.py reason strings (#341, item 4).

These tests pin the EXACT reason strings the verifier emits from the branches that the
behaviour-neutral helper extraction touches (FINAL_PLAN_PATH / REVIEW_DIR lookup, the
under-root containment checks, the relay-shortfall suffix, the identity floor, and the
implement/fix commit checks). They were written against the pre-refactor code and must keep
passing unchanged after the refactor.

Run with: python3 tests/test_flow_verify_receipt_characterization.py
"""

import importlib.util
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness

script_path = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)

check_plan = flow_verify_module.check_plan
check_review = flow_verify_module.check_review
check_fix = flow_verify_module.check_fix
check_implement = flow_verify_module.check_implement

PLAN_NOT_UNDER = "plan artifacts missing (FINAL_PLAN_PATH not under plans/ or no invocation id)"
PLAN_FILE_MISSING = "plan artifacts missing (FINAL_PLAN_PATH file does not exist)"
REVIEW_NOT_UNDER = "review artifacts missing (REVIEW_DIR not under reviews/)"
PLAN_OLDER_ATTEMPT = "plan artifacts missing (session dir older than this attempt)"
REVIEW_OLDER_ATTEMPT = "review artifacts missing (action plan older than this attempt)"
IMPL_MISSING = "implement artifacts missing (no new commits on feat/x since step began)"
FIX_MISSING = "fix artifacts missing (no new commits on feat/x since step began)"

INVOCATION = "20261020T235259-30176"
ACTION_PLAN_ONE_NEEDS_YOU = (
    "# Action plan\n\n## Needs you\n\n### 1. A call\n\nbody\n\n## Doing it: 0\n"
)
ACTION_PLAN_ONE_DOING_IT = (
    "# Action plan\n\n## Doing it: 1\n\n| ID | Item |\n|---|---|\n| D1 | do it |\n"
)
DECISIONS_ONE_BRIEF = "# Decisions\n\n#### Q1 — brief\n\nbody\n"


def make_plan_fixture(tmp: Path, decisions: str = DECISIONS_ONE_BRIEF):
    """Build a fake HOME with a plan file and a matching session dir. Returns (home, plan, session)."""
    home = tmp / "home"
    plans = home / ".claude" / "plans"
    plans.mkdir(parents=True)
    plan = plans / f"slug-{INVOCATION}.md"
    plan.write_text("# plan\n", encoding="utf-8")
    session = home / ".claude" / "plan-sessions" / "proj" / f"slug-{INVOCATION}"
    session.mkdir(parents=True)
    (session / "context.md").write_text("context\n", encoding="utf-8")
    (session / "selected-experts.md").write_text(
        "| Expert | Role |\n|---|---|\n| Auditor | audit |\n", encoding="utf-8"
    )
    (session / "plan.md").write_text("plan\n", encoding="utf-8")
    (session / "decisions.md").write_text(decisions, encoding="utf-8")
    return home, plan, session


def make_review_fixture(tmp: Path, action_plan: str = ACTION_PLAN_ONE_NEEDS_YOU, checkpoints: int = 2):
    """Build a fake HOME with a review dir under reviews/. Returns (home, review_dir)."""
    home = tmp / "home"
    review_dir = home / ".claude" / "reviews" / "r1"
    review_dir.mkdir(parents=True)
    (review_dir / "claude-action-plan.md").write_text(action_plan, encoding="utf-8")
    (review_dir / "final-report.md").write_text("report\n", encoding="utf-8")
    (review_dir / "findings.json").write_text("{}\n", encoding="utf-8")
    for i in range(checkpoints):
        (review_dir / f"rev{i}-pass1.md").write_text("checkpoint\n", encoding="utf-8")
    return home, review_dir


def write_receipt(tmp: Path, text: str) -> Path:
    receipt = tmp / "receipt.md"
    receipt.write_text(text, encoding="utf-8")
    return receipt


def expect(h: Harness, name: str, got, want) -> None:
    h.test_result(name, got == want, f"got {got!r}, want {want!r}")


def main() -> None:
    h = Harness("Flow verify receipt characterization (pre-refactor reason strings)")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- plan: FINAL_PLAN_PATH lookup and containment -------------------------------------
        home, plan, _ = make_plan_fixture(tmp / "p1")
        receipt = write_receipt(tmp / "p1", "ATTEMPT: 1\n")
        expect(h, "plan: no FINAL_PLAN_PATH line gives not-under reason",
               check_plan(receipt, 0.0, home, None), PLAN_NOT_UNDER)

        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {tmp / 'p1' / 'outside.md'}\nATTEMPT: 1\n")
        expect(h, "plan: FINAL_PLAN_PATH outside plans/ gives not-under reason",
               check_plan(receipt, 0.0, home, None), PLAN_NOT_UNDER)

        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {plan}\nFINAL_PLAN_PATH: {tmp / 'p1' / 'outside.md'}\nATTEMPT: 1\n")
        expect(h, "plan: last FINAL_PLAN_PATH line wins (outside path rejected)",
               check_plan(receipt, 0.0, home, None), PLAN_NOT_UNDER)

        plan_no_id = home / ".claude" / "plans" / "no-id.md"
        plan_no_id.write_text("x\n", encoding="utf-8")
        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {plan_no_id}\nATTEMPT: 1\n")
        expect(h, "plan: plan file without invocation id in name",
               check_plan(receipt, 0.0, home, None), PLAN_NOT_UNDER)

        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {home / '.claude' / 'plans' / 'absent-20261020T235259-30176.md'}\nATTEMPT: 1\n")
        expect(h, "plan: FINAL_PLAN_PATH file does not exist",
               check_plan(receipt, 0.0, home, None), PLAN_FILE_MISSING)

        # Session dir missing for the invocation id
        (home / ".claude" / "plan-sessions" / "proj" / f"slug-{INVOCATION}").rename(tmp / "p1" / "moved")
        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {plan}\nATTEMPT: 1\n")
        expect(h, "plan: no session dir gives match-count reason",
               check_plan(receipt, 0.0, home, None),
               f"plan artifacts missing (session dir: 0 matches for {INVOCATION})")
        (tmp / "p1" / "moved").rename(home / ".claude" / "plan-sessions" / "proj" / f"slug-{INVOCATION}")

        # --- plan: identity floor -------------------------------------------------------------
        receipt = write_receipt(tmp / "p1", f"FINAL_PLAN_PATH: {plan}\nATTEMPT: 1\n")
        expect(h, "plan: session dir older than the attempt floor (no flow dir)",
               check_plan(receipt, time.time() + 1000, home, None), PLAN_OLDER_ATTEMPT)

        # --- plan: legacy relay shortfall, both suffix branches -------------------------------
        home2, plan2, _ = make_plan_fixture(tmp / "p2")
        receipt = write_receipt(tmp / "p2", f"FINAL_PLAN_PATH: {plan2}\nATTEMPT: 1\n")
        expect(h, "plan: brief not asked, flow dir unresolved suffix",
               check_plan(receipt, 0.0, home2, None),
               "plan checkpoint incomplete (briefs: 1, asked: 0, flow dir unresolved)")

        flow_dir = tmp / "p2" / "flow"
        (flow_dir / "questions").mkdir(parents=True)
        expect(h, "plan: brief not asked, no answered files suffix",
               check_plan(receipt, 0.0, home2, flow_dir),
               "plan checkpoint incomplete (briefs: 1, asked: 0, no answered plan-* question files for attempt 1)")

        # --- review: REVIEW_DIR lookup and containment ----------------------------------------
        home3, review_dir = make_review_fixture(tmp / "r1")
        receipt = write_receipt(tmp / "r1", "ATTEMPT: 1\n")
        expect(h, "review: no REVIEW_DIR line gives not-under reason",
               check_review(receipt, 0.0, home3, None), REVIEW_NOT_UNDER)

        outside = tmp / "r1" / "elsewhere"
        outside.mkdir()
        receipt = write_receipt(tmp / "r1", f"REVIEW_DIR: {outside}\nATTEMPT: 1\n")
        expect(h, "review: REVIEW_DIR outside reviews/ gives not-under reason",
               check_review(receipt, 0.0, home3, None), REVIEW_NOT_UNDER)

        receipt = write_receipt(tmp / "r1", f"REVIEW_DIR: {review_dir}\nREVIEW_DIR: {outside}\nATTEMPT: 1\n")
        expect(h, "review: last REVIEW_DIR line wins (outside path rejected)",
               check_review(receipt, 0.0, home3, None), REVIEW_NOT_UNDER)

        # Required files, in order
        (review_dir / "final-report.md").unlink()
        receipt = write_receipt(tmp / "r1", f"REVIEW_DIR: {review_dir}\nATTEMPT: 1\n")
        expect(h, "review: missing final-report.md",
               check_review(receipt, 0.0, home3, None), "review artifacts missing (final-report.md)")
        (review_dir / "final-report.md").write_text("report\n", encoding="utf-8")

        # Floor
        expect(h, "review: action plan older than the attempt floor",
               check_review(receipt, time.time() + 1000, home3, None), REVIEW_OLDER_ATTEMPT)

        # Reviewer checkpoint count
        home4, review_dir4 = make_review_fixture(tmp / "r2", checkpoints=1)
        receipt = write_receipt(tmp / "r2", f"REVIEW_DIR: {review_dir4}\nATTEMPT: 1\n")
        expect(h, "review: fewer than two reviewer checkpoints",
               check_review(receipt, 0.0, home4, None),
               "review artifacts missing (reviewer checkpoints: 1)")

        # --- review: rulings shortfall, both suffix branches ----------------------------------
        home5, review_dir5 = make_review_fixture(tmp / "r3")
        receipt = write_receipt(tmp / "r3", f"REVIEW_DIR: {review_dir5}\nATTEMPT: 1\n")
        expect(h, "review: needs-you not asked, flow dir unresolved suffix",
               check_review(receipt, 0.0, home5, None),
               "review rulings incomplete (needs-you: 1, asked: 0, flow dir unresolved)")

        flow_dir5 = tmp / "r3" / "flow"
        (flow_dir5 / "questions").mkdir(parents=True)
        expect(h, "review: needs-you not asked, no answered files suffix",
               check_review(receipt, 0.0, home5, flow_dir5),
               "review rulings incomplete (needs-you: 1, asked: 0, no answered review-* question files for attempt 1)")

        # --- implement and fix: commit checks -------------------------------------------------
        original_count = flow_verify_module._count_commits
        try:
            flow_verify_module._count_commits = lambda repo, base, branch: 0
            expect(h, "implement: no new commits",
                   check_implement(tmp / "r3" / "receipt.md", "/repo", "feat/x", "main"), IMPL_MISSING)

            home6, review_dir6 = make_review_fixture(tmp / "f1", action_plan=ACTION_PLAN_ONE_DOING_IT)
            receipt6 = write_receipt(tmp / "f1", "ATTEMPT: 1\n")

            outside6 = tmp / "f1" / "elsewhere"
            outside6.mkdir()
            (outside6 / "claude-action-plan.md").write_text(ACTION_PLAN_ONE_DOING_IT, encoding="utf-8")
            expect(h, "fix: zero commits, review dir outside reviews/",
                   check_fix(receipt6, "/repo", "feat/x", "main", str(outside6), home6), FIX_MISSING)

            expect(h, "fix: zero commits, action plan missing under reviews/",
                   check_fix(receipt6, "/repo", "feat/x", "main", str(tmp / "f1" / "absent"), home6), FIX_MISSING)

            expect(h, "fix: zero commits, doing-it items present",
                   check_fix(receipt6, "/repo", "feat/x", "main", str(review_dir6), home6), FIX_MISSING)

            (review_dir6 / "claude-action-plan.md").write_text(ACTION_PLAN_ONE_NEEDS_YOU, encoding="utf-8")
            expect(h, "fix: zero commits, zero doing-it items passes",
                   check_fix(receipt6, "/repo", "feat/x", "main", str(review_dir6), home6), None)
        finally:
            flow_verify_module._count_commits = original_count

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
