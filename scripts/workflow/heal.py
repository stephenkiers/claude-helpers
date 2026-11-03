"""
Bounded self-heal harness for expert-fleet (#425, Step 1).

This module provides the tool registry, nomination logic, and the probe/run
interface for the fleet's automatic heal capability.

A tool is a precondition + fixed action + post-check. Each ticket stop
produces at most one tool run.

Invoked by scripts/workflow/cli.py heal subcommand.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, TypedDict, cast, get_args

from . import deps

# Type aliases
Permission = Literal["runs-repo-code", "writes-gitignored", "no-write"]
Approval = Literal["run-first", "wait-for-approval"]
Gate = Literal["harness", "ensure-deps-exception"]
Status = Literal["no-op", "healed", "refused", "failed"]
# Closed kinds of StopEvidence (see _read_stop_evidence).
EvidenceKind = Literal["flow-stop", "failed", "stopped"]
# Closed classes produced by _classify_reason from a stop_reason string.
ReasonClass = Literal["permission-refusal", "check-not-configured", "reduced-run"]

# Reason enum tokens (closed set)
HealReason = Literal[
    "currency-current",
    "currency-installed",
    "currency-manifest-changed",
    "currency-ambiguous",
    "currency-skipped",
    "currency-failed",
    "no-evidence",
    "failures-exhausted",
    "unknown-stop",
    "manifest-changed",
    "permission-refusal",
    "unchanged-inputs-gate",
    "awaiting-approval",
    "precondition-refused",
    "internal-error",
]

# Recommendation enum tokens
Recommendation = Literal["retry-recommended", "take-over"]

# Closed sets taken from the documented state.json schema (commands/expert-flow.md, State section).
KNOWN_STEPS: Tuple[str, ...] = (
    "worktree", "plan", "track", "implement", "review", "fix", "verify", "assess", "ship", "merge",
)
_STATUSES: Tuple[str, ...] = ("running", "awaiting", "paused", "failed", "abandoned", "done")
_STOP_SOURCES: Tuple[str, ...] = ("receipt", "validation", "audit", "tree")
_DEPS_RESULTS: Tuple[str, ...] = (
    "installed", "current", "skipped", "ambiguous", "manifest-changed", "failed",
)
_DEPS_STEPS: Tuple[str, ...] = ("worktree", "implement", "fix", "verify")
# Membership sets for validating values that cross a boundary (tool results, reasons) before any cast.
_STATUS_VALUES: Tuple[str, ...] = get_args(Status)
_HEAL_REASONS: Tuple[str, ...] = get_args(HealReason)

# The Flow stop gate is the only ask that can nominate a heal.
FLOW_STOP_HEADER = "Flow stop"

# Permission-classifier refusals surface in stop_reason with Claude Code's own wording ("Permission ...
# has been denied"). No sample of that text exists in this repo, so the marker is pinned here as the
# pair of case-insensitive substrings below; a stop whose reason carries both is never healed.
PERMISSION_MARKERS: Tuple[str, str] = ("permission", "denied")

# Per-parameter bounds for the project override: (minimum, maximum), both inclusive.
PARAM_BOUNDS: Dict[str, Tuple[int, int]] = {"timeout_secs": (1, deps.DEPS_TIMEOUT_SECS)}

OVERRIDE_INVALID_LINE = "Project heal override is invalid; no tool will run."
OVERRIDE_DISABLED_LINE = "Heal tool is disabled by the project override."


class _RunOutcomeRequired(TypedDict):
    status: str  # Validated against Status by cli_run before use
    reason: str  # Validated against HealReason by cli_run before use
    changed: bool


class RunOutcome(_RunOutcomeRequired, total=False):
    """What a Tool.run returns. The optional tail is redacted and written to the tail file by cli_run."""
    tail: List[str]


@dataclass(frozen=True)
class Tool:
    """A tool that the harness can run on a stopped ticket."""
    name: str
    cause: str  # For install, "missing-deps"
    permission: Permission
    approval: Approval
    gate: Gate
    inputs: Tuple[str, ...]  # Repo paths compared against merge-base
    params: Tuple[str, ...]  # Declared parameter names
    precondition: Callable[[Path, Path, Optional[str]], bool]  # (worktree, main, base) -> can-run
    run: Callable[[Path, Path, Optional[str], int, Optional[Path]], RunOutcome]  # (worktree, main, base, timeout, tail_file)
    verify: Callable[[Path, Path, Optional[str]], bool]  # (worktree, main, base) -> changed


@dataclass(frozen=True)
class StopEvidence:
    """Parsed flow data that nominates a tool or a diagnosis."""
    kind: EvidenceKind  # "flow-stop", "failed", "stopped"
    header: Optional[str]  # Gate header name, if any
    step: Optional[str]  # The step that failed
    failures: Dict[str, int]  # failures[step] counters
    stop_source: Optional[str]  # Where the stop came from
    stop_reason_class: Optional[ReasonClass]  # closed enum: permission-refusal, check-not-configured, reduced-run
    last_deps_result: Optional[str]  # Result of the newest deps entry, or None
    offers_retry: bool  # Does the open gate's own recorded options include Retry?
    stop_key: Optional[str]  # Identifies this stop (see stop_key_for)
    last_deps_step: Optional[str] = None  # Step of the newest deps entry
    worktree: Optional[str] = None  # state.json worktree, validated against the argument


@dataclass(frozen=True)
class HealResult:
    """Outcome of a heal run."""
    tool: str
    status: Status
    reason: HealReason
    changed: bool
    recommendation: Recommendation
    stop_key: Optional[str]


def stop_key_for(ask_bytes: Optional[bytes], step: str, status: str, reason: str) -> str:
    """
    The one definition of a stop's identity.

    An open ask is identified by the sha256 of its fleet-ask.json bytes; a stop with no open ask by the
    sha256 of "step", "status" and "stop_reason[step]" (empty when absent) joined by newlines. Both are
    truncated to 16 hex digits. scripts/fleet.py cannot import this module, so it carries a mirror of
    the same computation (fleet.current_stop_key) that must stay identical.
    """
    if ask_bytes is not None:
        return hashlib.sha256(ask_bytes).hexdigest()[:16]
    return hashlib.sha256(f"{step}\n{status}\n{reason}".encode("utf-8")).hexdigest()[:16]


# ────────────────────────────────────────────────────────────────────────────
# The install tool
# ────────────────────────────────────────────────────────────────────────────

def _note_failure(stage: str) -> None:
    """
    A fixed stderr line for a stage that raised. The exception text is never printed, since it can
    carry paths or command output. The caller still returns its own fixed result, so nothing propagates.
    """
    print(f"heal: stage '{stage}' failed unexpectedly; returning a fixed result.", file=sys.stderr)


def _install_precondition(worktree: Path, main_worktree: Path, base: Optional[str]) -> bool:
    """Install runs only if the read-only currency check says an install would run."""
    if base is None:
        return False
    try:
        return deps.deps_currency(worktree, main_worktree, base) == "stale"
    except Exception:
        _note_failure("install precondition")
        return False


def _install_run(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    timeout: int,
    tail_file: Optional[Path],  # reserved: the tail travels in the RunOutcome and cli_run writes it
) -> RunOutcome:
    """Run ensure_deps with ignore_approvals=True. The redacted tail comes back under "tail"."""
    if base is None:
        return {"status": "refused", "reason": "precondition-refused", "changed": False}
    try:
        result = deps.ensure_deps(
            worktree,
            main_worktree,
            base=base,
            approve_manifest=False,
            timeout=timeout,
            ignore_approvals=True,
        )
        tail = list(result.tail)
        if result.status == "installed":
            return {"status": "healed", "reason": "currency-installed", "changed": True, "tail": tail}
        if result.status == "current":
            return {"status": "no-op", "reason": "currency-current", "changed": False, "tail": tail}
        if result.status == "manifest-changed":
            return {"status": "refused", "reason": "currency-manifest-changed", "changed": False, "tail": tail}
        if result.status == "ambiguous":
            return {"status": "refused", "reason": "currency-ambiguous", "changed": False, "tail": tail}
        if result.status == "skipped":
            return {"status": "refused", "reason": "currency-skipped", "changed": False, "tail": tail}
        if result.status == "failed":
            return {"status": "failed", "reason": "currency-failed", "changed": False, "tail": tail}
        return {"status": "failed", "reason": "internal-error", "changed": False, "tail": tail}
    except Exception:
        _note_failure("install run")
        return {"status": "failed", "reason": "internal-error", "changed": False}


def _install_verify(worktree: Path, main_worktree: Path, base: Optional[str]) -> bool:
    """Post-check: the read-only currency check reports 'current' after a heal."""
    if base is None:
        return False
    try:
        return deps.deps_currency(worktree, main_worktree, base) == "current"
    except Exception:
        _note_failure("install verify")
        return False


# Install tool entry
INSTALL_TOOL = Tool(
    name="install",
    cause="missing-deps",
    permission="runs-repo-code",
    approval="run-first",
    gate="ensure-deps-exception",
    inputs=(),  # No inputs to check (exception to Q4)
    params=("timeout_secs",),
    precondition=_install_precondition,
    run=_install_run,
    verify=_install_verify,
)

# Tools registry
TOOLS: Tuple[Tool, ...] = (INSTALL_TOOL,)

# Diagnosis messages for causes 2-6 (never nominate a tool)
DIAGNOSES: Dict[str, str] = {
    "missing-check-config": "Check config not found in main worktree; cannot validate.",
    "check-source-flip": "Check source changed; re-run needed.",
    "missing-test-db": "Per-worktree test database missing (local-only target).",
    "unregistered-agent": "Unregistered agent type; fresh session needed.",
    "malformed-receipt": "Malformed receipt or reduced run detected.",
}

# Structured stop_reason_class values (from the reader) that map to a diagnosis.
_CLASS_DIAGNOSES: Dict[str, str] = {
    "check-not-configured": DIAGNOSES["missing-check-config"],
    "reduced-run": DIAGNOSES["malformed-receipt"],
}


# ────────────────────────────────────────────────────────────────────────────
# Core harness logic (pure functions)
# ────────────────────────────────────────────────────────────────────────────

def nominate(
    evidence: Optional[StopEvidence],
    tools: Tuple[Tool, ...],
    override: Dict[str, Any],
) -> Tuple[Optional[Tool], Optional[str]]:
    """
    Nominate a tool or a diagnosis line. Pure, no I/O.

    Returns (tool, None) for a nomination, (None, line) for a refusal or diagnosis, and (None, None)
    when there is no evidence to act on.

    Nomination needs valid evidence and either a known stop_source or a failed Deps-precondition
    entry for the stopped step. Whether an install is actually due is the probe's decision
    (deps_currency), never this function's.

    Two quirks, both pinned by tests. The first tool the loop reaches with `enabled: false` in the
    override returns OVERRIDE_DISABLED_LINE, so a disabled tool's line wins over any later tool.
    The match is hard-coded to cause "missing-deps": a tool with any other cause is skipped, and
    with no match the result is "No matching heal tool." until a branch for that cause exists.
    """
    if evidence is None:
        return None, None

    if evidence.kind not in ("flow-stop", "failed", "stopped"):
        return None, "Unknown stop type."
    if evidence.step not in KNOWN_STEPS:
        return None, "Unknown stop type."
    if evidence.header is not None and evidence.header != FLOW_STOP_HEADER:
        return None, "Unknown stop type."

    failed_precondition = (
        evidence.last_deps_result == "failed" and evidence.last_deps_step == evidence.step
    )
    if evidence.stop_source is None and not failed_precondition:
        return None, None

    # W1: heal only while failures[STEP] < 2. An absent counter is 0.
    if evidence.failures.get(str(evidence.step), 0) >= 2:
        return None, "Failure limit reached; heal skipped."

    if evidence.stop_reason_class == "permission-refusal":
        return None, "Permission refusal; cannot heal."

    if evidence.last_deps_result == "manifest-changed":
        return None, "Manifest changed; needs approval."

    if evidence.stop_reason_class in _CLASS_DIAGNOSES:
        return None, _CLASS_DIAGNOSES[str(evidence.stop_reason_class)]

    for tool in tools:
        tool_config = override.get(tool.name)
        if isinstance(tool_config, dict) and tool_config.get("enabled") is False:
            return None, OVERRIDE_DISABLED_LINE
        if tool.cause == "missing-deps":
            return tool, None

    return None, "No matching heal tool."


def probe(
    tool: Tool,
    context: Tuple[Path, Path, Optional[str]],
    issue: Optional[int] = None,
    state_worktree: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Read-only probe of whether a tool can run. No cap spent.

    Returns (True, None) if the precondition passes, or (False, refusal_line) otherwise.
    """
    worktree, main_worktree, base = context

    validation_error = _validate_worktree(worktree, main_worktree, issue, state_worktree)
    if validation_error:
        return False, validation_error

    if tool.gate == "harness" and tool.inputs:
        if not _check_unchanged_inputs(worktree, main_worktree, base, tool.inputs):
            return False, "Tool inputs have changed."

    if tool.approval == "wait-for-approval":
        return False, f"Tool {tool.name} awaiting approval."

    if not tool.precondition(worktree, main_worktree, base):
        return False, f"Precondition failed for {tool.name}."

    return True, None


