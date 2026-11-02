#!/usr/bin/env python3
"""
Spec-blind black-box tests for the `issue-comments` helper (ADR-0023 and ADR-0008 2026-10-30 amendments).

Written from prompts/issue-comments-reference.md, the action-plan rulings (fail closed for unverified
comments, acknowledgement resolved from questions/ and answers/ artifacts, typed `unsupported`) and the
CLI --help text. The implementation modules were not read. Every check drives the real CLI as a
subprocess (`python3 -m scripts.workflow.cli issue-comments fetch|finalize`) against a throwaway
fixture directory. `gh` is replaced by a fake on PATH that serves canned JSON, so no network is used.

Covers (behaviours not already named in tests/test_issue_comments.py or
tests/test_issue_comments_call_sites.py):
1. finalize with an empty verdict directory fails closed at the CLI boundary (unverified dropped,
   trusted kept, dropped unverified counted as scout-invalid)
2. a missing verdict directory fails closed: non-zero exit, or exit 0 with no unverified text
3. invalid JSON, an unknown verdict value, an unknown reason and a missing id: only an explicit valid
   include survives
4. a scout verdict that drops a trusted comment never removes it
5. relevance batches cover every unverified id exactly once and never contain a trusted id
6. finalize --format markdown omits dropped unverified text and keeps included text
7. a missing or corrupt --fetched file exits non-zero (the plan step then sets comments unavailable)
8. acknowledgement is resolved from questions/ + answers/ at the CLI: only an exact Continue answer
   acknowledges; Stop, free text, a multiSelect answer, a near-miss label and an unanswered question do not
9. --mode local reports comments_status "unsupported" with an error message
10. an issue fetch failure exits 3 and still emits comments_status "unavailable"

Deprecated-planner "no issue-comments wiring" is already named in
tests/test_issue_comments_call_sites.py ("Deprecated Planners Have No Issue-Comments Wiring"),
so it is not repeated here.

Run with: python3 tests/test_issue_comments_spec.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

CONTINUE = "Continue — the plan stands"
STOP = "Stop for re-plan"

# Canned `gh` for the two calls the helper makes: `repo view` and `issue view`.
FAKE_GH = """#!/bin/sh
case "$*" in
  "repo view --json nameWithOwner")
    printf '%s' '{"nameWithOwner":"acme/widgets"}'
    exit 0 ;;
  issue\\ view\\ *)
    if [ -n "$FAKE_GH_FAIL" ]; then echo "fake gh failure" >&2; exit 1; fi
    cat "$FAKE_GH_ISSUE"
    exit 0 ;;
esac
echo "unexpected gh call: $*" >&2
exit 1
"""


def run_cli(args, path_prefix=None, extra_env=None):
    """Run `python3 -m scripts.workflow.cli issue-comments <args>`. Returns (rc, stdout, stderr)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if path_prefix:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-m", "scripts.workflow.cli", "issue-comments", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def parse_json(text):
    """Parse CLI stdout as JSON; return {} when it is not a JSON object."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def comment(cid, body, login, association, created_at):
    return {
        "id": cid,
        "author": {"login": login},
        "authorAssociation": association,
        "body": body,
        "createdAt": created_at,
        "isMinimized": False,
    }


ISSUE_CREATED = "2026-01-01T00:00:00Z"
TRUSTED_ID = "IC_T1"
TRUSTED = comment(TRUSTED_ID, "TRUSTED-DECISION-TEXT", "alice", "OWNER", "2026-01-02T00:00:00Z")


def unverified(n):
    return comment(f"IC_U{n}", f"UNVERIFIED-TEXT-{n}", f"outsider{n}", "NONE", f"2026-01-03T00:0{n}:00Z")


UNVERIFIED_IDS = {f"IC_U{n}" for n in range(1, 7)}
FIXTURE = [TRUSTED, *[unverified(n) for n in range(1, 7)]]


def verdict_doc(entries):
    """entries: list of (comment_id, verdict, reason) tuples."""
    return {
        "schema_version": 1,
        "verdicts": [{"comment_id": c, "verdict": v, "reason": r} for c, v, r in entries],
    }


def ids_in(data):
    return {c.get("id") for c in data.get("comments", [])}


class Sandbox:
    """A throwaway tree: bin/gh (fake), issue.json, flow/ (FLOW_DIR), verdicts/ (scout verdicts)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.issue_file = self.root / "issue.json"
        self.flow = self.root / "flow"
        self.flow.mkdir()
        self.verdicts = self.root / "verdicts"
        self.verdicts.mkdir()

    def fetch(self, comments, extra=(), fail=False):
        issue = {
            "number": 7,
            "title": "Ticket",
            "body": "Issue body",
            "labels": [],
            "state": "OPEN",
            "url": "https://github.com/acme/widgets/issues/7",
            "createdAt": ISSUE_CREATED,
            "comments": comments,
        }
        self.issue_file.write_text(json.dumps(issue))
        return run_cli(
            ["fetch", "--issue", "7", "--cwd", str(self.root), "--format", "json", *extra],
            path_prefix=str(self.bin),
            extra_env={
                "FAKE_GH_ISSUE": str(self.issue_file),
                "FAKE_GH_FAIL": "1" if fail else "",
            },
        )

    def fetch_to_file(self, comments, extra=()):
        rc, out, err = self.fetch(comments, extra)
        path = self.root / "issue-comments.json"
        path.write_text(out)
        return rc, out, err, path

    def write_verdict(self, name, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.verdicts / name).write_text(text)

    def finalize(self, fetched, verdicts=None, fmt="json"):
        vdir = self.verdicts if verdicts is None else verdicts
        return run_cli(["finalize", "--fetched", str(fetched), "--verdicts", str(vdir), "--format", fmt])


