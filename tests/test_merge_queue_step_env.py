"""run_step must not leak the invoking worktree's direnv-scoped vars into scratch-dir steps."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

from workflow.merge_queue import (
    SCRATCH_ENV_STRIPPED_VARS,
    run_step,
    _scratch_step_env,
    _verify_base_in_scratch,
    _phase_run_gate_and_assertions,
    MergeQueueConfig,
    Step,
    VerifyBaseOutcome,
)

h = Harness("MERGE QUEUE STEP ENV TEST SUITE")
test_result = h.test_result

# Literal hardcoded values for environment variable test
leaked_env = {"COMPOSE_PROJECT_NAME": "leaked", "DATABASE_URL": "leaked", "KEPT_VAR": "kept"}

# Test (a): default run_step (no env) preserves both variables
with tempfile.TemporaryDirectory() as tmp:
    with patch.dict(os.environ, leaked_env):
        log = Path(tmp) / "step.log"
        outcome = run_step('echo "$COMPOSE_PROJECT_NAME|$DATABASE_URL|$KEPT_VAR"', Path(tmp), log)
        test_result("(a) Step succeeds with default env", outcome.success, f"error={outcome.error}")
        test_result(
            "(a) Default run_step (no env) preserves both variables",
            log.read_text().strip() == "leaked|leaked|kept",
            f"log={log.read_text()!r}",
        )

# Test (b): run_step with explicit env uses it verbatim
with tempfile.TemporaryDirectory() as tmp:
    with patch.dict(os.environ, leaked_env):
        config = MergeQueueConfig(base="main", steps=[])
        step_env = _scratch_step_env(config)
        log = Path(tmp) / "step.log"
        outcome = run_step('echo "$COMPOSE_PROJECT_NAME|$DATABASE_URL|$KEPT_VAR"', Path(tmp), log, env=step_env)
        test_result("(b) Step succeeds with filtered env", outcome.success, f"error={outcome.error}")
        test_result(
            "(b) run_step with filtered env strips vars",
            log.read_text().strip() == "||kept",
            f"log={log.read_text()!r}",
        )

# Test (c): _verify_base_in_scratch passes filtered env to both run_step calls
with tempfile.TemporaryDirectory() as tmp:
    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")], scratch_setup=["true"])
    scratch_dir = Path(tmp) / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / ".git").mkdir()

    run_step_mock = MagicMock(return_value=MagicMock(success=True))
    with patch("workflow.merge_queue.run_step", run_step_mock):
        with patch("workflow.merge_queue.get_scratch_dir", return_value=scratch_dir):
            with patch("workflow.merge_queue.Runner.run_git", return_value="dummy_sha\n"):
                with patch.dict(os.environ, leaked_env):
                    verify_outcome = _verify_base_in_scratch("dummy_sha", config)
                    test_result("(c) _verify_base_in_scratch succeeds", verify_outcome == VerifyBaseOutcome.VERIFIED)
                    test_result(
                        "(c) run_step called twice",
                        run_step_mock.call_count == 2,
                        f"call_count={run_step_mock.call_count}",
                    )

                    # Check that both calls have env kwarg without COMPOSE_PROJECT_NAME/DATABASE_URL and with KEPT_VAR
                    for call_idx, call in enumerate(run_step_mock.call_args_list):
                        env_kwarg = call.kwargs.get("env")
                        test_result(
                            f"(c) Call {call_idx+1} env is dict",
                            isinstance(env_kwarg, dict),
                            f"env type={type(env_kwarg)}",
                        )
                        if isinstance(env_kwarg, dict):
                            test_result(
                                f"(c) Call {call_idx+1} env lacks COMPOSE_PROJECT_NAME",
                                "COMPOSE_PROJECT_NAME" not in env_kwarg,
                                f"env keys={list(env_kwarg.keys())[:10]}",
                            )
                            test_result(
                                f"(c) Call {call_idx+1} env lacks DATABASE_URL",
                                "DATABASE_URL" not in env_kwarg,
                                f"env keys={list(env_kwarg.keys())[:10]}",
                            )
                            test_result(
                                f"(c) Call {call_idx+1} env retains KEPT_VAR",
                                env_kwarg.get("KEPT_VAR") == "kept",
                                f"KEPT_VAR={env_kwarg.get('KEPT_VAR')}",
                            )

# Test (d): _phase_run_gate_and_assertions passes no env to run_step
with tempfile.TemporaryDirectory() as tmp:
    pr_worktree = Path(tmp) / "pr"
    pr_worktree.mkdir()

    config = MergeQueueConfig(base="main", steps=[Step(cmd="true")])

    run_step_mock = MagicMock(return_value=MagicMock(success=True))
    with patch("workflow.merge_queue.run_step", run_step_mock):
        with patch("workflow.merge_queue.Runner.run_git", return_value="dummy_sha\n"):
            with patch("workflow.merge_queue.ensure_state_dir", return_value=Path(tmp) / "state"):
                with patch("workflow.merge_queue._is_tree_clean", return_value=True):
                    result, tested_sha = _phase_run_gate_and_assertions(
                        pr=1,
                        branch="test-branch",
                        worktree=str(pr_worktree),
                        orig_head="orig_sha",
                        config=config,
                        lock_fd_to_inherit=None,
                        cwd=pr_worktree,
                    )
                    test_result(
                        "(d) _phase_run_gate_and_assertions completes",
                        True,
                        f"result={result}, tested_sha={tested_sha}",
                    )

                    # Check that run_step was called at least once
                    test_result(
                        "(d) run_step called at least once",
                        run_step_mock.call_count >= 1,
                        f"call_count={run_step_mock.call_count}",
                    )

                    # Check that all calls have no env kwarg or env is None, and env not passed positionally
                    for call_idx, call in enumerate(run_step_mock.call_args_list):
                        env_kwarg = call.kwargs.get("env")
                        test_result(
                            f"(d) Call {call_idx+1} has no env or env is None",
                            env_kwarg is None,
                            f"env={env_kwarg}",
                        )
                        test_result(
                            f"(d) Call {call_idx+1} env not passed positionally",
                            len(call.args) < 6,
                            f"args length={len(call.args)}",
                        )

# Verify constant and default values
test_result(
    "SCRATCH_ENV_STRIPPED_VARS is correct",
    SCRATCH_ENV_STRIPPED_VARS == ("COMPOSE_PROJECT_NAME", "DATABASE_URL"),
    f"SCRATCH_ENV_STRIPPED_VARS={SCRATCH_ENV_STRIPPED_VARS}",
)

default_config = MergeQueueConfig(base="main", steps=[])
test_result(
    "Default MergeQueueConfig.scratch_env_strip matches constant",
    default_config.scratch_env_strip == ["COMPOSE_PROJECT_NAME", "DATABASE_URL"],
    f"scratch_env_strip={default_config.scratch_env_strip}",
)

test_result(
    "Default MergeQueueConfig.scratch_env is empty dict",
    default_config.scratch_env == {},
    f"scratch_env={default_config.scratch_env}",
)

print()
h.summarize_and_exit()
