#!/usr/bin/env python3
"""Tests for scripts/workflow/heal.py (expert-fleet bounded self-heal harness, #425).

Covers the core heal logic: nomination, probing, recommendations, override schema,
install tool status mapping, and error handling.

Run with: python3 tests/test_heal.py
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import REPO_ROOT, Harness

sys.path.insert(0, str(REPO_ROOT))
from scripts.workflow import heal  # noqa: E402

GIT = [
    "git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
]
ISSUE = 42
BRANCH = "feature/42-thing"
HEX_DIGITS = set("0123456789abcdef")


def make_evidence(**kwargs) -> heal.StopEvidence:
    """Build a StopEvidence with sensible defaults."""
    defaults = {
        "kind": "flow-stop",
        "header": "Flow stop",
        "step": "implement",
        "failures": {},
        "stop_source": "deps",
        "stop_reason_class": None,
        "last_deps_result": None,
        "offers_retry": False,
        "stop_key": "abc123",
    }
    defaults.update(kwargs)
    return heal.StopEvidence(**defaults)


def main():
    h = Harness("heal module tests")

    # ────────────────────────────────────────────────────────────────────────────
    # Section 1: nominate() — Tool nomination and refusals
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 1] nominate() — Tool nomination and refusals")

    # W1 A: failures >= 2 refuses (failure cap)
    evidence_w1_pass = make_evidence(
        failures={"implement": 1},
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_w1_pass, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures=1 allows nomination",
        tool is not None,
        f"Got diagnosis: {diag}",
    )

    evidence_w1_fail = make_evidence(failures={"implement": 2})
    tool, diag = heal.nominate(evidence_w1_fail, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures >= 2 refuses with 'Failure limit reached'",
        tool is None and "Failure limit reached" in (diag or ""),
        f"tool={tool}, diag={diag}",
    )

    evidence_w1_fail_3 = make_evidence(failures={"implement": 5})
    tool, diag = heal.nominate(evidence_w1_fail_3, heal.TOOLS, {})
    h.test_result(
        "W1 A: failures = 5 also refuses",
        tool is None and "Failure limit reached" in (diag or ""),
        f"diag={diag}",
    )

    # manifest-changed refusal
    evidence_manifest = make_evidence(
        last_deps_result="manifest-changed",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_manifest, heal.TOOLS, {})
    h.test_result(
        "manifest-changed refusal includes 'approval'",
        tool is None and "approval" in (diag or "").lower(),
        f"diag={diag}",
    )

    # permission-refusal marker never nominates
    evidence_perm = make_evidence(
        stop_reason_class="permission-refusal",
        stop_source="stop_reason",
    )
    tool, diag = heal.nominate(evidence_perm, heal.TOOLS, {})
    h.test_result(
        "permission-refusal marker never nominates",
        tool is None and "Permission refusal" in (diag or ""),
        f"diag={diag}",
    )

    # Install tool nomination (missing-deps / stale)
    evidence_install = make_evidence(
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_install, heal.TOOLS, {})
    h.test_result(
        "Nominates install tool when last_deps_result='stale'",
        tool is not None and tool.name == "install",
        f"tool={tool}, diag={diag}",
    )

    # Tool disabled in override
    evidence_install2 = make_evidence(
        last_deps_result="stale",
        stop_source="deps",
    )
    override_disabled = {"install": {"enabled": False}}
    tool, diag = heal.nominate(evidence_install2, heal.TOOLS, override_disabled)
    h.test_result(
        "Disabled tool in override is skipped",
        tool is None,
        f"tool={tool}",
    )

    # No evidence
    tool, diag = heal.nominate(None, heal.TOOLS, {})
    h.test_result(
        "No evidence returns (None, None)",
        tool is None and diag is None,
    )

    # Unknown stop kind
    evidence_bad_kind = make_evidence(kind="unknown-stop")
    tool, diag = heal.nominate(evidence_bad_kind, heal.TOOLS, {})
    h.test_result(
        "Unknown stop kind returns diagnosis",
        tool is None and diag is not None,
        f"diag={diag}",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 2: after_heal() — Recommendation logic
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 2] after_heal() — Recommendation logic")

    evidence_with_retry = make_evidence(
        offers_retry=True,
        last_deps_result=None,
    )
    result_changed = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed, evidence_with_retry)
    h.test_result(
        "after_heal: retry-recommended when offers_retry, changed, no manifest-changed",
        rec == "retry-recommended",
        f"Got {rec}",
    )

    evidence_no_retry = make_evidence(offers_retry=False)
    result_changed2 = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed2, evidence_no_retry)
    h.test_result(
        "after_heal: take-over when offers_retry=False",
        rec == "take-over",
        f"Got {rec}",
    )

    evidence_manifest_changed = make_evidence(
        offers_retry=True,
        last_deps_result="manifest-changed",
    )
    result_changed3 = heal.HealResult(
        tool="install",
        status="healed",
        reason="currency-installed",
        changed=True,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_changed3, evidence_manifest_changed)
    h.test_result(
        "after_heal: take-over when last_deps_result='manifest-changed'",
        rec == "take-over",
        f"Got {rec}",
    )

    result_no_change = heal.HealResult(
        tool="install",
        status="no-op",
        reason="currency-current",
        changed=False,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_no_change, evidence_with_retry)
    h.test_result(
        "after_heal: take-over when tool didn't change anything",
        rec == "take-over",
        f"Got {rec}",
    )

    result_healed_unchanged = heal.HealResult(
        tool="install",
        status="healed",
        reason="installed",
        changed=False,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_healed_unchanged, evidence_with_retry)
    h.test_result(
        "after_heal: take-over when healed but nothing changed",
        rec == "take-over",
        f"Got {rec}",
    )

    result_refused = heal.HealResult(
        tool="install",
        status="refused",
        reason="awaiting-approval",
        changed=False,
        recommendation="take-over",
        stop_key="abc",
    )
    rec = heal.after_heal(result_refused, evidence_with_retry)
    h.test_result(
        "after_heal: take-over for refused tools",
        rec == "take-over",
        f"Got {rec}",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 3: Override schema validation and fail-closed behavior
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 3] Override schema validation (fail-closed)")

    with tempfile.TemporaryDirectory() as tmp:
        main_wt = Path(tmp) / "main"
        main_wt.mkdir()

        # Valid override
        project_yaml = main_wt / ".claude" / "project.yaml"
        project_yaml.parent.mkdir(parents=True)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: true
        timeout_secs: 600
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Valid override reads successfully",
            override.get("install", {}).get("timeout_secs") == 600,
            f"Got {override}",
        )

        # Unknown key (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: true
        unknown_param: 999
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Unknown param fails closed with a diagnosis",
            odiag is not None,
            f"Got {override}",
        )

        # Invalid enabled type (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        enabled: "yes"
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Invalid enabled type returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Out-of-range timeout (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        timeout_secs: 0
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "timeout_secs <= 0 returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Negative timeout (fail-closed)
        project_yaml.write_text(
            """
