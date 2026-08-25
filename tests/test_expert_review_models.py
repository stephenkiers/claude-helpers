#!/usr/bin/env python3
"""
Black-box behavioral test suite for scripts/resolve-expert-review-models.py (issue #76).

Exercises the resolver's registry validation, settings-aware availability
checking, fallback policy, and explicit --model override handling — all via
subprocess so the resolver's internal logic is never imported directly.
Temporary fixture files stand in for both the model registry and settings.json
so the user's real configuration is never touched.

Run with: python3 tests/test_expert_review_models.py
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

RESOLVER = REPO_ROOT / "scripts" / "resolve-expert-review-models.py"

# --- Deterministic test provider IDs (never depend on the real environment) ---
TEST_HAIKU_ID = "test-haiku-id"
TEST_SONNET_ID = "test-sonnet-id"
TEST_OPUS_ID = "test-opus-id"

TEST_ENV = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": TEST_HAIKU_ID,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": TEST_SONNET_ID,
    "ANTHROPIC_DEFAULT_OPUS_MODEL": TEST_OPUS_ID,
}

VALID_REGISTRY = json.dumps({
    "schemaVersion": 1,
    "aliases": ["haiku", "sonnet", "opus", "fable"],
    "roles": {
        "router": ["sonnet", "haiku"],
        "mechanical": ["haiku", "sonnet"],
        "panel": ["sonnet", "haiku"],
        "escalation": ["opus", "sonnet"],
    },
})

ALL_ROLES = ["router", "mechanical", "panel", "escalation"]

# Expected first-alias per role when availability is unchecked or all-available.
FIRST_ALIAS = {
    "router": "sonnet",
    "mechanical": "haiku",
    "panel": "sonnet",
    "escalation": "opus",
}

# --- Temp fixture directory ---
_fixture_dir = Path(tempfile.mkdtemp(prefix="expert-review-models-test-"))
atexit.register(shutil.rmtree, _fixture_dir, ignore_errors=True)

_counter = [0]


def _unique_path(stem):
    _counter[0] += 1
    return _fixture_dir / f"{stem}_{_counter[0]}.json"


def write_registry(content=VALID_REGISTRY):
    """Write a registry fixture file and return its path."""
    p = _unique_path("registry")
    p.write_text(content)
    return p


def write_settings(enforce=None, available=None, env_models=None):
    """Write a settings.json fixture and return its path."""
    settings = {}
    if enforce is not None:
        settings["enforceAvailableModels"] = enforce
    if available is not None:
        settings["availableModels"] = available
    if env_models is not None:
        settings["env"] = env_models
    p = _unique_path("settings")
    p.write_text(json.dumps(settings))
    return p


# Reusable settings fixtures ----------------------------------------------------

# All three mapped models available.
SETTINGS_ALL_AVAILABLE = None  # built lazily below (needs temp path)

# Only haiku and opus available — sonnet missing, triggers fallback for router/panel.
SETTINGS_SONNET_MISSING = None

# Only opus available — haiku and sonnet both missing, router/panel/mechanical fail.
SETTINGS_ONLY_OPUS = None

# Enforcement off.
SETTINGS_ENFORCE_OFF = None


def _init_settings():
    """Build the reusable settings fixtures once."""
    global SETTINGS_ALL_AVAILABLE, SETTINGS_SONNET_MISSING, SETTINGS_ONLY_OPUS, SETTINGS_ENFORCE_OFF
    env_models = {
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": TEST_HAIKU_ID,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": TEST_SONNET_ID,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": TEST_OPUS_ID,
    }
    SETTINGS_ALL_AVAILABLE = write_settings(
        enforce=True,
        available=[TEST_HAIKU_ID, TEST_SONNET_ID, TEST_OPUS_ID],
        env_models=env_models,
    )
    SETTINGS_SONNET_MISSING = write_settings(
        enforce=True,
        available=[TEST_HAIKU_ID, TEST_OPUS_ID],
        env_models=env_models,
    )
    SETTINGS_ONLY_OPUS = write_settings(
        enforce=True,
        available=[TEST_OPUS_ID],
        env_models=env_models,
    )
    SETTINGS_ENFORCE_OFF = write_settings(enforce=False)


def run_resolver(registry_path=None, settings_path=None, model=None):
    """Run the resolver as a subprocess. Returns (exit_code, stdout, stderr)."""
    env = {**os.environ, **TEST_ENV}
    if registry_path is not None:
        env["EXPERT_REVIEW_MODELS_PATH"] = str(registry_path)
    if settings_path is not None:
        env["EXPERT_REVIEW_SETTINGS_PATH"] = str(settings_path)

    cmd = [sys.executable, str(RESOLVER)]
    if model:
        cmd.extend(["--model", model])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return result.returncode, result.stdout, result.stderr


def parse_json(stdout):
    """Parse JSON from stdout, returning None on failure."""
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Initialise reusable fixtures
# ---------------------------------------------------------------------------
_init_settings()
_valid_registry = write_registry()

h = Harness("EXPERT-REVIEW MODEL RESOLVER TEST SUITE")
t = h.test_result

# ============================================================================
# SECTION 1: Valid default registry and deterministic resolution
# ============================================================================
print("[Section 1] Valid registry and deterministic resolution")

# 1a — enforcement off: every role resolves to first alias, status unchecked
code, out, err = run_resolver(_valid_registry, SETTINGS_ENFORCE_OFF)
data = parse_json(out)
t(
    "enforcement off: exit 0 with valid JSON",
    code == 0 and data is not None,
    f"exit={code}, json={data is not None}, stderr={err[:120]}",
)
if data:
    t(
        "enforcement off: schemaVersion is 1",
        data.get("schemaVersion") == 1,
        f"got {data.get('schemaVersion')}",
    )
    for role in ALL_ROLES:
        t(
            f"enforcement off: {role} resolves to first alias '{FIRST_ALIAS[role]}'",
            data.get("resolved", {}).get(role) == FIRST_ALIAS[role],
            f"got {data.get('resolved', {}).get(role)}",
        )
        t(
            f"enforcement off: {role} status is 'unchecked'",
            data.get("status", {}).get(role) == "unchecked",
            f"got {data.get('status', {}).get(role)}",
        )
    t(
        "enforcement off: availableChecked is false",
        data.get("availableChecked") is False,
        f"got {data.get('availableChecked')}",
    )
    t(
        "enforcement off: panelOverride is false",
        data.get("panelOverride") is False,
        f"got {data.get('panelOverride')}",
    )
    t(
        "enforcement off: no fallbacks",
        data.get("fallbacks") == [],
        f"got {data.get('fallbacks')}",
    )

# 1b — all models available: every role resolves to first alias, status available
code, out, err = run_resolver(_valid_registry, SETTINGS_ALL_AVAILABLE)
data = parse_json(out)
t(
    "all available: exit 0 with valid JSON",
    code == 0 and data is not None,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    for role in ALL_ROLES:
        t(
            f"all available: {role} resolves to first alias '{FIRST_ALIAS[role]}'",
            data.get("resolved", {}).get(role) == FIRST_ALIAS[role],
            f"got {data.get('resolved', {}).get(role)}",
        )
        t(
            f"all available: {role} status is 'available'",
            data.get("status", {}).get(role) == "available",
            f"got {data.get('status', {}).get(role)}",
        )
    t(
        "all available: availableChecked is true",
        data.get("availableChecked") is True,
        f"got {data.get('availableChecked')}",
    )
    t(
        "all available: no fallbacks",
        data.get("fallbacks") == [],
        f"got {data.get('fallbacks')}",
    )

# 1c — configured lists are echoed from the registry
if data:
    expected_configured = json.loads(VALID_REGISTRY)["roles"]
    t(
        "configured lists match registry roles",
        data.get("configured") == expected_configured,
        f"got {data.get('configured')}",
    )

print()

# ============================================================================
# SECTION 2: Registry validation errors (exit 1, no stdout)
# ============================================================================
print("[Section 2] Registry validation errors")

def test_registry_error(label, content, *, path=None, expect_in_err=None):
    """Assert that a malformed registry produces exit 1, empty stdout, stderr message.

    When expect_in_err is given, additionally assert that substring appears in
    stderr — pinning the message content so a broken impl that prints any ERROR
    cannot pass on exit code alone.
    """
    if path is not None:
        reg = path
    else:
        reg = write_registry(content)
    code, out, err = run_resolver(reg, SETTINGS_ENFORCE_OFF)
    t(
        f"{label}: exit 1",
        code == 1,
        f"expected exit 1, got {code}",
    )
    t(
        f"{label}: no stdout",
        out == "",
        f"stdout should be empty, got {out[:80]}",
    )
    t(
        f"{label}: error message on stderr",
        "ERROR" in err,
        f"stderr: {err[:120]}",
    )
    if expect_in_err is not None:
        t(
            f"{label}: stderr mentions '{expect_in_err}'",
            expect_in_err in err,
            f"stderr: {err[:160]}",
        )


test_registry_error(
    "malformed JSON",
    '{"schemaVersion":1, broken}',
    expect_in_err="malformed JSON",
)
test_registry_error(
    "wrong schema version",
    '{"schemaVersion":99,"aliases":["haiku"],"roles":{"router":["haiku"]}}',
    expect_in_err="schemaVersion",
)
test_registry_error(
    "schemaVersion float (1.0) rejected",
    '{"schemaVersion":1.0,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],'
    '"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="schemaVersion",
)
test_registry_error(
    "schemaVersion bool (true) rejected",
    '{"schemaVersion":true,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],'
    '"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="schemaVersion",
)
test_registry_error(
    "missing role (no escalation)",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"]}}',
    expect_in_err="escalation",
)
test_registry_error(
    "empty fallback list for router",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":[],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="router",
)
test_registry_error(
    "duplicate alias in aliases",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable","haiku"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="duplicate",
)
test_registry_error(
    "duplicate alias within a role list",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],'
    '"panel":["sonnet","sonnet"],"escalation":["opus","sonnet"]}}',
    expect_in_err="duplicate",
)
test_registry_error(
    "unknown alias in role list",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["sonnet","turbo"],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="unknown alias",
)
test_registry_error(
    "exact provider model ID in role list",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable"],'
    '"roles":{"router":["claude-sonnet-4-5"],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="provider",
)
test_registry_error(
    "missing aliases key",
    '{"schemaVersion":1,"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],'
    '"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="aliases",
)
test_registry_error(
    "unknown alias in aliases vocabulary",
    '{"schemaVersion":1,"aliases":["haiku","sonnet","opus","fable","turbo"],'
    '"roles":{"router":["sonnet","haiku"],"mechanical":["haiku","sonnet"],"panel":["sonnet","haiku"],"escalation":["opus","sonnet"]}}',
    expect_in_err="vocabulary",
)

# Missing registry file (nonexistent path)
test_registry_error(
    "missing registry file",
    "",
    path=_fixture_dir / "nonexistent_registry.json",
    expect_in_err="not found",
)

print()

# ============================================================================
# SECTION 3: Settings-aware resolution
# ============================================================================
print("[Section 3] Settings-aware resolution")

# 3a — primary available: all roles get first alias with status "available"
# (already tested in Section 1b, but here we verify the settings-availability
#  path explicitly by using a fixture where only the primaries are available)
code, out, err = run_resolver(_valid_registry, SETTINGS_ALL_AVAILABLE)
data = parse_json(out)
t(
    "primary available: exit 0",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    t(
        "primary available: no fallbacks emitted",
        data.get("fallbacks") == [],
        f"got {data.get('fallbacks')}",
    )
    t(
        "primary available: no warnings on stderr",
        "WARNING" not in err,
        f"stderr: {err[:120]}",
    )

# 3b — primary missing, fallback available: router and panel fall back to haiku
code, out, err = run_resolver(_valid_registry, SETTINGS_SONNET_MISSING)
data = parse_json(out)
t(
    "sonnet missing + fallback: exit 0",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    # Roles whose primary is sonnet (router, panel) should fall back to haiku
    t(
        "sonnet missing: router resolves to 'haiku' (fallback)",
        data.get("resolved", {}).get("router") == "haiku",
        f"got {data.get('resolved', {}).get('router')}",
    )
    t(
        "sonnet missing: panel resolves to 'haiku' (fallback)",
        data.get("resolved", {}).get("panel") == "haiku",
        f"got {data.get('resolved', {}).get('panel')}",
    )
    t(
        "sonnet missing: router status is 'fallback'",
        data.get("status", {}).get("router") == "fallback",
        f"got {data.get('status', {}).get('router')}",
    )
    t(
        "sonnet missing: panel status is 'fallback'",
        data.get("status", {}).get("panel") == "fallback",
        f"got {data.get('status', {}).get('panel')}",
    )
    # mechanical primary is haiku (available) — no fallback
    t(
        "sonnet missing: mechanical stays 'haiku' (available, no fallback)",
        data.get("resolved", {}).get("mechanical") == "haiku"
        and data.get("status", {}).get("mechanical") == "available",
        f"resolved={data.get('resolved', {}).get('mechanical')}, "
        f"status={data.get('status', {}).get('mechanical')}",
    )
    # escalation primary is opus (available) — no fallback
    t(
        "sonnet missing: escalation stays 'opus' (available, no fallback)",
        data.get("resolved", {}).get("escalation") == "opus"
        and data.get("status", {}).get("escalation") == "available",
        f"resolved={data.get('resolved', {}).get('escalation')}, "
        f"status={data.get('status', {}).get('escalation')}",
    )
    # fallbacks list should contain router and panel entries
    fallbacks = data.get("fallbacks", [])
    fb_roles = {fb.get("role") for fb in fallbacks}
    t(
        "sonnet missing: fallbacks list contains router and panel",
        fb_roles == {"router", "panel"},
        f"got {fb_roles}",
    )
    # each fallback entry should name role, primary, missingModel, fallback
    for fb in fallbacks:
        role = fb.get("role")
        t(
            f"sonnet missing: fallback receipt for {role} has all fields",
            all(k in fb for k in ("role", "primary", "missingModel", "fallback")),
            f"keys: {sorted(fb.keys())}",
        )
        t(
            f"sonnet missing: fallback receipt for {role} names primary 'sonnet'",
            fb.get("primary") == "sonnet",
            f"got {fb.get('primary')}",
        )
        t(
            f"sonnet missing: fallback receipt for {role} names fallback 'haiku'",
            fb.get("fallback") == "haiku",
            f"got {fb.get('fallback')}",
        )
        t(
            f"sonnet missing: fallback receipt for {role} names missingModel",
            fb.get("missingModel") == TEST_SONNET_ID,
            f"got {fb.get('missingModel')}",
        )
    # warnings should appear on stderr
    t(
        "sonnet missing: WARNING on stderr for router fallback",
        "WARNING" in err and "router" in err,
        f"stderr: {err[:200]}",
    )
    t(
        "sonnet missing: WARNING on stderr for panel fallback",
        "WARNING" in err and "panel" in err,
        f"stderr: {err[:200]}",
    )

# 3c — all candidates missing for a role: ERROR, exit 1, no stdout
code, out, err = run_resolver(_valid_registry, SETTINGS_ONLY_OPUS)
t(
    "all candidates missing: exit 1",
    code == 1,
    f"expected exit 1, got {code}",
)
t(
    "all candidates missing: no stdout",
    out == "",
    f"stdout should be empty, got {out[:80]}",
)
t(
    "all candidates missing: ERROR on stderr",
    "ERROR" in err,
    f"stderr: {err[:200]}",
)
t(
    "all candidates missing: error names the failed role",
    "router" in err,
    f"stderr: {err[:200]}",
)

# 3d — enforceAvailableModels false → unchecked, availableChecked false
code, out, err = run_resolver(_valid_registry, SETTINGS_ENFORCE_OFF)
data = parse_json(out)
t(
    "enforce false: exit 0",
    code == 0,
    f"exit={code}",
)
if data:
    t(
        "enforce false: availableChecked is false",
        data.get("availableChecked") is False,
        f"got {data.get('availableChecked')}",
    )
    for role in ALL_ROLES:
        t(
            f"enforce false: {role} status is 'unchecked'",
            data.get("status", {}).get(role) == "unchecked",
            f"got {data.get('status', {}).get(role)}",
        )

# 3e — missing settings file → unchecked (not a failure)
code, out, err = run_resolver(
    _valid_registry, _fixture_dir / "nonexistent_settings.json",
)
data = parse_json(out)
t(
    "missing settings: exit 0 (not a failure)",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    t(
        "missing settings: availableChecked is false",
        data.get("availableChecked") is False,
        f"got {data.get('availableChecked')}",
    )
    for role in ALL_ROLES:
        t(
            f"missing settings: {role} status is 'unchecked'",
            data.get("status", {}).get(role) == "unchecked",
            f"got {data.get('status', {}).get(role)}",
        )

# 3f — empty settings file → unchecked (not a failure)
empty_settings = _unique_path("empty_settings")
empty_settings.write_text("")
code, out, err = run_resolver(_valid_registry, empty_settings)
data = parse_json(out)
t(
    "empty settings: exit 0 (not a failure)",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    t(
        "empty settings: availableChecked is false",
        data.get("availableChecked") is False,
        f"got {data.get('availableChecked')}",
    )

print()

# ============================================================================
# SECTION 4: Explicit --model override
# ============================================================================
print("[Section 4] Explicit --model override")

# 4a — valid available alias: panelOverride true, panel = haiku, exit 0
code, out, err = run_resolver(_valid_registry, SETTINGS_SONNET_MISSING, model="haiku")
data = parse_json(out)
t(
    "--model haiku (available): exit 0",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    t(
        "--model haiku: panelOverride is true",
        data.get("panelOverride") is True,
        f"got {data.get('panelOverride')}",
    )
    t(
        "--model haiku: panel resolves to 'haiku'",
        data.get("resolved", {}).get("panel") == "haiku",
        f"got {data.get('resolved', {}).get('panel')}",
    )
    t(
        "--model haiku: panel status is 'available'",
        data.get("status", {}).get("panel") == "available",
        f"got {data.get('status', {}).get('panel')}",
    )

# 4b — valid unavailable alias: ERROR, exit 1, no fallback
code, out, err = run_resolver(_valid_registry, SETTINGS_SONNET_MISSING, model="sonnet")
t(
    "--model sonnet (unavailable): exit 1",
    code == 1,
    f"expected exit 1, got {code}",
)
t(
    "--model sonnet (unavailable): no stdout",
    out == "",
    f"stdout should be empty, got {out[:80]}",
)
t(
    "--model sonnet (unavailable): ERROR on stderr",
    "ERROR" in err,
    f"stderr: {err[:200]}",
)
t(
    "--model sonnet (unavailable): error mentions 'sonnet'",
    "sonnet" in err,
    f"stderr: {err[:200]}",
)

# 4c — valid unmapped alias (fable): unchecked, exit 0
code, out, err = run_resolver(_valid_registry, SETTINGS_ALL_AVAILABLE, model="fable")
data = parse_json(out)
t(
    "--model fable (unmapped): exit 0",
    code == 0,
    f"exit={code}, stderr={err[:120]}",
)
if data:
    t(
        "--model fable: panelOverride is true",
        data.get("panelOverride") is True,
        f"got {data.get('panelOverride')}",
    )
    t(
        "--model fable: panel resolves to 'fable'",
        data.get("resolved", {}).get("panel") == "fable",
        f"got {data.get('resolved', {}).get('panel')}",
    )
    t(
        "--model fable: panel status is 'unchecked'",
        data.get("status", {}).get("panel") == "unchecked",
        f"got {data.get('status', {}).get('panel')}",
    )

# 4d — invalid alias: ERROR, exit 1
code, out, err = run_resolver(_valid_registry, SETTINGS_ALL_AVAILABLE, model="bogus")
t(
    "--model bogus (invalid): exit 1",
    code == 1,
    f"expected exit 1, got {code}",
)
t(
    "--model bogus (invalid): no stdout",
    out == "",
    f"stdout should be empty, got {out[:80]}",
)
t(
    "--model bogus (invalid): ERROR on stderr",
    "ERROR" in err,
    f"stderr: {err[:200]}",
)

# 4e — exact provider model ID: ERROR, exit 1
code, out, err = run_resolver(
    _valid_registry, SETTINGS_ALL_AVAILABLE, model="claude-sonnet-4-5",
)
t(
    "--model claude-sonnet-4-5 (exact ID): exit 1",
    code == 1,
    f"expected exit 1, got {code}",
)
t(
    "--model claude-sonnet-4-5 (exact ID): no stdout",
    out == "",
    f"stdout should be empty, got {out[:80]}",
)
t(
    "--model claude-sonnet-4-5 (exact ID): ERROR on stderr",
    "ERROR" in err,
    f"stderr: {err[:200]}",
)

print()
h.summarize_and_exit()
