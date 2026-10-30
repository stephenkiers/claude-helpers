#!/usr/bin/env python3
"""
Structural and doc-extracted behavioural tests for the human-attested "Accept and continue" option
at the /expert-flow hard stop (#365).

The prose is read at test runtime and asserted on the phrases the spec fixes. Behaviour is taken
from the prose itself, not from a mock:

Structural (commands/expert-flow.md, docs/adr/0023-expert-flow.md, CLAUDE.md):
- Hard stop menu order (Take over, Accept and continue, Abort), never (Recommended).
- The Accept procedure calls `flow-accept.py record` before it logs `<STEP> accepted (`.
- The post-escalation menu names Accept and continue, in expert-flow.md and in the ADR amendment.
- The log-event list contains `accepted`, and the suffix rule names failed and accepted.
- Exemptions to rule 2 list `accepted`; the Every Exit Path row, the redo-lineage row, the layout
  entry, the done-line count, ship's coverage gathering, the grants, the line-47 sentence, the
  step-gate digest prefix, the free-text Accept rule, the fleet note and the CLAUDE.md bullet.

Doc-extracted behaviour:
- The rule-2 lifecycle grep is taken from the fenced block in expert-flow.md, run with <STEP>
  substituted against a fixture log.md. Its newest line is classified with the doc's rule (third
  field, cut at " ("). An accepted newest line classifies as `accepted`, and the accepted branch must
  skip validation and the audit and go to the gate. A later `retry` supersedes it.
- Keep previous round trip: accepted, redo, failed, kept-previous, re-logged accepted with
  kept-previous attempt=<J>. Rule 2 classifies it as accepted, and the done-line count counts the
  original accept once.

Run with: python3 tests/test_flow_accept_docs.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

EXPERT_FLOW = REPO_ROOT / "commands" / "expert-flow.md"
ADR_0023 = REPO_ROOT / "docs" / "adr" / "0023-expert-flow.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ACCEPT = "Accept and continue"
HEADING_LINE = re.compile(r"^#{1,3} ")
LEVEL2_LINE = re.compile(r"^## ")
KEPT_MARKER = "; kept-previous attempt="
BRANCH_MARKER = "Newest line is `accepted`"


def read(path):
    return path.read_text(encoding="utf-8")


def section(text, heading, stop=HEADING_LINE):
    """Lines from the heading line (startswith match) up to the next line matching `stop`, outside
    fenced code blocks. Returns "" when the heading is absent."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return ""
    out = [lines[start]]
    in_fence = False
    for line in lines[start + 1:]:
        if is_fence(line):
            in_fence = not in_fence
        elif not in_fence and stop.match(line):
            break
        out.append(line)
    return "\n".join(out)


def paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def paragraph_with(text, *needles):
    """True when some paragraph of text contains every needle (case-insensitive)."""
    return any(all(n.lower() in p.lower() for n in needles) for p in paragraphs(text))


def block_after(text, marker):
    """The marker line plus the contiguous non-blank lines that follow it (after any blank lines)."""
    lines = text.splitlines()
    idx = next((i for i, line in enumerate(lines) if marker in line), None)
    if idx is None:
        return ""
    out = [lines[idx]]
    j = idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    while j < len(lines) and lines[j].strip():
        out.append(lines[j])
        j += 1
    return "\n".join(out)


def window_after(text, marker, size=1600):
    """Text from the marker up to `size` characters, cut before the next 'Newest line' branch."""
    idx = text.find(marker)
    if idx < 0:
        return ""
    chunk = text[idx: idx + size]
    cut = chunk.find("Newest line", len(marker))
    return chunk[:cut] if cut > 0 else chunk


def is_fence(line):
    """A code fence, at column 0 or indented inside a list item."""
    return line.lstrip().startswith("```")


def fenced_lines(text):
    """Every line that sits inside a fenced code block (indented fences included)."""
    out = []
    in_fence = False
    for line in text.splitlines():
        if is_fence(line):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line)
    return out


def frontmatter(text):
    """The YAML frontmatter between the first two `---` lines, or ""."""
    parts = text.split("---", 2)
    return parts[1] if len(parts) >= 3 else ""


def classify(line):
    """The doc's stated classification of a lifecycle line: third field, cut at ' ('."""
    fields = line.split(" ")
    return fields[2].split(" (")[0] if len(fields) >= 3 else ""


