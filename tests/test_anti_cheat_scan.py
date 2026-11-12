#!/usr/bin/env python3
"""
Black-box tests for scripts/anti-cheat-scan.py (#380).

Driven via the CLI entry point, in-process through tests/_inproc.py; one test via subprocess. Covers:
1. `implement` profile parity with /implement-with-haiku Gate step 2.
2. `heal` profile: test edits, gate-config edits, added suppressions, env guards, and the
   revert-to-pass rule (post-image must not restore pre-revert-base text).
3. `fix-now` profile: no stricter than an ordinary fix, but still the implement profile's rules.
4. Profile/--revert-base argument rules and fail-closed behaviour on bad refs.

Run with: python3 tests/test_anti_cheat_scan.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT
from _inproc import run_python, RepoTemplate

SCAN = REPO_ROOT / "scripts" / "anti-cheat-scan.py"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false",
          "-c", "core.hooksPath=/dev/null"]
NAMED_REASONS = {
    "test-edit", "gate-config-edit", "added-suppression", "env-guard", "revert-to-pass",
    "test-removed", "neutered-verification", "stub-body", "bad-ref", "missing-revert-base",
    "revert-base-not-allowed",
}

BASE_FILES = {
    "README.md": "# repo\n",
    "src/app.py": 'VALUE = "old"\n\n\ndef keep():\n    return 1\n',
    "src/other.py": "def g( ):\n    return 2\n",
    "tests/test_app.py": "def test_ok():\n    assert True\n",
    "ruff.toml": '[lint]\nselect = ["F"]\n',
    "justfile": "check:\n    ruff check .\n",
    "pyproject.toml": "[project]\nname = 'x'\n",
    "Makefile": "test:\n\tpytest\n",
    "scripts/flow-verify-receipt.py": "print('verify')\n",
}

# What the implement commit writes to src/app.py: changes VALUE, adds `import os` and new code.
IMPLEMENT_APP = (
    'VALUE = "new"\n'
    "import os\n"
    "\n\n"
    "def keep():\n"
    "    return 1\n"
    "\n\n"
    "def added():\n"
    "    unused = 5\n"
    "    return os.sep\n"
)

SKIP_PYPROJECT = "[project]\nname = 'x'\n[tool.pytest]\nmarkers = 'pytest.mark.skip'\n"


def run_git(repo, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), capture_output=True, text=True)
    return result.stdout.strip()


def commit(repo, files, message="c", remove=()):
    """Write files (path -> text), delete `remove`, commit, and return the new HEAD sha."""
    for rel, text in files.items():
        path = Path(repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for rel in remove:
        (Path(repo) / rel).unlink()
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def new_repo(root, name="repo"):
    repo = Path(root) / name
    repo.mkdir()
    run_git(repo, "init", "-q")
    return repo


def _build_base(root):
    run_git(root, "init", "-q")
    return commit(root, BASE_FILES, "base"), commit(root, {"src/app.py": IMPLEMENT_APP}, "implement")


_BASE = RepoTemplate(_build_base)


def make_repo(tmp):
    """C0 = pre-work, C1 = the implement commit. Returns (repo, c0, c1)."""
    repo = _BASE.copy(Path(tmp) / "repo")
    c0, c1 = _BASE.meta
    return repo, c0, c1


def scan_e2e(repo, base, head, profile, revert_base=None):
    cmd = [sys.executable, str(SCAN), "--repo", str(repo), "--base", base, "--head", head,
           "--profile", profile]
    if revert_base is not None:
        cmd += ["--revert-base", revert_base]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    return result.returncode, data


def scan(repo, base, head, profile, revert_base=None):
    cmd = [str(SCAN), "--repo", str(repo), "--base", base, "--head", head,
           "--profile", profile]
    if revert_base is not None:
        cmd += ["--revert-base", revert_base]
    result = run_python(cmd)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    return result.returncode, data


def passed(rc, data):
    return rc == 0 and data.get("verdict") == "pass"


def reason_names(data):
    """A reason is its name optionally followed by ': <detail>' (file/lines); return just the names."""
    return [str(r).split(":", 1)[0].strip() for r in data.get("reasons", [])]


def failed_with(rc, data, reason=None):
    ok = rc == 1 and data.get("verdict") == "fail" and bool(data.get("reasons"))
    if ok and reason is not None:
        ok = reason in reason_names(data)
    return ok


def detail(rc, data):
    return f"rc={rc} data={data}"


def heal_case(h, label, files, reason):
    """A heal commit on top of the implement commit that must fail with `reason`."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, files, "heal")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result(f"heal: {label} fails with '{reason}'", failed_with(rc, data, reason), detail(rc, data))


