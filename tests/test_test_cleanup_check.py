#!/usr/bin/env python3
"""
Black-box tests for scripts/test-cleanup-check.py (#458).

Driven only through the CLI (subprocess) against tiny throwaway git repos. Covers:
1. fence: the binding-edit allowlist (imports, callee names, argument counts) and the violations
   (assertion lines, expected values, literal arguments, added or removed lines, unrecognised lines).
2. fence: mixed files (a green test's strengthening is not inspected), --head INDEX / WORKTREE,
   restore ranges, support edits, unlocated tests, and the no-change case.
3. fence: JS, Rust and Go test bodies.
4. survivors: named survivors must be collected; mutual duplicates are reported.
5. names: spec-blind names in the working set; D2 scoping; --allow.
6. Exit codes: 0 pass, 1 violation or fail-closed input, 2 argparse errors.

Run with: python3 tests/test_test_cleanup_check.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

CHECK = REPO_ROOT / "scripts" / "test-cleanup-check.py"
GIT_CFG = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=/dev/null"]
TID = "tests/test_x.py::test_parse_basic"

BASE_PY = """import math
from pkg import thing


def helper():
    return 1


def test_parse_basic():
    expected = 3
    result = parse(1, 2, 3)
    assert result == expected


def test_green_other():
    assert other(1) == 1
"""

# The same file, but the test's assertion calls the function under test (callee-rename case).
BASE_PY_ASSERT_CALL = BASE_PY.replace("assert result == expected", "assert parse(1) == expected")

BASE_JS = """import { parse } from "./parse";

it("parses basic", () => {
  const expected = 3;
  const result = parse(1, 2, 3);
  expect(result).toBe(expected);
});
"""

BASE_RUST = """use crate::parse::parse;

#[test]
fn parses_basic() {
    let expected = 3;
    let result = parse(1, 2, 3);
    assert_eq!(result, expected);
}
"""

BASE_GO = """package x

import "testing"

func TestParse(t *testing.T) {
	result := parse(1, 2, 3)
	if result != 3 {
		t.Errorf("bad")
	}
}
"""


BASE_PY_MULTI = """import math


def test_multi(
    a,
    b,
):
    expected = 3
    assert a + b == expected
"""

BASE_PY_CLASSES = """class TestA:
    def test_same(self):
        assert 1 == 1


class TestB:
    def test_same(self):
        assert 2 == 2
"""

BASE_SWIFT = """import XCTest

