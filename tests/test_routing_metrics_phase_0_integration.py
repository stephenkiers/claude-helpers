#!/usr/bin/env python3
"""
Spec-blind sanity check for Routing v2 Phase 0 - reviewer-yield.py.

Written from the plan's spec, not from the implementation. Each assertion imports
the real scripts/reviewer-yield.py (via importlib, since the filename is hyphenated)
and checks its actual output; nothing here re-implements the logic under test.

Covers: token parsing with dedup/safe defaults, severity weighting, regime
classification, solo-finding attribution, n_included/n_excluded, and the
--report Markdown / --report-json output shape.

Run with: python3 tests/test_routing_metrics_phase_0_integration.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT_PATH = REPO_ROOT / "scripts" / "reviewer-yield.py"

_spec = importlib.util.spec_from_file_location("reviewer_yield", SCRIPT_PATH)
ry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ry)


def write_session(path: Path, entries: list) -> Path:
    """Write a subagent transcript JSONL made of the given entries."""
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def assistant(msg_id, usage):
    message = {"usage": usage, "content": []}
    if msg_id is not None:
        message["id"] = msg_id
    return {"type": "assistant", "message": message}


def build_reviews(home: Path, repo: str, runs: dict) -> None:
    """runs: {dir_name: findings list or None}; creates ~/.claude/reviews/{repo}/..."""
    for name, findings in runs.items():
        run_dir = home / ".claude" / "reviews" / repo / name
        run_dir.mkdir(parents=True)
        (run_dir / "final-report.md").write_text("# Report\n")
        (run_dir / "uncle-bob-pass1.md").write_text("# pass1\n")
        if findings is not None:
            (run_dir / "findings.json").write_text(
                json.dumps({"schema_version": 1, "findings": findings}))


def f(fid, severity, raised_by, supported_by, verdict):
    return {"id": fid, "severity": severity, "raised_by": raised_by,
            "supported_by": supported_by, "verdict": verdict}


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 0 - SPEC-BLIND SANITY CHECK")
    t = h.test_result

    # ========================================================================
    print("[Section 1] Script invocation")
    res = subprocess.run([sys.executable, str(SCRIPT_PATH), "--help"], capture_output=True, text=True, timeout=10)
    t("--help returns exit code 0", res.returncode == 0)
    t("--help prints usage", "--report" in res.stdout)

    # ========================================================================
    print("\n[Section 2] Token parsing: dedup by id and safe defaults")
    with tempfile.TemporaryDirectory() as tmpdir:
        session = write_session(Path(tmpdir) / "s.jsonl", [
            assistant("m1", {"input_tokens": 100, "output_tokens": 50}),
            assistant("m2", {"input_tokens": 200, "output_tokens": 100}),
            assistant("m1", {"input_tokens": 100, "output_tokens": 50}),  # duplicate id
            assistant("m3", {}),  # empty usage -> zeros
            assistant(None, {"input_tokens": 7, "output_tokens": 3}),  # no id -> counted
            {"type": "assistant", "message": {"id": "m4"}},  # no usage -> skipped
            {"type": "user", "message": {"content": "hi"}},
        ])
        tok = ry.parse_tokens_from_subagent(session)
        t("input tokens deduplicated", tok["input_tokens"] == 307, str(tok))
        t("output tokens deduplicated", tok["output_tokens"] == 153, str(tok))
        t("missing cache fields default to 0",
          tok["cache_read_input_tokens"] == 0 and tok["cache_creation_input_tokens"] == 0)
        missing = ry.parse_tokens_from_subagent(Path(tmpdir) / "absent.jsonl")
        t("unreadable file yields zero-filled record", all(v == 0 for v in missing.values()))

    # ========================================================================
    print("\n[Section 3] Regime classification")
    for ts, expected in [(datetime(2026, 3, 1), "pre-router"), (datetime(2026, 8, 1), "judgment-router"),
                         (datetime(2026, 10, 1), "post-148-sam-gated"), (None, "unknown")]:
        t(f"{ts} -> {expected}", ry.classify_regime(ts) == expected)

    # ========================================================================
    print("\n[Section 4] Severity weighting, solo findings, n_included/n_excluded")
    old_home = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        build_reviews(home, "repo", {
            "b-20261001T000000-00001": [
                f("1", "Critical", "alice", [], "CONFIRMED"),
                f("2", "High", "bob", ["alice"], "CONFIRMED"),
                f("3", "High", "bob", [], "CONFIRMED"),
                f("4", "Medium", "carol", [], "CONFIRMED"),
                f("5", "Medium", "carol", [], "DOWNGRADED"),
                f("6", "Low", "dave", [], "REJECTED"),
            ],
            "b-20261002T000000-00002": [f("7", "Low", "dave", ["bob"], "CONFIRMED")],
            "b-20260101T000000-00003": [f("8", "Critical", "zed", [], "CONFIRMED")],
            "b-20260501T000000-00004": None,
        })
        try:
            os.environ["HOME"] = tmpdir
            data = ry.compute_report_data("repo", ry.load_bucket_config())
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

        t("run values: 8+4+4+2 = 18 and 1",
          sorted(v for vals in data["verified_value_per_run"].values() for v in vals) == [1, 18],
          str(data["verified_value_per_run"]))
        t("crit/high per run: 3 and 0",
          sorted(v for vals in data["verified_crit_high_per_run"].values() for v in vals) == [0, 3])
        t("solo findings attributed to raising reviewer only",
          data["solo_findings_per_reviewer"] == {"alice": 1, "bob": 1, "carol": 1},
          str(data["solo_findings_per_reviewer"]))
        t("pre-post-148 findings excluded from solo tally", "zed" not in data["solo_findings_per_reviewer"])
        t("n_included_runs == 2", data["n_included_runs"] == 2)
        t("n_excluded_runs == 2", data["n_excluded_runs"] == 2)

        # ====================================================================
        print("\n[Section 5] --report / --report-json output shape")
        env = dict(os.environ, HOME=tmpdir)
        out_json = home / "out.json"
        res = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--report", "repo", "--report-json", str(out_json)],
            capture_output=True, text=True, env=env, timeout=30)
        t("--report exits 0", res.returncode == 0, res.stderr[-200:])
        t("stdout is Markdown with methodology", res.stdout.startswith("# ") and "## Methodology" in res.stdout)
        try:
            report = json.loads(out_json.read_text())
        except (OSError, json.JSONDecodeError):
            report = {}
        t("JSON has regime counts and n_included/n_excluded",
          "regime_counts" in report and report.get("n_included_runs") == 2 and report.get("n_excluded_runs") == 2)
        t("valLift renders not_yet_available with reason",
          report.get("valLift", {}).get("status") == "not_yet_available" and report["valLift"].get("reason"))
        t("shadow_miss_rate renders not_yet_available",
          report.get("shadow_miss_rate", {}).get("status") == "not_yet_available")
        t("tokens render as unavailable status, not zero", report.get("tokens", {}).get("status") == "unavailable")

    print()
    h.summarize_and_exit()
