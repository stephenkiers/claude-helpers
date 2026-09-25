#!/usr/bin/env python3
"""Pick /expert-plan effort (2 or 3) from ticket text, deterministically.

Usage: plan-effort.py <ticket-text-file> [--project-root DIR]

Prints one JSON line: {"effort": 2|3, "reason": "...", "source": "heuristic"|"fallback"}.
Config cascade (key by key): built-in defaults < ~/.claude/plan-effort-heuristic.yaml
< <project-root>/.claude/plan-effort-heuristic.yaml. Never fails the caller: on any
error it prints the default effort 2 with the error in "reason" and source: "fallback".
"""
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Literal, Tuple

DEFAULTS: Dict[str, Any] = {
    "default_effort": 2,
    "size_thresholds": {"body_chars": 3000, "task_items": 12},
    "risk_keywords": [
        "auth", "payment", "migration", "migrate", "schema", "security", "secret",
        "token", "crypto", "permission", "password", "concurrency", "irreversible",
    ],
}
CONFIG_NAME = "plan-effort-heuristic.yaml"
ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?\S", re.M)


def _load(path: Path) -> Dict[str, Any]:
    """Load YAML config file, surfacing parse errors to stderr."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print("warning: PyYAML not available; using defaults", file=sys.stderr)
        return {}
    try:
        data = yaml.safe_load(path.read_text())  # type: ignore[attr-defined]
    except yaml.YAMLError as e:  # type: ignore[union-attr]
        print(f"warning: failed to parse {path}: {e}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"warning: failed to read {path}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def load_config(project_root: Any = None, home: Any = None) -> Dict[str, Any]:
    """Load config with cascade: defaults < user-level < project-level.

    Validates: default_effort is int in (2, 3); thresholds are positive ints.
    Warns on invalid values and skips them.
    """
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    home = Path(home) if home else Path.home()
    paths = [home / ".claude" / CONFIG_NAME]
    if project_root:
        paths.append(Path(project_root) / ".claude" / CONFIG_NAME)
    for p in paths:
        if not p.is_file():
            continue
        data = _load(p)
        # Validate and apply default_effort
        if "default_effort" in data:
            if type(data["default_effort"]) is int and data["default_effort"] in (2, 3):
                cfg["default_effort"] = data["default_effort"]
            else:
                print(
                    f"warning: {p}: default_effort must be int (2 or 3), "
                    f"got {type(data['default_effort']).__name__}={data['default_effort']!r}; "
                    f"using default",
                    file=sys.stderr,
                )
        # Validate and apply size_thresholds
        if isinstance(data.get("size_thresholds"), dict):
            for k in cfg["size_thresholds"]:
                if k in data["size_thresholds"]:
                    v = data["size_thresholds"][k]
                    if type(v) is int and v > 0:
                        cfg["size_thresholds"][k] = v
                    else:
                        print(
                            f"warning: {p}: size_thresholds[{k}] must be positive int, "
                            f"got {type(v).__name__}={v!r}; using default",
                            file=sys.stderr,
                        )
        if isinstance(data.get("risk_keywords"), list):
            cfg["risk_keywords"] = [str(k) for k in data["risk_keywords"] if str(k).strip()]
    return cfg


def decide(text: str, cfg: Dict[str, Any]) -> Tuple[Literal[2, 3], str]:
    """Decide effort level from ticket text and config.

    Returns: (effort: 2 or 3, reason: explanation string)
    """
    for kw in cfg["risk_keywords"]:
        if re.search(r"\b" + re.escape(kw), text, re.I):
            return 3, "risk keyword '%s' in ticket" % kw
    th = cfg["size_thresholds"]
    if len(text) >= th["body_chars"]:
        return 3, "ticket is %d chars (>= %d)" % (len(text), th["body_chars"])
    items = len(ITEM_RE.findall(text))
    if items >= th["task_items"]:
        return 3, "ticket has %d task items (>= %d)" % (items, th["task_items"])
    return cfg["default_effort"], "no keywords matched; %d chars, %d task items" % (len(text), items)


def main(argv: list) -> None:
    """Main entry point. Parse args, decide effort, print JSON result.

    Never raises or returns non-zero; always prints valid JSON with source
    "heuristic" (on success) or "fallback" (on error).
    """
    root = None
    args = argv[1:]

    # Parse --project-root flag
    if "--project-root" in args:
        i = args.index("--project-root")
        root = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]

    # Validate args
    if not args:
        print(json.dumps({"effort": 2, "reason": "heuristic unavailable (usage: plan-effort.py <ticket-text-file> [--project-root DIR])", "source": "fallback"}))
        return

    try:
        text = Path(args[0]).read_text()
        effort, reason = decide(text, load_config(root))
        print(json.dumps({"effort": effort, "reason": reason, "source": "heuristic"}))
    except Exception as e:  # never block planning on the heuristic
        reason = "heuristic unavailable (%s)" % e
        print(json.dumps({"effort": 2, "reason": reason, "source": "fallback"}))


if __name__ == "__main__":
    main(sys.argv)
