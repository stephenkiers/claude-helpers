"""
A repo's own Flow v3 process: `.claude/flow.yaml` (PLAN.md section 9), plus init-time detection and the
doctor that proves the file works against the untouched base before any ticket runs.
"""

import hashlib
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml  # type: ignore[import-untyped]

from scripts.flow3 import checks, config, gitops
from scripts.flow3.paths import repo_dir
from scripts.workflow.fsutil import atomic_write_text, read_json_object

CONFIG_RELPATH = ".claude/flow.yaml"
_MODELS = ("haiku", "sonnet", "opus", "fable")
_PROVIDERS = ("github", "local")
_TOP_KEYS = ("base", "provider", "install", "check", "merge", "protected_paths", "principles",
             "budget", "models")
_CHECK_KEYS = ("fast", "full", "timeout_secs")
_MERGE_KEYS = ("method", "command", "pre_merge", "post_merge")
_BUDGET_KEYS = ("soft_usd", "hard_usd")
DEFAULT_PRINCIPLES = "docs/principles.md"
DEFAULT_TIMEOUT_SECS = 900


class ConfigError(Exception):
    """The repo's flow.yaml is missing or invalid. The message names the bad key."""


@dataclass(frozen=True)
class RepoConfig:
    base: str
    provider: str
    install: Optional[str]
    check_fast: str
    check_full: str
    check_timeout_secs: int
    merge_method: str
    merge_command: Optional[str]
    pre_merge: Tuple[str, ...]
    post_merge: Tuple[str, ...]
    protected_paths: Tuple[str, ...]
    principles: str
    soft_usd: float
    hard_usd: float
    models: Dict[str, str]


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    lines: Tuple[str, ...]


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------

def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("%s: expected a mapping" % where)
    return value


def _no_unknown(d: Dict[str, Any], allowed: Tuple[str, ...], prefix: str) -> None:
    for key in d:
        if key not in allowed:
            raise ConfigError("unknown key %s%s (allowed: %s)" % (prefix, key, ", ".join(allowed)))


