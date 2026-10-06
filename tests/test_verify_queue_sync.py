#!/usr/bin/env python3
"""
Test suite for the scripted verify-queue sync (scripts/workflow/verify_queue.py).

- parse_plan() extracts only pending-* items, maps STATUS to kind, takes Command for measurements
- sync() appends rows, is idempotent, preserves done/ignored rows, tolerates legacy/garbage rows
- sync(branch=...) scans only that branch's reviews
- the CLI wiring exists and commands/cleanup.md calls it instead of "assume" prose

Run with: python3 tests/test_verify_queue_sync.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import verify_queue as vq
from _test_harness import REPO_ROOT, Harness

PLAN = """# Plan

## Needs measurement

### 1. Does the registry expose `contract`?
- **Where**: a.ts
- **Command**: `curl -s example | jq .x`
- **STATUS**: pending-measurement
- **DECISION**: _(empty)_

### 2. Pick a naming scheme
- **STATUS**: pending-decision
- **DECISION**: _(empty)_

### 3. Already handled
- **STATUS**: measured
- **DECISION**: ok

## Doing it

### 4. Not a ruling
- no status here
"""

BRANCH_REVIEW = "feature-22-thing-2dac30a-20261002T201802-02809"
OTHER_REVIEW = "feature-23-other-aaaaaaa-20261002T201802-11111"

if __name__ == "__main__":
    h = Harness("VERIFY-QUEUE SYNC TEST SUITE")
    t = h.test_result

    rulings = vq.parse_plan(PLAN, "rev", "/p/claude-action-plan.md")
    t("only pending-* items are extracted", [r.kind for r in rulings] == ["measurement", "your-call"])
    t("heading number stripped from summary", rulings[0].summary == "Does the registry expose `contract`?")
    t("measurement carries its command, backticks stripped", rulings[0].command == "curl -s example | jq .x")
    t("your-call has empty command", rulings[1].command == "")
    t("slugs are kebab-case", rulings[1].slug == "pick-a-naming-scheme")
    dup = vq.parse_plan("### 1. Same\n- **STATUS**: pending-decision\n### 2. Same\n- **STATUS**: pending-decision\n", "r", "/p")
    t("duplicate titles get distinct slugs", len({r.slug for r in dup}) == 2)

    t("branch match is exact on slug",
      vq.review_matches_branch(BRANCH_REVIEW, "feature/22-thing")
      and not vq.review_matches_branch(BRANCH_REVIEW, "feature/22")
      and not vq.review_matches_branch(OTHER_REVIEW, "feature/22-thing"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rid in (BRANCH_REVIEW, OTHER_REVIEW):
            d = root / "reviews" / "owner-repo" / rid
            d.mkdir(parents=True)
            (d / vq.PLAN_FILENAME).write_text(PLAN)
        queue = root / "wt" / vq.QUEUE_FILENAME
        queue.parent.mkdir()
        queue.write_text('{"repo":"x","pr":1,"status":"pending","ruling":"legacy row"}\nnot json\n')

        kw = dict(reviews_root=root / "reviews", queue=queue, repo_key="owner-repo")
        r1 = vq.sync(branch="feature/22-thing", **kw)  # type: ignore[arg-type]
        t("branch sync scans only that branch", r1.scanned_plans == 1 and len(r1.added) == 2)
        r2 = vq.sync(branch="feature/22-thing", **kw)  # type: ignore[arg-type]
        t("re-sync is idempotent", r2.added == [] and len(r2.already_queued) == 2)
        lines = queue.read_text().splitlines()
        t("legacy and garbage rows preserved", lines[0].startswith('{"repo"') and lines[1] == "not json")

        rid = r1.added[0]
        rows = [json.loads(x) for x in lines[2:]]
        rows[0]["status"] = "ignored"
        queue.write_text("\n".join(lines[:2] + [json.dumps(x) for x in rows]) + "\n")
        r3 = vq.sync(branch="feature/22-thing", **kw)  # type: ignore[arg-type]
        t("ignored rows never resurface", rid not in r3.added and r3.open_total == 1)

        r4 = vq.sync(**kw)  # type: ignore[arg-type]
        t("no branch filter scans every review", r4.scanned_plans == 2 and len(r4.added) == 2)

    cli = (REPO_ROOT / "scripts/workflow/cli.py").read_text()
    doc = (REPO_ROOT / "commands/cleanup.md").read_text()
    t("CLI exposes verify-queue sync", '"verify-queue"' in cli and "verify_queue.sync" in cli)
    t("cleanup.md runs the scripted sync", "cli verify-queue sync" in doc)
    t("cleanup.md no longer relies on 'assume' prose", "For this doc, assume" not in doc)
    t("cleanup.md surfaces sync failure", "verify-queue sync FAILED" in doc)

    h.summarize_and_exit()
