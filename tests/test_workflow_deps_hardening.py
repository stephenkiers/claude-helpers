#!/usr/bin/env python3
"""
Hardening tests for `python3 -m scripts.workflow.cli deps ensure` (#417), written from the action plan's
"Doing it" table. They cover the deps behaviours the existing deps suites do not pin:

1. stamp freshness with no lockfile (commands.install only): a package.json edit reinstalls
2. install-config files (.npmrc, .yarnrc.yml, .pnpmfile.cjs, bunfig.toml) are part of the freshness key
   (a change reinstalls) and of the --base manifest gate (a change is manifest-changed)
3. yarn argv by major, black-box: 1.x runs --frozen-lockfile, 2.x runs --immutable
4. bun.lockb next to bun.lock is one manager (bun), not an ambiguity
5. a --base that starts with '-' is refused before any install runs
6. redaction: GitHub and npm token prefixes and GH_TOKEN / NPM_TOKEN key/value forms are masked
7. the timeout kills the whole install process group, not only the direct child
8. a symlinked stamp stays a symlink across a rewrite

Every case runs the real CLI as a subprocess against a temp layout (main worktree plus a linked worktree),
with fake package managers prepended to PATH; nothing touches the network or the real ~/.claude. The
process-group and redaction cases call the real `scripts.workflow.deps` functions in-process.

Not covered here (already pinned by the existing suites): commands.install provenance and shell-split,
yarn version unknown, the outside-parent refusal, the "could not start" OSError path, the approval
sequence, the checks provenance / opt-out / CRLF cases, and the plan-verifier check-question count.

Run with: python3 tests/test_workflow_deps_hardening.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from workflow import deps

FAKE_NAMES = ["npm", "pnpm", "yarn", "bun", "fakeinstall"]

# A fake package manager. __PY__, __LOG__, __FAIL__ and __NAME__ are replaced at creation time. It appends
# a JSON record of its argv and cwd to the log, then either prints the FAIL file and exits 1, or creates
# node_modules and exits 0.
FAKE_TEMPLATE = '''#!__PY__
import json
import os
import sys

LOG = __LOG__
FAIL = __FAIL__
NAME = __NAME__
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"name": NAME, "argv": sys.argv[1:], "cwd": os.path.realpath(os.getcwd())}) + "\\n")
if os.path.exists(FAIL):
    with open(FAIL, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    sys.exit(1)
os.makedirs("node_modules", exist_ok=True)
'''

DEPS_FIRST_LINE_OK = ("DEPS: installed", "DEPS: current")

# Token-shaped secrets. The raw values must never reach DEPS output.
GH_PAT = "ghp_" + "A" * 36
FINE_GRAINED_PAT = "github_pat_" + "B" * 40
NPM_PAT = "npm_" + "C" * 36
GH_TOKEN_VALUE = "SECRET_GH_KEY_ONE"
NPM_TOKEN_VALUE = "SECRET_NPM_KEY_TWO"
REDACT_SECRETS = [GH_PAT, FINE_GRAINED_PAT, NPM_PAT, GH_TOKEN_VALUE, NPM_TOKEN_VALUE]
REDACT_INPUT = [
    f"fetch with token {GH_PAT} failed",
    f"fine-grained clone used {FINE_GRAINED_PAT}",
    f"publish authenticated as {NPM_PAT}",
    f"env GH_TOKEN={GH_TOKEN_VALUE} was set",
    f"NPM_TOKEN: {NPM_TOKEN_VALUE}",
    "installed 42 packages in 3s",
]

INSTALL_CONFIG_FILES = [
    (".npmrc", "registry=https://registry.example.test/\n", "registry=https://mirror.example.test/\n"),
    (".yarnrc.yml", "nodeLinker: node-modules\n", "nodeLinker: pnp\n"),
    (".pnpmfile.cjs", "module.exports = { hooks: {} };\n", "module.exports = { hooks: { after: () => {} } };\n"),
    ("bunfig.toml", "[install]\ncache = false\n", "[install]\ncache = true\n"),
]


@dataclass
class Run:
    rc: Optional[int]
    stdout: str
    stderr: str
    hung: bool

    @property
    def first_line(self) -> str:
        lines = self.stdout.splitlines()
        return lines[0] if lines else ""

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=Hardening Test", "-c", "user.email=hardening@example.test",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_in(wt: Path, message: str) -> None:
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)


def package_json(deps_map: Optional[Dict[str, str]] = None, package_manager: Optional[str] = None) -> str:
    data: Dict[str, object] = {"name": "demo", "version": "1.0.0"}
    if deps_map is not None:
        data["dependencies"] = deps_map
    if package_manager is not None:
        data["packageManager"] = package_manager
    return json.dumps(data, indent=2) + "\n"


def make_layout(root: Path, tracked: Dict[str, str]):
    main = root / "repo" / "worktrees" / "main"
    wt = root / "repo" / "worktrees" / "feat"
    for rel, text in tracked.items():
        write(main / rel, text)
    git(main, "init", "-q", "-b", "main")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "base")
    git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    return main, wt


def make_bins(root: Path):
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log = root / "pm.log"
    fail = root / "fail-install"
    for name in FAKE_NAMES:
        script = (
            FAKE_TEMPLATE.replace("__PY__", sys.executable)
            .replace("__LOG__", json.dumps(str(log)))
            .replace("__FAIL__", json.dumps(str(fail)))
            .replace("__NAME__", json.dumps(name))
        )
        exe = bin_dir / name
        exe.write_text(script, encoding="utf-8")
        exe.chmod(0o755)
    return bin_dir, log, fail


def pm_calls(log: Path) -> List[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def deps_ensure(bin_dir: Path, wt: Path, main: Path, *extra: str, timeout: int = 60) -> Run:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    cmd = [sys.executable, "-m", "scripts.workflow.cli", "deps", "ensure",
           "--worktree", str(wt), "--main-worktree", str(main), *extra]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Run(None, "", "", True)
    return Run(proc.returncode, proc.stdout, proc.stderr, False)


def stamp_path(wt: Path) -> Path:
    out = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-dir"],
                         capture_output=True, text=True, check=True).stdout.strip()
    gitdir = Path(out)
    if not gitdir.is_absolute():
        gitdir = wt / gitdir
    return gitdir / "claude-helpers" / "deps-stamp.json"


@dataclass
class Case:
    root: Path
    main: Path
    wt: Path
    bin: Path
    log: Path
    fail: Path

    def run(self, *extra: str) -> Run:
        return deps_ensure(self.bin, self.wt, self.main, *extra)

    def calls(self) -> List[dict]:
        return pm_calls(self.log)


@contextmanager
def case(tracked: Dict[str, str], main_files: Optional[Dict[str, str]] = None) -> Iterator[Case]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, wt = make_layout(root, tracked)
        for rel, text in (main_files or {}).items():
            write(main / rel, text)
        bin_dir, log, fail = make_bins(root)
        yield Case(root, main, wt, bin_dir, log, fail)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


# ---------------------------------------------------------------- sections

def section_no_lockfile_stamp(t):
    tracked = {"README.md": "x\n", "package.json": package_json({"a": "1.0.0"})}
    with case(tracked, main_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall only'\n"}) as c:
        first = c.run()
        control = c.run()
        calls_after_control = len(c.calls())
        write(c.wt / "package.json", package_json({"a": "1.0.0", "left-pad": "1.3.0"}))
        changed = c.run()
        t("no-lockfile precondition: first run installs the commands.install command",
          first.first_line == "DEPS: installed", f"line={first.first_line!r}")
        t("no-lockfile control: unchanged package.json on the next run is DEPS: current",
          control.first_line == "DEPS: current" and calls_after_control == 1,
          f"line={control.first_line!r} calls={calls_after_control}")
        t("no-lockfile: a package.json edit after a successful install reinstalls (not DEPS: current)",
          changed.first_line == "DEPS: installed" and len(c.calls()) == 2,
          f"line={changed.first_line!r} calls={len(c.calls())}")


def section_install_config(t):
    for name, old, new in INSTALL_CONFIG_FILES:
        tracked = {"package-lock.json": "v1\n", "package.json": package_json({"a": "1.0.0"}), name: old}
        with case(tracked) as c:
            first = c.run()
            control = c.run()
            write(c.wt / name, new)
            commit_in(c.wt, f"edit {name}")
            changed = c.run()
            t(f"{name}: unchanged config on the next run is DEPS: current (precondition)",
              first.first_line == "DEPS: installed" and control.first_line == "DEPS: current",
              f"first={first.first_line!r} control={control.first_line!r}")
            t(f"{name}: a committed edit after a successful install reinstalls (DEPS: installed)",
              changed.first_line == "DEPS: installed" and len(c.calls()) == 2,
              f"line={changed.first_line!r} calls={len(c.calls())}")

        with case(tracked) as c:
            write(c.wt / name, new)
            commit_in(c.wt, f"edit {name}")
            gated = c.run("--base", "main")
            t(f"{name}: a branch edit against --base is DEPS: manifest-changed and nothing installs",
              gated.first_line.startswith("DEPS: manifest-changed") and c.calls() == [],
              f"line={gated.first_line!r} calls={c.calls()}")


def section_yarn_argv(t):
    for spec, argv in [("yarn@1.22.19", ["install", "--frozen-lockfile"]),
                       ("yarn@2.4.3", ["install", "--immutable"])]:
        tracked = {"yarn.lock": "y1\n", "package.json": package_json(package_manager=spec)}
        with case(tracked) as c:
            r = c.run()
            calls = c.calls()
            t(f"{spec} with yarn.lock runs `yarn {' '.join(argv)}` and prints DEPS: installed",
              r.first_line == "DEPS: installed" and len(calls) == 1
              and calls[0]["name"] == "yarn" and calls[0]["argv"] == argv,
              f"line={r.first_line!r} calls={calls}")


def section_bun_dedupe(t):
    with case({"bun.lockb": "b1\n", "bun.lock": "k1\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("bun.lockb with bun.lock is one bun manager: DEPS: installed, not ambiguous",
          r.first_line == "DEPS: installed", f"line={r.first_line!r}")
        t("bun.lockb with bun.lock runs the bun frozen argv exactly once",
          len(calls) == 1 and calls[0]["name"] == "bun" and calls[0]["argv"] == ["install", "--frozen-lockfile"],
          f"calls={calls}")


def section_base_dash(t):
    for base_arg in ["--base=-x", "--base=--end-of-options"]:
        with case({"package-lock.json": "v1\n", "package.json": package_json()}) as c:
            r = c.run(base_arg)
            t(f"{base_arg}: a base starting with '-' is refused: no installer runs",
              c.calls() == [] and not r.hung and r.rc not in (0, None),
              f"rc={r.rc} hung={r.hung} line={r.first_line!r} calls={c.calls()}")
            t(f"{base_arg}: the refusal does not print an install success line",
              not r.first_line.startswith(DEPS_FIRST_LINE_OK),
              f"line={r.first_line!r}")


def section_redaction(t):
    joined = "\n".join(deps.redact_output("\n".join(REDACT_INPUT)))
    leaked = [s for s in REDACT_SECRETS if s in joined]
    t("redact_output masks GitHub, fine-grained, npm token prefixes and GH_TOKEN / NPM_TOKEN values",
      not leaked, f"leaked={leaked} out={joined!r}")
    t("redact_output leaves a benign progress line intact",
      "installed 42 packages in 3s" in joined, f"out={joined!r}")

    with case({"package-lock.json": "v1\n", "package.json": package_json()}) as c:
        write(c.fail, "\n".join(REDACT_INPUT) + "\n")
        r = c.run()
        leaked_cli = [s for s in REDACT_SECRETS if s in r.combined]
        t("a failing install prints DEPS: failed and no token-shaped secret reaches its output",
          r.first_line.startswith("DEPS: failed") and not leaked_cli,
          f"line={r.first_line!r} leaked={leaked_cli}")


def section_process_group(t):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, feat = make_layout(root, {"package-lock.json": "v1\n", "package.json": package_json()})
        sleeper_dir = root / "sleepbin"
        sleeper_dir.mkdir()
        pid_file = root / "grandchild.pid"
        npm = sleeper_dir / "npm"
        # The install spawns a background grandchild and waits on it, so a kill that reaches only the
        # direct child leaves the grandchild running.
        npm.write_text("#!/bin/sh\nsleep 30 &\necho $! > \"$PIDFILE\"\nwait\nexit 0\n", encoding="utf-8")
        npm.chmod(0o755)
        saved = {key: os.environ.get(key) for key in ("PATH", "PIDFILE")}
        os.environ["PATH"] = str(sleeper_dir) + os.pathsep + (saved["PATH"] or "")
        os.environ["PIDFILE"] = str(pid_file)
        try:
            result = deps.ensure_deps(feat, main, timeout=1)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        grandchild = int(pid_file.read_text().strip()) if pid_file.exists() else None
        t("process-group precondition: the fake install started a background grandchild",
          grandchild is not None, f"pidfile_exists={pid_file.exists()}")
        t("a timed-out install is reported as failed",
          result.status == "failed", f"status={result.status} detail={result.detail}")
        if grandchild is not None:
            gone = wait_gone(grandchild)
            if not gone:
                try:
                    os.kill(grandchild, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            t("the timeout kills the whole install process group: the grandchild is gone",
              gone, f"pid={grandchild} still_alive={not gone}")


def section_symlinked_stamp(t):
    with case({"package-lock.json": "v1\n", "package.json": package_json()}) as c:
        first = c.run()
        sp = stamp_path(c.wt)
        t("symlink precondition: the first install wrote a stamp", first.first_line == "DEPS: installed"
          and sp.exists(), f"line={first.first_line!r} stamp={sp}")
        target = sp.parent / "deps-stamp-target.json"
        shutil.move(str(sp), str(target))
        os.symlink(str(target), str(sp))
        before = target.read_bytes()
        write(c.wt / "package-lock.json", "v2\n")
        second = c.run()
        t("symlinked stamp: a rewrite after a lockfile edit reinstalls (DEPS: installed)",
          second.first_line == "DEPS: installed", f"line={second.first_line!r}")
        t("symlinked stamp: the stamp path is still a symlink after the rewrite",
          os.path.islink(sp), f"stamp={sp} islink={os.path.islink(sp)}")
        t("symlinked stamp: the symlink target received the new stamp content",
          target.exists() and target.read_bytes() != before,
          f"target={target} changed={target.exists() and target.read_bytes() != before}")


if __name__ == "__main__":
    h = Harness("DEPS HARDENING TEST SUITE")
    t = h.test_result
    for section in (
        section_no_lockfile_stamp,
        section_install_config,
        section_yarn_argv,
        section_bun_dedupe,
        section_base_dash,
        section_redaction,
        section_process_group,
        section_symlinked_stamp,
    ):
        print(f"[{section.__name__}]")
        section(t)
        print()
    h.summarize_and_exit()
