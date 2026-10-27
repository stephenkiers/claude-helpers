#!/usr/bin/env python3
"""
Test suite for the North Star preflight (#273): scripts/north-star-preflight.py, its contract with
scripts/in-flight-snapshot.py, the doc wiring in the plan/review commands and the panel, and
`install.sh --check`.

Run with: python3 tests/test_north_star_preflight.py
"""

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

PREFLIGHT = REPO_ROOT / "scripts" / "north-star-preflight.py"
SNAPSHOT = REPO_ROOT / "scripts" / "in-flight-snapshot.py"
INSTALL_SH = REPO_ROOT / "install.sh"
TELEMETRY_SCHEMA = REPO_ROOT / "scripts" / "telemetry_schema.py"

FAKE_GH = """#!/bin/bash
# Fake gh. FAKE_GH_MODE: ok | fail | issues-fail | epics-fail | prs-fail
mode="${FAKE_GH_MODE:-ok}"
if [ "$1" = "auth" ]; then
  [ "$mode" = "auth-fail" ] && exit 1
  exit 0
fi
case "$mode" in
  fail) echo "boom" >&2; exit 1 ;;
  issues-fail) if [ "$1 $2" = "issue list" ]; then echo "issues boom" >&2; exit 1; fi ;;
  epics-fail)
    if [ "$1 $2" = "issue list" ]; then
      case " $* " in *" --limit "*) ;; *) echo "epics boom" >&2; exit 1 ;; esac
    fi ;;
  prs-fail) if [ "$1 $2" = "pr list" ]; then echo "prs boom" >&2; exit 1; fi ;;
esac
echo "[]"
"""


