"""The merge gate must not let an installer empty another checkout through a symlinked dependency dir."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness

from workflow.merge_queue import unlink_shared_dependency_dirs

h = Harness("MERGE QUEUE SHARED DEPENDENCY DIRS TEST SUITE")
test_result = h.test_result


def git_repo(root: Path) -> Path:
    repo = root / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


# (a) A symlinked node_modules is unlinked and its target keeps every file.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = git_repo(root)
    target = root / "other-checkout-node_modules"
    target.mkdir()
    (target / "keep.js").write_text("x")
    os.symlink(target, repo / "node_modules")

    removed = unlink_shared_dependency_dirs(repo)
    test_result("(a) node_modules symlink is reported", removed == ["node_modules"], f"removed={removed}")
    test_result("(a) the link is gone", not os.path.lexists(repo / "node_modules"))
    test_result("(a) the target is untouched", (target / "keep.js").read_text() == "x")

# (b) A dangling symlink is removed too.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = git_repo(root)
    os.symlink(root / "missing", repo / ".venv")
    removed = unlink_shared_dependency_dirs(repo)
    test_result("(b) dangling .venv symlink is removed", removed == [".venv"] and not os.path.lexists(repo / ".venv"))

# (c) A real directory is never touched.
with tempfile.TemporaryDirectory() as tmp:
    repo = git_repo(Path(tmp))
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.js").write_text("y")
    removed = unlink_shared_dependency_dirs(repo)
    test_result("(c) real dir is left alone", removed == [] and (repo / "node_modules" / "pkg.js").exists())

# (d) A tracked symlink is left alone, since removing it would dirty the tree.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = git_repo(root)
    (repo / "vendored").mkdir()
    os.symlink("vendored", repo / "node_modules")
    subprocess.run(["git", "-C", str(repo), "add", "node_modules"], check=True)
    removed = unlink_shared_dependency_dirs(repo)
    test_result("(d) tracked symlink is kept", removed == [] and os.path.islink(repo / "node_modules"))

print()
h.summarize_and_exit()