fleet:
  heal:
    tools:
      install:
        timeout_secs: -100
"""
        )
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Negative timeout_secs returns empty override (fail-closed)",
            override == {} and odiag is not None,
            f"Got {override}",
        )

        # Missing project.yaml
        project_yaml.unlink()
        override, odiag = heal._read_project_override(main_wt)
        h.test_result(
            "Missing project.yaml returns empty override, no diagnosis",
            override == {} and odiag is None,
            f"Got {override}",
        )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 4: Install tool status mapping
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 4] Install tool status mapping (Steps 3-4)")

    # The install tool is already tested above; here we document the mapping:
    # installed -> healed
    # current -> no-op
    # manifest-changed -> refused
    # ambiguous -> refused
    # skipped -> refused
    # failed -> failed
    # unknown -> failed

    # These are tested implicitly through the mock DepsResult in the integration,
    # but we can at least verify the tool exists and has the right properties.
    h.test_result(
        "Install tool has correct cause",
        heal.INSTALL_TOOL.cause == "missing-deps",
    )
    h.test_result(
        "Install tool has gate='ensure-deps-exception'",
        heal.INSTALL_TOOL.gate == "ensure-deps-exception",
    )
    h.test_result(
        "Install tool has approval='run-first'",
        heal.INSTALL_TOOL.approval == "run-first",
    )
    h.test_result(
        "Install tool has permission='runs-repo-code'",
        heal.INSTALL_TOOL.permission == "runs-repo-code",
    )
    h.test_result(
        "Install tool has no inputs",
        heal.INSTALL_TOOL.inputs == (),
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 5: Diagnoses (causes 2-6)
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 5] Diagnoses for non-tool causes (Step 3)")

    h.test_result(
        "DIAGNOSES maps missing-check-config",
        "missing-check-config" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps check-source-flip",
        "check-source-flip" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps missing-test-db",
        "missing-test-db" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps unregistered-agent",
        "unregistered-agent" in heal.DIAGNOSES,
    )
    h.test_result(
        "DIAGNOSES maps malformed-receipt",
        "malformed-receipt" in heal.DIAGNOSES,
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 6: Tool registry (frozen tuple, one entry at end of Step 4)
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 6] Tool registry")

    h.test_result(
        "TOOLS is a tuple",
        isinstance(heal.TOOLS, tuple),
    )
    h.test_result(
        "TOOLS contains exactly one tool (install)",
        len(heal.TOOLS) == 1 and heal.TOOLS[0].name == "install",
    )

    # ────────────────────────────────────────────────────────────────────────────
    # Section 7: Edge cases and error handling
    # ────────────────────────────────────────────────────────────────────────────

    print()
    print("[Section 7] Edge cases and error handling")

    # Missing failures field (should default to 0)
    evidence_no_failures = make_evidence(
        failures={},
        last_deps_result="stale",
        stop_source="deps",
    )
    tool, diag = heal.nominate(evidence_no_failures, heal.TOOLS, {})
    h.test_result(
        "Missing failures[step] counts as 0",
        tool is not None,  # Should be able to nominate
        f"diag={diag}",
    )

    # No stop_source and no deps entry
    evidence_no_source = make_evidence(
        stop_source=None,
        last_deps_result=None,
    )
    tool, diag = heal.nominate(evidence_no_source, heal.TOOLS, {})
    h.test_result(
        "No stop_source and no deps entry returns no evidence",
        tool is None,
        f"diag={diag}",
    )

    section_classify(h)
    section_evidence(h)
    section_nominate_quirks(h)
    section_entrypoints(h)
    section_run_normalisation(h)
    section_tail_file(h)

    h.summarize_and_exit()


# ────────────────────────────────────────────────────────────────────────────
# Fixtures shared by the entry-point sections (real temp git repos, fake tools)
# ────────────────────────────────────────────────────────────────────────────

def git(cwd: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=str(cwd), check=True, capture_output=True)


def make_repo(tmp: Path) -> Tuple[Path, Path]:
    """A main repo with one commit and one linked worktree on BRANCH. Returns (main, worktree)."""
    main = tmp / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp / "wt-42"
    git(main, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    return main, worktree


def base_state(**overrides: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "issue": ISSUE,
        "step": "implement",
        "status": "paused",
        "stop_source": {"implement": "receipt"},
        "stop_reason": {"implement": "Check failed"},
        "failures": {},
        "deps": [{"step": "implement", "result": "failed", "at": "2026-11-03T00:00:00Z"}],
    }
    state.update(overrides)
    return state


def write_state(flow_dir: Path, **overrides: Any) -> None:
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "state.json").write_text(json.dumps(base_state(**overrides)))


def write_gate(flow_dir: Path, options: List[Any], **overrides: Any) -> bytes:
    ask: Dict[str, Any] = {
        "status": "open",
        "kind": "gate",
        "step": "implement",
        "gate": {"header": "Flow stop", "options": options},
    }
    ask.update(overrides)
    data = json.dumps(ask).encode("utf-8")
    (flow_dir / "fleet-ask.json").write_bytes(data)
    return data


def flow_with(tmp: Path, name: str, retry: bool = False, **state: Any) -> Path:
    flow = tmp / name
    write_state(flow, **state)
    if retry:
        write_gate(flow, [{"label": "Retry"}, {"label": "Abort"}])
    return flow


def fake_tool(run: Callable[..., Any], verify: Optional[Callable[..., bool]] = None,
              precondition: Optional[Callable[..., bool]] = None,
              name: str = "install", cause: str = "missing-deps") -> heal.Tool:
    return heal.Tool(
        name=name,
        cause=cause,
        permission="runs-repo-code",
        approval="run-first",
        gate="ensure-deps-exception",
        inputs=(),
        params=("timeout_secs",),
        precondition=precondition or (lambda worktree, main, base: True),
        run=run,
        verify=verify or (lambda worktree, main, base: True),
    )


@contextlib.contextmanager
def tools_swapped(*tools: heal.Tool):
    """heal.cli_run and cli_probe read the module-level TOOLS at call time, so swapping it is enough."""
    saved = heal.TOOLS
    heal.TOOLS = tuple(tools)
    try:
        yield
    finally:
        heal.TOOLS = saved


@contextlib.contextmanager
def os_write_replaced(replacement: Callable[..., int]):
    saved = os.write
    os.write = replacement  # type: ignore[assignment]
    try:
        yield
    finally:
        os.write = saved  # type: ignore[assignment]


def quietly(fn: Callable[..., Any], *args: Any) -> Tuple[Any, str]:
    """Run fn with stderr captured. Returns (result, captured stderr)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn(*args)
    return result, buf.getvalue()


