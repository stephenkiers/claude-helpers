#!/usr/bin/env python3
"""
Spec-derived tests for the expert-fleet heal work (#425), written from the action plan's "Doing it"
table rather than from the implementation. They pin the behaviours the other heal/fleet suites leave
open:

1. deps_currency and ensure_deps agree on whether a worktree needs an install (plan row 3: one shared
   assessor, so the heal's verdict can never drift from what `deps ensure` does).
2. `heal probe` exits 0 once its arguments parse, whatever the outcome; a usage error exits 2 (plan
   row 21: the CLI contract the docstring and help state).
3. fleet._default_base fails closed when gh gives no usable answer (plan row 22).

Every case calls the real production symbol: `scripts.workflow.deps` in-process, the
`scripts.workflow.cli heal probe` entry point as a subprocess, and `scripts/fleet.py` loaded from its
path. Fake npm and fake gh binaries go first on PATH; nothing touches the network or the real ~/.claude.

Run with: python3 tests/test_heal_fleet_spec.py
"""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from workflow import deps  # noqa: E402

FLEET_SCRIPT = REPO_ROOT / "scripts" / "fleet.py"

# Fixtures are duplicated on purpose from tests/test_workflow_deps.py: each suite runs standalone with
# its own fake package manager and layout (the same convention the other deps suites follow).
GIT_PREFIX = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
FAKE_NPM = (
    "#!/bin/sh\n"
    "echo \"$@\" >> \"$MARKER\"\n"
    "mkdir -p node_modules\n"
    "exit 0\n"
)
NPM_LOCK = '{"lockfileVersion": 3, "packages": {}}\n'
BASE_PACKAGE = '{"name": "x", "version": "1.0.0", "dependencies": {}}\n'


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


def stamp_file(feat: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(feat), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip()) / "claude-helpers" / "deps-stamp.json"


def marker_lines(marker: Path) -> list:
    if not marker.exists():
        return []
    return [line for line in marker.read_text().splitlines() if line.strip()]


@contextlib.contextmanager
def fake_on_path(root: Path, name: str, body: str) -> Iterator[Path]:
    """Put an executable `name` with `body` first on PATH for the duration; yield the bin directory."""
    bin_dir = root / f"fakebin-{name}"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(body)
    exe.chmod(0o755)
    saved_path = os.environ.get("PATH")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + (saved_path or "")
    try:
        yield bin_dir
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path


@contextlib.contextmanager
def fake_npm(root: Path) -> Iterator[Path]:
    """Fake npm on PATH; yields the marker file that records each invocation's argv."""
    marker = root / "marker.log"
    saved = os.environ.get("MARKER")
    os.environ["MARKER"] = str(marker)
    try:
        with fake_on_path(root, "npm", FAKE_NPM):
            yield marker
    finally:
        if saved is None:
            os.environ.pop("MARKER", None)
        else:
            os.environ["MARKER"] = saved


def load_fleet():
    """Load scripts/fleet.py from its path, the same way tests/test_fleet_invariants.py does."""
    spec = importlib.util.spec_from_file_location("fleet", FLEET_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ────────────────────────────────────────────────────────────────────────────
# Row 3: deps_currency and ensure_deps agree
# ────────────────────────────────────────────────────────────────────────────

def section_currency_agrees_with_ensure(h: Harness) -> None:
    print("[Row 3] deps_currency and ensure_deps agree on whether an install is needed")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": NPM_LOCK, "package.json": BASE_PACKAGE})
        with fake_npm(root) as marker:
            before = deps.deps_currency(feat, main, "main")
            h.test_result(
                "currency: a never-installed worktree is not 'current'",
                before != "current",
                f"verdict={before!r}",
            )

            first = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: the never-installed worktree installs (the verdict above said it needed one)",
                first.status == "installed" and marker_lines(marker) == ["ci"],
                f"status={first.status} marker={marker_lines(marker)}",
            )

            after_install = deps.deps_currency(feat, main, "main")
            h.test_result(
                "currency: after a successful install the worktree reads 'current'",
                after_install == "current",
                f"verdict={after_install!r}",
            )

            second = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: a worktree that reads 'current' does not reinstall",
                second.status == "current" and marker_lines(marker) == ["ci"],
                f"status={second.status} marker={marker_lines(marker)}",
            )

            (feat / "package-lock.json").write_text(NPM_LOCK.replace("3", "2"))
            changed = deps.deps_currency(feat, main, "main")
            h.test_result(
                "currency: an edited lockfile no longer reads 'current'",
                changed != "current",
                f"verdict={changed!r}",
            )
            changed_run = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: an edited lockfile reinstalls (the verdict above said it needed one)",
                changed_run.status == "installed",
                f"status={changed_run.status}",
            )

            # Restore the committed lockfile, then drop the stamp: a missing stamp must read the same way.
            subprocess.run(["git", "-C", str(feat), "checkout", "--", "package-lock.json"],
                           check=True, capture_output=True)
            restored = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: restoring the committed lockfile reinstalls once more (stamp no longer matches)",
                restored.status == "installed",
                f"status={restored.status}",
            )
            stamp = stamp_file(feat)
            ensure_again = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: the stamp is present again after a reinstall (precondition for the next case)",
                stamp.exists() and ensure_again.status == "current",
                f"stamp={stamp.exists()} status={ensure_again.status}",
            )
            stamp.unlink()
            missing = deps.deps_currency(feat, main, "main")
            h.test_result(
                "currency: a deleted stamp does not read 'current'",
                missing != "current",
                f"verdict={missing!r}",
            )
            missing_run = deps.ensure_deps(feat, main)
            h.test_result(
                "ensure: a deleted stamp reinstalls (the verdict above said it needed one)",
                missing_run.status == "installed",
                f"status={missing_run.status}",
            )


