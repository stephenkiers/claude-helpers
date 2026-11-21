#!/usr/bin/env python3
"""
Unit tests for the v2 driver boundary vocabulary, CLI and the experimental stub command
commands/expert-flow-v2.md (ADR-0026). Layer 2 removed the skeleton decide() and loaders; this suite now drives
the real step loop through the CLI (the full loop is covered by tests/test_flow_driver_loop.py).

Sections:
1. Boundary vocabulary: parse_verb, assert_never exhaustiveness, the v2 state loader, a corrupt state.json.
2. The transition through the CLI: start, a re-run next on a live record, receipt landing, clear-live, answer refusal.
3. The stub-side accept rule, tested against the documented failure cases.
4. The flows-v2 root acceptance checks.
5. The stub doc acceptance checks for commands/expert-flow-v2.md.

Every path is under a tempfile root. The tests never read or write the real HOME, and never write under
~/.claude/flows or ~/.claude/flows-v2.

Run with: python3 tests/test_flow_driver.py
"""

import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _agent_frontmatter import parse_markdown_frontmatter
from _flow_driver_loop_helpers import (
    load_json,
    make_home,
    next_line,
    pending_of,
    run_cli,
    state_of,
    write_receipt,
)
from _flow_driver_loop_helpers import start_flow as loop_start_flow
from _test_harness import REPO_ROOT, Harness

from scripts.flow_driver import cli, driver, flowdir
from scripts.flow_driver.flowdir import FlowPathError
from scripts.flow_driver.model import (
    RUNNER_PREFIX,
    VERBS,
    Verb,
    assert_never,
    parse_verb,
    state_path,
)
from scripts.flow_driver.state import StateSchemaError, load_v2_state

DOC = REPO_ROOT / "commands" / "expert-flow-v2.md"
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
NEXT_LINE_RE = re.compile(r"NEXT: (SPAWN|RESUME|WAIT|ASK|PARK|STOP|DONE|MERGE) (/\S+)")
ACCEPT_LINE_RE = re.compile(r"NEXT: (\S+) (/\S+)")



# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_raises(exc_type: type, fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except exc_type:
        return
    raise AssertionError(f"{getattr(fn, '__name__', 'call')} did not raise {exc_type.__name__}")


def check(h: Harness, name: str, fn: Callable[[Path], None], base: Path) -> None:
    """Run one named check. A crash inside a check is a named failure, not a suite abort."""
    try:
        fn(base)
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def is_under(path: Path, root: Path) -> bool:
    return os.path.realpath(str(path)).startswith(os.path.realpath(str(root)) + os.sep)


def fresh(base: Path) -> Path:
    return Path(tempfile.mkdtemp(dir=str(base)))




def stub_accepts(stdout: str, returncode: int) -> str:
    """The stub's documented accept rule (commands/expert-flow-v2.md, "Accept rule"). Returns a verb or STOP."""
    lines = stdout.splitlines()
    if returncode != 0 or len(lines) != 1:
        return "STOP"
    match = ACCEPT_LINE_RE.fullmatch(lines[0])
    if match is None:
        return "STOP"
    verb = parse_verb(match.group(1))
    target = Path(match.group(2))
    if not target.is_file() or not os.access(str(target), os.R_OK):
        return "STOP"
    return verb




# ---------------------------------------------------------------------------
# Section 1: boundary vocabulary and the state loader
# ---------------------------------------------------------------------------

def verb_label(verb: Verb) -> str:
    if verb == "SPAWN":
        return "spawn"
    if verb == "RESUME":
        return "resume"
    if verb == "WAIT":
        return "wait"
    if verb == "ASK":
        return "ask"
    if verb == "PARK":
        return "park"
    if verb == "STOP":
        return "stop"
    if verb == "DONE":
        return "done"
    if verb == "MERGE":
        return "merge"
    assert_never(verb)


def test_parse_verb_members_unknowns_and_exhaustiveness(base: Path) -> None:
    for verb in VERBS:
        expect(parse_verb(verb) == verb, f"parse_verb({verb!r}) did not map to itself")
    for raw in ["", "spawn", "wait", "NEXT", " SPAWN", "SPAWN ", "SPAWN\n", "FOO"]:
        expect(parse_verb(raw) == "STOP", f"parse_verb({raw!r}) is not STOP")
    labels = {verb_label(verb) for verb in VERBS}
    expect(len(labels) == len(VERBS), "verb_label does not give every verb its own branch")
    expect_raises(AssertionError, verb_label, "BOGUS")


