#!/usr/bin/env python3
"""
Tests for scripts/flow-stop-baseline.py, the read-only Layer 0 stop-attribution baseline.

Every fixture is synthetic: flow dirs under a temp flows root, with state.json and a step receipt
invented here. Nothing reads the real ~/.claude/flows (every run passes --flows-root).

Covered behaviour (as the plan defines it):
- classify_stop_reason: "ledger unavailable" is orchestrator; an unknown reason and "plan/awaiting"
  are ambiguous.
- Counts over a mixed fixture, through main(argv), in both JSON and md output: flows started since
  --since, flows that reached merge or done, and stops per class.
- A flow started before --since is excluded from the started and reached counts.
- Edge cases: empty root, an undated flow, an unreadable state.json.
- Read-only: the fixture tree is byte-identical before and after each run.
- Aggregate-only: no fixture issue number, slug, title, repo name, PR URL or path appears in output.

The plan does not pin the JSON key names, so JSON counts are found by key-name token (for example any
key containing "started"). Assertions are on the counts, not on the shape. A count is accepted under
any key whose normalised name contains one of the tokens.

Run with: python3 tests/test_flow_stop_baseline.py
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


BASELINE_PATH = REPO_ROOT / "scripts" / "flow-stop-baseline.py"
REPO = "zephyr-repo"
SINCE = "2026-11-01"
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
COUNT_KEYS = {"count", "n", "total", "value"}

_MODULE = None


def baseline_module():
    """Load scripts/flow-stop-baseline.py by path (its filename has a hyphen)."""
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location("flow_stop_baseline_under_test", str(BASELINE_PATH))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {BASELINE_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _MODULE = module
    return _MODULE


def run_main(argv):
    """Call main(argv) with stdout captured. Returns (exit code, stdout text)."""
    module = baseline_module()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            ret = module.main(argv)
        except SystemExit as exc:
            ret = exc.code
    if ret is None:
        ret = 0
    if not isinstance(ret, int):
        ret = 1
    return ret, buffer.getvalue()


def _norm_key(text):
    """Lowercase letters only: 'step-agent', 'step_agent' and 'Step Agent' all become 'stepagent'."""
    return re.sub(r"[^a-z]", "", str(text).lower())


def _label(result):
    """Normalised label of a classify_stop_reason return value (str or Enum)."""
    return _norm_key(getattr(result, "value", result))


def json_ints(node, tokens):
    """Every integer found under a key whose normalised name contains one of tokens.

    Also accepts a count held as a sibling of a string label (for example {"class": "orchestrator",
    "count": 1}) when the key is one of COUNT_KEYS. Booleans are not counted.
    """
    wanted = [_norm_key(t) for t in tokens]
    found = []

    def labelled(mapping):
        return any(
            isinstance(v, str) and any(w in _norm_key(v) for w in wanted)
            for v in mapping.values()
        )

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                key_norm = _norm_key(key)
                if isinstance(child, int) and not isinstance(child, bool):
                    if any(w in key_norm for w in wanted):
                        found.append(child)
                    elif key_norm in COUNT_KEYS and labelled(value):
                        found.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def md_has_count(md, tokens, number):
    """True when some md line naming one of tokens also carries number (dates are ignored)."""
    wanted = [_norm_key(t) for t in tokens]
    for line in md.splitlines():
        if not any(w in _norm_key(line) for w in wanted):
            continue
        numbers = [int(x) for x in re.findall(r"\d+", DATE_RE.sub(" ", line))]
        if number in numbers:
            return True
    return False


def count_present(fmt, out, tokens, number):
    """Whether the output (json or md) reports number under a key or line named by tokens."""
    if fmt == "json":
        return number in json_ints(json.loads(out), tokens)
    return md_has_count(out, tokens, number)


def dated(status, step, started, stop_reason, issue, title):
    """A v1-shaped state dict with a start date (started and updated both set, so either is read)."""
    return {
        "issue": issue,
        "title": title,
        "status": status,
        "step": step,
        "started": started,
        "updated": started,
        "stop_reason": stop_reason,
        "pr": "https://example.invalid/pull/%d" % issue,
        "worktree": "/nonexistent-zephyr-worktree/%d" % issue,
    }


# Mixed fixture, with --since 2026-11-01: 5 flows started (the pre-since flow is excluded).
# Reached merge or done: 1 (9101). Orchestrator stops: 1 (ledger unavailable).
# Ambiguous stops: 2 (plan/awaiting and an unrecognised reason). The running flow has no stop.
MIXED_FLOWS = [
    ("9101-alpha-aa11", dated("done", "merge", "2026-11-03T09:00:00Z", {}, 9101, "Quux widget rewrite")),
    ("9102-beta-bb22", dated("failed", "implement", "2026-11-04T09:00:00Z",
                             {"implement": "ledger unavailable"}, 9102, "Quux widget two")),
    ("9103-gamma-cc33", dated("failed", "plan", "2026-11-05T09:00:00Z",
                              {"plan": "plan/awaiting"}, 9103, "Quux widget three")),
    ("9104-delta-dd44", dated("stopped", "verify", "2026-11-06T09:00:00Z",
                              {"verify": "qqq-unrecognised-7"}, 9104, "Quux widget four")),
    ("9105-epsilon-ee55", dated("running", "review", "2026-11-07T09:00:00Z", {}, 9105, "Quux widget five")),
]
PRE_SINCE_FLOW = ("9100-old-ff66", dated("done", "merge", "2026-10-01T09:00:00Z", {}, 9100, "Quux widget ancient"))
UNDATED_FLOW = ("9106-undated-gg77", {"issue": 9106, "title": "Quux undated", "status": "done",
                                      "step": "merge", "stop_reason": {}})
UNREADABLE_FLOW = ("9107-broken-hh88", "{not json")
FORBIDDEN_IN_OUTPUT = [
    "9100", "9101", "9102", "9103", "9104", "9105", "9106", "9107",
    "alpha", "beta", "gamma", "delta", "epsilon", "aa11", "bb22", "cc33", "dd44", "ee55", "ff66",
    "Quux", "zephyr", "example.invalid", "undated-gg77", "broken-hh88",
]


def write_flow(root, dirname, state, repo=REPO):
    """Create <root>/<repo>/<dirname>/ with state.json (dict, or a raw string) and one receipt."""
    flow = root / repo / dirname
    (flow / "steps").mkdir(parents=True)
    body = state if isinstance(state, str) else json.dumps(state)
    (flow / "state.json").write_text(body, encoding="utf-8")
    (flow / "steps" / "01-plan.md").write_text("Decision: OK\n<!-- step-end -->\n", encoding="utf-8")
    return flow


def build_root(td, flows):
    """Create <td>/flows with the given (dirname, state) flows and return the root path."""
    root = Path(td) / "flows"
    root.mkdir(parents=True, exist_ok=True)
    for dirname, state in flows:
        write_flow(root, dirname, state)
    return root


def snapshot(root):
    """Map every path under root to (kind, size, mtime_ns, sha256). Read-only check input."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            full = Path(dirpath) / name
            entries[str(full.relative_to(root))] = ("dir",)
        for name in filenames:
            full = Path(dirpath) / name
            stat = full.stat()
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
            entries[str(full.relative_to(root))] = ("file", stat.st_size, stat.st_mtime_ns, digest)
    return entries


