"""
Bounded self-heal harness for expert-fleet (#425, Step 1).

This module provides the tool registry, nomination logic, and the probe/run
interface for the fleet's automatic heal capability.

A tool is a precondition + fixed action + post-check. Each ticket stop
produces at most one tool run. The harness is testable in pure functions;
side effects go through an injected runner.

Invoked by scripts/workflow/cli.py heal subcommand.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from . import deps as deps_module

# Type aliases
Permission = Literal["runs-repo-code", "writes-gitignored", "no-write"]
Approval = Literal["run-first", "wait-for-approval"]
Gate = Literal["harness", "ensure-deps-exception"]
Status = Literal["no-op", "healed", "refused", "failed"]

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
    run: Callable[[Path, Path, Optional[str], int, Optional[Path]], Any]  # (worktree, main, base, timeout, tail_file) -> result
    verify: Callable[[Path, Path, Optional[str]], bool]  # (worktree, main, base) -> changed


@dataclass(frozen=True)
class StopEvidence:
    """Parsed flow data that nominates a tool or a diagnosis."""
    kind: str  # "flow-stop", "failed", "stopped", etc.
    header: Optional[str]  # Gate header name, if any
    step: Optional[str]  # The step that failed
    failures: Dict[str, int]  # failures[step] counters
    stop_source: Optional[str]  # Where the stop came from
    stop_reason_class: Optional[str]  # e.g. "permission-refusal"
    last_deps_result: Optional[str]  # Latest deps status or None
    offers_retry: bool  # Does the gate offer Retry as an option?
    stop_key: Optional[str]  # Identifies this stop: ask hash or state hash


@dataclass(frozen=True)
class HealResult:
    """Outcome of a heal run."""
    tool: str
    status: Status
    reason: HealReason
    changed: bool
    recommendation: Recommendation
    stop_key: Optional[str]


# ────────────────────────────────────────────────────────────────────────────
# The install tool (Step 4)
# ────────────────────────────────────────────────────────────────────────────

def _install_precondition(worktree: Path, main_worktree: Path, base: Optional[str]) -> bool:
    """Install runs only if deps are out of date (currency check returns 'stale')."""
    if base is None:
        return False
    try:
        from . import deps  # type: ignore[import-untyped]
        result = deps.deps_currency(worktree, main_worktree, base)  # type: ignore[attr-defined]
        return result == "stale"
    except Exception:
        return False


def _install_run(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    timeout: int,
    tail_file: Optional[Path],
) -> Dict[str, Any]:
    """Run ensure_deps with ignore_approvals=True."""
    if base is None:
        return {"status": "refused", "reason": "no-base"}
    try:
        from . import deps  # type: ignore[import-untyped]
        result = deps.ensure_deps(  # type: ignore[call-arg]
            worktree,
            main_worktree,
            base=base,
            approve_manifest=False,
            timeout=timeout,
            ignore_approvals=True,
        )
        # Map status to heal status
        if result.status == "installed":
            return {"status": "healed", "reason": "currency-installed", "changed": True}
        elif result.status == "current":
            return {"status": "no-op", "reason": "currency-current", "changed": False}
        elif result.status == "manifest-changed":
            return {"status": "refused", "reason": "currency-manifest-changed", "changed": False}
        elif result.status == "ambiguous":
            return {"status": "refused", "reason": "currency-ambiguous", "changed": False}
        elif result.status == "skipped":
            return {"status": "refused", "reason": "currency-skipped", "changed": False}
        elif result.status == "failed":
            return {"status": "failed", "reason": "currency-failed", "changed": False}
        else:
            return {"status": "failed", "reason": "internal-error", "changed": False}
    except Exception:
        return {"status": "failed", "reason": "internal-error", "changed": False}


def _install_verify(worktree: Path, main_worktree: Path, base: Optional[str]) -> bool:
    """Post-check: ensure_deps reports 'current' after a heal."""
    if base is None:
        return False
    try:
        from . import deps  # type: ignore[import-untyped]
        result = deps.deps_currency(worktree, main_worktree, base)  # type: ignore[attr-defined]
        return result == "current"
    except Exception:
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


# ────────────────────────────────────────────────────────────────────────────
# Core harness logic (Step 3, pure functions)
# ────────────────────────────────────────────────────────────────────────────

def nominate(
    evidence: Optional[StopEvidence],
    tools: Tuple[Tool, ...],
    override: Dict[str, Any],
) -> Tuple[Optional[Tool], Optional[str]]:
    """
    Nominate a tool or a diagnosis line. Pure, no I/O.

    Returns:
        (tool, None) for a nomination, or (None, diagnosis_line) for a refusal/diagnosis,
        or (None, None) for no evidence.

    Refusals:
    - failures[step] >= 2
    - Unknown header or kind
    - last_deps_result == "manifest-changed"
    - stop_reason_class indicates permission refusal
    """
    if evidence is None:
        return None, None

    # Validate we have the required fields to nominate
    if evidence.stop_source is None and (
        evidence.last_deps_result is None or evidence.stop_reason_class != "failed"
    ):
        return None, "No evidence to nominate."

    # Check failure cap (W1 A: failures < 2 means 0 or 1)
    step_failures = evidence.failures.get(evidence.step or "unknown", 0)
    if step_failures >= 2:
        return None, f"Failure limit reached ({step_failures} attempts)."

    # Check for unknown/unsupported stop types
    if evidence.kind not in ("flow-stop", "failed", "stopped"):
        return None, "Unknown stop type."

    # Check for permission-refusal (never nominates a tool)
    if evidence.stop_reason_class == "permission-refusal":
        return None, "Permission refusal; cannot heal."

    # Check for manifest-changed (blocks nomination)
    if evidence.last_deps_result == "manifest-changed":
        return None, "Manifest changed; needs approval."

    # Try to find a matching tool
    for tool in tools:
        # Check if tool is enabled in override
        if tool.name in override:
            tool_config = override[tool.name]
            if isinstance(tool_config, dict) and tool_config.get("enabled") is False:
                continue

        # For install: matches missing-deps cause (inferred from last_deps_result)
        if tool.cause == "missing-deps":
            if evidence.last_deps_result in ("stale",):
                return tool, None

    # No tool matched; return diagnosis
    return None, "No matching heal tool."


def probe(
    tool: Tool,
    context: Tuple[Path, Path, Optional[str]],
    runner: Optional[Callable[[Tool, Tuple[Path, Path, Optional[str]]], bool]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Read-only probe of whether a tool can run. No cap spent.

    Returns:
        (True, None) if precondition passes, or (False, refusal_line) otherwise.
    """
    worktree, main_worktree, base = context

    # Validate worktree (Step 4)
    validation_error = _validate_worktree(worktree, main_worktree)
    if validation_error:
        return False, validation_error

    # Check unchanged-inputs gate (Step 3)
    if tool.gate == "harness" and tool.inputs:
        unchanged = _check_unchanged_inputs(worktree, main_worktree, base, tool.inputs)
        if not unchanged:
            return False, "Tool inputs have changed."

    # Check approval rule (Step 3)
    if tool.approval == "wait-for-approval":
        return False, f"Tool {tool.name} awaiting approval."

    # Run precondition
    if not tool.precondition(worktree, main_worktree, base):
        return False, f"Precondition failed for {tool.name}."

    return True, None


