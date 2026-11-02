#!/usr/bin/env python3
"""
Doc and ADR contract for /expert-fleet (ADR-0025) and the fleet dispatcher's POC baseline.

Covers the documentation requirements from the expert-review action plan for the fleet PR:
  - ADR-0025 names every deferred finding, and every finding recorded for follow-up, in its gaps
    table (or on a line that points at #381).
  - The merge-gate Hold recovery is documented as `/expert-flow --resume <N>`, and the misleading
    "one answer away from merging" sentence is gone from commands/expert-flow.md.
  - The main.lock guarantee is described as fresh-vs-fresh only, in both the ADR and the command doc.
  - Free-text answers are framed with a fixed header, and the `--` argv question is recorded.
  - scripts/fleet.py is byte-identical to the POC base 8e97cbf (commit 982a75e on this branch).
  - Section 6 (#421): the fleet's 4(a)/4(b) relay slices, green on the current doc and red on the
    frozen pre-#421 fixture for the new-wording checks.
  - Section 7 (#421): both commands use the shared question renderer (flow-render-questions.py).

Bare `$1`-`$9` tokens in commands/*.md are enforced by tests/test_command_doc_shell_conventions.py
and are deliberately not repeated here.

Every check reads the real production file at run time, except the Section 6 red half, which reads
the frozen fixture tests/fixtures/expert-fleet-pre-421.md (`git show bbb84340:commands/expert-fleet.md`).
Run with:
  python3 tests/test_fleet_docs_contract.py
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

ADR_PATH = REPO_ROOT / "docs" / "adr" / "0025-expert-fleet.md"
FLEET_CMD_PATH = REPO_ROOT / "commands" / "expert-fleet.md"
FLOW_CMD_PATH = REPO_ROOT / "commands" / "expert-flow.md"
FLEET_PY_PATH = REPO_ROOT / "scripts" / "fleet.py"
PRE_421_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "expert-fleet-pre-421.md"
RENDERER_PATH = REPO_ROOT / "scripts" / "flow-render-questions.py"

POC_SHA = "982a75e"

RENDER_SCRIPT = "flow-render-questions.py"
RENDER_GRANT = 'Bash(python3 "$HOME/.claude/scripts/flow-render-questions.py":*)'
BASH_SENTENCE = "A Bash result is not shown to the human"

# Section 6 slice markers in commands/expert-fleet.md Step 4.4.
SEC_A_START = "**(a) Forms and gates.**"
SEC_B_START = "**(b) Deliver answers.**"
SEC_C_START = "**(c) Reports last.**"

# Section 6 checks whose new wording must be absent from the frozen pre-#421 fixture (D2, Q3).
RED_ON_FIXTURE = [
    "renderer_call",
    "same_turn",
    "bash_sentence",
    "skip_before_render",
    "echo",
    "no_id_label",
    "slim_inbox",
    "status_slim",
    "skipped_counts_running",
    "render_in_writes",
]

# Gaps the ADR-0025 gaps table must name (deferred findings, plus the "Doing it" findings recorded for
# follow-up). Each entry is (description, distinctive prose term). The term must appear in a markdown
# table row of the ADR; review-local finding labels (F1..F31) are not used in the ADR and are not keyed on.
GAP_TERMS = [
    ("gh_issue_closed lookup failures", "gh_issue_closed"),
    ("SETTLED constant unused", "SETTLED"),
    ("--max not validated", "`--max`"),
    ("question/report file containment", "containment"),
    ("answered-but-undelivered asks", "undelivered"),
    ("flocks not inherited by claude child", "Flocks are not inherited"),
    ("fleet.json schema_version", "schema_version"),
    ("no wall-clock bound", "wall-clock"),
    ("merge-gate Hold stranding", "Merge-gate Hold"),
    ("bypassPermissions on unattended agents", "bypassPermissions"),
    ("wait --since baseline", "`--since` baseline"),
    ("init validation tracebacks", "tracebacks"),
    ("unreadable state.json treated as no flow", "state.json"),
    ("stale session kept after unparsed run", "stale `session`"),
    ("find_flow counts older flows", "find_flow"),
    ("fleet.py outside mypy gate", "mypy"),
    ("run() mixes responsibilities", "mixes responsibilities"),
    ("lock handle never closed", "lock handle"),
    ("wait and run exit codes undocumented", "exit codes"),
]


def _between(text, start, end):
    """Text from start up to (not including) end; "" if either marker is missing or out of order."""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    if j < 0:
        return ""
    return text[i:j]


def _sentence_block(text, marker):
    """Text from marker up to the next bullet or heading line; "" if the marker is missing."""
    i = text.find(marker)
    if i < 0:
        return ""
    tail = text[i:]
    ends = [m.start() for m in re.finditer(r"\n(?:- |## )", tail)]
    return tail[: ends[0]] if ends else tail


def _section(text, marker):
    """Text from a heading marker up to the next heading line (any level); "" if missing."""
    i = text.find(marker)
    if i < 0:
        return ""
    tail = text[i:]
    m = re.search(r"\n#", tail)
    return tail[: m.start()] if m else tail


def _flat(text):
    """Collapse all whitespace (including the prose line wraps) so phrases match across a wrap."""
    return " ".join(text.split())


def section6_checks(text: str) -> Dict[str, bool]:
    """One named boolean per #421 4(a)/4(b) check, read from the full text of expert-fleet.md.

    Slices are whitespace-flattened before phrase matching, because the command doc wraps prose
    across lines (e.g. "previous\\n   turn"). Never raises: a missing slice marker makes every check
    that needs that slice False.
    """
    a = _flat(_between(text, SEC_A_START, SEC_B_START))
    b_raw = _between(text, SEC_B_START, SEC_C_START)
    b = _flat("\n".join(line for line in b_raw.splitlines() if "FLEET-REPLY" not in line))
    step41 = _flat(_between(text, "## Step 4: Dispatch loop", "2. **Launch**"))
    status = _flat(_between(text, "`--status` / `--resume`: list", "Otherwise, a new fleet:"))
    item4 = _flat(_between(text, "4. **Inbox.**", "   - **(a) Forms and gates.**"))
    step45 = _flat(_between(text, "## Step 4: Dispatch loop", "## What this command deliberately does not do"))
    writes = _flat(_sentence_block(text, "Its own writes are under"))

    renderer_i = a.find(RENDER_SCRIPT)
    skip_i = a.find("**Skip delivered tickets first.**")
    relay_i = a.find("**Relay items")
    skip_clause = a[skip_i:relay_i] if 0 <= skip_i < relay_i else ""

    return {
        "renderer_call": all(s in a for s in (RENDER_SCRIPT, "--out", "--prefix", "Read")),
        "assistant_message": "assistant message" in a and "before" in a,
        "same_turn": "same turn" in a,
        "mandatory": all(s in a for s in ("mandatory", "never folded", "summarised")),
        "bash_sentence": BASH_SENTENCE in a,
        "one_ticket": "another ticket" in a,
        "skip_before_render": bool(skip_clause) and "running" in skip_clause
        and "previous turn" in skip_clause and renderer_i > skip_i,
        "failure_rule": "left parked" in a and "Run /setup-local" in a,
        "no_id_label": "carries no `<id>:` label" in a,
        "gate_prefix": "Gate items" in a and "header prefixed `#N`" in a,
        "copied_unchanged": "copied unchanged" in a,
        "echo": "delivering #N: <header>: <label>" in b,
        "slim_inbox": "del(.question_data)" in step41,
        "status_slim": "del(.question_data)" in status,
        "reported_not_idle_block": "does not count as open inbox" in step45
        and "reaches `fleet: idle`" in step45,
        "skipped_counts_running": "previous turn" in item4 and "running" in item4 and "Step 5" in item4,
        "render_in_writes": "FLEET/render/" in writes,
    }


def table_rows(text):
    """Markdown table rows of the ADR (lines whose first non-space character is '|')."""
    return [line for line in text.splitlines() if line.lstrip().startswith("|")]


def finding_pattern(fid):
    """Match the finding id as a whole token: F1 must not match F10."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(fid)}(?![0-9])")


