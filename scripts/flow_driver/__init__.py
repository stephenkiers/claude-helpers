"""
The v2 /expert-flow driver (ADR-0026). Disk-only boundary; the pure transition lives in steps.py.

**Single-caller, single-answerer invariant:** every flow dir has exactly one sequential writer (one next/start
process at a time, no concurrent calls; one answer process after a previous receipt write, no stale agent
writes after clear-live). The state lock (state.mutating) enforces the single writer; a concurrent call is refused.

Modules: model (boundary vocabulary), flowdir (flow-dir paths and containment), driver (command strings),
steps (closed unions and the pure transition), recovery (decision table), loop (the mutating next, answer and
clear-live), cli (the init, resume, status, list and start, next, answer and clear-live edge). Run it through __main__.py.
"""

from .cli import main
from .driver import answer_command, clear_live_command, next_command
from .flowdir import FlowPathError, create_flow_dir, flows_v2_root, open_flow_dir

__all__ = [
    "FlowPathError",
    "answer_command",
    "clear_live_command",
    "create_flow_dir",
    "flows_v2_root",
    "main",
    "next_command",
    "open_flow_dir",
]
