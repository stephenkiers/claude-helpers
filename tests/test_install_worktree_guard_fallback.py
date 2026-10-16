#!/usr/bin/env python3
"""
Test suite for install.sh's linked-worktree guard on git builds that predate
`rev-parse --path-format` (git < 2.31).

The guard must not fail open: when the git on PATH rejects `--path-format`, a
linked-worktree install must still be refused, or at minimum must say something
on stderr instead of silently linking from the linked worktree.

Method: a `git` shim is placed first on PATH. It rejects any argument starting
with `--path-format` the way an old git does, and otherwise delegates to the real
git. The shim is a test double for the environment only; install.sh is run as-is.

Run with: python3 tests/test_install_worktree_guard_fallback.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

INSTALL_SH = REPO_ROOT / "install.sh"

GIT_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_VARS}
    env["HOME"] = str(home)
    return env


def make_shim_dir(tmpdir_path, real_git):
    """A directory holding a `git` that refuses --path-format like git < 2.31."""
    shim_dir = tmpdir_path / "shim-bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  case \"$a\" in\n"
        "    --path-format*) echo \"error: unknown option \\`path-format'\" >&2; exit 129 ;;\n"
        "  esac\n"
        "done\n"
        f"exec \"{real_git}\" \"$@\"\n"
    )
    shim.chmod(0o755)
    return shim_dir


def build_fixture(tmpdir_path):
    """Main repo with install.sh, one commit, and a linked worktree. Returns (main, linked)."""
    main_repo = tmpdir_path / "main"
    (main_repo / "commands").mkdir(parents=True)
    (main_repo / "commands" / "test_cmd.md").write_text("test command\n")
    for d in ("reviewers", "prompts", "agents", "scripts"):
        (main_repo / d).mkdir()
        (main_repo / d / "keep.md").write_text("keep\n")
    (main_repo / "prompts" / "preferences.yaml.template").write_text("{}\n")
    shutil.copy(INSTALL_SH, main_repo / "install.sh")
    (main_repo / "install.sh").chmod(0o755)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(main_repo)]
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    linked = tmpdir_path / "linked"
    subprocess.run(git + ["worktree", "add", "-q", str(linked)], check=True)
    return main_repo, linked


def run_install(args, cwd, env):
    # Run the fixture's own copy of install.sh: the script resolves REPO_DIR from its
    # own location, so running the repo's INSTALL_SH would guard the real checkout
    # (and pass or fail with whichever worktree the suite happens to run from).
    result = subprocess.run(
        ["bash", str(Path(cwd) / "install.sh")] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness("install.sh linked-worktree guard on git without --path-format")

    real_git = shutil.which("git")
    if not real_git:
        h.test_result("git is available on PATH", False, "no git binary found")
        h.summarize_and_exit()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir_path = Path(tmp)
        home = tmpdir_path / "home"
        home.mkdir()
        _main_repo, linked = build_fixture(tmpdir_path)
        shim_dir = make_shim_dir(tmpdir_path, real_git)

        # Control: with the real git, a linked-worktree install is refused.
        code, _, stderr = run_install([], linked, clean_env(home))
        h.test_result(
            "control: real git refuses install from a linked worktree",
            code != 0 and "linked worktree" in stderr,
            f"code={code} stderr={stderr!r}",
        )

        # Old-git shim: the guard must not silently allow the install.
        shim_env = clean_env(home)
        shim_env["PATH"] = f"{shim_dir}{os.pathsep}{shim_env.get('PATH', '')}"
        code, _, stderr = run_install([], linked, shim_env)
        refused = code != 0 and "linked worktree" in stderr
        warned = code == 0 and stderr.strip() != "" and (
            "warn" in stderr.lower() or "linked worktree" in stderr.lower()
        )
        h.test_result(
            "old git (no --path-format): linked-worktree install is refused or warned, never silent",
            refused or warned,
            f"code={code} stderr={stderr!r}",
        )

        # The shim must actually be in effect, or the case above proves nothing.
        probe = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(linked), capture_output=True, text=True, env=shim_env,
        )
        h.test_result(
            "shim precondition: --path-format is rejected through the shim",
            probe.returncode != 0,
            f"rc={probe.returncode} stdout={probe.stdout!r}",
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
