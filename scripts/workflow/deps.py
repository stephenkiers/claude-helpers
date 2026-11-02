"""
Dependency install precondition for /expert-flow (#417, Step 1).

`deps ensure` makes "dependencies current for this lockfile" an idempotent precondition. The
orchestrator runs it at worktree time and before every step that runs the check. Detection reads
files only and never probes a package manager. An install runs from a closed lockfile table (frozen
or immutable modes only) or from an explicit commands.install read from the MAIN worktree's
project.yaml. The worktree's own copy is ignored, because a flow agent can write it.

Result categories and exit codes (distinct per category; argparse keeps 2 for usage errors):
  current 0, installed 0, failed 1, ambiguous 3, manifest-changed 4, skipped 5
"""

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Tuple

from . import worktrees

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


# Closed lockfile -> package manager table. Any lockfile not listed here is ignored.
LOCKFILE_MANAGERS: Dict[str, str] = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "bun.lock": "bun",
}

# Frozen or immutable installs only. Never a plain `install` that may rewrite the lockfile.
MANAGER_INSTALL_ARGV: Dict[str, Tuple[str, ...]] = {
    "npm": ("npm", "ci"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile"),
    "yarn": ("yarn", "install", "--immutable"),
    "bun": ("bun", "install", "--frozen-lockfile"),
}

DEPS_TIMEOUT_SECS = 900
TAIL_LINE_LIMIT = 40
STAMP_DIR_NAME = "claude-helpers"
STAMP_FILE_NAME = "deps-stamp.json"
APPROVAL_KEY = "approved-manifest-sha"
DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")

EXIT_CODES: Dict[str, int] = {
    "current": 0,
    "installed": 0,
    "failed": 1,
    "ambiguous": 3,
    "manifest-changed": 4,
    "skipped": 5,
}

_ABSENT = "\0absent"

# Redactions applied to install output before any receipt, state file or park file sees it.
_REDACTIONS: List[Tuple[Any, str]] = [
    (re.compile(r"(_authToken\s*=\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(authorization:)[^\r\n]*"), r"\1 ***"),
    (re.compile(r"(_password\s*=\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s/@]+@"), r"\1***@"),
]


@dataclass
class DepsResult:
    """One deps ensure outcome: a status category, its detail, and a redacted tail on failure."""
    status: str
    detail: Optional[str] = None
    tail: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def output_lines(self) -> List[str]:
        head = f"DEPS: {self.status}" if self.detail is None else f"DEPS: {self.status}: {self.detail}"
        if self.status == "failed":
            return [head] + self.tail
        return [head]


@dataclass(frozen=True)
class Lockfile:
    """A root lockfile from LOCKFILE_MANAGERS and the package manager it selects."""
    name: str
    manager: str


@dataclass
class _ManifestChange:
    changed: bool
    names: List[str]
    error: Optional[str] = None


def read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """The parsed JSON object at path, or None when absent, unreadable, unparseable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _declared_package_manager(worktree: Path) -> Optional[str]:
    data = read_json_object(worktree / "package.json")
    if data is None:
        return None
    value = data.get("packageManager")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().split("@", 1)[0] or None


def find_lockfile(worktree: Path) -> Tuple[Optional[Lockfile], Optional[str]]:
    """
    Select the root lockfile of a worktree from the closed table.

    Returns (lockfile, ambiguous). ambiguous is a human-readable list of the conflicting files when
    more than one table lockfile is present, or when package.json packageManager names a different
    manager than the lockfile (D3). Both return None for the lockfile.
    """
    present = [name for name in sorted(LOCKFILE_MANAGERS) if (worktree / name).is_file()]
    if len(present) > 1:
        return None, ", ".join(present)
    if not present:
        return None, None
    name = present[0]
    manager = LOCKFILE_MANAGERS[name]
    declared = _declared_package_manager(worktree)
    if declared is not None and declared != manager:
        return None, f"{name} (package.json packageManager is {declared})"
    return Lockfile(name, manager), None


def manifest_sha(worktree: Path) -> str:
    """sha256 over package.json and every table lockfile's bytes (absence is part of the hash)."""
    digest = hashlib.sha256()
    for name in ["package.json"] + sorted(LOCKFILE_MANAGERS):
        digest.update(name.encode() + b"\0")
        path = worktree / name
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _lockfile_sha(worktree: Path, lockfile: Optional[Lockfile]) -> Optional[str]:
    if lockfile is None:
        return None
    return hashlib.sha256((worktree / lockfile.name).read_bytes()).hexdigest()


def _has_install_artifact(worktree: Path, lockfile: Lockfile) -> bool:
    """node_modules, or for yarn the Plug'n'Play .pnp.cjs (which creates no node_modules)."""
    if (worktree / "node_modules").is_dir():
        return True
    return lockfile.manager == "yarn" and (worktree / ".pnp.cjs").is_file()


def _target_mode(path: Path) -> int:
    """
    Permission bits for a rewritten file: the existing file's bits, or the umask default for a new file.

    mkstemp creates 0600 files, so without this a rewrite would silently tighten a 0644 project.yaml.
    """
    if path.exists():
        return path.stat().st_mode & 0o7777
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to path via a temp file in the same directory and os.replace. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temp_name, _target_mode(path))
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def redact_output(text: str) -> List[str]:
    """Mask credentials in install output and return at most the last TAIL_LINE_LIMIT lines."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text.splitlines()[-TAIL_LINE_LIMIT:]


def _git(args: List[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_worktree(worktree: Path, main_worktree: Path) -> Tuple[Optional[Path], Optional[str]]:
    """The realpath of the worktree, or an error when it is not a directory under the worktree parent."""
    real_worktree = Path(os.path.realpath(str(worktree)))
    if not real_worktree.is_dir():
        return None, f"worktree {real_worktree} is not a directory"
    try:
        parent_text = worktrees.detect_worktree_parent(cwd=main_worktree)
    except (worktrees.WorktreeParentError, OSError) as e:
        return None, f"cannot determine the worktree parent: {e}"
    if not parent_text:
        return None, "cannot determine the worktree parent (no worktrees listed)"
    real_parent = Path(os.path.realpath(parent_text))
    if real_worktree == real_parent or not _is_within(real_worktree, real_parent):
        return None, f"worktree {real_worktree} is outside the worktree parent {real_parent}"
    return real_worktree, None


def _main_install_command(main_worktree: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    commands.install from the MAIN worktree's project.yaml only.

    Returns (command, error). An absent file or key gives (None, None). A file that exists but cannot
    be read is an error, so the install is never silently replaced by the lockfile default.
    """
    path = main_worktree / ".claude" / "project.yaml"
    if not path.exists():
        return None, None
    if yaml is None:
        return None, "PyYAML is not installed, so main project.yaml cannot be read for commands.install"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError) as e:
        return None, f"main project.yaml is unreadable: {e}"
    if not isinstance(data, dict):
        return None, None
    commands = data.get("commands")
    if not isinstance(commands, dict) or commands.get("install") is None:
        return None, None
    install = commands.get("install")
    if not isinstance(install, str) or not install.strip():
        return None, "commands.install must be a non-empty string"
    return install.strip(), None


