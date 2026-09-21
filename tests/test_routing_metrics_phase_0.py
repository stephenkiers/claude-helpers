#!/usr/bin/env python3
"""
Test suite for Routing v2 Phase 0 - Make Reviewer Routing Measurable (issue #193).

Every assertion here runs against the REAL functions in scripts/reviewer-yield.py
(loaded via importlib, since the filename is hyphenated), never against a
hand-rolled copy of the logic.

Covers:
- 0a: transcript-origin.json discovery, token parsing (against the checked-in
  fixture), reviewer slug <-> display-name matching
- 0b: findings.json consumption
- 0c: regime classification, size buckets, --report / --report-json modes

Run with: python3 tests/test_routing_metrics_phase_0.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT_PATH = REPO_ROOT / "scripts" / "reviewer-yield.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "reviewer-yield" / "sample-uncle-bob-pass1.jsonl"

_spec = importlib.util.spec_from_file_location("reviewer_yield", SCRIPT_PATH)
ry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ry)


def load_json(path: Path) -> dict:
    """Load JSON file safely."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def run_reviewer_yield(args: list, home: str = None) -> subprocess.CompletedProcess:
    """Run reviewer-yield.py as a subprocess, optionally with an isolated HOME."""
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


class FakeHome:
    """Context manager that points HOME at a temp dir (Path.home() honours HOME on POSIX)."""

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


def make_run(home: Path, repo: str, name: str, findings=None, pass1=("uncle-bob",), patch_lines=None) -> Path:
    """Create ~/.claude/reviews/{repo}/{name} with optional findings.json / full-diff.patch."""
    run_dir = home / ".claude" / "reviews" / repo / name
    run_dir.mkdir(parents=True)
    (run_dir / "final-report.md").write_text("# Report\n")
    for slug in pass1:
        (run_dir / f"{slug}-pass1.md").write_text("# pass1\n")
    if findings is not None:
        (run_dir / "findings.json").write_text(json.dumps({"schema_version": 1, "findings": findings}))
    if patch_lines is not None:
        (run_dir / "full-diff.patch").write_text("--- a\n+++ b\n" + "+x\n" * patch_lines)
    return run_dir