def main():
    h = Harness("ANTI-CHEAT SCAN TEST SUITE")

    print("[Section 1] implement profile")
    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        rc, data = scan_e2e(repo, c0, c1, "implement")
        h.test_result("implement: a clean implementation diff passes", passed(rc, data), detail(rc, data))
        h.test_result("implement: output carries a reasons list", isinstance(data.get("reasons"), list), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"tests/test_app.py": "def test_ok():\n    assert 1\n"}, "edit test")
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("implement: a changed tests/ file fails", failed_with(rc, data), detail(rc, data))
        h.test_result("implement: failure reasons are named reasons",
                      bool(data.get("reasons")) and set(reason_names(data)) <= NAMED_REASONS, detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {}, "remove test", remove=["tests/test_app.py"])
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("implement: a removed test file fails with 'test-removed'",
                      failed_with(rc, data, "test-removed"), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"pyproject.toml": SKIP_PYPROJECT}, "skip")
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("implement: pytest.mark.skip added in pyproject.toml fails", failed_with(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"Makefile": "test:\n\tpytest || true\n"}, "neuter")
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("implement: '|| true' added to Makefile fails with 'neutered-verification'",
                      failed_with(rc, data, "neutered-verification"), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/new.py": "def todo():\n    raise NotImplementedError\n"}, "stub")
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("implement: an added stub body fails with 'stub-body'",
                      failed_with(rc, data, "stub-body"), detail(rc, data))

    print()
    print("[Section 2] heal profile: forbidden edits")
    heal_case(h, "a one-line change under tests/", {"tests/test_app.py": "def test_ok():\n    assert 2 > 1\n"}, "test-edit")
    heal_case(h, "an edit to ruff.toml", {"ruff.toml": '[lint]\nselect = ["F", "E"]\nignore = ["F401"]\n'}, "gate-config-edit")
    heal_case(h, "an edit to the justfile", {"justfile": "check:\n    true\n"}, "gate-config-edit")
    heal_case(h, "an edit to pyproject.toml", {"pyproject.toml": "[project]\nname = 'y'\n"}, "gate-config-edit")
    heal_case(h, "an edit to scripts/flow-verify-receipt.py", {"scripts/flow-verify-receipt.py": "print('weaker')\n"},
              "gate-config-edit")
    heal_case(h, "an edit under .claude/", {".claude/settings.json": "{}\n"}, "gate-config-edit")
    heal_case(h, "an added '# noqa'", {"src/other.py": "def g( ):  # noqa\n    return 2\n"}, "added-suppression")
    heal_case(h, "an added '# type: ignore'", {"src/other.py": "def g( ):  # type: ignore\n    return 2\n"},
              "added-suppression")
    heal_case(h, "an added '# nosec'", {"src/other.py": "def g( ):  # nosec\n    return 2\n"},
              "added-suppression")
    heal_case(h, "an added '|| true'", {"Makefile": "test:\n\tpytest || true\n"}, "added-suppression")
    heal_case(h, "an added '--no-verify'", {"src/app.py": 'os.system("git commit --no-verify")\n'}, "added-suppression")
    heal_case(h, "an added 'skipif' conditional", {"src/other.py": 'def f():\n    if skipif():\n        return\n'},
              "added-suppression")
    heal_case(h, "an added env-guard early return",
              {"src/other.py": 'import os\n\n\ndef g( ):\n    if not os.environ.get("DB_NAME"):\n        return\n    return 2\n'},
              "env-guard")

    print()
    print("[Section 3] heal profile: revert-to-pass (narrowed)")
    with tempfile.TemporaryDirectory() as tmp:
        # Dogfood 1 shape: implement added `import os` plus code; the heal deletes only the import line.
        repo = new_repo(tmp)
        r0 = commit(repo, BASE_FILES, "base")
        r1 = commit(repo, {"src/app.py": BASE_FILES["src/app.py"] + "\nimport os\n\n\ndef added():\n    return 3\n"},
                    "implement adds unused import and code")
        r2 = commit(repo, {"src/app.py": BASE_FILES["src/app.py"] + "\n\n\ndef added():\n    return 3\n"},
                    "heal deletes only the import")
        rc, data = scan(repo, r1, r2, "heal", revert_base=r0)
        h.test_result("heal: deleting only an unused import added inside the range passes", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        healed = IMPLEMENT_APP.replace("import os\n", "").replace("    return os.sep\n", "    return 1\n")
        c2 = commit(repo, {"src/app.py": healed}, "heal removes the import and the code that used it")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: rewriting added code does not restore pre-revert-base text (passes rule (b))",
                      passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/app.py": IMPLEMENT_APP.replace("    unused = 5\n", "")}, "heal removes added unused var")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: removing a non-import line added in the range passes (rule (a) removed)",
                      passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/app.py": IMPLEMENT_APP.replace('VALUE = "new"', 'VALUE = "old"')}, "heal restores old text")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: restoring the pre-revert-base text of a changed region fails with 'revert-to-pass'",
                      failed_with(rc, data, "revert-to-pass"), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/other.py": "def g():\n    return 2\n"}, "heal formats a pre-existing line")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: a plain fix to a file implement never touched passes", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"README.md": "# repo v2\n"}, "heal edits non-code file")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: editing a non-code file passes", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/new.py": "def added():\n    return 5\n", "src/other.py": "def g():\n    return 2\n"}, "heal edits multiple files")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: edits across multiple non-test files pass", passed(rc, data), detail(rc, data))

    print()
    print("[Section 4] fix-now profile")
    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"tests/test_app.py": "def test_ok():\n    assert 2 > 1\n"}, "test author edits a test")
        rc, data = scan(repo, c1, c2, "fix-now")
        h.test_result("fix-now: a test change passes (no test-path rule)", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"scripts/flow-verify-receipt.py": "print('verify v2')\n"}, "unit edits verifier")
        rc, data = scan(repo, c1, c2, "fix-now")
        h.test_result("fix-now: an edit to scripts/flow-verify-receipt.py passes", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/app.py": IMPLEMENT_APP.replace("    unused = 5\n", "    unused = 6\n")}, "rewrite added line")
        rc, data = scan(repo, c0, c2, "fix-now")
        h.test_result("fix-now: a rewrite of lines added in the range passes", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/app.py": IMPLEMENT_APP.replace("    unused = 5\n", "    unused = 6\n")}, "rewrite added line")
        rc, data = scan(repo, c1, c2, "heal", revert_base=c0)
        h.test_result("heal: a rewrite of lines added in the range passes (no restore of pre-revert-base)", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"ruff.toml": '[lint]\nselect = ["F", "E"]\n'}, "edit ruff.toml")
        rc, data = scan(repo, c1, c2, "fix-now")
        h.test_result("fix-now: an edit to ruff.toml passes (not in the implement profile's list)", passed(rc, data), detail(rc, data))

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"pyproject.toml": SKIP_PYPROJECT}, "skip")
        rc, data = scan(repo, c1, c2, "fix-now")
        h.test_result("fix-now: pytest.mark.skip added in pyproject.toml fails (same as implement)",
                      failed_with(rc, data), detail(rc, data))

    print()
    print("[Section 5] Criterion 3 regression: pin today's verdict for plan-owned test edits (#468)")
    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"tests/test_app.py": "def test_ok():\n    assert True\n\ndef test_new():\n    assert 1\n"}, "plan adds test")
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("criterion 3: additive test edit under implement fails", failed_with(rc, data), detail(rc, data))
        h.test_result("criterion 3: verdict is 'fail'", data.get("verdict") == "fail", detail(rc, data))
        h.test_result("criterion 3: reasons equals exactly ['test-edit: tests/test_app.py']",
                      data.get("reasons") == ["test-edit: tests/test_app.py"], f"reasons={data.get('reasons')}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {}, "delete test", remove=["tests/test_app.py"])
        rc, data = scan(repo, c1, c2, "implement")
        h.test_result("criterion 3: deleted test under implement fails", failed_with(rc, data), detail(rc, data))
        h.test_result("criterion 3: reasons equals exactly ['test-removed: tests/test_app.py']",
                      data.get("reasons") == ["test-removed: tests/test_app.py"], f"reasons={data.get('reasons')}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"tests/test_app.py": "def test_ok():\n    assert True\n\ndef test_new():\n    assert 1\n"}, "plan adds test")
        rc, data = scan(repo, c1, c2, "fix-now")
        h.test_result("criterion 3: additive test edit under fix-now passes", passed(rc, data), detail(rc, data))
        h.test_result("criterion 3: fix-now verdict is 'pass'", data.get("verdict") == "pass", detail(rc, data))
        h.test_result("criterion 3: fix-now reasons is empty", data.get("reasons") == [], f"reasons={data.get('reasons')}")

    print()
    print("[Section 6] --revert-base rules and fail-closed refs")
    with tempfile.TemporaryDirectory() as tmp:
        repo, c0, c1 = make_repo(tmp)
        c2 = commit(repo, {"src/other.py": "def g():\n    return 2\n"}, "clean")
        rc, data = scan(repo, c1, c2, "heal")
        h.test_result("heal without --revert-base fails closed with 'missing-revert-base'",
                      failed_with(rc, data, "missing-revert-base"), detail(rc, data))
        for profile in ("fix-now", "implement"):
            rc, data = scan(repo, c1, c2, profile, revert_base=c0)
            h.test_result(f"{profile} with --revert-base is rejected with 'revert-base-not-allowed'",
                          failed_with(rc, data, "revert-base-not-allowed"), detail(rc, data))
        bogus = "f" * 40
        for profile, rb in (("implement", None), ("fix-now", None), ("heal", c0)):
            rc, data = scan(repo, c1, bogus, profile, revert_base=rb)
            h.test_result(f"{profile}: a bad --head ref fails closed with 'bad-ref'",
                          failed_with(rc, data, "bad-ref"), detail(rc, data))
        rc, data = scan(repo, bogus, c2, "implement")
        h.test_result("implement: a bad --base ref fails closed with 'bad-ref'", failed_with(rc, data, "bad-ref"), detail(rc, data))
        rc, data = scan(repo, c1, c2, "heal", revert_base=bogus)
        h.test_result("heal: a bad --revert-base ref fails closed with 'bad-ref'", failed_with(rc, data, "bad-ref"), detail(rc, data))
        rc, data = scan(Path(tmp) / "no-such-repo", c1, c2, "implement")
        h.test_result("a missing repo path fails closed", failed_with(rc, data), detail(rc, data))

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
