#!/usr/bin/env python3
"""
Test suite for the `deps ensure` CLI (#417 Step 1): exit codes and DEPS: output lines.

Runs `python3 -m scripts.workflow.cli deps ensure` as a subprocess with HOME and PATH pointed at
temp dirs and a fake npm. No network, and nothing under the real ~/.claude.

Run with: python3 tests/test_workflow_deps_cli.py
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import Harness


# Fixtures (GIT_PREFIX, FAKE_NPM, make_layout, write_fake_npm) are duplicated on purpose from
# test_workflow_deps.py and test_workflow_deps_spec_blind.py: each suite runs standalone, so one shared
# helper would couple three independent suites' fixtures.
REPO_ROOT = Path(__file__).parent.parent
GIT_PREFIX = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]

FAKE_NPM = (
    "#!/bin/sh\n"
    "echo \"$@\" >> \"$MARKER\"\n"
    "mkdir -p node_modules\n"
    "if [ -n \"$FAKE_FAIL\" ]; then\n"
    "  echo 'registry _authToken=secret123'\n"
    "  exit 1\n"
    "fi\n"
    "exit 0\n"
)

NPM_LOCK = json.dumps({"lockfileVersion": 3, "packages": {}})
BASE_PACKAGE = json.dumps({"name": "x", "version": "1.0.0", "dependencies": {}})


def git(cwd: Path, *args: str) -> None:
    subprocess.run(GIT_PREFIX + list(args), cwd=str(cwd), check=True, capture_output=True, text=True)


def make_layout(root: Path, files: Dict[str, str]) -> Tuple[Path, Path]:
    """Create <root>/worktrees/main with files committed, and a linked worktree <root>/worktrees/feat."""
    main = root / "worktrees" / "main"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    for name, text in files.items():
        (main / name).write_text(text)
    git(main, "add", "-A")
    git(main, "commit", "-m", "init")
    feat = root / "worktrees" / "feat"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "feat", str(feat)],
        check=True, capture_output=True,
    )
    return main, feat


def write_fake_npm(root: Path) -> Path:
    bin_dir = root / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    npm = bin_dir / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    return bin_dir


def run_deps(
    root: Path, worktree: Path, main: Path, extra: Optional[List[str]] = None, fail: bool = False
) -> subprocess.CompletedProcess:
    bin_dir = write_fake_npm(root)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["MARKER"] = str(root / "marker.log")
    env["HOME"] = str(root / "home")
    env.pop("FAKE_FAIL", None)
    if fail:
        env["FAKE_FAIL"] = "1"
    args = [
        sys.executable, "-m", "scripts.workflow.cli", "deps", "ensure",
        "--worktree", str(worktree), "--main-worktree", str(main),
    ] + (extra or [])
    return subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)


if __name__ == "__main__":
    h = Harness("WORKFLOW DEPS CLI TEST SUITE")
    test_result = h.test_result

    print("[Section 1] deps ensure exit codes and output")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"README.md": "x\n"})
        proc = run_deps(root, feat, main)
        test_result(
            "no lockfile prints 'DEPS: skipped: no lockfile' and exits 5",
            proc.returncode == 5 and proc.stdout.strip() == "DEPS: skipped: no lockfile",
            f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr[-300:]!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "yarn.lock": ""})
        proc = run_deps(root, feat, main)
        test_result(
            "ambiguous lockfiles exit 3 with DEPS: ambiguous",
            proc.returncode == 3 and proc.stdout.startswith("DEPS: ambiguous: "),
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        first = run_deps(root, feat, main)
        second = run_deps(root, feat, main)
        test_result(
            "first run exits 0 with DEPS: installed",
            first.returncode == 0 and first.stdout.strip() == "DEPS: installed",
            f"rc={first.returncode} out={first.stdout!r} err={first.stderr[-300:]!r}",
        )
        test_result(
            "second run exits 0 with DEPS: current",
            second.returncode == 0 and second.stdout.strip() == "DEPS: current",
            f"rc={second.returncode} out={second.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        proc = run_deps(root, feat, main, fail=True)
        test_result(
            "a failed install exits 1, prints DEPS: failed, and the tail is redacted",
            proc.returncode == 1 and proc.stdout.startswith("DEPS: failed: exit 1")
            and "secret123" not in proc.stdout and "_authToken=***" in proc.stdout,
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        (feat / "package.json").write_text(json.dumps({"name": "x", "dependencies": {"left-pad": "1.3.0"}}))
        proc = run_deps(root, feat, main, extra=["--base", "main"])
        test_result(
            "an unapproved manifest change exits 4 with DEPS: manifest-changed naming the dependency",
            proc.returncode == 4 and proc.stdout.startswith("DEPS: manifest-changed: ") and "left-pad" in proc.stdout,
            f"rc={proc.returncode} out={proc.stdout!r}",
        )
        approved = run_deps(root, feat, main, extra=["--base", "main", "--approve-manifest"])
        test_result(
            "--approve-manifest clears the gate and exits 0",
            approved.returncode == 0 and approved.stdout.strip() == "DEPS: installed",
            f"rc={approved.returncode} out={approved.stdout!r} err={approved.stderr[-300:]!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        outside = root / "elsewhere"
        outside.mkdir()
        proc = run_deps(root, outside, main)
        test_result(
            "a worktree outside the worktree parent exits 1 before any install",
            proc.returncode == 1 and "outside" in proc.stdout,
            f"rc={proc.returncode} out={proc.stdout!r}",
        )

    print("[Section 2] deps ensure argument handling")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK})
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "deps", "ensure", "--worktree", str(feat)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        test_result(
            "a missing --main-worktree is an argparse usage error (exit 2)",
            proc.returncode == 2,
            f"rc={proc.returncode} err={proc.stderr[-200:]!r}",
        )

    print("[Section 3] an interrupted install still prints a DEPS line and kills the installer group")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK})
        bin_dir = root / "fakebin"
        bin_dir.mkdir()
        slow = bin_dir / "npm"
        slow.write_text("#!/bin/sh\necho $$ > \"$MARKER\"\nsleep 60\n")
        slow.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["MARKER"] = str(root / "pid.txt")
        env["HOME"] = str(root / "home")
        child = subprocess.Popen(
            [sys.executable, "-m", "scripts.workflow.cli", "deps", "ensure",
             "--worktree", str(feat), "--main-worktree", str(main)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        marker = root / "pid.txt"
        deadline = time.time() + 20
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=30)
        test_result(
            "SIGTERM mid-install exits 143 and prints 'DEPS: failed: interrupted'",
            child.returncode == 143 and "DEPS: failed: interrupted" in out,
            f"rc={child.returncode} out={out!r} err={err[-200:]!r}",
        )
        installer_alive = True
        try:
            os.kill(int(marker.read_text().strip()), 0)
        except (OSError, ValueError):
            installer_alive = False
        test_result("the installer process is gone after SIGTERM", not installer_alive, "installer still running")

    print()
    h.summarize_and_exit()