def finding(fid, severity, raised_by, supported_by, verdict):
    return {"id": fid, "severity": severity, "raised_by": raised_by,
            "supported_by": supported_by, "verdict": verdict}


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 0 - MAKE REVIEWER ROUTING MEASURABLE TEST SUITE")
    t = h.test_result

    # ========================================================================
    print("[Section 1] findings.json consumed by the real reader")
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        rows = [finding("f1", "Critical", "uncle-bob", ["security-sage"], "CONFIRMED"),
                finding("f2", "High", "rachel", [], "DOWNGRADED")]
        (d / "findings.json").write_text(json.dumps({"schema_version": 1, "findings": rows}))
        data = ry.read_findings_json(d)
        t("read_findings_json returns schema_version 1", data is not None and data.get("schema_version") == 1)
        t("read_findings_json preserves findings", data is not None and data["findings"] == rows)
        t("read_findings_json returns None when file missing", ry.read_findings_json(d / "nope") is None)
        (d / "findings.json").write_text("{not json")
        t("read_findings_json returns None on malformed JSON", ry.read_findings_json(d) is None)

    # ========================================================================
    print("\n[Section 2] reviewer-yield.py script executability")
    t("reviewer-yield.py exists", SCRIPT_PATH.exists())
    import stat
    t("reviewer-yield.py is executable", bool(SCRIPT_PATH.stat().st_mode & stat.S_IXUSR))
    t("reviewer-yield.py --help runs without error", run_reviewer_yield(["--help"]).returncode == 0)

    # ========================================================================
    print("\n[Section 3] Fixture parsed by real token parser (dedup, defaults)")
    tokens = ry.parse_tokens_from_subagent(FIXTURE)
    # msg-1 (dup skipped) + msg-2 + msg-3 (no cache fields) + id-less entry
    t("fixture input_tokens deduped by message id", tokens["input_tokens"] == 1000 + 500 + 200 + 150,
      f"got {tokens['input_tokens']}")
    t("fixture output_tokens deduped by message id", tokens["output_tokens"] == 500 + 200 + 100 + 75,
      f"got {tokens['output_tokens']}")
    t("fixture cache_read defaults missing to 0", tokens["cache_read_input_tokens"] == 100 + 50,
      f"got {tokens['cache_read_input_tokens']}")
    t("fixture cache_creation defaults missing to 0", tokens["cache_creation_input_tokens"] == 50 + 25,
      f"got {tokens['cache_creation_input_tokens']}")
    t("fixture Write tool_use anchors transcript to uncle-bob",
      ry._subagent_reviewer_for_review_dir(FIXTURE, "feature-123", ["rachel", "uncle-bob"]) == "uncle-bob")
    t("fixture is not attributed when review dir name differs",
      ry._subagent_reviewer_for_review_dir(FIXTURE, "other-run", ["uncle-bob"]) is None)

    # ========================================================================
    print("\n[Section 4] transcript-origin.json discovery (real schema) + tokens_status")
    with FakeHome() as home:
        run_dir = make_run(home, "test-repo", "feature-123")
        sub_dir = home / ".claude" / "projects" / "proj" / "sess" / "subagents"
        sub_dir.mkdir(parents=True)
        shutil.copy(FIXTURE, sub_dir / "agent-1.jsonl")
        origin = {"schema_version": 1, "cwd": "/x", "project_dir": "proj", "session_id": "sess",
                  "resolution": "env", "recorded_at": "2026-09-17T12:00:00Z"}
        (run_dir / "transcript-origin.json").write_text(json.dumps(origin))

        found = ry.find_subagent_files_by_reviewer(str(run_dir), ["uncle-bob"])
        t("discovery finds fixture transcript via origin file",
          found.get("uncle-bob") == [sub_dir / "agent-1.jsonl"])
        _, rows, status = ry.process_review_dir(str(run_dir))
        t("process_review_dir reports tokens_status measured", status == "measured")
        t("process_review_dir rows carry fixture token totals",
          len(rows) == 1 and rows[0]["input_tokens"] == 1850 and rows[0]["tokens_status"] == "measured")

        (run_dir / "transcript-origin.json").write_text(json.dumps({"resolution": "unavailable"}))
        _, rows, status = ry.process_review_dir(str(run_dir))
        t("unavailable origin gives tokens_status unavailable and zero tokens",
          status == "unavailable" and rows[0]["input_tokens"] == 0)

        (run_dir / "transcript-origin.json").unlink()
        _, rows, status = ry.process_review_dir(str(run_dir))
        t("missing origin file gives tokens_status unavailable", status == "unavailable")

    # ========================================================================
    print("\n[Section 5] Reviewer slug <-> display-name matching")
    names = ry.load_reviewer_display_names()
    t("display names loaded from reviewers/index.yaml", len(names) > 0)
    t("uncle-bob resolves to 'Uncle Bob'", names.get("uncle-bob") == "Uncle Bob")
    t("extract_reviewer_name strips pass suffix", ry.extract_reviewer_name("uncle-bob-pass1.md") == "uncle-bob")

    try:
        import yaml
    except ImportError:
        print("  (PyYAML not installed; skipping index.yaml structure check)")
    else:
        index_data = yaml.safe_load((REPO_ROOT / "reviewers" / "index.yaml").read_text())
        reviewers = index_data.get("reviewers", [])
        t("index.yaml has reviewers list", len(reviewers) > 0)
        t("every index.yaml reviewer has name and file", all("name" in r and "file" in r for r in reviewers))
        t("script's slug map covers every index.yaml reviewer",
          all(r["file"].rsplit(".", 1)[0] in names for r in reviewers))

    # ========================================================================
    print("\n[Section 6] Regime classification (real cutoffs 2026-07-12 / 2026-09-16)")
    from datetime import datetime
    cases = [
        (datetime(2026, 1, 1), "pre-router"),
        (datetime(2026, 7, 11, 23, 59, 59), "pre-router"),
        (datetime(2026, 7, 12), "judgment-router"),
        (datetime(2026, 9, 15, 23, 59, 59), "judgment-router"),
        (datetime(2026, 9, 16), "post-148-sam-gated"),
        (datetime(2026, 12, 1), "post-148-sam-gated"),
        (None, "unknown"),
    ]
    for ts, expected in cases:
        t(f"classify_regime({ts}) == {expected}", ry.classify_regime(ts) == expected)
    t("parse_review_timestamp reads dir-name stamp",
      ry.parse_review_timestamp("feature-20260917T120000-00001") == datetime(2026, 9, 17, 12, 0, 0))

    # ========================================================================
    print("\n[Section 7] Size buckets (real config + classify_size_bucket)")
    cfg = ry.load_bucket_config()
    t("bucket config has config_version", cfg.get("config_version") is not None)
    for changed, expected in [(0, "xs"), (49, "xs"), (50, "s"), (199, "s"), (200, "m"),
                              (799, "m"), (800, "l"), (100000, "l"), (None, "unknown")]:
        t(f"classify_size_bucket({changed}) == {expected}", ry.classify_size_bucket(changed, cfg) == expected)

    # ========================================================================
    print("\n[Section 8] compute_report_data: weighting, solo, regimes, exclusions, tokens")
    with FakeHome() as home:
        post = "feature-20260917T120000-00001"
        make_run(home, "r", post, patch_lines=10, findings=[
            finding("f1", "Critical", "security-sage", ["tara-typesafe"], "CONFIRMED"),
            finding("f2", "High", "uncle-bob", [], "CONFIRMED"),
            finding("f3", "Medium", "rachel", [], "DOWNGRADED"),
            finding("f4", "Low", "contract-chris", [], "CONFIRMED"),
            finding("f5", "Medium", "eric-evans", ["rachel"], "REJECTED"),
        ])
        make_run(home, "r", "feature-20260101T000000-00002", findings=[
            finding("g1", "Critical", "old-reviewer", [], "CONFIRMED")])
        make_run(home, "r", "feature-20260801T000000-00003")
        make_run(home, "r", "feature-20260918T000000-00004")  # post regime, no findings.json
        data = ry.compute_report_data("r", cfg)

        t("verified value = crit*8 + high*4 + low*1 over CONFIRMED only",
          data["verified_value_per_run"].get("xs") == [8 + 4 + 1], str(data["verified_value_per_run"]))
        t("verified crit/high count excludes downgraded/rejected",
          data["verified_crit_high_per_run"].get("xs") == [2])
        t("solo findings only from CONFIRMED with empty supported_by, post regime only",
          data["solo_findings_per_reviewer"] == {"uncle-bob": 1, "contract-chris": 1},
          str(data["solo_findings_per_reviewer"]))
        t("regime_counts tallies every regime",
          data["regime_counts"] == {"pre-router": 1, "judgment-router": 1, "post-148-sam-gated": 2},
          str(data["regime_counts"]))
        t("n_included_runs counts post-148 runs", data["n_included_runs"] == 2)
        t("n_excluded_runs counts other regimes", data["n_excluded_runs"] == 2)
        t("post run missing findings.json adds no zero to finding denominators",
          len(data["verified_value_per_run"].get("xs", [])) == 1)
        t("tokens unavailable (no measured rows) is a status dict, not zero",
          data["tokens"].get("status") == "unavailable" and "total_input_tokens" not in data["tokens"])
        t("valLift is not_yet_available", data["valLift"]["status"] == "not_yet_available")
        t("shadow_miss_rate is not_yet_available", data["shadow_miss_rate"]["status"] == "not_yet_available")
        t("methodology carries observation-only sentence",
          data["methodology"]["observation_only_sentence"] == ry.OBSERVATION_ONLY_NOTE)

        yield_file = home / ".claude" / "reviews" / "r" / "reviewer-yield.jsonl"
        rows = [
            {"run_id": post, "reviewer": "a", "tokens_status": "measured", "input_tokens": 100,
             "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"run_id": post, "reviewer": "b", "tokens_status": "unavailable", "input_tokens": 0,
             "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ]
        yield_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        measured = ry.compute_report_data("r", cfg)["tokens"]
        t("unavailable rows excluded from token totals", measured.get("total_input_tokens") == 100)

    # ========================================================================
    print("\n[Section 9] --report / --report-json end to end")
    with FakeHome() as home:
        make_run(home, "r", "feature-20260917T120000-00001", findings=[
            finding("f1", "High", "uncle-bob", [], "CONFIRMED")])
        with tempfile.TemporaryDirectory() as out:
            json_path = Path(out) / "report.json"
            res = run_reviewer_yield(["--report", "r", "--report-json", str(json_path)], home=str(home))
            t("--report exits 0", res.returncode == 0, res.stderr[-200:])
            t("--report prints Markdown to stdout", "# Reviewer Routing Metrics Report" in res.stdout)
            report = load_json(json_path)
            t("--report-json writes JSON with same n_included_runs", report.get("n_included_runs") == 1)
            t("Markdown and JSON agree on solo findings",
              report.get("solo_findings_per_reviewer") == {"uncle-bob": 1}
              and "uncle-bob: 1 solo findings" in res.stdout)
            t("render_report_markdown output matches stdout section",
              ry.render_report_markdown(report).strip() in res.stdout)

    print()
    h.summarize_and_exit()