def after_heal(result: HealResult, evidence: StopEvidence) -> Recommendation:
    """
    Pure function: choose retry or take-over based on result and evidence.

    retry-recommended only when the result is healed (post-check passed), the tool changed something,
    the gate's own options offer Retry, and the newest deps result is not manifest-changed.
    Every other case, including refused, failed and no-op, is take-over.
    """
    if (
        result.status == "healed"
        and result.changed
        and evidence.offers_retry
        and evidence.last_deps_result != "manifest-changed"
    ):
        return "retry-recommended"
    return "take-over"


# ────────────────────────────────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────────────────────────────────

def _git_wt(args: List[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """git with the fsmonitor hook disabled, since a ticket worktree's config is ticket-controlled."""
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false"] + args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _validate_worktree(
    worktree: Path,
    main_worktree: Path,
    issue: Optional[int] = None,
    state_worktree: Optional[str] = None,
) -> Optional[str]:
    """
    Validate that the worktree is safe to modify. Returns a fixed error line or None if valid.

    Checks: it is listed by `git worktree list --porcelain` of the main repo; its realpath matches the
    listed path and, when given, state.json's worktree; it is not the main worktree; and, when an
    issue is given, its branch names the issue.
    """
    try:
        result = _git_wt(["worktree", "list", "--porcelain"], main_worktree)
        if result.returncode != 0:
            return "Could not list worktrees."

        worktree_real = os.path.realpath(str(worktree))
        main_real = os.path.realpath(str(main_worktree))
        if worktree_real == main_real:
            return "Cannot heal the main worktree."

        if state_worktree is not None and os.path.realpath(state_worktree) != worktree_real:
            return "Worktree does not match the flow state."

        records: List[Dict[str, str]] = []
        current: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    records.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            if key in ("worktree", "branch"):
                current[key] = value
        if current:
            records.append(current)

        match: Optional[Dict[str, str]] = None
        for index, rec in enumerate(records):
            path = rec.get("worktree")
            if path is None:
                continue
            real = os.path.realpath(path)
            if real == worktree_real:
                if index == 0 or real == main_real:
                    return "Cannot heal the main worktree."
                match = rec
                break
        if match is None:
            return "Worktree not in git worktree list."

        if issue is not None:
            branch = match.get("branch", "")
            name = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
            if not name or re.search(r"(?<!\d)%d(?!\d)" % issue, name) is None:
                return "Worktree branch does not name the issue."

        return None
    except subprocess.TimeoutExpired:
        return "Worktree list check timed out."
    except Exception:
        _note_failure("worktree validation")
        return "Worktree validation failed."


def _check_unchanged_inputs(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    inputs: Tuple[str, ...],
) -> bool:
    """Check that input files are unchanged against the merge-base. False on any change or error."""
    if not inputs:
        return True
    if not base:
        return False

    try:
        result = _git_wt(["diff", "--name-only", f"{base}...HEAD"], worktree)
        if result.returncode != 0:
            return False
        changed_files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
        return not any(inp in changed_files for inp in inputs)
    except Exception:
        _note_failure("check unchanged inputs")
        return False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_object(path: Path) -> Optional[Dict[str, Any]]:
    """A JSON object file; None when missing, unreadable or not an object."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _str_map(value: Any, allowed: Optional[Tuple[str, ...]]) -> Optional[Dict[str, str]]:
    """A step -> string object (absent or null reads as empty); None when malformed."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            return None
        if allowed is not None and item not in allowed:
            return None
    return dict(value)


def _classify_reason(reason: str) -> Optional[ReasonClass]:
    """
    Map a stop_reason onto a closed class token. The text itself never travels further.

    The literals are not verified against live Claude Code or check-runner output: the permission
    wording and the check_not_configured / "Reduced run (audit)" prefixes are pinned as fixtures in
    tests/test_heal.py. Permission is tested first, so a reason carrying both classes is a refusal.
    """
    low = reason.lower()
    if all(marker in low for marker in PERMISSION_MARKERS):
        return "permission-refusal"
    if "check_not_configured" in low:
        return "check-not-configured"
    if low.startswith("reduced run (audit)"):
        return "reduced-run"
    return None


def _read_stop_evidence(flow_dir: Path, issue: int) -> Optional[StopEvidence]:
    """
    Build StopEvidence from the flow's real state.json (and fleet-ask.json when present).

    Fails closed: any malformed or non-integer field, an unknown step or status, or an ask that is
    not an open Flow stop gate produces None. An absent failures[STEP] counts as 0.

    Residual (accepted, ADR-0025 §6): state.json and fleet-ask.json sit in the flow directory, which
    the ticket's own session can write. Nomination therefore starts from ticket-writable data. The
    probe's read-only preconditions (deps_currency, worktree validation), not this evidence, decide
    whether an install is actually due.
    """
    try:
        state = _load_object(flow_dir / "state.json")
        if state is None or str(state.get("issue")) != str(issue):
            return None

        step = state.get("step")
        status = state.get("status")
        if not isinstance(step, str) or step not in KNOWN_STEPS:
            return None
        if not isinstance(status, str) or status not in _STATUSES:
            return None

        raw_failures = state.get("failures")
        if raw_failures is None:
            raw_failures = {}
        if not isinstance(raw_failures, dict):
            return None
        failures: Dict[str, int] = {}
        for key, count in raw_failures.items():
            if not isinstance(key, str) or not _is_int(count) or count < 0:
                return None
            failures[key] = count

        sources = _str_map(state.get("stop_source"), _STOP_SOURCES)
        reasons = _str_map(state.get("stop_reason"), None)
        if sources is None or reasons is None:
            return None
        stop_source = sources.get(step)
        reason = reasons.get(step, "")

        raw_deps = state.get("deps")
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list):
            return None
        for entry in raw_deps:
            if (
                not isinstance(entry, dict)
                or entry.get("step") not in _DEPS_STEPS
                or entry.get("result") not in _DEPS_RESULTS
                or not isinstance(entry.get("at"), str)
            ):
                return None
        last_deps_result = raw_deps[-1]["result"] if raw_deps else None
        last_deps_step = raw_deps[-1]["step"] if raw_deps else None

        wt = state.get("worktree")
        if wt is not None and not isinstance(wt, str):
            return None

        ask_path = flow_dir / "fleet-ask.json"
        ask_bytes: Optional[bytes] = None
        ask: Optional[Dict[str, Any]] = None
        if ask_path.exists():
            ask_bytes = ask_path.read_bytes()
            try:
                parsed = json.loads(ask_bytes)
            except ValueError:
                return None
            if not isinstance(parsed, dict):
                return None
            ask = parsed

        kind: EvidenceKind
        offers_retry = False
        header: Optional[str] = None
        if ask is not None and ask.get("status") == "open":
            gate = ask.get("gate")
            if ask.get("kind") != "gate" or not isinstance(gate, dict):
                return None
            if gate.get("header") != FLOW_STOP_HEADER or ask.get("step") != step:
                return None
            options = gate.get("options")
            if not isinstance(options, list):
                return None
            labels = []
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    return None
                labels.append(option["label"])
            offers_retry = "Retry" in labels
            kind = "flow-stop"
            header = FLOW_STOP_HEADER
            key = stop_key_for(ask_bytes, step, status, reason)
        else:
            if status == "failed":
                kind = "failed"
            elif status == "paused":
                kind = "stopped"
            else:
                return None
            key = stop_key_for(None, step, status, reason)

        return StopEvidence(
            kind=kind,
            header=header,
            step=step,
            failures=failures,
            stop_source=stop_source,
            stop_reason_class=_classify_reason(reason),
            last_deps_result=last_deps_result,
            offers_retry=offers_retry,
            stop_key=key,
            last_deps_step=last_deps_step,
            worktree=wt,
        )
    except Exception:
        _note_failure("read stop evidence")
        return None


