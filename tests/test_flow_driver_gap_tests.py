#!/usr/bin/env python3
"""
Gap tests for the layer 3 relay/assess/state code: the plain-answer and --item refusals (D6), relay.write_exclusive
(D8), shell quoting of the --item directive (D20), and the assess_gate expected-step check on load (D21).

Run with: python3 tests/test_flow_driver_gap_tests.py
"""

import copy
import errno
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import load_json, next_line, run_cli, state_of, write_receipt  # noqa: E402
from _test_harness import Harness  # noqa: E402
from test_flow_driver_assess_loop import ASSESS_SENTINEL, RECORD, to_landed_verify  # noqa: E402

from scripts.flow_driver import loop, relay  # noqa: E402
from scripts.flow_driver.state import StateSchemaError, load_v2_state  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fn(Path(tmp))
        except AssertionError as exc:
            h.test_result(name, False, str(exc) or "assertion failed")
        except Exception as exc:
            h.test_result(name, False, f"{type(exc).__name__}: {exc}")
        else:
            h.test_result(name, True)


def parked_assess(base: Path) -> Any:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    (flow / "steps" / "07-assess-records.txt").write_text(RECORD + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    _, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "ASK", f"setup: expected the batch ASK, got {verb} {err}")
    expect(state_of(flow)["parked"]["kind"] == "assess", "setup: assess batch not parked")
    return home, flow, env


# D6
def test_plain_answer_refused_with_exact_message(base: Path) -> None:
    _, flow, _ = parked_assess(base)
    try:
        loop.run_answer(flow, "Finish it")
    except loop.LoopRefusal as exc:
        expect(str(exc) == "answer refused: the assess batch is parked; answer each item with --item", str(exc))
    else:
        raise AssertionError("a plain answer to a parked assess batch was accepted")
    expect(not (flow / "answers" / "assess-1.json").exists(), "a refused plain answer wrote an answer file")


def test_item_refused_when_no_assess_batch_parked(base: Path) -> None:
    _, flow, _, _, _ = to_landed_verify(base, assess=False)
    expect(state_of(flow)["parked"] is None, "setup: something is parked")
    try:
        loop.run_answer(flow, "y", item="x")
    except loop.LoopRefusal as exc:
        expect(str(exc) == "answer refused: no assess batch is parked, so --item does not apply", str(exc))
    else:
        raise AssertionError("--item was accepted with no assess batch parked")


# D8
def test_write_exclusive_creates_file(base: Path) -> None:
    path = base / "a.json"
    relay.write_exclusive(path, "hello\n")
    expect(path.read_text(encoding="utf-8") == "hello\n", "content not written")


def test_write_exclusive_existing_untouched(base: Path) -> None:
    path = base / "a.json"
    path.write_text("original", encoding="utf-8")
    try:
        relay.write_exclusive(path, "new")
    except FileExistsError:
        pass
    else:
        raise AssertionError("an existing path did not raise FileExistsError")
    expect(path.read_text(encoding="utf-8") == "original", "the existing file was modified")


def test_write_exclusive_failed_write_unlinks_and_closes_once(base: Path) -> None:
    path = base / "a.json"
    fds: List[int] = []
    real_open = os.open
    close_calls: List[int] = []
    real_close = os.close

    def spy_open(p: Any, *a: Any, **k: Any) -> int:
        fd = real_open(p, *a, **k)
        fds.append(fd)
        return fd

    def spy_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    with patch.object(relay.os, "open", spy_open), patch.object(relay.os, "close", spy_close):
        try:
            relay.write_exclusive(path, 123)  # type: ignore[arg-type]  # a non-str makes the file write raise
        except TypeError:
            pass
        else:
            raise AssertionError("a failing write did not re-raise")
    expect(not path.exists(), "the partial file was not unlinked")
    expect(len(fds) == 1, f"expected one open, got {fds}")
    # The fd is closed (by the file object), and the code never closes it a second time itself.
    try:
        os.fstat(fds[0])
    except OSError as exc:
        expect(exc.errno == errno.EBADF, f"unexpected errno {exc.errno}")
    else:
        raise AssertionError("the fd is still open after the failure")
    expect(close_calls == [], f"write_exclusive called os.close itself (double-close risk): {close_calls}")


# D20
def test_item_directive_is_shell_quoted(base: Path) -> None:
    home, flow, env, _, _ = to_landed_verify(base, assess=True)
    item = "Step 3; echo 'x' $(id)"
    write_receipt(flow, "implement", 1, extra=f"OUTSTANDING: {item} (tests missing) [self-report]\n")
    record = RECORD.replace("implement:Step 3", f"implement:{item}")
    (flow / "steps" / "07-assess-records.txt").write_text(record + "\n" + ASSESS_SENTINEL + "\n", encoding="utf-8")
    run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched", extra_env=env)
    _, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "ASK", f"expected the batch ASK, got {verb} {out} {err}")
    command = str(load_json(path)["answer_command"])
    tokens = shlex.split(command)
    expect(tokens[-2] == "--item", f"--item is not the second-last token: {tokens}")
    expect(tokens[-1] == f"implement:{item}", f"the item did not survive as one token: {tokens}")


# D21
def _state_dict(base: Path) -> Any:
    _, flow, _ = parked_assess(base)
    return copy.deepcopy(state_of(flow))


def _with_gate_step(state: Any, step: str) -> Any:
    state["assess_gate"] = {"step": step, "attempt": 1, "nonce": "n0", "state": "spawned"}
    return state


def test_assess_gate_step_mismatch_raises(base: Path) -> None:
    state = _with_gate_step(_state_dict(base), "verify")
    try:
        load_v2_state(state)
    except StateSchemaError as exc:
        expect("assess_gate" in str(exc) and "assess" in str(exc), str(exc))
    else:
        raise AssertionError("an assess_gate with step=verify loaded")


def test_assess_gate_step_match_loads(base: Path) -> None:
    loaded = load_v2_state(_with_gate_step(_state_dict(base), "assess"))
    expect(loaded.assess_gate is not None and loaded.assess_gate.step == "assess", "a matching assess_gate did not load")


CHECKS = [
    ("1: D6 plain answer refused while assess batch is parked", test_plain_answer_refused_with_exact_message),
    ("2: D6 --item refused when no assess batch is parked", test_item_refused_when_no_assess_batch_parked),
    ("3: D8 write_exclusive creates the file", test_write_exclusive_creates_file),
    ("4: D8 write_exclusive leaves an existing file untouched", test_write_exclusive_existing_untouched),
    ("5: D8 failed write unlinks the partial file and closes the fd once", test_write_exclusive_failed_write_unlinks_and_closes_once),
    ("6: D20 --item directive is shell-quoted", test_item_directive_is_shell_quoted),
    ("7: D21 assess_gate step mismatch raises StateSchemaError", test_assess_gate_step_mismatch_raises),
    ("8: D21 assess_gate step match loads", test_assess_gate_step_match_loads),
]


def main() -> None:
    h = Harness("FLOW DRIVER GAP TESTS")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
