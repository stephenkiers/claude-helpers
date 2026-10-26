#!/usr/bin/env python3
"""Plan-derived tests for the curated-layer review fixes (ADR-0024, Precedent Pat).

Written from the review action plan for feature/297 (round 2), not from the implementation.
Every test drives the real scripts/precedent-digest.py or scripts/precedent-render.py as a
subprocess, with HOME pinned to a temp directory and a synthetic git fixture (GitFixture).

Covered plan items:
  F1/F2  symlink escaping the repo is rejected and counted once; non-.md entries in the
         curated directories are skipped silently and are not counted as rejections
  F1     absolute and ".." directory settings in the curated yaml are rejected
  F8     a dangling symlink does not discard the whole curated layer
  F4     one unreadable file does not discard the layer and is not a whole-layer error
  F10    the curated layer is capped at 20 entries and the overflow is counted
  F11    a UTF-8 BOM in a curated .md file and in the curated yaml is handled
  F20    an oversize (>64 KiB) yaml yields exit 0, zero curated entries and a status reason
  F9     an unwritable --status-out / --out names the flag that failed
  F3     a curated citation's answer is compared whitespace-normalized; a mismatched
         curated citation drops only its own block

Run with: python3 tests/test_precedent_curated_review_fixes.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _git_fixture import GitFixture
from _test_harness import Harness, REPO_ROOT

DIGEST = REPO_ROOT / "scripts" / "precedent-digest.py"
RENDER = REPO_ROOT / "scripts" / "precedent-render.py"

ORIGIN = "https://github.com/exowner/fx.git"
YAML_PATH = Path(".claude") / "reviewers" / "north-star-nick-local.yaml"
YAML = (
    "project: fx\n"
    "strategicDocuments:\n"
    "  decisionsDirectory: docs/decisions/\n"
    "  constraintsDirectory: docs/constraints/\n"
)
ALPHA = (
    "# Alpha Rule\n\n"
    "Rule: Always pin the schema version before deploy.\n\n"
    "Spirit: Drift is silent otherwise.\n"
)
BETA = (
    "# Beta Constraint\n\n"
    "## Decision\n\n"
    "Keep the reader stateless.\n\n"
    "## Rationale\n\n"
    "Easier to roll back.\n"
)
ALPHA_ANSWER = "Always pin the schema version before deploy."


def put(path, data):
    """Write str (utf-8) or bytes to path, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def make_repo(tmp, yaml_text=YAML):
    """Synthetic repo with an origin remote (so the repo key resolves) and the curated yaml."""
    fx = GitFixture(tmp / "fx")
    fx._run_git(["remote", "add", "origin", ORIGIN])
    root = fx.repo_root
    put(root / YAML_PATH, yaml_text)
    (root / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "constraints").mkdir(parents=True, exist_ok=True)
    return root


