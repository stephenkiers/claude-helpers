#!/usr/bin/env python3
"""
Spec-blind tests for `python3 -m scripts.workflow.cli checks ensure-check`, `checks record-check-answer`
and the resolver's provenance label (#417, plan Step 2 and the Testing Strategy's checks section),
written from the plan alone. The checks module, the CLI module and the flow/fleet docs were not read.

Every case runs the real CLI as a subprocess against a temp layout:
  <tmp>/repo/worktrees/main   main worktree (git repo; .claude/ files are untracked, as in real use)
  <tmp>/repo/worktrees/feat   linked worktree (git worktree add)
Only two Python imports are made in-process, both pure data: RepoCacheData (to build a schema-valid
pre-existing repo-cache.json) and read_repo_cache (to check the file still loads).

Covered (plan item -> section):
1. detection table: package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lockb and bun.lock map
   scripts.check to `npm|pnpm|yarn|bun run check`, written into the MAIN repo-cache.json, never the worktree
2. an ambiguous lockfile (two managers) detects nothing and writes nothing
3. the detector's write keeps every other key of a pre-existing file, sets the sidecar
   commandSources.check = "package-json", leaves a loadable file behind, and leaves no temp files
4. provenance: the resolver's source names package-json for a detector-written value, and not for a
   hand-written repo-cache value
5. ensure-check writes nothing when resolution already exists (project.yaml, a hand-written repo-cache
   value) or when checkOptOut applies
6. CLAUDE.md-only content (and nothing else) is unresolved, is shown as a suggestion, and is persisted
   nowhere; ensure-check exits 0 for every state
7. record-check-answer: no file creates a block; a file without commands appends one; a block-style
   commands with no check gets an indented check line, with comments and order preserved byte-for-byte
   outside that line; flow style, an existing check and unparseable YAML are refused with the file
   unchanged; multi-line and empty commands are refused; quoting round-trips through the resolver
8. --opt-out appends checkOptOut, round-trips through the resolver, and refuses a second one
9. PyYAML absent: the record reports failure (non-zero) instead of claiming success
10. --command and --opt-out are mutually exclusive

Skipped (not observable black-box, or the plan does not pin it): the byte-level atomicity of the write
(only checked as "no temp files left behind"), the exact wording of the CLAUDE.md suggestion line
beyond the backticked command, and the shipit-doc mapping (that doc was forbidden to read).

Run with: python3 tests/test_workflow_checks_spec_blind.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _test_harness import Harness, REPO_ROOT
from _inproc import run_python

from scripts.workflow.cache import read_repo_cache  # noqa: E402
from scripts.workflow.models import RepoCacheData  # noqa: E402

import yaml  # noqa: E402

MAP = [
    ("package-lock.json", "npm run check"),
    ("pnpm-lock.yaml", "pnpm run check"),
    ("yarn.lock", "yarn run check"),
    ("bun.lockb", "bun run check"),
    ("bun.lock", "bun run check"),
]


@dataclass
class Run:
    rc: int
    stdout: str
    stderr: str

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


def node_tracked(lockfile: Optional[str], scripts: Optional[dict] = None) -> Dict[str, str]:
    pkg: Dict[str, object] = {"name": "demo", "version": "1.0.0"}
    if scripts is not None:
        pkg["scripts"] = scripts
    tracked = {"package.json": json.dumps(pkg, indent=2) + "\n"}
    if lockfile:
        tracked[lockfile] = "lock\n"
    return tracked


@dataclass
class Case:
    root: Path
    main: Path
    wt: Path

    @property
    def cache(self) -> Path:
        return self.main / ".claude" / "repo-cache.json"

    @property
    def project(self) -> Path:
        return self.main / ".claude" / "project.yaml"

    def cli(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> Run:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        r = run_python(
            ["-m", "scripts.workflow.cli", "checks", *args],
            cwd=REPO_ROOT, env=env,
        )
        return Run(r.returncode, r.stdout, r.stderr)

    def cli_e2e(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> Run:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        r = subprocess.run(
            [sys.executable, "-m", "scripts.workflow.cli", "checks", *args],
            cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )
        return Run(r.returncode, r.stdout, r.stderr)

    def ensure(self) -> Run:
        return self.cli("ensure-check", "--worktree", str(self.wt), "--main-worktree", str(self.main))

    def resolve(self) -> dict:
        r = self.cli("resolve-check", "--worktree", str(self.wt), "--main-worktree", str(self.main))
        return json.loads(r.stdout)

    def record(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> Run:
        return self.cli("record-check-answer", "--main-worktree", str(self.main), *args,
                        extra_env=extra_env)

    def record_e2e(self, *args: str, extra_env: Optional[Dict[str, str]] = None) -> Run:
        return self.cli_e2e("record-check-answer", "--main-worktree", str(self.main), *args,
                            extra_env=extra_env)

    def read_cache_json(self) -> Optional[dict]:
        if not self.cache.exists():
            return None
        return json.loads(self.cache.read_text(encoding="utf-8"))


@contextmanager
def case(tracked: Dict[str, str],
         main_files: Optional[Dict[str, str]] = None,
         worktree_files: Optional[Dict[str, str]] = None) -> Iterator[Case]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "repo" / "worktrees" / "main"
        wt = root / "repo" / "worktrees" / "feat"
        for rel, text in tracked.items():
            write(main / rel, text)
        git(main, "init", "-q", "-b", "main")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "base")
        git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
        for rel, text in (main_files or {}).items():
            write(main / rel, text)
        for rel, text in (worktree_files or {}).items():
            write(wt / rel, text)
        yield Case(root, main, wt)


# ---------------------------------------------------------------- sections

def section_detection(t):
    for lockfile, expected in MAP:
        with case(node_tracked(lockfile, {"check": "node check.js"})) as c:
            r = c.ensure()
            cache = c.read_cache_json()
            written = (cache or {}).get("commands", {}).get("check")
            t(f"{lockfile}: ensure-check exits 0 and writes '{expected}' into main repo-cache.json",
              r.rc == 0 and written == expected, f"rc={r.rc} cache={cache} out={r.combined!r}")
            t(f"{lockfile}: ensure-check reports the command as detected",
              "detected" in r.stdout, f"out={r.stdout!r}")
            t(f"{lockfile}: nothing is written into the worktree's .claude/",
              not (c.wt / ".claude" / "repo-cache.json").exists(),
              "repo-cache.json appeared in the worktree")

    with case(node_tracked("package-lock.json", {"check": "node check.js"}),
              main_files={}) as c:
        write(c.wt / "pnpm-lock.yaml", "p\n")
        write(c.main / "pnpm-lock.yaml", "p\n")
        r = c.ensure()
        cache = c.read_cache_json()
        t("two lockfiles (npm and pnpm): ensure-check detects nothing and writes no check",
          cache is None or "check" not in cache.get("commands", {}),
          f"cache={cache} out={r.stdout!r}")
        t("two lockfiles: ensure-check still exits 0 and reports unresolved",
          r.rc == 0 and "unresolved" in r.stdout, f"rc={r.rc} out={r.stdout!r}")


def section_detector_write(t):
    existing = RepoCacheData(commands={"test": "pytest -q"}).to_dict()
    existing["extraKey"] = "keep-me"
    with case(node_tracked("package-lock.json", {"check": "node check.js"})) as c:
        write(c.cache, json.dumps(existing, indent=2) + "\n")
        r = c.ensure()
        cache = c.read_cache_json() or {}
        t("detector keeps the other commands and unknown top-level keys in repo-cache.json",
          r.rc == 0 and cache.get("commands", {}).get("test") == "pytest -q"
          and cache.get("commands", {}).get("check") == "npm run check"
          and cache.get("extraKey") == "keep-me",
          f"cache={cache}")
        t("detector writes the provenance sidecar commandSources.check = package-json",
          cache.get("commandSources", {}).get("check") == "package-json", f"cache={cache}")
        data, err = read_repo_cache(c.cache)
        t("the rewritten repo-cache.json still loads through read_repo_cache",
          err is None and data is not None and data.commands.get("check") == "npm run check",
          f"err={err!r} data={data!r}")
        leftovers = sorted(p.name for p in (c.main / ".claude").iterdir() if p.name != "repo-cache.json")
        t("no temp or sidecar files are left in .claude/ after the atomic write",
          leftovers == [], f"leftovers={leftovers}")

    with case(node_tracked("package-lock.json", {"check": "node check.js"})) as c:
        r = c.ensure()
        leftovers = sorted(p.name for p in (c.main / ".claude").iterdir())
        t("ensure-check on a repo with no .claude/ creates only a schema-valid repo-cache.json",
          r.rc == 0 and leftovers == ["repo-cache.json"]
          and read_repo_cache(c.cache)[1] is None,
          f"leftovers={leftovers} rc={r.rc}")


def section_provenance(t):
    with case(node_tracked("package-lock.json", {"check": "node check.js"})) as c:
        c.ensure()
        res = c.resolve()
        t("resolver source for a detector-written command carries the package-json provenance",
          res.get("command") == "npm run check" and "package-json" in str(res.get("source")),
          f"resolve={res}")

    with case(node_tracked(None, None)) as c:
        cache = RepoCacheData(commands={"check": "make check"}).to_dict()
        write(c.cache, json.dumps(cache, indent=2) + "\n")
        res = c.resolve()
        t("a hand-written repo-cache check (no detector sidecar) resolves without the package-json label",
          res.get("command") == "make check" and "package-json" not in str(res.get("source")),
          f"resolve={res}")


def section_no_write(t):
    with case(node_tracked("package-lock.json", {"check": "node check.js"}),
              main_files={".claude/project.yaml": 'commands:\n  check: "just check"\n'}) as c:
        before = c.project.read_bytes()
        r = c.ensure()
        t("project.yaml check already resolves: ensure-check reports resolved and writes no repo-cache",
          "resolved" in r.stdout and not c.cache.exists() and c.project.read_bytes() == before,
          f"out={r.stdout!r} cache_exists={c.cache.exists()}")

    with case(node_tracked("package-lock.json", {"check": "node check.js"})) as c:
        handwritten = RepoCacheData(commands={"check": "make check"}).to_dict()
        write(c.cache, json.dumps(handwritten, indent=2) + "\n")
        before = c.cache.read_bytes()
        r = c.ensure()
        t("an existing repo-cache check is kept: ensure-check changes no bytes and does not detect",
          c.cache.read_bytes() == before and "detected" not in r.stdout,
          f"out={r.stdout!r}")

    with case(node_tracked("package-lock.json", {"check": "node check.js"}),
              main_files={".claude/project.yaml": 'checkOptOut: "docs-only repo"\n'}) as c:
        r = c.ensure()
        t("checkOptOut applies: ensure-check reports opt-out and writes no repo-cache",
          "opt-out" in r.stdout and not c.cache.exists() and r.rc == 0,
          f"rc={r.rc} out={r.stdout!r} cache_exists={c.cache.exists()}")


def section_unresolved(t):
    claude = "# Demo\n\nBefore shipping, run `just check` from the repo root.\n"
    with case({"CLAUDE.md": claude, "README.md": "x\n"}) as c:
        r = c.ensure()
        t("CLAUDE.md-only content: ensure-check exits 0 and reports unresolved",
          r.rc == 0 and "unresolved" in r.stdout, f"rc={r.rc} out={r.stdout!r}")
        t("CLAUDE.md-only content writes no repo-cache and no project.yaml",
          not c.cache.exists() and not c.project.exists(),
          f"cache={c.cache.exists()} project={c.project.exists()}")
        t("the CLAUDE.md candidate is shown as a SUGGESTION line (display only)",
          any(line.startswith("SUGGESTION") and "just check" in line for line in r.stdout.splitlines()),
          f"out={r.stdout!r}")
        res = c.resolve()
        t("CLAUDE.md is never resolved as the check command", not res.get("command"), f"resolve={res}")

    with case({"README.md": "x\n"}) as c:
        r = c.ensure()
        t("no check anywhere: ensure-check exits 0, reports unresolved, writes nothing",
          r.rc == 0 and "unresolved" in r.stdout and not c.cache.exists(),
          f"rc={r.rc} out={r.stdout!r}")


def section_record(t):
    with case({"README.md": "x\n"}) as c:
        r = c.record("--command", "npm run check")
        parsed = yaml.safe_load(c.project.read_text(encoding="utf-8")) if c.project.exists() else None
        t("no project.yaml: record creates one whose commands.check is the answer",
          r.rc == 0 and isinstance(parsed, dict) and parsed.get("commands", {}).get("check") == "npm run check",
          f"rc={r.rc} parsed={parsed} out={r.combined!r}")
        res = c.resolve()
        t("the recorded answer resolves back through resolve-check",
          res.get("command") == "npm run check", f"resolve={res}")
        t("record writes the MAIN project.yaml only, not the worktree's",
          not (c.wt / ".claude" / "project.yaml").exists(), "project.yaml appeared in the worktree")

    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": "# My project\nname: demo\n"}) as c:
        old = c.project.read_text(encoding="utf-8")
        r = c.record("--command", "npm run check")
        new = c.project.read_text(encoding="utf-8")
        t("file without commands: the original bytes are a prefix of the new file (append only)",
          r.rc == 0 and new.startswith(old), f"rc={r.rc} new={new!r}")
        t("file without commands: the appended block parses and the comment survives",
          "# My project" in new and yaml.safe_load(new).get("commands", {}).get("check") == "npm run check",
          f"new={new!r}")

    old_text = (
        "# top comment\n"
        "name: demo  # inline\n"
        "commands:\n"
        "  # test runner comment\n"
        "  test: \"pytest -q\"\n"
        "# trailing comment\n"
    )
    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": old_text}) as c:
        r = c.record("--command", "npm run check")
        new = c.project.read_text(encoding="utf-8")
        old_lines = old_text.splitlines()
        new_lines = new.splitlines()
        inserted = [i for i, line in enumerate(new_lines) if line.strip().startswith("check:")]
        removed_ok = (len(inserted) == 1
                      and new_lines[:inserted[0]] + new_lines[inserted[0] + 1:] == old_lines)
        t("block commands with no check: only one check line is added, everything else is byte-identical",
          r.rc == 0 and removed_ok, f"rc={r.rc} new={new!r}")
        comments = [line for line in old_lines if line.lstrip().startswith("#")]
        new_comments = [line for line in new_lines if line.lstrip().startswith("#")]
        t("block commands with no check: every comment survives in order",
          comments == new_comments, f"old={comments} new={new_comments}")
        parsed = yaml.safe_load(new)
        t("block commands with no check: parsed YAML has the answer and keeps commands.test",
          parsed.get("commands", {}).get("check") == "npm run check"
          and parsed.get("commands", {}).get("test") == "pytest -q" and parsed.get("name") == "demo",
          f"parsed={parsed}")
        t("block commands with no check: the recorded answer resolves",
          c.resolve().get("command") == "npm run check", f"resolve={c.resolve()}")

    flow = "commands: {test: pytest}\n"
    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": flow}) as c:
        before = c.project.read_bytes()
        r = c.record("--command", "npm run check")
        t("flow-style commands are refused with a non-zero exit and the file unchanged",
          r.rc != 0 and c.project.read_bytes() == before, f"rc={r.rc} out={r.combined!r}")
        t("flow-style refusal prints the exact check line for the human to add",
          "check:" in r.combined and "npm run check" in r.combined, f"out={r.combined!r}")

    existing = 'commands:\n  check: "old-check"\n'
    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": existing}) as c:
        before = c.project.read_bytes()
        r = c.record("--command", "npm run check")
        t("an existing check value is refused and not overwritten",
          r.rc != 0 and c.project.read_bytes() == before, f"rc={r.rc} out={r.combined!r}")

    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": "commands: [unclosed\n"}) as c:
        before = c.project.read_bytes()
        r = c.record("--command", "npm run check")
        t("unparseable project.yaml is refused and left unchanged",
          r.rc != 0 and c.project.read_bytes() == before, f"rc={r.rc} out={r.combined!r}")

    with case({"README.md": "x\n"}) as c:
        r = c.record("--command", "npm run check\nrm -rf /tmp/unused")
        t("a multi-line command is rejected (non-zero, nothing written)",
          r.rc != 0 and not c.project.exists(), f"rc={r.rc} out={r.combined!r}")

    with case({"README.md": "x\n"}) as c:
        r = c.record("--command", "")
        t("an empty command is rejected (non-zero, nothing written)",
          r.rc != 0 and not c.project.exists(), f"rc={r.rc} out={r.combined!r}")

    quoted = "node -e \"console.log('a: b # c')\" && echo done"
    with case({"README.md": "x\n"}) as c:
        r = c.record("--command", quoted)
        res = c.resolve()
        t("a command with quotes, a colon and a hash round-trips exactly through the resolver",
          r.rc == 0 and res.get("command") == quoted, f"rc={r.rc} resolve={res}")

    with case({"README.md": "x\n"}) as c:
        r = c.record("--command", "npm run check", "--opt-out", "reason")
        t("--command and --opt-out together are a usage error",
          r.rc != 0 and not c.project.exists(), f"rc={r.rc} out={r.combined!r}")
        r2 = c.record()
        t("neither --command nor --opt-out is a usage error",
          r2.rc != 0 and not c.project.exists(), f"rc={r2.rc} out={r2.combined!r}")


def section_opt_out(t):
    with case({"README.md": "x\n"}) as c:
        r = c.record("--opt-out", "docs only, no code")
        parsed = yaml.safe_load(c.project.read_text(encoding="utf-8")) if c.project.exists() else {}
        t("--opt-out appends a top-level checkOptOut with the reason",
          r.rc == 0 and parsed.get("checkOptOut") == "docs only, no code", f"parsed={parsed} out={r.combined!r}")
        res = c.resolve()
        t("the opt-out round-trips: resolver source is opt-out with the same reason",
          res.get("source") == "opt-out" and res.get("opt_out_reason") == "docs only, no code",
          f"resolve={res}")
        before = c.project.read_bytes()
        r2 = c.record("--opt-out", "another reason")
        t("a second --opt-out is refused and the file is unchanged",
          r2.rc != 0 and c.project.read_bytes() == before, f"rc={r2.rc} out={r2.combined!r}")

    with case({"README.md": "x\n"}, main_files={".claude/project.yaml": 'checkOptOut: "hand written"\n'}) as c:
        before = c.project.read_bytes()
        r = c.record("--opt-out", "new reason")
        t("--opt-out refuses when a hand-written checkOptOut already exists",
          r.rc != 0 and c.project.read_bytes() == before, f"rc={r.rc} out={r.combined!r}")


def section_no_pyyaml(t):
    shim = Path(tempfile.mkdtemp())
    try:
        (shim / "yaml").mkdir()
        write(shim / "yaml" / "__init__.py", 'raise ImportError("simulated: PyYAML is not installed")\n')
        with case({"README.md": "x\n"}) as c:
            control = c.record("--command", "npm run check")
            t("PyYAML-absent control: the same record without the shim succeeds",
              control.rc == 0, f"rc={control.rc} out={control.combined!r}")
        with case({"README.md": "x\n"}) as c:
            r = c.record_e2e("--command", "npm run check", extra_env={"PYTHONPATH": str(shim)})
            t("PyYAML absent: the record reports failure (non-zero) instead of claiming success",
              r.rc != 0, f"rc={r.rc} out={r.combined!r}")
    finally:
        shutil.rmtree(shim, ignore_errors=True)


def control_cli(t, *argv):
    """Fail loudly if the CLI surface is missing, so no negative assertion can pass vacuously."""
    r = subprocess.run([sys.executable, "-m", "scripts.workflow.cli", *argv, "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    ok = r.returncode == 0
    t(f"control: `scripts.workflow.cli {' '.join(argv)} --help` is available", ok,
      f"rc={r.returncode} stderr={r.stderr[:200]!r}")
    if not ok:
        raise RuntimeError("control failed: CLI surface missing; the remaining cases would pass vacuously")


if __name__ == "__main__":
    h = Harness("CHECKS ENSURE / RECORD SPEC-BLIND TEST SUITE")
    test_result = h.test_result
    for section in (
        lambda t: control_cli(t, "checks", "ensure-check"),
        section_detection,
        section_detector_write,
        section_provenance,
        section_no_write,
        section_unresolved,
        section_record,
        section_opt_out,
        section_no_pyyaml,
    ):
        print(f"[{section.__name__}]")
        section(test_result)
        print()
    h.summarize_and_exit()
