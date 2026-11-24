#!/usr/bin/env python3
"""
Implement-step test-change checks for scripts/flow-verify-receipt.py (#511).

The implement step may name human-approved existing-test edits (TEST_EDIT) and removals
(TEST_REMOVAL) in its receipt. Each line is `<id> <path> answers/implement-<SEQ>.json`, and the
receipt carries TEST_EDIT_COMMIT when the hatch applied a change. The verifier fails closed on
malformed lines, whitespace in item ids and paths it cannot decode.

These cases run the real verifier as a subprocess against a temporary git repository and a real
FLOW_DIR tree built with the shared assess fixtures. Ledger records are appended through
scripts/flow-ledger.py.

Covered here:
- an implement receipt with TEST_EDIT none passes (baseline for the fixture)
- a TEST_EDIT or TEST_REMOVAL line with the wrong number of tokens fails closed
- an item id containing whitespace is rejected and never mis-parsed into a valid approval
- a non-UTF-8 path in the receipt does not crash the verifier (no traceback)

Not covered: a fully approved implement edit or removal. The approval commit's required recap
form is not documented in the repository's ADRs or prompts, so the approval fixture is not built
here. Reuse across attempts, the sha256, supersede and blob-id rejections are untested.

Run with: python3 tests/test_flow_verify_receipt_implement_hatch.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import Harness, REPO_ROOT  # noqa: E402
from _assess_fixtures import make_flow, make_home  # noqa: E402

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]
SINCE = "2026-10-01T00:00:00Z"
LOG_LINE = "2026-10-27T10:00:00Z implement ok"


def git(cwd, *args):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout.strip()


def build_fixture(td):
    """Temp repo: base commit, then one ordinary unit commit. Returns a dict with repo, base, head, flow, env."""
    repo = Path(td) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "repo-cache.json").write_text(json.dumps({"commands": {"check": "true"}}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "unit")
    head = git(repo, "rev-parse", "HEAD")

    home = make_home(td)
    flow = make_flow(td, implement={"outstanding": []}, log=[LOG_LINE])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    env["FLOW_VERIFY_GIT_TIMEOUT_SECS"] = "60"
    return {"repo": str(repo), "base": base, "head": head, "flow": flow, "env": env}


def write_receipt(fx, extra_lines, attempt=1):
    """Write steps/03-implement.md with the given extra receipt lines. Returns its path."""
    body = (
        "ROUND 1\n"
        "CHECK: true\n"
        "CHECK_EXIT: 0\n"
        "CHECK_SOURCE: repo-cache\n"
        f"CHECK_SHA: {fx['head']}\n"
        "SPLIT_BASIS: parallel\n"
        "MIN_UNITS: 1\n"
        "PRIOR_SPLIT_BASIS: none\n"
        "UNITS: 1\n"
        + "".join(f"{line}\n" for line in extra_lines)
        + "OUTSTANDING: none\n"
        f"ATTEMPT: {attempt}\n"
        "Decision: OK\n"
        "<!-- step-end -->\n"
    )
    path = fx["flow"] / "steps" / "03-implement.md"
    path.write_bytes(body.encode("utf-8") if isinstance(body, str) else body)
    return path


def run_implement(fx, receipt_path):
    """Run the verifier's implement step on the receipt. Returns (rc, combined stdout+stderr, stderr)."""
    cmd = [sys.executable, str(VERIFIER), "--step", "implement", "--receipt", str(receipt_path),
           "--since", SINCE, "--repo", fx["repo"], "--branch", "HEAD", "--base", fx["base"],
           "--flow-dir", str(fx["flow"])]
    result = subprocess.run(cmd, capture_output=True, text=True, env=fx["env"])
    return result.returncode, result.stdout + result.stderr, result.stderr