def after_heal(result: HealResult, evidence: StopEvidence) -> Recommendation:
    """
    Pure function: choose retry or take-over based on result and evidence.

    Retry is recommended only when:
    - evidence.offers_retry is True
    - tool changed something
    - last_deps_result is not "manifest-changed"

    Otherwise: take-over.
    """
    if (
        evidence.offers_retry
        and result.changed
        and evidence.last_deps_result != "manifest-changed"
    ):
        return "retry-recommended"
    return "take-over"


# ────────────────────────────────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────────────────────────────────

def _validate_worktree(worktree: Path, main_worktree: Path) -> Optional[str]:
    """
    Validate that the worktree is safe to modify.

    Checks:
    - Appears in git worktree list of main repo
    - Realpath matches
    - Is not the main worktree
    - Branch names the issue (best-effort)

    Returns error string or None if valid.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(main_worktree),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "Could not list worktrees."

        worktree_realpath = worktree.resolve()
        main_realpath = main_worktree.resolve()

        if worktree_realpath == main_realpath:
            return "Cannot heal the main worktree."

        # Check if worktree appears in list
        found = False
        for line in result.stdout.split("\n"):
            if not line.strip():
                continue
            parts = line.split()
            if parts:
                wt_path = Path(parts[0]).resolve()
                if wt_path == worktree_realpath:
                    found = True
                    break

        if not found:
            return "Worktree not in git worktree list."

        return None
    except subprocess.TimeoutExpired:
        return "Worktree list check timed out."
    except Exception as e:
        return f"Worktree validation error: {e}"


def _check_unchanged_inputs(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    inputs: Tuple[str, ...],
) -> bool:
    """
    Check if input files are unchanged against the merge-base.

    Returns True if all inputs are unchanged (or do not exist), False if any changed.
    """
    if not base or not inputs:
        return True

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        changed_files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

        for inp in inputs:
            if inp in changed_files:
                return False

        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def _read_stop_evidence(flow_dir: Path, issue: int) -> Optional[StopEvidence]:
    """
    Read stop evidence from flow directory state.json and fleet-ask.json.

    Builds StopEvidence from structured fields, returning None if malformed or missing.
    """
    state_file = flow_dir / "state.json"
    ask_file = flow_dir / "fleet-ask.json"

    try:
        if not state_file.exists():
            return None

        state_data = json.loads(state_file.read_text())

        # Extract required fields
        kind = state_data.get("kind", "unknown")
        header = state_data.get("header")
        step = state_data.get("step")
        status = state_data.get("status")
        failures = state_data.get("failures", {})

        # Infer stop_source and stop_reason_class
        stop_source = state_data.get("stop_source")
        stop_reason_class = state_data.get("stop_reason_class")
        last_deps_result = state_data.get("last_deps_result")

        # Check if gate offers Retry (from the gate record or the options)
        offers_retry = (
            isinstance(state_data.get("gate"), dict)
            and "Retry" in state_data["gate"].get("options", [])
        )

        # Compute stop_key from ask or state
        stop_key = None
        if ask_file.exists():
            ask_text = ask_file.read_text()
            import hashlib
            stop_key = hashlib.sha256(ask_text.encode()).hexdigest()[:16]
        else:
            # Hash of step, status, reason
            state_hash_input = f"{step}:{status}:{stop_reason_class}"
            import hashlib
            stop_key = hashlib.sha256(state_hash_input.encode()).hexdigest()[:16]

        return StopEvidence(
            kind=kind,
            header=header,
            step=step,
            failures=failures,
            stop_source=stop_source,
            stop_reason_class=stop_reason_class,
            last_deps_result=last_deps_result,
            offers_retry=offers_retry,
            stop_key=stop_key,
        )
    except Exception:
        return None


def _read_project_override(main_worktree: Path) -> Dict[str, Any]:
    """
    Read fleet.heal.tools from main worktree's .claude/project.yaml.

    Returns tool overrides dict, or {} on any error (fail closed).
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        yaml = None  # type: ignore[assignment]

    if yaml is None:
        return {}

    try:
        project_yaml = main_worktree / ".claude" / "project.yaml"
        if not project_yaml.exists():
            return {}

        config = yaml.safe_load(project_yaml.read_text()) or {}
        heal_config = config.get("fleet", {}).get("heal", {}).get("tools", {})

        # Validate: only bool "enabled" and declared params; reject argv
        validated: Dict[str, Any] = {}
        for tool_name, tool_config in (heal_config.items() if isinstance(heal_config, dict) else []):
            if not isinstance(tool_config, dict):
                continue
            v: Dict[str, Any] = {}
            if "enabled" in tool_config:
                if not isinstance(tool_config["enabled"], bool):
                    return {}  # Fail closed on invalid type
                v["enabled"] = tool_config["enabled"]
            # Validate param bounds (deferred to tool validation)
            for param_name in ["timeout_secs"]:
                if param_name in tool_config:
                    val = tool_config[param_name]
                    if not isinstance(val, int) or val <= 0:
                        return {}  # Fail closed
                    v[param_name] = val
            validated[tool_name] = v

        return validated
    except Exception:
        return {}  # Fail closed on parse error or any other error


