#!/usr/bin/env python3
"""
Test suite for scripts/workflow/deps.py (#417 Step 1).

Uses temp git repos laid out as <root>/worktrees/main and <root>/worktrees/feat, and a fake npm on
PATH that records its argv to a marker file. No network, and nothing under the real ~/.claude.

Run with: python3 tests/test_workflow_deps.py
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import deps
from _test_harness import Harness


GIT_PREFIX = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]

FAKE_NPM = (
    "#!/bin/sh\n"
    "echo \"$@\" >> \"$MARKER\"\n"
    "mkdir -p node_modules\n"
    "if [ -n \"$FAKE_FAIL\" ]; then\n"
    "  echo 'registry _authToken=secret123 ok'\n"
    "  echo 'Authorization: Bearer topsecret'\n"
    "  exit 1\n"
    "fi\n"
    "exit 0\n"
)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(GIT_PREFIX + list(args), cwd=str(cwd), check=True, capture_output=True, text=True)


def make_layout(root: Path, files: Dict[str, str]) -> Tuple[Path, Path]:
    """Create <root>/worktrees/main with files committed, and a linked worktree <root>/worktrees/feat."""
    main = root / "worktrees" / "main"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    for name, text in files.items():
        path = main / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(main, "add", "-A")
    git(main, "commit", "-m", "init")
    feat = root / "worktrees" / "feat"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "feat", str(feat)],
        check=True, capture_output=True,
    )
    return main, feat


@contextlib.contextmanager
def fake_npm(root: Path, fail: bool = False) -> Iterator[Path]:
    """Put a fake npm first on PATH; yield the marker file that records each invocation's argv."""
    bin_dir = root / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    npm = bin_dir / "npm"
    npm.write_text(FAKE_NPM)
    npm.chmod(0o755)
    marker = root / "marker.log"
    saved = {key: os.environ.get(key) for key in ("PATH", "MARKER", "FAKE_FAIL")}
    os.environ["PATH"] = str(bin_dir) + os.pathsep + (saved["PATH"] or "")
    os.environ["MARKER"] = str(marker)
    if fail:
        os.environ["FAKE_FAIL"] = "1"
    else:
        os.environ.pop("FAKE_FAIL", None)
    try:
        yield marker
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def marker_lines(marker: Path) -> list:
    if not marker.exists():
        return []
    return [line for line in marker.read_text().splitlines() if line.strip()]


def stamp_file(feat: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(feat), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip()) / "claude-helpers" / "deps-stamp.json"


NPM_LOCK = json.dumps({"lockfileVersion": 3, "packages": {}})
BASE_PACKAGE = json.dumps({"name": "x", "version": "1.0.0", "dependencies": {}})


