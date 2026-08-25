#!/usr/bin/env python3
"""
Resolve expert-review model aliases to a concrete per-role selection.

Reads the checked-in registry at config/expert-review-models.json (located relative to this
script's real, symlink-resolved path, so it works when symlinked into ~/.claude/scripts/),
validates it against a hard-coded schema version and Agent-tool alias vocabulary, then optionally
consults ~/.claude/settings.json as an availability source to pick the first proven-available
alias per role — falling back to the next configured alias when the primary is proven unavailable,
or returning the first alias with status "unchecked" when availability cannot be proven.

Emits exactly one JSON object to stdout on success. On any validation or resolution error, prints
a precise message to stderr (naming the file path and a one-line remediation) and exits non-zero,
with no JSON on stdout. This keeps stdout pure JSON so callers can pipe it through jq safely.

Never rewrites settings.json. Never stores gateway/provider model IDs in the registry — the
registry holds semantic alias slots only; this resolver is the bridge that reads the settings env
mapping as an availability source.
"""

import argparse
import json
import os
import sys

SUPPORTED_SCHEMA_VERSION = 1
# The only aliases the Agent tool accepts as a `model` argument in this setup.
SUPPORTED_ALIASES = ("haiku", "sonnet", "opus", "fable")
REQUIRED_TOP_KEYS = ("schemaVersion", "aliases", "roles")
REQUIRED_ROLES = ("router", "mechanical", "panel", "escalation")
# Maps a semantic alias to the settings `env` key that names its concrete provider model ID.
# fable has no mapping here — its availability is always "unchecked".
ALIAS_TO_SETTINGS_ENV = {
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
}
# Production path for settings. Overridable via the EXPERT_REVIEW_SETTINGS_PATH env var so the
# resolver can be driven against temporary settings fixtures in tests without touching the user's
# real ~/.claude/settings.json. Unset in production → the real path is used.
SETTINGS_PATH = os.environ.get(
    "EXPERT_REVIEW_SETTINGS_PATH", os.path.expanduser("~/.claude/settings.json")
)