def clean_env(home):
    """Child environment: HOME pinned, ambient session id dropped (mirrors run_all.py)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    return {**env, "HOME": str(home)}


def run_digest(root, home, *extra):
    cmd = [sys.executable, str(DIGEST), "--format", "jsonl", *extra, str(root)]
    return subprocess.run(cmd, capture_output=True, text=True, env=clean_env(home))


def run_digest_with_status(root, home, tmp):
    """Run the digest with --status-out; return (process, status dict or None)."""
    status_path = tmp / "status.json"
    proc = run_digest(root, home, "--status-out", str(status_path))
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else None
    return proc, status


def curated_entries(stdout):
    entries = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return [e for e in entries if e.get("source") == "curated"]


def run_validate(tmp, precedent_text, questions, digest_stdout):
    (tmp / "precedent.md").write_text(precedent_text, encoding="utf-8")
    (tmp / "questions.json").write_text(json.dumps({"questions": questions}), encoding="utf-8")
    (tmp / "digest.jsonl").write_text(digest_stdout, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(RENDER), "validate",
         "--precedent", str(tmp / "precedent.md"),
         "--questions", str(tmp / "questions.json"),
         "--digest", str(tmp / "digest.jsonl")],
        capture_output=True, text=True, env=clean_env(tmp),
    )
    try:
        result = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        result = {}
    return proc, result


def curated_block(qid, index, answer, path, prediction):
    return (
        f"## Q{index} [{qid}] Pin schema?\n\n"
        f"**Precedent**: curated rule \"{answer}\" ({path}) · spirit: \"Drift is silent otherwise.\"\n"
        f"**Prediction**: {prediction}\n"
        f"**Confidence**: medium\n"
        f"**Why**: Project rule.\n"
        f"**Distinguish**: None.\n\n"
    )


def precedent_doc(blocks_text, count):
    return (
        "# Precedent — session-curated\n\n"
        f"precedent-pat | questions: {count} | spoke-on: {count} | citations: {count} | "
        f"confidence: high=0 medium={count}\n\n"
        + blocks_text
    )


def question(qid, index):
    return {
        "id": qid,
        "header": f"Pin schema {index}?",
        "question": "Pin it?",
        "options": ["Pin the schema", "Skip the pin"],
        "recommended": "Skip the pin",
    }


# ============================================================================
# F2: non-.md entries are skipped silently; F1: escaping symlink is rejected once
# ============================================================================

def case_happy_path(h):
    with tempfile.TemporaryDirectory(prefix="curated-happy-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        put(root / "docs" / "constraints" / "beta-constraint.md", BETA)

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        by_header = {e.get("header"): e for e in entries}
        alpha = by_header.get("Alpha Rule", {})
        beta = by_header.get("Beta Constraint", {})

        h.test_result("happy: exit 0 with two curated rules", proc.returncode == 0,
                      f"rc={proc.returncode}")
        h.test_result("happy: each curated .md file yields one curated entry",
                      sorted(by_header) == ["Alpha Rule", "Beta Constraint"],
                      f"headers={sorted(by_header)}")
        h.test_result("happy: 'Rule:' line becomes the curated answer",
                      alpha.get("answer") == ALPHA_ANSWER,
                      f"answer={alpha.get('answer')!r}")
        h.test_result("happy: 'Spirit:' line becomes the curated reason",
                      alpha.get("reason") == "Drift is silent otherwise.",
                      f"reason={alpha.get('reason')!r}")
        h.test_result("happy: '## Decision' prose used when no 'Rule:' line",
                      beta.get("answer") == "Keep the reader stateless.",
                      f"answer={beta.get('answer')!r}")
        h.test_result("happy: '## Rationale' prose becomes the reason",
                      beta.get("reason") == "Easier to roll back.",
                      f"reason={beta.get('reason')!r}")
        h.test_result("happy: curated entries carry an absolute path",
                      bool(entries) and all(str(e.get("path", "")).startswith("/") for e in entries),
                      f"paths={[e.get('path') for e in entries]}")
        h.test_result("happy: status reports ok curated layer with no rejections",
                      status is not None
                      and status.get("yaml") == "ok"
                      and status.get("curated_root") == "ok"
                      and status.get("curated") == 2
                      and status.get("curated_error") is None
                      and status.get("curated_rejected") == 0,
                      f"status={status}")


def case_non_md_skipped_silently(h):
    with tempfile.TemporaryDirectory(prefix="curated-nonmd-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        put(root / "docs" / "decisions" / "notes.txt", "just some notes\n")
        put(root / "docs" / "decisions" / "rules.json", '{"rule": "not markdown"}\n')
        put(root / "docs" / "constraints" / "rules.yaml", "Rule: not markdown\n")

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        headers = sorted(e.get("header") for e in entries)
        non_md = [e.get("path") for e in entries
                  if not str(e.get("path", "")).endswith(".md")]

        h.test_result("F2: non-.md files in curated dirs produce no curated entries",
                      headers == ["Alpha Rule"] and not non_md,
                      f"headers={headers} non_md={non_md}")
        h.test_result("F2: non-.md files are not counted as curated rejections",
                      status is not None and status.get("curated_rejected") == 0,
                      f"status={status}")
        h.test_result("F2: skipping non-.md files leaves the layer error-free",
                      status is not None and status.get("curated_error") is None
                      and proc.returncode == 0,
                      f"rc={proc.returncode} status={status}")


def case_escaping_symlink_rejected_once(h):
    with tempfile.TemporaryDirectory(prefix="curated-escape-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        outside = tmp / "outside"
        put(outside / "secret.md",
            "# Secret Rule\n\nRule: Leaked text from outside the repo.\n")
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        os.symlink(outside / "secret.md", root / "docs" / "decisions" / "leak.md")

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        leaked = [e for e in entries if "Leaked" in str(e.get("answer", ""))
                  or "leak.md" in str(e.get("path", ""))]

        h.test_result("F1: symlink escaping the repo yields no curated entry",
                      not leaked, f"leaked={leaked}")
        h.test_result("F1: escaping symlink is counted as exactly one curated rejection",
                      status is not None and status.get("curated_rejected") == 1,
                      f"status={status}")
        h.test_result("F1: in-repo rule beside the escaping symlink is still used",
                      [e.get("header") for e in entries] == ["Alpha Rule"],
                      f"headers={[e.get('header') for e in entries]}")


def case_outside_directory_setting_rejected(h):
    # Absolute path in the yaml.
    with tempfile.TemporaryDirectory(prefix="curated-abs-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        outside = tmp / "outside-decisions"
        put(outside / "abs-rule.md", "# Outside Rule\n\nRule: Absolute path rule.\n")
        yaml_text = (
            "project: fx\nstrategicDocuments:\n"
            f"  decisionsDirectory: {outside}/\n"
        )
        root = make_repo(tmp, yaml_text=yaml_text)

        proc, status = run_digest_with_status(root, home, tmp)
        headers = [e.get("header") for e in curated_entries(proc.stdout)]
        h.test_result("F1: absolute decisionsDirectory outside the repo yields no curated entry",
                      "Outside Rule" not in headers and proc.returncode == 0,
                      f"rc={proc.returncode} headers={headers}")

    # '..' segment in the yaml.
    with tempfile.TemporaryDirectory(prefix="curated-dotdot-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        yaml_text = (
            "project: fx\nstrategicDocuments:\n"
            "  decisionsDirectory: ../escape/\n"
        )
        root = make_repo(tmp, yaml_text=yaml_text)
        put(root.parent / "escape" / "up-rule.md", "# Up Rule\n\nRule: Parent directory rule.\n")

        proc, status = run_digest_with_status(root, home, tmp)
        headers = [e.get("header") for e in curated_entries(proc.stdout)]
        h.test_result("F1: '..' segment in decisionsDirectory yields no curated entry",
                      "Up Rule" not in headers and proc.returncode == 0,
                      f"rc={proc.returncode} headers={headers}")


# ============================================================================
# F8 / F4: one bad file does not discard the layer
# ============================================================================

def case_dangling_symlink_keeps_layer(h):
    with tempfile.TemporaryDirectory(prefix="curated-dangling-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        put(root / "docs" / "constraints" / "beta-constraint.md", BETA)
        os.symlink(root / "docs" / "decisions" / "nowhere.md",
                   root / "docs" / "decisions" / "gone.md")

        proc, status = run_digest_with_status(root, home, tmp)
        headers = sorted(e.get("header") for e in curated_entries(proc.stdout))
        h.test_result("F8: a dangling symlink does not discard the other curated rules",
                      proc.returncode == 0 and headers == ["Alpha Rule", "Beta Constraint"],
                      f"rc={proc.returncode} headers={headers} status={status}")


def case_unreadable_file_keeps_layer(h):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  (skip) F4 unreadable-file case: running as root, chmod 000 is not enforced")
        return
    with tempfile.TemporaryDirectory(prefix="curated-locked-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        locked = root / "docs" / "decisions" / "locked.md"
        put(locked, "# Locked Rule\n\nRule: Unreadable rule text.\n")
        locked.chmod(0)
        try:
            proc, status = run_digest_with_status(root, home, tmp)
        finally:
            locked.chmod(0o644)

        headers = [e.get("header") for e in curated_entries(proc.stdout)]
        h.test_result("F4: one unreadable curated file keeps the other rule",
                      proc.returncode == 0 and headers == ["Alpha Rule"],
                      f"rc={proc.returncode} headers={headers}")
        h.test_result("F4: one unreadable curated file is not reported as a whole-layer error",
                      status is not None and status.get("curated_error") is None
                      and status.get("curated") == 1,
                      f"status={status}")


# ============================================================================
# F10: cap of 20 with overflow counted
# ============================================================================

def case_cap_counts_overflow(h):
    with tempfile.TemporaryDirectory(prefix="curated-cap-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        decisions = root / "docs" / "decisions"
        for i in range(25):
            put(decisions / f"rule-{i:02d}.md",
                f"# Rule {i:02d}\n\nRule: Rule text {i:02d}.\n")

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        all_in_dir = all(Path(e.get("path", "")).parent == decisions
                         or str(decisions) in str(e.get("path", "")) for e in entries)

        h.test_result("F10: 25 curated rules keep exactly 20 entries",
                      proc.returncode == 0 and len(entries) == 20,
                      f"rc={proc.returncode} entries={len(entries)}")
        h.test_result("F10: the 5 overflow rules are counted as capped",
                      status is not None and status.get("curated") == 20
                      and status.get("curated_capped") == 5,
                      f"status={status}")
        h.test_result("F10: surviving entries all come from the decisions directory",
                      all_in_dir and len({e.get("path") for e in entries}) == 20,
                      f"paths={[e.get('path') for e in entries]}")


# ============================================================================
# F11: BOM handling in curated .md and yaml
# ============================================================================

def case_bom_markdown_and_yaml(h):
    with tempfile.TemporaryDirectory(prefix="curated-bom-md-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "bom-rule.md",
            b"\xef\xbb\xbf# Bom Rule\n\nRule: BOM rule text.\n")

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        entry = entries[0] if len(entries) == 1 else {}
        h.test_result("F11: BOM-prefixed curated .md header has no stray BOM character",
                      entry.get("header") == "Bom Rule",
                      f"entries={entries}")
        h.test_result("F11: BOM-prefixed curated .md still yields its Rule: text",
                      entry.get("answer") == "BOM rule text.",
                      f"answer={entry.get('answer')!r}")

    with tempfile.TemporaryDirectory(prefix="curated-bom-yaml-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp, yaml_text=b"\xef\xbb\xbf" + YAML.encode("utf-8"))
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)

        proc, status = run_digest_with_status(root, home, tmp)
        h.test_result("F11: BOM-prefixed curated yaml is still read (yaml ok, one rule)",
                      status is not None and status.get("yaml") == "ok"
                      and status.get("curated") == 1,
                      f"status={status}")


# ============================================================================
# F20: oversize yaml
# ============================================================================

def case_oversize_yaml(h):
    with tempfile.TemporaryDirectory(prefix="curated-oversize-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        padding = "# " + "x" * 126 + "\n"  # 129 bytes per line
        yaml_text = YAML + padding * 600  # about 77 KB, above the 64 KiB limit
        root = make_repo(tmp, yaml_text=yaml_text)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)

        proc, status = run_digest_with_status(root, home, tmp)
        entries = curated_entries(proc.stdout)
        reason = status.get("reason") if status else None

        h.test_result("F20: oversize yaml exits 0",
                      proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr[-300:]!r}")
        h.test_result("F20: oversize yaml yields zero curated entries",
                      status is not None and status.get("curated") == 0 and not entries,
                      f"status={status} entries={len(entries)}")
        h.test_result("F20: oversize yaml sets a non-empty status reason",
                      isinstance(reason, str) and reason.strip() != "",
                      f"reason={reason!r}")


# ============================================================================
# F9: unwritable output paths name the flag that failed
# ============================================================================

def case_unwritable_status_out_named(h):
    with tempfile.TemporaryDirectory(prefix="curated-statusout-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        bad = tmp / "no-such-dir" / "status.json"

        proc = run_digest(root, home, "--status-out", str(bad))
        h.test_result("F9: unwritable --status-out exits non-zero",
                      proc.returncode != 0, f"rc={proc.returncode}")
        h.test_result("F9: unwritable --status-out error names --status-out",
                      "--status-out" in proc.stderr,
                      f"stderr={proc.stderr[-300:]!r}")


def case_unwritable_out_named(h):
    with tempfile.TemporaryDirectory(prefix="curated-out-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        root = make_repo(tmp)
        put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
        bad = tmp / "no-such-dir" / "out.jsonl"

        proc = run_digest(root, home, "--out", str(bad))
        h.test_result("F9: unwritable --out exits non-zero",
                      proc.returncode != 0, f"rc={proc.returncode}")
        h.test_result("F9: unwritable --out error names --out",
                      "--out" in proc.stderr,
                      f"stderr={proc.stderr[-300:]!r}")


# ============================================================================
# F3: curated answer comparison is whitespace-normalized; mismatch drops one block
# ============================================================================

def digest_for_alpha(tmp, home):
    """Build a repo with the alpha rule and return (digest stdout, alpha entry path)."""
    root = make_repo(tmp)
    put(root / "docs" / "decisions" / "alpha-rule.md", ALPHA)
    proc = run_digest(root, home)
    entries = curated_entries(proc.stdout)
    assert len(entries) == 1, f"fixture did not produce one curated entry: {proc.stdout!r}"
    return proc.stdout, entries[0]["path"]


def case_curated_answer_whitespace_normalized(h):
    with tempfile.TemporaryDirectory(prefix="curated-norm-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        digest_stdout, path = digest_for_alpha(tmp, home)
        # Same rule text, with a doubled internal space and a trailing space.
        block = curated_block("q-001", 1, "Always pin the schema  version before deploy. ",
                              path, "Pin the schema")
        proc, result = run_validate(tmp, precedent_doc(block, 1),
                                    [question("q-001", 1)], digest_stdout)

        lines = result.get("blocks", {}).get("q-001", {}).get("precedent_lines", [])
        h.test_result("F3: whitespace-variant curated answer validates (status ok)",
                      proc.returncode == 0 and result.get("status") == "ok",
                      f"rc={proc.returncode} result={result}")
        h.test_result("F3: whitespace-variant curated citation is shown",
                      result.get("shown") == 1 and len(lines) == 1
                      and lines[0].get("source") == "curated",
                      f"shown={result.get('shown')} lines={lines}")


def case_curated_mismatch_drops_only_offending_block(h):
    with tempfile.TemporaryDirectory(prefix="curated-mismatch-") as d:
        tmp = Path(d)
        home = tmp / "home"
        home.mkdir()
        digest_stdout, path = digest_for_alpha(tmp, home)
        blocks = (
            curated_block("q-001", 1, "Never pin anything.", path, "Pin the schema")
            + curated_block("q-002", 2, ALPHA_ANSWER, path, "Pin the schema")
        )
        proc, result = run_validate(tmp, precedent_doc(blocks, 2),
                                    [question("q-001", 1), question("q-002", 2)],
                                    digest_stdout)

        kept = sorted(result.get("blocks", {}).keys())
        h.test_result("F3: mismatched curated citation's block is dropped",
                      "q-001" not in kept,
                      f"kept={kept} result={result}")
        h.test_result("F3: the matching curated block in the same file is kept",
                      kept == ["q-002"] and result.get("shown") == 1,
                      f"kept={kept} shown={result.get('shown')} asked={result.get('asked')}")


CASES = [
    ("happy path", case_happy_path),
    ("F2 non-.md skipped silently", case_non_md_skipped_silently),
    ("F1 escaping symlink rejected once", case_escaping_symlink_rejected_once),
    ("F1 outside directory setting rejected", case_outside_directory_setting_rejected),
    ("F8 dangling symlink keeps layer", case_dangling_symlink_keeps_layer),
    ("F4 unreadable file keeps layer", case_unreadable_file_keeps_layer),
    ("F10 cap counts overflow", case_cap_counts_overflow),
    ("F11 BOM markdown and yaml", case_bom_markdown_and_yaml),
    ("F20 oversize yaml", case_oversize_yaml),
    ("F9 unwritable --status-out named", case_unwritable_status_out_named),
    ("F9 unwritable --out named", case_unwritable_out_named),
    ("F3 whitespace-normalized answer", case_curated_answer_whitespace_normalized),
    ("F3 mismatch drops only its block", case_curated_mismatch_drops_only_offending_block),
]


def main():
    h = Harness("PRECEDENT CURATED REVIEW FIXES TEST SUITE")
    for name, fn in CASES:
        try:
            fn(h)
        except Exception as exc:  # a crashing case is a failure, not a suite abort
            h.test_result(f"{name}: case ran without exception", False,
                          f"{type(exc).__name__}: {exc}")
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
