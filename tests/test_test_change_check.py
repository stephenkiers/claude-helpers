#!/usr/bin/env python3
"""
Tests for scripts/test-change-check.py, the static removal check for test files (#511).

The script takes --repo, --base and one or more --remove PATH arguments, and prints one JSON
object with `verdict` and `findings`. Every case runs the real script as a subprocess against a
temporary git repository built here.

Covered:
- a single removal that leaves its directory with other test files is not a suite emptying
- one call that removes every test file in a directory and leaves other files behind reports `suite-emptied`
- removing every file in a directory (nothing left behind) reports no `suite-emptied`
- a --remove path absent from the base tree exits 2
- an empty --remove path is rejected with exit 2

Run with: python3 tests/test_test_change_check.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "test-change-check.py"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]

BASE_FILES = {
    "tests/a/test_one.py": "def test_one():\n    assert True\n",
    "tests/a/test_two.py": "def test_two():\n    assert True\n",
    "tests/b/test_three.py": "def test_three():\n    assert True\n",
    "tests/a/README.md": "notes\n",
    "tests/b/README.md": "notes\n",
    "tests/c/test_four.py": "def test_four():\n    assert True\n",
    "src/app.py": "VALUE = 1\n",
}


def git(cwd, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout.strip()


def build_base_repo(root):
    """Temp git repo with BASE_FILES committed. Returns (repo path, base sha)."""
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    for rel, text in BASE_FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def run_check(repo, base, removals):
    """Run the script with the given --remove paths. Returns (exit code, parsed JSON or None, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base]
    for path in removals:
        cmd += ["--remove", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        parsed = None
    return result.returncode, parsed, result.stdout, result.stderr


def main():
    h = Harness("TEST-CHANGE-CHECK TESTS (#511)")

    print("[Removal of test files: suite emptying]")

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, parsed, out, err = run_check(repo, base, ["tests/a/test_one.py"])
        h.test_result(
            "removing one of two test files leaves the suite non-empty (exit 0, verdict ok)",
            code == 0 and parsed is not None and parsed.get("verdict") == "ok",
            f"exit={code} out={out[:300]} err={err[-300:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, parsed, out, err = run_check(repo, base, ["tests/a/test_one.py", "tests/a/test_two.py"])
        flagged = "suite-emptied" in out
        h.test_result(
            "one call removing every test file in tests/a reports suite-emptied",
            flagged and parsed is not None and parsed.get("verdict") != "ok",
            f"exit={code} out={out[:400]} err={err[-300:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, parsed, out, err = run_check(repo, base, ["tests/b/test_three.py"])
        h.test_result(
            "removing the only test file in tests/b reports suite-emptied",
            "suite-emptied" in out and parsed is not None and parsed.get("verdict") != "ok",
            f"exit={code} out={out[:400]} err={err[-300:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, parsed, out, err = run_check(repo, base, ["tests/c/test_four.py"])
        h.test_result(
            "removing the only file of a directory leaves nothing behind and reports no suite-emptied",
            "suite-emptied" not in out and code == 0,
            f"exit={code} out={out[:400]} err={err[-300:]}",
        )

    print("[Removal argument validation]")

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, _parsed, out, err = run_check(repo, base, ["tests/a/does_not_exist.py"])
        h.test_result(
            "--remove path absent from the base tree exits 2",
            code == 2,
            f"exit={code} out={out[:300]} err={err[-300:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        repo, base = build_base_repo(td)
        code, _parsed, out, err = run_check(repo, base, [""])
        h.test_result(
            "empty --remove value is rejected with exit 2",
            code == 2,
            f"exit={code} out={out[:300]} err={err[-300:]}",
        )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
