"""
Flows-v2 root and flow-dir builder: the only place a v2 skeleton flow path is made (v2 plan Q3 A, D5 A).

Containment: the v1 flows directory is the sibling `flows/` of the flows-v2 root (root.parent / "flows").
Every root and flow dir is resolved with os.path.realpath and refused before any write when it is, or lies
inside, that v1 directory. The check runs before any write because scripts.workflow.fsutil.atomic_write_text
follows symlinks.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import NewType

from scripts.workflow.fsutil import atomic_write_text
from scripts.workflow.paths import is_within

from .model import Mode, StateRecord, WaitMode, state_to_dict, state_path

FlowDir = NewType("FlowDir", Path)


class FlowPathError(ValueError):
    """A repo key or flow path is malformed or is not allowed under the flows-v2 root."""


_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TOKEN_RE = re.compile(r"[0-9a-f]+")

HUMAN_NOTES_TEMPLATE = """# Human notes (v2 skeleton flow)

Record only facts that only a human sees. Do not paste personal paths or private names.

- Run id:
- Kill kind and point (K1 or K2), if any:
- Session that re-ran the flow:
- Observed stall, if any:
"""


def flows_v2_root(home: Path) -> Path:
    return home / ".claude" / "flows-v2"


def repo_key(owner_name: str) -> str:
    """The directory key for OWNER/NAME: the same shape v1 and fleet.py use, never the worktree basename."""
    if ".." in owner_name or not _REPO_RE.fullmatch(owner_name):
        raise FlowPathError(f"repo must be OWNER/NAME, got {owner_name!r}")
    return owner_name.replace("/", "_")


def format_utc(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _contained_realpath(path: Path, root: Path) -> str:
    """The realpath of path, refused when it is, or lies inside, the v1 flows directory next to root."""
    if not path.is_absolute() or not root.is_absolute():
        raise FlowPathError(f"flow paths must be absolute: {path} (root {root})")
    real = os.path.realpath(str(path))
    v1_flows = os.path.realpath(str(root.parent / "flows"))
    if real == v1_flows or is_within(real, v1_flows):
        raise FlowPathError(f"{real} lies inside the v1 flows directory; v2 never writes there")
    return real


def create_flow_dir(
    root: Path,
    repo: str,
    now: datetime,
    token: str,
    *,
    mode: Mode,
    wait_mode: WaitMode,
    step_minutes: int,
) -> FlowDir:
    """
    Create a new, unique flow dir `<root>/<repo_key>/skeleton-<UTC stamp>-<token>` with its state.json.

    `now` must be timezone-aware UTC. `token` must be lowercase hex. The dir is created with exist_ok=False,
    so two calls never share a dir.
    """
    key = repo_key(repo)
    if not _TOKEN_RE.fullmatch(token):
        raise FlowPathError(f"token must be lowercase hex, got {token!r}")
    _contained_realpath(root, root)
    flow = root / key / f"skeleton-{now.strftime('%Y%m%dT%H%M%SZ')}-{token}"
    _contained_realpath(flow, root)
    flow.mkdir(parents=True, exist_ok=False)
    state = StateRecord(
        repo=repo,
        flow_id=flow.name,
        mode=mode,
        wait_mode=wait_mode,
        step_minutes=step_minutes,
        created_utc=format_utc(now),
        last_attempt=0,
    )
    atomic_write_text(state_path(flow), json.dumps(state_to_dict(state), indent=2) + "\n")
    atomic_write_text(flow / "human-notes.md", HUMAN_NOTES_TEMPLATE)
    return FlowDir(flow)


def open_flow_dir(root: Path, path: Path) -> FlowDir:
    """An existing flow dir that lies strictly under the flows-v2 root. Returns its realpath."""
    real = _contained_realpath(path, root)
    if not is_within(real, os.path.realpath(str(root))):
        raise FlowPathError(f"flow dir {real} is not strictly under the flows-v2 root")
    if not os.path.isdir(real):
        raise FlowPathError(f"flow dir {real} does not exist")
    return FlowDir(Path(real))
