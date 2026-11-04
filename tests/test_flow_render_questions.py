#!/usr/bin/env python3
"""
Unit tests for scripts/flow-render-questions.py, the one question renderer shared by /expert-flow
and /expert-fleet (#421).

The script is driven as a black box through its CLI against question fixtures written to a temp
dir. Covered:

1. form kind: 6 questions chunked into 2 batches; summary and briefs in the right preamble; option
   descriptions (including Pat's Precedent text) byte-equal to the source; ask entries copy the
   question text, header and multiSelect and drop id and brief.
2. no `<id>:` label line in any preamble, and questions[].id preserved in order.
3. an old file with no `kind` renders as a form.
4. null summary and every brief null: the context_path file is the (fenced) preamble; a null
   context_path gives the placeholder and a warning.
5. a brief containing a backtick fence is enclosed by a longer fence.
6. report kind: section order, `#` escaping, Pat text verbatim, the closing sentence (matches the
   command doc after whitespace normalisation).
7. --prefix header cap (12 characters; a prefix of 12 characters or more is a usage error); no cap
   without --prefix.
8. failure exits: 3 (missing file, malformed JSON) and 4 (missing id, null question, non-list
   questions, duplicate question text), each with no --out written.
9. output discipline: stdout is one status line and never carries brief text.

Run with: python3 tests/test_flow_render_questions.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

RENDER = REPO_ROOT / "scripts" / "flow-render-questions.py"
EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"

REPORT_CLOSING = (
    "Reply in your own words: answer every Q, and name any D or W you want to change. "
    "Unmentioned D and W items stand; unanswered Qs will be asked again unless you say continue. "
    "Example: **Q1: B, Q2: A, change D1 to focus on security.**"
)


def run_render(question_file, out, prefix=None):
    """Run the renderer as a subprocess. Returns the CompletedProcess."""
    cmd = [sys.executable, str(RENDER), str(question_file), "--out", str(out)]
    if prefix is not None:
        cmd += ["--prefix", prefix]
    return subprocess.run(cmd, capture_output=True, text=True)


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_question(n, brief=True):
    """A form question with a Pat line in option A's description, and a brief unless brief is False."""
    return {
        "id": f"q{n}",
        "question": f"Question {n} text?",
        "header": f"Hdr {n}",
        "multiSelect": False,
        "options": [
            {"label": "A", "description": f"Option A for q{n}. Precedent: user chose A on 2026-09-01 (high)."},
            {"label": "B", "description": f"Option B for q{n}."},
        ],
        "brief": f"#### Q{n}. Brief for question {n}\nBody of brief {n}." if brief else None,
    }


def make_form(questions, **extra):
    data = {"kind": "form", "summary": "SUMMARY LINE for the ticket", "context_path": None,
            "questions": questions}
    data.update(extra)
    return data


def make_report():
    return {
        "kind": "report",
        "headline": "Plan ready for review",
        "north_star_blind": None,
        "ticket": "Add the renderer",
        "roundup": "Round-up text",
        "items": [
            {"handle": "Q1", "title": "# Injected heading", "must_answer": True,
             "options": [{"label": "A", "for": "fast", "against": "slow"},
                         {"label": "B", "for": "safe", "against": "costly"}],
             "decision_or_recommendation": "A", "backed_by": ["Uncle Bob"],
             "why": "Precedent: user chose A on 2026-09-01.", "business_case": "Saves time"},
            {"handle": "D1", "title": "Decision title", "decision_or_recommendation": "Use stdlib",
             "backed_by": ["Dana"], "why": "Keeps deps down."},
            {"handle": "W1", "title": "Worth a look", "decision_or_recommendation": "Check the cap",
             "backed_by": ["Penny"], "why": "Edge case."},
        ],
    }