def recorded_lines(text, fid):
    """Lines recording a finding: a markdown table row naming it, or any line citing #381."""
    pat = finding_pattern(fid)
    hits = []
    for line in text.splitlines():
        if not pat.search(line):
            continue
        if line.lstrip().startswith("|") or "#381" in line:
            hits.append(line.strip())
    return hits


def main():
    h = Harness("EXPERT-FLEET DOCS CONTRACT TEST SUITE")

    print()
    print("[Section 0] Required files exist")

    required = {
        "docs/adr/0025-expert-fleet.md": ADR_PATH,
        "commands/expert-fleet.md": FLEET_CMD_PATH,
        "commands/expert-flow.md": FLOW_CMD_PATH,
        "scripts/fleet.py": FLEET_PY_PATH,
    }
    missing = [name for name, p in required.items() if not p.is_file()]
    h.test_result(
        "ADR, command docs and scripts/fleet.py exist",
        not missing,
        f"missing: {', '.join(missing)}" if missing else "",
    )
    if missing:
        h.summarize_and_exit()

    adr = ADR_PATH.read_text()
    fleet_cmd = FLEET_CMD_PATH.read_text()
    flow_cmd = FLOW_CMD_PATH.read_text()

    print()
    print("[Section 1] ADR-0025 gaps table names every deferred and recorded finding")

    rows = table_rows(adr)
    unrecorded = [desc for desc, term in GAP_TERMS if not any(term in row for row in rows)]
    h.test_result(
        f"ADR-0025 gaps table names all {len(GAP_TERMS)} gap findings (by prose term in a table row)",
        not unrecorded,
        f"no gaps-table row for: {'; '.join(unrecorded)}" if unrecorded else "",
    )

    print()
    print("[Section 2] Hold recovery is documented; the 'one answer away' claim is corrected")

    stale_claim = "one answer away from merging"
    h.test_result(
        "commands/expert-flow.md no longer claims a held PR is 'one answer away from merging'",
        stale_claim not in flow_cmd,
        f"stale sentence still present: '{stale_claim}'" if stale_claim in flow_cmd else "",
    )

    resume_cmd = "/expert-flow --resume <N>"
    fleet_lines = fleet_cmd.splitlines()
    resume_idxs = [i for i, line in enumerate(fleet_lines) if resume_cmd in line]
    h.test_result(
        f"commands/expert-fleet.md documents `{resume_cmd}` as the recovery",
        bool(resume_idxs),
        f"`{resume_cmd}` not found" if not resume_idxs else "",
    )

    # The recovery must sit beside the Hold explanation: a 'held' mention within 10 lines of it.
    near_held = any(
        "held" in " ".join(fleet_lines[max(0, i - 10): i + 11]).lower()
        for i in resume_idxs
    )
    h.test_result(
        f"`{resume_cmd}` appears within 10 lines of a 'held' (Hold) explanation",
        near_held,
        "no 'held' within 10 lines of the resume recovery" if not near_held else "",
    )

    print()
    print("[Section 3] main.lock guarantee is described as fresh-vs-fresh only")

    fresh_pat = re.compile(r"fresh[- ]vs[- ]fresh", re.IGNORECASE)
    h.test_result(
        "ADR-0025 describes the main.lock guarantee as fresh-vs-fresh",
        bool(fresh_pat.search(adr)),
        "no 'fresh-vs-fresh' wording in the ADR" if not fresh_pat.search(adr) else "",
    )
    h.test_result(
        "commands/expert-fleet.md describes the main.lock guarantee as fresh-vs-fresh",
        bool(fresh_pat.search(fleet_cmd)),
        "no 'fresh-vs-fresh' wording in the command doc" if not fresh_pat.search(fleet_cmd) else "",
    )

    print()
    print("[Section 4] Free-text answers: fixed header and the `--` argv question")

    header_pat = re.compile(r"\bheader\b", re.IGNORECASE)
    h.test_result(
        "commands/expert-fleet.md frames free-text answers with a fixed header",
        bool(header_pat.search(fleet_cmd)),
        "no 'header' wording in the command doc" if not header_pat.search(fleet_cmd) else "",
    )

    # F17 is the `--` question: either recorded for #381 in the ADR, or handled by the fixed header
    # asserted above. Either route satisfies the plan, so accept either here.
    f17_recorded = bool(recorded_lines(adr, "F17"))
    f17_handled = bool(header_pat.search(fleet_cmd))
    h.test_result(
        "F17 (free-text answer as bare argv) is recorded in ADR-0025 or handled by a fixed header",
        f17_recorded or f17_handled,
        "neither an ADR-0025 F17 record nor a fixed header in the command doc" if not (f17_recorded or f17_handled) else "",
    )

    print()
    print(f"[Section 5] scripts/fleet.py is byte-identical to POC commit {POC_SHA}")

    try:
        poc = subprocess.run(
            ["git", "show", f"{POC_SHA}:scripts/fleet.py"],
            cwd=REPO_ROOT, capture_output=True, timeout=60,
        )
        poc_ok = poc.returncode == 0
        poc_bytes = poc.stdout
        poc_err = poc.stderr.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        poc_ok, poc_bytes, poc_err = False, b"", str(exc)

    h.test_result(
        f"git can read scripts/fleet.py at {POC_SHA}",
        poc_ok,
        poc_err,
    )

    current = FLEET_PY_PATH.read_bytes()
    same = poc_ok and current == poc_bytes
    detail = ""
    if poc_ok and not same:
        cur_lines = current.decode(errors="replace").splitlines()
        poc_lines = poc_bytes.decode(errors="replace").splitlines()
        first = next(
            (i + 1 for i, (a, b) in enumerate(zip(cur_lines, poc_lines)) if a != b),
            min(len(cur_lines), len(poc_lines)) + 1,
        )
        detail = f"differs from {POC_SHA} starting at line {first}"
    h.test_result(
        f"scripts/fleet.py matches {POC_SHA} byte for byte",
        same,
        detail,
    )

    print()
    print("[Section 9] #417 check-command preflight and deps chore in commands/expert-fleet.md")

    fm_match = re.search(r"^allowed-tools:\s*(.*)$", fleet_cmd, re.MULTILINE)
    fm_tools = fm_match.group(1) if fm_match else ""
    h.test_result(
        "expert-fleet grants checks ensure-check and checks record-check-answer",
        "Bash(python3 -m scripts.workflow.cli checks ensure-check:*)" in fm_tools
        and "Bash(python3 -m scripts.workflow.cli checks record-check-answer:*)" in fm_tools,
    )
    pkg_grant = re.search(r"Bash\((npm|pnpm|yarn|bun)\b", fm_tools)
    h.test_result(
        "expert-fleet grants no package-manager command",
        pkg_grant is None,
        f"found grant: {pkg_grant.group(0)}" if pkg_grant else "",
    )

    preflight_idx = fleet_cmd.find("## Check-command preflight (before Step 3)")
    launch_idx = fleet_cmd.find("## Step 3: Launch")
    h.test_result(
        "Check-command preflight section sits before ## Step 3: Launch",
        preflight_idx >= 0 and launch_idx > preflight_idx,
    )
    preflight = fleet_cmd[preflight_idx:launch_idx] if preflight_idx >= 0 and launch_idx > preflight_idx else ""
    h.test_result(
        "preflight resolves with checks ensure-check against MAIN as worktree and main worktree",
        'checks ensure-check --worktree "<MAIN>" --main-worktree "<MAIN>"' in preflight,
    )
    h.test_result(
        "preflight refuses to launch and says not to run fleet.py run",
        "**Refuse to launch**" in preflight and "Do not run `fleet.py run`" in preflight,
    )
    h.test_result(
        "preflight question uses header epic and joins the --prepass batch",
        "header `epic`" in preflight and "--prepass" in preflight,
    )
    h.test_result(
        "preflight persists with checks record-check-answer",
        "checks record-check-answer --main-worktree \"<MAIN>\"" in preflight,
    )
    step3 = fleet_cmd[launch_idx:]
    h.test_result(
        "Step 3 runs the preflight before fleet.py ready",
        "Run the Check-command preflight above, then `fleet.py ready FLEET`." in step3,
    )
    step2_idx = fleet_cmd.find("## Step 2: Big-stones pre-pass")
    h.test_result(
        "Step 2 runs preflight step 1 before the first AskUserQuestion",
        step2_idx >= 0 and "step 1 of the Check-command preflight" in fleet_cmd[step2_idx:launch_idx],
    )
    deps_idx = fleet_cmd.find("**Deps chore inside tickets.**")
    deps = fleet_cmd[deps_idx:fleet_cmd.find("## What this command deliberately does not do")] if deps_idx >= 0 else ""
    deps_flat = " ".join(deps.split())
    h.test_result("deps chore paragraph exists", deps_idx >= 0)
    h.test_result(
        "successful deps results never park",
        "A successful result" in deps_flat and "never parks" in deps_flat,
    )
    h.test_result(
        "failed and manifest-changed deps results park as a gate",
        "`failed`" in deps_flat and "`manifest-changed`" in deps_flat and "`gate`" in deps_flat,
    )
    h.test_result(
        "deps chore inherits #382 and moves to #370's Validate phase",
        "#382" in deps_flat and "#370" in deps_flat,
    )
    h.test_result(
        "deliberate-not-do bullet carves out checks record-check-answer",
        "checks record-check-answer" in fleet_cmd[fleet_cmd.find("## What this command deliberately does not do"):],
    )

    print("[Section 6] Fleet 4(a)/4(b) rewrite (#421): green on the current doc")

    current_checks = section6_checks(fleet_cmd)
    for name, passed in current_checks.items():
        h.test_result(f"expert-fleet.md section 6 check: {name}", passed)

    print()
    print("[Section 6b] Fleet 4(a)/4(b) rewrite (#421): red on the frozen pre-#421 fixture")

    fixture_ok = PRE_421_FIXTURE_PATH.is_file()
    h.test_result(
        "frozen fixture tests/fixtures/expert-fleet-pre-421.md exists",
        fixture_ok,
        f"missing: {PRE_421_FIXTURE_PATH}" if not fixture_ok else "",
    )
    if fixture_ok:
        fixture_checks = section6_checks(PRE_421_FIXTURE_PATH.read_text())
        for name in RED_ON_FIXTURE:
            got = fixture_checks[name]
            h.test_result(
                f"pre-#421 fixture: new-wording check '{name}' is False",
                not got,
                "check is True on the pre-change text, so it does not discriminate" if got else "",
            )

    print()
    print("[Section 7] Both commands use the shared question renderer (#421)")

    fleet_tools = re.search(r"^allowed-tools:(.*)$", fleet_cmd, re.MULTILINE)
    flow_tools = re.search(r"^allowed-tools:(.*)$", flow_cmd, re.MULTILINE)
    h.test_result(
        "commands/expert-fleet.md frontmatter grants the renderer",
        bool(fleet_tools) and RENDER_GRANT in fleet_tools.group(1),
    )
    h.test_result(
        "commands/expert-flow.md frontmatter grants the renderer",
        bool(flow_tools) and RENDER_GRANT in flow_tools.group(1),
    )

    report_sec = _between(flow_cmd, "#### Report-kind questions", "#### Form-kind questions")
    form_sec = _section(flow_cmd, "#### Form-kind questions")
    h.test_result(
        "expert-flow 'Report-kind questions' section names the renderer",
        RENDER_SCRIPT in report_sec,
        "section missing or no renderer call" if RENDER_SCRIPT not in report_sec else "",
    )
    h.test_result(
        "expert-flow 'Form-kind questions' section names the renderer",
        RENDER_SCRIPT in form_sec,
        "section missing or no renderer call" if RENDER_SCRIPT not in form_sec else "",
    )

    # The Park bullet says "by running the renderer" (the script is named in the relay bullet above
    # it), so the check is on the word "renderer", as the plan's "names the renderer" reads.
    park_report = _flat(_between(flow_cmd, "`report` — a plan checkpoint report", "`gate` — one of this"))
    h.test_result(
        "expert-flow fleet Park 'report' bullet names the renderer and fleet-report-<SEQ>.md",
        "renderer" in park_report and "fleet-report-<SEQ>.md" in park_report,
        "Park report bullet missing" if not park_report else "",
    )

    for label, sec in (("Report-kind questions", report_sec), ("Form-kind questions", form_sec)):
        flat_sec = _flat(sec)
        h.test_result(
            f"expert-flow '{label}' maps renderer exit 3 or 4 to 'question file unreadable'",
            "exit 3 or 4" in flat_sec and "question file unreadable" in flat_sec,
        )
        h.test_result(
            f"expert-flow '{label}' does not claim the invalid-file case 'cannot occur'",
            bool(flat_sec) and "cannot occur" not in flat_sec,
        )

    render_calls = [line for line in form_sec.splitlines() if RENDER_SCRIPT in line]
    h.test_result(
        "expert-flow form-kind renderer call passes no --prefix",
        bool(render_calls) and all("--prefix" not in line for line in render_calls),
        f"calls: {render_calls}",
    )
    h.test_result("scripts/flow-render-questions.py exists", RENDERER_PATH.is_file())
    h.test_result("expert-flow.md says 'Six added script grants'",
                  "Six added script grants" in flow_cmd)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
