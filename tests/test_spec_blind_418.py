#!/usr/bin/env python3
"""
Spec-blind black-box tests for #418 (flow-guard and flow-bookkeeping follow-ups from #358):
#403 (quote-aware Bash guard), #404 (mutants, covered elsewhere), #405 (fail-closed exits),
#408 (scripts/review-ruling.py).

Written from the plan's behaviour statements alone. Every case runs the real production script
as a subprocess (no imports of guard internals). The only in-process import is precedent-digest.py's
parse_review, which the plan names as the consumer that must read the ruling output back.

Run with: python3 tests/test_spec_blind_418.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"
RULING = REPO_ROOT / "scripts" / "review-ruling.py"
PRECEDENT_DIGEST = REPO_ROOT / "scripts" / "precedent-digest.py"

# Fixed wall-clock bound for guard runs (plan: 5 s, far below Claude Code's default hook timeout).
TIME_BOUND = 5


# ---------------------------------------------------------------------------
# Bash guard helpers (black box: JSON on stdin, --agent argv, exit code + stderr)
# ---------------------------------------------------------------------------

def guard(command=None, agent="flow-implement", agent_type=None, raw=None,
          tool_name="Bash", with_agent_arg=True, argv_extra=None, stdin_bytes=None,
          bound=TIME_BOUND, closed_stdin=False):
    """Run flow-bash-guard.py. Returns (returncode or None on timeout, stdout, stderr)."""
    if stdin_bytes is None:
        if raw is None:
            payload = {"tool_name": tool_name, "tool_input": {"command": command}}
            if agent_type is not None:
                payload["agent_type"] = agent_type
            raw = json.dumps(payload)
        stdin_bytes = raw.encode("utf-8")
    argv = [sys.executable, str(BASH_GUARD)]
    if with_agent_arg:
        argv += ["--agent", agent]
    if argv_extra:
        argv += argv_extra
    try:
        if closed_stdin:
            # Close fd 0 in the child so Python sees no stdin at all.
            result = subprocess.run(
                ["/bin/sh", "-c", 'exec "$0" "$@" <&-', *argv],
                capture_output=True, timeout=bound,
            )
        else:
            result = subprocess.run(argv, input=stdin_bytes, capture_output=True, timeout=bound)
    except subprocess.TimeoutExpired:
        return None, "", f"TIMEOUT after {bound}s"
    return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")


def blocked(command, **kw):
    rc, _, _ = guard(command, **kw)
    return rc == 2


def allowed(command, **kw):
    rc, _, _ = guard(command, **kw)
    return rc == 0


def describe(command, rc, err):
    return f"rc={rc} cmd={command[:80]!r} stderr={err[:160]!r}"


# ---------------------------------------------------------------------------
# review-ruling.py helpers
# ---------------------------------------------------------------------------

PLAN_TEXT = """# Expert review action plan

## Needs you

### 1. Stale cache reads
- **Where**: scripts/cache.py:10 · **Severity**: HIGH · **Raised by**: Uncle Bob
- **Why this is yours**: tripped test 3
- **The finding**: The cache is never invalidated after writes, so reads can be stale.
- **Options**:
  - **A — Invalidate on write** (recommended): drop the key on every write · Pro: simple · Con: extra I/O
  - **B — Use a TTL**: expire entries after 60 s · Pro: bounded staleness · Con: still stale in window
  - **C — Leave as-is**: do nothing · Pro: no churn · Con: stale reads persist
- **Recommendation**: A, because writes are rare.
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

### 2. Misleading module name
- **Where**: scripts/x.py:1 · **Severity**: LOW · **Raised by**: Nick
- **Why this is yours**: tripped test 3
- **The finding**: The name suggests a different responsibility.
- **Options**:
  - **A — Rename the module — now**: rename it everywhere · Pro: clarity · Con: churn
  - **B — Leave as-is**: do nothing
- **Recommendation**: B
- **STATUS**: pending-decision
- **DECISION**: _(empty until STATUS changes)_