def test_form_batches(h, tmp):
    questions = [make_question(n) for n in range(1, 7)]
    src = write_json(tmp / "form6.json", make_form(questions))
    out = tmp / "form6.out.json"
    proc = run_render(src, out)
    h.test_result("form (6 questions): exit 0", proc.returncode == 0, proc.stderr)
    if proc.returncode != 0:
        return
    doc = json.loads(out.read_text(encoding="utf-8"))
    batches = doc["batches"]
    h.test_result("form (6 questions): kind is form", doc.get("kind") == "form", str(doc.get("kind")))
    h.test_result("form (6 questions): two batches of 4 and 2",
                  [len(b["questions"]) for b in batches] == [4, 2],
                  str([len(b["questions"]) for b in batches]))

    p1, p2 = batches[0]["preamble"], batches[1]["preamble"]
    h.test_result("batch 1 preamble carries the summary and briefs q1-q4",
                  "SUMMARY LINE for the ticket" in p1
                  and all(f"Body of brief {n}." in p1 for n in range(1, 5)),
                  p1)
    h.test_result("batch 1 preamble omits the briefs of q5 and q6",
                  "Body of brief 5." not in p1 and "Body of brief 6." not in p1, p1)
    h.test_result("batch 2 preamble carries only the briefs of q5 and q6",
                  "Body of brief 5." in p2 and "Body of brief 6." in p2
                  and not any(f"Body of brief {n}." in p2 for n in range(1, 5)), p2)
    h.test_result("batch 2 preamble does not repeat the summary",
                  "SUMMARY LINE" not in p2, p2)

    got_options = [q["ask"]["options"] for b in batches for q in b["questions"]]
    want_options = [q["options"] for q in questions]
    h.test_result("option label and description are byte-equal to the source (Precedent text kept)",
                  got_options == want_options, str(got_options))

    asks_ok = True
    for b in batches:
        for entry in b["questions"]:
            src_q = questions[int(entry["id"][1:]) - 1]
            ask = entry["ask"]
            if not (ask["question"] == src_q["question"] and ask["header"] == src_q["header"]
                    and ask["multiSelect"] == src_q["multiSelect"]):
                asks_ok = False
            if "id" in ask or "brief" in ask:
                asks_ok = False
    h.test_result("ask copies question, header and multiSelect; strips id and brief",
                  asks_ok)

    stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    h.test_result("output discipline: stdout is one status line",
                  len(stdout_lines) == 1 and stdout_lines[0].startswith("rendered: form 2 -> "),
                  proc.stdout)
    h.test_result("output discipline: stdout carries no brief text",
                  "Body of brief" not in proc.stdout and "SUMMARY LINE" not in proc.stdout,
                  proc.stdout)


def test_no_id_label(h, tmp):
    questions = [make_question(n) for n in range(1, 7)]
    src = write_json(tmp / "nolabel.json", make_form(questions))
    out = tmp / "nolabel.out.json"
    proc = run_render(src, out)
    if proc.returncode != 0:
        h.test_result("no id label: render succeeded", False, proc.stderr)
        return
    doc = json.loads(out.read_text(encoding="utf-8"))
    preambles = [b["preamble"] for b in doc["batches"]]
    labelled = [p for p in preambles if re.search(r"^q[0-9]+:", p, re.MULTILINE)]
    bare = [p for p in preambles for n in range(1, 7) if f"q{n}:" in p.splitlines()]
    h.test_result("no id label: no preamble line matches ^q[0-9]+:", not labelled, str(labelled))
    h.test_result("no id label: no preamble line equals <id>:", not bare, str(bare))
    ids = [q["id"] for b in doc["batches"] for q in b["questions"]]
    h.test_result("no id label: questions[].id still equals the source ids in order",
                  ids == [f"q{n}" for n in range(1, 7)], str(ids))


def test_old_file_without_kind(h, tmp):
    data = {"summary": "OLD SUMMARY", "context_path": None,
            "questions": [make_question(1), make_question(2)]}
    src = write_json(tmp / "old.json", data)
    out = tmp / "old.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    h.test_result("old file with no kind renders as a form",
                  proc.returncode == 0 and doc.get("kind") == "form"
                  and len(doc.get("batches", [])) == 1,
                  proc.stderr or str(doc.get("kind")))


def test_null_context(h, tmp):
    # Create context file under ~/.claude/flows/ to pass containment check
    flows_dir = Path.home() / ".claude" / "flows" / "test-context"
    flows_dir.mkdir(parents=True, exist_ok=True)
    context_text = "CONTEXT FILE LINE 1\nsome `inline` code\nCONTEXT LINE 3\n"
    context_path = flows_dir / "context.md"
    context_path.write_text(context_text, encoding="utf-8")
    questions = [make_question(n, brief=False) for n in range(1, 3)]
    data = make_form(questions, summary=None, context_path=str(context_path))
    src = write_json(tmp / "nullctx.json", data)
    out = tmp / "nullctx.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    preamble = doc.get("batches", [{}])[0].get("preamble", "")
    want = "```\n" + context_text.rstrip("\n") + "\n```"
    h.test_result("null summary and null briefs: batch 1 preamble is the context_path file (fenced)",
                  proc.returncode == 0 and preamble == want, preamble)
    # Clean up
    try:
        context_path.unlink()
        flows_dir.rmdir()
    except OSError:
        pass

    data_none = make_form(questions, summary=None, context_path=None)
    src_none = write_json(tmp / "nullctx-none.json", data_none)
    out_none = tmp / "nullctx-none.out.json"
    proc_none = run_render(src_none, out_none)
    doc_none = json.loads(out_none.read_text(encoding="utf-8")) if proc_none.returncode == 0 else {}
    placeholder = doc_none.get("batches", [{}])[0].get("preamble", "")
    h.test_result("null context_path: placeholder preamble",
                  proc_none.returncode == 0 and placeholder == "(no context provided by the step)",
                  placeholder)
    h.test_result("null context_path: a warning is recorded",
                  bool(doc_none.get("warnings")), str(doc_none.get("warnings")))


