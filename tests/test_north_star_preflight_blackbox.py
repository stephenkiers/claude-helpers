#!/usr/bin/env python3
"""
Black-box spec tests for North Star Nick's preflight and related wiring (#273, round 2 review).

Written from the action plan's "Doing it" items and rulings, not from the implementation. The
preflight script is driven only through its CLI (subprocess), and the install script through
`install.sh --check` in a throwaway HOME. The few doc checks read the docs at runtime and assert
on the rulings' observable wording; they do not parse prose beyond that.

Covers (plan item in parentheses):
1. files phase: missing installs FAIL with a neutral remedy; gh is advisory; blind run (F2, F3, F25)
2. snapshot phase: empty / partial / complete outputs; remedies per token (F2, F10-adjacent)
3. blind-line: token output, first-seen order, invalid-token fallback (F1, F24)
4. blind-line: free-text nick= validation: comma, control chars, length cap, encoding (F9, F15, F17, F27)
5. Emitter/consumer agreement: every token the phases emit is accepted by blind-line (F16)
6. Side-effect freedom of files and blind-line (F25)
7. Command docs: --allow-degraded wiring, failure-class literals, stale err cleanup (F4, F5, F10)
8. ADR notes: superseded pointer and scout-quality scope sentence (F6 ruling B, F13 ruling A)
9. install.sh --check: dir-symlink reported once; real file at managed path is non-drift (F18, F19)

Run with: python3 tests/test_north_star_preflight_blackbox.py
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

PREFLIGHT = REPO_ROOT / "scripts" / "north-star-preflight.py"
TELEMETRY_SCHEMA = REPO_ROOT / "scripts" / "telemetry_schema.py"
INSTALL_SH = REPO_ROOT / "install.sh"

BLIND_PREFIX = "NORTH STAR BLIND: "
REQUIRED_SOURCES = [
    ("scripts/in-flight-snapshot.py", "scripts/in-flight-snapshot.py"),
    ("prompts/north-star-scout.md", "prompts/north-star-scout.md"),
]


def read_repo(relative):
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def normalized(text):
    return " ".join(text.split())


def empty_path_dir(tmp):
    """A PATH directory with nothing in it, so `gh` and `git` cannot be resolved."""
    d = Path(tmp) / "empty-path"
    d.mkdir()
    return str(d)


def run_preflight(args, path_value=None, cwd=None):
    env = dict(os.environ)
    if path_value is not None:
        env["PATH"] = path_value
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT)] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    return result.returncode, result.stdout, result.stderr


def populated_claude_dir(tmp):
    """A ~/.claude-shaped dir holding the files the files phase checks for."""
    claude = Path(tmp) / "claude"
    for src, dst in REQUIRED_SOURCES:
        target = claude / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / src, target)
    return claude


def blind_tokens(stdout):
    """Tokens from the last 'NORTH STAR BLIND: ' line, split on commas or whitespace."""
    lines = [line for line in stdout.splitlines() if line.startswith(BLIND_PREFIX)]
    if not lines:
        return None
    body = lines[-1][len(BLIND_PREFIX):]
    return [t for t in re.split(r"[,\s]+", body) if t]


def status_tokens(stdout, status):
    """Tokens after the 'FAIL ' or 'BLIND ' status line emitted on stdout, or None."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(status + " "):
            return [t for t in re.split(r"[,\s]+", line[len(status) + 1:]) if t]
        if line == status:
            return []
    return None


