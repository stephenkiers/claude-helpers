#!/usr/bin/env python3
"""
Cross-file command-line contract for the redo-with-notes scripts (#286).

Every check runs the real scripts (scripts/flow-redo.py, scripts/flow-ledger.py) as subprocesses.

1. Documented invocations: every `flow-redo.py` / `flow-ledger.py` call shape written in
   commands/expert-flow.md, prompts/flow-reference.md, commands/expert-plan.md and
   commands/expert-review.md is parsed out of the prose. Its placeholders are filled with fixture
   values and it runs against a fresh fixture flow dir. Each call must use only flags that the
   script's --help declares, carry --flow-dir (sanitize takes --file instead), exit with a code the
   script documents (never argparse's 2), and a `--by` flag only with `--status addressed`.
2. HUMAN_NOTE block: the fence and preamble documented in prompts/flow-reference.md are the ones the
   sanitiser enforces (fence tokens are stripped from note text) and round-trip through the block.
3. End-to-end walk: sanitise a note, append it as a direction record, archive attempt J, confirm
   `check` passes for exactly J and fails for J+1 and for an audit-file look-alike, restore, and
   confirm the receipt text and the printed attempt. Archive refuses a mismatched attempt.

Run with: python3 tests/test_flow_command_contract.py
"""

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

FLOW_REDO = REPO_ROOT / "scripts" / "flow-redo.py"
FLOW_LEDGER = REPO_ROOT / "scripts" / "flow-ledger.py"
FLOW_REFERENCE = REPO_ROOT / "prompts" / "flow-reference.md"
SCRIPTS = {"flow-redo": FLOW_REDO, "flow-ledger": FLOW_LEDGER}
DOC_FILES = [
    REPO_ROOT / "commands" / "expert-flow.md",
    FLOW_REFERENCE,
    REPO_ROOT / "commands" / "expert-plan.md",
    REPO_ROOT / "commands" / "expert-review.md",
]
SUBCOMMANDS = {
    "flow-redo": ("sanitize", "archive", "restore", "check"),
    "flow-ledger": ("append", "mark", "list"),
}
# Exit codes each script documents. Code 2 (argparse usage error) is never acceptable in a documented call.
EXIT_CODES = {
    "flow-redo": {0, 1, 3, 4, 5},
    "flow-ledger": {0, 1},
}
# Fixture values for the placeholders used in the documented call shapes. An unmapped <placeholder>
# fails the contract: a new placeholder is a new documented shape that this table must cover.
PLACEHOLDERS = {
    "<NN>": "01",
    "<STEP>": "plan",
    "<seq>": "1",
    "<k>": "2",
    "<J>": "1",
    "<ATTEMPT>": "1",
    "<attempts[STEP]>": "1",
    "<KIND>": "answer",
}
# A documented call ends at a heredoc, a stdin or stdout redirection, a pipe, or a chain operator.
CALL = re.compile(r'flow-(redo|ledger)\.py"?[ \t]+([a-z]+)([^\n`]*)')
CUT = re.compile(r'\s<<|\s<\s|\s>\s|\s>"|\s\|\s|&&|;')
VALID_RECEIPT = "ATTEMPT=1\nFINAL_PLAN_PATH: /path/to/plan\nREVIEW_DIR: /path/to/review\nDecision: OK\n<!-- step-end -->\n"
RAW_NOTE = "Tighten the API section.\n"


def run_script(key, argv, stdin=None):
    """Run the real script `key` with argv. stdin=None gives an empty stdin."""
    kwargs = {"capture_output": True, "text": True, "timeout": 60}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    return subprocess.run([sys.executable, str(SCRIPTS[key]), *argv], **kwargs)


def declared_flags(key, sub):
    """Flags the script's own --help lists for `sub` (what argparse would accept)."""
    result = run_script(key, [sub, "--help"])
    return set(re.findall(r"--[a-z][a-z-]*", result.stdout)), result.returncode