def _run(h, label, fn):
    """Run one test function, recording a crash as a failure instead of aborting the suite."""
    try:
        passed, msg = fn()
    except Exception as exc:
        passed, msg = False, f"{type(exc).__name__}: {exc}"
    h.test_result(label, passed, msg)


# ── Section 1: classify_stop_reason (pure) ───────────────────────────────────

def test_classify_ledger_unavailable_is_orchestrator():
    """The fidelity audit's 'ledger unavailable' stop is an orchestrator stop."""
    label = _label(baseline_module().classify_stop_reason("ledger unavailable"))
    if "orchestrator" not in label or "ambiguous" in label:
        return False, f"classified as {label!r}, expected orchestrator"
    return True, ""


def test_classify_plan_awaiting_is_ambiguous():
    """'plan/awaiting' is named by the plan as an ambiguous row for the human to label."""
    label = _label(baseline_module().classify_stop_reason("plan/awaiting"))
    if "ambiguous" not in label:
        return False, f"classified as {label!r}, expected ambiguous"
    return True, ""


def test_classify_unknown_reason_is_ambiguous():
    """A stop_reason no rule matches is ambiguous, never silently dropped into another class."""
    label = _label(baseline_module().classify_stop_reason("qqq-never-seen-before-42"))
    if "ambiguous" not in label:
        return False, f"unknown reason classified as {label!r}, expected ambiguous"
    return True, ""


