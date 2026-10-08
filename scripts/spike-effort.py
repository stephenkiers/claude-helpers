#!/usr/bin/env python3
"""Pick /expert-spike effort (2, 3, or 4) from question text, deterministically.

Usage: spike-effort.py <question-text-file> [--project-root DIR]

Prints one JSON line: {"effort": N, "reason": "...", "source": "heuristic"|"fallback"}.
Config cascade (key by key): built-in defaults < ~/.claude/spike-effort-heuristic.yaml
< <project-root>/.claude/spike-effort-heuristic.yaml. Never fails the caller: on any
error it prints the default effort 2 with the error in "reason" and source: "fallback".
Levels 1 and 5 are explicit-only and are never returned by this heuristic.
"""
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

DEFAULTS: Dict[str, Any] = {
    "default_effort": 2,
    "risk_keywords": [
        "auth", "payment", "migration", "migrate", "schema", "security", "secret",
        "token", "crypto", "permission", "password", "concurrency", "irreversible",
    ],
    "decision_cues": [
        "best way", "should we", "compare", "vs", "migrate", "replace",
        "architecture", "strategy",
    ],
    "long_question_chars": 600,
    "subsystem_min": 2,
}
CONFIG_NAME = "spike-effort-heuristic.yaml"
VALID_DEFAULT_EFFORTS = (2, 3, 4)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
PATH_STRIP = "\"'()[]{}<>`.,;:!?"


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


def _is_pos_int(v: Any) -> bool:
    return type(v) is int and v > 0


def load_config(project_root: Any = None, home: Any = None) -> Dict[str, Any]:
    """Load config with cascade: defaults < user-level < project-level.

    Validates: default_effort is int in (2, 3, 4); thresholds are positive ints;
    cue/keyword lists are lists. Warns on invalid values and keeps the prior value.
    """
    cfg = copy.deepcopy(DEFAULTS)
    home = Path(home) if home else Path.home()
    paths = [home / ".claude" / CONFIG_NAME]
    if project_root:
        paths.append(Path(project_root) / ".claude" / CONFIG_NAME)
    for p in paths:
        if not p.is_file():
            continue
        data = _load(p)
        if "default_effort" in data:
            v = data["default_effort"]
            if type(v) is int and v in VALID_DEFAULT_EFFORTS:
                cfg["default_effort"] = v
            else:
                print(
                    f"warning: {p}: default_effort must be int (2, 3, or 4), "
                    f"got {type(v).__name__}={v!r}; using default",
                    file=sys.stderr,
                )
        for key in ("risk_keywords", "decision_cues"):
            if key not in data:
                continue
            v = data[key]
            if isinstance(v, list):
                cfg[key] = [str(k).strip() for k in v if str(k).strip()]
            else:
                print(
                    f"warning: {p}: {key} must be a list, got {type(v).__name__}={v!r}; "
                    f"using default",
                    file=sys.stderr,
                )
        for key in ("long_question_chars", "subsystem_min"):
            if key not in data:
                continue
            v = data[key]
            if _is_pos_int(v):
                cfg[key] = v
            else:
                print(
                    f"warning: {p}: {key} must be positive int, "
                    f"got {type(v).__name__}={v!r}; using default",
                    file=sys.stderr,
                )
    return cfg


def _cue_regex(entry: str) -> "re.Pattern[str]":
    """Word-boundary start match; the exact cue 'vs' must match as a whole word."""
    if entry.lower() == "vs":
        return re.compile(r"\bvs\b", re.I)
    return re.compile(r"\b" + re.escape(entry), re.I)


def _matched_signals(text: str, cfg: Dict[str, Any]) -> List[str]:
    """Distinct (lowercased) cue/keyword entries that match; an entry in both lists counts once."""
    seen: Set[str] = set()
    out: List[str] = []
    for entry in list(cfg["decision_cues"]) + list(cfg["risk_keywords"]):
        key = entry.lower()
        if key in seen:
            continue
        if _cue_regex(entry).search(text):
            seen.add(key)
            out.append(key)
    return out


def _subsystem_tokens(text: str) -> List[str]:
    """Distinct backticked identifiers and path-like tokens (case-sensitive dedupe)."""
    seen: Set[str] = set()
    out: List[str] = []
    for m in BACKTICK_RE.finditer(text):
        tok = m.group(1)
        if tok.strip() and tok not in seen:
            seen.add(tok)
            out.append(tok)
    for raw in text.split():
        if "://" in raw:
            continue
        tok = raw.strip(PATH_STRIP)
        if "://" in tok or "/" not in tok or not any(c.isalnum() for c in tok):
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def decide(text: str, cfg: Dict[str, Any]) -> Tuple[int, str]:
    """Decide effort level from question text and config.

    Returns: (effort: 2, 3, or 4, reason: explanation naming the rule that fired).
    Never returns 1 or 5.
    """
    if not text.strip():
        return cfg["default_effort"], "empty question; default effort"
    signals = _matched_signals(text, cfg)
    if len(signals) >= 2:
        return 4, "multiple signals (%d): %s" % (len(signals), ", ".join(signals))
    if len(text) > cfg["long_question_chars"]:
        return 4, "long question (%d chars > %d)" % (len(text), cfg["long_question_chars"])
    subs = _subsystem_tokens(text)
    if len(subs) >= cfg["subsystem_min"]:
        return 4, "names %d subsystems (>= %d): %s" % (
            len(subs), cfg["subsystem_min"], ", ".join(subs[:5]),
        )
    if len(signals) == 1:
        return 3, "single signal: '%s'" % signals[0]
    return cfg["default_effort"], "no cues matched; %d chars" % len(text)


def _fallback(reason: str) -> None:
    print(json.dumps({"effort": 2, "reason": "heuristic unavailable (%s)" % reason, "source": "fallback"}))


def main(argv: list) -> None:
    """Main entry point. Parse args, decide effort, print JSON result.

    Never raises or returns non-zero; always prints valid JSON with source
    "heuristic" (on success) or "fallback" (on error).
    """
    args = argv[1:]
    root = None
    try:
        if "--project-root" in args:
            i = args.index("--project-root")
            if i + 1 >= len(args):
                _fallback("usage: spike-effort.py <question-text-file> [--project-root DIR]")
                return
            root = args[i + 1]
            del args[i:i + 2]
        if len(args) != 1:
            _fallback("usage: spike-effort.py <question-text-file> [--project-root DIR]")
            return
        text = Path(args[0]).read_text()
        effort, reason = decide(text, load_config(root))
        print(json.dumps({"effort": effort, "reason": reason, "source": "heuristic"}))
    except Exception as e:  # never block the spike on the heuristic
        _fallback(str(e))


if __name__ == "__main__":
    main(sys.argv)