def is_key(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 16 and set(value) <= HEX_DIGITS


# ────────────────────────────────────────────────────────────────────────────
# Section 8: _classify_reason fixtures (pins the unverified literals)
# ────────────────────────────────────────────────────────────────────────────

def section_classify(h: Harness) -> None:
    print()
    print("[Section 8] _classify_reason() fixtures (literals pinned here)")

    cases = [
        ("Permission to run npm test has been denied", "permission-refusal"),
        ("permission request was DENIED by policy", "permission-refusal"),
        ("check_not_configured: no check command resolved", "check-not-configured"),
        ("Reduced run (audit): 2 of 5 units ran", "reduced-run"),
        ("permission denied while check_not_configured", "permission-refusal"),
        ("permission prompt pending", None),
        ("denied by the reviewer", None),
        ("reduced run without an audit", None),
        ("", None),
    ]
    for reason, expected in cases:
        got = heal._classify_reason(reason)
        h.test_result(
            f"_classify_reason({reason!r}) is {expected!r}",
            got == expected,
            f"got {got!r}",
        )


# ────────────────────────────────────────────────────────────────────────────
# Section 9: _read_stop_evidence (valid, malformed, ask handling)
# ────────────────────────────────────────────────────────────────────────────

def section_evidence(h: Harness) -> None:
    print()
    print("[Section 9] _read_stop_evidence() — valid, malformed and ask cases")

    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp) / "flow"

        def read() -> Optional[heal.StopEvidence]:
            return heal._read_stop_evidence(flow, ISSUE)

        write_state(flow)
        ev = read()
        h.test_result(
            "valid paused state reads as a stopped flow",
            ev is not None
            and ev.kind == "stopped"
            and ev.step == "implement"
            and ev.stop_source == "receipt"
            and ev.last_deps_result == "failed"
            and ev.offers_retry is False
            and ev.header is None
            and ev.stop_reason_class is None
            and is_key(ev.stop_key),
            f"got {ev}",
        )

        write_state(flow, status="failed")
        ev = read()
        h.test_result("failed status reads as kind 'failed'", ev is not None and ev.kind == "failed", f"got {ev}")

        write_state(flow, stop_reason={"implement": "check_not_configured"})
        ev = read()
        h.test_result(
            "stop_reason classified into stop_reason_class",
            ev is not None and ev.stop_reason_class == "check-not-configured",
            f"got {ev}",
        )

        malformed: List[Tuple[str, Dict[str, Any]]] = [
            ("issue mismatch", {"issue": ISSUE + 1}),
            ("unknown step", {"step": "deploy"}),
            ("unknown status", {"status": "weird"}),
            ("stop_source outside the closed set", {"stop_source": {"implement": "bogus"}}),
            ("negative failure count", {"failures": {"implement": -1}}),
            ("boolean failure count", {"failures": {"implement": True}}),
            ("unknown deps result", {"deps": [{"step": "implement", "result": "maybe", "at": "x"}]}),
            ("deps not a list", {"deps": "failed"}),
            ("non-string worktree", {"worktree": 7}),
            ("running without an open ask", {"status": "running"}),
        ]
        for label, overrides in malformed:
            write_state(flow, **overrides)
            h.test_result(f"malformed evidence fails closed: {label}", read() is None)

        (flow / "state.json").write_text("{not json")
        h.test_result("non-JSON state.json fails closed", read() is None)

        (flow / "state.json").write_text("[]")
        h.test_result("state.json that is not an object fails closed", read() is None)

        (flow / "state.json").unlink()
        h.test_result("missing state.json fails closed", read() is None)

        write_state(flow)
        data = write_gate(flow, [{"label": "Retry"}, {"label": "Abort"}])
        ev = read()
        h.test_result(
            "open Flow stop gate with Retry: flow-stop, offers_retry, key from ask bytes",
            ev is not None
            and ev.kind == "flow-stop"
            and ev.header == "Flow stop"
            and ev.offers_retry is True
            and ev.stop_key == hashlib.sha256(data).hexdigest()[:16],
            f"got {ev}",
        )

        write_gate(flow, [{"label": "Abort"}])
        ev = read()
        h.test_result("open gate without Retry: offers_retry False", ev is not None and ev.offers_retry is False)

        (flow / "fleet-ask.json").write_text(json.dumps({
            "status": "open",
            "kind": "gate",
            "step": "implement",
            "gate": {"header": "Other", "options": [{"label": "Retry"}]},
        }))
        h.test_result("gate with a foreign header fails closed", read() is None)

        write_gate(flow, [{"label": "Retry"}], kind="question")
        h.test_result("open ask that is not a gate fails closed", read() is None)

        write_gate(flow, [{"label": "Retry"}], step="plan")
        h.test_result("gate for a different step fails closed", read() is None)

        write_gate(flow, [{"label": "Retry"}, "Abort"])
        h.test_result("gate option that is not an object fails closed", read() is None)

        (flow / "fleet-ask.json").write_text("not json")
        h.test_result("unparseable fleet-ask.json fails closed", read() is None)

        (flow / "fleet-ask.json").write_text("[1, 2]")
        h.test_result("fleet-ask.json that is not an object fails closed", read() is None)

        write_gate(flow, [{"label": "Retry"}], status="answered")
        ev = read()
        h.test_result(
            "answered ask is ignored: falls back to the paused state",
            ev is not None and ev.kind == "stopped" and ev.header is None and ev.offers_retry is False,
            f"got {ev}",
        )