def check_no_verdicts_fail_closed(h):
    sb = Sandbox()
    rc, _, err, fetched = sb.fetch_to_file(FIXTURE)
    h.test_result("fixture fetch exits 0 (precondition)", rc == 0, err[:200])

    frc, fout, ferr = sb.finalize(fetched)
    h.test_result("finalize with an empty verdict directory exits 0", frc == 0, ferr[:200])
    data = parse_json(fout)
    ids = ids_in(data)
    h.test_result("empty verdict directory: trusted comment kept", TRUSTED_ID in ids, str(sorted(ids)))
    h.test_result(
        "empty verdict directory: every unverified comment dropped",
        not (ids & UNVERIFIED_IDS),
        str(sorted(ids & UNVERIFIED_IDS)),
    )
    counts = data.get("counts", {})
    h.test_result(
        "empty verdict directory: six unruled unverified counted as scout-invalid",
        counts.get("dropped_scout_invalid") == 6,
        str(counts),
    )


def check_missing_verdict_dir_fail_closed(h):
    sb = Sandbox()
    rc, _, err, fetched = sb.fetch_to_file(FIXTURE)
    h.test_result("fixture fetch exits 0 (precondition, missing-dir case)", rc == 0, err[:200])

    absent = sb.root / "no-such-verdict-dir"
    frc, fout, ferr = sb.finalize(fetched, verdicts=absent)
    if frc == 0:
        ids = ids_in(parse_json(fout))
        ok = not (ids & UNVERIFIED_IDS) and TRUSTED_ID in ids
        detail = f"exit 0, ids={sorted(ids)}"
    else:
        ok = "UNVERIFIED-TEXT" not in fout
        detail = f"exit {frc}; stdout carries no unverified text: {ok}"
    h.test_result(
        "missing verdict directory: non-zero exit or no unverified text kept",
        ok,
        detail,
    )


def check_invalid_reports_keep_only_explicit_include(h):
    sb = Sandbox()
    rc, _, err, fetched = sb.fetch_to_file(FIXTURE)
    h.test_result("fixture fetch exits 0 (precondition, invalid-report case)", rc == 0, err[:200])

    sb.write_verdict("a01-garbage.json", "not json {")
    sb.write_verdict("a02-unknown-verdict.json", verdict_doc([("IC_U2", "maybe", None)]))
    sb.write_verdict("a03-unknown-reason.json", verdict_doc([("IC_U3", "drop", "because_i_said")]))
    sb.write_verdict("a04-valid-include.json", verdict_doc([("IC_U1", "include", None)]))

    frc, fout, ferr = sb.finalize(fetched)
    h.test_result("mixed invalid reports: finalize exits 0", frc == 0, ferr[:200])
    ids = ids_in(parse_json(fout))
    kept = ids & UNVERIFIED_IDS
    h.test_result(
        "mixed invalid reports: exactly the explicit valid include (IC_U1) survives",
        kept == {"IC_U1"},
        str(sorted(kept)),
    )
    h.test_result("mixed invalid reports: trusted comment kept", TRUSTED_ID in ids)