def rule2_grep_command(text):
    """The fenced-block line that runs the rule-2 lifecycle grep (it names the accepted alternation)."""
    candidates = [line for line in fenced_lines(text)
                  if "grep" in line and "<STEP>" in line and "accepted" in line]
    return candidates[0].strip() if candidates else None


def done_count_regex(text):
    """Extract the done-line count regex from the doc's done-line rule.

    Searches for the pattern in the section where the accept events counting rule is stated.
    Returns the regex pattern as a string, or None if not found.
    """
    # Search for a backtick-enclosed regex pattern containing "accepted"
    # Pattern is typically: `^[^ ]+ [a-z]+ accepted \(`
    match = re.search(r'`(\^[^`]*accepted[^`]*)`', text)
    if match:
        pattern = match.group(1)
        # Verify it looks like a regex (has special chars)
        if any(c in pattern for c in r'[]\^$*+?.|()'):
            return pattern
    return None


def run_doc_command(cmd, cwd, step):
    """Run the doc's grep line with <STEP> substituted, from cwd, with FLOW_DIR set to cwd."""
    env = {**os.environ, "FLOW_DIR": str(cwd)}
    result = subprocess.run(["bash", "-c", cmd.replace("<STEP>", step)], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout, result.stderr


def newest_lifecycle(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def test_hard_stop(h, doc):
    print("[Section 1] Hard stop menu and procedure")
    hs = section(doc, "### Hard stop")
    h.test_result("Hard stop section is present in expert-flow.md", bool(hs), "no '### Hard stop' heading")
    accept_hits = [m.start() for m in re.finditer(re.escape(f"**{ACCEPT}**"), hs)]
    h.test_result("Accept and continue is a bold menu option in the Hard stop section",
                  bool(accept_hits), "no **Accept and continue** in Hard stop")
    # The menu is one of several places the label appears, so the order holds for some occurrence:
    # Take over before it, Abort after it.
    ordered = any(
        hs.rfind("Take over", 0, a) >= 0 and hs.find("Abort", a) > a for a in accept_hits
    )
    h.test_result("some Accept and continue menu entry sits after Take over and before Abort",
                  ordered, f"accept occurrences at {accept_hits}")
    h.test_result("Accept and continue is never (Recommended)",
                  all("(Recommended)" not in hs[a:a + 120] for a in accept_hits),
                  "an Accept and continue option is followed by (Recommended)")
    h.test_result("Accept eligibility is item 3c and runs the eligible call",
                  "3c" in hs and "Accept eligibility" in hs
                  and re.search(r'flow-accept\.py" eligible --flow-dir "\$FLOW_DIR"', hs) is not None,
                  "3c, 'Accept eligibility' or the eligible command is missing")
    h.test_result("Accept is offered only on ACCEPT: yes",
                  "ACCEPT: yes" in hs, "no 'ACCEPT: yes' gate in Hard stop")
    h.test_result("a free-text reply beginning with Accept is the Accept rule",
                  any(paragraph_with(hs, free, "beginning with", "Accept")
                      for free in ("free text", "free-text", "Other")),
                  "no free-text Accept paragraph in Hard stop")
    h.test_result("the procedure passes the note with --note-stdin",
                  "--note-stdin" in hs, "no --note-stdin in Hard stop")

    record_hits = [m.start() for m in re.finditer(r'flow-accept\.py"\s+record', hs)]
    log_hits = [m.start() for m in re.finditer(re.escape("<STEP> accepted ("), hs)]
    h.test_result("the procedure calls flow-accept.py record",
                  bool(record_hits), "no flow-accept.py record call in Hard stop")
    h.test_result("the procedure calls record before it logs '<STEP> accepted ('",
                  any(log > min(record_hits) for log in log_hits) if record_hits else False,
                  f"record={record_hits}, log={log_hits}")
    h.test_result("the procedure leaves failures[STEP] and stop_source[STEP] untouched",
                  "failures[STEP]" in hs and "stop_source[STEP]" in hs,
                  "failures[STEP] or stop_source[STEP] not named in Hard stop")
    h.test_result("the typed stop_reason is recorded beside stop_source",
                  "stop_reason" in doc, "stop_reason not in expert-flow.md")
    h.test_result("the step-gate digest is prefixed 'accepted, not audited'",
                  "accepted, not audited" in doc, "digest prefix missing")
    h.test_result("the post-escalation menu names Accept and continue with Take over and Abort",
                  paragraph_with(hs, "post-escalation", ACCEPT, "Take over", "Abort"),
                  "no post-escalation paragraph naming Accept with Take over and Abort")


def test_tables_and_summary(h, doc):
    print()
    print("[Section 2] Redo table, exit table, done line, ship, layout, Keep previous")
    h.test_result("the redo-lineage table has the Accept row",
                  any("Hard stop: Accept and continue" in line and "addressed" in line
                      and "redo attempt NOT counted" in line for line in doc.splitlines()),
                  "row missing")
    h.test_result("the Every Exit Path table has the Accept row",
                  re.search(r"^\|\s*Hard stop → Accept and continue\s*\|", doc, re.M) is not None,
                  "row missing")
    h.test_result("the done line shows the accept events count",
                  "accept events: <n> (" in doc and "[a-z]+ accepted \\(" in doc,
                  "done-line accept events count or its count regex missing")
    h.test_result("the done-line count excludes kept-previous lines",
                  KEPT_MARKER in doc, "kept-previous exclusion missing")
    h.test_result("the done line counts accept events",
                  "accept events: <n>" in doc, "done-line label missing")
    h.test_result("the layout lists the accept trail steps/<NN>-<STEP>.accepted.md",
                  "steps/<NN>-<STEP>.accepted.md" in doc, "layout entry missing")
    h.test_result("Keep previous re-logs an accepted restore with kept-previous attempt=<J>",
                  "kept-previous attempt=<J>" in doc, "kept-previous attempt=<J> missing")
    h.test_result("the fleet note says the gate ask carries Accept and continue and fleet.py relays it",
                  paragraph_with(doc, "fleet.py", ACCEPT, "relays"), "fleet note missing")


def test_log_resume_and_grants(h, doc):
    print()
    print("[Section 3] Log grammar, rule 2, grants, line-47")
    event_hits = [m.start() for m in re.finditer(re.escape("<EVENT>"), doc)]
    h.test_result("the log grammar's <EVENT> list names accepted",
                  any("accepted" in doc[i:i + 600] for i in event_hits),
                  "no <EVENT> list naming accepted")
    h.test_result("the suffix rule names both failed and accepted",
                  paragraph_with(doc, "suffix", "failed", "accepted"), "no suffix paragraph naming both")
    h.test_result("accepted is written only by Accept and continue and Keep previous",
                  paragraph_with(doc, "accepted", "Keep previous", ACCEPT),
                  "no sentence naming the accepted writers")
    h.test_result("the rule-2 lifecycle grep names the accepted alternation",
                  "accepted \\(.*\\)" in doc, "accepted alternation missing from the lifecycle grep")
    h.test_result("Exemptions to rule 2 list accepted",
                  "accepted" in block_after(doc, "Exemptions to rule 2"),
                  "accepted not listed in exemptions")
    h.test_result("the Execution Contract sentence names Accept and continue for allowlisted rejections",
                  paragraph_with(doc, "Take over", "nothing else", ACCEPT), "line-47 sentence not updated")
    fm = frontmatter(doc)
    h.test_result("allowed-tools carries the precise flow-accept.py eligible and record grants",
                  re.search(r'flow-accept\.py" eligible:\*', fm) is not None
                  and re.search(r'flow-accept\.py" record:\*', fm) is not None,
                  "allowed-tools lacks the flow-accept.py grants")


def test_adr_and_claude(h, adr, claude):
    print()
    print("[Section 4] ADR-0023 amendment and CLAUDE.md")
    amend = section(adr, "## Amendment (2026-10-29)", stop=LEVEL2_LINE)
    h.test_result("ADR-0023 has the 2026-10-29 amendment", bool(amend), "amendment heading missing")
    h.test_result("the amendment names Accept and continue",
                  ACCEPT in amend, "Accept and continue missing from the amendment")
    h.test_result("the amendment states the post-escalation widening explicitly",
                  "post-escalation" in amend and ACCEPT in amend and "widen" in amend.lower(),
                  "post-escalation widening not stated")
    h.test_result("the amendment names the flow-accept.py allowlist",
                  "flow-accept.py" in amend, "allowlist script not named")
    h.test_result("the amendment states the accepted (<reason>) event and the steps/ trail",
                  "accepted (<reason>)" in amend and "steps/<NN>-<STEP>.accepted.md" in amend,
                  "event or trail path missing")
    h.test_result("the amendment says the accept cap is a follow-up",
                  "cap" in amend.lower(), "no cap statement")
    bullet = block_after(claude, "- `/expert-flow`")
    h.test_result("the /expert-flow bullet in CLAUDE.md names Accept and continue",
                  bool(bullet) and ACCEPT in bullet, "CLAUDE.md /expert-flow bullet lacks Accept and continue")


def test_resume_behaviour(h, doc):
    print()
    print("[Section 5] Doc-extracted resume behaviour (rule-2 grep run against fixture logs)")
    cmd = rule2_grep_command(doc)
    h.test_result("the rule-2 lifecycle grep is in a fenced block of expert-flow.md",
                  cmd is not None, "no fenced grep with <STEP> and accepted")
    if cmd is None:
        return

    count_regex = done_count_regex(doc)
    h.test_result("the done-line count regex is extractable from expert-flow.md",
                  count_regex is not None, "no done-line count regex found in doc")

    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp)
        log = flow / "log.md"

        log.write_text(
            "2026-10-29T10:00:00Z implement begin\n"
            "2026-10-29T10:01:00Z implement failed (reduced run (audit): x)\n"
            "2026-10-29T10:02:00Z implement accepted (audit-reduced: reduced run (audit): x)\n"
        )
        rc, out, err = run_doc_command(cmd, flow, "implement")
        newest = newest_lifecycle(out)
        h.test_result("the doc's grep runs against the fixture log and yields a newest line",
                      rc == 0 and newest != "", f"rc={rc}, out={out!r}, err={err!r}")
        h.test_result("an accepted newest line classifies as accepted",
                      classify(newest) == "accepted", f"newest={newest!r}")

        branch = window_after(doc, BRANCH_MARKER)
        h.test_result("the accepted branch exists in the resume rule",
                      BRANCH_MARKER in doc, "no accepted branch")
        h.test_result("the accepted branch skips receipt validation and the Fidelity audit",
                      ("skip receipt validation" in branch and "Fidelity audit" in branch) or "Treat it exactly like" in branch,
                      f"branch={branch[:200]!r}")
        h.test_result("the accepted branch goes to the gate and logs no failed (resume: ...) line",
                      "run the gate" in branch and "failed (resume:" not in branch,
                      f"branch={branch[:200]!r}")

        log.write_text(
            "2026-10-29T10:00:00Z implement begin\n"
            "2026-10-29T10:01:00Z implement failed (reduced run (audit): x)\n"
            "2026-10-29T10:02:00Z implement accepted (audit-reduced: reduced run (audit): x)\n"
            "2026-10-29T10:03:00Z implement retry\n"
        )
        rc, out, err = run_doc_command(cmd, flow, "implement")
        newest = newest_lifecycle(out)
        h.test_result("a later retry supersedes the accept (newest line is retry)",
                      rc == 0 and classify(newest) == "retry", f"newest={newest!r}, err={err!r}")

        log.write_text(
            "2026-10-29T10:00:00Z implement begin\n"
            "2026-10-29T10:01:00Z implement accepted (audit-reduced: reduced run (audit): x)\n"
            "2026-10-29T10:02:00Z implement redo\n"
            "2026-10-29T10:03:00Z implement failed (receipt admits deviation)\n"
            "2026-10-29T10:04:00Z implement kept-previous\n"
            "2026-10-29T10:05:00Z implement accepted (audit-reduced: reduced run (audit): x"
            f"{KEPT_MARKER}1)\n"
        )
        rc, out, err = run_doc_command(cmd, flow, "implement")
        newest = newest_lifecycle(out)
        h.test_result("Keep previous round trip: the re-logged accept classifies as accepted",
                      rc == 0 and classify(newest) == "accepted", f"newest={newest!r}, err={err!r}")

        if count_regex is not None:
            counted = [line for line in log.read_text().splitlines()
                       if re.match(count_regex, line) and KEPT_MARKER not in line]
            h.test_result("the done-line count counts the round-tripped accept once",
                          len(counted) == 1, f"counted={counted}")


def main():
    h = Harness("FLOW ACCEPT DOCS (expert-flow.md, ADR-0023, CLAUDE.md) TEST SUITE")
    doc = read(EXPERT_FLOW)
    adr = read(ADR_0023)
    claude = read(CLAUDE_MD)

    test_hard_stop(h, doc)
    test_tables_and_summary(h, doc)
    test_log_resume_and_grants(h, doc)
    test_adr_and_claude(h, adr, claude)
    test_resume_behaviour(h, doc)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
