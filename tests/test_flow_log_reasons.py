#!/usr/bin/env python3
"""
Black-box tests for scripts/flow_log.py, the log.md attempt-start reader shared by the verifier.

Driven through its public functions (imported, not copied). Covers:
1. ISO_UTC_Z_RE accepts plain ASCII UTC timestamps and rejects non-ASCII digit characters.
2. newest_attempt_start returns the timestamp of the newest begin/retry line for the step.
3. When the bounded tail read cannot see the begin line (log truncated, no match), the detail
   result differs from a short log with no begin line at all.
4. A malformed newest line raises MalformedLineError; a missing log.md raises FlowLogError only, so
   the verifier can label the two differently.

Run with: python3 tests/test_flow_log_reasons.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_log  # noqa: E402

FILLER = "noise line padding for the tail read\n"


def test_iso_utc_z_regex_is_ascii_only(h):
    m = flow_log.ISO_UTC_Z_RE.fullmatch("2026-10-14T22:20:13Z")
    h.test_result("ISO_UTC_Z_RE accepts an ASCII UTC timestamp",
                  m is not None and m.group(0) == "2026-10-14T22:20:13Z",
                  f"match={m!r}")
    arabic_indic = "٢٠٢٦-١٠-١٤T١٠:٠٠:٠٠Z"
    fullwidth = "２０２６-１０-１４T１０:００:００Z"
    h.test_result("ISO_UTC_Z_RE rejects Arabic-Indic digits",
                  flow_log.ISO_UTC_Z_RE.fullmatch(arabic_indic) is None,
                  f"match={flow_log.ISO_UTC_Z_RE.fullmatch(arabic_indic)!r}")
    h.test_result("ISO_UTC_Z_RE rejects fullwidth digits",
                  flow_log.ISO_UTC_Z_RE.fullmatch(fullwidth) is None,
                  f"match={flow_log.ISO_UTC_Z_RE.fullmatch(fullwidth)!r}")


def test_newest_attempt_start_returns_newest_begin_or_retry(tmp, h):
    log = Path(tmp) / "log.md"
    log.write_text(
        "2026-10-20T10:00:00Z implement begin\n"
        "2026-10-20T10:30:00Z implement failed\n"
        "2026-10-20T11:00:00Z implement retry\n"
        "2026-10-20T11:30:00Z review begin\n"
    )
    got = flow_log.newest_attempt_start(log, "implement")
    h.test_result("newest_attempt_start: returns the newest implement begin/retry timestamp",
                  got == "2026-10-20T11:00:00Z", f"got={got!r}")


def test_truncated_tail_detail_differs_from_plain_miss(tmp, h):
    truncated = Path(tmp) / "truncated.md"
    lines = "2026-10-20T10:00:00Z implement begin\n" + FILLER * (3 * 1024 * 1024 // len(FILLER))
    truncated.write_text(lines)
    plain = Path(tmp) / "plain.md"
    plain.write_text("some unrelated line\n")

    detail_truncated = flow_log.newest_attempt_start_detail(truncated, "implement")
    detail_plain = flow_log.newest_attempt_start_detail(plain, "implement")
    h.test_result("truncated tail with no visible begin line: detail is (None, True)",
                  detail_truncated == (None, True), f"truncated={detail_truncated!r}")
    h.test_result("plain miss with no begin line: detail is (None, False)",
                  detail_plain == (None, False), f"plain={detail_plain!r}")


def test_malformed_line_and_missing_log_raise_distinct_errors(tmp, h):
    malformed = Path(tmp) / "malformed.md"
    malformed.write_text("2026-13-45T99:00:00Z implement begin\n")
    try:
        flow_log.newest_attempt_start_detail(malformed, "implement")
        raised = None
    except flow_log.FlowLogError as e:
        raised = e
    h.test_result("malformed newest line raises MalformedLineError",
                  isinstance(raised, flow_log.MalformedLineError), f"raised={raised!r}")

    missing = Path(tmp) / "missing.md"
    try:
        flow_log.newest_attempt_start_detail(missing, "implement")
        raised_missing = None
    except flow_log.FlowLogError as e:
        raised_missing = e
    h.test_result("missing log.md raises FlowLogError but not MalformedLineError",
                  raised_missing is not None and not isinstance(raised_missing, flow_log.MalformedLineError),
                  f"raised={raised_missing!r}")


def main():
    h = Harness("flow_log.py reasons and ISO UTC regex")
    test_iso_utc_z_regex_is_ascii_only(h)
    with tempfile.TemporaryDirectory() as tmp:
        test_newest_attempt_start_returns_newest_begin_or_retry(tmp, h)
    with tempfile.TemporaryDirectory() as tmp:
        test_truncated_tail_detail_differs_from_plain_miss(tmp, h)
    with tempfile.TemporaryDirectory() as tmp:
        test_malformed_line_and_missing_log_raise_distinct_errors(tmp, h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