def check_zero_unit_all_declined(h):
    """#526 / #511 regression: a zero-units run whose Test changes were all declined stays an OK receipt.

    The declined item is reported as OUTSTANDING (test-edit-declined: <path>) and the verifier raises
    no split-basis or UNITS error for the zero-units basis. Plan cross-checks (resolved plan) are not
    asserted here: how the plan is resolved is not visible from the verifier's CLI.
    """
    print("[Zero-unit run, every Test changes item declined (#526)]")
    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        body = (
            "ROUND 1\nCHECK: true\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            f"CHECK_SHA: {fx['head']}\nSPLIT_BASIS: zero-units\nMIN_UNITS: 1\nPRIOR_SPLIT_BASIS: none\n"
            "UNITS: 0\nTEST_EDIT: none\n"
            "OUTSTANDING: D5 (test-edit-declined: tests/test_x.py) [self-report]\n"
            "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n"
        ).encode("utf-8")
        rc, out, _err = run_implement(fx, write_receipt_bytes(fx, body))
        h.test_result(
            "zero-units receipt with a declined item raises no split-basis or UNITS error",
            "Traceback" not in out and "cannot be less than" not in out
            and "requires UNITS: 0" not in out and "requires SPLIT_BASIS: zero-units" not in out,
            f"rc={rc} out={out[-400:]}",
        )
        h.test_result(
            "zero-units receipt with a declined item does not reject the test-edit-declined OUTSTANDING line",
            "test-edit-declined" not in out or "OUTSTANDING" not in out.split("test-edit-declined")[0][-80:],
            f"rc={rc} out={out[-400:]}",
        )


def main():
    h = Harness("FLOW-VERIFY-RECEIPT IMPLEMENT-STEP TEST-CHANGE TESTS (#511)")

    print("[Baseline]")

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        rc, out, _err = run_implement(fx, write_receipt(fx, ["TEST_EDIT: none"]))
        h.test_result(
            "implement receipt with TEST_EDIT none and no approvals passes",
            rc == 0 and "ARTIFACTS: ok" in out,
            f"rc={rc} out={out[-400:]}",
        )

    print("[Malformed test-change lines fail closed]")

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        rc, out, _err = run_implement(
            fx, write_receipt(fx, ["TEST_REMOVAL: D3 tests/test_x.py"]))
        h.test_result(
            "TEST_REMOVAL with two tokens (no answers file) fails closed",
            rc != 0 and "Traceback" not in out,
            f"rc={rc} out={out[-400:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        rc, out, _err = run_implement(
            fx, write_receipt(fx, ["TEST_EDIT: D3 tests/test_x.py answers/implement-02.json extra"]))
        h.test_result(
            "TEST_EDIT with four tokens fails closed",
            rc != 0 and "Traceback" not in out,
            f"rc={rc} out={out[-400:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        rc, out, _err = run_implement(
            fx, write_receipt(fx, ["TEST_REMOVAL: D3 tests/test_x.py answers/implement-02.json extra"]))
        h.test_result(
            "TEST_REMOVAL with four tokens fails closed",
            rc != 0 and "Traceback" not in out,
            f"rc={rc} out={out[-400:]}",
        )

    print("[Item ids and paths]")

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        rc, out, _err = run_implement(
            fx, write_receipt(fx, ["TEST_EDIT: D 3 tests/test_x.py answers/implement-02.json"]))
        h.test_result(
            "item id containing whitespace is rejected, not parsed into a valid approval",
            rc != 0 and "Traceback" not in out,
            f"rc={rc} out={out[-400:]}",
        )

    with tempfile.TemporaryDirectory() as td:
        fx = build_fixture(td)
        raw = (
            "ROUND 1\nCHECK: true\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            f"CHECK_SHA: {fx['head']}\nSPLIT_BASIS: parallel\nMIN_UNITS: 1\nPRIOR_SPLIT_BASIS: none\n"
            "UNITS: 1\nTEST_EDIT: D3 tests/café_ÿ.py answers/implement-02.json\n"
            "OUTSTANDING: none\nATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n"
        ).encode("utf-8")
        raw = raw.replace("café_ÿ".encode("utf-8"), b"caf\xe9_\xff")
        rc, out, _err = run_implement(fx, write_receipt_bytes(fx, raw))
        h.test_result(
            "non-UTF-8 path in TEST_EDIT does not crash the verifier (no traceback)",
            "Traceback" not in out,
            f"rc={rc} out={out[-400:]}",
        )

    check_zero_unit_all_declined(h)

    h.summarize_and_exit()


def write_receipt_bytes(fx, raw):
    """Write raw receipt bytes to steps/03-implement.md. Returns its path."""
    path = fx["flow"] / "steps" / "03-implement.md"
    path.write_bytes(raw)
    return path


if __name__ == "__main__":
    main()