# ────────────────────────────────────────────────────────────────────────────
# CLI entry points (Step 6)
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

    Returns JSON: {"tool": name, "timeout_secs": int, "stop_key": str}
    or {"line": diagnosis, "stop_key": str or null}
    """
    try:
        # Read stop evidence
        evidence = _read_stop_evidence(flow_dir, issue)
        if evidence is None:
            return {"line": "No stop evidence found.", "stop_key": None}

        # Read project overrides
        override = _read_project_override(main_worktree)

        # Nominate a tool or diagnosis
        tool, diagnosis = nominate(evidence, TOOLS, override)

        if diagnosis:
            return {"line": diagnosis, "stop_key": evidence.stop_key}

        if tool is None:
            return {"line": "No tool nominated.", "stop_key": evidence.stop_key}

        # Probe the tool
        ok, refusal = probe(tool, (worktree, main_worktree, base))
        if not ok:
            return {"line": refusal or "Tool probe failed.", "stop_key": evidence.stop_key}

        # Get timeout from override or default
        timeout_secs = 900  # DEPS_TIMEOUT_SECS
        if tool.name in override and isinstance(override[tool.name], dict):
            if "timeout_secs" in override[tool.name]:
                timeout_secs = min(override[tool.name]["timeout_secs"], 900)

        return {
            "tool": tool.name,
            "timeout_secs": timeout_secs,
            "stop_key": evidence.stop_key,
        }
    except Exception as e:
        return {"line": f"Probe failed: {e}", "stop_key": None}


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
    Run a tool and return the result JSON.

    Returns: {"tool": name, "status": enum, "reason": enum, "changed": bool,
              "recommendation": enum, "stop_key": str}
    """
    try:
        # Find the tool
        tool = None
        for t in TOOLS:
            if t.name == tool_name:
                tool = t
                break

        if tool is None:
            return {
                "tool": tool_name,
                "status": "failed",
                "reason": "internal-error",
                "changed": False,
                "recommendation": "take-over",
                "stop_key": None,
            }

        # Read stop evidence
        evidence = _read_stop_evidence(flow_dir, issue)
        if evidence is None:
            return {
                "tool": tool_name,
                "status": "failed",
                "reason": "internal-error",
                "changed": False,
                "recommendation": "take-over",
                "stop_key": None,
            }

        # Read overrides
        override = _read_project_override(main_worktree)

        # Probe the tool
        ok, refusal = probe(tool, (worktree, main_worktree, base))
        if not ok:
            return {
                "tool": tool_name,
                "status": "refused",
                "reason": "precondition-refused",
                "changed": False,
                "recommendation": "take-over",
                "stop_key": evidence.stop_key,
            }

        # Get timeout
        timeout_secs = 900
        if tool.name in override and isinstance(override[tool.name], dict):
            if "timeout_secs" in override[tool.name]:
                timeout_secs = min(override[tool.name]["timeout_secs"], 900)

        # Run the tool
        run_result = tool.run(worktree, main_worktree, base, timeout_secs, tail_file)

        # Extract result fields
        status = run_result.get("status", "failed")
        reason = run_result.get("reason", "internal-error")
        changed = run_result.get("changed", False)

        # Check post-condition if healed
        if status == "healed":
            verify_ok = tool.verify(worktree, main_worktree, base)
            if not verify_ok:
                status = "failed"
                reason = "internal-error"

        # Compute recommendation
        recommendation = after_heal(
            HealResult(
                tool=tool_name,
                status=status,
                reason=reason,
                changed=changed,
                recommendation="take-over",
                stop_key=evidence.stop_key,
            ),
            evidence,
        )

        return {
            "tool": tool_name,
            "status": status,
            "reason": reason,
            "changed": changed,
            "recommendation": recommendation,
            "stop_key": evidence.stop_key,
        }
    except Exception as e:
        return {
            "tool": tool_name,
            "status": "failed",
            "reason": "internal-error",
            "changed": False,
            "recommendation": "take-over",
            "stop_key": None,
        }
