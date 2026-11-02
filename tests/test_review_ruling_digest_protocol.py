#!/usr/bin/env python3
"""
Spec-blind tests for scripts/review-ruling.py and digest module integration from plan #418.

Plan items covered:
- review-ruling.py loads digest module untyped: Protocol for the four helpers, cast, one pinning test
  This test verifies that review-ruling.py can properly consume output from the digest module's helpers
  (parse_review function) and that the returned data structures have well-defined types.

Run with: python3 tests/test_review_ruling_digest_protocol.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

RULING = REPO_ROOT / "scripts" / "review-ruling.py"
PRECEDENT_DIGEST = REPO_ROOT / "scripts" / "precedent-digest.py"

PLAN_TEXT = """# Expert review action plan

## Needs you

### 1. Cache invalidation strategy
- **Where**: scripts/cache.py:10 · **Severity**: HIGH · **Raised by**: Uncle Bob
- **Why this is yours**: tripped test 3
- **The finding**: The cache is never invalidated after writes, so reads can be stale.
- **Options**:
  - **A — Invalidate on write** (recommended): drop the key on every write · Pro: simple · Con: extra I/O
  - **B — Use a TTL**: expire entries after 60 s · Pro: bounded staleness · Con: still stale in window
  - **C — Leave as-is**: do nothing · Pro: no churn · Con: stale reads persist
- **Recommendation**: A, because writes are rare.
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

### 2. Module naming
- **Where**: scripts/x.py:1 · **Severity**: LOW · **Raised by**: Nick
- **Why this is yours**: tripped test 3
- **The finding**: The name suggests a different responsibility.
- **Options**:
  - **A — Rename the module — now**: rename it everywhere · Pro: clarity · Con: churn
  - **B — Leave as-is**: do nothing
- **Recommendation**: B
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

---

## Needs measurement: 1

### 1. Latency regression
- **Where**: scripts/y.py:5 · **Severity**: MEDIUM · **Raised by**: Dana
- **Why this needs measurement**: cannot be judged from the report alone
- **The finding**: The change may slow the hot path.
- **Command**: `python3 scripts/bench.py --n 1000`
- **Resolves via**: if p95 drops, keep the change; if not, revert it.
- **STATUS**: pending-measurement
- **DECISION**: _(empty until STATUS changes — Claude records the reported result here once you tell it what the command above returned; never hand-edited)_

