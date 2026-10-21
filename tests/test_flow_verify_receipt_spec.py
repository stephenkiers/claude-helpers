#!/usr/bin/env python3
"""
Black-box spec tests for scripts/flow-verify-receipt.py (#303 round 2 review).

Written from the action plan's behaviour list, not from the implementation. The verifier is driven
only through its CLI (subprocess). Every fixture lives in a throwaway temp dir:

- HOME is pointed at the temp dir, because the verifier resolves REVIEW_DIR under $HOME/.claude/reviews
  and FINAL_PLAN_PATH under $HOME/.claude/plans. Nothing under the real ~/.claude is read or written.
- The review step is the exercised step. A plan-step receipt needs a populated plan session dir
  ($HOME/.claude/plan-sessions/<repo>/<slug>-<invocation-id>/ with selected-experts.md contributions),
  which this suite does not build; the plan path is therefore not covered here.

Covers (plan item in parentheses):
1. F3: "answered" is existence-only: a regular answers file counts, empty or non-JSON content
   included; a directory at the answers path does not count (F3).
2. F4: any unanswered marker fails: single unanswered, over-asked with one unanswered, stale marker
   whose question file is gone, and a Doing-it-only plan with an unanswered marker (F4).
3. F8: ", flow dir unresolved" is appended when no flow dir resolves and markers exist; not appended
   when there are no markers (F8).
4. F9: the zero-marker reason names this step's markers (review-*), not markers in general (F9).
5. F16: a traversal marker `questions/../../evil.json` never counts as answered, never reads the
   escape target, and does not mask a real unanswered marker (F16).

Run with: python3 tests/test_flow_verify_receipt_spec.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"

QUESTION = {
    "step": "review",
    "command": "expert-review",
    "context_path": None,
    "summary": "summary",
    "attempt": 1,
    "questions": [
        {
            "id": "q1",
            "brief": "brief",
            "question": "Which way?",
            "header": "Way",
            "options": [{"label": "A", "description": "first"}],
            "multiSelect": False,
        }
    ],
}

ANSWER = {"answers": {"q1": "A"}}

NEEDS_YOU_PLAN = (
    "# Action Plan\n\n"
    "## Needs you\n\n"
    "### 1. Pick a thing\n"
    "- **Why**: a human call\n\n"
    "## Doing it: 0\n"
)

DOING_ONLY_PLAN = "# Action Plan\n\n## Doing it: 0\n"


def since_timestamp():
    """ISO-8601 UTC an hour ago, so every fixture file is fresher than --since."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fixture:
    """A temp tree: HOME/.claude/reviews/rv (REVIEW_DIR), flow/ (FLOW_DIR), escape target beside flow."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.review_dir = self.home / ".claude" / "reviews" / "rv"
        self.flow = self.root / "flow"
        self.receipt = self.flow / "steps" / "04-review.md"
        self.review_dir.mkdir(parents=True)
        for sub in ("steps", "questions", "answers"):
            (self.flow / sub).mkdir(parents=True)
        self.set_action_plan(NEEDS_YOU_PLAN)
        for name in ("full-diff.patch", "diff-index.md", "final-report.md", "findings.json"):
            (self.review_dir / name).write_text("x\n", encoding="utf-8")
        for reviewer in ("ariadne", "nigel", "contrarian-carl"):
            (self.review_dir / f"{reviewer}-pass1.md").write_text(
                f"{reviewer} review\n<!-- pass1-end -->\n", encoding="utf-8")

    def set_action_plan(self, text):
        (self.review_dir / "claude-action-plan.md").write_text(text, encoding="utf-8")

    def write_question(self, name, payload=QUESTION):
        (self.flow / "questions" / name).write_text(json.dumps(payload), encoding="utf-8")

    def write_answer(self, name, payload=ANSWER):
        (self.flow / "answers" / name).write_text(json.dumps(payload), encoding="utf-8")

    def write_receipt(self, marker_lines, path=None):
        """A review receipt whose body carries the given marker lines, with a valid OK trailer."""
        body = [
            "Review summary",
            f"REVIEW_DIR: {self.review_dir}",
            "CONFIRMED: critical=0 high=0 medium=0 low=0",
        ]
        body += list(marker_lines)
        body += ["ATTEMPT: 1", "Decision: OK", "Reason: completed", "<!-- step-end -->"]
        target = Path(path) if path else self.receipt
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(body) + "\n", encoding="utf-8")
        return target


def marker(name):
    return f"<!-- awaiting-answers: questions/{name} -->"


def run_verifier(fx, receipt=None, flow_dir=None):
    """Run the verifier CLI with HOME pinned to the fixture. Returns (exit code, combined output)."""
    args = [
        sys.executable,
        str(VERIFIER),
        "--step", "review",
        "--receipt", str(receipt or fx.receipt),
        "--since", since_timestamp(),
    ]
    if flow_dir is not None:
        args += ["--flow-dir", str(flow_dir)]
    env = {**os.environ, "HOME": str(fx.home)}
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


def main():
    h = Harness("flow-verify-receipt.py: spec behaviours (#303 round 2)")

    # --- F3: "answered" is existence-only (is_file) -------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        fx.write_answer("review-01.json")
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F3 happy: marker with a valid question and answers file passes (exit 0)",
                      rc == 0, f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        (fx.flow / "answers" / "review-01.json").write_text("", encoding="utf-8")
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F3: an empty regular answers file counts as answered (existence-only, exit 0)",
                      rc == 0, f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        (fx.flow / "answers" / "review-01.json").write_text("not json at all\n", encoding="utf-8")
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F3: a non-JSON answers file still counts as answered (existence-only, exit 0)",
                      rc == 0, f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        (fx.flow / "answers" / "review-01.json").mkdir()
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F3: a directory at the answers path is not an answer (exit 1, unanswered)",
                      rc == 1 and "unanswered: review-01.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F3 negative: no answers file means unanswered (exit 1)",
                      rc == 1 and "unanswered: review-01.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- F4: any unanswered marker fails ----------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.set_action_plan(DOING_ONLY_PLAN)
        fx.write_question("review-01.json")
        fx.write_receipt([marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F4: an unanswered marker fails even when the plan has no Needs-you items (exit 1)",
                      rc == 1 and "unanswered: review-01.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        fx.write_question("review-02.json")
        fx.write_answer("review-01.json")
        fx.write_receipt([marker("review-01.json"), marker("review-02.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F4 over-asked: one answered and one unanswered marker fails, naming the unanswered one",
                      rc == 1 and "unanswered: review-02.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_answer("review-01.json")
        fx.write_question("review-01.json")
        fx.write_receipt([marker("review-01.json"), marker("review-02.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F4 stale marker: a marker whose question file is gone and has no answer fails (exit 1)",
                      rc == 1 and "unanswered: review-02.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- F8: ", flow dir unresolved" when no flow dir resolves and markers exist --------------
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_receipt([marker("review-01.json")], path=fx.root / "outside" / "04-review.md")
        rc, out = run_verifier(fx, receipt=fx.root / "outside" / "04-review.md")
        h.test_result("F8: no resolvable flow dir with markers present adds ', flow dir unresolved' (exit 1)",
                      rc == 1 and ", flow dir unresolved" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_receipt([], path=fx.root / "outside" / "04-review.md")
        rc, out = run_verifier(fx, receipt=fx.root / "outside" / "04-review.md")
        h.test_result("F8 negative: no flow dir and no markers still adds flow dir unresolved when items exist",
                      "flow dir unresolved" in out and "asked: 0" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- F9: zero-marker reason names this step's markers -----------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_receipt([])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F9: zero markers with Needs-you items names this step's review-* markers (exit 1)",
                      rc == 1 and "review-*" in out and "asked: 0" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("plan-01.json")
        fx.write_answer("plan-01.json")
        fx.write_receipt([marker("plan-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F9: a plan-step marker on a review receipt does not satisfy the review-* count (exit 1)",
                      rc == 1 and "review-*" in out and "asked: 0" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    # --- F16: traversal marker never counts as answered and never escapes the flow dir ------
    escape_name = "questions/../../evil.json"
    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        (fx.root / "evil.json").write_text("{}\n", encoding="utf-8")
        fx.write_receipt([marker(escape_name)])
        rc_present, out_present = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F16: a traversal marker with an escape target present does not count as answered (exit 1)",
                      rc_present == 1 and "asked: 0" in out_present,
                      f"rc={rc_present} out={out_present.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_receipt([marker(escape_name)])
        rc_absent, out_absent = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F16: traversal marker verdict does not depend on the escape target existing",
                      rc_absent == 1 and "asked: 0" in out_absent,
                      f"rc={rc_absent} out={out_absent.strip()[-300:]}")

    with tempfile.TemporaryDirectory() as tmp:
        fx = Fixture(tmp)
        fx.write_question("review-01.json")
        fx.write_receipt([marker(escape_name), marker("review-01.json")])
        rc, out = run_verifier(fx, flow_dir=fx.flow)
        h.test_result("F16: a traversal marker does not mask a real unanswered marker (exit 1, names review-01)",
                      rc == 1 and "unanswered: review-01.json" in out,
                      f"rc={rc} out={out.strip()[-300:]}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