# ────────────────────────────────────────────────────────────────────────────
# Section 10: nominate() quirks (disabled override, hard-coded cause)
# ────────────────────────────────────────────────────────────────────────────

def section_nominate_quirks(h: Harness) -> None:
    print()
    print("[Section 10] nominate() quirks: hard-coded cause and disabled override")

    evidence = make_evidence(stop_source="receipt", last_deps_result="failed", last_deps_step="implement")
    other = fake_tool(lambda *a: {}, name="other", cause="other-cause")

    tool, diag = heal.nominate(evidence, (other,), {})
    h.test_result(
        "a tool whose cause is not missing-deps is skipped",
        tool is None and diag == "No matching heal tool.",
        f"tool={tool}, diag={diag}",
    )

    tool, diag = heal.nominate(evidence, (other, heal.INSTALL_TOOL), {})
    h.test_result(
        "the missing-deps tool is nominated past a skipped tool",
        tool is not None and tool.name == "install" and diag is None,
        f"tool={tool}, diag={diag}",
    )

    tool, diag = heal.nominate(evidence, heal.TOOLS, {"install": {"enabled": False}})
    h.test_result(
        "enabled: false on install returns the disabled line",
        tool is None and diag == heal.OVERRIDE_DISABLED_LINE,
        f"tool={tool}, diag={diag}",
    )

    tool, diag = heal.nominate(evidence, (other, heal.INSTALL_TOOL), {"other": {"enabled": False}})
    h.test_result(
        "a disabled earlier tool wins over a later matching tool",
        tool is None and diag == heal.OVERRIDE_DISABLED_LINE,
        f"tool={tool}, diag={diag}",
    )