# ── Section 2: counts through main(argv) ─────────────────────────────────────

def test_mixed_fixture_counts_json():
    """JSON: 5 started since 2026-11-01, 1 reached merge or done, 1 orchestrator, 2 ambiguous."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc} on a valid synthetic root"
    obj = json.loads(out)
    checks = [
        ("started", ["started", "window"], 5),
        ("reached merge or done", ["reached", "merge", "done"], 1),
        ("orchestrator stops", ["orchestrator"], 1),
        ("ambiguous stops", ["ambiguous"], 2),
    ]
    for label, tokens, expected in checks:
        values = json_ints(obj, tokens)
        if expected not in values:
            return False, f"{label}: expected {expected} under a key naming {tokens}, found {values}"
    return True, ""


def test_mixed_fixture_counts_md():
    """md: the same counts appear on lines naming each quantity."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "md"])
    if rc != 0:
        return False, f"exit {rc} on a valid synthetic root"
    checks = [
        ("started", ["started", "window"], 5),
        ("reached merge or done", ["reached", "merge", "done"], 1),
        ("orchestrator stops", ["orchestrator"], 1),
        ("ambiguous stops", ["ambiguous"], 2),
    ]
    for label, tokens, expected in checks:
        if not md_has_count(out, tokens, expected):
            return False, f"{label}: no md line naming {tokens} carries {expected}"
    return True, ""


def test_since_excludes_flow_started_before_date():
    """The pre-since flow (done, 2026-10-01) is in neither the started nor the reached count."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    obj = json.loads(out)
    started = json_ints(obj, ["started", "window"])
    reached = json_ints(obj, ["reached", "merge", "done"])
    if 5 not in started or 6 in started:
        return False, f"started values {started}, expected 5 and not 6 (pre-since flow included?)"
    if 1 not in reached or 2 in reached:
        return False, f"reached values {reached}, expected 1 and not 2 (pre-since done flow included?)"
    return True, ""

def test_since_includes_flow_started_on_the_date():
    """A flow started on the --since date itself counts as started (the boundary is inclusive)."""
    on_date = ("9108-edge-ii99", dated("done", "merge", SINCE + "T09:00:00Z", {}, 9108, "Quux widget edge"))
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [on_date])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    flows_in_window = json_ints(json.loads(out), ["flows_in_window"])
    if 1 not in flows_in_window:
        return False, f"flows_in_window {flows_in_window}, expected 1 (on-date flow excluded?)"
    return True, ""


def test_since_boundary_midnight_included():
    """A flow started at exactly midnight (00:00:00) of the --since date is included."""
    at_midnight = ("9109-midnight-jj00", dated("done", "merge", SINCE + "T00:00:00Z", {}, 9109, "Quux midnight"))
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [at_midnight])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    flows_in_window = json_ints(json.loads(out), ["flows_in_window"])
    if 1 not in flows_in_window:
        return False, f"flows_in_window {flows_in_window}, expected 1 (midnight flow on --since date excluded?)"
    return True, ""


def test_since_boundary_day_before_excluded():
    """A flow started on the day before the --since date is excluded."""
    day_before = ("9110-before-kk11", dated("done", "merge", "2026-10-31T23:59:59Z", {}, 9110, "Quux before"))
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [day_before])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    flows_in_window = json_ints(json.loads(out), ["flows_in_window"])
    if 0 not in flows_in_window:
        return False, f"flows_in_window {flows_in_window}, expected 0 (day-before flow included?)"
    return True, ""


# ── Section 3: edge cases ────────────────────────────────────────────────────

def test_empty_root_reports_zero_counts():
    """An empty flows root exits 0 and reports no started or reached flows, in both formats."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "flows"
        root.mkdir()
        rc_json, out_json = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
        rc_md, _ = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "md"])
    if rc_json != 0 or rc_md != 0:
        return False, f"exit codes json={rc_json} md={rc_md} on an empty root"
    obj = json.loads(out_json)
    nonzero = [v for v in json_ints(obj, ["started", "window", "reached", "merge", "done", "orchestrator", "ambiguous"]) if v]
    if nonzero:
        return False, f"empty root reported non-zero counts {nonzero}"
    return True, ""


