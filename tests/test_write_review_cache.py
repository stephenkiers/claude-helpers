#!/usr/bin/env python3
"""
Test suite for write-review-cache.py.

Run with: python3 tests/test_write_review_cache.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness

SCRIPT = Path(__file__).parent.parent / "scripts" / "write-review-cache.py"

REQUIRED_ARGS = [
    "--last-run", "2026-01-01T00:00:00Z",
    "--commit", "abc1234",
    "--branch", "feature/162-foo",
    "--review-dir", "/tmp/reviewdir",
    "--reviewers", "code-rot-cody,consistency-checker",
    "--findings-critical", "0",
    "--findings-high", "1",
    "--findings-medium", "2",
    "--findings-low", "3",
]


def run(cache_path: Path, extra_args=None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), "--cache-path", str(cache_path)] + REQUIRED_ARGS
    if extra_args:
        args += extra_args
    return subprocess.run(args, capture_output=True, text=True)


if __name__ == "__main__":
    h = Harness("WRITE-REVIEW-CACHE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Fresh cache file — writes full schema")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"
        result = run(cache_path)
        test_result("Exits 0 on a fresh cache path", result.returncode == 0)

        data = json.loads(cache_path.read_text())
        review = data.get("review", {})
        test_result("Writes 'branch'", review.get("branch") == "feature/162-foo")
        test_result("Writes 'commit'", review.get("commit") == "abc1234")
        test_result("Writes 'lastRun'", review.get("lastRun") == "2026-01-01T00:00:00Z")
        test_result("Writes 'reviewDir'", review.get("reviewDir") == "/tmp/reviewdir")
        test_result(
            "Splits 'reviewers' on comma",
            review.get("reviewers") == ["code-rot-cody", "consistency-checker"],
        )
        test_result(
            "Writes 'findings' with all four severities",
            review.get("findings") == {"critical": 0, "high": 1, "medium": 2, "low": 3},
        )
        test_result("Does not invent undocumented keys", set(review.keys()) <= {
            "lastRun", "commit", "branch", "reviewDir", "reviewers", "findings",
            "panelModel", "metricsPath",
        })

    print()
    print("[Section 2] Preserves existing top-level sections")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"
        cache_path.write_text(json.dumps({
            "branch": "main",
            "issue": {"number": 162, "url": "https://example.com/162"},
        }))
        result = run(cache_path)
        test_result("Exits 0 with pre-existing sections", result.returncode == 0)

        data = json.loads(cache_path.read_text())
        test_result("Preserves top-level 'branch'", data.get("branch") == "main")
        test_result("Preserves 'issue' section", data.get("issue", {}).get("number") == 162)
        test_result("Adds 'review' section alongside", "review" in data)

    print()
    print("[Section 3] Overwrites a stale 'review' section on re-run")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"
        cache_path.write_text(json.dumps({
            "review": {"commit": "stale000", "branch": "feature/162-foo", "reviewDir": "/tmp/old"},
        }))
        result = run(cache_path)
        test_result("Exits 0 overwriting a stale review", result.returncode == 0)
        data = json.loads(cache_path.read_text())
        test_result("New commit replaces stale commit", data["review"]["commit"] == "abc1234")
        test_result("Old reviewDir is gone", data["review"]["reviewDir"] == "/tmp/reviewdir")

    print()
    print("[Section 4] Optional fields")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"
        result = run(cache_path, ["--panel-model", "sonnet", "--metrics-path", "/tmp/metrics.json"])
        test_result("Exits 0 with optional fields", result.returncode == 0)
        data = json.loads(cache_path.read_text())
        test_result("Writes optional 'panelModel'", data["review"].get("panelModel") == "sonnet")
        test_result("Writes optional 'metricsPath'", data["review"].get("metricsPath") == "/tmp/metrics.json")

        result2 = run(cache_path)
        data2 = json.loads(cache_path.read_text())
        test_result(
            "Omitting optional fields on a later run drops them (no stale carry-over)",
            "panelModel" not in data2["review"] and "metricsPath" not in data2["review"],
        )

    print()
    print("[Section 5] Fails loudly on missing/empty required fields")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"

        # argparse-level: required arg omitted entirely
        args = [sys.executable, str(SCRIPT), "--cache-path", str(cache_path)] + REQUIRED_ARGS[2:]
        result = subprocess.run(args, capture_output=True, text=True)
        test_result("Missing required --last-run exits non-zero", result.returncode != 0)
        test_result("Cache file untouched when a required arg is missing", not cache_path.exists())

        # value-level: required arg present but empty string
        result2 = run(cache_path, ["--branch", ""])
        test_result("Empty --branch value exits non-zero", result2.returncode != 0)
        test_result("Empty --branch value reports which field", "--branch" in result2.stderr)
        test_result("Cache file untouched when a required value is empty", not cache_path.exists())

    print()
    print("[Section 6] Malformed existing cache is not silently clobbered")
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "github-cache.json"
        cache_path.write_text("{not valid json")
        result = run(cache_path)
        test_result("Exits non-zero on unparseable existing cache", result.returncode != 0)
        test_result(
            "Leaves the malformed file exactly as it was (no truncation)",
            cache_path.read_text() == "{not valid json",
        )

    print()
    h.summarize_and_exit()