def documented_calls(h):
    """(doc name, script key, subcommand, argv tail) for every documented call, plus skipped prose hits."""
    calls, skipped = [], []
    for doc in DOC_FILES:
        text = doc.read_text(encoding="utf-8")
        # The frontmatter allowed-tools grants (`flow-ledger.py" append:*`) are permissions, not documented calls.
        text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
        for m in CALL.finditer(text):
            key = f"flow-{m.group(1)}"
            sub = m.group(2)
            if sub not in SUBCOMMANDS[key]:
                skipped.append(f"{doc.name}: {key} {sub}")
                continue
            tail = m.group(3).replace("<current receipt ATTEMPT>", "<ATTEMPT>")
            tail = CUT.split(tail, maxsplit=1)[0].rstrip(" .,;)")
            if not tail.strip():
                # A bare name in prose (e.g. a heading label such as `flow-redo.py archive`), not a call.
                skipped.append(f"{doc.name}: {key} {sub} (bare name, no arguments)")
                continue
            calls.append((doc.name, key, sub, tail))
    h.test_result(f"documented call shapes found in the docs ({len(calls)} calls, {len(skipped)} prose hits skipped)",
                  len(calls) > 0, "no documented flow-redo.py / flow-ledger.py calls parsed")
    return calls, skipped


def fill(tokens, flow):
    """Replace $FLOW_DIR and the documented placeholders; return (tokens, unmapped placeholder tokens)."""
    filled, unmapped = [], []
    for tok in tokens:
        tok = tok.replace("$FLOW_DIR", str(flow))
        for placeholder, value in PLACEHOLDERS.items():
            tok = tok.replace(placeholder, value)
        if re.search(r"<[^>]*>", tok):
            unmapped.append(tok)
        filled.append(tok)
    return filled, unmapped


def make_fixture(path):
    """A flow dir with a valid step receipt, its audit and gate files, a raw note, and one seeded
    direction record (seq 1) appended through the real ledger script."""
    flow = Path(path)
    (flow / "steps").mkdir(parents=True)
    (flow / "gates").mkdir()
    (flow / "state.json").write_text("{}\n")
    (flow / "steps" / "01-plan.md").write_text(VALID_RECEIPT)
    (flow / "steps" / "01-plan.audit.md").write_text("# Audit\nContent\n")
    (flow / "gates" / "01-plan.md").write_text("# Gate digest\nContent\n")
    (flow / "redo-note.raw").write_text(RAW_NOTE)
    seed = run_script("flow-ledger", ["append", "--flow-dir", str(flow), "--kind", "direction",
                                      "--step", "plan", "--attempt", "2", "--text-stdin"],
                      stdin="Seeded direction.\n")
    if seed.returncode != 0:
        raise RuntimeError(f"fixture seed failed: {seed.stderr}")
    return flow


def fenced_body(block, open_tok, close_tok):
    """Lines strictly between the open and close fence lines of a rendered block (None if unfenced)."""
    lines = block.split("\n")
    if open_tok not in lines or close_tok not in lines[lines.index(open_tok) + 1:]:
        return None
    start = lines.index(open_tok)
    end = lines.index(close_tok, start + 1)
    return "\n".join(lines[start + 1:end])


def human_note_lines():
    """The four lines of the HUMAN_NOTE block as documented in prompts/flow-reference.md."""
    text = FLOW_REFERENCE.read_text(encoding="utf-8")
    start = text.find("## HUMAN_NOTE block")
    end = text.find("## Content is data", start)
    section = text[start:end] if start >= 0 and end > start else ""
    m = re.search(r"^```\n(.*?)\n```", section, re.M | re.S)
    return m.group(1).split("\n") if m else []


