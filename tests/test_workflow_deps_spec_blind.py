#!/usr/bin/env python3
"""
Spec-blind tests for `python3 -m scripts.workflow.cli deps ensure` (#417), written from the plan alone
(Step 1 and the Testing Strategy's deps and manifest-gate sections). The deps module, the checks module,
the CLI module and the flow/fleet docs were not read when these were written.

Every case runs the real CLI as a subprocess against a temp layout:
  <tmp>/repo/worktrees/main   main worktree (a git repo; holds the trusted .claude/project.yaml)
  <tmp>/repo/worktrees/feat   linked worktree the install runs in (git worktree add)
A fake package manager (npm, pnpm, yarn, bun, fakeinstall) is prepended to PATH. Each call appends a
JSON record to a log instead of touching the network. The fake reads stdin, so a CLI that does not
redirect stdin would hang; the runner treats a hang as a failure instead of blocking.

Covered (plan item -> section):
1. Step 1 table: each lockfile maps to its exact frozen argv; cwd is the realpath of the worktree;
   COREPACK_ENABLE_DOWNLOAD_PROMPT=0 and CI=1 in the child env; installer stdin is at EOF
2. D3 ambiguity: two table lockfiles, and a packageManager that names another manager, give
   `ambiguous` with nothing executed; a matching packageManager is not ambiguous
3. skipped and commands.install: no lockfile and no install command gives `skipped`; commands.install
   is read only from the main worktree; an explicit commands.install wins over the lockfile; the value
   is split with shlex and never passed to a shell
4. stamp and current: the second run is `current`; deleting node_modules forces a reinstall; a yarn
   Plug'n'Play install (.pnp.cjs, no node_modules) is `current`; an edited lockfile reinstalls
5. D2 failure: a failing installer gives `failed`, leaves no stamp, and deletes any previous stamp;
   the next run installs again once the failure is gone
6. redaction and tail bound: tokens, bearer headers, URL credentials and _password are masked; at most
   the last 40 lines of installer output are kept
7. a worktree outside the worktree parent is rejected and nothing runs
8. manifest gate (Q3): a committed or uncommitted manifest change gives `manifest-changed` and names
   the packages; a base that already gained a dependency installs; no --base skips the gate; approval
   via --approve-manifest clears it while the hash is unchanged and a later edit re-arms it
9. CLI contract: the DEPS line format; current and installed exit 0; the four non-success categories
   exit with pairwise distinct codes; missing required arguments are a usage error

Skipped (not observable black-box, or not pinned by the plan): the timeout path (the CLI exposes no
knob to shorten the 900s default, so `failed: timeout` cannot be driven without reading the code),
the Corepack download policy (an open item in the plan), and the atomic stamp write (not observable).

Run with: python3 tests/test_workflow_deps_spec_blind.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

# Fixtures (git, make_layout, make_fake_bin) are duplicated on purpose from test_workflow_deps.py and
# test_workflow_deps_cli.py: each suite runs standalone, and this one is written from the plan alone.
FAKE_NAMES = ["npm", "pnpm", "yarn", "bun", "fakeinstall"]

# lockfile -> (manager name, frozen argv after the manager name)
TABLE = [
    ("package-lock.json", "npm", ["ci"]),
    ("pnpm-lock.yaml", "pnpm", ["install", "--frozen-lockfile"]),
    ("yarn.lock", "yarn", ["install", "--immutable"]),
    ("bun.lockb", "bun", ["install", "--frozen-lockfile"]),
    ("bun.lock", "bun", ["install", "--frozen-lockfile"]),
]

DEPS_LINE = re.compile(
    r"^DEPS: (current|installed|skipped: .+|ambiguous: .+|manifest-changed: .+|failed: .+)$"
)

SECRETS = ["SECRET_TOKEN_ONE", "SECRET_BEARER_TWO", "SECRET_PASS_THREE", "SECRET_PASS_FOUR"]
REDACTION_LINES = [
    "npm ERR! fetch failed with //registry.npmjs.org/:_authToken=SECRET_TOKEN_ONE",
    "request sent with Authorization: Bearer SECRET_BEARER_TWO",
    "clone https://deploy:SECRET_PASS_THREE@git.example.test/repo.git failed",
    "//npm.example.test/:_password=SECRET_PASS_FOUR",
]

# A fake package manager. __PY__, __LOG__, __FAIL__ and __NAME__ are replaced at creation time.
# It records argv, cwd, stdin, and the env the CLI promised to set, then succeeds by creating the
# artifact a real install would (node_modules, or .pnp.cjs for yarn). If the FAIL file exists it prints
# that file to stdout and exits 1.
FAKE_TEMPLATE = '''#!__PY__
import json
import os
import sys

LOG = __LOG__
FAIL = __FAIL__
NAME = __NAME__
stdin_text = sys.stdin.read()
record = {
    "name": NAME,
    "argv": sys.argv[1:],
    "cwd": os.path.realpath(os.getcwd()),
    "stdin": stdin_text,
    "corepack": os.environ.get("COREPACK_ENABLE_DOWNLOAD_PROMPT"),
    "ci": os.environ.get("CI"),
}
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")
if os.path.exists(FAIL):
    with open(FAIL, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    sys.exit(1)
if NAME == "yarn":
    open(".pnp.cjs", "w").close()
else:
    os.makedirs("node_modules", exist_ok=True)
'''


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
        ["git", "-C", str(cwd), "-c", "user.name=Spec Test", "-c", "user.email=spec@example.test",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def package_json(deps=None, dev=None, package_manager: Optional[str] = None) -> str:
    data: Dict[str, object] = {"name": "demo", "version": "1.0.0"}
    if deps is not None:
        data["dependencies"] = deps
    if dev is not None:
        data["devDependencies"] = dev
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


def make_fake_bin(root: Path):
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
    out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdin=subprocess.PIPE,
                            stdout=out_f, stderr=err_f)
    hung = False
    rc: Optional[int]
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = None
        hung = True
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
    out_f.seek(0)
    err_f.seek(0)
    result = Run(rc, out_f.read(), err_f.read(), hung)
    out_f.close()
    err_f.close()
    return result


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
def case(tracked: Dict[str, str],
         worktree_files: Optional[Dict[str, str]] = None,
         main_files: Optional[Dict[str, str]] = None) -> Iterator[Case]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main, wt = make_layout(root, tracked)
        for rel, text in (main_files or {}).items():
            write(main / rel, text)
        for rel, text in (worktree_files or {}).items():
            write(wt / rel, text)
        bin_dir, log, fail = make_fake_bin(root)
        yield Case(root, main, wt, bin_dir, log, fail)


def commit_in(wt: Path, message: str) -> None:
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)


# ---------------------------------------------------------------- sections

def section_table(t):
    for lockfile, name, args in TABLE:
        tracked = {lockfile: "lock-v1\n"}
        if name == "yarn":
            # Yarn needs a readable version (packageManager) to pick its argv; 4.x is the Berry --immutable case.
            tracked["package.json"] = package_json(package_manager="yarn@4.1.0")
        with case(tracked) as c:
            r = c.run()
            calls = c.calls()
            t(f"{lockfile}: prints DEPS: installed and exits 0",
              not r.hung and r.rc == 0 and r.first_line == "DEPS: installed",
              f"rc={r.rc} hung={r.hung} out={r.combined!r}")
            expected = " ".join([name, *args])
            t(f"{lockfile}: runs exactly `{expected}`",
              len(calls) == 1 and calls[0]["name"] == name and calls[0]["argv"] == args,
              f"calls={calls}")
            if len(calls) == 1:
                call = calls[0]
                t(f"{lockfile}: installer cwd is the realpath of the worktree",
                  call["cwd"] == os.path.realpath(c.wt), f"cwd={call['cwd']!r}")
                t(f"{lockfile}: child env has COREPACK_ENABLE_DOWNLOAD_PROMPT=0 and CI=1",
                  call["corepack"] == "0" and call["ci"] == "1", f"env={call}")
                t(f"{lockfile}: installer stdin is at EOF immediately",
                  call["stdin"] == "", f"stdin={call['stdin']!r}")


def section_ambiguous(t):
    with case({"package-lock.json": "a\n", "yarn.lock": "b\n"}) as c:
        r = c.run()
        t("two lockfiles -> DEPS: ambiguous",
          r.first_line.startswith("DEPS: ambiguous"), f"line={r.first_line!r}")
        t("two lockfiles: ambiguous output names both files",
          "package-lock.json" in r.combined and "yarn.lock" in r.combined, f"out={r.combined!r}")
        t("two lockfiles: no installer is executed", c.calls() == [], f"calls={c.calls()}")
        t("two lockfiles: exit code is non-zero", r.rc not in (0, None), f"rc={r.rc}")

    mismatches = [
        ("yarn@4.1.0", "pnpm-lock.yaml"),
        ("pnpm@9.0.0", "package-lock.json"),
    ]
    for manager, lockfile in mismatches:
        tracked = {"package.json": package_json(package_manager=manager), lockfile: "x\n"}
        with case(tracked) as c:
            r = c.run()
            t(f"packageManager {manager} with {lockfile} -> DEPS: ambiguous",
              r.first_line.startswith("DEPS: ambiguous"), f"line={r.first_line!r}")
            t(f"packageManager {manager} with {lockfile}: nothing executed",
              c.calls() == [], f"calls={c.calls()}")

    with case({"package.json": package_json(package_manager="pnpm@9.0.0"),
               "pnpm-lock.yaml": "p\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("packageManager pnpm@9.0.0 matching pnpm-lock.yaml is not ambiguous",
          r.first_line == "DEPS: installed", f"line={r.first_line!r}")
        t("matching packageManager runs the pnpm frozen install",
          len(calls) == 1 and calls[0]["name"] == "pnpm"
          and calls[0]["argv"] == ["install", "--frozen-lockfile"], f"calls={calls}")


def section_skipped_and_install(t):
    with case({"README.md": "hi\n"}) as c:
        r = c.run()
        t("no lockfile and no commands.install -> DEPS: skipped with a no-lockfile reason",
          r.first_line.startswith("DEPS: skipped") and "no lockfile" in r.first_line,
          f"line={r.first_line!r}")
        t("skipped executes nothing", c.calls() == [], f"calls={c.calls()}")

    with case({"README.md": "hi\n"},
              worktree_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall wt-only'\n"}) as c:
        r = c.run()
        t("commands.install in the worktree project.yaml is ignored (skipped)",
          r.first_line.startswith("DEPS: skipped") and c.calls() == [],
          f"line={r.first_line!r} calls={c.calls()}")

    with case({"README.md": "hi\n"},
              main_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall run \"two words\"'\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("commands.install in main project.yaml is used and shlex-split into argv",
          r.first_line == "DEPS: installed" and len(calls) == 1
          and calls[0]["name"] == "fakeinstall" and calls[0]["argv"] == ["run", "two words"],
          f"line={r.first_line!r} calls={calls}")
        t("commands.install runs with cwd at the realpath of the worktree",
          len(calls) == 1 and calls[0]["cwd"] == os.path.realpath(c.wt), f"calls={calls}")

    with case({"README.md": "hi\n"},
              main_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall run \"x && y\" \"$HOME\"'\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("commands.install is never run through a shell (metacharacters stay literal)",
          len(calls) == 1 and calls[0]["argv"] == ["run", "x && y", "$HOME"],
          f"line={r.first_line!r} calls={calls}")

    with case({"package-lock.json": "x\n"},
              main_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall custom'\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("explicit main commands.install wins over the lockfile table",
          len(calls) == 1 and calls[0]["name"] == "fakeinstall" and calls[0]["argv"] == ["custom"]
          and not any(call["name"] == "npm" for call in calls),
          f"line={r.first_line!r} calls={calls}")

    with case({"package-lock.json": "x\n"},
              worktree_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall wt'\n"}) as c:
        r = c.run()
        calls = c.calls()
        t("worktree commands.install is ignored, so the lockfile table (npm ci) runs",
          len(calls) == 1 and calls[0]["name"] == "npm" and calls[0]["argv"] == ["ci"],
          f"line={r.first_line!r} calls={calls}")


def section_stamp(t):
    with case({"package-lock.json": "v1\n"}) as c:
        first = c.run()
        second = c.run()
        t("first run installs (npm)", first.first_line == "DEPS: installed", f"line={first.first_line!r}")
        t("second run with unchanged lockfile and node_modules is DEPS: current, exit 0",
          second.first_line == "DEPS: current" and second.rc == 0, f"rc={second.rc} line={second.first_line!r}")
        t("current run does not execute the installer again", len(c.calls()) == 1, f"calls={c.calls()}")
        sp = stamp_path(c.wt)
        t("success writes the stamp under the worktree's git dir (claude-helpers/deps-stamp.json)",
          sp.exists(), f"stamp={sp}")
        t("the stamp is outside the worktree tree",
          c.wt.resolve() not in sp.resolve().parents, f"stamp={sp}")

    with case({"package-lock.json": "v1\n"}) as c:
        c.run()
        shutil.rmtree(c.wt / "node_modules")
        r = c.run()
        t("stamp matches but node_modules is gone -> reinstall (DEPS: installed)",
          r.first_line == "DEPS: installed" and len(c.calls()) == 2,
          f"line={r.first_line!r} calls={len(c.calls())}")

    with case({"yarn.lock": "y1\n", "package.json": package_json(package_manager="yarn@4.1.0")}) as c:
        first = c.run()
        precondition = (c.wt / ".pnp.cjs").exists() and not (c.wt / "node_modules").exists()
        second = c.run()
        t("fixture: the yarn PnP install left .pnp.cjs and no node_modules",
          precondition, f"first={first.first_line!r}")
        t("yarn PnP install (.pnp.cjs, no node_modules) is DEPS: current on the second run",
          second.first_line == "DEPS: current" and len(c.calls()) == 1,
          f"line={second.first_line!r} calls={len(c.calls())}")

    with case({"package-lock.json": "v1\n"}) as c:
        c.run()
        write(c.wt / "package-lock.json", "v2\n")
        r = c.run()
        t("edited lockfile -> reinstall (DEPS: installed)",
          r.first_line == "DEPS: installed" and len(c.calls()) == 2,
          f"line={r.first_line!r} calls={len(c.calls())}")

    with case({"README.md": "x\n"},
              main_files={".claude/project.yaml": "commands:\n  install: 'fakeinstall only'\n"}) as c:
        c.run()
        r = c.run()
        t("commands.install-only: second run is DEPS: current (stamp matches the command)",
          r.first_line == "DEPS: current" and len(c.calls()) == 1,
          f"line={r.first_line!r} calls={len(c.calls())}")


def section_failure(t):
    with case({"package-lock.json": "v1\n"}) as c:
        c.run()
        t("precondition: a successful install left a stamp", stamp_path(c.wt).exists())
        write(c.wt / "package-lock.json", "v2\n")
        write(c.fail, "boom ERR_FAIL_MARKER\n")
        r = c.run()
        t("failing installer -> DEPS: failed with non-zero exit",
          r.first_line.startswith("DEPS: failed") and r.rc not in (0, None),
          f"rc={r.rc} line={r.first_line!r}")
        t("failure output is included after the DEPS line (redacted tail)",
          "ERR_FAIL_MARKER" in r.combined, f"out={r.combined!r}")
        t("failure deletes the previous stamp (never looks installed)",
          not stamp_path(c.wt).exists(), f"stamp={stamp_path(c.wt)}")
        c.fail.unlink()
        retry = c.run()
        t("after the failure is gone, the next run installs again",
          retry.first_line == "DEPS: installed", f"line={retry.first_line!r}")

    with case({"yarn.lock": "y1\n", "package.json": package_json(package_manager="yarn@4.1.0")}) as c:
        write(c.fail, "fresh failure\n")
        r = c.run()
        t("failed fresh install leaves no stamp",
          r.first_line.startswith("DEPS: failed") and not stamp_path(c.wt).exists(),
          f"line={r.first_line!r}")


def section_redaction(t):
    with case({"package-lock.json": "v1\n"}) as c:
        write(c.fail, "\n".join(REDACTION_LINES) + "\n")
        r = c.run()
        leaked = [s for s in SECRETS if s in r.combined]
        t("installer output with tokens, bearer headers, URL credentials and _password is masked",
          not leaked and r.first_line.startswith("DEPS: failed"),
          f"leaked={leaked} line={r.first_line!r}")

    with case({"package-lock.json": "v1\n"}) as c:
        lines = "\n".join(f"line-{i:03d}" for i in range(100)) + "\n"
        write(c.fail, lines)
        r = c.run()
        t("failure tail keeps the last lines of the output (line-099 present)",
          "line-099" in r.combined, f"out tail={r.combined[-200:]!r}")
        t("failure tail is bounded: the first lines of a 100-line output are dropped",
          re.search(r"\bline-000\b", r.combined) is None and re.search(r"\bline-059\b", r.combined) is None,
          "line-000 or line-059 still present, so the tail is longer than 40 lines")


def section_outside_parent(t):
    with case({"package-lock.json": "v1\n"}) as c:
        control = c.run()
        t("outside-parent control: a worktree under the normal parent installs",
          control.first_line == "DEPS: installed" and len(c.calls()) == 1,
          f"line={control.first_line!r} calls={len(c.calls())}")
        outside = c.root / "elsewhere" / "feat2"
        outside.parent.mkdir(parents=True)
        git(c.main, "worktree", "add", "-q", "-b", "feat2", str(outside))
        r = deps_ensure(c.bin, outside, c.main)
        t("worktree outside the worktree parent is rejected: no new installer run",
          len(c.calls()) == 1 and r.rc not in (0, None),
          f"rc={r.rc} line={r.first_line!r} calls={len(c.calls())}")


def control_cli(t, *argv):
    """Fail loudly if the CLI surface is missing, so no negative assertion can pass vacuously."""
    r = subprocess.run([sys.executable, "-m", "scripts.workflow.cli", *argv, "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    ok = r.returncode == 0
    t(f"control: `scripts.workflow.cli {' '.join(argv)} --help` is available", ok,
      f"rc={r.returncode} stderr={r.stderr[:200]!r}")
    if not ok:
        raise RuntimeError("control failed: CLI surface missing; the remaining cases would pass vacuously")


def section_manifest_gate(t):
    base = {
        "package.json": package_json(deps={"react": "18.0.0"}),
        "package-lock.json": "lock-base\n",
    }

    with case(base) as c:
        write(c.wt / "package.json",
              package_json(deps={"react": "18.0.0", "left-pad": "1.3.0"}, dev={"typescript": "5.0.0"}))
        write(c.wt / "package-lock.json", "lock-branch\n")
        commit_in(c.wt, "add deps")
        r = c.run("--base", "main")
        t("committed manifest change vs --base -> DEPS: manifest-changed naming the added packages",
          r.first_line.startswith("DEPS: manifest-changed")
          and "left-pad" in r.combined and "typescript" in r.combined,
          f"line={r.first_line!r} out={r.combined!r}")
        t("manifest-changed does not run the installer", c.calls() == [], f"calls={c.calls()}")
        t("manifest-changed exits non-zero", r.rc not in (0, None), f"rc={r.rc}")

    with case(base) as c:
        write(c.wt / "package.json", package_json(deps={"react": "18.0.0", "lodash": "4.17.21"},
                                                  dev={"jest": "29.0.0"}))
        r = c.run("--base", "main")
        t("uncommitted package.json edit is also caught -> manifest-changed naming lodash",
          r.first_line.startswith("DEPS: manifest-changed") and "lodash" in r.combined
          and c.calls() == [], f"line={r.first_line!r} calls={c.calls()}")

    updated = {
        "package.json": package_json(deps={"react": "18.0.0", "chalk": "5.0.0"}),
        "package-lock.json": "lock-chalk\n",
    }
    with case(updated) as c:
        write(c.wt / "src" / "index.js", "console.log(1)\n")
        commit_in(c.wt, "unrelated source change")
        r = c.run("--base", "main")
        t("base already gained chalk and the branch sits on it -> install runs (not manifest-changed)",
          r.first_line == "DEPS: installed" and len(c.calls()) == 1,
          f"line={r.first_line!r} calls={len(c.calls())}")

    with case(base) as c:
        write(c.wt / "package.json", package_json(deps={"react": "18.0.0", "left-pad": "1.3.0"}))
        write(c.wt / "package-lock.json", "lock-branch\n")
        commit_in(c.wt, "add left-pad")
        r = c.run()
        t("no --base: the manifest gate is skipped and the install runs",
          r.first_line == "DEPS: installed" and len(c.calls()) == 1,
          f"line={r.first_line!r} calls={len(c.calls())}")

    with case(base) as c:
        write(c.wt / "package.json", package_json(deps={"react": "18.0.0", "left-pad": "1.3.0"}))
        write(c.wt / "package-lock.json", "lock-branch\n")
        commit_in(c.wt, "add left-pad")
        r1 = c.run("--base", "main")
        t("approval sequence: step 1 is manifest-changed with no install",
          r1.first_line.startswith("DEPS: manifest-changed") and c.calls() == [],
          f"line={r1.first_line!r} calls={c.calls()}")
        r2 = c.run("--base", "main", "--approve-manifest")
        t("approval sequence: step 2 (--approve-manifest) clears the gate and installs once",
          r2.first_line == "DEPS: installed" and len(c.calls()) == 1,
          f"line={r2.first_line!r} calls={len(c.calls())}")
        r3 = c.run("--base", "main")
        t("approval sequence: step 3 (unchanged manifest, no flag) passes the gate as DEPS: current",
          r3.first_line == "DEPS: current" and len(c.calls()) == 1,
          f"line={r3.first_line!r} calls={len(c.calls())}")
        write(c.wt / "package.json",
              package_json(deps={"react": "18.0.0", "left-pad": "1.3.0", "extra": "9.9.9"}))
        r4 = c.run("--base", "main")
        t("approval sequence: a later manifest edit re-arms the gate (manifest-changed naming extra)",
          r4.first_line.startswith("DEPS: manifest-changed") and "extra" in r4.combined
          and len(c.calls()) == 1,
          f"line={r4.first_line!r} calls={len(c.calls())}")


def section_cli_contract(t):
    results: Dict[str, Run] = {}

    with case({"package-lock.json": "v1\n"}) as c:
        results["installed"] = c.run()
        results["current"] = c.run()

    with case({"README.md": "x\n"}) as c:
        results["skipped"] = c.run()

    with case({"package-lock.json": "a\n", "yarn.lock": "b\n"}) as c:
        results["ambiguous"] = c.run()

    with case(
        {"package.json": package_json(deps={"react": "18.0.0"}), "package-lock.json": "b\n"}
    ) as c:
        write(c.wt / "package.json", package_json(deps={"react": "18.0.0", "left-pad": "1.3.0"}))
        commit_in(c.wt, "add left-pad")
        results["manifest-changed"] = c.run("--base", "main")

    with case({"package-lock.json": "v1\n"}) as c:
        write(c.fail, "failing\n")
        results["failed"] = c.run()

    t("installed line is DEPS: installed", results["installed"].first_line == "DEPS: installed",
      f"line={results['installed'].first_line!r}")
    t("current line is DEPS: current", results["current"].first_line == "DEPS: current",
      f"line={results['current'].first_line!r}")
    t("installed and current both exit 0",
      results["installed"].rc == 0 and results["current"].rc == 0,
      f"installed rc={results['installed'].rc} current rc={results['current'].rc}")

    line_ok = all(
        DEPS_LINE.match(results[k].first_line) for k in ["skipped", "ambiguous", "manifest-changed", "failed"]
    )
    t("skipped, ambiguous, manifest-changed and failed each print one DEPS: line in the documented shape",
      line_ok, "; ".join(f"{k}={results[k].first_line!r}" for k in results))

    codes = {k: results[k].rc for k in ["skipped", "ambiguous", "manifest-changed", "failed"]}
    t("skipped, ambiguous, manifest-changed and failed exit with pairwise distinct codes",
      len(set(codes.values())) == 4 and None not in codes.values(), f"codes={codes}")
    t("ambiguous, manifest-changed and failed exit non-zero",
      all(codes[k] not in (0, None) for k in ["ambiguous", "manifest-changed", "failed"]),
      f"codes={codes}")

    usage = subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "deps", "ensure"],
        cwd=REPO_ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    t("deps ensure without --worktree/--main-worktree is a usage error (non-zero, usage text)",
      usage.returncode != 0 and "usage" in usage.stderr.lower(),
      f"rc={usage.returncode} stderr={usage.stderr[:200]!r}")


if __name__ == "__main__":
    h = Harness("DEPS ENSURE SPEC-BLIND TEST SUITE")
    test_result = h.test_result
    for section in (
        lambda t: control_cli(t, "deps", "ensure"),
        section_table,
        section_ambiguous,
        section_skipped_and_install,
        section_stamp,
        section_failure,
        section_redaction,
        section_outside_parent,
        section_manifest_gate,
        section_cli_contract,
    ):
        print(f"[{section.__name__}]")
        section(test_result)
        print()
    h.summarize_and_exit()
