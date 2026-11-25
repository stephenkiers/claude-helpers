"""
The stage registry. Stage functions are resolved lazily: importing this module never imports a stage
module, and a stage module that is missing or broken becomes a recorded stop when its stage runs, never
a crash at CLI start.

Test seam (not for production use): when the environment variable FLOW3_STAGES names a module, for
example `tests.flow3_fake_stages`, the registry is that module's `REGISTRY` instead.
"""

import importlib
import os
from typing import Any, Callable, Iterator, Mapping

from scripts.flow3.types import FlowStop, StageResult

StageFn = Callable[[Any], StageResult]

STAGE_MODULES = {
    "worktree": "scripts.flow3.steps.worktree",
    "plan": "scripts.flow3.steps.plan",
    "spec_tests": "scripts.flow3.steps.spec_tests",
    "implement": "scripts.flow3.steps.implement",
    "review": "scripts.flow3.steps.review",
    "fix": "scripts.flow3.steps.fix",
    "ship": "scripts.flow3.steps.ship",
    "merge": "scripts.flow3.mergegate",
}

ENV_OVERRIDE = "FLOW3_STAGES"


def _unavailable(stage: str, module: str, why: str) -> StageFn:
    """A stage function that stops the flow with a clear reason (the module could not be loaded)."""
    def run(ctx: Any) -> StageResult:
        raise FlowStop("runner", "the %s stage is not available: %s (%s)" % (stage, module, why))
    return run


def _load(stage: str) -> StageFn:
    module = STAGE_MODULES[stage]
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # a missing or half-written stage must stop the flow, not the CLI
        return _unavailable(stage, module, "%s: %s" % (type(exc).__name__, exc))
    fn = getattr(mod, "run", None)
    if not callable(fn):
        return _unavailable(stage, module, "no run(ctx) function")
    return fn  # type: ignore[no-any-return]


class _LazyRegistry(Mapping[str, StageFn]):
    """Maps stage name to its `run`, importing the stage's module only on lookup."""

    def _override(self) -> Any:
        name = os.environ.get(ENV_OVERRIDE)
        if not name:
            return None
        return importlib.import_module(name).REGISTRY

    def __getitem__(self, stage: str) -> StageFn:
        over = self._override()
        if over is not None:
            return over[stage]  # type: ignore[no-any-return]
        if stage not in STAGE_MODULES:
            raise KeyError(stage)
        return _load(stage)

    def __iter__(self) -> Iterator[str]:
        over = self._override()
        return iter(over if over is not None else STAGE_MODULES)

    def __len__(self) -> int:
        over = self._override()
        return len(over if over is not None else STAGE_MODULES)


REGISTRY: Mapping[str, StageFn] = _LazyRegistry()


def registry() -> Mapping[str, StageFn]:
    """The stage functions by name (lazy; honours the FLOW3_STAGES test seam at lookup time)."""
    return REGISTRY