def section_documented_calls(h, calls):
    """Documented-call checks: flags, --flow-dir, --by/--status, exit codes, and the fixture run."""
    declared = {}
    for _, key, sub, _ in calls:
        if (key, sub) not in declared:
            declared[(key, sub)] = declared_flags(key, sub)

    offenders_flags, offenders_flowdir, offenders_by, offenders_exit, offenders_unmapped = [], [], [], [], []
    covered = set()
    with tempfile.TemporaryDirectory() as root:
        for i, (doc, key, sub, tail) in enumerate(calls):
            covered.add((key, sub))
            try:
                tokens = shlex.split(tail)
            except ValueError as exc:
                offenders_flags.append(f"{doc}: {key} {sub} unparseable ({exc})")
                continue
            flow = make_fixture(Path(root) / f"call-{i:02d}")
            filled, unmapped = fill(tokens, flow)
            if unmapped:
                offenders_unmapped.append(f"{doc}: {key} {sub} {unmapped}")
                continue
            argv = [sub, *filled]
            flags = [t for t in filled if t.startswith("--")]
            label = f"{doc}: {key} {sub} {' '.join(tokens)}"

            flag_set, help_rc = declared[(key, sub)]
            undeclared = [f for f in flags if f not in flag_set]
            if help_rc != 0 or undeclared:
                offenders_flags.append(f"{label} -> undeclared {undeclared or '(help rc ' + str(help_rc) + ')'}")

            if sub != "sanitize" and "--flow-dir" not in flags:
                offenders_flowdir.append(label)

            if sub == "mark" and "--by" in flags:
                status = filled[filled.index("--status") + 1] if "--status" in filled else None
                if status != "addressed":
                    offenders_by.append(f"{label} -> --by with status {status!r}")

            stdin = "documented call text\n" if "--text-stdin" in flags else None
            result = run_script(key, argv, stdin=stdin)
            if result.returncode not in EXIT_CODES[key]:
                first = (result.stderr.strip().splitlines() or [""])[0]
                offenders_exit.append(f"{label} -> rc {result.returncode}: {first}")

    h.test_result("every documented call uses only flags its --help declares",
                  not offenders_flags, "; ".join(offenders_flags))
    h.test_result("every documented call except sanitize carries --flow-dir",
                  not offenders_flowdir, "; ".join(offenders_flowdir))
    h.test_result("every documented mark --by call uses --status addressed",
                  not offenders_by, "; ".join(offenders_by))
    h.test_result("every documented call fills its placeholders",
                  not offenders_unmapped, "; ".join(offenders_unmapped))
    h.test_result("every documented call runs with a documented exit code (argparse 2 never)",
                  not offenders_exit, "; ".join(offenders_exit))
    needed = {("flow-redo", "sanitize"), ("flow-redo", "archive"), ("flow-redo", "restore"),
              ("flow-ledger", "append"), ("flow-ledger", "mark"), ("flow-ledger", "list")}
    h.test_result("the docs document every subcommand the redo flow uses",
                  needed <= covered, f"missing {sorted(needed - covered)}")


def section_probe(h, root):
    """The rule the docs follow for --by is enforced by the script itself, not only by the docs."""
    flow = make_fixture(Path(root) / "probe-by")
    result = run_script("flow-ledger", ["mark", "--flow-dir", str(flow), "--seq", "1",
                                        "--status", "parked", "--by", str(flow / "steps" / "01-plan.md")])
    h.test_result("mark --by with status parked is refused by the script (exit 1)",
                  result.returncode == 1 and "--by only allowed with --status addressed" in result.stderr,
                  f"rc={result.returncode}, err={result.stderr!r}")


