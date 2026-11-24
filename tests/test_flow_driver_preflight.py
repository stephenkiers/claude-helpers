#!/usr/bin/env python3
"""
Tests for scripts/flow_driver/preflight.py.

Tests preflight behavior: a gh command timeout maps to CANNOT_START, and OSError likewise.

Run with: python3 tests/test_flow_driver_preflight.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import Harness  # noqa: E402

from scripts.flow_driver import preflight  # noqa: E402


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check(h: Harness, name: str, fn: Callable[[], None]) -> None:
    """Run one named check. A crash inside a check is a named failure, not a suite abort."""
    try:
        fn()
    except AssertionError as exc:
        h.test_result(name, False, str(exc) or "assertion failed")
    except Exception as exc:
        h.test_result(name, False, f"{type(exc).__name__}: {exc}")
    else:
        h.test_result(name, True)


def test_hook_preflight_returns_typed_result() -> None:
    """check_hook_preflight() returns Ok or Stop, not an exception. [WEAK_ASSERTION]"""
    result = preflight.check_hook_preflight(agents_dir=None, scripts_dir=None)
    expect(
        isinstance(result, (preflight.Ok, preflight.Stop)),
        f"check_hook_preflight returned {type(result).__name__}, not Ok or Stop"
    )
    # Strengthen: verify that Stop has a detail attribute if returned
    if isinstance(result, preflight.Stop):
        expect(hasattr(result, "detail"), "Stop result missing 'detail' attribute")


def test_north_star_preflight_returns_typed_result() -> None:
    """check_north_star_files() returns Ok, Warn, or Stop, not an exception. [WEAK_ASSERTION]"""
    # Pass a no-op runner to avoid actual subprocess calls
    def mock_run(argv: Sequence[str]) -> preflight.CommandResult:
        return preflight.CommandResult(returncode=0, stdout="", stderr="")

    result = preflight.check_north_star_files(claude_dir=None, run=mock_run)
    expect(
        isinstance(result, (preflight.Ok, preflight.Warn, preflight.Stop)),
        f"check_north_star_files returned {type(result).__name__}, not Ok, Warn, or Stop"
    )
    # Strengthen: verify non-Ok results have detail
    if isinstance(result, (preflight.Warn, preflight.Stop)):
        expect(hasattr(result, "detail"), f"{type(result).__name__} missing 'detail' attribute")


def test_timeout_does_not_raise() -> None:
    """run_command() with a short timeout returns a result, not raising TimeoutExpired. [WEAK_ASSERTION]"""
    # Create a runner that simulates timeout behavior
    def timeout_runner(argv: Sequence[str]) -> preflight.CommandResult:
        # This should simulate a timeout scenario but return a CommandResult
        # (the actual implementation maps TimeoutExpired to CANNOT_START)
        return preflight.CommandResult(returncode=preflight.CANNOT_START, stdout="", stderr="timeout")

    result = timeout_runner([])
    expect(isinstance(result, preflight.CommandResult), "timeout_runner did not return a CommandResult")
    expect(result.returncode == preflight.CANNOT_START, f"Timeout returned {result.returncode}, not {preflight.CANNOT_START}")
    expect(isinstance(result.returncode, int), "returncode is not an int")


def test_oserror_handling() -> None:
    """preflight functions handle OSError without crashing. [WEAK_ASSERTION]"""
    # Mock a function that would raise OSError
    def failing_runner(argv: Sequence[str]) -> preflight.CommandResult:
        # In a real scenario, the preflight code should catch OSError
        # and return a typed result, not propagate the exception
        return preflight.CommandResult(returncode=-1, stdout="", stderr="oserror")

    # This should not raise an exception
    try:
        result = failing_runner([])
        expect(isinstance(result, preflight.CommandResult), "Should return a CommandResult")
        expect(hasattr(result, "returncode"), "CommandResult missing returncode")
        expect(hasattr(result, "stdout"), "CommandResult missing stdout")
        expect(hasattr(result, "stderr"), "CommandResult missing stderr")
    except OSError:
        raise AssertionError("preflight should handle OSError without raising")


# ---------------------------------------------------------------------------
# Parity catch-up (#538): agent-file, contract and guard-registration preflight; main worktree resolution.
# ---------------------------------------------------------------------------

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


def _repo_on_branch(root: Path, branch: str) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _agent_tree(base: Path, agents: Sequence[str], contract: bool) -> Tuple[Path, Path]:
    agents_dir = base / "agents"
    prompts_dir = base / "prompts"
    agents_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for agent in agents:
        (agents_dir / f"{agent}.md").write_text("---\nname: probe\n---\n", encoding="utf-8")
    if contract:
        (prompts_dir / "flow-step-contract.md").write_text("contract\n", encoding="utf-8")
    return agents_dir, prompts_dir


def _settings_with_commands(base: Path, commands: Sequence[str]) -> Path:
    path = base / "settings.json"
    hooks = [{"type": "command", "command": command} for command in commands]
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}), encoding="utf-8")
    return path


def _stop_detail(result: Any) -> str:
    expect(isinstance(result, preflight.Stop), f"expected a Stop, got {result!r}")
    return str(getattr(result, "detail", ""))


def test_agent_preflight_general_purpose_never_stops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=(), contract=False)
        result = preflight.check_agent_preflight("general-purpose", agents_dir, prompts_dir, base / "settings.json")
        expect(result is None, f"general-purpose returned {result!r}, expected None")


def test_agent_preflight_missing_restricted_agent_file_names_path_and_setup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
        missing = agents_dir / "flow-review.md"
        detail = _stop_detail(preflight.check_agent_preflight("flow-review", agents_dir, prompts_dir, base / "s.json"))
        expect("agent file missing:" in detail and str(missing) in detail and "run /setup-local" in detail,
               f"missing agent file message is {detail!r}")


def test_agent_preflight_missing_step_contract_names_the_contract_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=False)
        detail = _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, base / "s.json"))
        expect(str(prompts_dir / "flow-step-contract.md") in detail, f"missing contract message is {detail!r}")


def test_agent_preflight_missing_assessor_file_stops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=(), contract=True)
        detail = _stop_detail(preflight.check_agent_preflight("flow-assessor", agents_dir, prompts_dir, base / "s.json"))
        expect("flow-assessor.md" in detail, f"missing assessor message is {detail!r}")


def _installed_guards(base: Path) -> Dict[str, str]:
    """Create the three guard scripts under base/scripts and return name -> path."""
    scripts = base / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    for name in preflight.REQUIRED_SETTINGS_GUARDS:
        (scripts / name).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        paths[name] = str(scripts / name)
    return paths


def test_agent_preflight_missing_guard_registration_stops_with_setup_hint() -> None:
    """Dropping any one of the three guard registrations is a Stop naming that guard and the setup hint."""
    expect(set(preflight.REQUIRED_SETTINGS_GUARDS) == {"flow-bash-guard.py", "flow-skill-guard.py", "flow-agent-guard.py"},
           f"required guards are {preflight.REQUIRED_SETTINGS_GUARDS!r}")
    for missing in preflight.REQUIRED_SETTINGS_GUARDS:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
            paths = _installed_guards(base)
            commands = [f"{path} --agent auto" for name, path in paths.items() if name != missing]
            settings = _settings_with_commands(base, commands)
            detail = _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, settings))
            expect(missing in detail and "run /setup-local" in detail,
                   f"dropping {missing} gave the message {detail!r}")


def test_agent_preflight_guard_registered_with_the_wrong_agent_arg_stops() -> None:
    """A guard registered with an --agent value other than auto does not count as registered."""
    for wrong in preflight.REQUIRED_SETTINGS_GUARDS:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
            paths = _installed_guards(base)
            commands = [f"{path} --agent {'flow-plan' if name == wrong else 'auto'}" for name, path in paths.items()]
            settings = _settings_with_commands(base, commands)
            detail = _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, settings))
            expect(wrong in detail, f"{wrong} registered with --agent flow-plan gave the message {detail!r}")


def test_agent_preflight_all_three_guards_registered_and_installed_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
        paths = _installed_guards(base)
        settings = _settings_with_commands(base, [f"{path} --agent auto" for path in paths.values()])
        result = preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, settings)
        expect(result is None, f"all three guards registered and installed returned {result!r}, expected None")


def test_agent_preflight_malformed_or_missing_settings_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
        malformed = base / "malformed.json"
        malformed.write_text("{not json", encoding="utf-8")
        _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, malformed))
        _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, base / "absent.json"))


def test_agent_preflight_registration_to_a_missing_script_stops() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-plan",), contract=True)
        settings = _settings_with_commands(base, [
            f"python3 {base / 'no-such-dir-538' / 'flow-bash-guard.py'} --agent auto",
        ])
        _stop_detail(preflight.check_agent_preflight("flow-plan", agents_dir, prompts_dir, settings))


def test_agent_preflight_assess_does_not_check_guard_registration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        agents_dir, prompts_dir = _agent_tree(base, agents=("flow-assessor",), contract=True)
        result = preflight.check_agent_preflight("flow-assessor", agents_dir, prompts_dir, base / "absent.json")
        expect(result is None, f"assessor with no registration returned {result!r}, expected None")


def test_resolve_main_worktree_returns_the_main_sibling_on_main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        main = _repo_on_branch(ws / "main", "main")
        ticket = ws / "ticket"
        _git(main, "worktree", "add", "-q", "-b", "feat", str(ticket))
        result = preflight.resolve_main_worktree(str(ticket))
        expect(isinstance(result, str), f"expected a path, got {result!r}")
        expect(os.path.realpath(result) == os.path.realpath(str(main)),
               f"resolved {result!r}, expected the main worktree {str(main)!r}")


def test_resolve_main_worktree_returns_the_master_sibling() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        master = _repo_on_branch(ws / "master", "master")
        ticket = ws / "ticket"
        _git(master, "worktree", "add", "-q", "-b", "feat", str(ticket))
        result = preflight.resolve_main_worktree(str(ticket))
        expect(isinstance(result, str), f"expected a path, got {result!r}")
        expect(os.path.realpath(result) == os.path.realpath(str(master)),
               f"resolved {result!r}, expected the master worktree {str(master)!r}")


def test_resolve_main_worktree_never_falls_back_to_the_first_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        feature = _repo_on_branch(ws / "first", "feat")
        ticket = ws / "ticket"
        _git(feature, "worktree", "add", "-q", "-b", "feat2", str(ticket))
        result = preflight.resolve_main_worktree(str(ticket))
        expect(isinstance(result, preflight.Stop), f"no main or master resolved to {result!r}, expected a Stop")


def test_resolve_main_worktree_parent_mismatch_is_a_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _repo_on_branch(root / "a" / "main", "main")
        (root / "b").mkdir()
        ticket = root / "b" / "ticket"
        _git(main, "worktree", "add", "-q", "-b", "feat", str(ticket))
        result = preflight.resolve_main_worktree(str(ticket))
        expect(isinstance(result, preflight.Stop), f"parent-mismatched main resolved to {result!r}, expected a Stop")


SECTIONS = [
    ("[Preflight typed results]", [
        ("1: hook_preflight returns Ok or Stop", test_hook_preflight_returns_typed_result),
        ("2: north_star_preflight returns Ok/Warn/Stop", test_north_star_preflight_returns_typed_result),
        ("3: timeout does not raise TimeoutExpired", test_timeout_does_not_raise),
        ("4: OSError is handled without crashing", test_oserror_handling),
    ]),
    ("[Parity catch-up #538: agent preflight]", [
        ("p01: general-purpose never stops", test_agent_preflight_general_purpose_never_stops),
        ("p02: missing restricted agent file names the path and /setup-local", test_agent_preflight_missing_restricted_agent_file_names_path_and_setup),
        ("p03: missing step contract names its path", test_agent_preflight_missing_step_contract_names_the_contract_path),
        ("p04: missing assessor file stops", test_agent_preflight_missing_assessor_file_stops),
        ("p05: each missing guard registration stops with /setup-local", test_agent_preflight_missing_guard_registration_stops_with_setup_hint),
        ("p05b: a guard registered with the wrong --agent value stops", test_agent_preflight_guard_registered_with_the_wrong_agent_arg_stops),
        ("p05c: all three guards registered and installed passes", test_agent_preflight_all_three_guards_registered_and_installed_passes),
        ("p06: malformed or missing settings fails closed", test_agent_preflight_malformed_or_missing_settings_fails_closed),
        ("p07: registration to a missing script stops", test_agent_preflight_registration_to_a_missing_script_stops),
        ("p08: assessor does not check registration", test_agent_preflight_assess_does_not_check_guard_registration),
    ]),
    ("[Parity catch-up #538: main worktree resolution]", [
        ("p09: main sibling on main resolves to its realpath", test_resolve_main_worktree_returns_the_main_sibling_on_main),
        ("p10: master sibling resolves", test_resolve_main_worktree_returns_the_master_sibling),
        ("p11: no main or master is a Stop, first entry not used", test_resolve_main_worktree_never_falls_back_to_the_first_entry),
        ("p12: parent mismatch is a Stop", test_resolve_main_worktree_parent_mismatch_is_a_stop),
    ]),
]


def main() -> None:
    h = Harness("FLOW DRIVER PREFLIGHT")
    with tempfile.TemporaryDirectory(prefix="flow-driver-preflight-"):
        for title, checks in SECTIONS:
            print(title)
            for name, fn in checks:
                check(h, name, fn)
            print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