def tree_state(root):
    """(relative path, bytes, mtime_ns) for every file under root, for side-effect checks."""
    state = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            state.append((str(path.relative_to(root)), path.read_bytes(), path.stat().st_mtime_ns))
    return state


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    h = Harness("NORTH STAR PREFLIGHT BLACK-BOX SPEC TEST SUITE")
    t = h.test_result

    # ------------------------------------------------------------------
    print("[Section 1] files phase: missing installs, gh advisory, blind run")

    with tempfile.TemporaryDirectory() as tmp:
        empty_claude = Path(tmp) / "empty-claude"
        empty_claude.mkdir()

        rc, out, err = run_preflight(["files", "--claude-dir", str(empty_claude)])
        combined = out + err
        t("files: missing installs hard-fail with exit 3",
          rc == 3, f"rc={rc}")
        fail_tokens = status_tokens(out, "FAIL")
        t("files: FAIL line names both missing install items",
          fail_tokens is not None and set(fail_tokens) == {"snapshot-script", "scout-prompt"},
          str(fail_tokens))
        t("files: missing-install remedy does NOT suggest `gh auth login` (F2)",
          "gh auth login" not in combined, combined.strip()[:300])
        t("files: missing-install remedy points at the install path (F2)",
          "/setup-local" in combined or "install.sh" in combined, combined.strip()[:300])

        rc, out, err = run_preflight(
            ["files", "--claude-dir", str(empty_claude), "--allow-degraded"])
        t("files: --allow-degraded turns a missing install into a blind run (exit 0)",
          rc == 0, f"rc={rc}")
        t("files: blind run reports the missing items in a BLIND line",
          set(status_tokens(out, "BLIND") or []) >= {"snapshot-script", "scout-prompt"},
          out.strip()[:300])

    with tempfile.TemporaryDirectory() as tmp:
        claude = populated_claude_dir(tmp)
        rc, out, err = run_preflight(["files", "--claude-dir", str(claude)])
        t("files: complete install passes with exit 0 (happy path)", rc == 0, f"rc={rc}")
        t("files: complete install prints OK, not FAIL",
          "OK" in out and "FAIL" not in out, out.strip()[:200])

    with tempfile.TemporaryDirectory() as tmp:
        claude = populated_claude_dir(tmp)
        rc, out, err = run_preflight(
            ["files", "--claude-dir", str(claude)], path_value=empty_path_dir(tmp))
        t("files: missing gh is advisory, not a hard stop (F3) — exit 0",
          rc == 0, f"rc={rc} stderr={err.strip()[:200]}")
        t("files: missing gh is reported as the gh-auth token (F3)",
          "gh-auth" in (status_tokens(out, "BLIND") or []), out.strip()[:300])

    # ------------------------------------------------------------------
    print("[Section 2] snapshot phase: empty, partial, complete")

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir)])
        t("snapshot: no in-flight.md is a hard fail with exit 3", rc == 3, f"rc={rc}")
        t("snapshot: empty output is reported as snapshot-empty",
          status_tokens(out, "FAIL") == ["snapshot-empty"], out.strip()[:200])
        t("snapshot-empty: remedy is neutral, not `gh auth login` (F2)",
          "gh auth login" not in (out + err), (out + err).strip()[:300])

        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir), "--allow-degraded"])
        t("snapshot: --allow-degraded lets snapshot-empty run blind (exit 0)",
          rc == 0 and status_tokens(out, "BLIND") == ["snapshot-empty"], f"rc={rc} out={out!r}")

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        (run_dir / "in-flight.md").write_text("# In-flight work snapshot\n\n## Open issues\n\n- x\n",
                                              encoding="utf-8")
        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir), "--allow-degraded"])
        t("snapshot: markdown with no bodies file is reported as snapshot-partial=issue-bodies",
          status_tokens(out, "BLIND") == ["snapshot-partial=issue-bodies"], out.strip()[:300])
        t("snapshot-partial: remedy names `gh auth login` (F2 positive case)",
          "gh auth login" in (out + err), (out + err).strip()[:300])
        t("snapshot-partial: remedy mentions --allow-degraded (F2)",
          "--allow-degraded" in (out + err), (out + err).strip()[:300])

    # Not covered here: a black-box "complete snapshot prints OK" happy path. Even a stub `gh`
    # returning a real issue with a body still yields snapshot-partial=issue-bodies, and the
    # complete-state contract is not described by the plan, so no honest fixture was found.

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        (run_dir / "in-flight.md").write_text("# In-flight\n\n- row\n", encoding="utf-8")
        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir)])
        # CLAUDE.md (project spec, not the plan): "missing inputs fail the run unless
        # --allow-degraded". A partial snapshot is a missing input, so it should not pass.
        t("snapshot-partial without --allow-degraded hard-stops with non-zero exit "
          "(CLAUDE.md: missing inputs fail the run unless --allow-degraded)",
          rc != 0, f"rc={rc} out={out.strip()[:200]!r}")

    # ------------------------------------------------------------------
    print("[Section 3] blind-line: token output and ordering")

    with tempfile.TemporaryDirectory() as tmp:
        tokens = Path(tmp) / "tokens.txt"
        tokens.write_text("", encoding="utf-8")

        rc, out, err = run_preflight(["blind-line", "--tokens-file", str(tokens)])
        t("blind-line: no tokens and no nick prints no blind line, exit 0",
          rc == 0 and BLIND_PREFIX not in out, f"rc={rc} out={out!r}")

        tokens.write_text("snapshot-script\nscout-prompt\n", encoding="utf-8")
        rc, out, err = run_preflight(["blind-line", "--tokens-file", str(tokens)])
        t("blind-line: newline-separated tokens-file yields a NORTH STAR BLIND line (exit 0)",
          rc == 0 and blind_tokens(out) is not None, f"rc={rc} out={out!r}")
        t("blind-line: tokens-file tokens appear in first-seen order, not alphabetical (F24)",
          blind_tokens(out) == ["snapshot-script", "scout-prompt"], str(blind_tokens(out)))

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens), "snapshot-empty", "gh-auth"])
        t("blind-line: positional tokens are appended after the tokens-file ones",
          blind_tokens(out) == ["snapshot-script", "scout-prompt", "snapshot-empty", "gh-auth"],
          str(blind_tokens(out)))

        tokens.write_text("", encoding="utf-8")
        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens), "gh-auth", "snapshot-script"])
        t("blind-line: positional order is kept first-seen (F24)",
          blind_tokens(out) == ["gh-auth", "snapshot-script"], str(blind_tokens(out)))

        rc, out, err = run_preflight(
            ["blind-line", "--tokens-file", str(tokens), "snapshot-empty", "bogus-token"])
        t("blind-line: an invalid token does not drop the line or exit 2 (F1) — exit 0",
          rc == 0, f"rc={rc} err={err.strip()[:200]}")
        t("blind-line: an invalid token falls back to nick=unparseable and keeps valid tokens (F1)",
          blind_tokens(out) == ["snapshot-empty", "nick=unparseable"], str(blind_tokens(out)))

    # ------------------------------------------------------------------
    print("[Section 4] blind-line: free-text nick= validation")

    with tempfile.TemporaryDirectory() as tmp:
        tokens = Path(tmp) / "tokens.txt"
        tokens.write_text("", encoding="utf-8")
        nick = Path(tmp) / "nick.txt"

        def blind_with_nick(text_bytes):
            nick.write_bytes(text_bytes)
            return run_preflight(
                ["blind-line", "--tokens-file", str(tokens), "--nick-file", str(nick),
                 "snapshot-empty"])

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=hello\n")
        t("nick: a plain nick= token is carried into the blind line (happy path)",
          rc == 0 and blind_tokens(out) == ["snapshot-empty", "nick=hello"],
          out.strip()[:200])

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=a, b\n")
        t("nick: a comma in the nick text collapses the whole line to nick=unparseable (F27)",
          blind_tokens(out) == ["snapshot-empty", "nick=unparseable"], str(blind_tokens(out)))

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=ok\x07bell\n")
        t("nick: a control character in the nick text maps to nick=unparseable (F9)",
          blind_tokens(out) == ["snapshot-empty", "nick=unparseable"], str(blind_tokens(out)))

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=" + b"y" * 80 + b"\n")
        t("nick: an 80-character nick is accepted verbatim (F9 cap is at least 80)",
          rc == 0 and ("nick=" + "y" * 80) in (blind_tokens(out) or []), out.strip()[:200])

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=" + b"y" * 201 + b"\n")
        t("nick: a 201-character nick is rejected to nick=unparseable (F9 cap is at most 200)",
          blind_tokens(out) == ["snapshot-empty", "nick=unparseable"], str(blind_tokens(out)))

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=\xff\xfe bad\n")
        t("nick: invalid UTF-8 in the nick file does not crash (F15) — exit 0, no traceback",
          rc == 0 and "Traceback" not in err, f"rc={rc} err={err.strip()[:200]}")
        t("nick: invalid UTF-8 still yields a NORTH STAR BLIND line (F15)",
          blind_tokens(out) is not None, out.strip()[:200])

        rc, out, err = blind_with_nick(
            b"## Pass 1\n\nNORTH STAR BLIND: nick=after-heading\n")
        t("nick: a NORTH STAR BLIND line after a heading line is still parsed (F7 scan option)",
          "nick=after-heading" in (blind_tokens(out) or []), out.strip()[:200])

        rc, out, err = blind_with_nick(b"NORTH STAR BLIND: nick=unparseable\n")
        t("nick: an explicit nick=unparseable from Nick is emitted as the sentinel (F17)",
          blind_tokens(out) == ["snapshot-empty", "nick=unparseable"], str(blind_tokens(out)))

        tokens.write_bytes(b"\xff\xfesnapshot-empty\n")
        nick.write_text("", encoding="utf-8")
        rc, out, err = run_preflight(["blind-line", "--tokens-file", str(tokens)])
        t("tokens-file: invalid UTF-8 does not crash (F15) — exit 0, no traceback",
          rc == 0 and "Traceback" not in err, f"rc={rc} err={err.strip()[:200]}")
        t("tokens-file: invalid UTF-8 token still yields a blind line with nick=unparseable (F15)",
          blind_tokens(out) == ["nick=unparseable"], str(blind_tokens(out)))

    # ------------------------------------------------------------------
    print("[Section 5] emitter/consumer agreement (F16)")

    with tempfile.TemporaryDirectory() as tmp:
        emitted = []

        rc, out, err = run_preflight(["files", "--claude-dir", str(Path(tmp) / "none"),
                                      "--allow-degraded"])
        emitted += status_tokens(out, "BLIND") or []

        rc, out, err = run_preflight(
            ["files", "--claude-dir", str(populated_claude_dir(tmp)), "--allow-degraded"],
            path_value=empty_path_dir(tmp))
        emitted += status_tokens(out, "BLIND") or []

        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir), "--allow-degraded"])
        emitted += status_tokens(out, "BLIND") or []

        (run_dir / "in-flight.md").write_text("# In-flight\n\n- row\n", encoding="utf-8")
        rc, out, err = run_preflight(["snapshot", "--dir", str(run_dir), "--allow-degraded"])
        emitted += status_tokens(out, "BLIND") or []

        emitted = sorted(set(emitted))
        t("emitter: the phases emit a non-trivial token set for the agreement check",
          len(emitted) >= 4, str(emitted))

        for token in emitted:
            tokens = Path(tmp) / "agree-tokens.txt"
            tokens.write_text("", encoding="utf-8")
            rc, out, err = run_preflight(["blind-line", "--tokens-file", str(tokens), token])
            t(f"emitter: blind-line accepts the emitted token '{token}' unchanged (F16)",
              rc == 0 and token in (blind_tokens(out) or []) and "nick=unparseable" not in out,
              f"rc={rc} out={out.strip()[:200]!r} err={err.strip()[:200]!r}")

    # ------------------------------------------------------------------
    print("[Section 6] side-effect freedom (F25)")

    with tempfile.TemporaryDirectory() as tmp:
        claude = populated_claude_dir(tmp)
        before = tree_state(claude)
        run_preflight(["files", "--claude-dir", str(claude)])
        after = tree_state(claude)
        t("files: never creates or modifies files (bytes and mtime unchanged) (F25)",
          before == after)

        tokens = Path(tmp) / "tokens.txt"
        tokens.write_text("snapshot-script\n", encoding="utf-8")
        nick = Path(tmp) / "nick.txt"
        nick.write_text("NORTH STAR BLIND: nick=hi\n", encoding="utf-8")
        before = tree_state(tmp)
        run_preflight(["blind-line", "--tokens-file", str(tokens), "--nick-file", str(nick)])
        after = tree_state(tmp)
        t("blind-line: never modifies its input files (bytes and mtime unchanged) (F25)",
          before == after)

    # ------------------------------------------------------------------
    print("[Section 7] command docs wiring (F4, F5, F10)")

    expert_plan = read_repo("commands/expert-plan.md")
    expert_review = read_repo("commands/expert-review.md")
    panel = read_repo("prompts/expert-review-panel.md")

    t("docs: expert-review.md parses --allow-degraded (F5)",
      "--allow-degraded" in expert_review)
    t("docs: expert-review.md assigns NS_FLAG for the degraded flag (F5)",
      "NS_FLAG" in expert_review)
    t("docs: expert-review-panel.md parses --allow-degraded (F5)",
      "--allow-degraded" in panel)
    t("docs: expert-review-panel.md assigns NS_FLAG for the degraded flag (F5)",
      "NS_FLAG" in panel)

    telemetry = load_module("telemetry_schema", TELEMETRY_SCHEMA)
    valid_classes = set(telemetry.FAILURE_CLASSES)
    literal_pattern = re.compile(r"--failure-class[ =]+[\"']?([A-Za-z_][A-Za-z0-9_\-]*)")
    literals = []
    for name, text in (("commands/expert-plan.md", expert_plan),
                       ("commands/expert-review.md", expert_review)):
        for m in literal_pattern.finditer(text):
            literals.append((name, m.group(1)))
    t("docs: at least one literal --failure-class value is present in the command docs (F4)",
      len(literals) >= 1, str(literals))
    bad = [(n, v) for n, v in literals if v not in valid_classes]
    t("docs: every literal --failure-class value is a FAILURE_CLASSES member (F4)",
      not bad, str(bad))

    # F10 offers two fixes: remove the stale outputs before the snapshot, or unlink a stale err.
    # Accept either primitive within a few lines of an in-flight.err reference.
    stale_primitives = ("rm -f", "unlink", "truncat", ": >")
    for name, text in (("commands/expert-plan.md", expert_plan),
                       ("commands/expert-review.md", expert_review)):
        lines = text.splitlines()
        ok = False
        for i, line in enumerate(lines):
            if "in-flight.err" in line:
                window = " ".join(lines[max(0, i - 3): i + 4])
                if any(p in window for p in stale_primitives):
                    ok = True
        t(f"docs: {name} clears a stale in-flight.err before the snapshot runs (F10)", ok)

    # ------------------------------------------------------------------
    print("[Section 8] ADR notes (F6 ruling B, F13 ruling A)")

    adr_0003 = read_repo("docs/adr/0003-tagger-routing.md")
    adr_0020 = read_repo("docs/adr/0020-expert-plan-v3-focused-panel.md")

    fail_open_hits = 0
    fail_open_ok = True
    for text in (adr_0003, adr_0020):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "Every piece is fail-open" in line:
                fail_open_hits += 1
                window = " ".join(lines[max(0, i - 2): i + 3]).lower()
                if "superseded" not in window:
                    fail_open_ok = False
    t("ADR: the 'Every piece is fail-open' contract is present in ADR-0003 (F13 anchor)",
      "Every piece is fail-open" in adr_0003)
    t("ADR: every 'Every piece is fail-open' statement carries a superseded pointer (F13)",
      fail_open_ok and fail_open_hits >= 1, f"hits={fail_open_hits}")

    combined_adr = normalized(adr_0003 + "\n" + adr_0020)
    scope_sentence = re.search(
        r"scout[^.]{0,200}(not gated|out of scope)|(not gated|out of scope)[^.]{0,200}scout",
        combined_adr, re.IGNORECASE)
    t("ADR: an amendment states scout output quality is not gated / out of scope (F6 ruling B)",
      scope_sentence is not None)

    # ------------------------------------------------------------------
    print("[Section 9] install.sh --check drift reporting (F18, F19)")

    def install_check(home):
        env = dict(os.environ)
        env["HOME"] = str(home)
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--check"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        return result.returncode, result.stdout + result.stderr

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        elsewhere = Path(tmp) / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, home / ".claude" / "commands")
        rc, out = install_check(home)
        reports = [line for line in out.splitlines()
                   if "dir-symlink" in line and ".claude/commands" in line]
        t("install --check: a directory symlink at a managed path is reported exactly once (F18)",
          len(reports) == 1, f"count={len(reports)}")
        t("install --check: a directory symlink still counts as drift (exit 1)",
          rc == 1, f"rc={rc}")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        install = subprocess.run(
            ["bash", str(INSTALL_SH)], capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)}, cwd=str(REPO_ROOT))
        t("install: a full install into a fresh HOME succeeds (precondition for F19 probe)",
          install.returncode == 0, install.stdout[-300:] + install.stderr[-300:])
        managed = home / ".claude" / "scripts" / "in-flight-snapshot.py"
        if managed.is_symlink():
            managed.unlink()
            managed.write_text("# a real file at a managed path\n", encoding="utf-8")
        rc, out = install_check(home)
        t("install --check: a real file at a managed path is non-drift (exit 0) (F19)",
          rc == 0, f"rc={rc} out={out.strip()[-300:]}")
        t("install --check: the real file at the managed path is still named in the output (F19)",
          "in-flight-snapshot.py" in out, out.strip()[-300:])

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
