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

Bare `$1`-`$9` tokens in commands/*.md are enforced by tests/test_command_doc_shell_conventions.py
and are deliberately not repeated here.

Every check reads the real production file at run time. Run with:
  python3 tests/test_fleet_docs_contract.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

ADR_PATH = REPO_ROOT / "docs" / "adr" / "0025-expert-fleet.md"
FLEET_CMD_PATH = REPO_ROOT / "commands" / "expert-fleet.md"
FLOW_CMD_PATH = REPO_ROOT / "commands" / "expert-flow.md"
FLEET_PY_PATH = REPO_ROOT / "scripts" / "fleet.py"

POC_SHA = "982a75e"

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

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