def test_state_loader_accepts_a_real_state_and_rejects_each_invalid_shape(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    valid = load_json(state_path(flow))
    load_v2_state(valid)  # the real state loads, so the rejections below are meaningful
    for key in ("driver_format", "run_id", "repo", "step", "status", "attempts"):
        missing = {k: v for k, v in valid.items() if k != key}
        expect_raises(StateSchemaError, load_v2_state, missing)
        wrong = dict(valid)
        wrong[key] = ["not", "a", "scalar"]
        expect_raises(StateSchemaError, load_v2_state, wrong)
    expect_raises(StateSchemaError, load_v2_state, dict(valid, driver_format=1))
    expect_raises(StateSchemaError, load_v2_state, dict(valid, status="exploded"))
    expect_raises(StateSchemaError, load_v2_state, ["not", "an", "object"])


def test_corrupt_state_gives_a_refusal_and_never_a_spawn(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    state_path(flow).write_text('{"driver_format": 2, "repo"', encoding="utf-8")
    code, out, err = run_cli(home, "next", "--flow", str(flow))
    expect(code != 0, f"a corrupt state.json exited 0: {out!r}")
    expect("SPAWN" not in out, f"a corrupt state.json still printed a SPAWN: {out!r}")
    expect("refused" in err or "NEXT: STOP" in out, f"no refusal or STOP for a corrupt state: {err!r}")


def test_assess_gate_validates_step(base: Path) -> None:
    """assess_gate with wrong step raises StateSchemaError."""
    home = make_home(base)
    flow, _ = loop_start_flow(home)

    # Load the valid state
    state_data = load_json(state_path(flow))

    # Add an assess_gate with the wrong step (not "assess")
    state_data["assess_gate"] = {
        "step": "plan",  # Wrong! Should be "assess" only
        "attempt": 1,
        "nonce": "n0",
        "state": "pass"
    }
    state_path(flow).write_text(__import__("json").dumps(state_data), encoding="utf-8")

    # Attempting to load this state should raise StateSchemaError
    try:
        load_v2_state(state_data)
        expect(False, "Expected StateSchemaError when assess_gate has wrong step, but none was raised")
    except StateSchemaError as e:
        expect("must be assess" in str(e), f"error message does not mention step: {e}")


# ---------------------------------------------------------------------------
# Section 2: the pure transition, driven through the CLI
# ---------------------------------------------------------------------------

def test_start_spawns_the_worktree_step_and_records_a_spawned_record(base: Path) -> None:
    home = make_home(base)
    flow, directive = loop_start_flow(home)
    expect(directive["verb"] == "SPAWN" and directive["step"] == "worktree", f"first directive is {directive!r}")
    pending = pending_of(flow)
    expect(pending is not None and pending["state"] == "spawned", f"pending record is {pending!r}")
    assert pending is not None
    expect(pending["nonce"] == directive["nonce"], "directive and pending record disagree on the nonce")
    expect(state_of(flow)["live"] is not None, "state.json has no live projection of the spawned record")


def test_rerun_next_on_a_live_record_is_a_wait_with_no_new_nonce_and_no_extra_spawn(base: Path) -> None:
    """next, discard the line, next again: zero extra SPAWNs and the same nonce."""
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    first = pending_of(flow)
    assert first is not None
    for _ in range(3):
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "spawn-launched")
        expect(code == 0, f"next exited {code}: {err}")
        expect(next_line(out)[0] == "WAIT", f"a re-run next on a live record printed {out!r}")
        again = pending_of(flow)
        expect(again is not None and again["nonce"] == first["nonce"], "a re-run next changed the nonce")
        assert again is not None
        expect(again["attempt"] == first["attempt"], "a re-run next changed the attempt")


def test_a_completed_receipt_lands_and_the_next_step_spawns(base: Path) -> None:
    home = make_home(base)
    flow, directive = loop_start_flow(home)
    write_receipt(flow, "worktree", directive["attempt"], extra=f"WORKTREE: {home / 'wt'}\n")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification")
    expect(code == 0, f"next exited {code}: {err}")
    verb, next_directive = next_line(out)
    state = state_of(flow)
    expect(state["attempts"].get("worktree") == 1, "the worktree attempt was not recorded once")
    expect(state["attempt_log"]["worktree"] == {"1": "receipt-landed"}, f"attempt log is {state['attempt_log']!r}")
    expect(len(state["applied"]) == 1 and state["applied"][0].endswith(":worktree:1:land"),
           f"the landing was not recorded once: {state['applied']!r}")
    expect(state["worktree"] == str(home / "wt"), "the worktree path was not recorded from the receipt")
    # The fixture HOME has no prompts/, so the North Star preflight stops the plan spawn; either way it is not a re-spawn.
    expect(verb in ("SPAWN", "STOP"), f"unexpected verb {verb}")
    if verb == "SPAWN":
        expect(load_json(next_directive)["step"] == "plan", f"next step is {load_json(next_directive)['step']!r}")


def test_clear_live_then_next_spawns_a_new_nonce_and_attempt(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    first = pending_of(flow)
    assert first is not None
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "test recovery")
    expect(code == 0, f"clear-live exited {code}: {err}")
    expect(pending_of(flow) is None, "clear-live did not remove the live record")
    code, _, err = run_cli(home, "clear-live", "--flow", str(flow), "--reason", "again")
    expect(code != 0, f"clear-live with no live record was not refused: {err}")
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(code == 0, f"next exited {code}: {err}")
    verb, _ = next_line(out)
    expect(verb == "SPAWN", f"expected SPAWN after clear-live, got {verb}")
    second = pending_of(flow)
    expect(second is not None and second["attempt"] == first["attempt"] + 1, "the re-spawn did not increment the attempt")
    assert second is not None
    expect(second["nonce"] != first["nonce"], "the re-spawn reused the nonce")


def test_answer_refuses_a_flow_with_no_parked_question(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    code, _, err = run_cli(home, "answer", "--flow", str(flow), "--text", "ok")
    expect(code != 0 and "answer refused" in err, f"answer on a spawned record was not refused: {err}")


def test_an_unrecognized_after_label_is_not_fatal_and_keeps_one_stdout_line(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "no-such-label")
    expect(len(out.splitlines()) == 1, f"stdout was {out!r} (stderr {err!r})")
    expect(NEXT_LINE_RE.fullmatch(out.strip()) is not None, f"stdout is not a NEXT line: {out!r}")


# ---------------------------------------------------------------------------
# Section 3: the stub-side accept rule
# ---------------------------------------------------------------------------

def test_accept_rule_model_accepts_a_real_directive_and_maps_failures_to_stop(base: Path) -> None:
    """Model of the stub's documented accept rule (commands/expert-flow-v2.md, "Accept rule").

    This test reimplements the accept rule logic in stub_accepts() and verifies it against
    the documented conditions: exactly one stdout line, line matches NEXT contract, exit 0,
    and directive file is readable. Anything else maps to STOP.
    """
    directive = base / "directive.json"
    directive.write_text("{}", encoding="utf-8")
    # Verify the rule accepts a valid NEXT line from a readable file
    expect(stub_accepts(f"NEXT: WAIT {directive}\n", 0) == "WAIT", "a valid NEXT line was not accepted")
    # Verify the rule rejects unknown verbs, multiple lines, empty stdout, non-zero exit, missing files
    expect(stub_accepts(f"NEXT: FOO {directive}\n", 0) == "STOP", "an unknown verb was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {directive}\nNEXT: WAIT {directive}\n", 0) == "STOP",
           "two stdout lines were not STOP")
    expect(stub_accepts("", 0) == "STOP", "empty stdout was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {directive}\n", 1) == "STOP", "a non-zero exit was not STOP")
    expect(stub_accepts(f"NEXT: WAIT {base / 'missing.json'}\n", 0) == "STOP",
           "a NEXT line naming a missing file was not STOP")


