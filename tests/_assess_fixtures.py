"""Shared fixture builders for the #383 slice 1 assess-stage verifier tests.

Not a suite: tests/run_all.py only globs test_*.py, so this helper is imported, never run.
Builds FLOW_DIR trees (state.json, log.md, steps/, questions/, answers/) in a temp dir and runs
scripts/flow-verify-receipt.py as a subprocess. The shared line grammar is the #383 contract:
" | " separated records, <id> = <source-step>:<unit-id>.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_harness import REPO_ROOT  # noqa: E402

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
TS = "2026-10-27T10:00:00Z"
TRAILER = "ATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n"
ISSUE_URL = "https://github.com/acme/app/issues/7"
DROP_REASON = "Superseded by the new parser design"
DECISION_OPTIONS = "Patch the gap now => pause ;; File a follow-up => issue"
ANS = "answers/assess-01.json"
S3 = "implement:Step 3"
S4 = "implement:Step 4"
S5 = "implement:Step 5"

# Derived set for this fixture: implement:Step 3 (table-only, verifier, uncovered) and
# implement:Step 4 (OUTSTANDING-declared, self-report, declared). Step 1 is covered and not an item.
BASE_IMPL = {
    "outstanding": ["Step 4 (deferred) [self-report]"],
    "rows": ["Step 3 | uncovered | not in diff", "Step 4 | declared | deferred", "Step 1 | covered | in diff"],
}
DEFAULT_LOG = [f"{TS} implement ok", f"{TS} fix skipped"]


def run_verifier(args, home):
    """Run the verifier CLI with HOME pinned to `home`. Returns (returncode, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    result = subprocess.run([sys.executable, str(VERIFIER), *args], capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def make_home(td):
    home = Path(td) / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _write_step(flow, name, spec):
    """spec keys: outstanding (list of receipt OUTSTANDING values), rows (table rows), rows_raw (raw
    coverage text), receipt (False to omit the receipt file)."""
    if spec.get("receipt", True):
        outs = spec.get("outstanding", [])
        body = "Decision: OK\n"
        body += "".join(f"OUTSTANDING: {line}\n" for line in outs) if outs else "OUTSTANDING: none\n"
        body += "<!-- step-end -->\n"
        (flow / "steps" / f"{name}.md").write_text(body)
    if "rows_raw" in spec:
        (flow / "steps" / f"{name}.coverage.txt").write_text(spec["rows_raw"])
    elif "rows" in spec:
        rows = spec["rows"]
        text = f"UNITS: {len(rows)}\n" + "".join(f"{r}\n" for r in rows)
        (flow / "steps" / f"{name}.coverage.txt").write_text(text)


def make_flow(td, *, implement=None, fix=None, log=None, state=None, no_issue_url=False):
    """Build <td>/flow. `implement`/`fix` are step specs (see _write_step) or None when the step has no
    files. `log` defaults to implement ok + fix skipped. `state` is merged over a default state.json that
    carries `issue_url` (acme/app, so ISSUE_URL binds); `no_issue_url=True` omits that default."""
    flow = Path(td) / "flow"
    for sub in ("steps", "questions", "answers"):
        (flow / sub).mkdir(parents=True, exist_ok=True)
    if implement is not None:
        _write_step(flow, "03-implement", implement)
    if fix is not None:
        _write_step(flow, "05-fix", fix)
    lines = DEFAULT_LOG if log is None else log
    (flow / "log.md").write_text("\n".join(lines) + "\n")
    base_state = {} if no_issue_url else {"issue_url": "https://github.com/acme/app/issues/1"}
    (flow / "state.json").write_text(json.dumps({**base_state, **(state or {})}))
    return flow


def assess_line(item, *, status="unfinished", kind="mechanical", scope="in-scope", leave="yes",
                cause="Handler missing from the router", evidence="src/api.py:12 has no handler; grep finds none",
                finish_plan=None, verification_proof=None, options=""):
    """One assessment record in the #383 line grammar. finish_plan/verification_proof default to filled
    values only for unfinished mechanical items (the only kind that must carry them)."""
    needs_plan = status == "unfinished" and kind == "mechanical"
    if finish_plan is None:
        finish_plan = "Add the handler in src/api.py" if needs_plan else ""
    if verification_proof is None:
        verification_proof = "pytest tests/test_api.py -k handler" if needs_plan else ""
    fields = [
        item, f"status={status}", f"kind={kind}", f"scope={scope}", f"leave_as_is_safe={leave}",
        f"cause={cause}", f"evidence={evidence}", f"finish_plan={finish_plan}",
        f"verification_proof={verification_proof}", f"options={options}",
    ]
    return " | ".join(fields)


def write_assess(flow, lines, end_marker=True):
    """Write steps/07-assess.md: item lines, assessment-end block, trailer."""
    text = "".join(f"{ln}\n" for ln in lines)
    if end_marker:
        text += "<!-- assessment-end -->\n"
    text += TRAILER
    (flow / "steps" / "07-assess.md").write_text(text)


def disp(item, disposition, *, by="human", answer_ref=ANS, option="none", route="none", url="none", reason="none"):
    """One disposition record in the #383 marker grammar."""
    fields = [
        item, f"disposition={disposition}", f"by={by}", f"answer_ref={answer_ref}", f"option={option}",
        f"route={route}", f"url={url}", f"reason={reason}",
    ]
    return " | ".join(fields)


def write_marker(flow, lines, *, fmt=True, assessment="assessor", sentinel=True):
    """Write steps/07-ship.outstanding.md. fmt=False gives a legacy-style marker (no format/assessment
    lines). sentinel=False omits <!-- outstanding-end -->."""
    body = []
    if fmt:
        body += ["format: 2", f"assessment: {assessment}"]
    body.append("<!-- outstanding-pending -->")
    body += lines
    if sentinel:
        body.append("<!-- outstanding-end -->")
    path = flow / "steps" / "07-ship.outstanding.md"
    path.write_text("\n".join(body) + "\n")
    return path


def write_answers(flow, seq, entries, ledger=True):
    """entries: list of (qid, item_id, label, notes_or_None). Writes questions/assess-<seq>.json (each
    question carries `item`) and answers/assess-<seq>.json (answers keyed by question id, notes under
    annotations.<qid>.notes). When ledger=True (default), appends a human.jsonl record for answer_ref tracking."""
    questions = []
    answers = {}
    annotations = {}
    for qid, item, label, notes in entries:
        questions.append({
            "id": qid, "item": item, "question": f"Outstanding item {item}", "header": "Outstanding",
            "options": [{"label": label, "description": ""}],
        })
        answers[qid] = label
        if notes is not None:
            annotations[qid] = {"notes": notes}
    (flow / "questions" / f"assess-{seq:02d}.json").write_text(
        json.dumps({"step": "assess", "seq": seq, "questions": questions}))
    answer_file = flow / "answers" / f"assess-{seq:02d}.json"
    answer_file.write_text(
        json.dumps({"step": "assess", "seq": seq, "answers": answers, "annotations": annotations}))

    if ledger:
        # Append human.jsonl record for answer_ref tracking (F2 directive)
        ledger_file = flow / "human.jsonl"
        answer_bytes = answer_file.read_bytes()
        sha256_hash = hashlib.sha256(answer_bytes).hexdigest()
        answer_ref = f"answers/assess-{seq:02d}.json"

        # Get current line count for seq
        line_count = 0
        if ledger_file.exists():
            line_count = len(ledger_file.read_text().splitlines())

        record = {
            "seq": line_count + 1,
            "ts": TS,
            "step": "assess",
            "attempt": 1,
            "kind": "answer",
            "text": f"answer_ref: {answer_ref}\nsha256: {sha256_hash}",
            "transcript": None,
            "status": "addressed",
            "addressed_by": "steps/07-assess.md",
            "supersedes": None,
        }
        with open(ledger_file, "a") as f:
            f.write(json.dumps(record) + "\n")


def parse_outstanding(stdout):
    """Parse --outstanding-set stdout into (declared_count, {id: (provenance, coverage, reason)})."""
    count = None
    rows = {}
    for line in stdout.splitlines():
        if line.startswith("ITEMS:"):
            count = int(line.split(":", 1)[1].strip())
            continue
        parts = line.split(" | ")
        if len(parts) >= 4:
            rows[parts[0]] = (parts[1], parts[2], " | ".join(parts[3:]))
    return count, rows


def gate(flow, home):
    return run_verifier(["--outstanding-gate", "--flow-dir", str(flow),
                         "--receipt", str(flow / "steps" / "07-ship.outstanding.md")], home)


def assess_check(flow, home):
    return run_verifier(["--step", "assess", "--receipt", str(flow / "steps" / "07-assess.md"),
                         "--flow-dir", str(flow)], home)


def outstanding_set(flow, home):
    return run_verifier(["--outstanding-set", "--flow-dir", str(flow)], home)
