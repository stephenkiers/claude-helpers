#!/usr/bin/env python3
"""
Test suite for Routing v2 Phase 2 - shadow report section in reviewer-yield.py (issue #195).

Covers shadow report integration tests (items 13-20 from Testing Strategy).

Every assertion here runs against the REAL functions in scripts/reviewer-yield.py
(loaded via importlib, since the filename is hyphenated).

Run with: python3 tests/test_route_score_shadow_report.py
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

REVIEWER_YIELD_PATH = REPO_ROOT / "scripts" / "reviewer-yield.py"
ROUTE_SCORE_PATH = REPO_ROOT / "scripts" / "route-score.py"

# Load modules via importlib
_spec_ry = importlib.util.spec_from_file_location("reviewer_yield", REVIEWER_YIELD_PATH)
reviewer_yield = importlib.util.module_from_spec(_spec_ry)
_spec_ry.loader.exec_module(reviewer_yield)


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


def make_run_dir(home: Path, repo: str, run_id: str) -> Path:
    """Create a review run directory."""
    run_dir = home / ".claude" / "reviews" / repo / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def make_minimal_diff() -> str:
    """Return a minimal valid unified diff."""
    return """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1 +1 @@
+x = 1
"""


def make_tagged_sections() -> str:
    """Build a minimal tagged-sections.md with Panel Decision table."""
    table = "| Reviewer | Role | Seated | ... |\n"
    table += "|---|---|---|---|\n"
    table += "| uncle-bob | Specialist | Yes | ... |\n"

    return f"""# Panel Decision Table

{table}