def test_undated_flow_does_not_break_report():
    """A flow with no start date does not crash the run or change the dated orchestrator count."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [UNDATED_FLOW, MIXED_FLOWS[1]])
        rc, out = run_main(["--flows-root", str(root), "--format", "json"])
    if rc != 0:
        return False, f"exit {rc} with an undated flow present"
    obj = json.loads(out)
    if 1 not in json_ints(obj, ["orchestrator"]):
        return False, "dated orchestrator stop was not counted alongside the undated flow"
    if "undated-gg77" in out or "9106" in out:
        return False, "undated flow identifiers leaked into the output"
    return True, ""


def test_unreadable_state_json_is_survived():
    """A flow whose state.json is not JSON does not crash the run (json and md), and does not
    change the orchestrator count of the readable flows."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [UNREADABLE_FLOW, MIXED_FLOWS[1]])
        rc_json, out_json = run_main(["--flows-root", str(root), "--format", "json"])
        rc_md, _ = run_main(["--flows-root", str(root), "--format", "md"])
    if rc_json != 0 or rc_md != 0:
        return False, f"exit codes json={rc_json} md={rc_md} with an unreadable state.json"
    obj = json.loads(out_json)
    if 1 not in json_ints(obj, ["orchestrator"]):
        return False, "unreadable sibling changed the orchestrator count of readable flows"
    return True, ""


# ── Section 4: read-only and aggregate-only ──────────────────────────────────

def test_read_only_tree_unchanged():
    """Running the script (json with --since, md without) leaves the flows root byte-identical."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW, UNDATED_FLOW, UNREADABLE_FLOW])
        before = snapshot(root)
        run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
        run_main(["--flows-root", str(root), "--format", "md"])
        after = snapshot(root)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        return False, f"tree changed: added={added[:3]} removed={removed[:3]} changed={changed[:3]}"
    return True, ""


def test_output_is_aggregate_only():
    """Neither format echoes a fixture issue number, slug, title, repo, PR URL, or the root path."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW, UNDATED_FLOW, UNREADABLE_FLOW])
        _, out_json = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
        _, out_md = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "md"])
        root_str = str(root)
    for label, text in (("json", out_json), ("md", out_md)):
        leaked = [token for token in FORBIDDEN_IN_OUTPUT if token in text]
        if leaked:
            return False, f"{label} output contains fixture identifiers: {leaked}"
        if root_str in text:
            return False, f"{label} output contains the fixture root path"
    return True, ""


# ── Section 5: RULES table-driven tests ──────────────────────────────────────