def read(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def load_module(name, path):
    """Import a repo script by path (its names are not importable as plain modules)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight_module():
    return load_module("north_star_preflight", PREFLIGHT)


def run(args, env=None, cwd=None):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, env=env, cwd=cwd)
    return proc.returncode, proc.stdout.strip(), proc.stderr


def make_bin(tmp):
    bindir = Path(tmp) / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bindir


def env_with(bindir, mode="ok", path_only_bin=False):
    env = os.environ.copy()
    env["PATH"] = str(bindir) + (os.pathsep + env["PATH"] if not path_only_bin else "")
    env["FAKE_GH_MODE"] = mode
    return env


def make_claude_dir(tmp, snapshot=True, scout=True):
    claude = Path(tmp) / "claude"
    (claude / "scripts").mkdir(parents=True)
    (claude / "prompts").mkdir(parents=True)
    if snapshot:
        (claude / "scripts" / "in-flight-snapshot.py").write_text("# x\n")
    if scout:
        (claude / "prompts" / "north-star-scout.md").write_text("scout\n")
    return claude


def files(claude, env, *extra):
    return run([sys.executable, str(PREFLIGHT), "files", "--claude-dir", str(claude), *extra], env=env)


def snap(directory, *extra):
    return run([sys.executable, str(PREFLIGHT), "snapshot", "--dir", str(directory), *extra])


def blind(tokens_file, *extra):
    return run([sys.executable, str(PREFLIGHT), "blind-line", "--tokens-file", str(tokens_file), *extra])


def read_optional(path):
    """Text of `path`, or "<absent>" when the file does not exist (used in failure messages)."""
    p = Path(path)
    return p.read_text() if p.exists() else "<absent>"


def section(doc, heading):
    """Markdown section whose heading contains `heading`, up to the next heading of the same or a
    higher level. Fenced code blocks are skipped, so a `# comment` inside bash is not a heading."""
    body = []
    level = None
    fenced = False
    for line in doc.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else re.match(r"^(#{1,6})\s+(.*)$", line)
        if level is None:
            if match and heading in match.group(2):
                level = len(match.group(1))
                body.append(line)
            continue
        if match and len(match.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def section_files(h, t):
    print("[Section 1] files phase")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_bin(tmp)
        env = env_with(bindir)
        claude = make_claude_dir(tmp)
        rc, out, _ = files(claude, env)
        t("files: everything present gives OK exit 0", (rc, out) == (0, "OK"), "%s %s" % (rc, out))

        (Path(tmp) / "a").mkdir()
        c2 = make_claude_dir(Path(tmp) / "a", snapshot=False)
        rc, out, err = files(c2, env)
        t("files: missing snapshot script is FAIL snapshot-script, exit 3, /setup-local in stderr",
          rc == 3 and out == "FAIL snapshot-script" and "/setup-local" in err, "%s %s" % (rc, out))

        (Path(tmp) / "b").mkdir()
        c3 = make_claude_dir(Path(tmp) / "b", scout=False)
        rc, out, _ = files(c3, env)
        t("files: missing scout prompt is FAIL scout-prompt", (rc, out) == (3, "FAIL scout-prompt"), out)

        (Path(tmp) / "c").mkdir()
        c4 = make_claude_dir(Path(tmp) / "c")
        (c4 / "scripts" / "in-flight-snapshot.py").unlink()
        (c4 / "scripts" / "in-flight-snapshot.py").symlink_to(Path(tmp) / "nowhere.py")
        rc, out, _ = files(c4, env)
        t("files: a dangling symlink counts as missing", (rc, out) == (3, "FAIL snapshot-script"), out)

        (Path(tmp) / "d").mkdir()
        c5 = make_claude_dir(Path(tmp) / "d")
        (c5 / "prompts" / "north-star-scout.md").write_text("")
        rc, out, _ = files(c5, env)
        t("files: an empty file counts as missing", (rc, out) == (3, "FAIL scout-prompt"), out)

        rc, out, _ = files(claude, env_with(bindir, "auth-fail"))
        t("files: gh auth failure is a hard FAIL gh-auth, exit 3",
          (rc, out) == (3, "FAIL gh-auth"), out)

        empty_bin = Path(tmp) / "emptybin"
        empty_bin.mkdir()
        env2 = os.environ.copy()
        env2["PATH"] = str(empty_bin)
        rc, out, _ = run([sys.executable, str(PREFLIGHT), "files", "--claude-dir", str(claude)], env=env2)
        t("files: gh absent from PATH is a hard FAIL gh-auth, exit 3",
          (rc, out) == (3, "FAIL gh-auth"), out)

        rc, out, _ = files(c2, env_with(bindir, "auth-fail"), "--allow-degraded")
        t("files: --allow-degraded downgrades to BLIND with the same tokens, exit 0",
          (rc, out) == (0, "BLIND snapshot-script gh-auth"), "%s %s" % (rc, out))


def section_snapshot(h, t):
    print("\n[Section 2] snapshot phase")

    def mk(tmp, md=None, err=None, bodies="x"):
        d = Path(tmp)
        if md is not None:
            (d / "in-flight.md").write_text(md)
        if err is not None:
            (d / "in-flight.err").write_text(err)
        if bodies is not None:
            (d / "in-flight-issues.md").write_text(bodies)
        return d

    with tempfile.TemporaryDirectory() as tmp:
        rc, out, _ = snap(mk(tmp, md="# ok\n"))
        t("snapshot: clean run is OK", (rc, out) == (0, "OK"), out)
    with tempfile.TemporaryDirectory() as tmp:
        rc, out, _ = snap(mk(tmp, md=""))
        t("snapshot: empty in-flight.md is FAIL snapshot-empty", (rc, out) == (3, "FAIL snapshot-empty"), out)
    with tempfile.TemporaryDirectory() as tmp:
        rc, out, _ = snap(mk(tmp, bodies=None))
        t("snapshot: missing in-flight.md is FAIL snapshot-empty", (rc, out) == (3, "FAIL snapshot-empty"), out)
        rc, out, _ = snap(mk(tmp, bodies=None), "--allow-degraded")
        t("snapshot: missing in-flight.md with --allow-degraded is BLIND snapshot-empty",
          (rc, out) == (0, "BLIND snapshot-empty"), out)
    with tempfile.TemporaryDirectory() as tmp:
        rc, out, _ = snap(mk(tmp, md="# x\n", err="issues: a\nprs: b\n"))
        t("snapshot: both issues and prs failing is FAIL snapshot-unavailable",
          (rc, out) == (3, "FAIL snapshot-unavailable"), out)
    for key in ("issues", "prs", "epics"):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = snap(mk(tmp, md="# x\n", err="%s: nope\n" % key))
            t("snapshot: only %s failing is a notice" % key,
              (rc, out) == (0, "BLIND snapshot-partial=%s" % key), out)
    with tempfile.TemporaryDirectory() as tmp:
        rc, out, _ = snap(mk(tmp, md="# x\n", bodies=""))
        t("snapshot: empty issue bodies is a notice", (rc, out) == (0, "BLIND snapshot-partial=issue-bodies"), out)


def section_blind_line(h, t):
    print("\n[Section 3] blind-line grammar")
    with tempfile.TemporaryDirectory() as tmp:
        tf = Path(tmp) / "tokens"
        tf.write_text("gh-auth\nscout-failed=docs\ngh-auth\n")
        rc, out, _ = blind(tf, "nick=no ADR index")
        t("blind-line: exact prefix, de-duplicated union joined with comma-space",
          (rc, out) == (0, "NORTH STAR BLIND: gh-auth, scout-failed=docs, nick=no ADR index"), out)
        before = sorted(p.name for p in Path(tmp).iterdir())
        blind(tf)
        t("blind-line: side-effect free", before == sorted(p.name for p in Path(tmp).iterdir()))
        rc, out, _ = blind(Path(tmp) / "missing")
        t("blind-line: missing file and no extras prints nothing, exit 0", (rc, out) == (0, ""), out)
        rc, out, err = blind(tf, "bogus-token")
        t("blind-line: unknown extra token becomes nick=unparseable, exit 0",
          (rc, out) == (0, "NORTH STAR BLIND: gh-auth, scout-failed=docs, nick=unparseable"), out)
        t("blind-line: an invalid token is warned about on stderr", "invalid" in err, err)
        rc, out, _ = blind(tf, "nick=a,b", "bogus-token")
        t("blind-line: several invalid tokens de-duplicate to one nick=unparseable",
          (rc, out) == (0, "NORTH STAR BLIND: gh-auth, scout-failed=docs, nick=unparseable"), out)
        rc, out, _ = blind(Path(tmp) / "missing", "scout-failed=all", "nick=unparseable")
        t("blind-line: scout-failed=all and nick=unparseable accepted",
          (rc, out) == (0, "NORTH STAR BLIND: scout-failed=all, nick=unparseable"), out)
        nf = Path(tmp) / "nick.md"
        nf.write_text("NORTH STAR BLIND: nick=no ADR index\nbody\n")
        rc, out, _ = blind(tf, "--nick-file", str(nf))
        t("blind-line --nick-file: Nick's nick= token joins the union",
          (rc, out) == (0, "NORTH STAR BLIND: gh-auth, scout-failed=docs, nick=no ADR index"), out)
        nf.write_text("NORTH STAR BLIND: nick=a, b\n")
        rc, out, _ = blind(Path(tmp) / "missing", "--nick-file", str(nf))
        t("blind-line --nick-file: a comma-split piece that is not a nick= token collapses the whole line "
          "to exactly one nick=unparseable, exit 0",
          (rc, out) == (0, "NORTH STAR BLIND: nick=unparseable"), out)
        nf.write_text("Plain contribution, no blind line\n")
        rc, out, _ = blind(Path(tmp) / "missing", "--nick-file", str(nf))
        t("blind-line --nick-file: no blind line from Nick prints nothing", (rc, out) == (0, ""), out)
        rc, out, _ = blind(Path(tmp) / "missing", "--nick-file", str(Path(tmp) / "absent.md"))
        t("blind-line --nick-file: missing file prints nothing", (rc, out) == (0, ""), out)


def section_exit_table(h, t):
    print("\n[Section 3b] exit-code and degraded branches (table-driven)")

    def files_case(gh_mode="ok", break_snapshot=False, extra=()):
        def build(tmp):
            bindir = make_bin(tmp)
            claude = make_claude_dir(tmp)
            if break_snapshot:
                (claude / "scripts" / "in-flight-snapshot.py").unlink()
            return ["files", "--claude-dir", str(claude), *extra], env_with(bindir, gh_mode)
        return build

    def snap_case(md=None, err=None, extra=()):
        def build(tmp):
            d = Path(tmp) / "session"
            d.mkdir()
            if md is not None:
                (d / "in-flight.md").write_text(md)
            if err is not None:
                (d / "in-flight.err").write_text(err)
            (d / "in-flight-issues.md").write_text("x")
            return ["snapshot", "--dir", str(d), *extra], None
        return build

    def usage_case(args):
        def build(tmp):
            return args, None
        return build

    cases = [
        # (label, builder, want_rc, want_stdout)
        ("files: gh-auth alone is FAIL gh-auth, exit 3",
         files_case(gh_mode="auth-fail"), 3, "FAIL gh-auth"),
        ("files: gh-auth alone under --allow-degraded is BLIND gh-auth, exit 0",
         files_case(gh_mode="auth-fail", extra=("--allow-degraded",)), 0, "BLIND gh-auth"),
        ("files: snapshot-script plus gh-auth: FAIL lists both hard tokens, exit 3",
         files_case(gh_mode="auth-fail", break_snapshot=True), 3, "FAIL snapshot-script gh-auth"),
        ("files: both hard tokens under --allow-degraded: BLIND in order, exit 0",
         files_case(gh_mode="auth-fail", break_snapshot=True, extra=("--allow-degraded",)), 0,
         "BLIND snapshot-script gh-auth"),
        ("files: hard failure without --allow-degraded is exit 3",
         files_case(break_snapshot=True), 3, "FAIL snapshot-script"),
        ("snapshot: unavailable under --allow-degraded is BLIND snapshot-unavailable, exit 0",
         snap_case(md="# x\n", err="issues: a\nprs: b\n", extra=("--allow-degraded",)), 0,
         "BLIND snapshot-unavailable"),
        ("snapshot: unavailable plus an epics notice: FAIL omits the notice, exit 3",
         snap_case(md="# x\n", err="issues: a\nprs: b\nepics: c\n"), 3, "FAIL snapshot-unavailable"),
        ("snapshot: empty plus an epics notice: FAIL omits the notice, exit 3",
         snap_case(md="", err="epics: c\n"), 3, "FAIL snapshot-empty"),
        ("snapshot: empty plus an epics notice under --allow-degraded: BLIND hard then notice, exit 0",
         snap_case(md="", err="epics: c\n", extra=("--allow-degraded",)), 0,
         "BLIND snapshot-empty snapshot-partial=epics"),
        ("snapshot: clean run with an epics notice is BLIND snapshot-partial=epics, exit 0",
         snap_case(md="# x\n", err="epics: c\n"), 0, "BLIND snapshot-partial=epics"),
        ("usage: no subcommand is exit 2", usage_case([]), 2, ""),
        ("usage: unknown subcommand is exit 2", usage_case(["bogus"]), 2, ""),
        ("usage: snapshot without --dir is exit 2", usage_case(["snapshot"]), 2, ""),
    ]
    for label, build, want_rc, want_out in cases:
        with tempfile.TemporaryDirectory() as tmp:
            args, env = build(tmp)
            rc, out, err = run([sys.executable, str(PREFLIGHT), *args], env=env)
            t(label, (rc, out) == (want_rc, want_out), "rc=%s out=%r err=%r" % (rc, out, err[:120]))


def section_contract(h, t):
    print("\n[Section 4] contract pin: real in-flight-snapshot.py through the preflight")
    cases = [
        ("fail", "FAIL snapshot-unavailable", 3),
        ("issues-fail", "BLIND snapshot-partial=issues", 0),
        ("epics-fail", "BLIND snapshot-partial=epics", 0),
        ("prs-fail", "BLIND snapshot-partial=prs", 0),
        ("ok", "OK", 0),
    ]
    for mode, want, want_rc in cases:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = make_bin(tmp)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            # Capture the subprocess stderr separately from in-flight.err: the script
            # unlinks a stale in-flight.err when the snapshot has no failures, so the
            # test must not depend on that file existing after the run.
            with open(str(out_dir / "snapshot-stderr.txt"), "w") as err_file:
                proc = subprocess.run(
                    [sys.executable, str(SNAPSHOT), "--repo-dir", str(repo),
                     "--out", str(out_dir / "in-flight.md"), "--bodies-out", str(out_dir / "in-flight-issues.md")],
                    stdout=subprocess.PIPE, stderr=err_file,
                    universal_newlines=True, env=env_with(bindir, mode))
            rc, out, _ = snap(out_dir)
            t("contract (%s): snapshot exits 0 and preflight says %s" % (mode, want),
              proc.returncode == 0 and (rc, out) == (want_rc, want),
              "snap=%s preflight=%s %s err=%s" % (proc.returncode, rc, out,
                                                  read_optional(out_dir / "in-flight.err")[:120]))


def section_vocabulary(h, t):
    print("\n[Section 4b] token vocabulary and remedies (module-level)")
    pf = preflight_module()
    emitted = (list(pf.FILE_TOKENS) + list(pf.FIXED_TOKENS)
               + ["snapshot-partial=" + s for s in pf.PARTIAL_SECTIONS]
               + ["scout-failed=" + s for s in pf.SCOUT_LENSES]
               + ["nick=unparseable"])
    rejected = [tok for tok in emitted if not pf.valid_token(tok)]
    t("vocabulary: every token the script can emit passes valid_token", not rejected, ", ".join(rejected))
    t("vocabulary: FILE_TOKENS is derived from REQUIRED_FILES",
      tuple(tok for tok, _ in pf.REQUIRED_FILES) == tuple(pf.FILE_TOKENS))
    t("vocabulary: FIXED_TOKENS = FILE_TOKENS + (gh-auth, snapshot-empty, snapshot-unavailable)",
      tuple(pf.FIXED_TOKENS) == tuple(pf.FILE_TOKENS) + ("gh-auth", "snapshot-empty", "snapshot-unavailable"))

    gh_remedy = "gh auth login"
    setup = "/setup-local"
    r = pf._remedy(["gh-auth"])
    t("remedy: gh-auth gets the gh auth login remedy and not /setup-local",
      gh_remedy in r and setup not in r, r)
    r = pf._remedy(["snapshot-unavailable"])
    t("remedy: snapshot-unavailable gets the gh auth login remedy and not /setup-local",
      gh_remedy in r and setup not in r, r)
    r = pf._remedy(["snapshot-partial=prs"])
    t("remedy: snapshot-partial=<section> gets the gh auth login remedy",
      gh_remedy in r and setup not in r, r)
    r = pf._remedy(["snapshot-script", "scout-prompt"])
    t("remedy: file tokens get the /setup-local remedy only",
      setup in r and gh_remedy not in r, r)
    r = pf._remedy(["snapshot-empty"])
    t("remedy: snapshot-empty gets a neutral remedy naming in-flight.md and in-flight.err",
      "in-flight.md" in r and "in-flight.err" in r and setup not in r and gh_remedy not in r, r)


def section_docs(h, t):
    print("\n[Section 5] doc wiring")
    plan = read("commands/expert-plan.md")
    review = read("commands/expert-review.md")
    panel = read("prompts/expert-review-panel.md")
    nick = read("reviewers/north-star-nick.yaml")
    script = read("scripts/north-star-preflight.py")

    # Guard: no doc swallows a snapshot failure with `|| true`. Whole-doc negative check.
    for name, doc in (("expert-plan", plan), ("expert-review", review), ("panel", panel)):
        t("%s: no '|| true' after in-flight-snapshot.py" % name,
          not re.search(r"in-flight-snapshot\.py[^\n]*\|\|\s*true", doc))
    # Guard: neither the plan nor the panel describes the snapshot gate as optional or non-blocking.
    t("expert-plan: no 'non-blocking' / 'never blocks' wording",
      "non-blocking" not in plan and "never blocks" not in plan)
    t("panel: no 'non-blocking' / 'never blocks' wording",
      "non-blocking" not in panel and "never blocks" not in panel)
    # Guard: plan Step 1 skips the snapshot on a missing script; Step 2 skips scouts on a missing prompt.
    t("expert-plan: Step 1 skips the snapshot on snapshot-script, Step 2 skips scouts on scout-prompt",
      "snapshot-script" in section(plan, "Step 1:") and "scout-prompt" in section(plan, "Step 2:"))
    # Guard: panel Step 6 runs the snapshot phase and records failed scouts as scout-failed= tokens.
    p6 = section(panel, "Step 6:")
    t("panel: Step 6 runs the snapshot phase and appends scout-failed tokens",
      "north-star-preflight.py\" snapshot --dir" in p6 and "scout-failed=" in p6)
    # Guard: plan Step 1 runs the snapshot phase and Step 2 appends scout-failed tokens.
    t("expert-plan: Step 1 runs the snapshot phase, Step 2 appends scout-failed tokens",
      "north-star-preflight.py\" snapshot --dir" in section(plan, "Step 1:")
      and "scout-failed=" in section(plan, "Step 2:"))
    # Guard: the token file is the single source; the retired NS_BLIND_TOKENS variable never returns.
    for name, doc in (("expert-plan", plan), ("expert-review", review), ("panel", panel)):
        t("%s: uses north-star-blind.tokens, never NS_BLIND_TOKENS" % name,
          "north-star-blind.tokens" in doc and "NS_BLIND_TOKENS" not in doc)
    # Guard: both entry points bootstrap the script and run the files phase before anything else.
    t("expert-plan: Step 0 has the test -f bootstrap and calls the files phase",
      ('test -f "$HOME/.claude/scripts/north-star-preflight.py"' in section(plan, "Step 0:")
       or '[ ! -f "$HOME/.claude/scripts/north-star-preflight.py" ]' in section(plan, "Step 0:"))
      and "north-star-preflight.py\" files" in section(plan, "Step 0:"))
    t("expert-review: Step 3 has the test -f bootstrap and calls the files phase",
      ('test -f "$HOME/.claude/scripts/north-star-preflight.py"' in section(review, "Step 3:")
       or '[ ! -f "$HOME/.claude/scripts/north-star-preflight.py" ]' in section(review, "Step 3:"))
      and "north-star-preflight.py\" files" in section(review, "Step 3:"))

    # Guard: --allow-degraded is parsed before the catch-all unknown-flag case in expert-plan.
    i_allow = section(plan, "Step 0:").find("--allow-degraded)")
    i_other = section(plan, "Step 0:").find("--*)")
    t("expert-plan: --allow-degraded case precedes the catch-all flag case",
      i_allow != -1 and (i_other == -1 or i_allow < i_other))
    # Guard: the flag is advertised in the command's argument-hint.
    t("expert-review: argument-hint lists --allow-degraded",
      re.search(r"^argument-hint:.*--allow-degraded", review, re.M) is not None)
    # Guard: the preflight is gated on EFFORT 3-5 and sits before PR Mode.
    i_pre = review.find("North Star preflight (#273)")
    t("expert-review: preflight is gated on EFFORT 3-5 and placed before PR Mode",
      i_pre != -1 and i_pre < review.find("### PR Mode") and "3, 4 and 5" in section(review, "Step 3:"))
    # Guard: Step 11 prepends the blind line and the output template prints it first.
    t("expert-review: Step 11 prepends the blind line and the template prints it first",
      "North Star blind line" in section(review, "Step 11:")
      and "NORTH STAR BLIND: {tokens}" in section(review, "Template for the in-conversation message"))
    # Guard: the panel falls back to the files-phase file and prepends the line to final-report.md.
    t("panel: gated on EFFORT with the north-star-preflight.files fallback and final-report prepend",
      "north-star-preflight.files" in panel and "never of the Router's roster" in panel
      and "prepend it to `final-report.md`" in section(panel, "Step 10:"))

    # Guard: the blind prefix is spelled identically in the script, both commands, the panel and Nick.
    m = re.search(r'BLIND_PREFIX = "([^"]+)"', script)
    prefix = m.group(1) if m else None
    t("NORTH STAR BLIND prefix in docs and persona matches the script constant",
      prefix == "NORTH STAR BLIND: " and all(prefix.rstrip() in d for d in (plan, review, panel, nick)))
    # Guard: Nick's persona no longer refers to the retired issues-index wording.
    t("persona: no 'issues index' wording", "issues index" not in nick)
    # Guard: the two ADRs record the 2026-10-13 --allow-degraded amendment.
    for adr in ("docs/adr/0003-tagger-routing.md", "docs/adr/0020-expert-plan-v3-focused-panel.md"):
        doc = read(adr)
        t("%s: 2026-10-13 amendment mentions --allow-degraded" % adr,
          "Amendment (2026-10-13)" in doc and "--allow-degraded" in doc)

    # Guard: every --failure-class literal the three docs pass to run-metrics.py is a telemetry class.
    schema = load_module("telemetry_schema", TELEMETRY_SCHEMA)
    literals = []
    for name, doc in (("expert-plan", plan), ("expert-review", review), ("panel", panel)):
        literals.extend((name, lit) for lit in re.findall(r"--failure-class\s+([A-Za-z][A-Za-z0-9_-]*)", doc))
    unknown = sorted({"%s:%s" % pair for pair in literals if pair[1] not in schema.FAILURE_CLASSES})
    t("docs: every --failure-class literal in expert-plan, expert-review and the panel is in FAILURE_CLASSES",
      bool(literals) and not unknown,
      "unknown: " + ", ".join(unknown) if unknown else "no literals found")


def make_repo(tmp):
    repo = Path(tmp) / "repo"
    (repo / "commands").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "commands" / "a.md").write_text("a\n")
    (repo / "scripts" / "b.py").write_text("# b\n")
    shutil.copy(str(INSTALL_SH), str(repo / "install.sh"))
    home = Path(tmp) / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    return repo, home, env


def tree(home):
    return sorted((str(p.relative_to(home)), os.readlink(str(p)) if p.is_symlink() else "")
                  for p in home.rglob("*"))


def seed_flow_hooks(home):
    """Pre-seed settings.json with the flow guard hooks so install.sh --check has no hook drift."""
    import json
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        ("Agent|Task", "flow-agent-guard.py"),
        ("Skill", "flow-skill-guard.py"),
        ("Bash", "flow-bash-guard.py"),
    ]
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": m, "hooks": [{"type": "command",
                                  "command": "%s/.claude/scripts/%s --agent auto" % (home, g)}]}
        for m, g in entries]}}))


def section_install(h, t):
    print("\n[Section 6] install.sh --check")
    with tempfile.TemporaryDirectory() as tmp:
        repo, home, env = make_repo(tmp)
        sh = str(repo / "install.sh")
        run(["bash", sh], env=env)
        seed_flow_hooks(home)
        rc, out, _ = run(["bash", sh, "--check"], env=env)
        t("--check: exit 0 after a clean install", rc == 0, out)
        link = home / ".claude" / "commands" / "a.md"
        link.unlink()
        before = tree(home)
        rc, out, _ = run(["bash", sh, "--check"], env=env)
        t("--check: deleted link reported as unlinked, exit 1", rc == 1 and "unlinked: " + str(link) in out, out)
        t("--check: changes nothing", before == tree(home))
        run(["bash", sh], env=env)
        (repo / "scripts" / "new.py").write_text("# n\n")
        rc, out, _ = run(["bash", sh, "--check"], env=env)
        t("--check: a new repo file is reported unlinked", rc == 1 and "unlinked: " in out and "new.py" in out, out)
        run(["bash", sh], env=env)
        (repo / "scripts" / "new.py").unlink()
        rc, out, _ = run(["bash", sh, "--check"], env=env)
        t("--check: a stale link into the repo is reported stale", rc == 1 and "stale: " in out, out)
        run(["bash", sh], env=env)
        user = home / ".claude" / "commands" / "mine.md"
        user.write_text("mine\n")
        (home / ".claude" / "commands" / "a.md").unlink()
        (home / ".claude" / "commands" / "a.md").write_text("user copy\n")
        rc, out, _ = run(["bash", sh, "--check"], env=env)
        t("--check: a user-owned file in the way is not-ours and not drift",
          rc == 0 and "not-ours: " in out, "%s %s" % (rc, out))


def main():
    h = Harness("NORTH STAR PREFLIGHT TEST SUITE (#273)")
    t = h.test_result
    section_files(h, t)
    section_snapshot(h, t)
    section_blind_line(h, t)
    section_exit_table(h, t)
    section_contract(h, t)
    section_vocabulary(h, t)
    section_docs(h, t)
    section_install(h, t)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
