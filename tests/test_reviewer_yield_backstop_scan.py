#!/usr/bin/env python3
"""
Test suite for reviewer-yield.py backstop scan and pod format handling
(Testing Strategy cases a, b, c).

Covers observable behavior:
- Pod runs excluded from reviewers_per_run
- Pod/unknown runs show explicit notices via CLI
- Origin file remains unchanged after scan (observation-only)
- The backstop scan mechanism exists and is called

Run with: python3 tests/test_reviewer_yield_backstop_scan.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT_PATH = REPO_ROOT / "scripts" / "reviewer-yield.py"

# Load the module
_spec = importlib.util.spec_from_file_location("reviewer_yield", SCRIPT_PATH)
reviewer_yield = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reviewer_yield)


class FakeHome:
    """Context manager that points HOME at a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old
        self._tmp.cleanup()


def run_script(args, home: str = None) -> subprocess.CompletedProcess:
    """Run reviewer-yield.py as a subprocess."""
    env = dict(os.environ)
    if home:
        env["HOME"] = home
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)] + args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def main():
    h = Harness("REVIEWER-YIELD BACKSTOP SCAN & POD FORMAT TEST SUITE")
    t = h.test_result

    # ========================================================================
    print("[Case c] Pod-format run non-contamination and proper accounting")

    with FakeHome() as home:
        repo = "test-repo"

        # Helper: create a patch file with N changed lines to reach the "s" bucket
        def make_patch(n_lines):
            lines = ["--- a/file.txt", "+++ b/file.txt"]
            for i in range(n_lines):
                lines.append(f"+added line {i}")
            return "\n".join(lines)

        # Create 2 classic runs with different reviewer counts (both in "s" bucket)
        classic1_dir = home / ".claude" / "reviews" / repo / "classic-1-20260917T120000-00001"
        classic1_dir.mkdir(parents=True)
        (classic1_dir / "final-report.md").write_text("# Report\n")
        (classic1_dir / "uncle-bob-pass1.md").write_text("# pass1\n")
        (classic1_dir / "security-sage-pass1.md").write_text("# pass1\n")
        (classic1_dir / "full-diff.patch").write_text(make_patch(100))  # 100 lines -> "s" bucket

        classic2_dir = home / ".claude" / "reviews" / repo / "classic-2-20260917T120100-00002"
        classic2_dir.mkdir(parents=True)
        (classic2_dir / "final-report.md").write_text("# Report\n")
        (classic2_dir / "contract-chris-pass1.md").write_text("# pass1\n")
        (classic2_dir / "full-diff.patch").write_text(make_patch(50))  # 50 lines -> "s" bucket

        # Create 1 pod run with pod-format files (in "s" bucket)
        pod_dir = home / ".claude" / "reviews" / repo / "pod-run-20260917T120200-00003"
        pod_dir.mkdir(parents=True)
        (pod_dir / "final-report.md").write_text("# Report\n")
        (pod_dir / "architecture-reliability-pod.md").write_text("# Pod\n")
        (pod_dir / "contracts-correctness-pod.md").write_text("# Pod\n")
        (pod_dir / "pod-verification.md").write_text("# Verification\n")
        (pod_dir / "full-diff.patch").write_text(make_patch(75))  # 75 lines -> "s" bucket
        (pod_dir / "review-metrics.json").write_text(json.dumps({
            "pods": ["architecture-reliability", "contracts-correctness"],
            "lenses": ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"]
        }))

        cfg = reviewer_yield.load_bucket_config()
        report_data = reviewer_yield.compute_report_data(repo, cfg)

        # Check that pod run didn't affect reviewer counts
        reviewers_per_run = report_data.get("reviewers_per_run", {})
        t("Pod run not in reviewers_per_run: bucket exists",
          "s" in reviewers_per_run,
          f"got reviewers_per_run: {reviewers_per_run}")
        counts = reviewers_per_run.get("s", [])
        t("Pod run not in reviewers_per_run: exactly 2 classic runs",
          isinstance(counts, list) and len(counts) == 2 and sorted(counts) == [1, 2],
          f"got counts: {counts}, sorted: {sorted(counts)}")

    # ========================================================================
    print("\n[Case c sub-case] Pod-run CLI shows explicit pod notice")

    with FakeHome() as home:
        repo = "test-repo"

        pod_dir = home / ".claude" / "reviews" / repo / "pod-run-20260917T120200-00003"
        pod_dir.mkdir(parents=True)
        (pod_dir / "final-report.md").write_text("# Report\n")
        (pod_dir / "architecture-reliability-pod.md").write_text("# Pod\n")
        (pod_dir / "contracts-correctness-pod.md").write_text("# Pod\n")
        (pod_dir / "pod-verification.md").write_text("# Verification\n")
        (pod_dir / "review-metrics.json").write_text(json.dumps({
            "pods": ["architecture-reliability", "contracts-correctness"],
            "lenses": ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"]
        }))

        result = run_script([str(pod_dir)], home=str(home))

        t("Pod-run CLI: exit 0", result.returncode == 0, result.stderr[:200])
        t("Pod-run CLI: shows pod notice",
          "Pod-format" in result.stdout or "effort 2" in result.stdout,
          f"stdout: {result.stdout[:300]}")
        t("Pod-run CLI: lists pod names",
          "architecture" in result.stdout.lower() or "contracts" in result.stdout.lower(),
          f"stdout: {result.stdout[:300]}")

    # ========================================================================
    print("\n[Case c sub-case] Unknown-format run excluded from reviewers average")

    with FakeHome() as home:
        repo = "test-repo"

        # Helper: create a patch file with N changed lines to reach the "s" bucket
        def make_patch(n_lines):
            lines = ["--- a/file.txt", "+++ b/file.txt"]
            for i in range(n_lines):
                lines.append(f"+added line {i}")
            return "\n".join(lines)

        # Classic run (in "s" bucket)
        classic_dir = home / ".claude" / "reviews" / repo / "classic-20260917T120000-00001"
        classic_dir.mkdir(parents=True)
        (classic_dir / "final-report.md").write_text("# Report\n")
        (classic_dir / "uncle-bob-pass1.md").write_text("# pass1\n")
        (classic_dir / "full-diff.patch").write_text(make_patch(100))  # 100 lines -> "s" bucket

        # Unknown-format run (no *-pass1.md, no *-pod.md, in "s" bucket)
        unknown_dir = home / ".claude" / "reviews" / repo / "unknown-20260917T120100-00002"
        unknown_dir.mkdir(parents=True)
        (unknown_dir / "final-report.md").write_text("# Report\n")
        (unknown_dir / "some-random-file.md").write_text("# Unknown\n")
        (unknown_dir / "full-diff.patch").write_text(make_patch(75))  # 75 lines -> "s" bucket

        cfg = reviewer_yield.load_bucket_config()
        report_data = reviewer_yield.compute_report_data(repo, cfg)

        reviewers_per_run = report_data.get("reviewers_per_run", {})
        t("Unknown-format run excluded: bucket exists",
          "s" in reviewers_per_run,
          f"got reviewers_per_run: {reviewers_per_run}")
        counts = reviewers_per_run.get("s", [])
        t("Unknown-format run excluded from reviewers average: exactly 1 classic run",
          isinstance(counts, list) and len(counts) == 1,
          f"expected 1 count for classic only, got {counts}")

    # ========================================================================
    print("\n[Observation-only: origin file preserved]")

    with FakeHome() as home:
        repo = "test-repo"

        # Create a run with origin file
        run_dir = home / ".claude" / "reviews" / repo / "test-20260917T120000-00001"
        run_dir.mkdir(parents=True)
        (run_dir / "final-report.md").write_text("# Report\n")
        (run_dir / "uncle-bob-pass1.md").write_text("# pass1\n")

        origin_data = {
            "schema_version": 1,
            "cwd": "/test",
            "project_dir": "test-project",
            "session_id": "test-session",
            "resolution": "unavailable",
            "recorded_at": "2026-09-17T12:00:00Z"
        }
        origin_file = run_dir / "transcript-origin.json"
        origin_file.write_text(json.dumps(origin_data))
        original_bytes = origin_file.read_bytes()

        # Process the review (which may trigger scan)
        _, _, _ = reviewer_yield.process_review_dir(str(run_dir))

        # Origin file should not be modified
        t("Origin file unchanged after processing",
          origin_file.read_bytes() == original_bytes)

    # ========================================================================
    print("\n[Existence of backstop scan mechanisms]")

    # Verify that the module has the expected functions
    t("find_subagent_files_by_reviewer function exists",
      hasattr(reviewer_yield, 'find_subagent_files_by_reviewer'))
    t("classify_review_format function exists",
      hasattr(reviewer_yield, 'classify_review_format'))
    t("read_pod_manifest function exists",
      hasattr(reviewer_yield, 'read_pod_manifest'))

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