def fail(message):
    """Print a precise error to stderr and exit non-zero. Never emits JSON to stdout."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def registry_path_for(script_file):
    """Resolve the registry path relative to this script's real (symlink-resolved) location.

    Overridable via the EXPERT_REVIEW_MODELS_PATH env var so tests can point the resolver at a
    temporary registry fixture (malformed JSON, bad schema, etc.) without overwriting the
    checked-in file. Unset in production → the path next to this script is used.
    """
    override = os.environ.get("EXPERT_REVIEW_MODELS_PATH")
    if override:
        return override
    script_dir = os.path.dirname(os.path.realpath(script_file))
    return os.path.normpath(os.path.join(script_dir, "..", "config", "expert-review-models.json"))


def looks_like_provider_id(value):
    """True if an alias slot looks like an exact/provider model ID rather than a semantic alias."""
    if not isinstance(value, str):
        return False
    return (":" in value) or ("/" in value) or value.startswith("claude-")


def load_registry(path):
    """Read and JSON-parse the registry file, failing precisely on missing/malformed input."""
    if not os.path.isfile(path):
        fail(
            f"registry not found at {path}. Expected config/expert-review-models.json next to "
            "this script. Run /setup-local from the claude-helpers repo to install the symlinks."
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        fail(f"registry at {path} is malformed JSON: {exc}. Fix the file or re-run /setup-local.")


def validate_registry(data, path):
    """Validate the registry structure and contents. Returns (aliases, roles) on success."""
    if not isinstance(data, dict):
        fail(f"registry at {path} must be a JSON object at the top level.")

    keys = set(data.keys())
    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    extra = [k for k in keys if k not in REQUIRED_TOP_KEYS]
    if missing:
        fail(f"registry at {path} is missing top-level key(s): {', '.join(missing)}.")
    if extra:
        fail(
            f"registry at {path} has unknown top-level key(s): {', '.join(extra)}. "
            f"Allowed: {', '.join(REQUIRED_TOP_KEYS)}."
        )

    schema_version = data["schemaVersion"]
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        fail(
            f"registry at {path} has schemaVersion {schema_version!r}; this resolver supports "
            f"only {SUPPORTED_SCHEMA_VERSION}. Update scripts/resolve-expert-review-models.py "
            "or lower the registry schemaVersion."
        )

    aliases = data["aliases"]
    if not isinstance(aliases, list) or not aliases:
        fail(f"registry at {path}: 'aliases' must be a non-empty list.")
    seen = set()
    for alias in aliases:
        if not isinstance(alias, str):
            fail(f"registry at {path}: alias entry {alias!r} is not a string.")
        if alias in seen:
            fail(f"registry at {path}: duplicate alias {alias!r} in 'aliases'.")
        seen.add(alias)
        if looks_like_provider_id(alias):
            fail(
                f"registry at {path}: alias {alias!r} looks like a provider model ID "
                "(contains ':' or '/' or starts with 'claude-'); 'aliases' holds semantic "
                "slots only, not gateway IDs."
            )
        if alias not in SUPPORTED_ALIASES:
            fail(
                f"registry at {path}: alias {alias!r} is not in the resolver's supported "
                f"vocabulary {list(SUPPORTED_ALIASES)} — the Agent tool cannot accept it as a "
                "model argument."
            )

    roles = data["roles"]
    if not isinstance(roles, dict):
        fail(f"registry at {path}: 'roles' must be a JSON object.")
    role_keys = set(roles.keys())
    missing_roles = [r for r in REQUIRED_ROLES if r not in roles]
    extra_roles = [r for r in role_keys if r not in REQUIRED_ROLES]
    if missing_roles:
        fail(f"registry at {path}: missing role(s): {', '.join(missing_roles)}.")
    if extra_roles:
        fail(
            f"registry at {path}: unknown role(s): {', '.join(extra_roles)}. "
            f"Allowed roles: {', '.join(REQUIRED_ROLES)}."
        )

    alias_set = set(aliases)
    for role in REQUIRED_ROLES:
        role_list = roles[role]
        if not isinstance(role_list, list) or not role_list:
            fail(f"registry at {path}: role '{role}' must be a non-empty list.")
        for entry in role_list:
            if not isinstance(entry, str):
                fail(f"registry at {path}: role '{role}' entry {entry!r} is not a string.")
            if looks_like_provider_id(entry):
                fail(
                    f"registry at {path}: role '{role}' entry {entry!r} looks like a provider "
                    "model ID; role lists hold semantic aliases only."
                )
            if entry not in alias_set:
                fail(
                    f"registry at {path}: role '{role}' references unknown alias {entry!r}; "
                    f"not declared in 'aliases' ({', '.join(aliases)})."
                )

    return aliases, roles


def load_settings():
    """Defensively read ~/.claude/settings.json. Returns (settings_dict_or_None, read_ok)."""
    if not os.path.isfile(SETTINGS_PATH):
        return None, False
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None, False
    if not isinstance(settings, dict):
        return None, False
    return settings, True


def provider_id_for(alias, settings):
    """Return the mapped provider model ID for an alias from settings.env, or None."""
    env_key = ALIAS_TO_SETTINGS_ENV.get(alias)
    if env_key is None:
        return None
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return None
    provider_id = env.get(env_key)
    if isinstance(provider_id, str) and provider_id:
        return provider_id
    return None


def alias_availability(alias, settings, available_checked):
    """Return 'available', 'unavailable', or 'unchecked' for a single alias.

    'available'   — a provider ID is mapped AND it appears in settings.availableModels.
    'unavailable' — a provider ID is mapped AND it does NOT appear in settings.availableModels.
    'unchecked'   — availability cannot be proven (no mapping, no env, no usable list, or
                    availability enforcement is off entirely).
    """
    if not available_checked:
        return "unchecked"
    env_key = ALIAS_TO_SETTINGS_ENV.get(alias)
    if env_key is None:
        # fable (or any alias without an env mapping): availability cannot be proven.
        return "unchecked"
    env = settings.get("env")
    if not isinstance(env, dict):
        return "unchecked"
    provider_id = env.get(env_key)
    if not isinstance(provider_id, str) or not provider_id:
        return "unchecked"
    available_models = settings.get("availableModels")
    if not isinstance(available_models, list):
        return "unchecked"
    if provider_id in available_models:
        return "available"
    return "unavailable"


def resolve_role(role, alias_list, settings, available_checked, diagnostics):
    """Resolve a role to (chosen_alias, status, fallback_entry_or_None).

    Emits any fallback warning to stderr and appends diagnostics as needed.
    """
    if not available_checked:
        diagnostics.append(
            f"role '{role}': availability not enforced; returning first alias '{alias_list[0]}' "
            "unchecked (runtime will handle a spawn error with bounded fallback)."
        )
        return alias_list[0], "unchecked", None

    primary = alias_list[0]
    primary_avail = alias_availability(primary, settings, available_checked)

    if primary_avail == "available":
        return primary, "available", None
    if primary_avail == "unchecked":
        diagnostics.append(
            f"role '{role}': primary alias '{primary}' availability could not be proven "
            "(unchecked); returning it unchecked (runtime will handle a spawn error with "
            "bounded fallback)."
        )
        return primary, "unchecked", None

    # primary is proven unavailable — scan the rest for the first usable alias.
    missing_provider = provider_id_for(primary, settings)
    for alias in alias_list[1:]:
        avail = alias_availability(alias, settings, available_checked)
        if avail == "available":
            fallback_entry = {
                "role": role,
                "primary": primary,
                "missingModel": missing_provider,
                "fallback": alias,
            }
            warning = (
                f"WARNING: role '{role}' primary alias '{primary}'"
                + (f" (model {missing_provider})" if missing_provider else "")
                + f" is not available; falling back to '{alias}'."
            )
            print(warning, file=sys.stderr)
            diagnostics.append(warning)
            return alias, "fallback", fallback_entry
        if avail == "unchecked":
            # Primary is known-bad but this candidate cannot be proven available — advance to
            # it and defer to runtime rather than claiming a proven fallback.
            diagnostics.append(
                f"role '{role}': primary alias '{primary}' is proven unavailable"
                + (f" (model {missing_provider})" if missing_provider else "")
                + f"; advanced to alias '{alias}' whose availability is unchecked (runtime "
                "will handle a spawn error with bounded fallback)."
            )
            return alias, "unchecked", None
        # 'unavailable' — keep scanning.

    # Every alias in the list is proven unavailable.
    fail(
        f"role '{role}' has no available alias: every entry in {alias_list!r} is proven "
        f"unavailable under enforceAvailableModels. Change the '{role}' entry in "
        f"config/expert-review-models.json (or relax enforceAvailableModels)."
    )


def main():
    parser = argparse.ArgumentParser(
        prog="resolve-expert-review-models.py",
        description="Resolve expert-review model aliases to a concrete per-role selection.",
    )
    parser.add_argument(
        "--model",
        metavar="<alias>",
        help="Explicit panel model override (must be a known alias).",
    )
    args = parser.parse_args()

    registry_path = registry_path_for(__file__)
    data = load_registry(registry_path)
    aliases, roles = validate_registry(data, registry_path)
    alias_set = set(aliases)

    settings, read_ok = load_settings()
    enforce = bool(settings.get("enforceAvailableModels") is True) if settings else False
    available_checked = read_ok and enforce

    diagnostics = []
    if available_checked:
        env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        mapped = []
        for alias in aliases:
            pid = provider_id_for(alias, settings)
            if pid is not None:
                mapped.append(f"{alias}={pid}")
        if mapped:
            diagnostics.append(
                "enforceAvailableModels is on; checked provider IDs: " + ", ".join(mapped)
            )
        if not isinstance(settings.get("availableModels"), list):
            diagnostics.append(
                "enforceAvailableModels is on but 'availableModels' is missing or not a list; "
                "availability cannot be proven for any alias (all roles unchecked)."
            )
    else:
        diagnostics.append(
            "enforceAvailableModels is off or settings could not be read; availability is "
            "unchecked for every role (runtime handles spawn errors with bounded fallback)."
        )

    fallbacks = []
    resolved = {}
    status = {}

    for role in REQUIRED_ROLES:
        chosen, st, fb = resolve_role(
            role, roles[role], settings, available_checked, diagnostics
        )
        resolved[role] = chosen
        status[role] = st
        if fb is not None:
            fallbacks.append(fb)

    panel_override = False
    if args.model is not None:
        panel_override = True
        if args.model not in alias_set:
            fail(
                f"--model {args.model!r} is not a known alias. Known aliases: "
                f"{', '.join(aliases)}. (This also rejects raw provider model IDs — pass a "
                "semantic alias like 'sonnet'.)"
            )
        model_avail = alias_availability(args.model, settings, available_checked)
        if model_avail == "unavailable":
            fail(
                f"--model {args.model!r} is proven unavailable under enforceAvailableModels. "
                "An explicit user choice is never silently changed — pass an available alias "
                "or relax enforceAvailableModels."
            )
        if model_avail == "unchecked":
            diagnostics.append(
                f"--model '{args.model}' availability is unchecked; returning it for the panel "
                "(runtime will handle a spawn error with bounded fallback)."
            )
        resolved["panel"] = args.model
        status["panel"] = model_avail if model_avail != "unavailable" else "unchecked"
        # 'available' or 'unchecked' are the only reachable values here (unavailable failed above).

    output = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "configured": {role: roles[role] for role in REQUIRED_ROLES},
        "resolved": {role: resolved[role] for role in REQUIRED_ROLES},
        "status": {role: status[role] for role in REQUIRED_ROLES},
        "fallbacks": fallbacks,
        "panelOverride": panel_override,
        "availableChecked": available_checked,
        "diagnostics": diagnostics,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