---

## Needs measurement: 1

### 1. Latency regression
- **Where**: scripts/y.py:5 · **Severity**: MEDIUM · **Raised by**: Dana
- **Why this needs measurement**: cannot be judged from the report alone
- **The finding**: The change may slow the hot path.
- **Command**: `python3 scripts/bench.py --n 1000`
- **Resolves via**: if p95 drops, keep the change; if not, revert it.
- **STATUS**: pending-measurement
- **DECISION**: _(empty until STATUS changes — Claude records the reported result here once you tell it what the command above returned; never hand-edited)_

---
"""


def item_text(text, section_prefix, n):
    """Return the text of item `### n.` inside the `## <section_prefix>` section."""
    out = []
    in_section = False
    collecting = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip().startswith(section_prefix)
            collecting = False
            continue
        if in_section and line.startswith(f"### {n}. "):
            collecting = True
            out.append(line)
            continue
        if collecting and line.startswith("### "):
            break
        if collecting:
            out.append(line)
    return "\n".join(out)


def count_prefix(block, prefix):
    return sum(1 for line in block.splitlines() if line.startswith(prefix))


def field_value(block, prefix):
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


class Sandbox:
    """A temp HOME with a reviews root and one review dir holding claude-action-plan.md."""

    def __init__(self, plan_text=PLAN_TEXT):
        self.tmp = Path(tempfile.mkdtemp(prefix="spec418-"))
        self.home = self.tmp / "home"
        self.root = self.home / ".claude" / "reviews"
        self.review = self.root / "acme" / "feature-1-abc"
        self.review.mkdir(parents=True)
        self.plan = self.review / "claude-action-plan.md"
        self.plan.write_text(plan_text)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bytes(self):
        return self.plan.read_bytes()

    def ruling(self, args, extra_env=None):
        env = {**os.environ, "HOME": str(self.home)}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if extra_env:
            env.update(extra_env)
        r = subprocess.run([sys.executable, str(RULING), *args], env=env,
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout, r.stderr

    def decide(self, item, option=None, custom=None, reasoning=None, mode=None, review_dir=None):
        args = ["decide", "--review-dir", str(review_dir or self.review), "--item", str(item)]
        if option is not None:
            args += ["--option", option]
        if custom is not None:
            args += ["--custom", custom]
        if reasoning is not None:
            args += ["--reasoning", reasoning]
        if mode is not None:
            args += ["--mode", mode]
        return self.ruling(args)

    def measure(self, item, result, mode=None, review_dir=None):
        args = ["measure", "--review-dir", str(review_dir or self.review), "--item", str(item),
                "--result", result]
        if mode is not None:
            args += ["--mode", mode]
        return self.ruling(args)


def load_parse_review():
    spec = importlib.util.spec_from_file_location("precedent_digest_418", PRECEDENT_DIGEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_review


# ============================================================================
h = Harness("SPEC-BLIND #418: FLOW BASH GUARD, FAIL-CLOSED EXITS, REVIEW RULINGS")
t = h.test_result

# ----------------------------------------------------------------------------
print("[Precondition] scripts exist")
t("flow-bash-guard.py exists", BASH_GUARD.exists(), str(BASH_GUARD))
t("review-ruling.py exists", RULING.exists(), str(RULING))

# ----------------------------------------------------------------------------
print("\n[#403] quoted separators do not split: allowed")
quoted_allowed = [
    'git commit -m "fix; tidy"',
    "git commit -m 'a(b'",
    'echo "a; b"',
    "echo 'x && y'",
    'git commit -m "see (#403)"',
    'git commit -m "fix; refs #403"',
    'git commit -m "tidy (closes #403); docs"',
]
for cmd in quoted_allowed:
    rc, _, err = guard(cmd)
    t(f"allowed: {cmd}", rc == 0, describe(cmd, rc, err))

# ----------------------------------------------------------------------------
print("\n[#403] real separators and wrappers still block publishing verbs")
real_blocked = [
    "echo hi; git push",
    "echo x && git push",
    'echo "x" && git push',
    'echo "$(git push)"',
    'echo "`git push`"',
    'bash -c "git push"',
    "sh -c 'a; git push'",
    '"git" push',
    'git "push"',
    'g"i"t push',
    "cat <(git push)",
    "git \\\npush",
    "git pu\\\nsh",
    '"git" \\\npush',
    "echo 'a\\' ; git push #'",
    'echo "a\\" ; git push #"',
    'echo "$(echo \')\' ; git push)"',
    'echo "a" # c\ngit push',
    "cat <<EOF\ndon't\nEOF\ngit push #'",
    "echo hi # it's\ngit push #'",
]
for cmd in real_blocked:
    rc, _, err = guard(cmd)
    t(f"blocked: {cmd!r}", rc == 2, describe(cmd, rc, err))

print("\n[#403] escaped-$ before a quote does not hide a separator (F1)")
escaped_dollar_blocked = [
    "echo \\$'x' ; git push ; echo \\'",
    'echo "a\\$" ; git push ; echo "b\\$"',
    "echo \\$'x' ; git push ; echo \\' # c",
]
for cmd in escaped_dollar_blocked:
    rc, _, err = guard(cmd)
    t(f"blocked (escaped $): {cmd!r}", rc == 2, describe(cmd, rc, err))

print("\n[#403] unterminated input is blocked")
for cmd in ['git commit -m "abc', "$(git status"]:
    rc, _, err = guard(cmd)
    t(f"blocked (unterminated): {cmd!r}", rc == 2, describe(cmd, rc, err))

print("\n[#403] $'...' and $\"...\" bypass is blocked in every mode")
dollar_bypass = [
    "echo hi; $'git' push",
    "echo hi # x\n$'git' push",
    "cat <<EOF\nx\nEOF\n$'git' push",
]
for cmd in dollar_bypass:
    rc, _, err = guard(cmd)
    t(f"blocked ($' bypass): {cmd!r}", rc == 2, describe(cmd, rc, err))

print("\n[#403 Q2] command-prefix forms are normalised before the deny rules")
prefix_blocked = [
    "GIT_TERMINAL_PROMPT=0 git push",
    "A=1 B=2 git push",
    "command git push",
    "exec git push",
    "nohup git push",
    "time git push",
    "timeout 5 git push",
    "nice -n 10 git push",
    "=git push",
]
for cmd in prefix_blocked:
    rc, _, err = guard(cmd)
    t(f"blocked (prefix): {cmd!r}", rc == 2, describe(cmd, rc, err))

print("\n[#403] must-stay-allowed: process substitution, $'' in arguments, $ at end of pattern")
must_stay_allowed = [
    "diff <(sort a) <(sort b)",
    "while read -r f; do :; done < <(find . -name x)",
    "IFS=$'\\n' read -r x",
    "sort -t $'\\t' -k2 f",
    "grep -n 'foo$' file",
    "rg 'done$'",
]
for cmd in must_stay_allowed:
    rc, _, err = guard(cmd)
    t(f"allowed: {cmd!r}", rc == 0, describe(cmd, rc, err))

print("\n[#403 ledger] residuals main allows that this change does not close")
ledger_allowed = [
    "exec -a x git push",
    "command -p git push",
    "timeout -s KILL 5 git push",
    "nice -5 git push",
    "$'\\x67it' push",
    "*(e:'git push':)",
    "python3 -c 'import json; print(1)'",
]
for cmd in ledger_allowed:
    rc, _, err = guard(cmd)
    t(f"ledger residual allowed: {cmd!r}", rc == 0, describe(cmd, rc, err))

print("\n[#403] interpreter one-liners: publishing verb pair blocks")
interpreter_blocked = [
    'python3 -c \'import os; os.system("git push")\'',
    'python3 -c \'import os; os.system("git -C . push")\'',
    'python3 -c \'import subprocess; subprocess.run(["git","--no-pager","push"])\'',
    'python3 -c \'import subprocess; subprocess.run(["git","push"])\'',
    'python3.12 -Ic \'import os; os.system("git push")\'',
    "node -e \"require('child_process').execSync('gh pr create')\"",
    "node --eval \"require('child_process').execSync('gh pr create')\"",
    "perl -E 'system(\"git push\")'",
    'PYTHONPATH=. python3 -c \'import os; os.system("git push")\'',
]
for cmd in interpreter_blocked:
    rc, _, err = guard(cmd)
    t(f"blocked (interpreter): {cmd!r}", rc == 2, describe(cmd, rc, err))

# ----------------------------------------------------------------------------
print("\n[#405 W2] --agent handling")
rc, _, err = guard("echo hi", agent="flow-implment")
t("typo --agent exits 2", rc == 2, describe("echo hi", rc, err))
t("typo --agent names the unknown value", "Unknown --agent" in err, f"stderr={err[:160]!r}")
rc, _, err = guard("echo hi", agent="bogus-agent")
t("unregistered --agent exits 2", rc == 2, describe("echo hi", rc, err))
rc, _, err = guard("git push", agent="flow-review")
t("--agent flow-review passes through (exit 0)", rc == 0, describe("git push", rc, err))
rc, _, err = guard("git push", agent="auto", agent_type="flow-implement")
t("--agent auto with flow-implement agent_type blocks git push", rc == 2, describe("git push", rc, err))
rc, _, err = guard("git -c x", agent="auto")
t("--agent auto with missing agent_type passes through (exit 0)", rc == 0, describe("git -c x", rc, err))

print("\n[#405] path-specific fail-closed exits (bash guard)")
rc, _, err = guard(with_agent_arg=False, raw=json.dumps({"tool_input": {"command": "echo hi"}}))
t("missing --agent exits 2 with Usage", rc == 2 and "Usage" in err, f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(raw="{not json}")
t("malformed JSON exits 2 with parse marker",
  rc == 2 and "Failed to parse JSON" in err, f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(raw="[]")
t("non-dict payload exits 2 with 'not a dict'", rc == 2 and "not a dict" in err,
  f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(raw=json.dumps({"tool_name": "Bash", "tool_input": "echo hi"}))
t("non-dict tool_input exits 2 with 'Invalid tool_input: not a dict'",
  rc == 2 and "Invalid tool_input: not a dict" in err, f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(stdin_bytes=b"")
t("empty stdin exits 2", rc == 2, f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(stdin_bytes=b'{"tool_name": "Bash", "tool_input": {"command": "\xff\xfe"}}')
t("non-UTF-8 stdin exits 2 with catch-all 'Unexpected error'",
  rc == 2 and "Unexpected error" in err, f"rc={rc} stderr={err[:160]!r}")
rc, _, err = guard(closed_stdin=True)
t("closed stdin exits 2 with catch-all 'Unexpected error'",
  rc == 2 and "Unexpected error" in err, f"rc={rc} stderr={err[:160]!r}")

print("\n[#405] pinned exit 0 pass-throughs")
rc, _, err = guard("git push", tool_name="Read")
t("non-Bash tool_name passes through (exit 0)", rc == 0, describe("git push", rc, err))

# ----------------------------------------------------------------------------
print("\n[#403] nesting cap: deterministic exit 2 on both paths")
deep_quote_aware = "echo " + "$(" * 70 + "x" + ")" * 70
rc, _, err = guard(deep_quote_aware)
t("nesting 70 of $( on quote-aware path exits 2 with 'nesting too deep'",
  rc == 2 and "nesting too deep" in err, describe("deep $(", rc, err))
shallow = "echo " + "$(" * 10 + "x" + ")" * 10
rc, _, err = guard(shallow)
t("nesting 10 of $( (below cap) exits 0", rc == 0, describe("shallow $(", rc, err))
deep_heredoc = "cat <<EOF\n" + "$(" * 70 + "ls" + ")" * 70 + "\nEOF"
rc, _, err = guard(deep_heredoc)
t("nesting 70 of $( on fallback (heredoc) path exits 2 with 'nesting too deep'",
  rc == 2 and "nesting too deep" in err, describe("deep heredoc", rc, err))

# ----------------------------------------------------------------------------
print("\n[#405] linear-time bounds (fixed 5 s per case)")
wide = "echo \"" + ";" * 50000 + "\" " + ";".join(["$(a)"] * 40000)
rc, _, err = guard(wide)
t(f"wide shallow quote-aware input (~250 KB) exits 0 within {TIME_BOUND}s",
  rc == 0, f"rc={rc} stderr={err[:160]!r}")

wide_heredoc = "cat <<EOF\n" + wide + "\nEOF"
rc, _, err = guard(wide_heredoc)
t(f"wide shallow fallback (heredoc) input exits 0 within {TIME_BOUND}s",
  rc == 0, f"rc={rc} stderr={err[:160]!r}")

# Isolates the linear-time claim: the sibling groups alone, inside a heredoc, with no quoted run.
wide_groups_heredoc = "cat <<EOF\n" + ";".join(["$(a)"] * 40000) + "\nEOF"
rc, _, err = guard(wide_groups_heredoc)
t(f"wide shallow sibling groups only, inside heredoc, exit 0 within {TIME_BOUND}s",
  rc == 0, f"rc={rc} stderr={err[:160]!r}")

deep_body = "cat <<EOF\n" + "(" * 100000 + "ls" + ")" * 100000 + "\nEOF"
rc, _, err = guard(deep_body)
t(f"(x100000) nesting inside heredoc exits 2 with depth reason within {TIME_BOUND}s",
  rc == 2 and "nesting too deep" in err, f"rc={rc} stderr={err[:160]!r}")

big_heredoc = "cat <<EOF\n" + ("x" * 255 + "\n") * 1000 + "EOF"
rc, _, err = guard(big_heredoc)
t(f"~256 KB heredoc write exits 0 within {TIME_BOUND}s",
  rc == 0, f"rc={rc} stderr={err[:160]!r}")

# ============================================================================
print("\n[#408] review-ruling.py: decide round-trip through parse_review")
parse_review = load_parse_review()

canonical_a = "A — Invalidate on write"
for label in [canonical_a, canonical_a + " (recommended)"]:
    sb = Sandbox()
    try:
        rc, _, err = sb.decide(1, option=label)
        t(f"decide option {label!r} exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
        records, counts = parse_review(sb.plan.read_text())
        answers = [r["answer"] for r in records]
        t(f"parse_review reads back canonical answer for {label!r}",
          len(answers) == 1 and answers[0] == canonical_a,
          f"answers={answers!r} counts={counts!r}")
        if records:
            t(f"parse_review options still parse after decide ({label!r})",
              len(records[0]["options"]) == 3,
              f"options={records[0]['options']!r}")
        item = item_text(sb.plan.read_text(), "Needs you", 1)
        t(f"exactly one DECISION line after decide ({label!r})",
          count_prefix(item, "- **DECISION**:") == 1,
          f"DECISION lines={count_prefix(item, '- **DECISION**:')}")
        t(f"DECISION placeholder gone after decide ({label!r})",
          "_(empty until STATUS changes)_" not in item, "placeholder still present")
        t(f"STATUS set to decided after decide ({label!r})",
          field_value(item, "- **STATUS**:") == "decided",
          f"STATUS={field_value(item, '- **STATUS**:')!r}")
    finally:
        sb.close()

print("\n[#408] decide and parse_review with Leave as-is")
sb = Sandbox()
try:
    rc, _, err = sb.decide(1, option="C — Leave as-is")
    t("decide Leave as-is exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    item = item_text(sb.plan.read_text(), "Needs you", 1)
    t("Leave as-is sets STATUS no-op", field_value(item, "- **STATUS**:") == "no-op",
      f"STATUS={field_value(item, '- **STATUS**:')!r}")
    records, _ = parse_review(sb.plan.read_text())
    t("parse_review reads back the no-op ruling",
      len(records) == 1 and records[0]["answer"].startswith("C"),
      f"records={records!r}")
finally:
    sb.close()

print("\n[#408] decide --custom records decided with the custom text")
sb = Sandbox()
try:
    rc, _, err = sb.decide(1, custom="Move the cache behind a feature flag")
    t("decide --custom exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    item = item_text(sb.plan.read_text(), "Needs you", 1)
    t("--custom sets STATUS decided", field_value(item, "- **STATUS**:") == "decided",
      f"STATUS={field_value(item, '- **STATUS**:')!r}")
    t("--custom text appears in DECISION",
      "Move the cache behind a feature flag" in (field_value(item, "- **DECISION**:") or ""),
      f"DECISION={field_value(item, '- **DECISION**:')!r}")
finally:
    sb.close()

print("\n[#408] --option and --custom together are refused, file unchanged")
sb = Sandbox()
try:
    before = sb.bytes()
    rc, _, err = sb.ruling(["decide", "--review-dir", str(sb.review), "--item", "1",
                            "--option", canonical_a, "--custom", "x"])
    t("option + custom together exits non-zero", rc != 0, f"rc={rc}")
    t("option + custom together leaves file byte-identical", sb.bytes() == before)
finally:
    sb.close()

print("\n[#408] unrelated items are byte-unchanged")
sb = Sandbox()
try:
    before_item2 = item_text(sb.plan.read_text(), "Needs you", 2)
    before_measure = item_text(sb.plan.read_text(), "Needs measurement", 1)
    rc, _, err = sb.decide(1, option=canonical_a)
    t("decide item 1 exits 0 in multi-item plan", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    after = sb.plan.read_text()
    t("item 2 (Needs you) bytes unchanged", item_text(after, "Needs you", 2) == before_item2)
    t("Needs measurement item bytes unchanged",
      item_text(after, "Needs measurement", 1) == before_measure)
    t("only claude-action-plan.md in review dir after decide",
      sorted(p.name for p in sb.review.iterdir()) == ["claude-action-plan.md"],
      f"entries={sorted(p.name for p in sb.review.iterdir())!r}")
finally:
    sb.close()

print("\n[#408] idempotent re-run and conflicting answer (exit 3)")
sb = Sandbox()
try:
    rc, _, err = sb.decide(1, option=canonical_a)
    t("first decide exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    after_first = sb.bytes()
    rc, _, err = sb.decide(1, option=canonical_a)
    t("same answer re-run exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    t("same answer re-run writes nothing", sb.bytes() == after_first)
    rc, _, err = sb.decide(1, option="C — Leave as-is")
    t("conflicting answer on decided item exits 3", rc == 3, f"rc={rc} stderr={err[:200]!r}")
    t("conflicting answer leaves file byte-identical", sb.bytes() == after_first)
finally:
    sb.close()

print("\n[#408] refusals exit 1 and leave the file byte-identical")
sb = Sandbox()
try:
    before = sb.bytes()
    rc, _, err = sb.decide(1, option="Z — Not a real option")
    t("unknown label exits 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("unknown label leaves file byte-identical", sb.bytes() == before)

    rc, _, err = sb.decide(9, option=canonical_a)
    t("item number not present exits 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("missing item leaves file byte-identical", sb.bytes() == before)

    rc, _, err = sb.decide(1, option=canonical_a, mode="pr")
    t("--mode pr refused with exit 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("--mode pr leaves file byte-identical", sb.bytes() == before)

    rc, _, err = sb.measure(2, "p95 dropped 30%")
    t("measure against a Needs-you-only item number exits 1", rc == 1,
      f"rc={rc} stderr={err[:200]!r}")
    t("measure refusal leaves file byte-identical", sb.bytes() == before)
finally:
    sb.close()

print("\n[#408] containment refusals")
sb = Sandbox()
try:
    outside = sb.tmp / "elsewhere"
    outside.mkdir()
    outside_plan = outside / "claude-action-plan.md"
    outside_plan.write_text(PLAN_TEXT)
    before_outside = outside_plan.read_bytes()
    rc, _, err = sb.decide(1, option=canonical_a, review_dir=outside)
    t("review dir outside ~/.claude/reviews exits 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("outside review dir left byte-identical", outside_plan.read_bytes() == before_outside)

    dotdot = sb.root / "acme" / ".." / "acme" / "feature-1-abc"
    before = sb.bytes()
    rc, _, err = sb.decide(1, option=canonical_a, review_dir=dotdot)
    t("review dir with '..' segment exits 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("'..' refusal leaves file byte-identical", sb.bytes() == before)

    escape_link = sb.root / "linked"
    escape_link.symlink_to(outside)
    rc, _, err = sb.decide(1, option=canonical_a, review_dir=escape_link)
    t("symlinked review dir escaping the root exits 1", rc == 1,
      f"rc={rc} stderr={err[:200]!r}")
    t("symlinked review dir leaves outside file byte-identical",
      outside_plan.read_bytes() == before_outside)
finally:
    sb.close()

sb = Sandbox()
try:
    target = sb.tmp / "real-plan.md"
    target.write_text(PLAN_TEXT)
    before_target = target.read_bytes()
    sb.plan.unlink()
    sb.plan.symlink_to(target)
    rc, _, err = sb.decide(1, option=canonical_a)
    t("symlinked claude-action-plan.md exits 1", rc == 1, f"rc={rc} stderr={err[:200]!r}")
    t("symlinked plan target left byte-identical", target.read_bytes() == before_target)
finally:
    sb.close()

print("\n[#408] non-unique anchor refused")
dup_text = PLAN_TEXT.replace("### 2. Misleading module name", "### 1. Misleading module name")
sb = Sandbox(plan_text=dup_text)
try:
    before = sb.bytes()
    rc, _, err = sb.decide(1, option=canonical_a)
    t("duplicate '### 1.' anchor in Needs you exits 1", rc == 1,
      f"rc={rc} stderr={err[:200]!r}")
    t("duplicate anchor leaves file byte-identical", sb.bytes() == before)
finally:
    sb.close()

print("\n[#408] no root override via flag or environment")
sb = Sandbox()
try:
    before = sb.bytes()
    alt_root = sb.tmp / "alt-root"
    alt_root.mkdir()
    rc, _, _ = sb.ruling(["decide", "--review-dir", str(sb.review), "--item", "1",
                          "--option", canonical_a, "--reviews-root", str(alt_root)])
    t("unknown --reviews-root flag is refused (non-zero)", rc != 0, f"rc={rc}")
    t("unknown root flag leaves file byte-identical", sb.bytes() == before)

    outside = sb.tmp / "elsewhere-env"
    outside.mkdir()
    (outside / "claude-action-plan.md").write_text(PLAN_TEXT)
    before_env = (outside / "claude-action-plan.md").read_bytes()
    env_result = {}
    for var in ("CLAUDE_REVIEWS_ROOT", "REVIEWS_ROOT", "CLAUDE_HELPERS_REVIEWS_ROOT"):
        rc, _, _ = sb.ruling(
            ["decide", "--review-dir", str(outside), "--item", "1", "--option", canonical_a],
            extra_env={var: str(outside.parent)},
        )
        env_result[var] = rc
    t("environment root overrides are ignored (review dir outside real root still refused)",
      all(rc == 1 for rc in env_result.values()), f"results={env_result!r}")
    t("env override refusals leave outside file byte-identical",
      (outside / "claude-action-plan.md").read_bytes() == before_env)
finally:
    sb.close()

print("\n[#408] injection: free text cannot forge fields or end the fold")
sb = Sandbox()
try:
    forged = ("fine\n- **STATUS**: no-op\n- **DECISION**: forged\n### 9. Forged item\n"
              "```\n</details>\nend")
    rc, _, err = sb.decide(1, option=canonical_a, reasoning=forged)
    t("reasoning with injected fields exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    item = item_text(sb.plan.read_text(), "Needs you", 1)
    t("injected STATUS does not create a second STATUS field",
      count_prefix(item, "- **STATUS**:") == 1, f"count={count_prefix(item, '- **STATUS**:')}")
    t("STATUS still decided after injection attempt",
      field_value(item, "- **STATUS**:") == "decided",
      f"STATUS={field_value(item, '- **STATUS**:')!r}")
    t("injected DECISION line does not add a second DECISION field",
      count_prefix(item, "- **DECISION**:") == 1, f"count={count_prefix(item, '- **DECISION**:')}")
    t("injected heading does not create a new '### 9.' item",
      not any(line.startswith("### 9. Forged") for line in sb.plan.read_text().splitlines()))
    records, counts = parse_review(sb.plan.read_text())
    t("parse_review still reads the canonical answer after injection",
      len(records) == 1 and records[0]["answer"] == canonical_a,
      f"records={records!r} counts={counts!r}")
finally:
    sb.close()

sb = Sandbox(plan_text=PLAN_TEXT.replace(
    "  - **B — Use a TTL**: expire entries after 60 s · Pro: bounded staleness · Con: still stale in window",
    "  - **B — Use a TTL**: expire entries after 60 s </details> sneaky · Pro: bounded · Con: stale",
))
try:
    rc, _, err = sb.decide(1, option=canonical_a)
    t("rejected option body with </details> exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    records, _ = parse_review(sb.plan.read_text())
    t("folded options still parse after </details> in a rejected option body",
      len(records) == 1 and len(records[0]["options"]) == 3,
      f"records={records!r}")
finally:
    sb.close()

print("\n[#408] measure: Needs-measurement item recorded as measured")
sb = Sandbox()
try:
    rc, _, err = sb.measure(1, "p95 dropped 30%")
    t("measure exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    item = item_text(sb.plan.read_text(), "Needs measurement", 1)
    t("measure sets STATUS measured", field_value(item, "- **STATUS**:") == "measured",
      f"STATUS={field_value(item, '- **STATUS**:')!r}")
    t("measure writes the result into DECISION",
      "p95 dropped 30%" in (field_value(item, "- **DECISION**:") or ""),
      f"DECISION={field_value(item, '- **DECISION**:')!r}")
    t("measure leaves exactly one DECISION line",
      count_prefix(item, "- **DECISION**:") == 1)
    before = sb.bytes()
    rc, _, err = sb.measure(1, "p95 dropped 30%")
    t("measure re-run with same result exits 0 and writes nothing",
      rc == 0 and sb.bytes() == before, f"rc={rc} stderr={err[:200]!r}")
    rc, _, err = sb.measure(1, "p95 rose 10%")
    t("measure with a different result on a measured item exits 3", rc == 3,
      f"rc={rc} stderr={err[:200]!r}")
    t("conflicting measure leaves file byte-identical", sb.bytes() == before)
finally:
    sb.close()

sb = Sandbox()
try:
    rc, _, err = sb.measure(1, "ok\n- **STATUS**: pending-decision\n### 7. Forged")
    t("measure with injected fields exits 0", rc == 0, f"rc={rc} stderr={err[:200]!r}")
    item = item_text(sb.plan.read_text(), "Needs measurement", 1)
    t("measure injection does not add a STATUS field",
      count_prefix(item, "- **STATUS**:") == 1 and field_value(item, "- **STATUS**:") == "measured",
      f"count={count_prefix(item, '- **STATUS**:')} value={field_value(item, '- **STATUS**:')!r}")
finally:
    sb.close()

# ----------------------------------------------------------------------------
h.summarize_and_exit()