# ────────────────────────────────────────────────────────────────────────────
# Section 11: entry points (rejected worktrees, probe refusals, unknown tools)
# ────────────────────────────────────────────────────────────────────────────

def section_entrypoints(h: Harness) -> None:
    print()
    print("[Section 11] cli_probe / cli_run entry points: rejected worktrees and malformed input")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main, wt = make_repo(tmp_path)
        flow = flow_with(tmp_path, "flow", worktree=str(wt))

        h.test_result(
            "_validate_worktree accepts the linked worktree for its issue",
            heal._validate_worktree(wt, main, ISSUE, None) is None,
        )
        h.test_result(
            "_validate_worktree rejects the main worktree",
            heal._validate_worktree(main, main) == "Cannot heal the main worktree.",
        )
        h.test_result(
            "_validate_worktree rejects a branch naming another issue",
            heal._validate_worktree(wt, main, ISSUE + 1, None) == "Worktree branch does not name the issue.",
        )
        h.test_result(
            "_validate_worktree rejects a partial number match (4 is not 42)",
            heal._validate_worktree(wt, main, 4, None) == "Worktree branch does not name the issue.",
        )
        h.test_result(
            "_validate_worktree rejects a path git does not list",
            heal._validate_worktree(tmp_path / "nowhere", main) == "Worktree not in git worktree list.",
        )
        h.test_result(
            "_validate_worktree rejects a state.json worktree that differs",
            heal._validate_worktree(wt, main, ISSUE, str(main)) == "Worktree does not match the flow state.",
        )

        probe_main = heal.cli_probe(main, main, "main", flow, ISSUE)
        h.test_result(
            "cli_probe on the main worktree refuses with a line and a stop key",
            probe_main.get("line") == "Cannot heal the main worktree." and is_key(probe_main.get("stop_key")),
            f"got {probe_main}",
        )

        probe_nobase = heal.cli_probe(wt, main, None, flow, ISSUE)
        h.test_result(
            "cli_probe without a base ref refuses",
            probe_nobase.get("line") == "No base ref; cannot heal.",
            f"got {probe_nobase}",
        )

        with tools_swapped(fake_tool(lambda *a: {}, precondition=lambda w, m, b: True)):
            probe_ok = heal.cli_probe(wt, main, "main", flow, ISSUE)
        h.test_result(
            "cli_probe nominates install with the default timeout",
            probe_ok.get("tool") == "install"
            and probe_ok.get("timeout_secs") == heal.deps.DEPS_TIMEOUT_SECS
            and is_key(probe_ok.get("stop_key")),
            f"got {probe_ok}",
        )

        with tools_swapped(fake_tool(lambda *a: {}, precondition=lambda w, m, b: False)):
            probe_pre = heal.cli_probe(wt, main, "main", flow, ISSUE)
        h.test_result(
            "cli_probe reports a failed precondition as a line",
            probe_pre.get("line") == "Precondition failed for install.",
            f"got {probe_pre}",
        )

        (main / ".claude").mkdir()
        (main / ".claude" / "project.yaml").write_text(
            "fleet:\n  heal:\n    tools:\n      install:\n        enabled: false\n"
        )
        probe_disabled = heal.cli_probe(wt, main, "main", flow, ISSUE)
        h.test_result(
            "cli_probe honours enabled: false from the main worktree override",
            probe_disabled.get("line") == heal.OVERRIDE_DISABLED_LINE,
            f"got {probe_disabled}",
        )
        (main / ".claude" / "project.yaml").unlink()

        broken = tmp_path / "broken"
        write_state(broken, step="deploy")
        probe_broken = heal.cli_probe(wt, main, "main", broken, ISSUE)
        h.test_result(
            "cli_probe on malformed evidence returns only a null stop key",
            probe_broken == {"stop_key": None},
            f"got {probe_broken}",
        )

        run_nobase = heal.cli_run(wt, main, None, flow, ISSUE, "install", None)
        h.test_result(
            "cli_run without a base ref is refused as precondition-refused",
            run_nobase["status"] == "refused"
            and run_nobase["reason"] == "precondition-refused"
            and run_nobase["recommendation"] == "take-over"
            and run_nobase["changed"] is False
            and is_key(run_nobase["stop_key"]),
            f"got {run_nobase}",
        )

        # The real install tool is safe here: validation rejects the main worktree before any deps call.
        run_main = heal.cli_run(main, main, "main", flow, ISSUE, "install", None)
        h.test_result(
            "cli_run on the main worktree is refused before anything runs",
            run_main["status"] == "refused" and run_main["reason"] == "precondition-refused",
            f"got {run_main}",
        )

        run_unknown = heal.cli_run(wt, main, "main", flow, ISSUE, "rm-everything", None)
        h.test_result(
            "cli_run with an unknown tool name fails with an empty tool and no key",
            run_unknown == {
                "tool": "", "status": "failed", "reason": "internal-error",
                "changed": False, "recommendation": "take-over", "stop_key": None,
            },
            f"got {run_unknown}",
        )

        run_malformed = heal.cli_run(wt, main, "main", broken, ISSUE, "install", None)
        h.test_result(
            "cli_run on malformed evidence fails as internal-error with no key",
            run_malformed["tool"] == "install"
            and run_malformed["status"] == "failed"
            and run_malformed["reason"] == "internal-error"
            and run_malformed["stop_key"] is None,
            f"got {run_malformed}",
        )

        original_read = heal._read_stop_evidence

        def raising_read(flow_dir: Path, issue: int) -> Optional[heal.StopEvidence]:
            raise RuntimeError("secret detail /private/path")

        heal._read_stop_evidence = raising_read  # type: ignore[assignment]
        try:
            probe_raise, probe_err = quietly(heal.cli_probe, wt, main, "main", flow, ISSUE)
            run_raise, run_err = quietly(heal.cli_run, wt, main, "main", flow, ISSUE, "install", None)
        finally:
            heal._read_stop_evidence = original_read  # type: ignore[assignment]
        h.test_result(
            "an unexpected probe error returns the fixed line and names the stage on stderr",
            probe_raise == {"line": "Heal probe failed.", "stop_key": None}
            and "stage 'probe'" in probe_err
            and "secret detail" not in probe_err,
            f"got {probe_raise!r} / {probe_err!r}",
        )
        h.test_result(
            "an unexpected run error returns the fixed result and names the stage on stderr",
            run_raise["status"] == "failed"
            and run_raise["reason"] == "internal-error"
            and "stage 'run'" in run_err
            and "secret detail" not in run_err,
            f"got {run_raise!r} / {run_err!r}",
        )