---
"""


def load_parse_review():
    """Load and return the parse_review function from precedent-digest.py."""
    spec = importlib.util.spec_from_file_location("precedent_digest_test", PRECEDENT_DIGEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_review


class Sandbox:
    """A temp HOME with a reviews root and one review dir holding claude-action-plan.md."""

    def __init__(self, plan_text=PLAN_TEXT):
        self.tmp = Path(tempfile.mkdtemp(prefix="digest-protocol-test-"))
        self.home = self.tmp / "home"
        self.root = self.home / ".claude" / "reviews"
        self.review = self.root / "acme" / "feature-1-abc"
        self.review.mkdir(parents=True)
        self.plan = self.review / "claude-action-plan.md"
        self.plan.write_text(plan_text)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decide(self, item, option, reasoning=None):
        env = {**os.environ, "HOME": str(self.home)}
        env.pop("CLAUDE_CODE_SESSION_ID", None)

        args = [
            sys.executable, str(RULING),
            "decide",
            "--review-dir", str(self.review),
            "--item", str(item),
            "--option", option,
        ]
        if reasoning:
            args += ["--reasoning", reasoning]

        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout, r.stderr


def main():
    h = Harness("REVIEW-RULING DIGEST PROTOCOL INTEGRATION TEST")

    print("digest module parse_review protocol: structure and types")

    # Load the parse_review function
    try:
        parse_review = load_parse_review()
    except Exception as e:
        h.test_result(
            "precedent-digest.py loads successfully",
            False,
            f"Failed to load parse_review: {e}"
        )
        h.summarize_and_exit()

    h.test_result(
        "parse_review function is callable",
        callable(parse_review),
        "parse_review is not callable"
    )

    print("\nparse_review return structure after decide")

    sb = Sandbox()
    try:
        # Make a decision
        option_label = "A — Invalidate on write"
        rc, _, err = sb.decide(1, option_label, reasoning="Simple and fast")
        h.test_result(
            "decide item 1 exits 0",
            rc == 0,
            f"rc={rc} stderr={err[:200]!r}"
        )

        # Parse the result
        plan_text = sb.plan.read_text()
        records, counts = parse_review(plan_text)

        h.test_result(
            "parse_review returns a tuple of (records, counts)",
            isinstance(records, (list, tuple)) and isinstance(counts, dict),
            f"type(records)={type(records)} type(counts)={type(counts)}"
        )

        # Each record should have expected keys
        h.test_result(
            "parse_review returns at least one record after decide",
            len(records) >= 1,
            f"records={records}"
        )

        if records:
            record = records[0]
            h.test_result(
                "record has 'answer' key",
                "answer" in record,
                f"record keys: {list(record.keys())}"
            )

            h.test_result(
                "record has 'options' key",
                "options" in record,
                f"record keys: {list(record.keys())}"
            )

            h.test_result(
                "'answer' value is a string",
                isinstance(record.get("answer"), str),
                f"answer type: {type(record.get('answer'))}"
            )

            h.test_result(
                "'options' value is a list or tuple",
                isinstance(record.get("options"), (list, tuple)),
                f"options type: {type(record.get('options'))}"
            )

            # The answer should be the canonical form of the selected option
            h.test_result(
                "answer matches the selected option (canonical form)",
                record.get("answer") == "A — Invalidate on write",
                f"answer: {record.get('answer')!r}"
            )

        # Check counts structure - counts dict exists and has expected structure
        h.test_result(
            "counts is a dict",
            isinstance(counts, dict),
            f"counts type: {type(counts)}"
        )

        h.test_result(
            "counts has unanswered and malformed keys",
            "unanswered" in counts and "malformed" in counts,
            f"counts keys: {list(counts.keys())}"
        )

    finally:
        sb.close()

    print("\nparse_review with multiple items")

    sb = Sandbox()
    try:
        # Decide both items
        rc1, _, _ = sb.decide(1, "A — Invalidate on write")
        rc2, _, _ = sb.decide(2, "B — Leave as-is")

        h.test_result(
            "both decisions exit 0",
            rc1 == 0 and rc2 == 0,
            f"rc1={rc1} rc2={rc2}"
        )

        # Parse the result
        plan_text = sb.plan.read_text()
        records, counts = parse_review(plan_text)

        h.test_result(
            "parse_review returns records for all decided items",
            len(records) == 2,
            f"record count: {len(records)}"
        )

        if len(records) == 2:
            answers = [r.get("answer") for r in records]
            h.test_result(
                "both answers are canonical form",
                "A — Invalidate on write" in answers and "B — Leave as-is" in answers,
                f"answers: {answers}"
            )

        h.test_result(
            "counts reflects unanswered and malformed counts",
            "unanswered" in counts and "malformed" in counts,
            f"counts: {counts}"
        )

    finally:
        sb.close()

    print("\nparse_review with measurement")

    sb = Sandbox()
    try:
        # Make a measurement decision (using measure command via review-ruling)
        env = {**os.environ, "HOME": str(sb.home)}
        env.pop("CLAUDE_CODE_SESSION_ID", None)

        args = [
            sys.executable, str(RULING),
            "measure",
            "--review-dir", str(sb.review),
            "--item", "1",
            "--result", "p95 dropped 30%",
        ]
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)

        h.test_result(
            "measure command exits 0",
            r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr[:200]!r}"
        )

        # The measurement is on a different item (Needs measurement, not Needs you)
        # So parse_review should not return it (it only reads Needs you and Needs measurement sections)
        plan_text = sb.plan.read_text()
        records, counts = parse_review(plan_text)

        # This is a Needs measurement item, so depending on parse_review's scope,
        # it may or may not be included. We just verify the function handles it without crashing.
        h.test_result(
            "parse_review completes without error on measured items",
            isinstance(records, (list, tuple)) and isinstance(counts, dict),
            "parse_review failed on measured items"
        )

    finally:
        sb.close()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