if __name__ == "__main__":
    h = Harness("WORKFLOW DEPS TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Lockfile table and manager detection")

    test_result(
        "table maps every lockfile to a manager that has a frozen install argv",
        set(deps.LOCKFILE_MANAGERS.values()) == set(deps.MANAGER_INSTALL_ARGV),
        f"managers={set(deps.LOCKFILE_MANAGERS.values())} argv={set(deps.MANAGER_INSTALL_ARGV)}",
    )
    frozen = all(
        "--frozen-lockfile" in argv or "--immutable" in argv or argv == ("npm", "ci")
        for argv in deps.MANAGER_INSTALL_ARGV.values()
    )
    test_result("every table install argv is frozen or immutable", frozen)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "one").mkdir()
        (root / "one" / "package-lock.json").write_text(NPM_LOCK)
        lock, ambiguous = deps.find_lockfile(root / "one")
        test_result(
            "a single package-lock.json selects npm",
            lock is not None and lock.manager == "npm" and ambiguous is None,
            f"lock={lock} ambiguous={ambiguous}",
        )

        (root / "one" / "yarn.lock").write_text("")
        lock, ambiguous = deps.find_lockfile(root / "one")
        test_result(
            "two table lockfiles are ambiguous (no lockfile selected)",
            lock is None and ambiguous is not None and "package-lock.json" in ambiguous,
            f"lock={lock} ambiguous={ambiguous}",
        )

        (root / "two").mkdir()
        (root / "two" / "pnpm-lock.yaml").write_text("")
        (root / "two" / "package.json").write_text(json.dumps({"packageManager": "npm@10.0.0"}))
        lock, ambiguous = deps.find_lockfile(root / "two")
        test_result(
            "packageManager naming a different manager than the lockfile is ambiguous",
            lock is None and ambiguous is not None and "packageManager" in ambiguous,
            f"lock={lock} ambiguous={ambiguous}",
        )

        (root / "three").mkdir()
        lock, ambiguous = deps.find_lockfile(root / "three")
        test_result("no lockfile yields neither a lockfile nor an ambiguity", lock is None and ambiguous is None)

    print("[Section 2] Redaction and tail")

    redacted = deps.redact_output(
        "a _authToken=xyz\nAuthorization: Bearer q\n_password = pw\nfetch https://user:pw@host/x"
    )
    joined = "\n".join(redacted)
    test_result(
        "credentials are masked in install output",
        "xyz" not in joined and "Bearer q" not in joined and "pw@host" not in joined
        and "_authToken=***" in joined and "***@host" in joined,
        f"out={joined!r}",
    )
    test_result(
        "the redacted tail keeps at most the last 40 lines",
        len(deps.redact_output("\n".join(str(i) for i in range(100)))) == 40,
    )

    print("[Section 3] ensure_deps outcomes")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"README.md": "x\n"})
        result = deps.ensure_deps(feat, main)
        test_result(
            "no lockfile and no commands.install is skipped (exit 5)",
            result.status == "skipped" and result.exit_code == 5,
            f"status={result.status}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "yarn.lock": ""})
        result = deps.ensure_deps(feat, main)
        test_result(
            "two lockfiles are reported ambiguous (exit 3)",
            result.status == "ambiguous" and result.exit_code == 3,
            f"status={result.status} detail={result.detail}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, _feat = make_layout(root, {"package-lock.json": NPM_LOCK})
        outside = root / "elsewhere"
        outside.mkdir()
        result = deps.ensure_deps(outside, main)
        test_result(
            "a worktree outside the worktree parent is refused before any git work (failed)",
            result.status == "failed" and "outside" in (result.detail or ""),
            f"status={result.status} detail={result.detail}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        with fake_npm(root) as marker:
            first = deps.ensure_deps(feat, main)
            second = deps.ensure_deps(feat, main)
            test_result(
                "first run installs with the frozen argv (npm ci)",
                first.status == "installed" and marker_lines(marker) == ["ci"],
                f"status={first.status} marker={marker_lines(marker)}",
            )
            test_result(
                "second run with a matching stamp and artifact is current and does not reinstall",
                second.status == "current" and marker_lines(marker) == ["ci"],
                f"status={second.status} marker={marker_lines(marker)}",
            )
            test_result("the stamp lives in the worktree's git dir", stamp_file(feat).exists())

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        (main / ".claude").mkdir()
        (main / ".claude" / "project.yaml").write_text("commands:\n  install: npm install --frozen-lockfile\n")
        (feat / ".claude").mkdir()
        (feat / ".claude" / "project.yaml").write_text("commands:\n  install: npm ci --ignore-scripts\n")
        with fake_npm(root) as marker:
            result = deps.ensure_deps(feat, main)
            test_result(
                "commands.install comes from the MAIN project.yaml, not the worktree's copy",
                result.status == "installed" and marker_lines(marker) == ["install --frozen-lockfile"],
                f"status={result.status} marker={marker_lines(marker)}",
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        (main / ".claude").mkdir()
        (main / ".claude" / "project.yaml").write_text("commands: [unclosed\n")
        with fake_npm(root) as marker:
            result = deps.ensure_deps(feat, main)
            test_result(
                "an unreadable main project.yaml fails closed instead of falling back to the lockfile default",
                result.status == "failed" and marker_lines(marker) == [],
                f"status={result.status} detail={result.detail}",
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        (main / ".claude").mkdir()
        (main / ".claude" / "project.yaml").write_text("commands:\n  install: 'npm ci\n")
        with fake_npm(root) as marker:
            result = deps.ensure_deps(feat, main)
            test_result(
                "a commands.install that does not shell-split is failed without running anything",
                result.status == "failed" and marker_lines(marker) == [],
                f"status={result.status} detail={result.detail}",
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        with fake_npm(root, fail=True):
            failed = deps.ensure_deps(feat, main)
        joined = "\n".join(failed.output_lines())
        test_result(
            "a failing install is failed (exit 1) with a redacted tail",
            failed.status == "failed" and failed.exit_code == 1
            and "secret123" not in joined and "topsecret" not in joined
            and "_authToken=***" in joined and "Authorization: ***" in joined,
            f"status={failed.status} out={joined!r}",
        )
        with fake_npm(root) as marker:
            retry = deps.ensure_deps(feat, main)
            test_result(
                "after a failed install the stamp is absent, so the next run reinstalls rather than reporting current",
                retry.status == "installed" and len(marker_lines(marker)) == 2,
                f"status={retry.status} marker={marker_lines(marker)}",
            )

    print("[Section 4] Manifest gate")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        changed = json.loads(BASE_PACKAGE)
        changed["dependencies"] = {"left-pad": "1.3.0"}
        (feat / "package.json").write_text(json.dumps(changed))
        with fake_npm(root) as marker:
            gated = deps.ensure_deps(feat, main, base="main")
            test_result(
                "an uncommitted dependency change against the base stops at manifest-changed (exit 4)",
                gated.status == "manifest-changed" and gated.exit_code == 4
                and "left-pad" in (gated.detail or "") and marker_lines(marker) == [],
                f"status={gated.status} detail={gated.detail}",
            )
            approved = deps.ensure_deps(feat, main, base="main", approve_manifest=True)
            test_result(
                "approving the current manifest hash clears the gate and installs",
                approved.status == "installed" and marker_lines(marker) == ["ci"],
                f"status={approved.status} marker={marker_lines(marker)}",
            )
            after = deps.ensure_deps(feat, main, base="main")
            test_result(
                "after approval the gate stays clear and the install is current",
                after.status == "current",
                f"status={after.status}",
            )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        with fake_npm(root):
            missing_base = deps.ensure_deps(feat, main, base="no-such-ref")
            test_result(
                "a base ref that has no merge-base with HEAD fails the gate closed",
                missing_base.status == "failed" and "merge-base" in (missing_base.detail or ""),
                f"status={missing_base.status} detail={missing_base.detail}",
            )

    print()
    h.summarize_and_exit()
