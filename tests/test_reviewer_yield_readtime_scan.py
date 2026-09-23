#!/usr/bin/env python3
"""
Regression tests for reviewer-yield.py's read-time backstop scan and exact-filename
attribution (issue #215, Testing Strategy (a)/(b) and decision 4).

Run with: python3 tests/test_reviewer_yield_readtime_scan.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

_spec = importlib.util.spec_from_file_location("reviewer_yield", REPO_ROOT / "scripts" / "reviewer-yield.py")
reviewer_yield = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reviewer_yield)

RUN_NAME = "feat-abc1234-20260922T094212-08870"


def _transcript(path: Path, review_dir: Path, written: str, msg_id: str) -> None:
    entry = {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(review_dir / written)}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry) + "\n")


def _setup(tmp: Path, origin):
    review_dir = tmp / ".claude" / "reviews" / RUN_NAME
    review_dir.mkdir(parents=True)
    if origin is not None:
        (review_dir / "transcript-origin.json").write_text(json.dumps(origin))
    subagents = tmp / ".claude" / "projects" / "proj" / "real-sess" / "subagents"
    _transcript(subagents / "a.jsonl", review_dir, "uncle-bob-pass1.md", "m1")
    _transcript(subagents / "b.jsonl", review_dir, "uncle-bob-pass2.md", "m2")
    _transcript(subagents / "c.jsonl", review_dir, "uncle-bob-questions-answered.md", "m3")
    return review_dir, subagents


def _find(tmp: Path, review_dir: Path):
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp)
    try:
        return reviewer_yield.find_subagent_files_by_reviewer(str(review_dir), ["uncle-bob"])
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def main():
    h = Harness("REVIEWER-YIELD READ-TIME SCAN TEST SUITE")
    t = h.test_result

    print("[unavailable origin, valid project_dir -> scan recovers]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin = {"schema_version": 1, "cwd": "/x", "project_dir": "proj", "session_id": None,
                  "resolution": "unavailable", "recorded_at": "2026-09-22T09:42:12Z"}
        review_dir, _ = _setup(tmp, origin)
        before = (review_dir / "transcript-origin.json").read_bytes()
        res = _find(tmp, review_dir)
        t("pass1+pass2 attributed to uncle-bob", len(res.get("uncle-bob", [])) == 2)
        t("Q&A goes to overhead unit, not the reviewer",
          len(res.get("overhead:questions-answered", [])) == 1)
        t("origin file untouched", (review_dir / "transcript-origin.json").read_bytes() == before)

    print("[wrong recorded session -> scan still recovers]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin = {"schema_version": 1, "cwd": "/x", "project_dir": "proj", "session_id": "old-sess",
                  "resolution": "most-recent-dir", "recorded_at": "2026-09-22T09:42:12Z"}
        review_dir, _ = _setup(tmp, origin)
        (tmp / ".claude" / "projects" / "proj" / "old-sess" / "subagents").mkdir(parents=True)
        res = _find(tmp, review_dir)
        t("recovers via scan", len(res.get("uncle-bob", [])) == 2)

    print("[time bound excludes stale transcripts]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin = {"schema_version": 1, "cwd": "/x", "project_dir": "proj", "session_id": None,
                  "resolution": "unavailable", "recorded_at": "2026-09-22T09:42:12Z"}
        review_dir, sub = _setup(tmp, origin)
        old = time.time() - 30 * 86400
        for f in sub.glob("*.jsonl"):
            os.utime(f, (old, old))
        res = _find(tmp, review_dir)
        t("stale transcripts not attributed", not res.get("uncle-bob"))

    print("[missing origin, unrelated cwd -> clean empty result]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        review_dir, _ = _setup(tmp, None)
        res = _find(tmp, review_dir)
        t("no crash, nothing attributed", not res.get("uncle-bob"))

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