def test_rules_table_all_entries_classified():
    """Every RULES entry is tested: each pattern matches and classifies correctly."""
    module = baseline_module()
    test_samples = {
        r"ledger unavailable": "ledger unavailable",
        r"^anti-tamper gate": "anti-tamper gate on flow 123",
        r"^gate-not-converged": "gate-not-converged after 10 attempts",
        r"^unit-record-mismatch": "unit-record-mismatch in unit 2",
        r"^primary-checkout-drift": "primary-checkout-drift detected",
        r"^check-not-configured": "check-not-configured error",
        r"^check source mismatch": "check source mismatch found",
        r"^check-not-run": "check-not-run due to timeout",
        r"^queue kickback": "queue kickback from merge",
        r"^skill invocation failed": "skill invocation failed: unknown",
        r"^check (gate )?failed": "check gate failed on verify",
        r"^(malformed receipt|no receipt written|receipt:|assess receipt)": "receipt: badly formed",
        r"^(implementer-shortfall|implement run incomplete)": "implementer-shortfall in unit 1",
        r"^reduced run": "reduced run by fidelity",
        r"^fidelity audit failed": "fidelity audit failed: other",
        r"^planned-change-breaks-test": "planned-change-breaks-test: tests/test_x.py",
        r"^test-only-plan": "test-only-plan: no Round 1 work",
        r"^existing-test-edit": "existing-test-edit: tests/test_x.py",
        r"^test-path-in-units": "test-path-in-units: unit 1",
        r"^plan-test-paths": "plan-test-paths: check could not run",
    }
    for pattern, stop_class, label in module.RULES:
        # Use pre-computed test samples for common patterns.
        test_reason = test_samples.get(pattern, "test reason containing " + label)
        classified_class, classified_label = module.classify_stop_reason(test_reason)
        if classified_class != stop_class or classified_label != label:
            return False, f"rule '{pattern}' with reason '{test_reason}' -> ({stop_class}, {label}) but got ({classified_class}, {classified_label})"
    return True, ""


def test_receipt_reason_fixture_success():
    """A flow with a receipt containing Decision: FAILED and Reason: yields the reason."""
    with tempfile.TemporaryDirectory() as td:
        flow = Path(td) / "flows" / "repo" / "test-flow"
        flow.mkdir(parents=True)
        (flow / "steps").mkdir()
        receipt_text = "Decision: FAILED\n\nReason: check failed\n\n<!-- step-end -->\n"
        (flow / "steps" / "03-implement.md").write_text(receipt_text)
        module = baseline_module()
        reason = module.receipt_reason(flow, "implement")
    if reason != "check failed":
        return False, f"receipt reason: expected 'check failed', got {reason!r}"
    return True, ""


def test_receipt_reason_no_receipt():
    """A flow with no receipt for the step yields None."""
    with tempfile.TemporaryDirectory() as td:
        flow = Path(td) / "flows" / "repo" / "test-flow"
        flow.mkdir(parents=True)
        (flow / "steps").mkdir()
        module = baseline_module()
        reason = module.receipt_reason(flow, "implement")
    if reason is not None:
        return False, f"receipt reason with no receipt: expected None, got {reason!r}"
    return True, ""


def test_receipt_reason_no_failed_decision():
    """A receipt without Decision: FAILED yields None (even if Reason: is present)."""
    with tempfile.TemporaryDirectory() as td:
        flow = Path(td) / "flows" / "repo" / "test-flow"
        flow.mkdir(parents=True)
        (flow / "steps").mkdir()
        receipt_text = "Decision: OK\n\nReason: check passed\n\n<!-- step-end -->\n"
        (flow / "steps" / "03-implement.md").write_text(receipt_text)
        module = baseline_module()
        reason = module.receipt_reason(flow, "implement")
    if reason is not None:
        return False, f"receipt reason without FAILED: expected None, got {reason!r}"
    return True, ""


def test_exit_code_valid_root():
    """--flows-root pointing to a valid directory exits 0."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "flows"
        root.mkdir()
        rc, _ = run_main(["--flows-root", str(root)])
    if rc != 0:
        return False, f"valid root exited {rc}, expected 0"
    return True, ""


def test_exit_code_invalid_since():
    """--since with invalid date exits 2."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "flows"
        root.mkdir()
        rc, _ = run_main(["--flows-root", str(root), "--since", "2026-13-45"])
    if rc != 2:
        return False, f"invalid --since exited {rc}, expected 2"
    return True, ""