def check_verdict_cannot_drop_trusted(h):
    sb = Sandbox()
    rc, _, err, fetched = sb.fetch_to_file(FIXTURE)
    h.test_result("fixture fetch exits 0 (precondition, trusted-drop case)", rc == 0, err[:200])

    entries = [(TRUSTED_ID, "drop", "low_signal"), ("IC_U1", "include", None)]
    entries += [(f"IC_U{n}", "drop", "off_topic") for n in range(2, 7)]
    sb.write_verdict("v1.json", verdict_doc(entries))

    frc, fout, ferr = sb.finalize(fetched)
    h.test_result("trusted-drop verdict: finalize exits 0", frc == 0, ferr[:200])
    ids = ids_in(parse_json(fout))
    h.test_result(
        "a verdict that drops a trusted comment never removes it",
        TRUSTED_ID in ids,
        str(sorted(ids)),
    )
    h.test_result(
        "explicit include still kept alongside the ignored trusted drop",
        ids & UNVERIFIED_IDS == {"IC_U1"},
        str(sorted(ids & UNVERIFIED_IDS)),
    )


def check_relevance_batches_exclude_trusted(h):
    sb = Sandbox()
    rc, out, err = sb.fetch(FIXTURE)
    h.test_result("six unverified fetch exits 0", rc == 0, err[:200])
    rf = parse_json(out).get("relevance_filter", {})
    h.test_result("six unverified survivors: relevance filter required", rf.get("required") is True, str(rf)[:200])
    batches = rf.get("batches", [])
    flat = [cid for batch in batches for cid in batch]
    h.test_result(
        "relevance batches cover every unverified id exactly once",
        sorted(flat) == sorted(UNVERIFIED_IDS),
        str(sorted(flat)),
    )
    h.test_result("relevance batches never contain a trusted comment id", TRUSTED_ID not in flat)


def check_markdown_omits_dropped_unverified(h):
    sb = Sandbox()
    rc, _, err, fetched = sb.fetch_to_file(FIXTURE)
    h.test_result("fixture fetch exits 0 (precondition, markdown case)", rc == 0, err[:200])

    entries = [("IC_U1", "include", None)] + [(f"IC_U{n}", "drop", "off_topic") for n in range(2, 7)]
    sb.write_verdict("v1.json", verdict_doc(entries))

    frc, fout, ferr = sb.finalize(fetched, fmt="markdown")
    h.test_result("markdown finalize exits 0", frc == 0, ferr[:200])
    h.test_result("markdown keeps trusted comment text", "TRUSTED-DECISION-TEXT" in fout)
    h.test_result("markdown keeps the explicitly included unverified text", "UNVERIFIED-TEXT-1" in fout)
    leaked = [n for n in range(2, 7) if f"UNVERIFIED-TEXT-{n}" in fout]
    h.test_result("markdown omits every dropped unverified text", not leaked, f"leaked: {leaked}")


def check_bad_fetched_file_exits_nonzero(h):
    sb = Sandbox()
    absent = sb.root / "absent-issue-comments.json"
    rc, _, _ = run_cli(["finalize", "--fetched", str(absent), "--verdicts", str(sb.verdicts), "--format", "json"])
    h.test_result("absent --fetched file exits non-zero", rc != 0, f"rc={rc}")

    corrupt = sb.root / "corrupt-issue-comments.json"
    corrupt.write_text("{not json")
    rc2, _, _ = run_cli(["finalize", "--fetched", str(corrupt), "--verdicts", str(sb.verdicts), "--format", "json"])
    h.test_result("corrupt --fetched file exits non-zero", rc2 != 0, f"rc={rc2}")