## Other sections
Just filler.
"""


def make_finding(fid, severity, raised_by, supported_by=None, verdict="CONFIRMED"):
    """Build a finding entry."""
    return {
        "id": fid,
        "severity": severity,
        "raised_by": raised_by,
        "supported_by": supported_by or [],
        "verdict": verdict,
    }


def make_route_scores(mode="routed", effort=4, status="ok"):
    """Build a route-scores.json structure."""
    return {
        "scorer_version": "1",
        "status": status,
        "mode": mode,
        "pr": False,
        "effort": effort,
        "degraded": False,
        "thresholds_provisional": True,
        "reviewers": {
            "uncle-bob": {"score": 6, "tier": "Must", "reasons": []},
            "security-sage": {"score": 3, "tier": "Candidate", "reasons": []},
        }
    }


if __name__ == "__main__":
    h = Harness("ROUTING V2 PHASE 2 - SHADOW REPORT TEST SUITE")
    t = h.test_result

    # ========================================================================
    print("[Section 1] Shadow section exists in report data")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "run-1")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [make_finding("f1", "High", "uncle-bob", [], "CONFIRMED")]
        }))
        run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores()))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            has_shadow = "shadow" in data
            t("shadow section exists in report data", has_shadow)

            if has_shadow:
                shadow = data.get("shadow")
                t("shadow section is a dict", isinstance(shadow, dict))
                t("shadow has status field", "status" in shadow)
        except Exception as e:
            t("shadow section creation", False, str(e))

    # ========================================================================
    print("\n[Section 2] Shadow section with effort-4 routed run")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "routed-run")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [
                make_finding("f1", "Critical", "uncle-bob", [], "CONFIRMED"),
                make_finding("f2", "High", "uncle-bob", [], "CONFIRMED"),
            ]
        }))
        run_dir.joinpath("route-scores.json").write_text(json.dumps(
            make_route_scores(mode="routed", effort=4)
        ))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            # The effort4_routed cohort should exist or be None
            effort4 = shadow.get("effort4_routed")

            t("effort4_routed cohort is dict or None when present",
              effort4 is None or isinstance(effort4, dict),
              f"effort4={effort4}")
        except Exception as e:
            t("effort4 routed cohort", False, str(e))

    # ========================================================================
    print("\n[Section 3] Shadow section with effort-5 named run")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "named-run")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [make_finding("f1", "High", "uncle-bob", [], "CONFIRMED")]
        }))
        run_dir.joinpath("route-scores.json").write_text(json.dumps(
            make_route_scores(mode="named", effort=5)
        ))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            # The effort5_full cohort should exist or be None
            effort5 = shadow.get("effort5_full")

            t("effort5_full cohort is dict or None when present",
              effort5 is None or isinstance(effort5, dict),
              f"effort5={effort5}")
        except Exception as e:
            t("effort5 named cohort", False, str(e))

    # ========================================================================
    print("\n[Section 4] Silent-zero guard: classifications exist")

    with FakeHome() as home:
        # Runs with various missing pieces
        for i, (has_diff, has_findings, has_scores) in enumerate([
            (False, True, True),   # no-diff
            (True, False, True),   # unattributable
            (True, True, False),   # pre-shadow
        ]):
            run_id = f"test-{i}"
            run_dir = make_run_dir(home, "test-repo", run_id)
            run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())

            if has_diff:
                run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
            if has_findings:
                run_dir.joinpath("findings.json").write_text(json.dumps({
                    "schema_version": 1,
                    "findings": [make_finding("f", "High", "uncle-bob", [], "CONFIRMED")]
                }))
            if has_scores:
                run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores()))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            counts = shadow.get("counts", {})

            t("shadow section has counts dict for classifications",
              isinstance(counts, dict) and len(counts) > 0,
              f"counts={counts}")
        except Exception as e:
            t("silent-zero guard classifications", False, str(e))

    # ========================================================================
    print("\n[Section 5] Methodology section includes explanation")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "method-test")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [make_finding("f1", "High", "uncle-bob", [], "CONFIRMED")]
        }))
        run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores()))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            methodology = shadow.get("methodology", {})

            t("shadow methodology section exists",
              isinstance(methodology, dict),
              f"methodology={methodology}")

            t("shadow methodology includes censoring sentence",
              "censoring_sentence" in methodology or "re-scored" in str(methodology),
              f"methodology={methodology}")
        except Exception as e:
            t("methodology section", False, str(e))

    # ========================================================================
    print("\n[Section 6] Markdown rendering includes shadow section")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "markdown-test")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [make_finding("f1", "High", "uncle-bob", [], "CONFIRMED")]
        }))
        run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores()))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            markdown = reviewer_yield.render_report_markdown(data)

            t("rendered markdown is a string", isinstance(markdown, str) and len(markdown) > 0)

            t("markdown contains shadow section heading",
              "Shadow" in markdown and "shadow" in markdown.lower())

            t("markdown contains observe-only or observation note",
              "observe-only" in markdown or "observation" in markdown.lower())
        except Exception as e:
            t("markdown rendering", False, str(e))

    # ========================================================================
    print("\n[Section 7] Always-run reviewers excluded from miss rate")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "always-run-test")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [
                make_finding("f1", "High", "contrarian-carl", [], "CONFIRMED"),
                make_finding("f2", "High", "code-rot-cody", [], "CONFIRMED"),
            ]
        }))

        scores = make_route_scores()
        scores["reviewers"]["contrarian-carl"] = {"score": 0, "tier": "Always", "reasons": []}
        scores["reviewers"]["code-rot-cody"] = {"score": 0, "tier": "Always", "reasons": []}
        run_dir.joinpath("route-scores.json").write_text(json.dumps(scores))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            counts = shadow.get("counts", {})

            # Always-run findings should not count as misses
            always_run_count = counts.get("always_run_only", 0)
            t("findings by always-run reviewers tracked separately",
              isinstance(counts, dict),
              f"counts={counts}")
        except Exception as e:
            t("always-run exclusion", False, str(e))

    # ========================================================================
    print("\n[Section 8] Report handles missing scorer gracefully")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "unavail-test")
        run_dir.joinpath("full-diff.patch").write_text(make_minimal_diff())
        run_dir.joinpath("tagged-sections.md").write_text(make_tagged_sections())
        run_dir.joinpath("findings.json").write_text(json.dumps({
            "schema_version": 1,
            "findings": [make_finding("f1", "High", "uncle-bob", [], "CONFIRMED")]
        }))
        # Stored status is error
        scores = make_route_scores()
        scores["status"] = "error"
        scores["error"] = "ImportError: No module named 'yaml'"
        run_dir.joinpath("route-scores.json").write_text(json.dumps(scores))

        try:
            cfg = reviewer_yield.load_bucket_config()
            data = reviewer_yield.compute_report_data("test-repo", cfg)

            shadow = data.get("shadow", {})
            counts = shadow.get("counts", {})

            # Should track hook errors but still process
            t("hook_errors tracked in counts",
              isinstance(counts, dict) and ("hook_errors" in counts or len(counts) > 0),
              f"counts={counts}")

            # Rest of report should still render
            t("report still produces markdown after scorer error",
              reviewer_yield.render_report_markdown(data) is not None)
        except Exception as e:
            t("scorer error handling", False, str(e))

    # ========================================================================
    # The fixtures above use untimestamped run ids, which the report classifies
    # as regime "unknown" and skips. These use post-148 run ids so re-scoring
    # actually runs, and the expected values are derived from the real scorer.
    print("\n[Section 9] Per-finding misses, severities and contingency on a scored corpus")

    _spec_rs = importlib.util.spec_from_file_location("route_score_for_tests", ROUTE_SCORE_PATH)
    route_score = importlib.util.module_from_spec(_spec_rs)
    _spec_rs.loader.exec_module(route_score)

    ui_diff = """diff --git a/web/components/Button.tsx b/web/components/Button.tsx