final class ParseTests: XCTestCase {
    func testParseBasic() {
        let expected = 3
        let result = parse(1, 2, 3)
        XCTAssertEqual(result, expected)
    }
}
"""

BASE_TS = BASE_JS.replace("const expected = 3;", "const expected: number = 3;")


def git(repo, *args):
    result = subprocess.run(["git", *GIT_CFG, *args], cwd=str(repo), capture_output=True, text=True)
    return result.stdout.strip()


def write_files(repo, files):
    for rel, text in files.items():
        path = Path(repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit(repo, files, message="c", remove=()):
    """Write files (path -> text), delete `remove`, commit everything, and return the new HEAD sha."""
    write_files(repo, files)
    for rel in remove:
        (Path(repo) / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(root, files):
    """A fresh repo whose base commit holds `files`. Returns (repo, base_sha)."""
    repo = Path(root) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    return repo, commit(repo, files, "base")


def run(repo, argv):
    """Run the checker with cwd=repo. Returns (rc, stdout, stderr)."""
    result = subprocess.run([sys.executable, str(CHECK), *argv], cwd=str(repo),
                            capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def fence_head(repo, base, head_files, test_id=TID, extra=()):
    """Commit head_files on top of base, then run the fence against that commit."""
    head_sha = commit(repo, head_files, "head")
    return run(repo, ["fence", "--base", base, "--head", head_sha, "--test", test_id, *extra])


def edited(text, old, new):
    assert old in text, f"fixture text lacks {old!r}"
    return text.replace(old, new)


def main():
    h = Harness("TEST CLEANUP CHECK TEST SUITE")

    print("[Section 1] fence: binding-edit allowlist")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(
            edited(BASE_PY, "from pkg import thing", "from pkg import thing2"),
            "parse(1, 2, 3)", "parse_cfg(1, 2, 3)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("import line and callee rename pass as binding edits",
                      rc == 0 and f"binding-edit {TID} " in out and "violation" not in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "parse(1, 2, 3)", "parse(1, 2, 3, 4)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("argument count change with literals kept passes",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "parse(1, 2, 3)", "parse(1, 2)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("dropping a trailing literal argument is a violation",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    print("[Section 2] fence: violations")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "assert result == expected", "assert result == 4")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("assert x == 3 becoming x == 4 fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")
        h.test_result("a violation prints the base body range for restore",
                      f"restore {TID} 9-12" in out, f"out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "expected = 3", "expected = 4")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("indirect expected value (expected = 3 becoming 4) fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY_ASSERT_CALL})
        head_text = edited(BASE_PY_ASSERT_CALL, "parse(1)", "parse_cfg(1)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("callee renamed inside an assertion line fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "parse(1, 2, 3)", "parse(1, 2, 4)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("a changed literal argument (f(3) becoming f(4)) fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "    expected = 3\n", "    expected = 3\n    extra = 1\n")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("an added non-import line in the body fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "    expected = 3\n", "")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("a removed non-import line in the body fails",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "result = parse(1, 2, 3)", "result = [parse(1, 2, 3)]")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("an unrecognised line (changed prefix) fails closed",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r}")

    print("[Section 3] fence: mixed file, heads, support edits")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "assert other(1) == 1",
                           "assert other(1) == 1\n    assert other(2) == 2")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("a green test strengthened in the same file is not inspected",
                      rc == 0 and "test_green_other" not in out and out.strip() == "",
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(
            edited(BASE_PY, "assert other(1) == 1", "assert other(1) == 1\n    assert other(2) == 2"),
            "assert result == expected", "assert result == 4")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("a violation reports only the violating id, not the green test",
                      rc == 1 and "test_green_other" not in out and "violation" in out,
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        git_path = Path(repo) / "tests" / "test_x.py"
        staged = edited(BASE_PY, "parse(1, 2, 3)", "parse_cfg(1, 2, 3)")
        git_path.write_text(staged)
        git(repo, "add", "tests/test_x.py")
        git_path.write_text(edited(staged, "assert result == expected", "assert result == 4"))
        rc_index, out_index, _ = run(repo, ["fence", "--base", base, "--head", "INDEX",
                                            "--test", TID])
        rc_work, out_work, _ = run(repo, ["fence", "--base", base, "--head", "WORKTREE",
                                          "--test", TID])
        h.test_result("--head INDEX ignores unstaged changes",
                      rc_index == 0 and "violation" not in out_index,
                      f"rc={rc_index} out={out_index!r}")
        h.test_result("--head WORKTREE sees unstaged changes",
                      rc_work == 1 and f"violation {TID} " in out_work,
                      f"rc={rc_work} out={out_work!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY, "src/app.py": "VALUE = 1\n"})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": BASE_PY,
                                               "conftest.py": "def sample():\n    return 1\n"},
                                  extra=["--impl-files", "src/app.py"])
        h.test_result("a changed conftest.py is a support edit and does not fail the fence",
                      rc == 0 and "support-edit conftest.py" in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY, "src/app.py": "VALUE = 1\n"})
        head_text = edited(BASE_PY, "def helper():\n    return 1", "def helper():\n    return 2")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text},
                                  extra=["--impl-files", "src/app.py"])
        h.test_result("a changed helper outside test bodies is a support edit",
                      rc == 0 and "support-edit tests/test_x.py" in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY, "src/app.py": "VALUE = 1\n"})
        rc, out, err = fence_head(repo, base, {"src/app.py": "VALUE = 2\n"},
                                  extra=["--impl-files", "src/app.py"])
        h.test_result("an implementation file is not a support edit",
                      rc == 0 and "support-edit" not in out and out.strip() == "",
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY, "src/app.py": "VALUE = 1\n"})
        head_text = edited(BASE_PY, "parse(1, 2, 3)", "parse_cfg(1, 2, 3)")
        head_text = edited(head_text, "assert other(1) == 1", "assert other(1) == 1\n    assert other(2) == 2")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text},
                                  extra=["--impl-files", "src/app.py"])
        h.test_result("binding edits and green-test strengthening in a test file are not support edits",
                      rc == 0 and "support-edit" not in out, f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        rc, out, err = run(repo, ["fence", "--base", base, "--head", "HEAD",
                                  "--test", TID])
        h.test_result("a test with no changes passes with no output",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = BASE_PY + "\n\ndef test_new():\n    assert 1 == 1\n"
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text},
                                  test_id="tests/test_x.py::test_new")
        h.test_result("a test id absent from base is unlocated and fails",
                      rc == 1 and "unlocated tests/test_x.py::test_new base" in out,
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        rc, out, err = run(repo, ["fence", "--base", "f" * 40, "--head", "HEAD", "--test", TID])
        h.test_result("a bad base ref fails closed with rc 1",
                      rc == 1 and "bad-ref" in err, f"rc={rc} err={err!r}")

    print("[Section 4] fence: JS, Rust and Go bodies")
    with tempfile.TemporaryDirectory() as tmp:
        js_id = "tests/w.test.js::parses basic"
        repo, base = make_repo(tmp, {"tests/w.test.js": BASE_JS})
        head_text = edited(edited(BASE_JS, '"./parse"', '"./parse2"'),
                           "const result = parse(1, 2, 3);", "const result = parseCfg(1, 2, 3);")
        rc, out, err = fence_head(repo, base, {"tests/w.test.js": head_text}, test_id=js_id)
        h.test_result("JS: import and callee rename pass",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        js_id = "tests/w.test.js::parses basic"
        repo, base = make_repo(tmp, {"tests/w.test.js": BASE_JS})
        head_text = edited(BASE_JS, "toBe(expected)", "toBe(4)")
        rc, out, err = fence_head(repo, base, {"tests/w.test.js": head_text}, test_id=js_id)
        h.test_result("JS: an expect(...).toBe(...) change fails",
                      rc == 1 and "violation tests/w.test.js::parses basic" in out,
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        rs_id = "tests/lib.rs::parses_basic"
        repo, base = make_repo(tmp, {"tests/lib.rs": BASE_RUST})
        head_text = edited(BASE_RUST, "let result = parse(1, 2, 3);", "let result = parse_cfg(1, 2, 3);")
        rc, out, err = fence_head(repo, base, {"tests/lib.rs": head_text}, test_id=rs_id)
        h.test_result("Rust: callee rename passes, assert_eq! unchanged",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        rs_id = "tests/lib.rs::parses_basic"
        repo, base = make_repo(tmp, {"tests/lib.rs": BASE_RUST})
        head_text = edited(BASE_RUST, "assert_eq!(result, expected);", "assert_eq!(result, 4);")
        rc, out, err = fence_head(repo, base, {"tests/lib.rs": head_text}, test_id=rs_id)
        h.test_result("Rust: an assert_eq! change fails",
                      rc == 1 and "violation tests/lib.rs::parses_basic" in out,
                      f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        go_id = "x_test.go::TestParse"
        repo, base = make_repo(tmp, {"x_test.go": BASE_GO})
        head_text = edited(BASE_GO, "parse(1, 2, 3)", "parseCfg(1, 2, 3)")
        rc, out, err = fence_head(repo, base, {"x_test.go": head_text}, test_id=go_id)
        h.test_result("Go: callee rename passes",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        go_id = "x_test.go::TestParse"
        repo, base = make_repo(tmp, {"x_test.go": BASE_GO})
        head_text = edited(BASE_GO, "if result != 3 {", "if result != 4 {")
        rc, out, err = fence_head(repo, base, {"x_test.go": head_text}, test_id=go_id)
        h.test_result("Go: a changed comparison fails",
                      rc == 1 and "violation x_test.go::TestParse" in out, f"rc={rc} out={out!r}")

    print("[Section 5] survivors")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        doc = (Path(__file__).resolve().parent.parent / "commands" / "implement-with-haiku.md").read_text()
        header = next(l.strip().strip("`") for l in doc.splitlines()
                      if l.strip().strip("`").startswith("| id |") and "survivor or proof" in l)
        ncols = header.count("|") - 1
        deletions = root / "round4-deletions.md"
        deletions.write_text(
            header + "\n|" + "---|" * ncols + "\n"
            "| tests/test_a.py::test_dup | duplicate | edit | same | `tests/test_b.py::test_b` | abc1234 |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/test_b.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("the doc's own 4.4 table header is accepted by survivors",
                      ncols >= 6 and rc == 0, f"ncols={ncols} rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text(
            "| id | class | reason | survivor or proof |\n"
            "|---|---|---|---|\n"
            "| tests/test_a.py::test_dup | duplicate | same as b | `tests/test_b.py::test_b` |\n"
            "| tests/test_c.py::test_noop | no-op | vacuous | proof: vacuous |\n"
            "| tests/test_d.py::test_h | human-spec-wrong | wrong oracle | 1a2b3c4 |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/test_b.py::test_b\ntests/test_z.py::test_z\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("survivors pass when every named survivor is collected",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text(
            "| id | class | reason | survivor or proof |\n"
            "|---|---|---|---|\n"
            "| tests/t.py::test_a | duplicate | same as b | tests/t.py::test_b |\n"
            "| tests/t.py::test_b | duplicate | same as a | tests/t.py::test_a |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_z\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("mutual duplicates both fail, naming the deleted test",
                      rc == 1 and "missing-survivor tests/t.py::test_b named-by tests/t.py::test_a" in out
                      and "missing-survivor tests/t.py::test_a named-by tests/t.py::test_b" in out,
                      f"rc={rc} out={out!r}")

    print("[Section 6] names")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        commit(repo, {"tests/test_spec_blind_foo.py": "def test_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an added spec-blind file path fails",
                      rc == 1 and "spec-blind path tests/test_spec_blind_foo.py" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        commit(repo, {"tests/test_new.py": "def test_spec_blind_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an added spec-blind test name fails",
                      rc == 1 and "spec-blind line tests/test_new.py:1" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        commit(repo, {"tests/w.test.js": 'describe("spec-blind widget", () => {});\n'}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an added spec-blind describe string fails",
                      rc == 1 and "spec-blind line" in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n",
                                     "tests/test_legacy.py": "# spec-blind legacy note\n"})
        commit(repo, {"README.md": "# r\nmore\n"}, "unrelated")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("a term only in lines unchanged since base passes (D2 scoping)",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        commit(repo, {"tests/test_spec_blind_foo.py": "def test_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base,
                                  "--allow", "tests/test_spec_blind_foo.py"])
        h.test_result("--allow exempts an allowed path",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    print("[Section 7] argparse usage errors exit 2")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        rc, _, _ = run(repo, ["fence", "--head", "HEAD", "--test", TID])
        h.test_result("fence without --base exits 2", rc == 2, f"rc={rc}")
        rc, _, _ = run(repo, ["fence", "--base", base, "--head", "HEAD", "--test", "nocolons"])
        h.test_result("a --test value without FILE::NAME exits 2", rc == 2, f"rc={rc}")
        rc, _, _ = run(repo, ["survivors", "--deletions", "x"])
        h.test_result("survivors without --collected exits 2", rc == 2, f"rc={rc}")
        rc, _, _ = run(repo, [])
        h.test_result("no subcommand exits 2", rc == 2, f"rc={rc}")

    print("[Section 8] fence: sizing, classes, decorators, continuation lines (fail closed)")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY_MULTI})
        head_sha = commit(repo, {"tests/test_x.py": edited(BASE_PY_MULTI, "assert a + b == expected",
                                                           "assert a + b == 4")}, "head")
        multi_id = "tests/test_x.py::test_multi"
        rc, out, err = run(repo, ["fence", "--base", base, "--head", head_sha, "--test", multi_id])
        h.test_result("multi-line signature: a body edit after the signature is a violation",
                      rc == 1 and f"violation {multi_id} " in out and f"restore {multi_id} 4-9" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY_MULTI})
        head_sha = commit(repo, {"tests/test_x.py": "import math\n\n\ndef test_multi(\n    a,\n    b\n"},
                          "head")
        rc, out, err = run(repo, ["fence", "--base", base, "--head", head_sha,
                                  "--test", "tests/test_x.py::test_multi"])
        h.test_result("a signature that never closes is unlocated and fails closed",
                      rc == 1 and "unlocated tests/test_x.py::test_multi head" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_c.py": BASE_PY_CLASSES})
        head_sha = commit(repo, {"tests/test_c.py": edited(BASE_PY_CLASSES, "assert 2 == 2",
                                                           "assert 2 == 3")}, "head")
        rc_b, out_b, err_b = run(repo, ["fence", "--base", base, "--head", head_sha,
                                        "--test", "tests/test_c.py::TestB::test_same"])
        rc_a, out_a, _ = run(repo, ["fence", "--base", base, "--head", head_sha,
                                    "--test", "tests/test_c.py::TestA::test_same"])
        rc_amb, out_amb, _ = run(repo, ["fence", "--base", base, "--head", head_sha,
                                        "--test", "tests/test_c.py::test_same"])
        h.test_result("class segment selects the TestB definition (edit is a violation)",
                      rc_b == 1 and "violation tests/test_c.py::TestB::test_same " in out_b,
                      f"rc={rc_b} out={out_b!r} err={err_b!r}")
        h.test_result("class segment selects TestA, which is unchanged (passes)",
                      rc_a == 0 and out_a == "", f"rc={rc_a} out={out_a!r}")
        h.test_result("a same-named test without a class segment is ambiguous and fails",
                      rc_amb == 1 and "unlocated tests/test_c.py::test_same base/head" in out_amb,
                      f"rc={rc_amb} out={out_amb!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_sha = commit(repo, {"tests/test_x.py": edited(
            BASE_PY, "def test_parse_basic():", "@pytest.mark.skip\ndef test_parse_basic():")}, "head")
        rc, out, err = run(repo, ["fence", "--base", base, "--head", head_sha, "--test", TID])
        h.test_result("a decorator added above a test is a violation",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base_text = edited(BASE_PY, "def test_parse_basic():",
                           '@pytest.mark.skip(reason="old")\ndef test_parse_basic():')
        repo, base = make_repo(tmp, {"tests/test_x.py": base_text})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": edited(
            base_text, "@pytest.mark.skip", "@pytest.mark.xfail")})
        h.test_result("a decorator callee swap with the same arguments is a violation, restore covers it",
                      rc == 1 and f"violation {TID} " in out and f"restore {TID} 9-13" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base_text = edited(BASE_PY, "    result = parse(1, 2, 3)\n", '    label = "parse(1)"\n')
        repo, base = make_repo(tmp, {"tests/test_x.py": base_text})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": edited(
            base_text, 'label = "parse(1)"', 'label = "parse_cfg(1)"')})
        h.test_result("a callee name inside a string literal is a violation, not a binding edit",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base_text = edited(BASE_JS, "expect(result).toBe(expected);",
                           "expect(result)\n    .toBe(expected);")
        repo, base = make_repo(tmp, {"tests/w.test.js": base_text})
        rc, out, err = fence_head(repo, base, {"tests/w.test.js": edited(
            base_text, ".toBe(expected);", ".toEqual(expected);")},
            test_id="tests/w.test.js::parses basic")
        h.test_result("a matcher swap on a method-chain continuation line is a violation",
                      rc == 1 and "violation tests/w.test.js::parses basic " in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base_text = edited(BASE_RUST, "assert_eq!(result, expected);",
                           "assert_eq!(\n        parse(1, 2, 3),\n        expected\n    );")
        repo, base = make_repo(tmp, {"tests/lib.rs": base_text})
        rc, out, err = fence_head(repo, base, {"tests/lib.rs": edited(
            base_text, "parse(1, 2, 3),", "parse_cfg(1, 2, 3),")},
            test_id="tests/lib.rs::parses_basic")
        h.test_result("a callee swap inside an open assert_eq! continuation is a violation",
                      rc == 1 and "violation tests/lib.rs::parses_basic " in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        base_text = edited(BASE_PY, "result = parse(1, 2, 3)", "result = parse(\n        1, 2, 3)")
        repo, base = make_repo(tmp, {"tests/test_x.py": base_text})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": edited(
            base_text, "result = parse(", "result = parse_cfg(")})
        h.test_result("a callee change in a multi-line call fails closed",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"x_test.go": BASE_GO})
        head_text = BASE_GO.rstrip()[: BASE_GO.rstrip().rfind("}")]
        rc, out, err = fence_head(repo, base, {"x_test.go": head_text}, test_id="x_test.go::TestParse")
        h.test_result("a brace block that never closes is unlocated and fails closed",
                      rc == 1 and "unlocated x_test.go::TestParse head" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 9] fence: Swift, TS, fallback extension, path confinement")
    with tempfile.TemporaryDirectory() as tmp:
        swift_id = "Tests/ParseTests.swift::testParseBasic"
        repo, base = make_repo(tmp, {"Tests/ParseTests.swift": BASE_SWIFT})
        rc, out, err = fence_head(repo, base, {"Tests/ParseTests.swift": edited(
            BASE_SWIFT, "parse(1, 2, 3)", "parseCfg(1, 2, 3)")}, test_id=swift_id)
        h.test_result("Swift: callee rename passes",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")
    with tempfile.TemporaryDirectory() as tmp:
        swift_id = "Tests/ParseTests.swift::testParseBasic"
        repo, base = make_repo(tmp, {"Tests/ParseTests.swift": BASE_SWIFT})
        rc, out, err = fence_head(repo, base, {"Tests/ParseTests.swift": edited(
            BASE_SWIFT, "XCTAssertEqual(result, expected)", "XCTAssertEqual(result, 4)")},
            test_id=swift_id)
        h.test_result("Swift: an XCTAssertEqual change fails",
                      rc == 1 and "violation Tests/ParseTests.swift::testParseBasic " in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        ts_id = "tests/w.test.ts::parses basic"
        repo, base = make_repo(tmp, {"tests/w.test.ts": BASE_TS})
        rc, out, err = fence_head(repo, base, {"tests/w.test.ts": edited(
            BASE_TS, "parse(1, 2, 3)", "parseCfg(1, 2, 3)")}, test_id=ts_id)
        h.test_result("TS: typed binding and callee rename pass",
                      rc == 0 and "violation" not in out, f"rc={rc} out={out!r} err={err!r}")
    with tempfile.TemporaryDirectory() as tmp:
        ts_id = "tests/w.test.ts::parses basic"
        repo, base = make_repo(tmp, {"tests/w.test.ts": BASE_TS})
        rc, out, err = fence_head(repo, base, {"tests/w.test.ts": edited(
            BASE_TS, "toBe(expected)", "toBe(4)")}, test_id=ts_id)
        h.test_result("TS: an expect(...).toBe(...) change fails",
                      rc == 1 and "violation tests/w.test.ts::parses basic" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY, "data/notes.txt": "one\n"})
        rc, out, err = fence_head(repo, base, {"data/notes.txt": "two\n"},
                                  extra=["--impl-files", "src/app.py"])
        h.test_result("fallback extension: a changed .txt file is a support edit",
                      rc == 0 and "support-edit data/notes.txt" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        rc, out, err = run(repo, ["fence", "--base", base, "--head", "HEAD", "--test", "../x.py::test_a"])
        h.test_result("a --test path with a '..' component is rejected",
                      rc == 1 and "escapes the repo root" in err, f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = run(repo, ["fence", "--base", base, "--head", "HEAD",
                                  "--test", "/etc/hosts::test_a"])
        h.test_result("an absolute --test path is rejected",
                      rc == 1 and "escapes the repo root" in err, f"rc={rc} out={out!r} err={err!r}")

    print("[Section 10] survivors: table contract and renamed survivors")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n")
        collected = root / "collected.txt"
        collected.write_text("tests/test_b.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a header-only table is a valid empty table and passes",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             "| tests/t.py::test_a | duplicate | same as b |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a data row with the wrong column count fails closed",
                      rc == 1 and "cannot be parsed" in err, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             "| tests/t.py::test_a | duplicate | same as b | nonsense |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a survivor cell that is neither a test id, proof, nor SHA fails closed",
                      rc == 1 and "not a test id" in err, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("Round 4 deletions were recorded in prose only.\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a non-empty file with no table fails closed",
                      rc == 1 and "no markdown table" in err, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             "| tests/t.py::test_gone | duplicate | same as new | `tests/t.py::test_old` |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_new\n")
        merged = root / "merged.txt"
        merged.write_text("# renamed in round 3\n\ntests/t.py::test_old -> tests/t.py::test_new\n")
        rc_m, out_m, err_m = run(root, ["survivors", "--deletions", str(deletions),
                                        "--collected", str(collected), "--merged", str(merged)])
        rc_n, out_n, _ = run(root, ["survivors", "--deletions", str(deletions),
                                    "--collected", str(collected)])
        h.test_result("a renamed survivor is found under its new id via --merged",
                      rc_m == 0 and out_m == "", f"rc={rc_m} out={out_m!r} err={err_m!r}")
        h.test_result("without --merged the pre-merge survivor id is looked up and missing",
                      rc_n == 1 and "missing-survivor tests/t.py::test_old named-by tests/t.py::test_gone" in out_n,
                      f"rc={rc_n} out={out_n!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             "| tests/t.py::test_gone | duplicate | same | tests/t.py::test_old |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_new\n")
        merged = root / "merged.txt"
        merged.write_text("tests/t.py::test_old tests/t.py::test_new\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected), "--merged", str(merged)])
        h.test_result("a malformed --merged line fails closed",
                      rc == 1 and "expected '<old test id> -> <new test id>'" in err,
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 11] names: working tree, untracked files, --head")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n", "tests/test_old.py": "def test_x():\n    pass\n"})
        os.rename(repo / "tests" / "test_old.py", repo / "tests" / "test_spec_blind_new.py")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an uncommitted rename to a spec-blind path fails in the working tree",
                      rc == 1 and "spec-blind path tests/test_spec_blind_new.py" in out,
                      f"rc={rc} out={out!r} err={err!r}")
        rc_head, out_head, _ = run(repo, ["names", "--base", base, "--head", "HEAD"])
        h.test_result("--head HEAD does not see the uncommitted rename",
                      rc_head == 0 and out_head == "", f"rc={rc_head} out={out_head!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        write_files(repo, {"tests/test_scratch.py": "# spec-blind note\n"})
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an untracked file's spec-blind line is a hit in the working tree",
                      rc == 1 and "spec-blind line tests/test_scratch.py:1" in out,
                      f"rc={rc} out={out!r} err={err!r}")
        rc_head, out_head, _ = run(repo, ["names", "--base", base, "--head", "HEAD"])
        h.test_result("--head HEAD ignores untracked files",
                      rc_head == 0 and out_head == "", f"rc={rc_head} out={out_head!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        commit(repo, {"tests/test_spec_blind_c.py": "def test_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base, "--head", "HEAD"])
        h.test_result("--head HEAD still flags a committed spec-blind path",
                      rc == 1 and "spec-blind path tests/test_spec_blind_c.py" in out,
                      f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = run(repo, ["names", "--base", base, "--head", "nope"])
        h.test_result("names rejects a --head value outside WORKTREE/HEAD (argparse usage error)",
                      rc == 2, f"rc={rc} err={err!r}")

    print("[Section 12] special characters and edge cases")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        # File path with spaces; should not cause parsing failures
        head_text = edited(BASE_PY, "parse(1, 2, 3)", "parse(1, 2)")
        commit(repo, {"tests/test with spaces.py": head_text}, "add")
        # Test against the spaced filename
        rc, out, err = run(repo, ["fence", "--base", base, "--head", "HEAD",
                                  "--test", "tests/test with spaces.py::test_parse_basic"])
        h.test_result("a fence check handles a test file with spaces in the name",
                      rc == 1 and "violation" in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        # Decorator/attribute on separate lines (multi-line decorators)
        base_multi_decorator = """import math

