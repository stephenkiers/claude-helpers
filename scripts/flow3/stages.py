"""
The stage registry. The orchestrator fills REGISTRY in later with each stage's `run(ctx)` function,
keyed by stage name (worktree, plan, spec_tests, implement, review, fix, ship, merge).
"""

from typing import Any, Callable, Dict

from scripts.flow3.types import StageResult

StageFn = Callable[[Any], StageResult]

REGISTRY: Dict[str, StageFn] = {}