def test_exit_code_missing_flows_root():
    """--flows-root pointing to a non-existent path exits 2."""
    rc, _ = run_main(["--flows-root", "/nonexistent/path/that/does/not/exist"])
    if rc != 2:
        return False, f"missing flows root exited {rc}, expected 2"
    return True, ""


def test_mixed_fixture_exact_counts_json():
    """JSON report has exactly the expected count keys with correct values."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    obj = json.loads(out)
    counts = obj.get("counts", {})
    expected = {"flows_in_window": 5, "reached_merge_or_done": 1, "outside_window": 1, "undated_excluded": 0, "unreadable_state": 0}
    for key, expected_val in expected.items():
        actual_val = counts.get(key)
        if actual_val != expected_val:
            return False, f"counts[{key}]: expected {expected_val}, got {actual_val}"
    extra_keys = set(counts.keys()) - set(expected.keys())
    if extra_keys:
        return False, f"counts has extra keys: {extra_keys}"
    return True, ""


def test_mixed_fixture_distinct_stop_counts():
    """Each distinct stop (class, label) pair has a count and no duplicates in stop_table."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS + [PRE_SINCE_FLOW])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    obj = json.loads(out)
    stop_table = obj.get("stop_table", [])
    seen = set()
    for row in stop_table:
        key = (row.get("class"), row.get("label"))
        if key in seen:
            return False, f"duplicate stop_table entry: {key}"
        seen.add(key)
    return True, ""


def test_healthy_awaiting_flow_is_not_a_stop():
    """A flow awaiting a human answer counts in the window but adds no stop."""
    awaiting = dated("awaiting", "plan", "2026-11-03T09:00:00Z", {}, 9301, "Quux awaiting")
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [("9301-await-a1", awaiting)])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    obj = json.loads(out)
    if obj["counts"]["flows_in_window"] != 1:
        return False, f"flows_in_window: expected 1, got {obj['counts']['flows_in_window']}"
    if obj["stops"]["total"] != 0 or obj["stop_table"] != []:
        return False, f"awaiting flow produced stops: {obj['stops']}"
    return True, ""


def test_unreadable_receipt_is_survived():
    """A flow whose receipt cannot be read is still counted and the run exits 0."""
    paused = dated("paused", "plan", "2026-11-03T09:00:00Z", {"plan": "user request"}, 9302, "Quux paused")
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [("9302-recv-b2", paused)])
        receipt = root / REPO / "9302-recv-b2" / "steps" / "01-plan.md"
        os.chmod(receipt, 0o000)
        try:
            rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
        finally:
            os.chmod(receipt, 0o644)
    if rc != 0:
        return False, f"exit {rc}"
    flows = json.loads(out)["counts"]["flows_in_window"]
    if flows != 1:
        return False, f"flows_in_window: expected 1, got {flows}"
    return True, ""