def _string(d: Dict[str, Any], key: str, where: str, default: Optional[str]) -> Optional[str]:
    if key not in d or d[key] is None:
        return default
    value = d[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s: expected a non-empty string" % where)
    return value.strip()


def _string_list(d: Dict[str, Any], key: str, where: str) -> Tuple[str, ...]:
    value = d.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError("%s: expected a list of non-empty strings" % where)
    return tuple(v.strip() for v in value)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError("%s: expected a positive number" % where)
    return float(value)


def parse(text: str) -> RepoConfig:
    """Parse flow.yaml text. Raises ConfigError naming the bad key."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError("not valid YAML: %s" % exc)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("the file must be a mapping of keys")
    _no_unknown(raw, _TOP_KEYS, "")

    base = _string(raw, "base", "base", "main") or "main"
    provider = _string(raw, "provider", "provider", "github") or "github"
    if provider not in _PROVIDERS:
        raise ConfigError("provider: must be one of %s, got %r" % ("/".join(_PROVIDERS), provider))
    install = _string(raw, "install", "install", None)

    chk = _mapping(raw.get("check"), "check")
    _no_unknown(chk, _CHECK_KEYS, "check.")
    full = _string(chk, "full", "check.full", None)
    if full is None:
        raise ConfigError("check.full: required (the command that proves the repo is green)")
    fast = _string(chk, "fast", "check.fast", full) or full
    timeout: Any = chk.get("timeout_secs", DEFAULT_TIMEOUT_SECS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError("check.timeout_secs: expected a positive integer")

    mrg = _mapping(raw.get("merge"), "merge")
    _no_unknown(mrg, _MERGE_KEYS, "merge.")
    method = _string(mrg, "method", "merge.method", "squash") or "squash"
    if method not in ("squash", "command"):
        raise ConfigError("merge.method: must be squash or command, got %r" % method)
    command = _string(mrg, "command", "merge.command", None)
    if method == "command" and command is None:
        raise ConfigError("merge.command: required when merge.method is command")
    pre = _string_list(mrg, "pre_merge", "merge.pre_merge")
    post = _string_list(mrg, "post_merge", "merge.post_merge")

    protected = _string_list(raw, "protected_paths", "protected_paths")
    principles = _string(raw, "principles", "principles", DEFAULT_PRINCIPLES) or DEFAULT_PRINCIPLES

    bud = _mapping(raw.get("budget"), "budget")
    _no_unknown(bud, _BUDGET_KEYS, "budget.")
    soft = _number(bud["soft_usd"], "budget.soft_usd") if "soft_usd" in bud else config.SOFT_BUDGET_USD
    hard = _number(bud["hard_usd"], "budget.hard_usd") if "hard_usd" in bud else config.HARD_BUDGET_USD
    if soft > hard:
        raise ConfigError("budget.soft_usd: must not exceed budget.hard_usd")

    mdl = _mapping(raw.get("models"), "models")
    models: Dict[str, str] = {}
    for role, model in mdl.items():
        if not isinstance(role, str) or (role != "*" and role not in config.MODELS):
            raise ConfigError("models.%s: unknown role" % (role,))
        if model not in _MODELS:
            raise ConfigError("models.%s: must be one of %s, got %r" % (role, "/".join(_MODELS), model))
        models[role] = model

    return RepoConfig(base=base, provider=provider, install=install, check_fast=fast, check_full=full,
                      check_timeout_secs=timeout, merge_method=method, merge_command=command,
                      pre_merge=pre, post_merge=post, protected_paths=protected,
                      principles=principles, soft_usd=soft, hard_usd=hard, models=models)


def _config_file(repo_root: Path) -> Path:
    return repo_root / CONFIG_RELPATH


def load(repo_root: Path) -> RepoConfig:
    """Read <repo_root>/.claude/flow.yaml. Raises ConfigError when it is missing or invalid."""
    path = _config_file(repo_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError("%s not found (run `flow3 init`)" % CONFIG_RELPATH)
    return parse(text)


def config_hash(repo_root: Path) -> str:
    """sha256 of the flow.yaml bytes."""
    return hashlib.sha256(_config_file(repo_root).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------
# Repo name and detection
# ---------------------------------------------------------------------------------------------

def _git_out(repo_root: Path, args: List[str]) -> str:
    try:
        return gitops.git(args, repo_root, check=False)
    except gitops.GitError:
        return ""


def _safe_component(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip(".-")
    return cleaned


def repo_name(repo_root: Path) -> str:
    """
    The repo's name for state directories: the last path component of the origin URL without `.git`,
    else the directory name. Always a safe single path component.
    """
    url = _git_out(repo_root, ["remote", "get-url", "origin"]).strip().rstrip("/")
    if url:
        last = re.split(r"[/:\\]", url)[-1]
        if last.endswith(".git"):
            last = last[:-4]
        name = _safe_component(last)
        if name:
            return name
    name = _safe_component(repo_root.resolve().name)
    return name or "repo"


def _recipes(text: str) -> List[str]:
    return re.findall(r"^([A-Za-z_][A-Za-z0-9_-]*)\b[^:=\n]*:(?!=)", text, re.MULTILINE)


def _detect_check(repo_root: Path) -> Dict[str, str]:
    for name in ("justfile", "Justfile", ".justfile"):
        jf = repo_root / name
        if jf.is_file():
            recipes = _recipes(jf.read_text(encoding="utf-8", errors="replace"))
            if "check" in recipes:
                found = {"full": "just check"}
                found["fast"] = "just check-minimum" if "check-minimum" in recipes else "just check"
                return found
    pkg = repo_root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for key in ("check", "test"):
                if key in scripts:
                    cmd = "npm run " + key
                    return {"full": cmd, "fast": cmd}
    for name in ("Makefile", "makefile", "GNUmakefile"):
        mk = repo_root / name
        if mk.is_file():
            targets = re.findall(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(?!=)",
                                 mk.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
            for key in ("check", "test"):
                if key in targets:
                    cmd = "make " + key
                    return {"full": cmd, "fast": cmd}
    return {}


def detect(repo_root: Path) -> Dict[str, Any]:
    """
    What `flow3 init` can work out alone, in the same nested shape `render` takes:
    {"base", "provider", "install" (or None), "check": {"fast", "full"} (empty when nothing found)}.
    """
    head = _git_out(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    base = head.split("/", 1)[1] if "/" in head else ""
    if not base:
        base = _git_out(repo_root, ["branch", "--show-current"]) or "main"
    origin = _git_out(repo_root, ["remote", "get-url", "origin"])
    provider = "github" if "github.com" in origin else "local"
    install: Optional[str] = None
    if (repo_root / "package-lock.json").is_file():
        install = "npm ci"
    elif (repo_root / "requirements.txt").is_file():
        install = "pip install -r requirements.txt"
    return {"base": base, "provider": provider, "install": install, "check": _detect_check(repo_root)}


def _q(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(value)


def _qlist(values: Any) -> str:
    return "[" + ", ".join(_q(str(v)) for v in values) + "]"


def render(values: Dict[str, Any]) -> str:
    """
    The flow.yaml text for `values` (the shape `detect` returns, plus optional `merge`,
    `protected_paths`, `principles`, `budget`, `models`). Missing keys get their defaults.
    """
    chk = values.get("check") or {}
    mrg = values.get("merge") or {}
    bud = values.get("budget") or {}
    full = chk.get("full")
    fast = chk.get("fast") or full
    lines = [
        "# Flow v3: how this repo is built, checked, and merged. Committed; read by `flow3`.",
        "# Run `flow3 doctor` after editing: it proves these commands work on a clean base.",
        "",
        "# The branch tickets start from and merge into.",
        "base: %s" % _q(str(values.get("base") or "main")),
        "# github (uses gh) or local (a bare remote; for tests).",
        "provider: %s" % _q(str(values.get("provider") or "github")),
    ]
    if values.get("install"):
        lines += ["# Run in a fresh worktree before checks.", "install: %s" % _q(str(values["install"]))]
    else:
        lines += ["# install: <command run in a fresh worktree before checks>"]
    lines += ["", "check:"]
    if full:
        lines += ["  # fast runs mid-flow; full runs at merge and is the authority.",
                  "  fast: %s" % _q(str(fast)), "  full: %s" % _q(str(full))]
    else:
        lines += ["  # full: <command that exits 0 only when the repo is green>   (required)"]
    lines += ["  timeout_secs: %d" % int(chk.get("timeout_secs") or DEFAULT_TIMEOUT_SECS), "", "merge:",
              "  # squash pins the head commit; command runs your own merge procedure.",
              "  method: %s" % _q(str(mrg.get("method") or "squash"))]
    if mrg.get("command"):
        lines += ["  command: %s" % _q(str(mrg["command"]))]
    lines += ["  pre_merge: %s" % _qlist(mrg.get("pre_merge") or []),
              "  post_merge: %s" % _qlist(mrg.get("post_merge") or []),
              "",
              "# Paths a ticket may not edit without a risk question (migrations, lockfiles, ...).",
              "protected_paths: %s" % _qlist(values.get("protected_paths") or []),
              "principles: %s" % _q(str(values.get("principles") or DEFAULT_PRINCIPLES)),
              "budget:",
              "  soft_usd: %s" % (bud.get("soft_usd") or config.SOFT_BUDGET_USD),
              "  hard_usd: %s" % (bud.get("hard_usd") or config.HARD_BUDGET_USD),
              "# role -> haiku | sonnet | opus | fable; \"*\" sets every role.",
              ]
    models = values.get("models") or {}
    if models:
        lines += ["models:"] + ["  %s: %s" % (_q(str(k)), _q(str(v))) for k, v in sorted(models.items())]
    else:
        lines += ["models: {}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------------------------

def _last_line(text: str) -> str:
    rows = [r for r in text.strip().splitlines() if r.strip()]
    return rows[-1][:200] if rows else ""


def doctor(repo_root: Path) -> DoctorReport:
    """
    Prove the repo's flow.yaml works: load it, find the base, make a throwaway worktree of the base,
    run install (if any) and both checks there, remove the worktree. A pass records doctor.json.
    """
    lines: List[str] = []
    try:
        cfg = load(repo_root)
    except ConfigError as exc:
        return DoctorReport(False, ("FAIL config: %s" % exc,))
    lines.append("ok   config: %s parsed" % CONFIG_RELPATH)

    ref = ""
    for candidate in ("origin/" + cfg.base, cfg.base):
        if _git_out(repo_root, ["rev-parse", "--verify", "--quiet", candidate + "^{commit}"]):
            ref = candidate
            break
    if not ref:
        lines.append("FAIL base: branch %r not found (tried origin/%s and %s)" % (cfg.base, cfg.base, cfg.base))
        return DoctorReport(False, tuple(lines))
    base_sha = gitops.git(["rev-parse", ref + "^{commit}"], repo_root)
    lines.append("ok   base: %s at %s" % (ref, base_sha[:10]))

    tmp = Path(tempfile.mkdtemp(prefix="flow3-doctor-"))
    wt = tmp / "base"
    ok = True
    try:
        try:
            gitops.git(["worktree", "add", "--detach", str(wt), ref], repo_root)
        except gitops.GitError as exc:
            lines.append("FAIL worktree: %s" % exc)
            return DoctorReport(False, tuple(lines))
        lines.append("ok   worktree: throwaway checkout of %s" % ref)
        steps: List[Tuple[str, str, str]] = []
        if cfg.install:
            steps.append(("install", cfg.install, "install"))
        if cfg.check_fast != cfg.check_full:
            steps.append(("fast", cfg.check_fast, "fast check"))
        steps.append(("full", cfg.check_full, "full check"))
        for mode, command, label in steps:
            res = checks.run(command, wt, mode, cfg.check_timeout_secs, tmp / "logs" / (mode + ".log"))
            if res.ok:
                lines.append("ok   %s: `%s` passed in %.1fs" % (label, command, res.duration_secs))
            else:
                why = "timed out after %ds" % cfg.check_timeout_secs if res.timed_out else \
                    "exited %d" % res.exit_code
                lines.append("FAIL %s: `%s` %s on the untouched base. %s"
                             % (label, command, why, _last_line(res.trimmed)))
                ok = False
                break
    finally:
        try:
            gitops.remove_worktree(repo_root, wt)
        except gitops.GitError:
            pass
        shutil.rmtree(str(tmp), ignore_errors=True)
    if ok and cfg.merge_method == "command":
        # Dry run of the merge procedure as far as is safe: the command's program must exist.
        program = (cfg.merge_command or "").split()[0] if (cfg.merge_command or "").split() else ""
        if program and (shutil.which(program) or (repo_root / program).exists()):
            lines.append("ok   merge: command `%s` found (not run)" % cfg.merge_command)
        else:
            lines.append("FAIL merge: the program for merge.command `%s` was not found" % cfg.merge_command)
            ok = False
    elif ok:
        lines.append("ok   merge: squash with the head commit pinned, through the %s provider" % cfg.provider)
    if ok:
        lines.append("ok   worktree: removed")
        record = {"config_hash": config_hash(repo_root), "base_sha": base_sha, "ts": time.time()}
        atomic_write_text(repo_dir(repo_name(repo_root)) / "doctor.json", json.dumps(record, indent=1) + "\n")
    return DoctorReport(ok, tuple(lines))


def doctor_passed(repo_root: Path) -> bool:
    """True when a recorded doctor pass matches the current flow.yaml."""
    rec = read_json_object(repo_dir(repo_name(repo_root)) / "doctor.json")
    if not rec:
        return False
    try:
        return rec.get("config_hash") == config_hash(repo_root)
    except OSError:
        return False