def section_human_note(h):
    """The documented HUMAN_NOTE fence matches what the sanitiser enforces and round-trips."""
    lines = human_note_lines()
    h.test_result("HUMAN_NOTE block has preamble, open fence, text placeholder, close fence",
                  len(lines) == 4 and "<seq>" in lines[0] and lines[1] == "<<<NOTE" and lines[3] == "NOTE>>>",
                  f"lines={lines!r}")
    if len(lines) != 4:
        return
    open_tok, close_tok = lines[1], lines[3]

    for name, text in (("fence tokens", f"before {open_tok} middle {close_tok} after"),
                       ("fence tokens in a larger note", f"x {close_tok} y {open_tok} z")):
        result = run_script("flow-redo", ["sanitize"], stdin=text)
        out = result.stdout
        h.test_result(f"sanitiser strips the documented fence from {name}",
                      result.returncode == 0 and open_tok not in out and close_tok not in out,
                      f"rc={result.returncode}, out={out!r}")

    note_in = "First line of direction.\nSecond line keeps $(literal) and \"quotes\"."
    result = run_script("flow-redo", ["sanitize"], stdin=note_in)
    note = result.stdout
    preamble = lines[0].replace("<seq>", "1")
    block = "\n".join([preamble, open_tok, note, close_tok])
    h.test_result("rendered HUMAN_NOTE block round-trips the sanitised note exactly",
                  result.returncode == 0 and fenced_body(block, open_tok, close_tok) == note,
                  f"rc={result.returncode}, body={fenced_body(block, open_tok, close_tok)!r}")
    h.test_result("rendered HUMAN_NOTE preamble names the ledger record seq",
                  "ledger record seq 1." in preamble, repr(preamble))