def test_brief_backtick_fence(h, tmp):
    brief = "#### Q1. Code\n```\nprint('x')\n```\nafter the fence"
    q1 = make_question(1)
    q1["brief"] = brief
    src = write_json(tmp / "fence.json", make_form([q1], summary=None))
    out = tmp / "fence.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    preamble = doc.get("batches", [{}])[0].get("preamble", "")
    h.test_result("brief with a ``` fence: the preamble fence is longer and encloses all of it",
                  proc.returncode == 0 and preamble.startswith("````\n")
                  and preamble.endswith("\n````") and brief in preamble, preamble)


def test_report(h, tmp):
    src = write_json(tmp / "report.json", make_report())
    out = tmp / "report.out.md"
    proc = run_render(src, out)
    h.test_result("report: exit 0", proc.returncode == 0, proc.stderr)
    if proc.returncode != 0:
        return
    content = out.read_text(encoding="utf-8")
    stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    h.test_result("report: stdout is one status line",
                  len(stdout_lines) == 1 and stdout_lines[0].startswith("rendered: report report -> "),
                  proc.stdout)

    markers = [
        "> Plan ready for review",
        "Needs your answer: Q1",
        "## What this ticket does",
        "## Questions for you (1)",
        "## Decided (1)",
        "## Worth a look (1)",
        "Round-up text",
        REPORT_CLOSING,
    ]
    positions = [content.find(m) for m in markers]
    in_order = all(p >= 0 for p in positions) and positions == sorted(positions)
    h.test_result("report: sections appear in the fixed order, closing sentence last",
                  in_order, str(positions))

    h.test_result("report: a title beginning with # is escaped",
                  "\\# Injected heading" in content and "\n# Injected heading" not in content,
                  content)
    h.test_result("report: the why text keeps its Precedent line verbatim",
                  "Precedent: user chose A on 2026-09-01." in content, content)

    doc_text = re.sub(r"\s+", " ", EXPERT_FLOW.read_text(encoding="utf-8"))
    h.test_result("report: closing sentence matches commands/expert-flow.md (whitespace normalised)",
                  REPORT_CLOSING in doc_text)


def test_prefix_cap(h, tmp):
    q1 = make_question(1)
    q1["header"] = "Review ruling"
    src = write_json(tmp / "cap.json", make_form([q1]))
    out = tmp / "cap.out.json"
    proc = run_render(src, out, prefix="#110 ")
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    header = doc.get("batches", [{}])[0].get("questions", [{}])[0].get("ask", {}).get("header", "")
    h.test_result("--prefix '#110 ' caps a 'Review ruling' header at 12 characters, starting with '#110 '",
                  proc.returncode == 0 and len(header) == 12 and header.startswith("#110 "),
                  repr(header))

    ok_out = tmp / "cap-11.out.json"
    proc_ok = run_render(src, ok_out, prefix="#110 abcdef")
    h.test_result("--prefix of 11 characters is accepted (boundary)", proc_ok.returncode == 0,
                  proc_ok.stderr)

    long_out = tmp / "cap-12.out.json"
    proc_long = run_render(src, long_out, prefix="#110 abcdefg ")
    h.test_result("--prefix of 12 characters is a usage error (exit 1)",
                  proc_long.returncode == 1 and not long_out.exists(),
                  f"exit {proc_long.returncode}")


def test_no_prefix_no_cap(h, tmp):
    q1 = make_question(1)
    q1["header"] = "Zero-commit fix"
    src = write_json(tmp / "nocap.json", make_form([q1]))
    out = tmp / "nocap.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    header = doc.get("batches", [{}])[0].get("questions", [{}])[0].get("ask", {}).get("header")
    h.test_result("no --prefix: a 15-character header passes through unchanged",
                  proc.returncode == 0 and header == "Zero-commit fix", repr(header))