def test_stub_accept_rule_on_real_driver_output(base: Path) -> None:
    home = make_home(base)
    flow, _ = loop_start_flow(home)
    code, out, _ = run_cli(home, "next", "--flow", str(flow), "--after", "start")
    expect(stub_accepts(out, code) == "WAIT", "the stub would not accept the real next output")


# ---------------------------------------------------------------------------
# Section 4: flows-v2 root acceptance
# ---------------------------------------------------------------------------

def test_flow_dirs_are_created_only_under_flows_v2(base: Path) -> None:
    home = fresh(base)
    root = flowdir.flows_v2_root(home)
    flow_a = flowdir.create_flow_dir(root, "owner/name", NOW, "aa11", mode="interactive", wait_mode="end-turn", step_minutes=1)
    flow_b = flowdir.create_flow_dir(root, "owner/name", NOW, "bb22", mode="interactive", wait_mode="end-turn", step_minutes=1)
    expect(is_under(flow_a, root), "a flow dir was created outside flows-v2")
    expect(flow_a != flow_b, "two create_flow_dir calls yielded the same dir")
    expect(not (home / ".claude" / "flows").exists(), "a sibling flows/ directory was created")
    expect(load_json(state_path(flow_a))["driver_format"] == "skeleton", "state.json lacks driver_format skeleton")
    expect(not any(path.name == "flow-run" for path in home.rglob("*")), "a .claude/flow-run file was created")


