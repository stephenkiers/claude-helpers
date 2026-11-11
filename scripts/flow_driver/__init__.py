"""
Walking skeleton of the v2 /expert-flow driver (Sub-issue S). Disk-only boundary; pure decide() at its core.

Modules: model (boundary types and loaders), flowdir (flow-dir paths and containment), driver (pure decide),
cli (the start, next, answer and clear-live edge). Run it through __main__.py.
"""

from .cli import main
from .driver import (
    Decision,
    Observation,
    ReceiptView,
    answer_command,
    clear_live_command,
    decide,
    next_command,
    parse_receipt,
)
from .flowdir import FlowPathError, create_flow_dir, flows_v2_root, open_flow_dir

__all__ = [
    "Decision",
    "FlowPathError",
    "Observation",
    "ReceiptView",
    "answer_command",
    "clear_live_command",
    "create_flow_dir",
    "decide",
    "flows_v2_root",
    "main",
    "next_command",
    "open_flow_dir",
    "parse_receipt",
]
