"""
Retention sweep for v2 flow dirs (Layer 2 Step 9; ADR-0026).

Only `start` sweeps, and only the flows of the repo being started. `list` and `status` never delete anything. A flow is
swept when its state.json says done or abandoned and its `updated` stamp is older than the retention age. A running,
failed, awaiting or initializing flow is never swept, and neither is the flow `start` was asked to open.

The age is one named module constant, so a test supplies its own threshold through the `days` argument and never
edits this value.

Python 3.8 floor: typing.List, Optional only.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .state import STATE_FILE

# Flows that finished (done or abandoned) and have not been updated for this many days are removed by `start`.
RETENTION_DAYS = 30

_TS = "%Y-%m-%dT%H:%M:%SZ"
_SWEPT_STATUSES = ("done", "abandoned")


def _updated_at(flow: Path) -> Optional[datetime]:
    """The state.json `updated` stamp as UTC, or None when the flow has no readable state."""
    try:
        data = json.loads((flow / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    updated = data.get("updated")
    if status not in _SWEPT_STATUSES or not isinstance(updated, str):
        return None
    try:
        return datetime.strptime(updated, _TS).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def expired_flows(repo_dir: Path, now: datetime, days: int = RETENTION_DAYS, keep: Optional[Path] = None) -> List[Path]:
    """
    The flow dirs under repo_dir that are done or abandoned and older than days. keep is never returned.
    Pure reads: nothing is removed here.
    """
    if not repo_dir.is_dir():
        return []
    cutoff = now - timedelta(days=days)
    found: List[Path] = []
    for flow in sorted(repo_dir.iterdir()):
        if not flow.is_dir() or flow.name.startswith("."):
            continue
        if keep is not None and flow.resolve() == keep.resolve():
            continue
        updated = _updated_at(flow)
        if updated is not None and updated <= cutoff:
            found.append(flow)
    return found


def sweep(repo_dir: Path, now: datetime, days: int = RETENTION_DAYS, keep: Optional[Path] = None) -> List[Path]:
    """Remove the expired flow dirs under repo_dir and return the paths removed. Called only from `start`."""
    removed: List[Path] = []
    for flow in expired_flows(repo_dir, now, days, keep):
        shutil.rmtree(flow)
        removed.append(flow)
    return removed
