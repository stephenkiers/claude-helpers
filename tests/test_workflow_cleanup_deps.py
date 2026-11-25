"""apply_cleanup brings main's dependencies current before validation, so missing tooling never reads as a regression."""

import json
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

from workflow.cleanup import apply_cleanup
from workflow.deps import DepsResult
from workflow.validation import EnvDerivation, ValidationRun, ValidationVerdict

h = Harness("WORKFLOW CLEANUP DEPS TEST SUITE")
test_result = h.test_result


@contextmanager
def lock_ok(cwd):
    yield None


def run(check_commands, deps_side_effect):
    """Run apply_cleanup up to validation with everything else stubbed; return (result, call order)."""
    order = []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.mkdir()
        plan = json.dumps({
            "target_worktree": str(target),
            "current_branch": "feature/x",
            "pr_state": "MERGED",
            "pr_number": 1,
            "expected_head_sha": "abc",
            "cache_hash": "h",
            "check_commands": check_commands,
            "stacked_children": [],
        })

        def fake_deps(worktree, main_worktree):
            order.append(("deps", Path(worktree), Path(main_worktree)))
            return deps_side_effect()

        def fake_validation(*args, **kwargs):
            order.append(("validation",))
            return ValidationRun(verdict=ValidationVerdict.PASS)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("workflow.cleanup._check_plan_fresh", return_value=None))
            stack.enter_context(mock.patch("workflow.validation.validation_lock", lock_ok))
            stack.enter_context(mock.patch("workflow.git.pull_ff_only", return_value=(True, None)))
            stack.enter_context(mock.patch(
                "workflow.validation.build_validation_env",
                return_value=EnvDerivation(env={}, dropped_names=[], inconclusive_reason=None, notes=[]),
            ))
            stack.enter_context(mock.patch("workflow.deps.ensure_deps", side_effect=fake_deps))
            stack.enter_context(mock.patch("workflow.validation.run_validation", side_effect=fake_validation))
            # Stop right after validation: a stale-plan re-check aborts before any mutation.
            stack.enter_context(mock.patch("workflow.git.get_head_sha", return_value="moved"))
            result, _ = apply_cleanup(plan, cwd=Path(tmp), queue_started_at=None)
            return result, order, Path(tmp)


# (a) deps ensure runs on main, before validation, and an install is noted.
result, order, main = run(["npm test"], lambda: DepsResult("installed"))
test_result("(a) deps ensure runs before validation", [o[0] for o in order] == ["deps", "validation"], f"order={order}")
test_result("(a) deps ensure targets the main worktree", order[0][1:] == (main, main), f"call={order[0]}")
test_result("(a) the install is noted", "installed main's dependencies before validation" in result.notes, f"notes={result.notes}")

# (b) a failed ensure is only a note; validation still runs.
result, order, _ = run(["npm test"], lambda: DepsResult("failed", "no lockfile manager"))
test_result("(b) validation still runs after a failed ensure", ("validation",) in order, f"order={order}")
test_result("(b) the failure is noted", any(n.startswith("deps ensure on main: failed") for n in result.notes), f"notes={result.notes}")

# (c) an exception from ensure is contained.
def boom():
    raise RuntimeError("kaboom")
result, order, _ = run(["npm test"], boom)
test_result("(c) validation still runs after an ensure exception", ("validation",) in order, f"order={order}")
test_result("(c) the exception is noted", any("kaboom" in n for n in result.notes), f"notes={result.notes}")

# (d) current deps add no note.
result, order, _ = run(["npm test"], lambda: DepsResult("current"))
test_result("(d) current deps add no deps note", not any("deps" in n for n in result.notes), f"notes={result.notes}")

# (e) no check commands: nothing to validate, so no install.
result, order, _ = run([], lambda: DepsResult("installed"))
test_result("(e) no checks means no deps ensure", not any(o[0] == "deps" for o in order), f"order={order}")

print()
h.summarize_and_exit()
