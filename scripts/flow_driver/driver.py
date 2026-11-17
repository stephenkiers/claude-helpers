"""
Command strings for the v2 driver's directives (ADR-0026).

Layer 2 (Step 2, session D8) removes the skeleton decide(), its Observation and Decision types and the skeleton receipt
view. The transition now lives in steps.py and the mutating loop in loop.py. What remains here is the one place each
runner command line is built, so every directive names the same `next`, `answer` and `clear-live` invocations.
"""

import shlex
from pathlib import Path

from .model import RUNNER_PREFIX


def _quoted(flow: Path) -> str:
    return shlex.quote(str(flow))


def next_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} next --flow {_quoted(flow)}"


def answer_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} answer --flow {_quoted(flow)} --text '<your answer>'"


def clear_live_command(flow: Path) -> str:
    return f"{RUNNER_PREFIX} clear-live --flow {_quoted(flow)} --reason 'human recovery'"