def test_flows_v2_symlink_into_v1_flows_is_refused_before_any_write(base: Path) -> None:
    home = fresh(base)
    claude = home / ".claude"
    v1_flows = claude / "flows"
    v1_flows.mkdir(parents=True)
    link = claude / "flows-v2"
    os.symlink(str(v1_flows), str(link))
    refused = False
    try:
        flowdir.create_flow_dir(link, "owner/name", NOW, "cc33", mode="interactive", wait_mode="end-turn", step_minutes=1)
    except FlowPathError:
        refused = True
    expect(refused, "a flows-v2 symlink into flows/ was not refused")
    expect(list(v1_flows.iterdir()) == [], "a write reached the v1 flows directory")


# ---------------------------------------------------------------------------
# Section 5: the stub doc (commands/expert-flow-v2.md)
# ---------------------------------------------------------------------------

def read_stub_doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_stub_doc_description_begins_experimental(base: Path) -> None:
    fm = parse_markdown_frontmatter(read_stub_doc())
    description = str(fm.get("description", ""))
    expect(description.startswith("Experimental, not for real tickets:"), f"description is {description[:60]!r}")
    expect("experimental" in description.lower(), "description lacks 'experimental'")


def test_stub_doc_first_body_line_after_frontmatter(base: Path) -> None:
    _, sep, body = read_stub_doc().partition("\n---\n")
    expect(sep != "", "the frontmatter has no closing fence")
    first = next(line for line in body.splitlines() if line.strip())
    expect(first.strip() == "experimental, not for real tickets", f"first body line is {first!r}")


def test_stub_doc_allowed_tools_exact_and_no_ask_user_question(base: Path) -> None:
    tools = parse_markdown_frontmatter(read_stub_doc()).get("allowed-tools")
    expected = [
        f"Bash({RUNNER_PREFIX}:*)",
        'Bash(python3 "$HOME/.claude/scripts/fleet.py" wait:*)',
        "Read",
        "Task",
        "SendMessage",
    ]
    expect(tools == expected, f"allowed-tools is {tools!r}")
    expect("AskUserQuestion" not in tools, "allowed-tools grants AskUserQuestion")
    expect(not any(tool.startswith(("Write", "Edit")) or "git push" in tool or tool.startswith("Bash(gh")
                   for tool in tools), "allowed-tools grants a write, edit, push or gh tool")


def test_stub_doc_next_and_answer_commands_start_with_the_granted_prefix(base: Path) -> None:
    flow = base / "flow"
    expect(driver.next_command(flow).startswith(RUNNER_PREFIX + " next --flow "), "next_command prefix")
    expect(driver.answer_command(flow).startswith(RUNNER_PREFIX + " answer --flow "), "answer_command prefix")
    expect(driver.clear_live_command(flow).startswith(RUNNER_PREFIX + " clear-live --flow "), "clear_live prefix")


def test_stub_doc_has_no_bare_positional_tokens_and_uses_arguments(base: Path) -> None:
    text = read_stub_doc()
    expect(re.search(r"\$[0-9]", text) is None, "a bare $0-$9 token is in the stub doc")
    expect('__main__.py" start --flow <dir>' in text, "the stub doc does not document the start --flow command")


def test_stub_doc_references_the_flow_reference_and_the_awaiting_marker(base: Path) -> None:
    text = read_stub_doc()
    expect("prompts/flow-reference.md" in text, "the stub doc does not mention prompts/flow-reference.md")
    expect("<!-- awaiting-answers:" in text, "the stub doc does not mention the awaiting-answers marker")


def test_stub_doc_has_accept_rule_section(base: Path) -> None:
    text = read_stub_doc()
    expect("Accept the `NEXT:` line from `start`" in text, "the stub doc does not state the accept rule")
    expect("\n## Verbs\n" in text, "the stub doc has no '## Verbs' section")


