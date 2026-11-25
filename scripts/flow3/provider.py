"""
Issue, pull request, and merge operations behind one interface. `LocalProvider` keeps its data inside
the bare remote that `origin` points at (for tests and sandbox runs); the GitHub provider is a later unit.

Layout under the bare remote: flow3/issues/<n>.md (first line `# <title>`, then the body; a line
`Depends on: #4, #5`), flow3/issues/<n>.closed when closed, flow3/prs/<n>.json.
"""

import argparse
import fcntl
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scripts.flow3 import gitops
from scripts.flow3.repoconfig import RepoConfig, repo_name
from scripts.workflow.fsutil import atomic_write_text, read_json_object


class ProviderError(Exception):
    """A provider operation failed."""


class BaseMoved(ProviderError):
    """The merge was refused because base moved (or the branch no longer applies to it)."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    depends_on: Tuple[int, ...]


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str
    base: str
    state: str  # "open" | "merged" | "closed"


class Provider:
    name = ""

    def get_issue(self, number: int) -> Issue:
        raise NotImplementedError

    def find_pr(self, branch: str) -> Optional[PullRequest]:
        raise NotImplementedError

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        raise NotImplementedError

    def pr_state(self, number: int) -> str:
        raise NotImplementedError

    def merge_pr(self, number: int, head_sha: str, title: str) -> None:
        raise NotImplementedError


_DEPENDS = re.compile(r"^\s*Depends on:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_CLOSES = re.compile(r"\bCloses\s+#(\d+)", re.IGNORECASE)


def _origin_dir(repo_root: Path) -> Path:
    url = gitops.git(["remote", "get-url", "origin"], repo_root, check=False)
    if not url:
        raise ProviderError("the repo has no origin remote")
    if url.startswith("file://"):
        url = url[len("file://"):]
    path = Path(url)
    if not path.is_absolute():
        path = repo_root / path
    if not path.is_dir():
        raise ProviderError("origin is not a local directory: %s" % url)
    return path.resolve()


class LocalProvider(Provider):
    name = "local"

    def __init__(self, origin: Path, repo: str) -> None:
        self.origin = origin
        self.repo = repo
        self.data = origin / "flow3"
        (self.data / "issues").mkdir(parents=True, exist_ok=True)
        (self.data / "prs").mkdir(parents=True, exist_ok=True)

    # -- issues ------------------------------------------------------------------------------
    def _issue_file(self, number: int) -> Path:
        return self.data / "issues" / ("%d.md" % number)

    def get_issue(self, number: int) -> Issue:
        path = self._issue_file(number)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise ProviderError("issue #%d not found" % number)
        first, _, rest = text.partition("\n")
        title = first[1:].strip() if first.startswith("#") else first.strip()
        deps: List[int] = []
        for match in _DEPENDS.finditer(rest):
            deps += [int(n) for n in re.findall(r"#(\d+)", match.group(1))]
        closed = (self.data / "issues" / ("%d.closed" % number)).exists()
        return Issue(number=number, title=title, body=rest.strip("\n"),
                     state="closed" if closed else "open", depends_on=tuple(deps))

    def create_issue(self, title: str, body: str, depends_on: Sequence[int] = ()) -> int:
        with self._lock("issues"):
            nums = [int(p.stem) for p in (self.data / "issues").glob("*.md") if p.stem.isdigit()]
            number = max(nums) + 1 if nums else 1
            lines = ["# " + title.strip(), ""]
            if depends_on:
                lines += ["Depends on: " + ", ".join("#%d" % d for d in depends_on), ""]
            lines.append(body)
            atomic_write_text(self._issue_file(number), "\n".join(lines) + "\n")
        return number

    # -- pull requests -----------------------------------------------------------------------
    def _lock(self, name: str) -> "_Lock":
        return _Lock(self.data / ("." + name + ".lock"))

    def _pr_file(self, number: int) -> Path:
        return self.data / "prs" / ("%d.json" % number)

    def _read_pr(self, number: int) -> Dict[str, Any]:
        rec = read_json_object(self._pr_file(number))
        if rec is None:
            raise ProviderError("PR #%d not found" % number)
        return rec

    @staticmethod
    def _as_pr(rec: Dict[str, Any]) -> PullRequest:
        return PullRequest(number=int(rec["number"]), url=str(rec["url"]), branch=str(rec["branch"]),
                           base=str(rec["base"]), state=str(rec["state"]))

    def _branch_tip(self, branch: str) -> str:
        return gitops.git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], self.origin,
                          check=False)

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        if not self._branch_tip(branch):
            raise ProviderError("branch %r is not on the remote; push it first" % branch)
        with self._lock("prs"):
            nums = [int(p.stem) for p in (self.data / "prs").glob("*.json") if p.stem.isdigit()]
            number = max(nums) + 1 if nums else 1
            rec = {"number": number, "url": "local://%s/pr/%d" % (self.repo, number), "branch": branch,
                   "base": base, "title": title, "body": body, "state": "open"}
            atomic_write_text(self._pr_file(number), json.dumps(rec, indent=1) + "\n")
        return self._as_pr(rec)

    def find_pr(self, branch: str) -> Optional[PullRequest]:
        found: Optional[Dict[str, Any]] = None
        for path in sorted((self.data / "prs").glob("*.json"), key=lambda p: int(p.stem)):
            rec = read_json_object(path)
            if rec and rec.get("branch") == branch and rec.get("state") in ("open", "merged"):
                found = rec
        return self._as_pr(found) if found else None

    def pr_state(self, number: int) -> str:
        return str(self._read_pr(number)["state"])

    def merge_pr(self, number: int, head_sha: str, title: str) -> None:
        rec = self._read_pr(number)
        if rec["state"] != "open":
            raise ProviderError("PR #%d is %s, not open" % (number, rec["state"]))
        branch, base = str(rec["branch"]), str(rec["base"])
        tip = self._branch_tip(branch)
        if tip != head_sha:
            raise ProviderError("head moved: remote %s is at %s, expected %s"
                                % (branch, tip[:10] or "(missing)", head_sha[:10]))
        tmp = Path(tempfile.mkdtemp(prefix="flow3-merge-"))
        try:
            clone = tmp / "clone"
            gitops.git(["clone", "--quiet", str(self.origin), str(clone)], tmp)
            gitops.git(["config", "user.name", "flow3"], clone)
            gitops.git(["config", "user.email", "flow3@example.invalid"], clone)
            gitops.git(["checkout", "--quiet", "-B", base, "origin/" + base], clone)
            try:
                gitops.git(["merge", "--squash", "origin/" + branch], clone)
            except gitops.GitError as exc:
                raise BaseMoved("the branch no longer applies cleanly to %s: %s" % (base, exc))
            try:
                gitops.git(["commit", "-q", "-m", title], clone)
            except gitops.GitError as exc:
                raise ProviderError("nothing to merge from %s: %s" % (branch, exc))
            try:
                gitops.git(["push", "--quiet", "origin", "%s:%s" % (base, base)], clone)
            except gitops.GitError as exc:
                raise BaseMoved("push to %s was rejected, base moved: %s" % (base, exc))
            gitops.git(["push", "--quiet", "origin", "--delete", branch], clone, check=False)
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)
        rec["state"] = "merged"
        atomic_write_text(self._pr_file(number), json.dumps(rec, indent=1) + "\n")
        for num in _CLOSES.findall(str(rec.get("body", ""))):
            if self._issue_file(int(num)).exists():
                atomic_write_text(self.data / "issues" / ("%s.closed" % int(num)), "")


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh: Any = None

    def __enter__(self) -> "_Lock":
        self.fh = open(str(self.path), "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fh.close()


def local_for(repo_root: Path) -> LocalProvider:
    return LocalProvider(_origin_dir(repo_root), repo_name(repo_root))


def for_repo(repo_root: Path, cfg: RepoConfig) -> Provider:
    if cfg.provider == "local":
        return local_for(repo_root)
    raise ProviderError("github provider not built yet")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI for sandbox scripts: merge-local and new-issue. Exit 0 ok, 3 BaseMoved, 1 other errors."""
    ap = argparse.ArgumentParser(prog="python3 -m scripts.flow3.provider")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge-local")
    m.add_argument("--repo-root", required=True)
    m.add_argument("--pr", type=int, required=True)
    m.add_argument("--head", required=True)
    m.add_argument("--title", required=True)
    n = sub.add_parser("new-issue")
    n.add_argument("--repo-root", required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--body", required=True)
    n.add_argument("--depends", default="")
    args = ap.parse_args(argv)
    try:
        prov = local_for(Path(args.repo_root))
        if args.cmd == "merge-local":
            prov.merge_pr(args.pr, args.head, args.title)
        else:
            deps = [int(x) for x in args.depends.replace("#", "").split(",") if x.strip()]
            print(prov.create_issue(args.title, args.body, deps))
        return 0
    except BaseMoved as exc:
        print("base moved: %s" % exc, file=sys.stderr)
        return 3
    except (ProviderError, gitops.GitError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