# ────────────────────────────────────────────────────────────────────────────
# Row 21: `heal probe` exit-code contract
# ────────────────────────────────────────────────────────────────────────────

def run_heal_probe(args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "heal", "probe", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )


def parse_json_object(text: str) -> Optional[dict]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def section_probe_exit_contract(h: Harness) -> None:
    print("[Row 21] heal probe exits 0 once its arguments parse, whatever the outcome")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"README.md": "x\n"})
        flow_dir = root / "flow"
        flow_dir.mkdir()

        refused = run_heal_probe([
            "--worktree", str(main), "--main-worktree", str(main),
            "--base", "main", "--flow-dir", str(flow_dir), "--issue", "42",
        ])
        data = parse_json_object(refused.stdout)
        h.test_result(
            "probe on the main worktree (a refusal) exits 0",
            refused.returncode == 0,
            f"rc={refused.returncode} stderr={refused.stderr.strip()[:200]!r}",
        )
        h.test_result(
            "probe refusal prints a JSON object that carries a stop_key field",
            data is not None and "stop_key" in data,
            f"stdout={refused.stdout.strip()[:200]!r}",
        )

        nothing = run_heal_probe([
            "--worktree", str(feat), "--main-worktree", str(main),
            "--base", "main", "--flow-dir", str(flow_dir), "--issue", "42",
        ])
        nothing_data = parse_json_object(nothing.stdout)
        h.test_result(
            "probe with no stop to heal exits 0 and prints a JSON object",
            nothing.returncode == 0 and nothing_data is not None,
            f"rc={nothing.returncode} stdout={nothing.stdout.strip()[:200]!r}",
        )

        usage = run_heal_probe([
            "--worktree", str(feat), "--main-worktree", str(main), "--flow-dir", str(flow_dir),
        ])
        h.test_result(
            "probe missing a required argument is a usage error and exits 2",
            usage.returncode == 2,
            f"rc={usage.returncode} stderr={usage.stderr.strip()[:200]!r}",
        )


# ────────────────────────────────────────────────────────────────────────────
# Row 22: fleet._default_base fails closed
# ────────────────────────────────────────────────────────────────────────────

GH_ANSWERS_MAIN = "#!/bin/sh\necho main\nexit 0\n"
GH_FAILS = "#!/bin/sh\necho 'gh: not authenticated' >&2\nexit 1\n"
GH_SILENT = "#!/bin/sh\nexit 0\n"


def section_default_base_fails_closed(h: Harness) -> None:
    print("[Row 22] fleet._default_base fails closed when gh gives no usable answer")
    fl = load_fleet()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, _feat = make_layout(root, {"README.md": "x\n"})
        bare = root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(main), str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(main), "remote", "add", "origin", str(bare)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(main), "fetch", "-q", "origin"], check=True, capture_output=True)

        with fake_on_path(root, "gh", GH_ANSWERS_MAIN):
            control = fl._default_base("owner/name", main)
        h.test_result(
            "control: with gh answering 'main' and origin/main present, the base is 'origin/main'",
            control == "origin/main",
            f"got {control!r}",
        )

        with fake_on_path(root, "gh", GH_FAILS):
            failed = fl._default_base("owner/name", main)
        h.test_result(
            "gh exiting non-zero gives no base (None), never a guessed one",
            failed is None,
            f"got {failed!r}",
        )

        with fake_on_path(root, "gh", GH_SILENT):
            silent = fl._default_base("owner/name", main)
        h.test_result(
            "gh exiting zero with no output gives no base (None)",
            silent is None,
            f"got {silent!r}",
        )


def main() -> None:
    h = Harness("heal and fleet spec tests (#425 plan rows 3, 21, 22)")
    section_currency_agrees_with_ensure(h)
    section_probe_exit_contract(h)
    section_default_base_fails_closed(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
