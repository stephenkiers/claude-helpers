#!/usr/bin/env python3
"""
Tests for `primary-checkout-drift.py primary` (#441): the two-rule primary-checkout resolver.

- Pure cases (a)-(h): parse_worktree_porcelain / select_primary over synthetic porcelain.
- Real-git blackboxes: a bare-clone layout (<repo>/.bare + .git file + worktrees/*), the
  default-branch candidate from origin/HEAD, a classic repo from the main checkout and from a
  linked worktree, and a classic repo reached through a symlinked parent.
- Scripted run of the doc's delimited re-check block (`# primary-recheck: begin` / `end`) in bash
  against those fixtures, with the fail call replaced by a stub that prints its arguments.
- Forced-exception test: primary must exit 2 with ERROR resolver-failed on stderr and nothing on stdout.

Run with: python3 tests/test_primary_checkout_drift_primary.py
"""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"
DOC = REPO_ROOT / "commands" / "implement-with-haiku.md"

# ----------------------------------------------------------------------------
# Synthetic porcelain (pure cases)
# ----------------------------------------------------------------------------

# (a) Bare ticket-layout shape: bare entry, detached merge-queue scratch, three ticket worktrees, main last.
BARE_TICKET_LAYOUT = (
    "worktree /r/.bare\nbare\n\n"
    "worktree /r/worktrees/merge-scratch\nHEAD 1111111\ndetached\n\n"
    "worktree /r/worktrees/ticket-1\nHEAD 2222222\nbranch refs/heads/ticket-1\n\n"
    "worktree /r/worktrees/ticket-2\nHEAD 2222222\nbranch refs/heads/ticket-2\n\n"
    "worktree /r/worktrees/ticket-3\nHEAD 2222222\nbranch refs/heads/ticket-3\n\n"
    "worktree /r/worktrees/main\nHEAD 3333333\nbranch refs/heads/main\n\n"
)

# (b) tailscale-harness shape: a nested agent worktree under .bare comes before main.
TAILSCALE = (
    "worktree /r/.bare\nbare\n\n"
    "worktree /r/.bare/.claude/worktrees/agent-a1\nHEAD 4444444\nbranch refs/heads/worktree-agent-a1\n\n"
    "worktree /r/worktrees/main\nHEAD 3333333\nbranch refs/heads/main\n\n"
)

# (c) Bare, no default-branch worktree.
BARE_NO_MAIN = (
    "worktree /r/.bare\nbare\n\n"
    "worktree /r/worktrees/feat\nHEAD 1111111\nbranch refs/heads/feat\n\n"
)

# (d) Bare, default-branch worktree is prunable.
BARE_MAIN_PRUNABLE = (
    "worktree /r/.bare\nbare\n\n"
    "worktree /r/worktrees/main\nHEAD 3333333\nbranch refs/heads/main\n"
    "prunable gitdir file points to non-existent location\n\n"
)

# (f) Classic repo: first entry on a feature branch, a linked worktree holds main.
CLASSIC_FEATURE_FIRST = (
    "worktree /r/feat-checkout\nHEAD 1111111\nbranch refs/heads/feature\n\n"
    "worktree /r/main-wt\nHEAD 3333333\nbranch refs/heads/main\n\n"
)

# (g) Classic repo whose first entry is prunable.
CLASSIC_PRUNABLE_FIRST = (
    "worktree /r/main\nHEAD 1111111\nbranch refs/heads/main\n"
    "prunable gitdir file points to non-existent location\n\n"
    "worktree /r/wt\nHEAD 2222222\nbranch refs/heads/feature\n\n"
)

# (h) Space in the first entry's path.
SPACE_IN_PATH = "worktree /tmp/a b/repo\nHEAD 1111111\nbranch refs/heads/main\n\n"


