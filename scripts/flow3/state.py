"""
The flow directory: state.json (atomic, flock-guarded), events.jsonl, and numbered call directories.

A flow lives at repo_dir(repo)/<issue>-<slug>/. Every mutation of state.json, every event append, and
every call-directory allocation takes an exclusive flock on <flow>/.lock, so threads and processes
cannot lose each other's writes. Callbacks given to `update` must not call back into this FlowDir.
"""

import dataclasses
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from scripts.flow3.paths import flow_home, repo_dir
from scripts.flow3.types import FlowState, StopInfo
from scripts.workflow.fsutil import atomic_write_text

__all__ = ["flow_home", "repo_dir", "slugify", "FlowDir"]

STATE_VERSION = 1
SUBDIRS = ("calls", "steps", "human", "decisions", "checks")


def slugify(title: str, max_len: int = 40) -> str:
    """Lowercase, hyphen-separated ASCII slug of title, at most max_len characters; "flow" when empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "flow"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stop_from_dict(raw: Dict[str, Any]) -> StopInfo:
    return StopInfo(
        cls=str(raw["cls"]), reason=str(raw["reason"]), stage=str(raw["stage"]),
        evidence=raw.get("evidence"), tried=tuple(str(t) for t in raw.get("tried") or ()),
        resume_command=str(raw["resume_command"]),
    )


class FlowDir:
    """One flow's directory and everything stored in it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- construction and lookup ------------------------------------------------------------

    @classmethod
    def create(cls, repo: str, issue: int, title: str, repo_root: Path) -> "FlowDir":
        """Make a new flow directory. Raises FileExistsError when the issue already has one."""
        existing = cls.find(repo, issue)
        if existing is not None:
            raise FileExistsError(f"flow already exists for issue {issue}: {existing.path}")
        slug = slugify(title)
        path = repo_dir(repo) / f"{issue}-{slug}"
        path.mkdir(parents=True, exist_ok=False)
        for sub in SUBDIRS:
            (path / sub).mkdir()
        doc: Dict[str, Any] = {
            "version": STATE_VERSION,
            "repo": repo,
            "issue": issue,
            "title": title,
            "slug": slug,
            "repo_root": str(repo_root),
            "worktree": "",
            "branch": "",
            "base": "",
            "pr": None,
            "flow": dataclasses.asdict(FlowState()),
            "data": {},
            "directions": [],
            "protected": [],
            "created": _now_iso(),
        }
        flow = cls(path)
        atomic_write_text(path / "state.json", json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return flow

    @classmethod
    def find(cls, repo: str, issue: int) -> Optional["FlowDir"]:
        """The flow directory for an issue, or None."""
        base = repo_dir(repo)
        for child in sorted(base.glob(f"{issue}-*")):
            if child.is_dir() and (child / "state.json").exists():
                return cls(child)
        return None

    @classmethod
    def all(cls, repo: Optional[str] = None) -> List["FlowDir"]:
        """Every flow directory (one that holds state.json) for one repo, or for all repos."""
        if repo is not None:
            repos = [repo_dir(repo)]
        else:
            repos = sorted(p for p in flow_home().iterdir() if p.is_dir())
        found: List[FlowDir] = []
        for rdir in repos:
            for child in sorted(rdir.iterdir()):
                if child.is_dir() and (child / "state.json").exists():
                    found.append(cls(child))
        return found

    @property
    def name(self) -> str:
        return self.path.name

    # -- locking ------------------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(str(self.path / ".lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    # -- state.json ---------------------------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        doc = json.loads((self.path / "state.json").read_text())
        if not isinstance(doc, dict):
            raise ValueError(f"state.json is not an object: {self.path}")
        return doc

    def load(self) -> Dict[str, Any]:
        """The whole state.json document."""
        return self._read()

    def update(self, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Under the flow lock: load state.json, apply fn to it in place, save atomically. Returns it."""
        with self._locked():
            doc = self._read()
            fn(doc)
            atomic_write_text(self.path / "state.json", json.dumps(doc, indent=2, sort_keys=True) + "\n")
            return doc

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        def apply(doc: Dict[str, Any]) -> None:
            doc[key] = value
        self.update(apply)

    def flow_state(self) -> FlowState:
        raw = self._read()["flow"]
        stop = raw.get("stop")
        return FlowState(
            stage=str(raw["stage"]), status=str(raw["status"]),
            review_round=int(raw.get("review_round", 0)), sendbacks=int(raw.get("sendbacks", 0)),
            stop=_stop_from_dict(stop) if isinstance(stop, dict) else None,
        )

    def set_flow_state(self, st: FlowState) -> None:
        self.set("flow", dataclasses.asdict(st))

    def data(self, stage: str) -> Dict[str, Any]:
        """A stage's working data; {} when none."""
        value = self._read().get("data", {}).get(stage)
        return dict(value) if isinstance(value, dict) else {}

    def set_data(self, stage: str, value: Dict[str, Any]) -> None:
        def apply(doc: Dict[str, Any]) -> None:
            doc.setdefault("data", {})[stage] = value
        self.update(apply)

    # -- events -------------------------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        """Append one line to events.jsonl: ts (UTC ISO-8601), kind, then the fields."""
        row: Dict[str, Any] = {"ts": _now_iso(), "kind": kind}
        row.update(fields)
        line = (json.dumps(row, sort_keys=False, default=str) + "\n").encode("utf-8")
        with self._locked():
            fd = os.open(str(self.path / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)

    def events(self) -> List[Dict[str, Any]]:
        path = self.path / "events.jsonl"
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def total_cost(self) -> float:
        """Sum of cost_usd over "call" events."""
        total = 0.0
        for row in self.events():
            if row.get("kind") == "call":
                try:
                    total += float(row.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    continue
        return total

    # -- calls --------------------------------------------------------------------------------

    def next_call_dir(self, role: str) -> Path:
        """Create and return calls/<nnn>-<role>/ with the next free number. Safe across threads."""
        safe_role = re.sub(r"[^A-Za-z0-9_.-]+", "-", role).strip("-") or "call"
        calls = self.path / "calls"
        with self._locked():
            calls.mkdir(exist_ok=True)
            highest = 0
            for child in calls.iterdir():
                match = re.match(r"^(\d+)-", child.name)
                if match:
                    highest = max(highest, int(match.group(1)))
            target = calls / f"{highest + 1:03d}-{safe_role}"
            target.mkdir()
            return target