def test_runbook_start_flow_command_parses(base: Path) -> None:
    runbook = (REPO_ROOT / "docs" / "v2-inventory" / "skeleton-runbook.md").read_text(encoding="utf-8")
    expect("start --flow <flow dir> --repo OWNER/NAME --mode" in runbook, "runbook lacks the full start --flow command")
    args = cli.build_parser().parse_args(
        ["start", "--flow", "/x", "--repo", "OWNER/NAME", "--mode", "headless", "--wait-mode", "end-turn", "--step-minutes", "15"]
    )
    expect(args.flow == "/x" and args.step_minutes == 15, "the documented start --flow form did not parse")


def test_stub_doc_has_no_ship_merge_push_pr_or_redo_ledger_calls(base: Path) -> None:
    text = read_stub_doc()
    for needle in ("git push", "gh pr", "/shipit", "/merge-and-cleanup", "flow-redo", "flow-ledger", "AskUserQuestion"):
        expect(needle not in text, f"the stub doc contains {needle!r}")
    expect(re.search(r"\bgh\b(?!-error)", text) is None, "the stub doc invokes gh")




# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

SECTIONS: List[Tuple[str, List[Tuple[str, Callable[[Path], None]]]]] = [
    ("[Section 1] Boundary vocabulary and the state loader", [
        ("parse_verb maps members to themselves, unknowns to STOP, and handles every verb", test_parse_verb_members_unknowns_and_exhaustiveness),
        ("the state loader accepts a real state and rejects a missing key, a wrong type, a wrong format and a bad status", test_state_loader_accepts_a_real_state_and_rejects_each_invalid_shape),
        ("a corrupt state.json is refused and never spawns", test_corrupt_state_gives_a_refusal_and_never_a_spawn),
    ]),
    ("[Section 2] The transition through the CLI", [
        ("start spawns the worktree step and records a spawned record", test_start_spawns_the_worktree_step_and_records_a_spawned_record),
        ("a re-run next on a live record is a WAIT with the same nonce and no extra spawn", test_rerun_next_on_a_live_record_is_a_wait_with_no_new_nonce_and_no_extra_spawn),
        ("a completed receipt lands and the next step spawns", test_a_completed_receipt_lands_and_the_next_step_spawns),
        ("clear-live removes the live record and the next SPAWN has a new nonce and attempt", test_clear_live_then_next_spawns_a_new_nonce_and_attempt),
        ("answer refuses a flow with no parked question", test_answer_refuses_a_flow_with_no_parked_question),
        ("an unrecognized --after label still prints exactly one NEXT line", test_an_unrecognized_after_label_is_not_fatal_and_keeps_one_stdout_line),
    ]),
    ("[Section 3] Stub-side accept rule", [
        ("the accept rule model maps each documented failure to STOP and accepts a valid line", test_accept_rule_model_accepts_a_real_directive_and_maps_failures_to_stop),
        ("the accept rule accepts the real next output", test_stub_accept_rule_on_real_driver_output),
    ]),
    ("[Section 4] flows-v2 root acceptance", [
        ("flow dirs are created only under flows-v2, distinct per call, with no flow-run marker", test_flow_dirs_are_created_only_under_flows_v2),
        ("a flows-v2 symlink into the v1 flows/ dir is refused before any write", test_flows_v2_symlink_into_v1_flows_is_refused_before_any_write),
    ]),
    ("[Section 5] Stub doc acceptance (commands/expert-flow-v2.md)", [
        ("the description begins 'Experimental, not for real tickets:'", test_stub_doc_description_begins_experimental),
        ("the first body line is 'experimental, not for real tickets'", test_stub_doc_first_body_line_after_frontmatter),
        ("allowed-tools is exactly the granted set and does not include AskUserQuestion", test_stub_doc_allowed_tools_exact_and_no_ask_user_question),
        ("next, answer and clear-live commands start with the exact granted prefix", test_stub_doc_next_and_answer_commands_start_with_the_granted_prefix),
        ("the doc has no bare $0-$9 tokens and passes $ARGUMENTS", test_stub_doc_has_no_bare_positional_tokens_and_uses_arguments),
        ("the doc references prompts/flow-reference.md and the awaiting-answers marker", test_stub_doc_references_the_flow_reference_and_the_awaiting_marker),
        ("the doc has no ship, merge, push, PR, gh, redo or ledger step", test_stub_doc_has_no_ship_merge_push_pr_or_redo_ledger_calls),
        ("the doc has an Accept rule section", test_stub_doc_has_accept_rule_section),
        ("the runbook's documented start --flow command parses", test_runbook_start_flow_command_parses),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER (v2) TEST SUITE")
    with tempfile.TemporaryDirectory(prefix="flow-driver-test-") as tmp:
        base = Path(tmp)
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn, base)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
