#!/usr/bin/env python3
"""
Spec-blind tests for scripts/review-ruling.py --reasoning handling from plan #418.

Plan items covered:
- idempotent re-run drops different --reasoning: document reasoning excluded from comparison
  This test verifies that idempotent re-runs with different --reasoning still succeeds (exit 0)
  and leaves the file unchanged, because reasoning is excluded from the idempotency check.

Every case runs the real production script as a subprocess.

Run with: python3 tests/test_review_ruling_reasoning_excluded.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

RULING = REPO_ROOT / "scripts" / "review-ruling.py"

PLAN_TEXT = """# Expert review action plan

## Needs you

### 1. Stale cache reads
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

---
"""


class Sandbox:
    """A temp HOME with a reviews root and one review dir holding claude-action-plan.md."""

    def __init__(self, plan_text=PLAN_TEXT):
        self.tmp = Path(tempfile.mkdtemp(prefix="reasoning-test-"))
        self.home = self.tmp / "home"
        self.root = self.home / ".claude" / "reviews"
        self.review = self.root / "acme" / "feature-1-abc"
        self.review.mkdir(parents=True)
        self.plan = self.review / "claude-action-plan.md"
        self.plan.write_text(plan_text)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bytes(self):
        return self.plan.read_bytes()

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
    h = Harness("REVIEW-RULING REASONING EXCLUDED FROM IDEMPOTENCY TEST")

    print("idempotent re-run with different --reasoning")

    # First decide with one reasoning
    sb = Sandbox()
    try:
        option = "A — Invalidate on write"
        reasoning1 = "Simple and fast, no TTL overhead"

        rc1, out1, err1 = sb.decide(1, option, reasoning=reasoning1)
        h.test_result(
            "first decide with reasoning exits 0",
            rc1 == 0,
            f"rc={rc1} stderr={err1[:200]!r}"
        )

        # Record the file state after first decide
        after_first = sb.bytes()

        # Second decide with same option, different reasoning
        reasoning2 = "Writes are rare so invalidation is best option"
        rc2, out2, err2 = sb.decide(1, option, reasoning=reasoning2)

        h.test_result(
            "re-run with same option but different --reasoning exits 0",
            rc2 == 0,
            f"rc={rc2} stderr={err2[:200]!r}"
        )

        # The file should be unchanged because reasoning is excluded from the comparison
        h.test_result(
            "re-run with different reasoning leaves file byte-identical",
            sb.bytes() == after_first,
            f"file changed: {len(after_first)} -> {len(sb.bytes())} bytes"
        )

        # Third decide with same option, no reasoning (only the first apply)
        rc3, out3, err3 = sb.decide(1, option, reasoning=None)

        h.test_result(
            "re-run with same option, no reasoning exits 0",
            rc3 == 0,
            f"rc={rc3} stderr={err3[:200]!r}"
        )

        h.test_result(
            "re-run with no reasoning leaves file byte-identical",
            sb.bytes() == after_first,
            f"file changed: {len(after_first)} -> {len(sb.bytes())} bytes"
        )

    finally:
        sb.close()

    print("\nidempotency respects only option, not reasoning")

    sb = Sandbox()
    try:
        option1 = "A — Invalidate on write"
        option2 = "B — Use a TTL"

        # First decide
        rc1, _, err1 = sb.decide(1, option1, reasoning="Reasoning A")
        h.test_result("first decide exits 0", rc1 == 0, f"rc={rc1}")
        after_first = sb.bytes()

        # Try to decide with different option and reasoning
        # This should be a conflict (exit 3) because the option differs, not because reasoning differs
        rc2, _, err2 = sb.decide(1, option2, reasoning="Different reasoning B")
        h.test_result(
            "decide with different option exits 3 (conflict)",
            rc2 == 3,
            f"rc={rc2} stderr={err2[:200]!r}"
        )

        # File should be unchanged
        h.test_result(
            "conflicting option leaves file byte-identical",
            sb.bytes() == after_first,
            "file changed"
        )

    finally:
        sb.close()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