def test_failure_exits(h, tmp):
    cases = [
        ("missing input file", None, 3),
        ("malformed JSON", "{not json", 3),
        ("question without id", make_form([{"question": "Q?", "options": []}]), 4),
        ("question with null question", make_form([{**make_question(1), "question": None}]), 4),
        ("non-list questions", {"kind": "form", "questions": "nope"}, 4),
        ("duplicate question text", make_form([make_question(1), {**make_question(2), "question": "Question 1 text?"}]), 4),
    ]
    for index, (name, payload, want) in enumerate(cases):
        if payload is None:
            src = tmp / "does-not-exist.json"
        elif isinstance(payload, str):
            src = tmp / f"fail-{index}.json"
            src.write_text(payload, encoding="utf-8")
        else:
            src = write_json(tmp / f"fail-{index}.json", payload)
        out = tmp / f"fail-{index}.out.json"
        proc = run_render(src, out)
        h.test_result(f"failure exit {want}: {name}",
                      proc.returncode == want and not out.exists(),
                      f"exit {proc.returncode}, out exists: {out.exists()}")


def test_empty_questions(h, tmp):
    """Empty questions list should exit 4."""
    data = make_form([])
    src = write_json(tmp / "empty.json", data)
    out = tmp / "empty.out.json"
    proc = run_render(src, out)
    h.test_result("empty questions list exits 4",
                  proc.returncode == 4 and not out.exists(),
                  f"exit {proc.returncode}, out exists: {out.exists()}")


def test_malformed_options(h, tmp):
    """Non-dict option entries should exit 4."""
    q = make_question(1)
    q["options"] = ["not", "a", "dict"]  # Bad: list of strings instead of dicts
    src = write_json(tmp / "bad_opts.json", make_form([q]))
    out = tmp / "bad_opts.out.json"
    proc = run_render(src, out)
    h.test_result("malformed options entries exit 4",
                  proc.returncode == 4 and not out.exists(),
                  f"exit {proc.returncode}, out exists: {out.exists()}")


def test_non_string_header(h, tmp):
    """Non-string header should exit 4."""
    q = make_question(1)
    q["header"] = 123  # Bad: not a string
    src = write_json(tmp / "bad_header.json", make_form([q]))
    out = tmp / "bad_header.out.json"
    proc = run_render(src, out)
    h.test_result("non-string header exits 4",
                  proc.returncode == 4 and not out.exists(),
                  f"exit {proc.returncode}, out exists: {out.exists()}")


def test_unknown_report_handle(h, tmp):
    """Report items with unknown handles should exit 4."""
    report = make_report()
    report["items"].append({"handle": "X1", "title": "Unknown", "decision_or_recommendation": "N/A", "backed_by": []})
    src = write_json(tmp / "bad_handle.json", report)
    out = tmp / "bad_handle.out.json"
    proc = run_render(src, out)
    h.test_result("unknown report handle exits 4",
                  proc.returncode == 4 and not out.exists(),
                  f"exit {proc.returncode}, out exists: {out.exists()}")


def test_unreadable_context_path(h, tmp):
    """Unreadable context_path should warn and render with placeholder."""
    questions = [make_question(n, brief=False) for n in range(1, 3)]
    context_path = tmp / "nonexistent" / "context.md"
    data = make_form(questions, summary=None, context_path=str(context_path))
    src = write_json(tmp / "unreadable.json", data)
    out = tmp / "unreadable.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    h.test_result("unreadable context_path: render succeeds",
                  proc.returncode == 0, f"exit {proc.returncode}")
    h.test_result("unreadable context_path: placeholder preamble",
                  doc.get("batches", [{}])[0].get("preamble") == "(no context provided by the step)",
                  doc.get("batches", [{}])[0].get("preamble", ""))
    # Check for warning about containment or unreadable
    warnings = doc.get("warnings", [])
    has_warning = any("not under" in w or "unreadable" in w.lower() for w in warnings)
    h.test_result("unreadable context_path: warning recorded",
                  has_warning,
                  str(warnings))


def test_context_path_not_under_flows(h, tmp):
    """context_path outside ~/.claude/flows/ should warn and skip."""
    questions = [make_question(n, brief=False) for n in range(1, 3)]
    # Create a context file outside the flows directory
    external_dir = tmp / "external"
    external_dir.mkdir()
    external_path = external_dir / "context.md"
    external_path.write_text("EXTERNAL CONTEXT", encoding="utf-8")

    data = make_form(questions, summary=None, context_path=str(external_path))
    src = write_json(tmp / "external_ctx.json", data)
    out = tmp / "external_ctx.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    h.test_result("context_path outside flows: render succeeds",
                  proc.returncode == 0, f"exit {proc.returncode}")
    h.test_result("context_path outside flows: warning recorded",
                  any("not under" in w for w in doc.get("warnings", [])),
                  str(doc.get("warnings")))