def _stamp_path(worktree: Path) -> Tuple[Optional[Path], Optional[str]]:
    """<absolute git dir of the worktree>/claude-helpers/deps-stamp.json; per worktree, never committed."""
    result = _git(["rev-parse", "--absolute-git-dir"], worktree)
    git_dir = result.stdout.strip()
    if result.returncode != 0 or not git_dir:
        return None, "not a git worktree, so no deps stamp location exists"
    return Path(git_dir) / STAMP_DIR_NAME / STAMP_FILE_NAME, None


def _read_stamp(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_stamp(path: Path, data: Dict[str, Any]) -> Optional[str]:
    try:
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        return f"could not write deps stamp: {e}"
    return None


def _remove_file(path: Path) -> Optional[str]:
    """Delete path. A missing file is fine; any other failure is returned so the caller can stop."""
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"could not remove {path.name}: {e}"
    return None


def _dependency_map(manifest: Dict[str, Any]) -> Dict[Tuple[str, str], Any]:
    out: Dict[Tuple[str, str], Any] = {}
    for field_name in DEP_FIELDS:
        section = manifest.get(field_name)
        if isinstance(section, dict):
            for name, spec in section.items():
                out[(field_name, name)] = spec
    return out


def _changed_dependency_names(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    before = _dependency_map(old)
    after = _dependency_map(new)
    keys = set(before) | set(after)
    names = {
        name
        for (field_name, name) in keys
        if before.get((field_name, name), _ABSENT) != after.get((field_name, name), _ABSENT)
    }
    return sorted(names)


def _manifest_change(worktree: Path, base: str) -> _ManifestChange:
    """
    Whether package.json or a table lockfile differs between merge-base(base, HEAD) and the working tree.

    Covers committed and uncommitted changes (git diff against the merge-base commit) and untracked
    manifests. The changed dependency names come from parsing both package.json versions as JSON.
    """
    merge_base = _git(["merge-base", base, "HEAD"], worktree)
    base_sha = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not base_sha:
        return _ManifestChange(False, [], f"cannot compute the merge-base of {base} and HEAD")
    paths = ["package.json"] + sorted(LOCKFILE_MANAGERS)
    diff = _git(["diff", "--quiet", base_sha, "--"] + paths, worktree)
    if diff.returncode not in (0, 1):
        return _ManifestChange(False, [], "git diff failed while checking the manifest")
    untracked = _git(["ls-files", "--others", "--exclude-standard", "--"] + paths, worktree)
    if untracked.returncode != 0:
        return _ManifestChange(False, [], "git ls-files failed while checking the manifest")
    if diff.returncode == 0 and not untracked.stdout.strip():
        return _ManifestChange(False, [])

    old_obj: Dict[str, Any] = {}
    old_show = _git(["show", f"{base_sha}:package.json"], worktree)
    if old_show.returncode == 0:
        try:
            parsed_old = json.loads(old_show.stdout)
        except ValueError:
            parsed_old = None
        if not isinstance(parsed_old, dict):
            return _ManifestChange(False, [], "base package.json is not a JSON object")
        old_obj = parsed_old

    new_obj: Dict[str, Any] = {}
    new_path = worktree / "package.json"
    if new_path.is_file():
        parsed_new = read_json_object(new_path)
        if parsed_new is None:
            return _ManifestChange(False, [], "worktree package.json is not a JSON object")
        new_obj = parsed_new

    return _ManifestChange(True, _changed_dependency_names(old_obj, new_obj))


def _describe_change(names: List[str]) -> str:
    if names:
        return ", ".join(names)
    return "lockfile or package.json metadata (no dependency names changed)"


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass


OUTPUT_TAIL_BYTES = 64 * 1024


def _read_output_tail(sink: IO[bytes]) -> str:
    """
    The last OUTPUT_TAIL_BYTES of captured install output, starting at a whole line.

    Output goes to a temp file and only this tail is read back, so a noisy install cannot grow the
    process without bound. A line cut by the window is dropped, so a credential is never shown half-masked.
    """
    sink.flush()
    size = sink.seek(0, os.SEEK_END)
    sink.seek(max(0, size - OUTPUT_TAIL_BYTES))
    text = sink.read().decode("utf-8", errors="replace")
    if size > OUTPUT_TAIL_BYTES and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _run_install(argv: List[str], worktree: Path, timeout: int) -> Tuple[Optional[int], str, Optional[str]]:
    """
    Run one install with shell=False, stdin from DEVNULL, a hard timeout and Corepack prompts off.

    Returns (returncode, output_tail, error). error is "timeout" or a launch failure, else None.
    """
    env = dict(os.environ)
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["CI"] = "1"
    with tempfile.TemporaryFile() as sink:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(worktree),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            return None, "", f"could not start {argv[0]}: {e}"
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            return None, _read_output_tail(sink), "timeout"
        return proc.returncode, _read_output_tail(sink), None


def ensure_deps(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str] = None,
    approve_manifest: bool = False,
    timeout: int = DEPS_TIMEOUT_SECS,
) -> DepsResult:
    """
    Make dependencies current for the worktree's lockfile, installing only when needed.

    Args:
        worktree: The step's worktree (must sit under the worktree parent).
        main_worktree: The main worktree whose project.yaml supplies commands.install.
        base: Flow base ref (e.g. origin/main). When given, the manifest gate runs against
            merge-base(base, HEAD). Omitted at worktree time, where the branch equals its base.
        approve_manifest: Record approval of the current manifest hash in the stamp, so the gate
            clears. Only the orchestrator passes this, at a manifest-changed stop the human chose to retry.
        timeout: Hard limit on the install, in seconds.
    """
    real_worktree, error = _validated_worktree(worktree, main_worktree)
    if error is not None or real_worktree is None:
        return DepsResult("failed", error or "invalid worktree")

    install_command, error = _main_install_command(main_worktree)
    if error is not None:
        return DepsResult("failed", error)

    lockfile, ambiguous = find_lockfile(real_worktree)
    if ambiguous is not None:
        return DepsResult("ambiguous", ambiguous)
    if lockfile is None and install_command is None:
        return DepsResult("skipped", "no lockfile")

    if install_command is not None:
        try:
            argv = shlex.split(install_command)
        except ValueError as e:
            return DepsResult("failed", f"commands.install is not a valid command ({e})")
        command_key = install_command
    else:
        assert lockfile is not None
        argv = list(MANAGER_INSTALL_ARGV[lockfile.manager])
        command_key = " ".join(argv)
    if not argv:
        return DepsResult("failed", "commands.install is empty")

    stamp_file, error = _stamp_path(real_worktree)
    if error is not None or stamp_file is None:
        return DepsResult("failed", error or "no stamp location")

    stamp = _read_stamp(stamp_file)
    if approve_manifest:
        stamp = {APPROVAL_KEY: manifest_sha(real_worktree)}
        write_error = _write_stamp(stamp_file, stamp)
        if write_error is not None:
            return DepsResult("failed", write_error)

    if base is not None:
        change = _manifest_change(real_worktree, base)
        if change.error is not None:
            return DepsResult("failed", change.error)
        if change.changed and stamp.get(APPROVAL_KEY) != manifest_sha(real_worktree):
            return DepsResult("manifest-changed", _describe_change(change.names))

    lock_sha = _lockfile_sha(real_worktree, lockfile)
    artifact_ok = lockfile is None or _has_install_artifact(real_worktree, lockfile)
    if stamp.get("command") == command_key and stamp.get("lockfile_sha256") == lock_sha and artifact_ok:
        return DepsResult("current")

    # The stamp goes before the install starts; a failed or killed install never looks installed.
    # A manifest approval survives in an approval-only stamp so a failed install does not force a new one.
    prior_approval = stamp.get(APPROVAL_KEY)
    remove_error = _remove_file(stamp_file)
    if remove_error is not None:
        return DepsResult("failed", remove_error)
    if prior_approval is not None:
        write_error = _write_stamp(stamp_file, {APPROVAL_KEY: prior_approval})
        if write_error is not None:
            return DepsResult("failed", write_error)

    code, output, run_error = _run_install(argv, real_worktree, timeout)
    if run_error == "timeout":
        return DepsResult("failed", "timeout", redact_output(output))
    if run_error is not None:
        return DepsResult("failed", run_error)
    if code != 0:
        return DepsResult("failed", f"exit {code}", redact_output(output))

    record: Dict[str, Any] = {
        "command": command_key,
        "lockfile": lockfile.name if lockfile is not None else None,
        "lockfile_sha256": lock_sha,
    }
    if prior_approval is not None:
        record[APPROVAL_KEY] = prior_approval
    write_error = _write_stamp(stamp_file, record)
    if write_error is not None:
        return DepsResult("failed", write_error)
    return DepsResult("installed")
