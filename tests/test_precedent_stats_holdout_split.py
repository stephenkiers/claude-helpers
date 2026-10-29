#!/usr/bin/env python3
"""Tests for the --stats displayed/hold-out split in scripts/precedent-digest.py (#296 follow-up).

Hold-out rows (logged with displayed: false) are the unbiased arm. The top-level predictions keys
cover displayed rows only (a row with no displayed field is displayed); hold-out rows are reported
separately under predictions.holdout, with the same always-recommended baseline.

Run with: python3 tests/test_precedent_stats_holdout_split.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness  # noqa: E402
from test_precedent_log import Fixture, _predictions, run_digest_stats  # noqa: E402
from test_precedent_log_review_hardening import _row, _write_rows, run_digest_md  # noqa: E402


def _held(row):
    row = dict(row)
    row["displayed"] = False
    return row


def case_split(h):
    with Fixture() as fx:
        _write_rows(fx, [
            _row("Q1", run="r1", predicted="A", recommended="B", actual="A", hit=True),  # no field
            _row("Q2", run="r2", predicted="A", recommended="B", actual="B", hit=False),
            dict(_row("Q3", run="r3", predicted="A", recommended="B", actual="A", hit=True), displayed=True),
            _held(_row("Q4", run="r4", predicted="A", recommended="B", actual="B", hit=False)),
            _held(_row("Q5", run="r5", predicted="A", recommended="B", actual="B", hit=False,
                       confidence="medium")),
        ])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        ho = pred.get("holdout") or {}
        high = (pred.get("by_confidence") or {}).get("high", {})
        h.test_result("split: exit 0", rc == 0, f"rc={rc}")
        h.test_result("split: top-level counts displayed rows only (missing field = displayed)",
                      pred.get("logged") == 3 and pred.get("scored") == 3, f"{pred.get('logged')}/{pred.get('scored')}")
        h.test_result("split: displayed hit rate is 2/3", pred.get("hit_rate") == round(2 / 3, 4),
                      f"{pred.get('hit_rate')}")
        h.test_result("split: displayed high-confidence excludes hold-out rows",
                      high.get("logged") == 3 and high.get("scored") == 3, f"{high}")
        h.test_result("split: holdout counts", ho.get("logged") == 2 and ho.get("scored") == 2, f"{ho}")
        h.test_result("split: holdout hit rate is 0.0 (not null) with always-recommended baseline 1.0",
                      ho.get("hit_rate") == 0.0 and ho.get("baseline_hit_rate") == 1.0
                      and ho.get("baseline_n") == 2, f"{ho}")
        hc = ho.get("by_confidence") or {}
        h.test_result("split: holdout by_confidence separates high and medium",
                      (hc.get("high") or {}).get("scored") == 1 and (hc.get("medium") or {}).get("scored") == 1,
                      f"{hc}")
        rc_md, out_md, _ = run_digest_md(fx.home, fx.log_root, "owner-name")
        h.test_result("split md: exit 0 and Hold-out lines present",
                      rc_md == 0 and "**Hold-out**" in out_md and "**Hold-out High**" in out_md, out_md[-400:])


def case_backward_compatible(h):
    with Fixture() as fx:
        _write_rows(fx, [_row("Q1", run="r1", predicted="A", recommended="A", actual="A", hit=True)])
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        pred = _predictions(stats)
        ho = pred.get("holdout") or {}
        h.test_result("compat: no hold-out rows leaves existing keys unchanged",
                      rc == 0 and pred.get("logged") == 1 and pred.get("hit_rate") == 1.0, f"{pred}")
        h.test_result("compat: empty holdout arm has null rates, zero counts",
                      ho.get("logged") == 0 and ho.get("hit_rate") is None and ho.get("baseline_hit_rate") is None,
                      f"{ho}")
    with Fixture() as fx:
        rc, stats, _ = run_digest_stats(fx.home, fx.log_root, "owner-name")
        h.test_result("compat: absent log still has holdout key",
                      rc == 0 and "holdout" in _predictions(stats), f"{_predictions(stats)}")


if __name__ == "__main__":
    h = Harness("PRECEDENT STATS HOLD-OUT SPLIT TEST SUITE")
    case_split(h)
    case_backward_compatible(h)
    h.summarize_and_exit()
