#!/usr/bin/env python3
"""
Loop-level test for `recover --choice reverify`: a PR whose head differs from the shipped head stops at the merge gate, reverify
re-reads the PR, and the merge gate is asked once the head matches.

Drives the real driver CLI under a temp HOME with a fake gh and fake step agents (no model runs).

Run with: python3 tests/test_flow_driver_recover_loop.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _flow_driver_loop_helpers import FakeGh, directive_text, drive_happy_path, make_home, next_line, run_cli, start_flow, state_of  # noqa: E402
from _test_harness import Harness  # noqa: E402

OTHER_HEAD = "b" * 40


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


def test_reverify_reasks_the_merge_gate(base: Path) -> None:
    home = make_home(base)
    gh = FakeGh(base, mode="nopr")
    env = gh.env()
    flow, first = start_flow(home, extra_env=env)
    head = subprocess.run(["git", "-C", str(home / "wt"), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    def open_pr(sha: str) -> None:
        gh.json_file.write_text(json.dumps({"number": 7, "state": "OPEN", "headRefOid": sha}), encoding="utf-8")
        gh.mode = "open"
        env.update(gh.env())

    verb, _, _ = drive_happy_path(home, flow, first, env, before_next={"ship": lambda: open_pr(OTHER_HEAD)})
    state = state_of(flow)
    expect(verb == "STOP" and state["stop_reason"]["ship"].startswith("merge-head-mismatch"), f"expected a head-mismatch STOP: {verb} {state['stop_reason']}")
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "reverify", extra_env=env)
    expect(code == 0, f"recover reverify exited {code}: {err}")
    expect(state_of(flow)["status"] == "running", "reverify did not reopen the flow")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, _ = next_line(out)
    expect(verb == "STOP", f"the PR is still on the other head, so the re-read must stop again, got {verb}")
    open_pr(head)  # the PR now matches the shipped head
    code, _, err = run_cli(home, "recover", "--flow", str(flow), "--choice", "reverify", extra_env=env)
    expect(code == 0, f"second reverify exited {code}: {err}")
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
    verb, path = next_line(out)
    expect(verb == "ASK" and "merge-gate" in directive_text(path), f"the merge gate was not asked after the re-read: {verb}")
    expect(state_of(flow)["merge_gate"]["approved_head"] == head, "the gate was recorded for a head other than the matching one")


def test_retried_merge_archives_the_consumed_gate_answer(base: Path) -> None:
    """A merge that failed after the human approved is retried: the consumed merge-gate answer is archived and the approval record cleared, so the retry asks again."""
    import dataclasses

    from scripts.flow_driver.loop import run_recover
    from scripts.flow_driver.state import InitSpec, MergeGateRecord, PendingSpawn, Sha, init_state, mutating

    flow = base / "flow"
    flow.mkdir()
    spec = InitSpec(repo="o/n", issue=1, worktree=str(base))
    with mutating(flow, create=spec) as locked:
        init_state(locked, spec)
        state = locked.require_state()
        locked.commit(
            dataclasses.replace(
                state,
                step="merge",  # type: ignore[arg-type]
                status="failed",  # type: ignore[arg-type]
                attempts={"merge": 1},  # type: ignore[dict-item]
                pending_spawn=PendingSpawn(step="merge", attempt=1, nonce="n0", state="receipt-landed", receipt_seen=None),  # type: ignore[arg-type]
                stop_reason={"merge": "step-failed: the merge failed"},  # type: ignore[dict-item]
                stop_source={"merge": "receipt"},  # type: ignore[dict-item]
                merge_gate=MergeGateRecord(pr_number=7, approved_head=Sha("a" * 40), repo="o/n"),
            )
        )
    landed = flow / "archive" / "merge" / "a1.landed.md"
    landed.parent.mkdir(parents=True)
    landed.write_text("ATTEMPT: 1\nDecision: FAILED\nReason: merge failed\n<!-- step-end -->\nbody\n", encoding="utf-8")
    answer = flow / "answers" / "merge-a1-merge-gate.json"
    answer.parent.mkdir(parents=True)
    answer.write_text(json.dumps({"text": "Merge"}), encoding="utf-8")
    run_recover(flow, "retry")
    state = json.loads((flow / "state.json").read_text(encoding="utf-8"))
    expect(state["merge_gate"] is None, "the approval record survived the retry, so the retried merge would not ask again")
    expect(not answer.exists(), "the consumed merge-gate answer was left in place, so the retried merge would be auto-answered")
    archived = sorted(p.name for p in (flow / "archive").rglob("*.answer-*.json"))
    expect(len(archived) == 1, f"the consumed answer is not archived exactly once: {archived}")



CHECKS = [
    ("1: reverify re-reads the PR and asks the merge gate once the head matches", test_reverify_reasks_the_merge_gate),
    ("2: a retried merge archives the consumed gate answer", test_retried_merge_archives_the_consumed_gate_answer),
]


def main() -> None:
    h = Harness("FLOW DRIVER RECOVER LOOP")
    for name, fn in CHECKS:
        check(h, name, fn)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
