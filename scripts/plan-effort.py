#!/usr/bin/env python3
"""Pick /expert-plan effort (2 or 3) from ticket text, deterministically.

Usage: plan-effort.py <ticket-text-file> [--project-root DIR]

Prints one JSON line: {"effort": 2|3, "reason": "...", "source": "heuristic"}.
Config cascade (key by key): built-in defaults < ~/.claude/plan-effort-heuristic.yaml
< <project-root>/.claude/plan-effort-heuristic.yaml. Never fails the caller: on any
error it prints the default effort 2 with the error in "reason".
"""
import json
import re
import sys
from pathlib import Path

DEFAULTS = {
    "default_effort": 2,
    "size_thresholds": {"body_chars": 3000, "task_items": 12},
    "risk_keywords": [
        "auth", "payment", "migration", "migrate", "schema", "security", "secret",
        "token", "crypto", "permission", "password", "concurrency", "irreversible",
    ],
}
CONFIG_NAME = "plan-effort-heuristic.yaml"
ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?\S", re.M)


def _load(path):
    try:
        import yaml
        data = yaml.safe_load(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_config(project_root=None, home=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    home = Path(home) if home else Path.home()
    paths = [home / ".claude" / CONFIG_NAME]
    if project_root:
        paths.append(Path(project_root) / ".claude" / CONFIG_NAME)
    for p in paths:
        if not p.is_file():
            continue
        data = _load(p)
        if data.get("default_effort") in (2, 3):
            cfg["default_effort"] = data["default_effort"]
        if isinstance(data.get("size_thresholds"), dict):
            for k in cfg["size_thresholds"]:
                if isinstance(data["size_thresholds"].get(k), int):
                    cfg["size_thresholds"][k] = data["size_thresholds"][k]
        if isinstance(data.get("risk_keywords"), list):
            cfg["risk_keywords"] = [str(k) for k in data["risk_keywords"] if str(k).strip()]
    return cfg


def decide(text, cfg):
    for kw in cfg["risk_keywords"]:
        if re.search(r"\b" + re.escape(kw), text, re.I):
            return 3, "risk keyword '%s' in ticket" % kw
    th = cfg["size_thresholds"]
    if len(text) >= th["body_chars"]:
        return 3, "ticket is %d chars (>= %d)" % (len(text), th["body_chars"])
    items = len(ITEM_RE.findall(text))
    if items >= th["task_items"]:
        return 3, "ticket has %d task items (>= %d)" % (items, th["task_items"])
    return cfg["default_effort"], "no risk keywords; %d chars, %d task items" % (len(text), items)


def main(argv):
    root = None
    args = argv[1:]
    if "--project-root" in args:
        i = args.index("--project-root")
        root = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    try:
        text = Path(args[0]).read_text()
        effort, reason = decide(text, load_config(root))
    except Exception as e:  # never block planning on the heuristic
        effort, reason = 2, "heuristic failed (%s); using default" % e
    print(json.dumps({"effort": effort, "reason": reason, "source": "heuristic"}))


if __name__ == "__main__":
    main(sys.argv)
