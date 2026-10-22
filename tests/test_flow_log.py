#!/usr/bin/env python3
"""Tests for scripts/flow_log.py (shared log.md attempt-start reader and ISO-8601 parser, #316).

Covers: newest-line selection in file order, the anchored line grammar, fail-closed cases (malformed,
naive, CRLF, NUL, future-dated newest line), the 1 MiB tail cap, the step-name guard, the flow-dir
validation rule, the parse_iso8601 contract, and that implement-ledger.py and flow-verify-receipt.py
still import flow_log when started through a symlink from a directory with no flow_log.py beside it.

Every subprocess runs with CLAUDE_CODE_SESSION_ID removed and HOME pointed at a temp dir.

Run with: python3 tests/test_flow_log.py
"""

import calendar
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT

FLOW_LOG = REPO_ROOT / "scripts" / "flow_log.py"
LEDGER = REPO_ROOT / "scripts" / "implement-ledger.py"
VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
_spec = importlib.util.spec_from_file_location("flow_log", FLOW_LOG)
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)

NOW = int(time.time())
MODULE_ERRORS = ("ModuleNotFoundError", "ImportError", "No module named", "flow_log module unavailable", "Traceback")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _newest(text, step="implement"):
    """Write text to a temp log.md and return newest_attempt_start(log, step)."""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.md"
        log.write_text(text)
        return fl.newest_attempt_start(log, step)


def _raises_flow_log_error(text, step="implement"):
    try:
        _newest(text, step)
    except fl.FlowLogError:
        return True
    return False


def _clean_env(home):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    env["IMPLEMENT_LEDGER_STATE_DIR"] = str(Path(home) / "state")
    return env


def _combined(proc):
    return (proc.stdout or "") + (proc.stderr or "")


def test_newest_of_several_matching_lines_is_last_in_file_order():
    """Among begin/retry lines for the step, the last one in file order wins; other events and steps are ignored."""
    t = [NOW - 500, NOW - 400, NOW - 300, NOW - 200, NOW - 100]
    text = (f"{_iso(t[0])} implement begin\n"
            f"{_iso(t[1])} implement failed\n"
            f"{_iso(t[2])} implement retry\n"
            f"{_iso(t[3])} fix begin\n"
            f"{_iso(t[4])} implement awaiting\n")
    got = _newest(text)
    return got == _iso(t[2]), f"got={got!r} expected={_iso(t[2])!r}"


def test_last_in_file_order_beats_larger_timestamp():
    """Newest means last in file order (log.md is append-only), not the largest timestamp."""
    later, earlier = _iso(NOW - 10), _iso(NOW - 200)
    got = _newest(f"{later} implement begin\n{earlier} implement retry\n")
    return got == earlier, f"got={got!r} expected={earlier!r}"


def test_empty_log_and_no_matching_line_return_none():
    """An empty log, or a log with no begin/retry line for the step, returns None (no exception)."""
    empty = _newest("")
    other = _newest(f"{_iso(NOW - 10)} fix begin\n{_iso(NOW - 5)} implement awaiting\n")
    return empty is None and other is None, f"empty={empty!r} other={other!r}"


