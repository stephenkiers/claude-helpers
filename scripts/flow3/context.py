"""The per-flow context handed to every stage."""

from dataclasses import dataclass
from pathlib import Path

from scripts.flow3 import provider as provider_mod
from scripts.flow3 import repoconfig
from scripts.flow3.provider import Provider
from scripts.flow3.repoconfig import RepoConfig
from scripts.flow3.state import FlowDir


@dataclass
class Ctx:
    flow: FlowDir
    cfg: RepoConfig
    repo_root: Path
    provider: Provider

    @property
    def worktree(self) -> Path:
        """The ticket worktree. Empty until the worktree stage has created it."""
        raw = str(self.flow.get("worktree") or "")
        if not raw:
            raise RuntimeError("the worktree has not been created yet")
        return Path(raw)

    @property
    def repo(self) -> str:
        return str(self.flow.get("repo"))

    @property
    def issue(self) -> int:
        return int(self.flow.get("issue"))


def build(flow: FlowDir) -> Ctx:
    """Load the repo's flow.yaml and provider for a flow. Raises ConfigError on a bad config."""
    repo_root = Path(str(flow.get("repo_root")))
    cfg = repoconfig.load(repo_root)
    return Ctx(flow=flow, cfg=cfg, repo_root=repo_root, provider=provider_mod.for_repo(repo_root, cfg))
