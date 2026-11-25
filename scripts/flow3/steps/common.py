"""Small read-only helpers shared by the stages."""

from pathlib import Path
from typing import Any, Dict, List

import yaml  # type: ignore[import-untyped]

from scripts.flow3 import gitops

REVIEWERS_DIR = Path(__file__).resolve().parents[3] / "reviewers"


def file_map(root: Path, limit: int = 400) -> str:
    """Tracked files under `root`, one per line, cut at `limit` with a note."""
    files = [f for f in gitops.git(["ls-files"], root).splitlines() if f]
    more = len(files) - limit
    return "\n".join(files[:limit] + (["(and %d more files)" % more] if more > 0 else []))


def reviewers(context: str) -> List[Dict[str, Any]]:
    """Entries of reviewers/index.yaml that take part in `context` ("plan" or "review")."""
    data = yaml.safe_load((REVIEWERS_DIR / "index.yaml").read_text(encoding="utf-8")) or {}
    return [r for r in data.get("reviewers", [])
            if isinstance(r, dict) and context in (r.get("contexts") or {})]


def persona_text(entry: Dict[str, Any]) -> str:
    """The reviewer's persona file, as text."""
    return (REVIEWERS_DIR / str(entry.get("file"))).read_text(encoding="utf-8")