# ────────────────────────────────────────────────────────────────────────────
# Section 12: cli_run result normalisation (status, reason, changed, recommendation)
# ────────────────────────────────────────────────────────────────────────────

def section_run_normalisation(h: Harness) -> None:
    print()
    print("[Section 12] cli_run result normalisation")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main, wt = make_repo(tmp_path)

        def run_with(outcome: Any, verify: Optional[Callable[..., bool]] = None,
                     retry: bool = False) -> Dict[str, Any]:
            flow = flow_with(tmp_path, f"flow-{id(outcome)}-{retry}", retry=retry, worktree=str(wt))
            with tools_swapped(fake_tool(lambda *a: outcome, verify=verify)):
                return heal.cli_run(wt, main, "main", flow, ISSUE, "install", None)

        healed = {"status": "healed", "reason": "currency-installed", "changed": True}

        result = run_with(healed)
        h.test_result(
            "healed and changed without a Retry offer: take-over",
            result["status"] == "healed" and result["changed"] is True
            and result["recommendation"] == "take-over" and result["stop_key"] is not None,
            f"got {result}",
        )

        result = run_with(healed, retry=True)
        h.test_result(
            "healed and changed with a Retry offer: retry-recommended",
            result["recommendation"] == "retry-recommended",
            f"got {result}",
        )

        result = run_with({"status": "healed", "reason": "currency-installed", "changed": False}, retry=True)
        h.test_result(
            "healed but unchanged: take-over even with a Retry offer",
            result["status"] == "healed" and result["recommendation"] == "take-over",
            f"got {result}",
        )

        result = run_with(healed, verify=lambda w, m, b: False, retry=True)
        h.test_result(
            "failed post-check downgrades healed to failed/internal-error with changed False",
            result["status"] == "failed" and result["reason"] == "internal-error"
            and result["changed"] is False and result["recommendation"] == "take-over",
            f"got {result}",
        )

        result = run_with({"status": "bogus", "reason": "currency-installed", "changed": True})
        h.test_result(
            "a status outside the Status literal becomes failed/internal-error",
            result["status"] == "failed" and result["reason"] == "internal-error" and result["changed"] is False,
            f"got {result}",
        )

        result = run_with({"status": "healed", "reason": "made-up-token", "changed": True})
        h.test_result(
            "a reason outside the HealReason literal becomes failed/internal-error",
            result["status"] == "failed" and result["reason"] == "internal-error" and result["changed"] is False,
            f"got {result}",
        )

        result = run_with({"status": "failed", "reason": 5, "changed": True})
        h.test_result(
            "a non-string reason becomes internal-error",
            result["status"] == "failed" and result["reason"] == "internal-error",
            f"got {result}",
        )

        result = run_with({"status": "no-op", "reason": "currency-current", "changed": "yes"})
        h.test_result(
            "changed is only a literal True: a truthy string is not a change",
            result["status"] == "no-op" and result["changed"] is False,
            f"got {result}",
        )

        result = run_with(None)
        h.test_result(
            "a non-dict run result becomes failed/internal-error",
            result["status"] == "failed" and result["reason"] == "internal-error",
            f"got {result}",
        )

        def raising_run(*args: Any) -> Dict[str, Any]:
            raise RuntimeError("secret detail /private/path")

        flow = flow_with(tmp_path, "flow-raising", worktree=str(wt))
        with tools_swapped(fake_tool(raising_run)):
            result, err = quietly(heal.cli_run, wt, main, "main", flow, ISSUE, "install", None)
        h.test_result(
            "a raising tool becomes failed/internal-error, keeps the tool name, and names the stage",
            result["tool"] == "install" and result["status"] == "failed"
            and result["reason"] == "internal-error" and "stage 'run'" in err
            and "secret detail" not in err,
            f"got {result} / {err!r}",
        )

        h.test_result(
            "_as_heal_reason accepts a member and rejects everything else",
            heal._as_heal_reason("currency-current") == "currency-current"
            and heal._as_heal_reason("nope") is None
            and heal._as_heal_reason(None) is None,
        )
        h.test_result(
            "_checked_outcome passes a valid result through",
            heal._checked_outcome(healed) == ("healed", "currency-installed", True),
        )