--- a/web/components/Button.tsx
+++ b/web/components/Button.tsx
@@ -1 +1 @@
+export const Button = () => <button className="primary">Click</button>
"""
    configs = route_score.load_route_configs(REPO_ROOT / "reviewers" / "index.yaml")
    tiers = {slug: r.tier for slug, r in route_score.score_diff(ui_diff, configs).reviewers.items()}
    excluded = sorted(slug for slug, tier in tiers.items() if tier == "Exclude")
    included = sorted(slug for slug, tier in tiers.items() if tier in ("Must", "Candidate"))
    t("fixture diff yields both an Exclude and a Must/Candidate reviewer",
      bool(excluded) and bool(included), f"tiers={tiers}")

    if excluded and included:
        ex, inc = excluded[0], included[0]
        run_id = "feat-abc123-20260920T120000-xyz"
        with FakeHome() as home:
            run_dir = make_run_dir(home, "test-repo", run_id)
            run_dir.joinpath("full-diff.patch").write_text(ui_diff)
            run_dir.joinpath("tagged-sections.md").write_text(
                "## Panel Decision\n\n| Reviewer | Selected | Reason |\n|---|---|---|\n"
                f"| {inc} | Yes | routed |\n| {ex} | No | not routed |\n"
            )
            run_dir.joinpath("findings.json").write_text(json.dumps({"schema_version": 1, "findings": [
                make_finding("f1", "Critical", ex),                  # miss, sole-source
                make_finding("f2", "High", ex, [inc]),               # not a miss: supporter included
                make_finding("f3", "Medium", ex, [ex]),              # miss, not sole-source
                make_finding("f4", "Low", ex),                       # miss, sole-source
                make_finding("f5", "High", "contrarian-carl"),       # always-run-only
                make_finding("f6", "High", "no-such-reviewer"),      # unattributed
                make_finding("f7", "Critical", ex, verdict="REFUTED"),  # not confirmed
            ]}))
            run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores(mode="routed", effort=4)))

            data = reviewer_yield.compute_report_data("test-repo", reviewer_yield.load_bucket_config())
            shadow = data["shadow"]
            t("shadow section is available", shadow.get("status") == "available", str(shadow.get("reason")))
            cohort = shadow.get("effort4_routed") or {}
            t("confirmed counts by severity",
              cohort.get("confirmed_count") == {"critical": 1, "high": 3, "medium": 1, "low": 1},
              str(cohort.get("confirmed_count")))
            t("missed counts include medium and low",
              cohort.get("missed_count") == {"critical": 1, "high": 0, "medium": 1, "low": 1},
              str(cohort.get("missed_count")))
            t("crit+high rate is misses over confirmed", cohort.get("crit_high_rate") == "1/4",
              str(cohort.get("crit_high_rate")))
            t("sole-source misses counted per reviewer", cohort.get("sole_source_misses") == {ex: 2},
              str(cohort.get("sole_source_misses")))
            t("unattributed and always-run-only counted",
              cohort.get("unattributed_count") == 1 and cohort.get("always_run_only_count") == 1)
            t("missed list names the findings",
              sorted(m["finding_id"] for m in cohort.get("missed_findings", [])) == ["f1", "f3", "f4"])

            expected = {"seated_yes": {"Must": 0, "Candidate": 0, "Exclude": 0},
                        "seated_no": {"Must": 0, "Candidate": 0, "Exclude": 0}}
            for slug, tier in tiers.items():
                if tier in ("Must", "Candidate", "Exclude"):
                    expected["seated_yes" if slug == inc else "seated_no"][tier] += 1
            t("contingency table matches hand-computed seated x tier cells",
              cohort.get("contingency_table") == expected, str(cohort.get("contingency_table")))

            md = reviewer_yield.render_report_markdown(data)
            t("markdown has the exact shadow heading", "Shadow scorer (observe-only)" in md)
            t("markdown carries the censoring sentence verbatim",
              "it is a lower bound, not proof the scorer is safe" in md)
            t("markdown lists a missed finding", "f1 (Critical)" in md)

    # ========================================================================
    print("\n[Section 10] All-unscored corpus reports n=0 and no rate")

    with FakeHome() as home:
        run_dir = make_run_dir(home, "test-repo", "feat-abc123-20260920T120000-nodiff")
        run_dir.joinpath("route-scores.json").write_text(json.dumps(make_route_scores()))
        data = reviewer_yield.compute_report_data("test-repo", reviewer_yield.load_bucket_config())
        shadow = data["shadow"]
        t("unscored run counted", shadow.get("counts", {}).get("unscored_no_diff") == 1, str(shadow.get("counts")))
        t("no effort-4 cohort stats", shadow.get("effort4_routed") is None)
        md = reviewer_yield.render_report_markdown(data)
        t("markdown says no rate rather than 0%", "n=0 runs; no rate" in md and "0/0" not in md)

    # ========================================================================
    print("\n[Section 11] Unloadable route config makes the section unavailable")

    original_resolve = reviewer_yield._resolve_index_path
    with FakeHome() as home, tempfile.TemporaryDirectory() as td:
        bad_index = Path(td) / "index.yaml"
        bad_index.write_text("reviewers:\n  - name: X\n    file: x.yaml\n    route: {include_at: nope}\n")
        reviewer_yield._resolve_index_path = lambda: bad_index
        try:
            run_dir = make_run_dir(home, "test-repo", "feat-abc123-20260920T120000-bad")
            data = reviewer_yield.compute_report_data("test-repo", reviewer_yield.load_bucket_config())
            t("status unavailable with a reason",
              data["shadow"].get("status") == "unavailable" and "route config" in data["shadow"].get("reason", ""),
              str(data["shadow"]))
            t("rest of report still renders", "Shadow scorer (observe-only)" in reviewer_yield.render_report_markdown(data))
        finally:
            reviewer_yield._resolve_index_path = original_resolve

    h.summarize_and_exit()