@pytest.mark.parametrize(
    "param",
    [1, 2, 3]
)
def test_parse_basic():
    expected = 3
    result = parse(1, 2, 3)
    assert result == expected
"""
        head_multi_decorator = base_multi_decorator.replace("parse(1, 2, 3)", "parse(1, 2)")
        repo, base = make_repo(tmp, {"tests/test_x.py": base_multi_decorator})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_multi_decorator})
        h.test_result("a multi-line decorator is part of the test range and violations are caught",
                      rc == 1 and "violation" in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        # Change decorator argument (should fail)
        base_with_decorator = edited(BASE_PY, "def test_parse_basic():",
                                    "@pytest.mark.xfail\ndef test_parse_basic():")
        head_with_decorator_change = edited(base_with_decorator, "@pytest.mark.xfail", "@pytest.mark.skip")
        repo, base = make_repo(tmp, {"tests/test_x.py": base_with_decorator})
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_with_decorator_change})
        h.test_result("a decorator change (xfail to skip) is a violation",
                      rc == 1 and "violation" in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        # Proof cell in survivors table (should pass)
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             "| tests/t.py::test_gone | junk | removed | proof: no calls |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a 'proof:' cell in the survivor column is allowed",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        # SHA cell in survivors table (should pass)
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        sha40 = "a" * 40
        deletions.write_text("| id | class | reason | survivor or proof |\n|---|---|---|---|\n"
                             f"| tests/t.py::test_gone | junk | removed | {sha40} |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/t.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("a 40-character SHA cell in the survivor column is allowed",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