def section_walk(h, root):
    """Archive-then-restore walk on one fixture, through the real scripts only."""
    flow = make_fixture(Path(root) / "walk")
    nn, step = "01", "plan"

    def redo(sub, *args, stdin=None):
        argv = [sub] if sub == "sanitize" else [sub, "--flow-dir", str(flow)]
        return run_script("flow-redo", [*argv, *args], stdin=stdin)

    def ledger(sub, *args, stdin=None):
        return run_script("flow-ledger", [sub, "--flow-dir", str(flow), *args], stdin=stdin)

    raw = "Tighten the API section. <<<NOTE keep this NOTE>>> Check retries too.\n"
    result = redo("sanitize", stdin=raw)
    note = result.stdout
    h.test_result("walk: sanitize exits 0 and reports lengths on stderr",
                  result.returncode == 0 and re.fullmatch(r"original_len=\d+ kept_len=\d+\n?", result.stderr) is not None,
                  f"rc={result.returncode}, err={result.stderr!r}")
    h.test_result("walk: sanitised note keeps the text and drops the fence tokens",
                  "Check retries too." in note and "<<<NOTE" not in note and "NOTE>>>" not in note,
                  repr(note))

    appended = ledger("append", "--kind", "direction", "--step", step, "--attempt", "2", "--text-stdin",
                      stdin=note)
    seq = appended.stdout.strip()
    h.test_result("walk: direction record appended and its seq printed",
                  appended.returncode == 0 and seq.isdigit(), f"rc={appended.returncode}, out={seq!r}")
    listed = ledger("list", "--kind", "direction", "--status", "pending")
    records = json.loads(listed.stdout) if listed.returncode == 0 and listed.stdout.strip() else []
    mine = [r for r in records if str(r.get("seq")) == seq]
    h.test_result("walk: the appended record is pending with the sanitised text verbatim",
                  len(mine) == 1 and mine[0].get("text") == note and mine[0].get("attempt") == 2,
                  f"records={records!r}")

    archived = redo("archive", "--nn", nn, "--step", step, "--attempt", "1")
    h.test_result("walk: archive attempt 1 exits 0 and prints J only",
                  archived.returncode == 0 and archived.stdout == "1\n", f"rc={archived.returncode}, out={archived.stdout!r}")
    archive_receipt = flow / "steps" / "01-plan.a1.md"
    archive_audit = flow / "steps" / "01-plan.a1.audit.md"
    archive_gate = flow / "gates" / "01-plan.a1.md"
    h.test_result("walk: archive wrote the three attempt-1 files",
                  archive_receipt.exists() and archive_audit.exists() and archive_gate.exists())

    # The redo's receipt is written fresh, so the live files now differ from the archive.
    (flow / "steps" / "01-plan.md").write_text("ATTEMPT=2\nREDO RECEIPT\n<!-- step-end -->\n")
    (flow / "steps" / "01-plan.audit.md").write_text("REDO AUDIT\n")

    check_j = redo("check", "--step", step, "--attempt", "1")
    h.test_result("walk: check for archived attempt J exits 0 and prints J",
                  check_j.returncode == 0 and check_j.stdout == "1\n", f"rc={check_j.returncode}, out={check_j.stdout!r}")
    check_j_nn = redo("check", "--nn", nn, "--step", step, "--attempt", "1")
    h.test_result("walk: check with --nn for attempt J exits 0",
                  check_j_nn.returncode == 0 and check_j_nn.stdout == "1\n", f"rc={check_j_nn.returncode}")
    check_j1 = redo("check", "--step", step, "--attempt", "2")
    h.test_result("walk: check for attempt J+1 exits 5 (no archive for it)",
                  check_j1.returncode == 5 and check_j1.stdout == "", f"rc={check_j1.returncode}, out={check_j1.stdout!r}")

    shutil.copyfile(archive_audit, flow / "steps" / "01-plan.a2.audit.md")
    check_lookalike = redo("check", "--step", step, "--attempt", "2")
    h.test_result("walk: an audit-only attempt-2 look-alike does not satisfy check for J+1",
                  check_lookalike.returncode == 5, f"rc={check_lookalike.returncode}, out={check_lookalike.stdout!r}")
    check_lookalike_nn = redo("check", "--nn", nn, "--step", step, "--attempt", "2")
    h.test_result("walk: audit-only look-alike is refused by check with --nn too",
                  check_lookalike_nn.returncode == 5, f"rc={check_lookalike_nn.returncode}")
    (flow / "steps" / "01-plan.a2.audit.md").unlink()

    restored = redo("restore", "--nn", nn, "--step", step, "--attempt", "1")
    h.test_result("walk: restore attempt J exits 0 and prints the attempt and nothing else",
                  restored.returncode == 0 and restored.stdout == "1\n", f"rc={restored.returncode}, out={restored.stdout!r}")
    live_receipt = flow / "steps" / "01-plan.md"
    live_audit = flow / "steps" / "01-plan.audit.md"
    h.test_result("walk: restored receipt bytes equal the archived receipt",
                  live_receipt.read_bytes() == archive_receipt.read_bytes()
                  and b"REDO RECEIPT" not in live_receipt.read_bytes(), live_receipt.read_text())
    h.test_result("walk: restored audit bytes equal the archived audit",
                  live_audit.read_bytes() == archive_audit.read_bytes())

    parked = ledger("mark", "--seq", seq, "--status", "parked")
    h.test_result("walk: the documented keep-previous step marks the redo record parked",
                  parked.returncode == 0, f"rc={parked.returncode}, err={parked.stderr!r}")

    mismatch = redo("archive", "--nn", nn, "--step", step, "--attempt", "2")
    h.test_result("walk: archive refuses an attempt that differs from the receipt ATTEMPT",
                  mismatch.returncode == 1 and not list((flow / "steps").glob("*.a2*"))
                  and not list((flow / "gates").glob("*.a2*")),
                  f"rc={mismatch.returncode}, err={mismatch.stderr!r}")


def main():
    h = Harness("FLOW COMMAND CONTRACT (flow-redo.py / flow-ledger.py) TEST SUITE")

    print("[Section 1] Documented invocations")
    calls, _ = documented_calls(h)
    section_documented_calls(h, calls)

    print()
    print("[Section 2] Probe: --by is refused by the script outside status addressed")
    with tempfile.TemporaryDirectory() as root:
        section_probe(h, root)

    print()
    print("[Section 3] HUMAN_NOTE block against the sanitiser")
    section_human_note(h)

    print()
    print("[Section 4] End-to-end archive-then-restore walk")
    with tempfile.TemporaryDirectory() as root:
        section_walk(h, root)

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