# ────────────────────────────────────────────────────────────────────────────
# Section 13: tail-file mode and tail-write failures
# ────────────────────────────────────────────────────────────────────────────

def section_tail_file(h: Harness) -> None:
    print()
    print("[Section 13] tail file: mode, symlink refusal, exclusive create, write failures")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main, wt = make_repo(tmp_path)
        tail_lines = ["installing dependencies", "done"]
        calls: List[int] = []

        def run_tail(*args: Any) -> Dict[str, Any]:
            calls.append(1)
            return {"status": "healed", "reason": "currency-installed", "changed": True, "tail": tail_lines}

        def cli_run_with_tail(flow: Path, tail: Path) -> Dict[str, Any]:
            with tools_swapped(fake_tool(run_tail)):
                return heal.cli_run(wt, main, "main", flow, ISSUE, "install", tail)

        flow = flow_with(tmp_path, "flow", worktree=str(wt))
        tail_path = tmp_path / "tail.log"
        result = cli_run_with_tail(flow, tail_path)
        expected = "\n".join(heal.deps.redact_output("\n".join(tail_lines))) + "\n"
        h.test_result(
            "tail file is created mode 0600 with the redacted tail",
            result["status"] == "healed"
            and tail_path.exists()
            and (tail_path.stat().st_mode & 0o777) == 0o600
            and tail_path.read_text() == expected,
            f"got {result}, content={tail_path.read_text() if tail_path.exists() else None!r}",
        )

        target = tmp_path / "target.txt"
        target.write_text("KEEP")
        link = tmp_path / "link.log"
        os.symlink(str(target), str(link))
        calls.clear()
        result = cli_run_with_tail(flow, link)
        h.test_result(
            "a symlink at the tail path is refused, the tool does not run, and the target is untouched",
            result["status"] == "failed" and result["reason"] == "internal-error"
            and not calls and target.read_text() == "KEEP",
            f"got {result}, calls={len(calls)}",
        )

        existing = tmp_path / "existing.log"
        existing.write_text("PRIOR")
        calls.clear()
        result = cli_run_with_tail(flow, existing)
        h.test_result(
            "a pre-existing tail file is refused by the exclusive create",
            result["status"] == "failed" and not calls and existing.read_text() == "PRIOR",
            f"got {result}, calls={len(calls)}",
        )

        failing_tail = tmp_path / "failing.log"

        def raising_write(fd: int, data: Any) -> int:
            raise OSError("disk full")

        calls.clear()
        with os_write_replaced(raising_write):
            result, err = quietly(cli_run_with_tail, flow, failing_tail)
        h.test_result(
            "a completed install stays healed when the tail write fails, and the failure is noted",
            result["status"] == "healed" and result["reason"] == "currency-installed"
            and result["changed"] is True and result["recommendation"] == "take-over"
            and "stage 'tail write'" in err,
            f"got {result} / {err!r}",
        )

        partial_tail = tmp_path / "partial.log"
        real_write = os.write

        def one_byte_write(fd: int, data: Any) -> int:
            return real_write(fd, bytes(data[:1]))

        with os_write_replaced(one_byte_write):
            result = cli_run_with_tail(flow, partial_tail)
        h.test_result(
            "short writes are looped until the whole tail is on disk",
            result["status"] == "healed" and partial_tail.read_text() == expected,
            f"got {result}, content={partial_tail.read_text()!r}",
        )


if __name__ == "__main__":
    main()
