#!/usr/bin/env python3
"""Read and write a research spike's spike.json state file (the only writer of that file).

Usage:
    spike-manifest.py init --dir D --question Q --slug S --effort N
                           [--models balanced|opus] [--expert NAME ...] [--command-id ID]
    spike-manifest.py mark --dir D --stage X --status S
    spike-manifest.py expect --dir D --stage X --path REL [--path REL ...]
    spike-manifest.py show --dir D
    spike-manifest.py list --root R
    spike-manifest.py resolve --root R --ref REF
    spike-manifest.py add-command-id --dir D --command-id ID

Every subcommand prints JSON to stdout on success (exit 0). A ManifestError prints
"error: <msg>" to stderr and exits 2; argparse usage errors also exit 2.

The manifest is fail-closed: load_manifest() raises ManifestError rather than return a
partially-valid dict. Writes are atomic (temp file in the same directory + os.replace).
Resume points are derived from artifacts on disk, not from the status field alone.

Importable API (tests load this file with importlib, since the name is hyphenated):
init_manifest, load_manifest, mark_stage, add_command_id, expect_artifacts,
missing_artifacts, resume_point, list_spikes, resolve_spike, ManifestError, STAGES, STATUSES, STAGE_ARTIFACTS.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1
MANIFEST_NAME = "spike.json"
MODELS = ("balanced", "opus")
EFFORTS = (1, 2, 3, 4, 5)
SLUG_RE = re.compile(r"[a-z0-9-]{1,50}")
EXPERT_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

STAGES: Tuple[str, ...] = (
    "gather-context",
    "decompose",
    "codebase-survey",
    "refine-questions",
    "expert-questions",
    "checkpoint",
    "research",
    "gap-check",
    "research-wave-2",
    "expert-assessment",
    "synthesize",
    "audit",
    "present",
)
STATUSES: Tuple[str, ...] = ("pending", "running", "done", "failed", "skipped")

MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "question",
        "slug",
        "effort",
        "models",
        "experts",
        "created",
        "updated",
        "command_ids",
        "stages",
    }
)

# Optional manifest key: stage -> relative artifact paths that must exist for the stage to count
# as complete (e.g. one survey file per sub-question). Absent until `expect` records it.
OPTIONAL_KEYS = frozenset({"expected_artifacts"})

# stage -> list of (relative path or glob, sentinel or None).
# A sentinel counts as present when the file's last non-blank line, stripped, equals it.
STAGE_ARTIFACTS: Dict[str, List[Tuple[str, Optional[str]]]] = {
    "gather-context": [
        (MANIFEST_NAME, None),
        ("question.md", None),
        ("README.md", None),
    ],
    "decompose": [("questions.md", None)],
    "codebase-survey": [("survey/*.md", "<!-- survey-end -->")],
    "refine-questions": [
        ("questions.md", None),
        ("knowledge/findings.md", None),
    ],
    "expert-questions": [("experts/*-questions.md", "<!-- spike-questions-end -->")],
    "checkpoint": [("decisions.md", None)],
    "research": [("research/*.md", "<!-- research-end -->")],
    "gap-check": [
        ("knowledge/findings.md", None),
        ("knowledge/sources.md", None),
    ],
    "research-wave-2": [("research/*.md", "<!-- research-end -->")],
    "expert-assessment": [("experts/*-assessment.md", "<!-- spike-assessment-end -->")],
    "synthesize": [("synthesis.md", None)],
    "audit": [("audit.md", None)],
    "present": [("README.md", None)],
}


class ManifestError(Exception):
    """Raised for any invalid, missing, or unsafe manifest operation."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _check_str_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{field} must be a list of strings")


def _check_relative_path(rel: str, where: str) -> None:
    """Raise ManifestError unless rel is a non-empty relative path with no '..' or absolute parts."""
    parts = Path(rel).parts
    if not rel.strip() or Path(rel).is_absolute() or ".." in parts or not parts:
        raise ManifestError(f"{where}: artifact path {rel!r} must be relative and stay inside the spike dir")