def test_non_list_items(h, tmp):
    """Non-list items in report should exit 4."""
    data = make_report()
    data["items"] = "not a list"
    src = write_json(tmp / "bad_items.json", data)
    out = tmp / "bad_items.out.json"
    proc = run_render(src, out)
    h.test_result("non-list items exits 4",
                  proc.returncode == 4 and not out.exists(),
                  f"exit {proc.returncode}, out exists: {out.exists()}")


def test_none_placeholder(h, tmp):
    """None values should use placeholder text."""
    q1 = make_question(1)
    src = write_json(tmp / "none.json", make_form([q1]))
    out = tmp / "none.out.json"
    proc = run_render(src, out)
    h.test_result("None placeholder rendered",
                  proc.returncode == 0)


def test_prefix_boundary_12_chars(h, tmp):
    """Test exact 12-character header with prefix."""
    q1 = make_question(1)
    q1["header"] = "X"  # 1 char + 11 char prefix = exactly 12
    src = write_json(tmp / "boundary.json", make_form([q1]))
    out = tmp / "boundary.out.json"
    proc = run_render(src, out, prefix="#1 six78901")  # exactly 11 chars
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    header = doc.get("batches", [{}])[0].get("questions", [{}])[0].get("ask", {}).get("header", "")
    h.test_result("--prefix of exactly 11 characters produces exactly 12 char header",
                  proc.returncode == 0 and len(header) == 12,
                  f"exit {proc.returncode}, header len {len(header)}: {repr(header)}")


def test_header_truncate_word_boundary(h, tmp):
    """Header should truncate at word boundary when possible."""
    q1 = make_question(1)
    q1["header"] = "Long multi word header"
    src = write_json(tmp / "wordbound.json", make_form([q1]))
    out = tmp / "wordbound.out.json"
    # Prefix of 4 chars + space + truncate to 12
    proc = run_render(src, out, prefix="Q1 ")
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    header = doc.get("batches", [{}])[0].get("questions", [{}])[0].get("ask", {}).get("header", "")
    h.test_result("header truncates at word boundary",
                  proc.returncode == 0 and len(header) <= 12 and "…" in header,
                  f"header: {repr(header)}")


def test_brief_fenced_as_text(h, tmp):
    """Brief content should be fenced in code block."""
    q1 = make_question(1)
    q1["brief"] = "Some brief text without backticks"
    src = write_json(tmp / "brief_fence.json", make_form([q1]))
    out = tmp / "brief_fence.out.json"
    proc = run_render(src, out)
    doc = json.loads(out.read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    preamble = doc.get("batches", [{}])[0].get("preamble", "")
    # Brief should be fenced
    h.test_result("brief is fenced",
                  proc.returncode == 0 and "`" in preamble,
                  preamble)


def test_missing_out_flag(h, tmp):
    """Missing --out flag should exit 1 (usage error)."""
    q1 = make_question(1)
    src = write_json(tmp / "no_out.json", make_form([q1]))
    # Run without --out flag
    result = subprocess.run(
        [sys.executable, str(RENDER), str(src)],
        capture_output=True, text=True
    )
    h.test_result("missing --out flag exits 1",
                  result.returncode == 1,
                  f"expected exit 1, got {result.returncode}")


def main():
    h = Harness("FLOW RENDER QUESTIONS UNIT TESTS")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        test_form_batches(h, tmp)
        test_no_id_label(h, tmp)
        test_old_file_without_kind(h, tmp)
        test_null_context(h, tmp)
        test_brief_backtick_fence(h, tmp)
        test_report(h, tmp)
        test_prefix_cap(h, tmp)
        test_no_prefix_no_cap(h, tmp)
        test_failure_exits(h, tmp)
        test_empty_questions(h, tmp)
        test_malformed_options(h, tmp)
        test_non_string_header(h, tmp)
        test_unknown_report_handle(h, tmp)
        test_unreadable_context_path(h, tmp)
        test_context_path_not_under_flows(h, tmp)
        test_non_list_items(h, tmp)
        test_none_placeholder(h, tmp)
        test_prefix_boundary_12_chars(h, tmp)
        test_header_truncate_word_boundary(h, tmp)
        test_brief_fenced_as_text(h, tmp)
        test_missing_out_flag(h, tmp)
    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