def _read_project_override(main_worktree: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Read fleet.heal.tools from the main worktree's .claude/project.yaml, never the ticket worktree.

    Returns (override, diagnosis). Fails closed: any problem returns ({}, OVERRIDE_INVALID_LINE), and
    the caller must then run no tool. Absent file, or absent fleet.heal.tools, is ({}, None). Accepted
    per tool: `enabled` (a real bool) and its declared params, each an int (not a bool) within its bounds.
    """
    path = main_worktree / ".claude" / "project.yaml"
    try:
        if not path.exists():
            return {}, None
        if deps.yaml is None:
            return {}, OVERRIDE_INVALID_LINE
        config = deps.yaml.safe_load(path.read_text())
        if config is None:
            return {}, None
        if not isinstance(config, dict):
            return {}, OVERRIDE_INVALID_LINE
        fleet = config.get("fleet")
        if fleet is None:
            return {}, None
        if not isinstance(fleet, dict):
            return {}, OVERRIDE_INVALID_LINE
        heal_config = fleet.get("heal")
        if heal_config is None:
            return {}, None
        if not isinstance(heal_config, dict) or set(heal_config) - {"tools"}:
            return {}, OVERRIDE_INVALID_LINE
        tools_config = heal_config.get("tools")
        if tools_config is None:
            return {}, None
        if not isinstance(tools_config, dict):
            return {}, OVERRIDE_INVALID_LINE

        known = {t.name: t for t in TOOLS}
        validated: Dict[str, Any] = {}
        for tool_name, tool_config in tools_config.items():
            if not isinstance(tool_name, str) or tool_name not in known:
                return {}, OVERRIDE_INVALID_LINE
            if not isinstance(tool_config, dict):
                return {}, OVERRIDE_INVALID_LINE
            allowed = {"enabled"} | set(known[tool_name].params)
            entry: Dict[str, Any] = {}
            for key, value in tool_config.items():
                if not isinstance(key, str) or key not in allowed:
                    return {}, OVERRIDE_INVALID_LINE
                if key == "enabled":
                    if not isinstance(value, bool):
                        return {}, OVERRIDE_INVALID_LINE
                else:
                    # A declared parameter with no bounds entry fails closed rather than taking a placeholder range.
                    bounds = PARAM_BOUNDS.get(key)
                    if bounds is None:
                        return {}, OVERRIDE_INVALID_LINE
                    low, high = bounds
                    if not _is_int(value) or not low <= value <= high:
                        return {}, OVERRIDE_INVALID_LINE
                entry[key] = value
            validated[tool_name] = entry
        return validated, None
    except Exception:
        _note_failure("read project override")
        return {}, OVERRIDE_INVALID_LINE


def _as_status(value: Any) -> Optional[Status]:
    """The value as a Status when it is a member of the Literal, else None. Membership is checked before the cast."""
    if isinstance(value, str) and value in _STATUS_VALUES:
        return cast(Status, value)
    return None


def _as_heal_reason(value: Any) -> Optional[HealReason]:
    """The value as a HealReason when it is a member of the Literal, else None. Membership is checked before the cast."""
    if isinstance(value, str) and value in _HEAL_REASONS:
        return cast(HealReason, value)
    return None


def _checked_outcome(raw: Any) -> Tuple[Status, HealReason, bool]:
    """
    Normalise a Tool.run result. Anything malformed, or any status or reason outside its Literal, becomes
    failed / internal-error with changed False. Only a literal True counts as changed.
    """
    if not isinstance(raw, dict):
        return "failed", "internal-error", False
    status = _as_status(raw.get("status"))
    reason = _as_heal_reason(raw.get("reason"))
    if status is None or reason is None:
        return "failed", "internal-error", False
    return status, reason, raw.get("changed") is True


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte, looping over short writes. A zero-byte write raises rather than spinning."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to tail file")
        view = view[written:]


def _write_tail(fd: int, tail: Any) -> None:
    """
    Write the redacted installer tail to the tail file. Any failure here is noted and swallowed, so a
    completed run keeps its own result and is not reported as failed because its log could not be saved.
    """
    if not isinstance(tail, list) or not tail:
        return
    try:
        text = "\n".join(deps.redact_output("\n".join(str(x) for x in tail))) + "\n"
        _write_all(fd, text.encode("utf-8", "replace"))
    except Exception:
        _note_failure("tail write")


def _effective_timeout(tool: Tool, override: Dict[str, Any]) -> int:
    """The tool's timeout: an override value, bounded above by DEPS_TIMEOUT_SECS."""
    entry = override.get(tool.name)
    if isinstance(entry, dict) and _is_int(entry.get("timeout_secs")):
        return max(1, min(int(entry["timeout_secs"]), deps.DEPS_TIMEOUT_SECS))
    return deps.DEPS_TIMEOUT_SECS


# ────────────────────────────────────────────────────────────────────────────
# CLI entry points
# ────────────────────────────────────────────────────────────────────────────

def cli_probe(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    flow_dir: Path,
    issue: int,
) -> Dict[str, Any]:
    """
    Probe for a tool to run. Read-only; no cap spent.

    Returns {"tool", "timeout_secs", "stop_key"} for a nomination, {"line", "stop_key"} for a
    diagnosis or refusal, and {"stop_key": None} when there is nothing to do. Never raises.
    """
    try:
        evidence = _read_stop_evidence(flow_dir, issue)
        if evidence is None:
            return {"stop_key": None}

        tool, diagnosis = nominate(evidence, TOOLS, {})
        if diagnosis:
            return {"line": diagnosis, "stop_key": evidence.stop_key}
        if tool is None:
            return {"stop_key": evidence.stop_key}

        override, override_error = _read_project_override(main_worktree)
        if override_error:
            return {"line": override_error, "stop_key": evidence.stop_key}
        tool, diagnosis = nominate(evidence, TOOLS, override)
        if diagnosis:
            return {"line": diagnosis, "stop_key": evidence.stop_key}
        if tool is None:
            return {"stop_key": evidence.stop_key}

        if not base:
            return {"line": "No base ref; cannot heal.", "stop_key": evidence.stop_key}

        ok, refusal = probe(tool, (worktree, main_worktree, base), issue, evidence.worktree)
        if not ok:
            return {"line": refusal or "Tool probe failed.", "stop_key": evidence.stop_key}

        return {
            "tool": tool.name,
            "timeout_secs": _effective_timeout(tool, override),
            "stop_key": evidence.stop_key,
        }
    except Exception:
        _note_failure("probe")
        return {"line": "Heal probe failed.", "stop_key": None}


def _run_result(
    tool_name: str,
    status: str,
    reason: str,
    changed: bool,
    recommendation: str,
    stop_key: Optional[str],
) -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "status": status,
        "reason": reason,
        "changed": changed,
        "recommendation": recommendation,
        "stop_key": stop_key,
    }