def _validate_manifest(data: Any, where: str) -> Dict[str, Any]:
    """Return data if it is a fully-valid manifest; otherwise raise ManifestError."""
    if not isinstance(data, dict):
        raise ManifestError(f"{where}: manifest must be a JSON object")
    if "schema_version" not in data:
        raise ManifestError(f"{where}: missing schema_version")
    if not _is_int(data["schema_version"]) or data["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(
            f"{where}: unsupported schema_version {data['schema_version']!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    missing = sorted(MANIFEST_KEYS - set(data))
    if missing:
        raise ManifestError(f"{where}: missing keys: {', '.join(missing)}")
    extra = sorted(set(data) - MANIFEST_KEYS - OPTIONAL_KEYS)
    if extra:
        raise ManifestError(f"{where}: unexpected keys: {', '.join(extra)}")

    if not isinstance(data["question"], str):
        raise ManifestError(f"{where}: question must be a string")
    if not isinstance(data["slug"], str) or not SLUG_RE.fullmatch(data["slug"]):
        raise ManifestError(f"{where}: slug must match [a-z0-9-] (1-50 chars), got {data['slug']!r}")
    if not _is_int(data["effort"]) or data["effort"] not in EFFORTS:
        raise ManifestError(f"{where}: effort must be an int 1-5, got {data['effort']!r}")
    if data["models"] not in MODELS:
        raise ManifestError(f"{where}: models must be one of {MODELS}, got {data['models']!r}")
    _check_str_list(data["experts"], "experts")
    for name in data["experts"]:
        if not EXPERT_RE.fullmatch(name):
            raise ManifestError(f"{where}: expert name {name!r} must match [a-z0-9][a-z0-9-]*")
    if not isinstance(data["created"], str):
        raise ManifestError(f"{where}: created must be a string")
    if not isinstance(data["updated"], str):
        raise ManifestError(f"{where}: updated must be a string")
    _check_str_list(data["command_ids"], "command_ids")

    if "expected_artifacts" in data:
        expected = data["expected_artifacts"]
        if not isinstance(expected, dict):
            raise ManifestError(f"{where}: expected_artifacts must be an object")
        for stage_name, paths in expected.items():
            if stage_name not in STAGE_ARTIFACTS:
                raise ManifestError(f"{where}: expected_artifacts has unknown stage {stage_name!r}")
            _check_str_list(paths, f"expected_artifacts[{stage_name}]")
            for rel in paths:
                _check_relative_path(rel, where)

    stages = data["stages"]
    if not isinstance(stages, dict):
        raise ManifestError(f"{where}: stages must be an object")
    stage_missing = [s for s in STAGES if s not in stages]
    if stage_missing:
        raise ManifestError(f"{where}: missing stages: {', '.join(stage_missing)}")
    stage_extra = sorted(set(stages) - set(STAGES))
    if stage_extra:
        raise ManifestError(f"{where}: unknown stages: {', '.join(stage_extra)}")
    for name in STAGES:
        status = stages[name]
        if status not in STATUSES:
            raise ManifestError(
                f"{where}: stage {name} has invalid status {status!r} (expected one of {STATUSES})"
            )
    return data


def _manifest_path(spike_dir: Any) -> Path:
    return Path(spike_dir) / MANIFEST_NAME


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a completed rename survives a crash."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_atomic(spike_dir: Path, data: Dict[str, Any], exclusive: bool = False) -> None:
    """Write data as spike.json: temp file in the same dir, fsync, then rename into place.

    exclusive=True uses os.link so an existing spike.json is never replaced (init's guarantee).
    """
    target = spike_dir / MANIFEST_NAME
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(spike_dir), prefix=MANIFEST_NAME + ".")
    except OSError as e:
        raise ManifestError(f"cannot write manifest in {spike_dir}: {e}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_path, 0o666 & ~umask)
        if exclusive:
            os.link(tmp_path, target)
        else:
            os.replace(tmp_path, target)
        _fsync_dir(spike_dir)
    except FileExistsError:
        raise ManifestError(f"{target} already exists; refusing to overwrite")
    except OSError as e:
        raise ManifestError(f"failed to write {target}: {e}")
    finally:
        _unlink_quiet(tmp_path)


def init_manifest(
    spike_dir: Any,
    question: str,
    slug: str,
    effort: int,
    models: str = "balanced",
    experts: Iterable[str] = (),
    command_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create spike_dir (if needed) and a fresh spike.json with every stage pending."""
    if not isinstance(question, str) or not question.strip():
        raise ManifestError("question must be a non-empty string")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ManifestError(f"slug must match [a-z0-9-] (1-50 chars), got {slug!r}")
    if not _is_int(effort) or effort not in EFFORTS:
        raise ManifestError(f"effort must be an int 1-5, got {effort!r}")
    if models not in MODELS:
        raise ManifestError(f"models must be one of {MODELS}, got {models!r}")
    if isinstance(experts, str):
        raise ManifestError("experts must be a sequence of names, not a string")
    expert_list = list(experts)
    for name in expert_list:
        if not isinstance(name, str) or not EXPERT_RE.fullmatch(name):
            raise ManifestError(f"expert name {name!r} must match [a-z0-9][a-z0-9-]*")
    if command_id is not None and (not isinstance(command_id, str) or not command_id.strip()):
        raise ManifestError("command_id must be a non-empty string when given")

    spike_path = Path(spike_dir)
    try:
        spike_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ManifestError(f"cannot create spike dir {spike_path}: {e}")
    if _manifest_path(spike_path).exists():
        raise ManifestError(f"{_manifest_path(spike_path)} already exists; refusing to overwrite")

    now = _now()
    data: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "slug": slug,
        "effort": effort,
        "models": models,
        "experts": expert_list,
        "created": now,
        "updated": now,
        "command_ids": [command_id] if command_id is not None else [],
        "stages": {name: "pending" for name in STAGES},
    }
    _write_atomic(spike_path, data, exclusive=True)
    return data


def load_manifest(spike_dir: Any) -> Dict[str, Any]:
    """Load and fully validate spike.json. Fails closed with ManifestError."""
    path = _manifest_path(spike_dir)
    try:
        present = path.is_file()
    except OSError as e:
        raise ManifestError(f"cannot read {path}: {e}")
    if not present:
        raise ManifestError(f"no {MANIFEST_NAME} in {spike_dir}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ManifestError(f"cannot read {path}: {e}")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ManifestError(f"invalid JSON in {path}: {e}")
    return _validate_manifest(data, str(path))


def mark_stage(spike_dir: Any, stage: str, status: str) -> Dict[str, Any]:
    """Set one stage's status. Any transition between valid statuses is allowed."""
    if stage not in STAGES:
        raise ManifestError(f"unknown stage {stage!r}")
    if status not in STATUSES:
        raise ManifestError(f"unknown status {status!r} (expected one of {STATUSES})")
    data = load_manifest(spike_dir)
    data["stages"][stage] = status
    data["updated"] = _now()
    _write_atomic(Path(spike_dir), data)
    return data


def add_command_id(spike_dir: Any, command_id: str) -> Dict[str, Any]:
    """Append command_id unless it is empty or already the last entry."""
    if not isinstance(command_id, str):
        raise ManifestError("command_id must be a string")
    data = load_manifest(spike_dir)
    if command_id.strip() and (not data["command_ids"] or data["command_ids"][-1] != command_id):
        data["command_ids"].append(command_id)
        data["updated"] = _now()
        _write_atomic(Path(spike_dir), data)
    return data


def expect_artifacts(spike_dir: Any, stage: str, paths: Iterable[str]) -> Dict[str, Any]:
    """Record the artifact paths stage must produce; resume treats any missing one as incomplete."""
    if stage not in STAGE_ARTIFACTS:
        raise ManifestError(f"unknown stage {stage!r}")
    if isinstance(paths, str):
        raise ManifestError("paths must be a sequence of relative paths, not a string")
    path_list = sorted(set(paths))
    for rel in path_list:
        if not isinstance(rel, str):
            raise ManifestError(f"artifact path {rel!r} must be a string")
        _check_relative_path(rel, "expect")
    data = load_manifest(spike_dir)
    data.setdefault("expected_artifacts", {})[stage] = path_list
    data["updated"] = _now()
    _write_atomic(Path(spike_dir), data)
    return data


def _is_safe_file(root: Path, path: Path) -> bool:
    """True for a regular file that is not a symlink and does not resolve outside root."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _content_ok(path: Path, sentinel: Optional[str]) -> bool:
    """True when the file is readable, non-blank, and (if given) ends with the sentinel line."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if sentinel is None:
        return text.strip() != ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == sentinel


def missing_artifacts(spike_dir: Any, stage: str, expected: Iterable[str] = ()) -> List[str]:
    """Relative paths/globs of expectations for stage that are not satisfied on disk.

    expected: extra relative paths recorded via expect_artifacts; each must be a safe,
    non-blank file. Symlinks never satisfy an expectation.
    """
    if stage not in STAGE_ARTIFACTS:
        raise ManifestError(f"unknown stage {stage!r}")
    root = Path(spike_dir)
    missing: List[str] = []
    for pattern, sentinel in STAGE_ARTIFACTS[stage]:
        if any(ch in pattern for ch in "*?["):
            matches = sorted(p for p in root.glob(pattern) if _is_safe_file(root, p))
            if not matches:
                missing.append(pattern)
                continue
            for match in matches:
                if not _content_ok(match, sentinel):
                    missing.append(match.relative_to(root).as_posix())
        else:
            target = root / pattern
            if not _is_safe_file(root, target) or not _content_ok(target, sentinel):
                missing.append(pattern)
    for rel in expected:
        target = root / rel
        if not _is_safe_file(root, target) or not _content_ok(target, None):
            if rel not in missing:
                missing.append(rel)
    return missing


def resume_point(spike_dir: Any) -> Optional[str]:
    """First stage that is not complete, derived from manifest plus disk. None when all are."""
    data = load_manifest(spike_dir)
    for stage in STAGES:
        status = data["stages"][stage]
        if status == "skipped":
            continue
        expected = data.get("expected_artifacts", {}).get(stage, [])
        if status == "done" and not missing_artifacts(spike_dir, stage, expected):
            continue
        return stage
    return None


def _spike_subdirs(root: Path) -> List[Path]:
    """Immediate subdirectories of root that contain a spike.json, sorted by name."""
    try:
        if not root.is_dir():
            return []
        children = sorted(root.iterdir())
    except OSError as e:
        raise ManifestError(f"cannot list {root}: {e}")
    found: List[Path] = []
    for child in children:
        try:
            if child.is_dir() and (child / MANIFEST_NAME).is_file():
                found.append(child)
        except OSError:
            found.append(child)  # unreadable entry: list_spikes reports it as malformed
    return found


def list_spikes(root: Any) -> List[Dict[str, Any]]:
    """Summarize every spike under root. Malformed manifests are reported, never fatal."""
    good: List[Dict[str, Any]] = []
    malformed: List[Dict[str, Any]] = []
    for child in _spike_subdirs(Path(root)):
        base: Dict[str, Any] = {"dir": str(child.resolve()), "name": child.name}
        try:
            data = load_manifest(child)
            rp = resume_point(child)
        except ManifestError as e:
            malformed.append(
                dict(
                    base,
                    question=None,
                    status="malformed",
                    last_stage=None,
                    resume_point=None,
                    updated=None,
                    error=str(e),
                )
            )
            continue
        last_stage = None
        for stage in STAGES:
            if data["stages"][stage] in ("done", "skipped"):
                last_stage = stage
        good.append(
            dict(
                base,
                question=data["question"],
                status="complete" if rp is None else "in-progress",
                last_stage=last_stage,
                resume_point=rp,
                updated=data["updated"],
            )
        )
    good.sort(key=lambda e: e["name"])
    good.sort(key=lambda e: e["updated"], reverse=True)
    malformed.sort(key=lambda e: e["name"])
    return good + malformed


def resolve_spike(root: Any, ref: str) -> str:
    """Resolve a --resume reference to an absolute spike dir. Fails closed on ambiguity."""
    if not isinstance(ref, str) or not ref.strip():
        raise ManifestError("resume reference must be a non-empty string")
    direct = Path(ref).expanduser()
    if direct.is_dir() and (direct / MANIFEST_NAME).is_file():
        return str(direct.resolve())
    candidates = _spike_subdirs(Path(root))
    for child in candidates:
        if child.name == ref:
            return str(child.resolve())
    matches = [child for child in candidates if child.name.startswith(ref)]
    if not matches:
        raise ManifestError(f"no spike matches {ref!r} under {root}")
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise ManifestError(f"ambiguous reference {ref!r}; matches: {names}")
    return str(matches[0].resolve())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spike-manifest.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    p_init = sub.add_parser("init", help="create a spike dir and spike.json")
    p_init.add_argument("--dir", required=True)
    p_init.add_argument("--question", required=True)
    p_init.add_argument("--slug", required=True)
    p_init.add_argument("--effort", required=True, type=int)
    p_init.add_argument("--models", default="balanced", choices=MODELS)
    p_init.add_argument("--expert", action="append", default=None, dest="experts")
    p_init.add_argument("--command-id", default=None, dest="command_id")

    p_mark = sub.add_parser("mark", help="set one stage's status")
    p_mark.add_argument("--dir", required=True)
    p_mark.add_argument("--stage", required=True)
    p_mark.add_argument("--status", required=True)

    p_expect = sub.add_parser("expect", help="record artifact paths a stage must produce")
    p_expect.add_argument("--dir", required=True)
    p_expect.add_argument("--stage", required=True)
    p_expect.add_argument("--path", action="append", default=None, dest="paths")

    p_show = sub.add_parser("show", help="print the manifest plus resume_point")
    p_show.add_argument("--dir", required=True)

    p_list = sub.add_parser("list", help="summarize spikes under a root")
    p_list.add_argument("--root", required=True)

    p_resolve = sub.add_parser("resolve", help="resolve a --resume reference to a dir")
    p_resolve.add_argument("--root", required=True)
    p_resolve.add_argument("--ref", required=True)

    p_add = sub.add_parser("add-command-id", help="append a telemetry command id")
    p_add.add_argument("--dir", required=True)
    p_add.add_argument("--command-id", required=True, dest="command_id")
    return parser


def _run(args: argparse.Namespace) -> Any:
    if args.command == "init":
        return init_manifest(
            args.dir,
            args.question,
            args.slug,
            args.effort,
            models=args.models,
            experts=args.experts or [],
            command_id=args.command_id,
        )
    if args.command == "mark":
        return mark_stage(args.dir, args.stage, args.status)
    if args.command == "expect":
        return expect_artifacts(args.dir, args.stage, args.paths or [])
    if args.command == "show":
        data = load_manifest(args.dir)
        data["resume_point"] = resume_point(args.dir)
        return data
    if args.command == "list":
        return list_spikes(args.root)
    if args.command == "resolve":
        return {"dir": resolve_spike(args.root, args.ref)}
    if args.command == "add-command-id":
        return add_command_id(args.dir, args.command_id)
    raise ManifestError(f"unknown command {args.command!r}")


def main(argv: List[str]) -> int:
    """Entry point. argv is sys.argv (argv[0] is the program name)."""
    parser = _build_parser()
    args = parser.parse_args(argv[1:])
    try:
        result = _run(args)
    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
