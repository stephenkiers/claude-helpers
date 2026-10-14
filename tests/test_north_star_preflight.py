#!/usr/bin/env python3
"""
Test suite for the North Star preflight (#273): scripts/north-star-preflight.py, its contract with
scripts/in-flight-snapshot.py, the doc wiring in the plan/review commands and the panel, and
`install.sh --check`.

Run with: python3 tests/test_north_star_preflight.py
"""

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


def section_files(h, t):
    print("[Section 1] files phase")
    with tempfile.TemporaryDirectory() as tmp:
        bindir = make_bin(tmp)
        env = env_with(bindir)
        claude = make_claude_dir(tmp)
        rc, out, _ = files(claude, env)
        t("files: everything present gives OK exit 0", (rc, out) == (0, "OK"), "%s %s" % (rc, out))

        c2 = make_claude_dir(Path(tmp) / "a", snapshot=False) if (Path(tmp) / "a").mkdir() is None else None
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
        t("files: gh auth failure is FAIL gh-auth", (rc, out) == (3, "FAIL gh-auth"), out)

        empty_bin = Path(tmp) / "emptybin"
        empty_bin.mkdir()
        env2 = os.environ.copy()
        env2["PATH"] = str(empty_bin)
        rc, out, _ = run([sys.executable, str(PREFLIGHT), "files", "--claude-dir", str(claude)], env=env2)
        t("files: gh absent from PATH is FAIL gh-auth", (rc, out) == (3, "FAIL gh-auth"), out)

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
        rc, out, _ = blind(tf, "bogus-token")
        t("blind-line: unknown token exits 2", rc == 2, out)
        rc, out, _ = blind(tf, "nick=a,b")
        t("blind-line: nick with a comma exits 2", rc == 2, out)
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
        t("blind-line --nick-file: a comma in Nick's text becomes nick=unparseable, exit 0",
          (rc, out) == (0, "NORTH STAR BLIND: nick=a, nick=unparseable"), out)
        nf.write_text("Plain contribution, no blind line\n")
        rc, out, _ = blind(Path(tmp) / "missing", "--nick-file", str(nf))
        t("blind-line --nick-file: no blind line from Nick prints nothing", (rc, out) == (0, ""), out)
        rc, out, _ = blind(Path(tmp) / "missing", "--nick-file", str(Path(tmp) / "absent.md"))
        t("blind-line --nick-file: missing file prints nothing", (rc, out) == (0, ""), out)


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
            proc = subprocess.run(
                [sys.executable, str(SNAPSHOT), "--repo-dir", str(repo),
                 "--out", str(out_dir / "in-flight.md"), "--bodies-out", str(out_dir / "in-flight-issues.md")],
                stdout=subprocess.PIPE, stderr=open(str(out_dir / "in-flight.err"), "w"),
                universal_newlines=True, env=env_with(bindir, mode))
            rc, out, _ = snap(out_dir)
            t("contract (%s): snapshot exits 0 and preflight says %s" % (mode, want),
              proc.returncode == 0 and (rc, out) == (want_rc, want),
              "snap=%s preflight=%s %s err=%s" % (proc.returncode, rc, out,
                                                  (out_dir / "in-flight.err").read_text()[:120]))


def section_docs(h, t):
    print("\n[Section 5] doc wiring")
    plan = read("commands/expert-plan.md")
    review = read("commands/expert-review.md")
    panel = read("prompts/expert-review-panel.md")
    nick = read("reviewers/north-star-nick.yaml")
    script = read("scripts/north-star-preflight.py")

    for name, doc in (("expert-plan", plan), ("expert-review", review), ("panel", panel)):
        t("%s: no '|| true' after in-flight-snapshot.py" % name,
          not re.search(r"in-flight-snapshot\.py[^\n]*\|\|\s*true", doc))
    for name, doc in (("expert-plan", plan), ("panel", panel)):
        t("%s: no 'non-blocking' / 'never blocks' wording" % name,
          "non-blocking" not in doc and "never blocks" not in doc)
        t("%s: skips the snapshot on snapshot-script and scouts on scout-prompt" % name,
          "snapshot-script" in doc and "scout-prompt" in doc)
        t("%s: runs the snapshot phase and appends scout-failed tokens" % name,
          "north-star-preflight.py\" snapshot --dir" in doc and "scout-failed=" in doc)
    for name, doc in (("expert-plan", plan), ("expert-review", review), ("panel", panel)):
        t("%s: uses north-star-blind.tokens, never NS_BLIND_TOKENS" % name,
          "north-star-blind.tokens" in doc and "NS_BLIND_TOKENS" not in doc)
    for name, doc in (("expert-plan", plan), ("expert-review", review)):
        t("%s: test -f bootstrap and files phase present" % name,
          'test -f "$HOME/.claude/scripts/north-star-preflight.py"' in doc or
          '[ ! -f "$HOME/.claude/scripts/north-star-preflight.py" ]' in doc)
        t("%s: calls the files phase" % name, "north-star-preflight.py\" files" in doc)

    i_allow = plan.find("--allow-degraded)")
    i_other = plan.find("--*)")
    t("expert-plan: --allow-degraded case precedes the catch-all flag case",
      i_allow != -1 and (i_other == -1 or i_allow < i_other))
    t("expert-review: argument-hint lists --allow-degraded",
      re.search(r"^argument-hint:.*--allow-degraded", review, re.M) is not None)
    i_pre = review.find("North Star preflight (#273)")
    t("expert-review: preflight is gated on EFFORT 3-5 and placed before PR Mode",
      i_pre != -1 and i_pre < review.find("### PR Mode") and "3, 4 and 5" in review)
    t("expert-review: Step 11 prepends the blind line and the template prints it first",
      "North Star blind line" in review and "NORTH STAR BLIND: {tokens}" in review)
    t("panel: gated on EFFORT with the north-star-preflight.files fallback and final-report prepend",
      "north-star-preflight.files" in panel and "never of the Router's roster" in panel
      and "prepend it to `final-report.md`" in panel)

    m = re.search(r'BLIND_PREFIX = "([^"]+)"', script)
    prefix = m.group(1) if m else None
    t("NORTH STAR BLIND prefix in docs and persona matches the script constant",
      prefix == "NORTH STAR BLIND: " and all(prefix.rstrip() in d for d in (plan, review, panel, nick)))
    t("persona: no 'issues index' wording", "issues index" not in nick)
    for adr in ("docs/adr/0003-tagger-routing.md", "docs/adr/0020-expert-plan-v3-focused-panel.md"):
        doc = read(adr)
        t("%s: 2026-10-13 amendment mentions --allow-degraded" % adr,
          "Amendment (2026-10-13)" in doc and "--allow-degraded" in doc)


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


def section_install(h, t):
    print("\n[Section 6] install.sh --check")
    with tempfile.TemporaryDirectory() as tmp:
        repo, home, env = make_repo(tmp)
        sh = str(repo / "install.sh")
        run(["bash", sh], env=env)
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
    section_contract(h, t)
    section_docs(h, t)
    section_install(h, t)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
