#!/usr/bin/env python3
"""
Black-box tests for scripts/heal-check.py (#380): signature, compare, complexity.

Driven only through the CLI (subprocess). `signature` runs against the real captured runner
output in tests/fixtures/heal/ plus synthetic infra/garbage windows. `complexity` runs against
tiny throwaway repos.

The formatting-only `skipped` case uses pytest-format counts that no runner in this repo emits
for source-level ids; it is synthetic and labelled as such below.

Run with: python3 tests/test_heal_check.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

HEAL_CHECK = REPO_ROOT / "scripts" / "heal-check.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "heal"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false",
          "-c", "core.hooksPath=/dev/null"]


def run_cli(*args):
    result = subprocess.run([sys.executable, str(HEAL_CHECK), *args], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    return result.returncode, data, result.stdout + result.stderr


def run_git(repo, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), capture_output=True, text=True)
    return result.stdout.strip()


def signature_of(tmp, text, name="out.txt"):
    path = Path(tmp) / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return run_cli("signature", "--output-file", str(path))


def fixture_text(name):
    return (FIXTURES / name).read_text()


def write_json(tmp, name, value):
    path = Path(tmp) / name
    path.write_text(json.dumps(value))
    return str(path)


def never_healable(data):
    """True when a signature result can never feed a heal (not lint/test)."""
    return data is None or data.get("class") not in ("lint", "test")


def make_repo(tmp, before_files, after_files):
    """Return (repo, sha_before, sha_after) where after_files overwrite/add on top of before_files."""
    repo = Path(tmp) / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    for files, msg in ((before_files, "before"), (after_files, "after")):
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", msg)
        if msg == "before":
            sha_before = run_git(repo, "rev-parse", "HEAD")
    return repo, sha_before, run_git(repo, "rev-parse", "HEAD")


def complexity(repo, base, head, failing_ids, before, after, healer_review=False):
    """before/after are dicts or None (literal 'null'). Returns (rc, data, raw)."""
    with tempfile.TemporaryDirectory() as tmp:
        ids_file = write_json(tmp, "ids.json", failing_ids)
        args = ["complexity", "--repo", str(repo), "--base", base, "--head", head, "--failing-ids", ids_file]
        for flag, counts, name in (("--before-counts", before, "before.json"), ("--after-counts", after, "after.json")):
            args += [flag, "null" if counts is None else write_json(tmp, name, counts)]
        if healer_review:
            args += ["--healer-review", "required"]
        return run_cli(*args)


def reasons_of(data):
    return [str(r).split(":", 1)[0].strip() for r in (data or {}).get("reasons", [])]


def test_signature(h):
    print("[Section 1] signature: real captures")
    lint_text = fixture_text("just-check-ruff-f401.txt")
    with tempfile.TemporaryDirectory() as tmp:
        rc, data, raw = signature_of(tmp, lint_text)
        h.test_result("lint capture: class is 'lint'", rc == 0 and data and data["class"] == "lint", raw[:300])
        h.test_result("lint capture: counts is null (just check stops at lint)", data and data.get("counts") is None, raw[:300])
        h.test_result("lint capture: ids name the rule and file",
                      data and any("F401" in i and "_seed_heal.py" in i for i in data.get("ids", [])), raw[:300])
        h.test_result("lint capture: signature is a sha256 hex digest",
                      data and re.fullmatch(r"[0-9a-f]{64}", data.get("signature", "")) is not None, raw[:300])
        sig_a = data["signature"] if data else None

        noisy_a = "2026-10-29T10:00:01Z starting check (took 1.2s)\n" + lint_text + "\nFinished in 3.41s tmp=/tmp/tmpabc123\n"
        noisy_b = "2026-11-02T23:59:59Z starting check (took 9.8s)\n" + lint_text + "\nFinished in 0.07s tmp=/tmp/tmpxyz789\n"
        _, da, _ = signature_of(tmp, noisy_a, "a.txt")
        _, db, _ = signature_of(tmp, noisy_b, "b.txt")
        h.test_result("lint capture: signature is stable across differing timestamps, durations and temp paths",
                      da and db and da["signature"] == db["signature"] == sig_a, f"{da} {db}")

        other = lint_text.replace("F401", "F841")
        _, do, _ = signature_of(tmp, other, "o.txt")
        h.test_result("lint capture: a different rule id gives a different signature",
                      do and do["signature"] != sig_a, f"{do}")
        other = lint_text.replace("_seed_heal.py", "_other_file.py")
        _, do, _ = signature_of(tmp, other, "p.txt")
        h.test_result("lint capture: a different file gives a different signature", do and do["signature"] != sig_a, f"{do}")

    with tempfile.TemporaryDirectory() as tmp:
        rc, data, raw = signature_of(tmp, fixture_text("run-all-failing-suite.txt"))
        h.test_result("run_all capture: class is 'test'", rc == 0 and data and data["class"] == "test", raw[:300])
        h.test_result("run_all capture: ids include the failing suite name",
                      data and any("test_bad_suite.py" in i for i in data.get("ids", [])), raw[:300])
        h.test_result("run_all capture: ids exclude passing suites",
                      data and not any("test_ok_one" in i or "test_ok_two" in i for i in data.get("ids", [])), raw[:300])
        h.test_result("run_all capture: counts are {run: 3, failed: 1}",
                      data and data.get("counts") == {"run": 3, "failed": 1}, raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        rc, data, raw = signature_of(tmp, fixture_text("pytest-failing-node.txt"))
        h.test_result("pytest capture: class is 'test'", rc == 0 and data and data["class"] == "test", raw[:300])
        h.test_result("pytest capture: ids carry the failing node id",
                      data and any("test_sample.py::test_b" in i for i in data.get("ids", [])), raw[:300])
        h.test_result("pytest capture: counts run = passed + failed + skipped = 3, failed = 1",
                      data and data.get("counts") == {"run": 3, "failed": 1}, raw[:300])

    print()
    print("[Section 2] signature: infra precedence and unknown")
    run_all = fixture_text("run-all-failing-suite.txt")
    pytest_out = fixture_text("pytest-failing-node.txt")
    infra_lines = {
        "DB_NAME unset": "ERROR: DB_NAME unset\n",
        "command not found": "zsh: command not found: psql\n",
        "lock marker": "another cleanup is validating main\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        for label, marker in infra_lines.items():
            rc, data, raw = signature_of(tmp, marker, "i.txt")
            h.test_result(f"infra marker alone ({label}): class is 'infra'", data and data["class"] == "infra", raw[:300])
            rc, data, raw = signature_of(tmp, run_all + marker, "i2.txt")
            h.test_result(f"infra marker with run_all test markers ({label}): infra wins", data and data["class"] == "infra", raw[:300])
            rc, data, raw = signature_of(tmp, pytest_out + marker, "i3.txt")
            h.test_result(f"infra marker with pytest test markers ({label}): infra wins", data and data["class"] == "infra", raw[:300])

        no_summary = run_all.split("AGGREGATE SUMMARY")[0]
        rc, data, raw = signature_of(tmp, no_summary, "u1.txt")
        h.test_result("a window with no recognised summary line is 'unknown'", data and data["class"] == "unknown", raw[:300])
        rc, data, raw = signature_of(tmp, "", "u2.txt")
        h.test_result("empty input is 'unknown'", data and data["class"] == "unknown", raw[:300])
        rc, data, raw = signature_of(tmp, "just some words\nnothing parseable here\n", "u3.txt")
        h.test_result("garbage text is 'unknown'", data and data["class"] == "unknown", raw[:300])
        rc, data, raw = signature_of(tmp, bytes(range(256)) * 4, "u4.txt")
        h.test_result("binary garbage never classifies as lint or test", never_healable(data), raw[:300])
        rc, data, raw = run_cli("signature", "--output-file", str(Path(tmp) / "does-not-exist.txt"))
        h.test_result("a missing output file never classifies as lint or test", never_healable(data), raw[:300])


def test_compare(h):
    print()
    print("[Section 3] compare")
    with tempfile.TemporaryDirectory() as tmp:
        _, lint, _ = signature_of(tmp, fixture_text("just-check-ruff-f401.txt"), "l.txt")
        _, lint2, _ = signature_of(tmp, "10:00:01 noise\n" + fixture_text("just-check-ruff-f401.txt"), "l2.txt")
        _, test, _ = signature_of(tmp, fixture_text("run-all-failing-suite.txt"), "t.txt")
        _, infra1, _ = signature_of(tmp, "ERROR: DB_NAME unset\n", "i1.txt")
        _, infra2, _ = signature_of(tmp, "ERROR: DB_NAME unset\n", "i2.txt")
        _, unk, _ = signature_of(tmp, "nothing\n", "u.txt")
        files = {name: write_json(tmp, f"{name}.json", value) for name, value in (
            ("lint", lint), ("lint2", lint2), ("test", test), ("infra1", infra1), ("infra2", infra2), ("unk", unk))}

        def recurs(a, b):
            rc, data, raw = run_cli("compare", "--first", files[a], "--second", files[b])
            return data.get("recurs") if data else None

        h.test_result("equal lint signatures recur", recurs("lint", "lint2") is True)
        h.test_result("equal test signatures recur", recurs("test", "test") is True)
        h.test_result("differing signatures do not recur", recurs("lint", "test") is False)
        h.test_result("identical infra results do not recur", recurs("infra1", "infra2") is False)
        h.test_result("unknown results do not recur", recurs("unk", "unk") is False)
        h.test_result("infra versus lint does not recur", recurs("infra1", "lint") is False)

        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json")
        rc, data, raw = run_cli("compare", "--first", str(bad), "--second", files["lint"])
        h.test_result("an unparseable input fails closed (never recurs: true)", not (data and data.get("recurs") is True), raw[:300])
        rc, data, raw = run_cli("compare", "--first", str(Path(tmp) / "missing.json"), "--second", files["lint"])
        h.test_result("a missing input fails closed (never recurs: true)", not (data and data.get("recurs") is True), raw[:300])


def test_complexity(h):
    print()
    print("[Section 4] complexity")
    SEED = {"scripts/_seed_heal.py": "import os\n\n\ndef f():\n    return 1\n"}
    SEED_FIXED = {"scripts/_seed_heal.py": "\n\ndef f():\n    return 1\n"}

    with tempfile.TemporaryDirectory() as tmp:
        lint_sig = signature_of(tmp, fixture_text("just-check-ruff-f401.txt"))[1]
        repo, b, a = make_repo(tmp, SEED, SEED_FIXED)
        rc, data, raw = complexity(repo, b, a, lint_sig["ids"], None, None)
        h.test_result("real lint capture (null counts): review is 'required' with 'counts-missing'",
                      data and data["review"] == "required" and "counts-missing" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        suite_sig = signature_of(tmp, fixture_text("run-all-failing-suite.txt"))[1]
        repo, b, a = make_repo(tmp, {"src/app.py": "x = 1\n"}, {"src/app.py": "x = 2\n"})
        rc, data, raw = complexity(repo, b, a, suite_sig["ids"], suite_sig["counts"], {"run": 3, "failed": 0})
        h.test_result("real run_all capture + source fix: 'required' with 'file-not-in-failing-ids'",
                      data and data["review"] == "required" and "file-not-in-failing-ids" in reasons_of(data), raw[:300])

    # Synthetic-only path in this repo: pytest-format counts with a source-level id.
    named = {"test_sample.py": "x=1\n\n\ndef test_b():\n    assert x == 1\n"}
    named_fmt = {"test_sample.py": "x = 1\n\n\ndef test_b():\n    assert x == 1\n"}
    ids = ["test_sample.py::test_b"]
    before = {"run": 3, "failed": 1}
    after = {"run": 3, "failed": 0}
    with tempfile.TemporaryDirectory() as tmp:
        repo, b, a = make_repo(tmp, named, named_fmt)
        rc, data, raw = complexity(repo, b, a, ids, before, after)
        h.test_result("SYNTHETIC: formatting-only fix in a named file with pytest-format counts is 'skipped'",
                      rc == 0 and data and data["review"] == "skipped", raw[:300])
        rc, data, raw = complexity(repo, b, a, ids, before, after, healer_review=True)
        h.test_result("--healer-review required forces 'required' on an otherwise-skipped heal",
                      data and data["review"] == "required" and "healer-raised" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        repo, b, a = make_repo(tmp, named, {**named_fmt, "other.py": "y = 1\n"})
        rc, data, raw = complexity(repo, b, a, ids, before, after)
        h.test_result("a touched unnamed file gives 'required' with 'file-not-in-failing-ids'",
                      data and data["review"] == "required" and "file-not-in-failing-ids" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        branchy = {"test_sample.py": "x = 1\n\n\ndef test_b():\n    if x:\n        assert x == 1\n"}
        repo, b, a = make_repo(tmp, named, branchy)
        rc, data, raw = complexity(repo, b, a, ids, before, after)
        h.test_result("an added 'if' gives 'required' with 'def-or-branch-changed'",
                      data and data["review"] == "required" and "def-or-branch-changed" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        newdef = {"test_sample.py": "x = 1\n\n\ndef helper():\n    return 1\n\n\ndef test_b():\n    assert x == 1\n"}
        repo, b, a = make_repo(tmp, named, newdef)
        rc, data, raw = complexity(repo, b, a, ids, before, after)
        h.test_result("an added 'def' gives 'required' with 'def-or-branch-changed'",
                      data and data["review"] == "required" and "def-or-branch-changed" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        repo, b, a = make_repo(tmp, {"commands/x.md": "a\n"}, {"commands/x.md": "b\n"})
        rc, data, raw = complexity(repo, b, a, ["commands/x.md"], before, after)
        h.test_result("an edit under commands/ gives 'required' with 'protected-dir-edit'",
                      data and data["review"] == "required" and "protected-dir-edit" in reasons_of(data), raw[:300])

    with tempfile.TemporaryDirectory() as tmp:
        repo, b, a = make_repo(tmp, named, named_fmt)
        cases = (
            ("after.run < before.run", before, {"run": 2, "failed": 0}, "fewer-tests-ran"),
            ("after.failed != 0", before, {"run": 3, "failed": 1}, "after-failed-nonzero"),
            ("before.failed != number of failing ids", {"run": 3, "failed": 2}, after, "before-failed-mismatch"),
            ("missing before counts", None, after, "counts-missing"),
            ("missing after counts", before, None, "counts-missing"),
        )
        for label, bc, ac, reason in cases:
            rc, data, raw = complexity(repo, b, a, ids, bc, ac)
            h.test_result(f"{label}: 'required' with '{reason}'",
                          data and data["review"] == "required" and reason in reasons_of(data), raw[:300])

    print()
    print("[Section 5] complexity: fail closed and monotonic")
    with tempfile.TemporaryDirectory() as tmp:
        repo, b, a = make_repo(tmp, named, named_fmt)
        rc, data, raw = complexity(repo, b, "f" * 40, ids, before, after)
        h.test_result("an unparseable diff (bad ref) is never 'skipped'", not (data and data.get("review") == "skipped"), raw[:300])
        h.test_result("an unparseable diff (bad ref) is 'required' with 'diff-unparseable' when it reports",
                      data is None or (data["review"] == "required" and "diff-unparseable" in reasons_of(data)), raw[:300])
        bad_ids = Path(tmp) / "bad-ids.json"
        bad_ids.write_text("{{{ not json")
        rc, data, raw = run_cli("complexity", "--repo", str(repo), "--base", b, "--head", a, "--failing-ids", str(bad_ids),
                                "--before-counts", "null", "--after-counts", "null")
        h.test_result("unparseable failing-ids input is never 'skipped'", not (data and data.get("review") == "skipped"), raw[:300])

        # Nothing lowers a 'required': every required scenario above stays required with --healer-review too.
        repo2_ids = ["test_sample.py::test_b"]
        rc, data, raw = complexity(repo, b, a, repo2_ids, None, None, healer_review=True)
        h.test_result("missing counts plus --healer-review stays 'required'", data and data["review"] == "required", raw[:300])


def main():
    h = Harness("HEAL-CHECK TEST SUITE")
    test_signature(h)
    test_compare(h)
    test_complexity(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