def test_byte_cap_finds_line_inside_the_tail():
    """A matching line within the last LOG_READ_CAP_BYTES of a log over the cap is found."""
    cap = fl.LOG_READ_CAP_BYTES
    ts = _iso(NOW - 60)
    filler = "filler line without any marker\n" * ((2 * cap) // len("filler line without any marker\n") + 1)
    got = _newest(filler + f"{ts} implement begin\n")
    return cap == 1_048_576 and got == ts, f"cap={cap} got={got!r} expected={ts!r}"


def test_byte_cap_ignores_line_before_the_tail():
    """A matching line that sits more than LOG_READ_CAP_BYTES from the end is outside the read window: None."""
    cap = fl.LOG_READ_CAP_BYTES
    ts = _iso(NOW - 60)
    filler = "filler line without any marker\n" * ((2 * cap) // len("filler line without any marker\n") + 1)
    got = _newest(f"{ts} implement begin\n" + filler)
    return got is None, f"got={got!r}"


def test_invalid_step_names_raise():
    """Step names outside [a-z][a-z-]* raise FlowLogError; valid names do not."""
    bad = ["Implement", "implement;rm", "", "fix begin", "9lives", "round sizing"]
    bad_raised = [s for s in bad if not _raises_flow_log_error("", s)]
    good_ok = _newest("", "implement") is None and _newest("", "round-sizing") is None
    return not bad_raised and good_ok, f"not raised for={bad_raised}; valid names ok={good_ok}"


def test_non_regular_or_missing_log_raises():
    """A log.md that is a directory, or a missing log.md, raises FlowLogError."""
    with tempfile.TemporaryDirectory() as td:
        as_dir = Path(td) / "as-dir"
        as_dir.mkdir(parents=True)
        (as_dir / "log.md").mkdir()
        missing = Path(td) / "missing"
        missing.mkdir()
        raised_dir = raised_missing = False
        try:
            fl.newest_attempt_start(as_dir / "log.md", "implement")
        except fl.FlowLogError:
            raised_dir = True
        try:
            fl.newest_attempt_start(missing / "log.md", "implement")
        except fl.FlowLogError:
            raised_missing = True
    return raised_dir and raised_missing, f"dir_raises={raised_dir} missing_raises={raised_missing}"


def test_malformed_newest_line_does_not_fall_back():
    """A newest matching line with a malformed timestamp raises; the older valid line is never used."""
    text = f"{_iso(NOW - 100)} implement begin\n2026-13-45T99:00:00Z implement begin\n"
    return _raises_flow_log_error(text), "malformed newest line did not raise"


def test_naive_newest_line_raises():
    """A newest matching line without a Z suffix raises (naive timestamps are not accepted from the log)."""
    text = f"{_iso(NOW - 100)} implement begin\n2026-10-19T15:58:14 implement begin\n"
    return _raises_flow_log_error(text), "naive newest line did not raise"


def test_crlf_newest_line_raises():
    """A CRLF-terminated newest begin line (trailing \\r) raises rather than falling back."""
    text = f"{_iso(NOW - 100)} implement begin\n{_iso(NOW - 50)} implement begin\r\n"
    return _raises_flow_log_error(text), "CRLF newest line did not raise"


def test_nul_byte_in_newest_candidate_raises():
    """A NUL byte inside the newest candidate's first field raises rather than falling back."""
    text = f"{_iso(NOW - 100)} implement begin\n{_iso(NOW - 50)}\x00 implement begin\n"
    return _raises_flow_log_error(text), "NUL-bearing newest candidate did not raise"


def test_trailing_nul_on_newest_candidate_raises():
    """A NUL byte after a valid newest begin line (trailing, not mid-token) raises rather than falling back to the older line."""
    text = f"{_iso(NOW - 2 * 3600)} implement begin\n{_iso(NOW - 30)} implement begin\x00\n"
    return _raises_flow_log_error(text), "trailing-NUL newest candidate did not raise (fell back to an older line?)"


def test_symlinked_log_md_raises():
    """A log.md that is a symlink to a valid log raises on its own (newest_attempt_start does not rely on validate_flow_dir)."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "real-log.md"
        target.write_text(f"{_iso(NOW - 60)} implement begin\n")
        link = Path(td) / "log.md"
        os.symlink(target, link)
        raised = False
        try:
            fl.newest_attempt_start(link, "implement")
        except fl.FlowLogError:
            raised = True
    return raised, "symlinked log.md was read instead of refused"


def test_future_dated_newest_line_raises():
    """A newest line later than now plus the skew allowance raises (fail closed)."""
    text = f"{_iso(NOW - 100)} implement begin\n{_iso(NOW + 3600)} implement begin\n"
    return _raises_flow_log_error(text), "future-dated newest line did not raise"


def test_loose_non_matches_are_ignored():
    """Lines that do not fullmatch the anchored grammar (extra token, leading space, other step) do not count."""
    t = _iso(NOW - 100)
    text = (f"{t} implement begin\n"
            f"{_iso(NOW - 1)} implement begin extra\n"
            f" {_iso(NOW - 1)} implement begin\n"
            f"{_iso(NOW - 1)} implementx begin\n"
            f"{_iso(NOW - 1)} fix begin\n")
    got = _newest(text, "implement")
    return got == t, f"got={got!r} expected={t!r}"


def test_parse_iso8601_contract():
    """parse_iso8601: Z and +00:00 agree, naive is UTC, garbage raises ValueError."""
    expected = calendar.timegm((2026, 10, 19, 15, 58, 14, 0, 0, 0))
    got_z = fl.parse_iso8601("2026-10-19T15:58:14Z")
    got_off = fl.parse_iso8601("2026-10-19T15:58:14+00:00")
    got_naive = fl.parse_iso8601("2026-10-19T15:58:14")
    raised = []
    for bad in ("garbage", "2026-13-45T99:00:00Z", ""):
        try:
            fl.parse_iso8601(bad)
        except ValueError:
            continue
        raised.append(bad)
    ok = got_z == expected and got_off == expected and got_naive == expected and not raised
    return ok, f"Z={got_z} offset={got_off} naive={got_naive} expected={expected} no_raise_for={raised}"


def test_validate_flow_dir_contract():
    """validate_flow_dir accepts a real flow dir under $HOME/.claude/flows and refuses relative, outside, and symlinked-log cases."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        flows = home / ".claude" / "flows"
        flow = flows / "f1"
        flow.mkdir(parents=True)
        (flow / "log.md").write_text("")
        outside = home / "escape-target"
        outside.mkdir(parents=True)
        (outside / "log.md").write_text("")
        linked = flows / "linked"
        linked.mkdir()
        (linked / "log.md").symlink_to(flow / "log.md")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            accepted = Path(fl.validate_flow_dir(str(flow))).resolve() == flow.resolve()
            refused = []
            for label, arg in (("relative", ".claude/flows/f1"), ("outside", str(outside)), ("symlinked log", str(linked))):
                try:
                    fl.validate_flow_dir(arg)
                except fl.FlowLogError:
                    continue
                refused.append(label)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
    return accepted and not refused, f"accepted={accepted} not_refused={refused}"


def test_symlinked_implement_ledger_imports_flow_log():
    """implement-ledger.py run through a symlink in a dir with no flow_log.py still imports it (no module error)."""
    with tempfile.TemporaryDirectory() as td:
        bin_dir = Path(td) / "bin"
        bin_dir.mkdir()
        link = bin_dir / "implement-ledger.py"
        os.symlink(LEDGER, link)
        home = Path(td) / "home"
        flow = home / ".claude" / "flows" / "f"
        flow.mkdir(parents=True)
        (flow / "log.md").write_text(f"{_iso(NOW - 60)} implement begin\n")
        repo = Path(td) / "repo"
        repo.mkdir()
        r = subprocess.run(
            [sys.executable, str(link), "locate", "--repo", str(repo), "--flow-dir", str(flow), "--step", "implement"],
            capture_output=True, text=True, env=_clean_env(home), cwd=str(repo), timeout=120,
        )
        out = _combined(r)
        hit = [m for m in MODULE_ERRORS if m in out]
        return not hit and r.returncode in (0, 3), f"rc={r.returncode} module_markers={hit} output={out[:300]!r}"


def test_symlinked_verifier_imports_flow_log():
    """flow-verify-receipt.py run through a symlink in a dir with no flow_log.py still imports it."""
    with tempfile.TemporaryDirectory() as td:
        bin_dir = Path(td) / "bin"
        bin_dir.mkdir()
        link = bin_dir / "flow-verify-receipt.py"
        os.symlink(VERIFIER, link)
        home = Path(td) / "home"
        flow = home / ".claude" / "flows" / "f"
        flow.mkdir(parents=True)
        (flow / "log.md").write_text(f"{_iso(NOW - 60)} implement begin\n")
        receipt = Path(td) / "receipt.md"
        receipt.write_text("Decision: OK\n<!-- step-end -->\n")
        r = subprocess.run(
            [sys.executable, str(link), "--step", "implement", "--receipt", str(receipt),
             "--since-from-log", "--flow-dir", str(flow)],
            capture_output=True, text=True, env=_clean_env(home), cwd=td, timeout=120,
        )
        out = _combined(r)
        hit = [m for m in MODULE_ERRORS if m in out]
        return not hit, f"rc={r.returncode} module_markers={hit} output={out[:300]!r}"


def test_iso_regex_is_ascii_only():
    """ISO_UTC_Z_RE accepts ASCII digits only; non-ASCII decimal digits (fullwidth, Arabic-Indic) do not match."""
    ascii_ok = fl.ISO_UTC_Z_RE.fullmatch("2026-10-19T15:58:14Z") is not None
    fullwidth = "２０２６-10-19T15:58:14Z"
    arabic_indic = "٢٠٢٦-10-19T15:58:14Z"
    non_ascii_rejected = fl.ISO_UTC_Z_RE.fullmatch(fullwidth) is None and fl.ISO_UTC_Z_RE.fullmatch(arabic_indic) is None
    return ascii_ok and non_ascii_rejected, f"ascii_ok={ascii_ok} non_ascii_rejected={non_ascii_rejected}"


def test_detail_reports_window_cut_only_for_truncated_miss():
    """newest_attempt_start_detail flags a miss as window_cut only when the log exceeds the cap; a small log miss is not flagged."""
    cap = fl.LOG_READ_CAP_BYTES
    filler = "filler line without any marker\n" * ((2 * cap) // len("filler line without any marker\n") + 1)
    with tempfile.TemporaryDirectory() as td:
        big = Path(td) / "big.md"
        big.write_text(filler)
        small = Path(td) / "small.md"
        small.write_text(f"{_iso(NOW - 60)} fix begin\n")
        big_result = fl.newest_attempt_start_detail(big, "implement")
        small_result = fl.newest_attempt_start_detail(small, "implement")
    return big_result == (None, True) and small_result == (None, False), f"big={big_result!r} small={small_result!r}"


def test_detail_returns_stamp_with_no_window_cut_when_found():
    """newest_attempt_start_detail returns the stamp and window_cut False when a matching line is in the window."""
    ts = _iso(NOW - 60)
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "log.md"
        log.write_text(f"{ts} implement retry\n")
        got = fl.newest_attempt_start_detail(log, "implement")
    return got == (ts, False), f"got={got!r} expected={(ts, False)!r}"


TESTS = [
    ("newest matching line is the last in file order", test_newest_of_several_matching_lines_is_last_in_file_order),
    ("file order beats a larger timestamp", test_last_in_file_order_beats_larger_timestamp),
    ("empty log and no matching line return None", test_empty_log_and_no_matching_line_return_none),
    ("byte cap: matching line inside the 1 MiB tail is found", test_byte_cap_finds_line_inside_the_tail),
    ("byte cap: matching line before the tail is not read", test_byte_cap_ignores_line_before_the_tail),
    ("invalid step names raise FlowLogError", test_invalid_step_names_raise),
    ("non-regular or missing log.md raises", test_non_regular_or_missing_log_raises),
    ("malformed newest line raises, no fallback", test_malformed_newest_line_does_not_fall_back),
    ("naive newest line raises", test_naive_newest_line_raises),
    ("CRLF newest line raises", test_crlf_newest_line_raises),
    ("NUL byte in newest candidate raises", test_nul_byte_in_newest_candidate_raises),
    ("trailing NUL on newest candidate raises, no fallback", test_trailing_nul_on_newest_candidate_raises),
    ("symlinked log.md raises", test_symlinked_log_md_raises),
    ("future-dated newest line raises", test_future_dated_newest_line_raises),
    ("loose non-matches are ignored", test_loose_non_matches_are_ignored),
    ("parse_iso8601 contract", test_parse_iso8601_contract),
    ("validate_flow_dir contract", test_validate_flow_dir_contract),
    ("symlinked implement-ledger.py imports flow_log", test_symlinked_implement_ledger_imports_flow_log),
    ("symlinked flow-verify-receipt.py imports flow_log", test_symlinked_verifier_imports_flow_log),
    ("ISO_UTC_Z_RE rejects non-ASCII digits", test_iso_regex_is_ascii_only),
    ("detail: window cut flagged only for a truncated miss", test_detail_reports_window_cut_only_for_truncated_miss),
    ("detail: found stamp has no window cut", test_detail_returns_stamp_with_no_window_cut_when_found),
]


if __name__ == "__main__":
    h = Harness("FLOW-LOG TEST SUITE")

    for name, fn in TESTS:
        try:
            passed, msg = fn()
        except Exception:
            passed, msg = False, f"exception: {traceback.format_exc(limit=3)}"
        h.test_result(name, passed, msg if not passed else "")

    h.summarize_and_exit()
