"""Where Flow v3 keeps its state. Tests point FLOW3_HOME at a temp directory."""

import os
from pathlib import Path


def flow_home() -> Path:
    """The state root: $FLOW3_HOME, else ~/.claude/flow3. Created on demand."""
    raw = os.environ.get("FLOW3_HOME")
    root = Path(raw) if raw else Path.home() / ".claude" / "flow3"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_home() -> Path:
    """
    Where per-repo config kept outside the repo lives: $FLOW3_CONFIG_HOME, else `claude-helpers` beside
    the state root (so ~/.claude/claude-helpers by default). Not created here: reading must not make it.
    """
    raw = os.environ.get("FLOW3_CONFIG_HOME")
    return Path(raw) if raw else flow_home().parent / "claude-helpers"


def repo_dir(repo: str) -> Path:
    """The per-repo state directory, created on demand."""
    if not repo or "/" in repo or repo in (".", ".."):
        raise ValueError("bad repo name: %r" % (repo,))
    path = flow_home() / repo
    path.mkdir(parents=True, exist_ok=True)
    return path