def load_drift_module():
    """Load scripts/primary-checkout-drift.py as a module (same idiom as the doc-consistency suite)."""
    spec = importlib.util.spec_from_file_location("primary_checkout_drift", DRIFT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["primary_checkout_drift"] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# Real-git helpers
# ----------------------------------------------------------------------------

def git_env() -> dict:
    """Environment for git fixtures: no inherited GIT_* repo pointers, fixed identity, no signing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return env


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), env=git_env(), capture_output=True, text=True, check=True)
    return r.stdout


def init_repo(path: Path) -> Path:
    """A non-bare repo on main with one commit."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    (path / "README.md").write_text("fixture\n")
    git(path, "add", "README.md")
    git(path, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return path


def make_bare_layout(root: Path) -> Path:
    """<root>/repo: .bare (bare clone), a .git file pointing at it, worktrees/main,
    worktrees/merge-scratch (detached) and worktrees/ticket-441. Returns the repo root."""
    seed = init_repo(root / "seed")
    repo = root / "repo"
    repo.mkdir()
    git(root, "clone", "-q", "--bare", str(seed), str(repo / ".bare"))
    (repo / ".git").write_text("gitdir: ./.bare\n")
    (repo / "worktrees").mkdir()
    git(repo, "worktree", "add", "-q", "worktrees/main", "main")
    git(repo, "worktree", "add", "-q", "--detach", "worktrees/merge-scratch", "main")
    git(repo, "worktree", "add", "-q", "-b", "ticket-441", "worktrees/ticket-441", "main")
    return repo


def make_classic(root: Path) -> Tuple[Path, Path, Path]:
    """A classic repo at <root>/repo (on main), a linked worktree <root>/repo-feature (on feature),
    and a plain subdirectory <root>/repo/sub. Returns (repo, linked, sub)."""
    repo = init_repo(root / "repo")
    linked = root / "repo-feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(linked))
    sub = repo / "sub"
    sub.mkdir()
    return repo, linked, sub


def run_primary(cwd: Path, *extra: str, env: Optional[dict] = None) -> Tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "primary", *extra],
        cwd=str(cwd), env=env if env is not None else git_env(), capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def oracle_first_entry(cwd: Path) -> str:
    """The pre-#441 site-1 pipeline, run as a black-box oracle for classic repos (paths without spaces)."""
    r = subprocess.run(
        "git worktree list --porcelain | grep '^worktree ' | head -1 | cut -d' ' -f2",
        shell=True, cwd=str(cwd), env=git_env(), capture_output=True, text=True,
    )
    return r.stdout


def real(path) -> str:
    return os.path.realpath(str(path))


# ----------------------------------------------------------------------------
# Scripted re-check block
# ----------------------------------------------------------------------------

RECHECK_RE = re.compile(
    r"^[ \t]*# primary-recheck: begin[^\n]*\n(.*?)^[ \t]*# primary-recheck: end",
    re.S | re.M,
)


def extract_recheck_block(doc_text: str) -> Optional[str]:
    m = RECHECK_RE.search(doc_text)
    return m.group(1) if m else None


def run_recheck(block: str, literal: str, cwd: Path, stub: Path, work: Path) -> Tuple[int, str, str]:
    """Run the re-check block under bash with <PRIMARY_CHECKOUT> set to literal and the drift
    script replaced by a stub that prints its arguments (STUB-FAIL ...) and exits 1."""
    body = block.replace("<PRIMARY_CHECKOUT>", literal)
    body = re.sub(r'"[^"\n]*primary-checkout-drift\.py"', lambda m: f'"{stub}"', body)
    body = re.sub(r"<[A-Za-z][A-Za-z0-9_-]*>", "PLACEHOLDER", body)
    body += 'printf "RECHECK_FAIL=%s\\n" "${RECHECK_FAIL:-}"\n'
    script = work / f"recheck-{len(list(work.glob('recheck-*.sh')))}.sh"
    script.write_text(body)
    r = subprocess.run(["bash", str(script)], cwd=str(cwd), env=git_env(), capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def recheck_outcome(rc: int, out: str) -> Tuple[bool, Optional[str]]:
    """(stub_called, RECHECK_FAIL detail or None when the marker line was not reached)."""
    stub_called = "STUB-FAIL" in out
    lines = [ln for ln in out.splitlines() if ln.startswith("RECHECK_FAIL=")]
    detail = lines[-1][len("RECHECK_FAIL="):] if lines else None
    return stub_called, detail


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

REAL_GIT = shutil.which("git") or "git"


def make_fake_git(root: Path, name: str) -> Path:
    """A `git` shim dir. `worktree list` prints $FAKE_PORCELAIN_FILE (or sleeps $FAKE_SLEEP seconds
    when set); every other git call runs the real git."""
    d = root / name
    d.mkdir(parents=True)
    shim = d / "git"
    shim.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"worktree list\"*)\n"
        "    if [ -n \"${FAKE_SLEEP:-}\" ]; then exec sleep \"$FAKE_SLEEP\"; fi\n"
        "    if [ -n \"${FAKE_PORCELAIN_FILE:-}\" ]; then cat \"$FAKE_PORCELAIN_FILE\"; exit 0; fi\n"
        "    ;;\n"
        "esac\n"
        f"exec '{REAL_GIT}' \"$@\"\n"
    )
    shim.chmod(0o755)
    return d


def fake_env(shim_dir: Path, porcelain_file: Optional[Path] = None, sleep: Optional[int] = None) -> dict:
    env = git_env()
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    if porcelain_file is not None:
        env["FAKE_PORCELAIN_FILE"] = str(porcelain_file)
    if sleep is not None:
        env["FAKE_SLEEP"] = str(sleep)
    return env


def run_recheck_env(block: str, literal: str, cwd: Path, stub: Path, work: Path, env: dict,
                    scratch: Optional[Path] = None) -> Tuple[int, str, str]:
    """As run_recheck, with an explicit environment and an optional <SCRATCH_PATH> substitution
    (SCRATCH_PATH is also exported). The drift script is replaced by the stub."""
    body = block.replace("<PRIMARY_CHECKOUT>", literal)
    if scratch is not None:
        body = body.replace("<SCRATCH_PATH>", str(scratch))
    body = re.sub(r'"[^"\n]*primary-checkout-drift\.py"', lambda m: f'"{stub}"', body)
    body = re.sub(r"<[A-Za-z][A-Za-z0-9_-]*>", "PLACEHOLDER", body)
    body += 'printf "RECHECK_FAIL=%s\\n" "${RECHECK_FAIL:-}"\n'
    script = work / f"recheck-env-{len(list(work.glob('recheck-env-*.sh')))}.sh"
    script.write_text(body)
    r = subprocess.run(["bash", str(script)], cwd=str(cwd), env=env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def main() -> None:
    h = Harness("primary-checkout-drift primary (#441)")
    drift = load_drift_module()
    parse = drift.parse_worktree_porcelain
    select = drift.select_primary

    # ---- pure cases ------------------------------------------------------------
    print("[Pure a] Bare ticket-layout shape: bare entry, detached scratch, ticket worktrees, main last")
    recs = parse(BARE_TICKET_LAYOUT)
    h.test_result("(a) parser yields six records", len(recs) == 6, str(len(recs)))
    h.test_result("(a) parser: first record is bare with no branch",
                  bool(recs[0].bare) and recs[0].branch is None, repr(recs[0]))
    h.test_result("(a) parser: scratch record is detached", bool(recs[1].detached), repr(recs[1]))
    h.test_result("(a) parser: branch is the full ref", recs[2].branch == "refs/heads/ticket-1", repr(recs[2]))
    path, detail = select(recs, ["main", "master"])
    h.test_result("(a) selects the refs/heads/main worktree (last entry)",
                  path == "/r/worktrees/main", f"path={path!r} detail={detail!r}")

    print("[Pure b] tailscale-harness shape: nested agent worktree before main")
    recs = parse(TAILSCALE)
    path, detail = select(recs, ["main", "master"])
    h.test_result("(b) selects main, never the nested agent worktree",
                  path == "/r/worktrees/main", f"path={path!r} detail={detail!r}")

    print("[Pure c] bare, no default-branch worktree")
    path, detail = select(parse(BARE_NO_MAIN), ["main", "master"])
    h.test_result("(c) returns None", path is None, repr(path))
    h.test_result("(c) detail lists the candidates tried",
                  isinstance(detail, str) and "main" in detail and "master" in detail, repr(detail))

    print("[Pure d] bare, default-branch worktree is prunable")
    path, detail = select(parse(BARE_MAIN_PRUNABLE), ["main", "master"])
    h.test_result("(d) returns None", path is None, repr(path))
    h.test_result("(d) detail names the prunable flag",
                  isinstance(detail, str) and "prunable" in detail.lower(), repr(detail))

    print("[Pure e] candidate order: origin/HEAD target first, then main, then master")
    develop_bare = parse(
        "worktree /r/.bare\nbare\n\n"
        "worktree /r/worktrees/main\nHEAD 1\nbranch refs/heads/main\n\n"
        "worktree /r/worktrees/develop\nHEAD 2\nbranch refs/heads/develop\n\n"
    )
    path, _ = select(develop_bare, ["develop", "main", "master"])
    h.test_result("(e) candidates [develop, main, master] select develop", path == "/r/worktrees/develop", repr(path))
    path, _ = select(develop_bare, ["main", "master"])
    h.test_result("(e) no origin/HEAD: [main, master] select main", path == "/r/worktrees/main", repr(path))
    master_only = parse(
        "worktree /r/.bare\nbare\n\n"
        "worktree /r/worktrees/master\nHEAD 1\nbranch refs/heads/master\n\n"
    )
    path, _ = select(master_only, ["main", "master"])
    h.test_result("(e) no main worktree: falls back to master", path == "/r/worktrees/master", repr(path))

    print("[Pure f] classic repo, first entry on a feature branch, linked worktree holds main")
    path, _ = select(parse(CLASSIC_FEATURE_FIRST), ["main", "master"])
    h.test_result("(f) returns the first entry, not the main worktree (rule 2 not consulted)",
                  path == "/r/feat-checkout", repr(path))

    print("[Pure g] classic repo, first entry prunable")
    path, _ = select(parse(CLASSIC_PRUNABLE_FIRST), ["main", "master"])
    h.test_result("(g) still returns the first entry unchanged", path == "/r/main", repr(path))

    print("[Pure h] space in the first entry's path")
    recs = parse(SPACE_IN_PATH)
    h.test_result("(h) parser keeps the path whole", recs[0].path == "/tmp/a b/repo", repr(recs[0].path))
    path, _ = select(recs, ["main", "master"])
    h.test_result("(h) selector returns the whole path verbatim", path == "/tmp/a b/repo", repr(path))
    cut_out = subprocess.run(["cut", "-d", " ", "-f2"], input="worktree /tmp/a b/repo\n",
                             capture_output=True, text=True).stdout
    h.test_result("(h) documented divergence: cut -d' ' -f2 truncates to /tmp/a",
                  cut_out == "/tmp/a\n", repr(cut_out))

    with tempfile.TemporaryDirectory() as tmp_s:
        root = Path(tmp_s)

        # ---- real-git bare layout ---------------------------------------------
        print("[Blackbox bare] primary from the ticket worktree on the bare ticket layout")
        bare_repo = make_bare_layout(root / "bare")
        ticket = bare_repo / "worktrees" / "ticket-441"
        main_wt = bare_repo / "worktrees" / "main"
        rc, out, err = run_primary(ticket)
        h.test_result("bare: primary exits 0", rc == 0, f"rc={rc} err={err}")
        h.test_result("bare: stdout is exactly one path line",
                      out.endswith("\n") and out.count("\n") == 1, repr(out))
        printed = out.rstrip("\n")
        h.test_result("bare: printed path is worktrees/main (realpath)",
                      real(printed) == real(main_wt), f"printed={printed!r}")
        non_bare = git(Path(printed), "rev-parse", "--is-bare-repository").strip()
        h.test_result("bare: printed checkout is non-bare", non_bare == "false", repr(non_bare))

        rc_repo, out_repo, _ = run_primary(bare_repo)
        h.test_result("bare: same answer from the repo root", rc_repo == 0 and out_repo == out,
                      repr(out_repo))

        baseline = root / "baseline.json"
        s_rc = subprocess.run(
            [sys.executable, str(DRIFT_SCRIPT), "snapshot", "--checkout", printed,
             "--orchestrator-cwd", str(ticket), "--out", str(baseline)],
            capture_output=True, text=True, env=git_env(),
        ).returncode
        h.test_result("bare: snapshot --checkout <primary> exits 0 (ticket acceptance)", s_rc == 0, f"rc={s_rc}")

        top_raw = git(Path(printed), "rev-parse", "--show-toplevel").strip()
        h.test_result("bare: rev-parse --show-toplevel equals the printed path (raw)",
                      top_raw == printed, f"toplevel={top_raw!r} printed={printed!r}")
        h.test_result("bare: rev-parse --show-toplevel equals the printed path (realpath)",
                      real(top_raw) == real(printed), f"toplevel={top_raw!r}")

        # ---- origin/HEAD candidate ----------------------------------------------
        print("[Blackbox bare] origin/HEAD set to develop selects the develop worktree")
        dev_repo = make_bare_layout(root / "devbare")
        git(dev_repo, "branch", "develop", "main")
        git(dev_repo, "worktree", "add", "-q", "worktrees/develop", "develop")
        main_sha = git(dev_repo, "rev-parse", "main").strip()
        git(dev_repo, "update-ref", "refs/remotes/origin/develop", main_sha)
        git(dev_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        rc, out, err = run_primary(dev_repo / "worktrees" / "ticket-441")
        h.test_result("origin/HEAD=develop: primary exits 0", rc == 0, f"rc={rc} err={err}")
        h.test_result("origin/HEAD=develop: selects worktrees/develop",
                      real(out.rstrip("\n")) == real(dev_repo / "worktrees" / "develop"), repr(out))

        # ---- real-git bare layout, no default-branch worktree --------------------
        print("[Blackbox bare] main worktree removed: exit 3 with empty stdout")
        gone_repo = make_bare_layout(root / "gone")
        shutil.rmtree(gone_repo / "worktrees" / "main")
        rc, out, err = run_primary(gone_repo / "worktrees" / "ticket-441")
        h.test_result("no candidate: exit 3", rc == 3, f"rc={rc} err={err}")
        h.test_result("no candidate: stdout empty", out == "", repr(out))
        h.test_result("no candidate: stderr starts with NOTE: no usable primary checkout",
                      err.startswith("NOTE: no usable primary checkout"), repr(err))

        # ---- outside any repo, and argparse errors -----------------------------
        print("[Blackbox] outside any repo: exit 2 with ERROR resolver-failed")
        outside = root / "outside"
        outside.mkdir()
        env = git_env()
        env["GIT_CEILING_DIRECTORIES"] = str(root)
        rc, out, err = run_primary(outside, env=env)
        h.test_result("outside repo: exit 2", rc == 2, f"rc={rc} err={err}")
        h.test_result("outside repo: stdout empty", out == "", repr(out))
        h.test_result("outside repo: stderr starts with ERROR resolver-failed",
                      err.startswith("ERROR resolver-failed"), repr(err))

        print("[Blackbox] primary takes no path argument: usage error exits 2 with empty stdout")
        rc, out, err = run_primary(ticket, "/some/path")
        h.test_result("extra argument: exit 2", rc == 2, f"rc={rc}")
        h.test_result("extra argument: stdout empty", out == "", repr(out))

        # ---- classic repo ----------------------------------------------------------
        print("[Blackbox classic] primary == old head -1 pipeline, from main checkout and linked worktree")
        classic_repo, classic_linked, _ = make_classic(root / "classic")
        for label, cwd in (("main checkout", classic_repo), ("linked worktree", classic_linked)):
            rc, out, err = run_primary(cwd)
            oracle = oracle_first_entry(cwd)
            h.test_result(f"classic from {label}: exit 0", rc == 0, f"rc={rc} err={err}")
            h.test_result(f"classic from {label}: stdout byte-identical to head -1 pipeline",
                          out == oracle, f"primary={out!r} oracle={oracle!r}")
            top = git(Path(out.rstrip("\n")), "rev-parse", "--show-toplevel").strip()
            h.test_result(f"classic from {label}: rev-parse --show-toplevel matches (realpath)",
                          real(top) == real(out.rstrip("\n")), f"toplevel={top!r} primary={out!r}")

        # ---- classic repo through a symlinked parent -------------------------------
        print("[Blackbox classic] symlinked parent: realpath re-check, first entry unchanged")
        real_root = root / "real"
        real_root.mkdir()
        sym_repo, sym_linked, _ = make_classic(real_root)
        link = root / "link"
        os.symlink(str(real_root), str(link))
        for label, cwd in (("link/repo", link / "repo"), ("link/repo-feature", link / "repo-feature")):
            rc, out, err = run_primary(cwd)
            oracle = oracle_first_entry(cwd)
            printed_sym = out.rstrip("\n")
            h.test_result(f"symlink from {label}: exit 0", rc == 0, f"rc={rc} err={err}")
            h.test_result(f"symlink from {label}: prints the first porcelain entry unchanged",
                          out == oracle, f"primary={out!r} oracle={oracle!r}")
            top = git(Path(printed_sym), "rev-parse", "--show-toplevel").strip()
            h.test_result(f"symlink from {label}: realpath(show-toplevel) == realpath(primary)",
                          real(top) == real(printed_sym), f"toplevel={top!r} primary={printed_sym!r}")
            raw_note = "raw strings identical" if top == printed_sym else (
                f"raw strings differ (toplevel={top!r}, primary={printed_sym!r}); realpath comparison is needed")
            print(f"  note: symlink from {label}: {raw_note}")

        # ---- scripted re-check block -----------------------------------------------
        print("[Scripted re-check] doc block run in bash against the fixtures")
        doc_text = DOC.read_text()
        block = extract_recheck_block(doc_text)
        h.test_result("re-check block delimited by '# primary-recheck: begin' / 'end' in the doc",
                      block is not None, "markers not found")
        if block is not None:
            stub = root / "stub.py"
            stub.write_text(
                "import sys\n"
                "print('STUB-FAIL ' + ' '.join(sys.argv[1:]))\n"
                "sys.exit(1)\n"
            )
            work = root / "work"
            work.mkdir()
            (bare_repo / "notes").mkdir(exist_ok=True)
            (classic_repo / "sub").mkdir(exist_ok=True)
            cases = [
                ("bare: worktrees/main literal passes", bare_repo, bare_repo / "worktrees" / "main", True),
                ("bare: .bare literal fails with no-usable-primary", bare_repo, bare_repo / ".bare", False),
                ("bare: nonexistent path fails", bare_repo, bare_repo / "worktrees" / "does-not-exist", False),
                ("bare: non-worktree sibling dir fails", bare_repo, bare_repo / "notes", False),
                ("classic: main checkout literal passes", classic_repo, classic_repo, True),
                ("classic: linked worktree literal passes (run from linked)", classic_linked, classic_linked, True),
                ("classic: non-worktree sub dir fails", classic_repo, classic_repo / "sub", False),
                ("classic: symlinked main checkout passes", link / "repo", link / "repo", True),
                ("classic: symlinked linked worktree passes", link / "repo-feature", link / "repo-feature", True),
            ]
            for name, cwd, literal, should_pass in cases:
                rc, out, err = run_recheck(block, str(literal), cwd, stub, work)
                stub_called, detail = recheck_outcome(rc, out)
                if should_pass:
                    ok = rc == 0 and not stub_called and detail == ""
                else:
                    ok = stub_called and "--cause no-usable-primary" in out and detail is None
                h.test_result(name, ok,
                              f"rc={rc} stdout={out!r} stderr={err[-300:]!r}")

        # ---- forced exception ---------------------------------------------------------
        print("[Forced exception] parse raises: exit 2, empty stdout, ERROR resolver-failed on stderr")
        forced_repo, _, _ = make_classic(root / "forced")
        calls = []
        original_parse = drift.parse_worktree_porcelain

        def boom(text):
            calls.append(text)
            raise RuntimeError("forced parse failure")

        old_argv = sys.argv
        old_cwd = os.getcwd()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        rc = None
        drift.parse_worktree_porcelain = boom
        sys.argv = [str(DRIFT_SCRIPT), "primary"]
        os.chdir(str(forced_repo))
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                try:
                    rc = drift.main()
                except SystemExit as exc:
                    rc = exc.code
        finally:
            os.chdir(old_cwd)
            sys.argv = old_argv
            drift.parse_worktree_porcelain = original_parse
        h.test_result("forced exception: parser was reached", len(calls) == 1, f"calls={len(calls)}")
        h.test_result("forced exception: exit code 2", rc == 2, f"rc={rc!r}")
        h.test_result("forced exception: stdout empty", out_buf.getvalue() == "", repr(out_buf.getvalue()))
        h.test_result("forced exception: stderr starts with ERROR resolver-failed",
                      err_buf.getvalue().startswith("ERROR resolver-failed"), repr(err_buf.getvalue()[:200]))

    # ---- round 2 (spec r2): re-check failure branches, baseline, empty list, space path,
    #      resolver edge cases, fail --cause/--reason conflict ------------------------------
    with tempfile.TemporaryDirectory() as tmp_n:
        root_n = Path(tmp_n)
        r2_block = extract_recheck_block(DOC.read_text()) or ""
        r2_stub = root_n / "stub.py"
        r2_stub.write_text(
            "import sys\n"
            "print('STUB-FAIL ' + ' '.join(sys.argv[1:]))\n"
            "sys.exit(1)\n"
        )
        r2_work = root_n / "work"
        r2_work.mkdir()
        shim = make_fake_git(root_n, "shim")

        print("[R-F4] re-check: literal that rev-parses but is the .bare directory (symlink / trailing slash) fails")
        bare_n = make_bare_layout(root_n / "bare")
        alias = root_n / "alias-bare"
        os.symlink(str(bare_n / ".bare"), str(alias))
        for name, literal in (
            ("R-F4 symlink to .bare literal fails with no-usable-primary", str(alias)),
            ("R-F4 .bare literal with trailing slash fails with no-usable-primary", str(bare_n / ".bare") + "/"),
        ):
            rc, out, err = run_recheck_env(r2_block, literal, bare_n, r2_stub, r2_work, git_env())
            stub_called, detail = recheck_outcome(rc, out)
            h.test_result(name, stub_called and "--cause no-usable-primary" in out and detail is None,
                          f"rc={rc} stdout={out!r} stderr={err[-300:]!r}")

        classic_n = init_repo(root_n / "classic" / "repo")
        linked_n = root_n / "classic" / "repo-feature"
        git(classic_n, "worktree", "add", "-q", "-b", "feature", str(linked_n))

        print("[R-F7] re-check: empty `git worktree list --porcelain` fails, never passes")
        empty_list = root_n / "empty-porcelain.txt"
        empty_list.write_text("")
        rc, out, err = run_recheck_env(r2_block, str(classic_n), classic_n, r2_stub, r2_work,
                                       fake_env(shim, porcelain_file=empty_list))
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F7 empty worktree list: re-check fails and mentions an empty worktree list",
                      stub_called and bool(re.search(r"empty", out + (detail or ""), re.I)),
                      f"rc={rc} stdout={out!r} stderr={err[-300:]!r}")

        print("[R-F2] re-check: baseline checkouts keys decide the literal, absent/unreadable baseline is skipped")
        scratch = root_n / "scratch"
        scratch.mkdir()
        baseline_file = scratch / "primary-baseline.json"
        scratch_env = git_env()
        scratch_env["SCRATCH_PATH"] = str(scratch)

        baseline_file.write_text('{"checkouts": {"%s": {}}}' % real(classic_n))
        rc, out, err = run_recheck_env(r2_block, str(classic_n), classic_n, r2_stub, r2_work,
                                       scratch_env, scratch=scratch)
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F2 literal realpath in baseline checkouts: passes",
                      rc == 0 and not stub_called and detail == "", f"rc={rc} out={out!r} err={err[-300:]!r}")

        baseline_file.write_text('{"checkouts": {"/elsewhere/not-the-literal": {}}}')
        rc, out, err = run_recheck_env(r2_block, str(classic_n), classic_n, r2_stub, r2_work,
                                       scratch_env, scratch=scratch)
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F2 literal missing from baseline checkouts: fails with 'literal not in baseline'",
                      stub_called and "not in baseline" in out.lower(),
                      f"rc={rc} out={out!r} err={err[-300:]!r}")

        baseline_file.unlink()
        rc, out, err = run_recheck_env(r2_block, str(classic_n), classic_n, r2_stub, r2_work,
                                       scratch_env, scratch=scratch)
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F2 baseline absent: sub-check skipped, passing case still passes",
                      rc == 0 and not stub_called and detail == "", f"rc={rc} out={out!r} err={err[-300:]!r}")

        baseline_file.write_text('{"checkouts": {}}')
        os.chmod(baseline_file, 0)
        readable = os.access(baseline_file, os.R_OK)
        rc, out, err = run_recheck_env(r2_block, str(classic_n), classic_n, r2_stub, r2_work,
                                       scratch_env, scratch=scratch)
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F2 baseline unreadable: sub-check skipped (skipped vacuously if running as root)",
                      readable or (rc == 0 and not stub_called and detail == ""),
                      f"rc={rc} out={out!r} err={err[-300:]!r}")
        os.chmod(baseline_file, 0o644)
        baseline_file.unlink()

        print("[R-F21] re-check and primary with a literal path containing a space")
        sp_root = root_n / "sp ace"
        sp_repo, sp_linked, sp_sub = make_classic(sp_root)
        rc, out, err = run_recheck_env(r2_block, str(sp_repo), sp_repo, r2_stub, r2_work, git_env())
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F21 spaced main checkout literal passes the re-check",
                      rc == 0 and not stub_called and detail == "", f"rc={rc} out={out!r} err={err[-300:]!r}")
        rc, out, err = run_recheck_env(r2_block, str(sp_sub), sp_sub, r2_stub, r2_work, git_env())
        stub_called, detail = recheck_outcome(rc, out)
        h.test_result("R-F21 spaced non-worktree subdir literal fails with no-usable-primary",
                      stub_called and "--cause no-usable-primary" in out and detail is None,
                      f"rc={rc} out={out!r} err={err[-300:]!r}")
        rc, out, err = run_primary(sp_linked)
        h.test_result("R-F21 primary from a linked worktree under a spaced path prints the whole main path",
                      rc == 0 and real(out.rstrip("\n")) == real(sp_repo), f"rc={rc} out={out!r} err={err!r}")

        print("[R-F20] resolver: malformed porcelain, duplicate entries, git timeout, git missing from PATH")
        malformed = root_n / "malformed-porcelain.txt"
        malformed.write_text("worktree\nHEAD\ngarbage\n\nbranch refs/heads/x\n")
        rc, out, err = run_primary(classic_n, env=fake_env(shim, porcelain_file=malformed))
        h.test_result("R-F20 malformed porcelain: fails closed (exit 2 or 3, empty stdout)",
                      rc in (2, 3) and out == "", f"rc={rc} out={out!r} err={err!r}")

        dup = root_n / "dup-porcelain.txt"
        dup.write_text(
            f"worktree {real(classic_n)}\nHEAD 1\nbranch refs/heads/main\n\n"
            f"worktree {real(classic_n)}\nHEAD 1\nbranch refs/heads/main\n\n"
        )
        rc, out, err = run_primary(classic_n, env=fake_env(shim, porcelain_file=dup))
        h.test_result("R-F20 duplicate worktree entries: exit 0 with exactly one path line",
                      rc == 0 and out.count("\n") == 1 and real(out.rstrip("\n")) == real(classic_n),
                      f"rc={rc} out={out!r} err={err!r}")

        slow_out = root_n / "slow-out.txt"
        slow_err = root_n / "slow-err.txt"
        with open(slow_out, "w") as fo, open(slow_err, "w") as fe:
            try:
                slow = subprocess.run([sys.executable, str(DRIFT_SCRIPT), "primary"], cwd=str(classic_n),
                                      env=fake_env(shim, sleep=300), stdout=fo, stderr=fe, timeout=200)
                slow_rc: Optional[int] = slow.returncode
            except subprocess.TimeoutExpired:
                slow_rc = None
        h.test_result("R-F20 git timeout: exits 2 or 3 with empty stdout (no hang)",
                      slow_rc in (2, 3) and slow_out.read_text() == "",
                      f"rc={slow_rc} out={slow_out.read_text()!r} err={slow_err.read_text()!r}")

        nogit_bin = root_n / "no-git-bin"
        nogit_bin.mkdir()
        nogit_env = git_env()
        nogit_env["PATH"] = str(nogit_bin)
        rc, out, err = run_primary(classic_n, env=nogit_env)
        h.test_result("R-F20 git missing from PATH: exit 2, ERROR resolver-failed, empty stdout",
                      rc == 2 and out == "" and err.startswith("ERROR resolver-failed"),
                      f"rc={rc} out={out!r} err={err!r}")

        print("[R-F8] primary: newline in a porcelain path, and first non-bare entry with a missing directory")
        drift_n = load_drift_module()
        nl_text = "worktree /r/x\ny/repo\nHEAD 1\nbranch refs/heads/main\n\n"
        nl_path, _ = drift_n.select_primary(drift_n.parse_worktree_porcelain(nl_text), ["main", "master"])
        h.test_result("R-F8 newline in path: resolves exactly or fails closed, never truncated",
                      nl_path is None or nl_path == "/r/x\ny/repo", repr(nl_path))

        missing_first = root_n / "missing-first-porcelain.txt"
        missing_first.write_text(
            f"worktree {root_n}/no-such-feature-dir\nHEAD 1\nbranch refs/heads/feature\n\n"
            f"worktree {real(classic_n)}\nHEAD 2\nbranch refs/heads/main\n\n"
        )
        rc, out, err = run_primary(classic_n, env=fake_env(shim, porcelain_file=missing_first))
        h.test_result("R-F8 first non-bare entry with a missing directory falls through to the main worktree",
                      rc == 0 and real(out.rstrip("\n")) == real(classic_n), f"rc={rc} out={out!r} err={err!r}")

        print("[R-F6] fail: --cause together with --reason is rejected")
        r6_receipt = root_n / "r6-receipt.md"
        r6 = subprocess.run(
            [sys.executable, str(DRIFT_SCRIPT), "fail", "--compare-exit", "2", "--cause", "no-usable-primary",
             "--reason", "primary-checkout-drift: no-usable-primary", "--receipt", str(r6_receipt)],
            capture_output=True, text=True, env=git_env(),
        )
        h.test_result("R-F6 --cause with --reason: nonzero exit", r6.returncode != 0, f"rc={r6.returncode}")
        h.test_result("R-F6 --cause with --reason: stderr names both flags",
                      "--cause" in r6.stderr and "--reason" in r6.stderr, repr(r6.stderr[-300:]))
        h.test_result("R-F6 --cause with --reason: nothing written",
                      not r6_receipt.exists() and r6.stdout == "", f"stdout={r6.stdout!r}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