def cli_run(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    flow_dir: Path,
    issue: int,
    tool_name: str,
    tail_file: Optional[Path],
) -> Dict[str, Any]:
    """
    Run a tool and return the result JSON. Never raises.

    Returns {"tool", "status", "reason", "changed", "recommendation", "stop_key"}. The tool,
    nomination, override and probe are all re-checked here, so a stale or hand-built call cannot
    bypass them. The redacted installer tail goes only to tail_file (mode 0600, created exclusively
    without following a symlink).
    """
    known = {t.name: t for t in TOOLS}
    fd: Optional[int] = None
    try:
        tool = known.get(tool_name)
        if tool is None:
            return _run_result("", "failed", "internal-error", False, "take-over", None)

        evidence = _read_stop_evidence(flow_dir, issue)
        if evidence is None:
            return _run_result(tool.name, "failed", "internal-error", False, "take-over", None)
        key = evidence.stop_key

        override, override_error = _read_project_override(main_worktree)
        if override_error:
            return _run_result(tool.name, "refused", "precondition-refused", False, "take-over", key)
        nominated, _line = nominate(evidence, TOOLS, override)
        if nominated is None or nominated.name != tool.name or not base:
            return _run_result(tool.name, "refused", "precondition-refused", False, "take-over", key)

        ok, _refusal = probe(tool, (worktree, main_worktree, base), issue, evidence.worktree)
        if not ok:
            return _run_result(tool.name, "refused", "precondition-refused", False, "take-over", key)

        if tail_file is not None:
            fd = os.open(
                str(tail_file),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )

        raw = tool.run(worktree, main_worktree, base, _effective_timeout(tool, override), tail_file)
        status, reason, changed = _checked_outcome(raw)

        # The tail is best effort: a failed write is noted inside _write_tail and never changes the result.
        if fd is not None and isinstance(raw, dict):
            _write_tail(fd, raw.get("tail"))

        # A failed post-check means nothing verified changed, so it can never recommend a retry.
        if status == "healed" and not tool.verify(worktree, main_worktree, base):
            status, reason, changed = "failed", "internal-error", False

        recommendation = after_heal(
            HealResult(
                tool=tool.name,
                status=status,
                reason=reason,
                changed=changed,
                recommendation="take-over",
                stop_key=key,
            ),
            evidence,
        )
        return _run_result(tool.name, status, reason, changed, recommendation, key)
    except Exception:
        _note_failure("run")
        return _run_result(
            tool_name if tool_name in known else "", "failed", "internal-error", False, "take-over", None
        )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
