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

import json
import os
import re
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
        doc = (Path(__file__).resolve().parent.parent / "prompts" / "test-cleanup-reference.md").read_text()
        header = next((ln.strip().strip("`") for ln in doc.splitlines()
                      if ln.strip().strip("`").startswith("| id |") and "survivor or proof" in ln),
                     None)
        h.test_result("test-cleanup-reference.md contains 4.4 header",
                      header is not None and "survivor or proof" in header,
                      "Header with 'survivor or proof' not found in reference doc")
        assert header is not None
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

    print("[Section 13] survivors: column-finding by header name (item 1)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deletions = root / "round4-deletions.md"
        # Reorder columns: survivor or proof comes first now, instead of being 4th
        deletions.write_text(
            "| survivor or proof | id | class | reason |\n"
            "|---|---|---|---|\n"
            "| `tests/test_b.py::test_b` | tests/test_a.py::test_dup | duplicate | same |\n")
        collected = root / "collected.txt"
        collected.write_text("tests/test_b.py::test_b\n")
        rc, out, err = run(root, ["survivors", "--deletions", str(deletions),
                                  "--collected", str(collected)])
        h.test_result("survivors finds columns by header name when order differs",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    print("[Section 14] fence: def lines (item 5)")
    with tempfile.TemporaryDirectory() as tmp:
        # Test that editing just the def line (function name) is a violation
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "def test_parse_basic():", "def test_parse_basic_v2():")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("renaming the test function (def line) is a violation",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        # Test that adding a parameter to the def line is a violation
        repo, base = make_repo(tmp, {"tests/test_x.py": BASE_PY})
        head_text = edited(BASE_PY, "def test_parse_basic():", "def test_parse_basic(extra):")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        h.test_result("adding a parameter to the test def line is a violation",
                      rc == 1 and f"violation {TID} " in out, f"rc={rc} out={out!r} err={err!r}")

    print("[Section 15] fence: span sizing fail-closed (item 2)")
    with tempfile.TemporaryDirectory() as tmp:
        # A test body that has a line starting at column 0 inside the body
        # The span should be sized by AST end_lineno, correctly ending before column-0 line
        base_bad_indent = """import math

def test_parse_basic():
    expected = 3
    result = parse(1, 2, 3)
    assert result == expected
# This line is at column 0 and follows the test body
def next_test():
    pass
"""
        repo, base = make_repo(tmp, {"tests/test_x.py": base_bad_indent})
        # Edit inside the test body to trigger violation detection
        head_text = base_bad_indent.replace("result = parse(1, 2, 3)", "result = parse(1, 2, 4)")
        rc, out, err = fence_head(repo, base, {"tests/test_x.py": head_text})
        # The span should be correctly sized (3-6) despite the column-0 line below
        h.test_result("span is correctly sized by AST end_lineno despite column-0 line below",
                      rc == 1 and "restore tests/test_x.py::test_parse_basic 3-6" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 16] names: quoted paths (item 4)")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        # File with spaces in path
        commit(repo, {"tests/test spec blind foo.py": "def test_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an added file with spaces and spec-blind in path fails",
                      rc == 1 and "spec-blind path" in out and "test spec blind foo.py" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        # Test within a file that has spaces in its name
        commit(repo, {"tests/w test.py": "def test_spec_blind_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("a spec-blind test name in a spaced filename is detected",
                      rc == 1 and "spec-blind line" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        # Test file with non-ASCII characters
        commit(repo, {"tests/test_café.py": "def test_x():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("an added file with non-ASCII characters is handled",
                      rc == 0 and out == "", f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        # Non-ASCII in spec-blind check
        commit(repo, {"tests/test_café.py": "def test_spec_blind_café():\n    pass\n"}, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("a spec-blind name with non-ASCII characters is caught",
                      rc == 1 and "spec-blind" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 17] names --hygiene: token hits and segment-only matches")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_parser_hardening.py": "def test_x():\n    pass\n",
            "tests/test_parser.py": "def test_a():\n    pass\n\ndef test_x_followups():\n    pass\n\ndef test_parse_wave_2():\n    pass\n"
        }, "add")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base])
        h.test_result("--hygiene detects all three added hygiene tokens: hardening, followups, wave_2",
                      rc == 1 and "test_parser_hardening.py" in out and "test_x_followups" in out
                      and "test_parse_wave_2" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_spec_loader.py": "def test_x():\n    pass\n",
            "tests/test_parser.py": "def test_a():\n    pass\n\ndef test_wave_2_case():\n    pass\n"
        }, "add")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base])
        h.test_result("segment-only matches (spec_loader, wave_2_case) are not hits and exit 0",
                      rc == 0 and "segment-only:" in out and "test_spec_loader.py" in out
                      and "test_wave_2_case" in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_parser_hardening.py": "def test_x():\n    pass\n"
        }, "add")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base,
                                  "--allow", "tests/test_parser_hardening.py"])
        h.test_result("--allow exempts a hygiene hit even with --hygiene flag",
                      rc == 0 and out == "",
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_parser_hardening.py": "def test_x_followups():\n    pass\n"
        }, "add")
        rc, out, err = run(repo, ["names", "--base", base])
        h.test_result("without --hygiene, hygiene tokens in added paths and names are not checked (rc 0)",
                      rc == 0 and out == "",
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_respec.py": "def test_x():\n    pass\n"
        }, "add")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base])
        h.test_result("a token embedded inside a word (test_respec) is not a hygiene hit",
                      rc == 0 and out == "",
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 18] working-ids: added tests only, sorted")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        commit(repo, {
            "tests/test_parser.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
            "tests/test_lexer.py": "def test_c():\n    pass\n"
        }, "add")
        head_sha = git(repo, "rev-parse", "HEAD")
        rc, out, err = run(repo, ["working-ids", "--base", base, "--head", head_sha])
        h.test_result("working-ids returns exactly the added tests, in sorted order, one per line",
                      rc == 0 and out == "tests/test_lexer.py::test_c\ntests/test_parser.py::test_b\n",
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    pass\n"})
        rc, out, err = run(repo, ["working-ids", "--base", base, "--head", "HEAD"])
        h.test_result("working-ids with no added tests prints nothing",
                      rc == 0 and out == "",
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 19] layout: test names from base SHA, no bodies")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {
            "tests/test_parser.py": "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n",
            "src/parser.py": "def parse(x):\n    return x\n"
        })
        commit(repo, {
            "tests/test_parser.py": "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n\ndef test_c():\n    assert 3\n"
        }, "add")
        rc, out, err = run(repo, ["layout", "--base", base, "--impl", "src/parser.py"])
        h.test_result("layout lists the file path and test names from base (test_a, test_b, NOT test_c)",
                      rc == 0 and "tests/test_parser.py" in out and "test_a" in out and "test_b" in out
                      and "test_c" not in out and "assert" not in out,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"src/parser.py": "def parse(x):\n    return x\n"})
        rc, out, err = run(repo, ["layout", "--base", base, "--impl", "src/new_module.py"])
        h.test_result("layout exits 0 with zero names when impl file is absent at base",
                      rc == 0 and out == "",
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {
            "tests/test_parser.py": "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n",
            "src/parser.py": "def parse(x):\n    return x\n"
        })
        rc, out, err = run(repo, ["layout", "--impl", "src/parser.py"])
        h.test_result("layout without --base lists only test paths from the working tree, no names",
                      rc == 0 and out.split("\n") == ["tests/test_parser.py", ""],
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {
            "tests/test_parser.py": "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n\ndef test_c():\n    assert 3\n",
            "src/parser.py": "def parse(x):\n    return x\n"
        })
        rc, out, err = run(repo, ["layout", "--base", base, "--impl", "src/parser.py", "--max-names", "2"])
        h.test_result("layout prints (truncated) only when a name is actually cut off",
                      rc == 0 and out.split("\n") == ["tests/test_parser.py", "  test_a", "  test_b", "(truncated)", ""],
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {
            "tests/test_parser.py": "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n\ndef test_c():\n    assert 3\n",
            "src/parser.py": "def parse(x):\n    return x\n"
        })
        rc, out, err = run(repo, ["layout", "--base", base, "--impl", "src/parser.py", "--max-names", "3"])
        h.test_result("layout with exactly max-names names does not print (truncated)",
                      rc == 0 and "(truncated)" not in out and out.count("  test_") == 3,
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"tests/test_parser.py": "def test_a():\n    assert 1\n"})
        rc, out, err = run(repo, ["layout", "--base", "0123456789abcdef0123456789abcdef01234567",
                                  "--impl", "src/parser.py"])
        h.test_result("layout with an unknown --base SHA fails (no silent exit 0)",
                      rc != 0,
                      f"rc={rc} out={out!r} err={err!r}")

    print("[Section 20] overlaps: parse overlaps lines, build candidates")
    # --added-ids and --report are file paths the command opens itself; nothing is read from stdin.
    # --out is confined to the repo (or $FLOW_DIR), which needs a git repo as the cwd, so these run in one.
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "report1.md").write_text("t::a | overlaps: none\nt::b | overlaps: t::old\nt::c overlaps t::x\n")
        (repo / "added.txt").write_text("t::b\nt::c\n")
        rc, out, err = run(repo, ["overlaps", "--report", "report1.md", "--added-ids", "added.txt",
                                  "--out", "candidates.md"])
        candidates = repo / "candidates.md"
        h.test_result("overlaps parses reports and added-ids and writes --out",
                      rc == 0 and candidates.exists(),
                      f"rc={rc} out={out!r} err={err!r}")
        content = candidates.read_text() if candidates.exists() else ""
        h.test_result("overlaps writes exact rows: candidate t::b -> t::old, unreported t::c, summary last",
                      content == "- t::b | overlaps: t::old | class-1 candidate\n"
                                 "unreported:\n- t::c\ncandidates: 1  none: 0  unreported: 1\n",
                      f"content={content!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "report1.md").write_text("t::a | overlaps: none\nt::b | overlaps: t::old\n")
        (repo / "report2.md").write_text("t::d | overlaps: NONE\nt::z | overlaps: t::q\n")
        (repo / "added.txt").write_text("t::a\nt::b\nt::d\n")
        rc, out, err = run(repo, ["overlaps", "--report", "report1.md", "--report", "report2.md",
                                  "--added-ids", "added.txt", "--out", "candidates.md"])
        candidates = repo / "candidates.md"
        content = candidates.read_text() if candidates.exists() else ""
        h.test_result("overlaps accepts multiple --report files, ignores ids not in added-ids, treats NONE as none",
                      rc == 0 and content == "- t::b | overlaps: t::old | class-1 candidate\n"
                                             "unreported:\ncandidates: 1  none: 2  unreported: 0\n",
                      f"rc={rc} content={content!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "report1.md").write_text("t::a | overlaps: none\n")
        (repo / "added.txt").write_text("t::a\nt::b\n")
        (repo / "candidates.md").write_text("old content\n")
        rc, out, err = run(repo, ["overlaps", "--report", "report1.md", "--added-ids", "added.txt",
                                  "--out", "candidates.md"])
        content = (repo / "candidates.md").read_text()
        h.test_result("overlaps overwrites --out file in full and lists unreported ids",
                      rc == 0 and "old content" not in content and "- t::b" in content
                      and "unreported:" in content,
                      f"rc={rc} content={content!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "added.txt").write_text("t::a\n")
        rc, out, err = run(repo, ["overlaps", "--report", "missing-report.md", "--added-ids", "added.txt",
                                  "--out", "candidates.md"])
        content = (repo / "candidates.md").read_text() if (repo / "candidates.md").exists() else ""
        h.test_result("overlaps with missing report file treats it as zero lines (t::a unreported)",
                      rc == 0 and content == "unreported:\n- t::a\ncandidates: 0  none: 0  unreported: 1\n",
                      f"rc={rc} content={content!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "added.txt").write_text("t::a\n")
        # the system temp dir is allowed (the run's scratch lives there); a path elsewhere is not
        in_tmp = Path(tmp) / "scratch-out.md"
        rc, out, err = run(repo, ["overlaps", "--added-ids", "added.txt", "--out", str(in_tmp)])
        h.test_result("overlaps accepts an absolute --out under the system temp dir",
                      rc == 0 and in_tmp.exists(), f"rc={rc} err={err!r}")
        outside = Path("/nonexistent-root-464/outside.md")
        rc, out, err = run(repo, ["overlaps", "--added-ids", "added.txt", "--out", str(outside)])
        h.test_result("overlaps refuses an absolute --out outside the repo",
                      rc == 1 and not outside.exists() and "escapes" in err,
                      f"rc={rc} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        (repo / "added.txt").write_text("t::a\n")
        rc, out, err = run(repo, ["overlaps", "--added-ids", "added.txt", "--out", "../escape.md"])
        h.test_result("overlaps refuses a relative --out that climbs out with ..",
                      rc == 1 and not (Path(tmp) / "escape.md").exists(),
                      f"rc={rc} err={err!r}")

    print("[Section 21] hygiene-json: measurement JSON from scratch evidence")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(Path(tmp) / "missing"), "--step", "implement",
                                  "--out", str(Path(tmp) / "m.json")])
        h.test_result("hygiene-json exits 2 when --scratch is not a directory",
                      rc == 2 and not (Path(tmp) / "m.json").exists(),
                      f"rc={rc} out={out!r} err={err!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()
        (scratch / "round2-hygiene.txt").write_text("round2-hygiene: 2 hits\nsegment-only: test_x_wave_2\n")
        (scratch / "round4-hygiene.txt").write_text("tests/test_a.py::test_spec_x\nsegment-only: tests/t.py::test_wave_2\n")
        (scratch / "round4-candidates.md").write_text("candidates: 1  none: 2  unreported: 0\n")
        (scratch / "round4.md").write_text("round4: 3 deleted, 1 relocated\n")
        (scratch / "layout-names-given.txt").write_text("true\n")
        out_path = Path(tmp) / "measure.json"
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "implement", "--out", str(out_path)])
        h.test_result("hygiene-json prints the output path on success and exits 0",
                      rc == 0 and out.strip() == str(out_path) and out_path.exists(),
                      f"rc={rc} out={out!r} err={err!r}")
        data = json.loads(out_path.read_text()) if out_path.exists() else {}
        expected = {
            "round2_hygiene_hits": 2, "round2_segment_only": 1,
            "round4_hygiene_hits": 1, "round4_segment_only": 1,
            "overlap_candidates": 1, "overlap_none": 2, "overlap_unreported": 0,
            "round4_deleted": 3, "round4_relocated": 1,
            "layout_names_given": True, "step": "implement",
        }
        h.test_result("hygiene-json writes exactly the documented keys with the scratch values",
                      data == expected, f"data={data!r}")

    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n"})
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()
        (scratch / "round2-hygiene.txt").write_text("round2-hygiene: unavailable\n")
        out_path = Path(tmp) / "measure.json"
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", str(out_path)])
        data = json.loads(out_path.read_text()) if out_path.exists() else {}
        h.test_result("hygiene-json reports null for missing or unavailable evidence and false for layout names",
                      rc == 0 and data.get("round2_hygiene_hits") is None
                      and data.get("round4_hygiene_hits") is None
                      and data.get("overlap_candidates") is None
                      and data.get("layout_names_given") is False,
                      f"rc={rc} data={data!r} err={err!r}")

    check_round2_gaps(h)
    check_round3_followups(h)
    check_empty_suites(h)

    h.summarize_and_exit()


def check_round2_gaps(h):
    """Cases the earlier groups do not reach: an edited existing test is not an added test for
    names --hygiene or working-ids; an untracked added file with a hygiene token in its stem is a
    hit in the working tree; hygiene-json reads a 'true' layout-names file from the scratch dir by
    default. Every case drives the CLI against a throwaway repo."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "edit"
        root.mkdir()
        repo, base = make_repo(root, {"tests/test_hardening_old.py": BASE_PY})
        write_files(repo, {"tests/test_hardening_old.py": BASE_PY + "\n# edited in place\n"})
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base, "--head", "WORKTREE"])
        h.test_result("names --hygiene ignores an edited existing test that has a hygiene token in its stem",
                      rc == 0, f"rc={rc} out={out!r} err={err!r}")
        write_files(repo, {"tests/test_new_followups.py": BASE_PY})
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base, "--head", "WORKTREE"])
        h.test_result("names --hygiene flags an untracked added file with a hygiene token in its stem",
                      rc == 1, f"rc={rc} out={out!r} err={err!r}")
        head = commit(repo, {"tests/test_committed_followups.py": BASE_PY}, "add committed hygiene file")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base, "--head", "HEAD"])
        h.test_result("names --hygiene flags a committed added file with a hygiene token in its stem",
                      rc == 1, f"rc={rc} out={out!r} err={err!r}")

        root = Path(tmp) / "wids"
        root.mkdir()
        repo, base = make_repo(root, {"tests/test_a.py": BASE_PY})
        edited_a = edited(BASE_PY, "assert result == expected", "assert result == 4")
        head = commit(repo, {"tests/test_a.py": edited_a + "\n\ndef test_brand_new():\n    assert 1 == 1\n"}, "head")
        rc, out, err = run(repo, ["working-ids", "--base", base, "--head", head])
        ids = set(out.split())
        h.test_result("working-ids lists only the new test when an existing test in the same file is edited",
                      rc == 0 and ids == {"tests/test_a.py::test_brand_new"},
                      f"rc={rc} ids={sorted(ids)!r} err={err!r}")

        root = Path(tmp) / "hyg"
        root.mkdir()
        scratch = root / "scratch"
        scratch.mkdir()
        (scratch / "layout-names-given.txt").write_text("true\n")
        out_path = root / "measure.json"
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "implement",
                                  "--out", str(out_path)])
        data = json.loads(out_path.read_text()) if out_path.exists() else {}
        h.test_result("hygiene-json records layout_names_given true from the default scratch file",
                      rc == 0 and data.get("layout_names_given") is True,
                      f"rc={rc} data={data!r} err={err!r}")


UNIT_SIZING = {"schema_version": 1, "kind": "plan", "steps": 2, "files": 2, "findings": None,
               "ceilings": {"steps": 8, "files": 12, "findings": 16}, "min_units": 1, "status": "ok", "reason": ""}
PROGRESS = REPO_ROOT / "scripts" / "unit-progress.py"


def init_units(scratch, anchor, extra_args=(), env=None, where=None):
    """Run unit-progress.py init (fresh main record pinned to `anchor`) for the implement step."""
    payload = json.dumps({"units": [{"unit_id": "u1", "sub_task": "one", "owned_files": ["a.py"]}],
                          "sizing": UNIT_SIZING, "basis": "single", "single_reason": "test"})
    cmd = [sys.executable, str(PROGRESS), "--step", "implement", *(where or ["--scratch", str(scratch)]),
           "init", "--anchor", anchor, "--base", anchor, *extra_args]
    return subprocess.run(cmd, input=payload, capture_output=True, text=True, env=env)


def check_round3_followups(h):
    """Round 3 follow-ups: names --hygiene error exit, hygiene-error evidence, skip flags, output
    confinement, layout-evidence, exact rows."""
    print("[Section 22] Round 3 follow-ups")
    with tempfile.TemporaryDirectory() as tmp:
        repo, base = make_repo(tmp, {"README.md": "# r\n", "src/parser.py": "x = 1\n",
                                     "tests/test_parser.py": BASE_PY, "tests/test_other.py": BASE_PY})
        bad = "0" * 39 + "1"

        # names --hygiene: an error exits 3, hits exit 1; plain names keeps exit 1 for an error.
        rc, out, err = run(repo, ["names", "--hygiene", "--base", bad, "--head", "HEAD"])
        h.test_result("names --hygiene with a bad base exits 3 and reports the error on stderr",
                      rc == 3 and out == "" and "bad-ref" in err, f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = run(repo, ["names", "--base", bad, "--head", "HEAD"])
        h.test_result("plain names with a bad base still exits 1", rc == 1, f"rc={rc} err={err!r}")

        # exact rows
        head = commit(repo, {"tests/test_parser_hardening.py": "def test_alpha_followups():\n    assert 1\n"}, "add")
        rc, out, err = run(repo, ["names", "--hygiene", "--base", base, "--head", "HEAD"])
        h.test_result("names --hygiene prints exactly the path row then the name row",
                      rc == 1 and out.splitlines() == [
                          "spec-blind path tests/test_parser_hardening.py",
                          "spec-blind line tests/test_parser_hardening.py:1 test test_alpha_followups"],
                      f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = run(repo, ["working-ids", "--base", base, "--head", head])
        h.test_result("working-ids prints exactly the added ids, sorted",
                      rc == 0 and out.splitlines() == ["tests/test_parser_hardening.py::test_alpha_followups"],
                      f"rc={rc} out={out!r} err={err!r}")

        # hygiene-json: hygiene-error first line, skip flags, confinement
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()
        (scratch / "round2-hygiene.txt").write_text("hygiene-error: names --hygiene exit 3\nerror: bad-ref x\n")
        (scratch / "round4-hygiene.txt").write_text("hygiene-error: names --hygiene exit 3\nerror: bad-ref x\n")
        out_path = Path(tmp) / "m.json"
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", str(out_path)])
        data = json.loads(out_path.read_text()) if out_path.exists() else {}
        h.test_result("hygiene-json treats a hygiene-error first line as null for hits and segment_only (both rounds)",
                      rc == 0 and all(data.get(k) is None for k in (
                          "round2_hygiene_hits", "round2_segment_only", "round4_hygiene_hits",
                          "round4_segment_only")), f"rc={rc} data={data!r} err={err!r}")

        (scratch / "round2-hygiene.txt").write_text("round2-hygiene: 2 hits\n")
        (scratch / "round4-hygiene.txt").write_text("tests/a.py::test_x_hardening\n")
        (scratch / "round4-candidates.md").write_text("candidates: 1  none: 0  unreported: 0\n")
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", str(out_path),
                                  "--skip-round2", "--skip-round4"])
        data = json.loads(out_path.read_text())
        h.test_result("hygiene-json --skip-round2 --skip-round4 ignore stale evidence files",
                      rc == 0 and all(data[k] is None for k in (
                          "round2_hygiene_hits", "round2_segment_only", "round4_hygiene_hits", "round4_segment_only",
                          "overlap_candidates", "overlap_none", "overlap_unreported")), f"data={data!r}")
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", str(out_path),
                                  "--skip-round4"])
        data = json.loads(out_path.read_text())
        h.test_result("hygiene-json --skip-round4 alone keeps the Round 2 count",
                      data["round2_hygiene_hits"] == 2 and data["round4_hygiene_hits"] is None
                      and data["overlap_candidates"] is None, f"data={data!r}")
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", str(out_path),
                                  "--skip-round2"])
        data = json.loads(out_path.read_text())
        h.test_result("hygiene-json --skip-round2 alone keeps the Round 4 count",
                      data["round2_hygiene_hits"] is None and data["round4_hygiene_hits"] == 1
                      and data["overlap_candidates"] == 1, f"data={data!r}")

        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "bogus", "--out", str(out_path)])
        h.test_result("hygiene-json rejects a --step other than implement or fix", rc == 2, f"rc={rc}")

        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", "/usr/m.json"])
        h.test_result("hygiene-json rejects an --out outside the repo, FLOW_DIR and the temp dir",
                      rc == 1 and "escapes" in err and not Path("/usr/m.json").exists(), f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", "../m.json"])
        h.test_result("hygiene-json rejects a relative --out with '..'", rc == 1, f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix", "--out", "rel-m.json"])
        h.test_result("hygiene-json accepts a relative --out inside the repo",
                      rc == 0 and (Path(repo) / "rel-m.json").exists(), f"rc={rc} err={err!r}")

        # layout-evidence
        ev = Path(tmp) / "scratch" / "evidence.txt"
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", str(ev)])
        h.test_result("layout-evidence without a unit record falls back to the base-less layout",
                      rc == 0 and out.strip() == str(ev)
                      and "tests/test_parser.py" in ev.read_text().splitlines()
                      and "  test_parse_basic" not in ev.read_text()
                      and (scratch / "layout-names-given.txt").read_text().strip() == "false",
                      f"rc={rc} out={out!r} err={err!r}")
        res = init_units(scratch, base)
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", str(ev)])
        text = ev.read_text()
        h.test_result("layout-evidence with a pinned layout_base_sha lists test names and writes true",
                      res.returncode == 0 and rc == 0 and text.splitlines()[:3] == [
                          "tests/test_parser.py", "  test_parse_basic", "  test_green_other"]
                      and (scratch / "layout-names-given.txt").read_text().strip() == "true",
                      f"init rc={res.returncode} rc={rc} text={text!r} err={err!r}")
        names_file = Path(tmp) / "given.txt"
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", str(ev), "--names-given-file", str(names_file)])
        h.test_result("layout-evidence --names-given-file overrides the default flag file",
                      rc == 0 and names_file.read_text().strip() == "true", f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", "/usr/ev.txt"])
        h.test_result("layout-evidence rejects an --out outside the allowed roots",
                      rc == 1 and "escapes" in err, f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", str(ev), "--names-given-file", "/usr/g.txt"])
        h.test_result("layout-evidence rejects a --names-given-file outside the allowed roots",
                      rc == 1 and "escapes" in err, f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch),
                                  "--impl", "src/parser.py", "--out", "rel-ev.txt"])
        h.test_result("layout-evidence accepts a relative --out inside the repo",
                      rc == 0 and (Path(repo) / "rel-ev.txt").exists(), f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--out", str(ev)])
        h.test_result("layout-evidence requires --flow-dir or --scratch", rc == 2, f"rc={rc}")

        # a pinned SHA that is not in the repo falls back to the base-less layout
        scratch2 = Path(tmp) / "scratch2"
        scratch2.mkdir()
        init_units(scratch2, bad)
        ev2 = scratch2 / "evidence.txt"
        rc, out, err = run(repo, ["layout-evidence", "--step", "implement", "--scratch", str(scratch2),
                                  "--impl", "src/parser.py", "--out", str(ev2)])
        h.test_result("layout-evidence with an unknown pinned SHA falls back to base-less and writes false",
                      rc == 0 and "  test_parse_basic" not in ev2.read_text()
                      and (scratch2 / "layout-names-given.txt").read_text().strip() == "false",
                      f"rc={rc} err={err!r}")

        # --flow-dir: flow dirs live under $HOME/.claude/flows, outside every default root
        home = Path(tmp) / "home"
        flow = home / ".claude" / "flows" / "repo" / "1-x-abc123"
        flow.mkdir(parents=True)
        (flow / "log.md").write_text("")
        env = {**os.environ, "HOME": str(home)}
        res = init_units(None, base, env=env, where=["--flow-dir", str(flow)])
        fev = flow / "layout-evidence.txt"
        proc = subprocess.run([sys.executable, str(CHECK), "layout-evidence", "--step", "implement",
                               "--flow-dir", str(flow), "--impl", "src/parser.py", "--out", str(fev)],
                              cwd=str(repo), capture_output=True, text=True, env=env)
        h.test_result("layout-evidence with --flow-dir resolves the base and writes under the flow dir",
                      res.returncode == 0 and proc.returncode == 0
                      and "  test_parse_basic" in fev.read_text()
                      and (flow / "layout-names-given.txt").read_text().strip() == "true",
                      f"init={res.stderr!r} rc={proc.returncode} err={proc.stderr!r}")

        # layout (no base) lists from the repo toplevel even from a subdirectory
        proc = subprocess.run([sys.executable, str(CHECK), "layout"], cwd=str(Path(repo) / "src"),
                              capture_output=True, text=True)
        h.test_result("layout without --base lists test paths from the toplevel when run in a subdirectory",
                      proc.returncode == 0 and "tests/test_parser.py" in proc.stdout.splitlines(),
                      f"rc={proc.returncode} out={proc.stdout!r}")

        # overlaps --out confinement (same rule)
        ids = Path(tmp) / "ids.txt"
        ids.write_text("tests/t.py::a\n")
        rc, out, err = run(repo, ["overlaps", "--added-ids", str(ids), "--out", "/usr/o.md"])
        h.test_result("overlaps rejects an --out outside the allowed roots", rc == 1 and "escapes" in err,
                      f"rc={rc} err={err!r}")
        rc, out, err = run(repo, ["overlaps", "--added-ids", str(ids), "--out", str(Path(tmp) / "o.md")])
        h.test_result("overlaps accepts an --out under the system temp dir", rc == 0, f"rc={rc} err={err!r}")

        # hygiene-json --flow-dir adds an allowed root
        rc, out, err = run(repo, ["hygiene-json", "--scratch", str(scratch), "--step", "fix",
                                  "--flow-dir", str(flow), "--out", str(flow / "test-hygiene-fix.json")])
        h.test_result("hygiene-json --flow-dir allows an --out under the flow dir",
                      rc == 0 and (flow / "test-hygiene-fix.json").exists(), f"rc={rc} err={err!r}")


# ---------------------------------------------------------------------------
# empty-suites: read-only empty-suite detection (#498) fixtures
# ---------------------------------------------------------------------------

EMPTY_SUITE_NONEMPTY = 'describe("n", () => { it("t", () => {}); });\n'

ACCEPT_1_TS = ('import { describe, it } from "vitest";\n'
               'describe("a", () => {\n'
               '});\n'
               'describe("b", () => {\n'
               '  it("keeps", () => {});\n'
               '});\n')

NESTED_TS = ('describe("outer", () => {\n'
             '  describe("inner", () => {});\n'
             '});\n'
             + EMPTY_SUITE_NONEMPTY)

# Fixture 2: the single-line and multi-line collapsed forms of an empty suite.
BIOME_FORMS = [
    'describe("x", () => {});',
    'describe.skip("x", () => {});',
    "describe.only('x', () => {});",
    "describe(`x`, () => {});",
    'describe("x", function () {});',
    'describe("x", async () => {});',
    'describe("x", () => { /* note */ });',
    'describe("x", () => {\n});',
]

# Fixture 6: suites that must never be removed.
KEPT_CASES = [
    'describe("k", () => { it("t", () => {}); });',
    'describe("k", () => { test("t", () => {}); });',
    'describe("k", () => { it.skip("t", () => {}); });',
    'describe("k", () => { it.todo("t"); });',
    'describe("k", () => { it.each([1, 2])("t %i", () => {}); });',
    'describe("k", () => { describe("n", () => { it("t", () => {}); }); });',
    'describe("k", () => { it("has { brace and `tick", () => {}); });',
    'describe("k", () => { for (const c of cases) { it(c, () => {}); } });',
    'describe("k", () => { cases.forEach((c) => it(c, () => {})); });',
]

# Fixture 7: suites that are kept and reported as kept-suspicious (the last one is unparseable).
SUSPICIOUS_CASES = [
    'describe("k", () => { for (const c of cases) { register(c); } });',
    'describe("k", () => { cases.forEach((c) => register(c)); });',
    'describe.each([[1]])("k %i", () => {});',
    'describe("k", () => { const helper = 1; });',
    'describe("k", () => { registerSuite(); });',
    'describe("k", () => { if (x) {',
]


def run_empty_suites(root, rels):
    """Run `empty-suites --root <root> <rels...>` with cwd=root. Returns (rc, findings, stdout, stderr).
    findings is the parsed top-level `findings` list, or None when stdout is not that JSON object."""
    result = subprocess.run([sys.executable, str(CHECK), "empty-suites", "--root", str(root), *rels],
                            cwd=str(root), capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        findings = None
    return result.returncode, findings, result.stdout, result.stderr


def of_kind(findings, kind):
    return [f for f in (findings or []) if f.get("kind") == kind]


def delete_spans(path, findings):
    """Delete each finding's line..end_line span from path (bottom-up, so earlier line numbers hold)."""
    lines = Path(path).read_text().splitlines(keepends=True)
    for f in sorted(findings, key=lambda item: item["line"], reverse=True):
        del lines[f["line"] - 1:f["end_line"]]
    Path(path).write_text("".join(lines))


def tree_snapshot(root):
    """Every directory and file under root, with file bytes, for read-only checks."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            snap[os.path.relpath(os.path.join(dirpath, name), root) + "/"] = None
        for name in filenames:
            full = os.path.join(dirpath, name)
            snap[os.path.relpath(full, root)] = Path(full).read_bytes()
    return snap


def call_count(text):
    """Count it(/test( call sites at the start of a line, including it.each-style modifiers."""
    return len(re.findall(r"(?m)^\s*(?:it|test)(?:\.\w+)?\(", text))


def check_empty_suites(h):
    """empty-suites: read-only JS/TS, pytest and Go detection, run as a subprocess against temp trees."""
    print("[Section empty-suites] read-only empty-suite detection")

    # Fixture 1: the deleted-suite case, then the same tree after the reported span is removed.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/a.test.ts": ACCEPT_1_TS})
        rc, findings, out, err = run_empty_suites(root, ["src/a.test.ts"])
        one = findings is not None and len(findings) == 1
        f = findings[0] if one else {}
        span_ok = (rc == 1 and one and f.get("kind") == "empty-suite" and f.get("name") == "a"
                   and f.get("action") == "remove" and f.get("stack") == "js"
                   and f.get("line") == 2 and f.get("end_line") == 3)
        h.test_result("empty-suites fixture 1: the emptied suite is one remove finding at lines 2-3, exit 1",
                      span_ok, f"rc={rc} findings={findings!r} err={err!r}")
        keys_ok = (one and f.get("file") == "src/a.test.ts" and isinstance(f.get("imported_by"), list)
                   and set(f) >= {"file", "line", "end_line", "stack", "kind", "name", "action",
                                  "reason", "imported_by", "text"}
                   and 'describe("a"' in str(f.get("text")))
        h.test_result("empty-suites fixture 1: a finding carries the documented keys, file relative to root",
                      keys_ok, f"finding={f!r}")
        if span_ok:
            delete_spans(root / "src/a.test.ts", findings)
            rc2, findings2, out2, err2 = run_empty_suites(root, ["src/a.test.ts"])
            h.test_result("empty-suites fixture 1: after deleting line..end_line the helper exits 0 with no findings",
                          rc2 == 0 and findings2 == [], f"rc={rc2} findings={findings2!r} err={err2!r}")
        else:
            h.test_result("empty-suites fixture 1: after deleting line..end_line the helper exits 0 with no findings",
                          False, "skipped: the reported span was not as specified")

    # Fixture 2: every collapsed form is one remove finding next to a non-empty suite.
    for form in BIOME_FORMS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_files(root, {"src/x.test.ts": form + "\n" + EMPTY_SUITE_NONEMPTY})
            rc, findings, out, err = run_empty_suites(root, ["src/x.test.ts"])
            es = of_kind(findings, "empty-suite")
            ok = (rc == 1 and findings is not None and len(findings) == 1 and len(es) == 1
                  and es[0].get("name") == "x" and es[0].get("action") == "remove")
            h.test_result(f"empty-suites fixture 2: collapsed empty suite is one remove finding: {form.splitlines()[0]}",
                          ok, f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 3: nested empties report only the outermost suite; the result is stable and idempotent.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/n.test.ts": NESTED_TS})
        rc, findings, out, err = run_empty_suites(root, ["src/n.test.ts"])
        one = findings is not None and len(findings) == 1 and findings[0].get("name") == "outer"
        h.test_result("empty-suites fixture 3: nested empty suites yield exactly one finding, for outer",
                      rc == 1 and one, f"rc={rc} findings={findings!r} err={err!r}")
        _, _, out_again, _ = run_empty_suites(root, ["src/n.test.ts"])
        h.test_result("empty-suites fixture 3: two runs on the same input give byte-identical stdout",
                      out == out_again, f"first={out!r} second={out_again!r}")
        if one:
            delete_spans(root / "src/n.test.ts", findings)
            rc2, findings2, out2, err2 = run_empty_suites(root, ["src/n.test.ts"])
            h.test_result("empty-suites fixture 3: after deleting outer's span the helper exits 0",
                          rc2 == 0 and findings2 == [], f"rc={rc2} findings={findings2!r} err={err2!r}")
        else:
            h.test_result("empty-suites fixture 3: after deleting outer's span the helper exits 0",
                          False, "skipped: no single outer finding")

    # Fixture 5: the helper is read-only, and removing the reported spans keeps every it/test call.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/a.test.ts": ACCEPT_1_TS, "src/n.test.ts": NESTED_TS})
        before = tree_snapshot(root)
        exit_codes = []
        unchanged = True
        for rels in (["src/a.test.ts"], ["src/n.test.ts"], ["src/a.test.ts", "src/n.test.ts"]):
            rc, _, _, _ = run_empty_suites(root, rels)
            exit_codes.append(rc)
            unchanged = unchanged and tree_snapshot(root) == before
        h.test_result("empty-suites fixture 5: the helper writes nothing under --root, including on exit-1 runs",
                      exit_codes == [1, 1, 1] and unchanged, f"exit_codes={exit_codes}")
    nested_own_line_ts = ('describe("outer", () => {\n'
                          '  describe("inner", () => {});\n'
                          '});\n'
                          'describe("n", () => {\n'
                          '  it("t", () => {});\n'
                          '});\n')
    for rel, text in (("src/a.test.ts", ACCEPT_1_TS), ("src/n.test.ts", nested_own_line_ts)):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_files(root, {rel: text})
            rc, findings, out, err = run_empty_suites(root, [rel])
            es = of_kind(findings, "empty-suite")
            if not es:
                h.test_result(f"empty-suites fixture 5: removing empty-suite spans from {rel} keeps every it/test call",
                              False, f"no empty-suite finding to remove: rc={rc} findings={findings!r}")
                continue
            before_calls = call_count(text)
            delete_spans(root / rel, es)
            after_calls = call_count((root / rel).read_text())
            h.test_result(f"empty-suites fixture 5: removing empty-suite spans from {rel} keeps every it/test call",
                          before_calls >= 1 and before_calls == after_calls,
                          f"before={before_calls} after={after_calls}")

    # Fixture 6: kept suites are never removed; the helper exits 0 when the suite is the only content.
    for case in KEPT_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_files(root, {"src/k.test.ts": case + "\n"})
            rc, findings, out, err = run_empty_suites(root, ["src/k.test.ts"])
            acted = [f for f in (findings or []) if f.get("action") in ("remove", "delete-file", "stop")]
            h.test_result(f"empty-suites fixture 6: suite is kept, never removed, exit 0: {case}",
                          findings is not None and rc == 0 and not acted,
                          f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 7: suspicious suites are reported (kept-suspicious, report, exit 0), never removed.
    for case in SUSPICIOUS_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_files(root, {"src/s.test.ts": EMPTY_SUITE_NONEMPTY + case + "\n"})
            rc, findings, out, err = run_empty_suites(root, ["src/s.test.ts"])
            ks = of_kind(findings, "kept-suspicious")
            acted = [f for f in (findings or []) if f.get("action") in ("remove", "delete-file", "stop")]
            h.test_result(f"empty-suites fixture 7: suspicious suite is kept-suspicious, report, exit 0: {case}",
                          findings is not None and rc == 0 and len(ks) >= 1
                          and all(f.get("action") == "report" for f in ks) and not acted,
                          f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 8: a suite holding only a hook and a comment is empty, and the hook goes with it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hook_suite = 'describe("h", () => {\n  beforeEach(() => {});\n  // nothing here\n});\n'
        write_files(root, {"src/h.test.ts": hook_suite + EMPTY_SUITE_NONEMPTY})
        rc, findings, out, err = run_empty_suites(root, ["src/h.test.ts"])
        es = of_kind(findings, "empty-suite")
        h.test_result("empty-suites fixture 8: a hook-and-comment suite is one remove finding whose text holds beforeEach",
                      findings is not None and len(findings) == 1 and len(es) == 1
                      and es[0].get("name") == "h" and es[0].get("action") == "remove"
                      and "beforeEach" in str(es[0].get("text")),
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 9: an emptied JS file with no importer is delete-file, and no suite finding is added.
    emptied = 'import { describe } from "vitest";\ndescribe("x", () => {});\n'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/only.test.ts": emptied})
        rc, findings, out, err = run_empty_suites(root, ["src/only.test.ts"])
        f = findings[0] if findings and len(findings) == 1 else {}
        h.test_result("empty-suites fixture 9: an emptied file with no importer is one delete-file finding",
                      rc == 1 and findings is not None and len(findings) == 1
                      and f.get("kind") == "empty-file" and f.get("action") == "delete-file"
                      and f.get("imported_by") == [],
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 10: the same emptied file, imported by another test file, is a stop.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/only.test.ts": emptied,
                           "src/other.test.ts": 'import { helper } from "./only.test";\n'})
        rc, findings, out, err = run_empty_suites(root, ["src/only.test.ts"])
        f = findings[0] if findings and len(findings) == 1 else {}
        h.test_result("empty-suites fixture 10: an emptied file imported elsewhere is empty-file-unsafe, stop, exit 1",
                      rc == 1 and findings is not None and len(findings) == 1
                      and f.get("kind") == "empty-file-unsafe" and f.get("action") == "stop"
                      and f.get("imported_by") == ["src/other.test.ts"],
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 11: an emptied file that still holds top-level code is empty-file-unsafe, stop.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/fx.test.ts": 'import { describe } from "vitest";\nexport const fixture = 1;\n'
                                             'describe("x", () => {});\n'})
        rc, findings, out, err = run_empty_suites(root, ["src/fx.test.ts"])
        unsafe = of_kind(findings, "empty-file-unsafe")
        h.test_result("empty-suites fixture 11: an emptied file with top-level code is empty-file-unsafe, stop",
                      rc == 1 and len(unsafe) == 1 and unsafe[0].get("action") == "stop",
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Edge (Step 2 precedence): a kept-suspicious suite in an otherwise-emptied file is reported as is.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"src/sus.test.ts": 'import { describe } from "vitest";\n'
                                              'describe("x", () => { register(); });\n'})
        rc, findings, out, err = run_empty_suites(root, ["src/sus.test.ts"])
        kinds = {f.get("kind") for f in (findings or [])}
        h.test_result("empty-suites: a kept-suspicious suite keeps its file out of empty-file and empty-file-unsafe",
                      rc == 0 and findings is not None and "kept-suspicious" in kinds
                      and not kinds & {"empty-file", "empty-file-unsafe"},
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Edge (Step 4 stack table): .tsx and .mjs are JS, and their emptied suites are remove findings.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        emptied_suite = 'describe("t", () => {});\n' + EMPTY_SUITE_NONEMPTY
        write_files(root, {"src/c.tsx": emptied_suite, "src/d.mjs": emptied_suite})
        rc, findings, out, err = run_empty_suites(root, ["src/c.tsx", "src/d.mjs"])
        es = of_kind(findings, "empty-suite")
        h.test_result("empty-suites: .tsx and .mjs files are stack js and report their empty suites",
                      rc == 1 and len(es) == 2 and all(f.get("stack") == "js" for f in es),
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 12: a Test* class with only a docstring and pass is empty-class, report only.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"tests/test_p.py": 'class TestEmpty:\n    """doc"""\n    pass\n'})
        rc, findings, out, err = run_empty_suites(root, ["tests/test_p.py"])
        ec = of_kind(findings, "empty-class")
        f = ec[0] if len(ec) == 1 else {}
        h.test_result("empty-suites fixture 12: an empty Test* class is one empty-class report, exit 0",
                      rc == 0 and findings is not None and len(findings) == 1 and f.get("name") == "TestEmpty"
                      and f.get("stack") == "python" and f.get("action") == "report",
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 13: classes that are not empty Test* classes produce no finding.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"tests/test_neg.py": 'import unittest\n\n\n'
                                                'class TestFull:\n    def test_a(self):\n        pass\n\n\n'
                                                'class TestCaseLike(unittest.TestCase):\n    pass\n\n\n'
                                                'class Helper:\n    pass\n'})
        rc, findings, out, err = run_empty_suites(root, ["tests/test_neg.py"])
        named = {"TestFull", "TestCaseLike", "Helper"}
        h.test_result("empty-suites fixture 13: no finding for TestFull, a TestCase subclass, or Helper",
                      findings is not None and not [f for f in findings if f.get("name") in named],
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Edge (Step 3): a Python file that cannot be parsed is one kept-suspicious report.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"tests/test_broken.py": 'def test_x(:\n    pass\n'})
        rc, findings, out, err = run_empty_suites(root, ["tests/test_broken.py"])
        ks = of_kind(findings, "kept-suspicious")
        h.test_result("empty-suites: an unparseable Python test file is one kept-suspicious report, exit 0",
                      rc == 0 and len(ks) == 1 and ks[0].get("action") == "report" and ks[0].get("stack") == "python",
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 14: a _test.go file with only a helper is empty-go-test-file, report only.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"pkg/x_test.go": 'package pkg\n\nfunc helper() int { return 1 }\n'})
        rc, findings, out, err = run_empty_suites(root, ["pkg/x_test.go"])
        f = findings[0] if findings and len(findings) == 1 else {}
        h.test_result("empty-suites fixture 14: a _test.go file with no Test/Benchmark/Fuzz/Example is report-only",
                      rc == 0 and f.get("kind") == "empty-go-test-file" and f.get("stack") == "go"
                      and f.get("action") == "report",
                      f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 15: _test.go files with a Test, TestMain or Benchmark function produce no finding.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {
            "pkg/a_test.go": 'package pkg\n\nimport "testing"\n\nfunc TestA(t *testing.T) {}\n',
            "pkg/m_test.go": 'package pkg\n\nimport "testing"\n\nfunc TestMain(m *testing.M) {}\n',
            "pkg/b_test.go": 'package pkg\n\nimport "testing"\n\nfunc BenchmarkA(b *testing.B) {}\n',
        })
        rc, findings, out, err = run_empty_suites(root, ["pkg/a_test.go", "pkg/m_test.go", "pkg/b_test.go"])
        h.test_result("empty-suites fixture 15: Test, TestMain and Benchmark files yield no finding",
                      rc == 0 and findings == [], f"rc={rc} findings={findings!r} err={err!r}")

    # Fixture 16: --help exits 0.
    help_run = subprocess.run([sys.executable, str(CHECK), "empty-suites", "--help"],
                              capture_output=True, text=True)
    h.test_result("empty-suites fixture 16: --help exits 0", help_run.returncode == 0,
                  f"rc={help_run.returncode} err={help_run.stderr!r}")

    # Fixture 17: a FILE outside --root exits 2 with a message on stderr.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        outside = Path(tmp) / "outside.test.ts"
        outside.write_text(ACCEPT_1_TS)
        rc, findings, out, err = run_empty_suites(root, [str(outside)])
        h.test_result("empty-suites fixture 17: a FILE outside --root exits 2 with a stderr message",
                      rc == 2 and err.strip() != "", f"rc={rc} err={err!r}")

    # Fixture 18: a Markdown argument produces no finding and exits 0.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_files(root, {"docs/notes.md": 'describe("x", () => {});\n'})
        rc, findings, out, err = run_empty_suites(root, ["docs/notes.md"])
        h.test_result("empty-suites fixture 18: a .md argument produces no finding and exits 0",
                      rc == 0 and findings == [], f"rc={rc} findings={findings!r} err={err!r}")


if __name__ == "__main__":
    main()