def test_flows_across_several_repos_are_all_counted():
    """Flow dirs under different repo directories of the flows root are all counted."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "flows"
        root.mkdir()
        for n, repo in enumerate(["repo-one", "repo-two", "repo-three"]):
            state = dated("done", "merge", "2026-11-03T09:00:00Z", {}, 9310 + n, "Quux multi")
            write_flow(root, "93%02d-multi-c%d" % (10 + n, n), state, repo=repo)
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    counts = json.loads(out)["counts"]
    if counts["flows_in_window"] != 3 or counts["reached_merge_or_done"] != 3:
        return False, f"expected 3 flows and 3 reached, got {counts}"
    return True, ""


def test_malformed_stop_reason_is_survived():
    """A stop_reason that is not a mapping does not crash the run; the flow is still counted."""
    paused = dated("paused", "plan", "2026-11-03T09:00:00Z", "not-a-dict", 9303, "Quux bad reason")
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, [("9303-badr-d4", paused)])
        rc, out = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "json"])
    if rc != 0:
        return False, f"exit {rc}"
    flows = json.loads(out)["counts"]["flows_in_window"]
    if flows != 1:
        return False, f"flows_in_window: expected 1, got {flows}"
    return True, ""


def test_default_format_is_markdown():
    """Without --format the output is the markdown report, not JSON."""
    with tempfile.TemporaryDirectory() as td:
        root = build_root(td, MIXED_FLOWS)
        rc_default, out_default = run_main(["--flows-root", str(root), "--since", SINCE])
        rc_md, out_md = run_main(["--flows-root", str(root), "--since", SINCE, "--format", "md"])
    if rc_default != 0 or rc_md != 0:
        return False, f"exit codes {rc_default}, {rc_md}"
    if out_default != out_md:
        return False, "default output differs from --format md output"
    try:
        json.loads(out_default)
    except ValueError:
        return True, ""
    return False, "default output parsed as JSON"


if __name__ == "__main__":
    h = Harness("FLOW STOP BASELINE TEST SUITE")

    print("[Section 1] classify_stop_reason")
    _run(h, "'ledger unavailable' is orchestrator", test_classify_ledger_unavailable_is_orchestrator)
    _run(h, "'plan/awaiting' is ambiguous", test_classify_plan_awaiting_is_ambiguous)
    _run(h, "an unknown stop_reason is ambiguous", test_classify_unknown_reason_is_ambiguous)
    print()

    print("[Section 2] counts through main(argv)")
    _run(h, "mixed fixture counts (json)", test_mixed_fixture_counts_json)
    _run(h, "mixed fixture counts (md)", test_mixed_fixture_counts_md)
    _run(h, "a flow started before --since is excluded", test_since_excludes_flow_started_before_date)
    _run(h, "a flow started on the --since date is included", test_since_includes_flow_started_on_the_date)
    _run(h, "a flow started at midnight of --since date is included", test_since_boundary_midnight_included)
    _run(h, "a flow started the day before --since is excluded", test_since_boundary_day_before_excluded)
    print()

    print("[Section 3] edge cases")
    _run(h, "empty root reports zero counts", test_empty_root_reports_zero_counts)
    _run(h, "undated flow does not break the report", test_undated_flow_does_not_break_report)
    _run(h, "unreadable state.json is survived", test_unreadable_state_json_is_survived)
    print()

    print("[Section 4] read-only and aggregate-only")
    _run(h, "flows root is byte-identical before and after", test_read_only_tree_unchanged)
    _run(h, "output carries aggregate counts only", test_output_is_aggregate_only)
    print()

    print("[Section 5] RULES table-driven tests")
    _run(h, "all RULES entries classify correctly", test_rules_table_all_entries_classified)
    _run(h, "receipt_reason with FAILED decision yields reason", test_receipt_reason_fixture_success)
    _run(h, "receipt_reason with no receipt yields None", test_receipt_reason_no_receipt)
    _run(h, "receipt_reason without FAILED decision yields None", test_receipt_reason_no_failed_decision)
    _run(h, "exit code 0 for valid root", test_exit_code_valid_root)
    _run(h, "exit code 2 for invalid --since", test_exit_code_invalid_since)
    _run(h, "exit code 2 for missing flows root", test_exit_code_missing_flows_root)
    _run(h, "exact count keys in JSON report", test_mixed_fixture_exact_counts_json)
    _run(h, "distinct stop counts (no duplicates)", test_mixed_fixture_distinct_stop_counts)
    _run(h, "a healthy awaiting flow adds no stop", test_healthy_awaiting_flow_is_not_a_stop)
    _run(h, "an unreadable receipt is survived", test_unreadable_receipt_is_survived)
    _run(h, "flows across several repos are all counted", test_flows_across_several_repos_are_all_counted)
    _run(h, "a malformed stop_reason is survived", test_malformed_stop_reason_is_survived)
    _run(h, "the default output format is markdown", test_default_format_is_markdown)
    print()

    h.summarize_and_exit()