def write_question(flow, seq, comment_id, multi=False):
    qdir = flow / "questions"
    qdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "step": "implement",
        "command": "implement-with-haiku",
        "context_path": None,
        "attempt": 1,
        "summary": "New comment on the issue since the plan was written.",
        "questions": [
            {
                "id": "q1",
                "brief": None,
                "comment_id": comment_id,
                "question": "A new comment on the issue arrived after the plan. Does the plan stand?",
                "header": "New comment",
                "options": [
                    {"label": CONTINUE, "description": "Proceed."},
                    {"label": STOP, "description": "Stop this run."},
                ],
                "multiSelect": multi,
            }
        ],
    }
    (qdir / f"implement-{seq}.json").write_text(json.dumps(doc))


def write_answer(flow, seq, value):
    adir = flow / "answers"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"implement-{seq}.json").write_text(json.dumps({"answers": {"q1": value}}))


def check_ack_resolved_from_artifacts(h):
    sb = Sandbox()
    # seq -> (comment id, multiSelect, answer or None for unanswered)
    cases = {
        "01": ("IC_A_CONT", False, CONTINUE),
        "02": ("IC_A_STOP", False, STOP),
        "03": ("IC_A_FREE", False, "Let me think about it first"),
        "04": ("IC_A_MULTI", True, [CONTINUE]),
        "05": ("IC_A_NEAR", False, "Continue"),
        "06": ("IC_A_UNANS", False, None),
    }
    comments = [TRUSTED]
    for seq, (cid, multi, answer) in cases.items():
        write_question(sb.flow, seq, cid, multi=multi)
        if answer is not None:
            write_answer(sb.flow, seq, answer)
        comments.append(comment(cid, f"ack body {seq}", "alice", "OWNER", "2026-01-04T00:00:00Z"))

    rc, out, err = sb.fetch(comments, extra=("--trusted-only", "--flow-dir", str(sb.flow)))
    h.test_result("fetch with --flow-dir exits 0", rc == 0, err[:200])
    by_id = {c.get("id"): c for c in parse_json(out).get("comments", [])}

    h.test_result(
        "exact Continue answer acknowledges its comment",
        by_id.get("IC_A_CONT", {}).get("acknowledged") is True,
        str(by_id.get("IC_A_CONT")),
    )
    for cid, label in [
        ("IC_A_STOP", "Stop answer"),
        ("IC_A_FREE", "free-text answer"),
        ("IC_A_MULTI", "multiSelect answer"),
        ("IC_A_NEAR", "near-miss label answer"),
        ("IC_A_UNANS", "unanswered question"),
        (TRUSTED_ID, "comment with no question"),
    ]:
        h.test_result(
            f"{label} does not acknowledge its comment",
            by_id.get(cid, {}).get("acknowledged") is False,
            str(by_id.get(cid)),
        )


def check_local_mode_unsupported(h):
    sb = Sandbox()
    rc, out, err = run_cli(["fetch", "--issue", "7", "--cwd", str(sb.root), "--mode", "local", "--format", "json"])
    data = parse_json(out)
    h.test_result(
        "local mode reports comments_status unsupported",
        data.get("comments_status") == "unsupported",
        out[:200] + err[:200],
    )
    h.test_result("local mode unsupported carries an error message", bool(data.get("error")), str(data)[:200])
    h.test_result("local mode unsupported carries no comments", data.get("comments") == [])


def check_issue_fetch_failure_exit_3(h):
    sb = Sandbox()
    rc, out, err = sb.fetch(FIXTURE, fail=True)
    data = parse_json(out)
    h.test_result("issue fetch failure exits 3", rc == 3, f"rc={rc} err={err[:200]}")
    h.test_result(
        "issue fetch failure still emits JSON with comments_status unavailable",
        data.get("comments_status") == "unavailable",
        out[:200],
    )


def main():
    h = Harness("ISSUE COMMENTS SPEC TEST (black-box CLI)")
    check_no_verdicts_fail_closed(h)
    check_missing_verdict_dir_fail_closed(h)
    check_invalid_reports_keep_only_explicit_include(h)
    check_verdict_cannot_drop_trusted(h)
    check_relevance_batches_exclude_trusted(h)
    check_markdown_omits_dropped_unverified(h)
    check_bad_fetched_file_exits_nonzero(h)
    check_ack_resolved_from_artifacts(h)
    check_local_mode_unsupported(h)
    check_issue_fetch_failure_exit_3(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
