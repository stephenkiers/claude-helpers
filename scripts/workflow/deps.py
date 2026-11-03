"""
Dependency install precondition for /expert-flow (#417, Step 1).

`deps ensure` makes "dependencies current for this lockfile" an idempotent precondition. The
orchestrator runs it at worktree time and before every step that runs the check. Detection reads
files only and never probes a package manager. An install runs from a closed lockfile table (frozen
or immutable modes only) or from an explicit commands.install read from the MAIN worktree's
project.yaml. The worktree's own copy is ignored, because a flow agent can write it.

Result categories and exit codes (distinct per category; argparse keeps 2 for usage errors):
  current 0, installed 0, failed 1, ambiguous 3, manifest-changed 4, skipped 5

Operational notes:
  - Timeout: an install is killed after DEPS_TIMEOUT_SECS (900s). A caller that runs `deps ensure`
    in the background must allow more than that before it kills the process, or the install may be
    cut short by the caller instead. A SIGKILL to this process cannot run the cleanup, so the
    installer's process group keeps running in that case. SIGINT and SIGTERM do run the cleanup:
    the group is killed, and SIGTERM exits 143.
  - Environment: the install inherits the full environment on purpose (see _run_install).
  - Stamp: a successful install records the command, lockfile hash, manifest hash and any manifest
    approval in <git dir>/claude-helpers/deps-stamp.json. An install that leaves neither node_modules
    nor .pnp.cjs cannot pass the artifact check, so it is reinstalled on every run.
  - Manifest gate: covers package.json, the table lockfiles, INSTALL_CONFIG_FILES and .yarn/releases.
    Its residual gaps are listed beside INSTALL_CONFIG_FILES.
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
from typing import IO, Any, Dict, List, Literal, Optional, Tuple

from . import worktrees
from .fsutil import atomic_write_text, read_json_object

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

# atomic_write_text and read_json_object live in fsutil.py; checks.py imports them from this module.

Status = Literal["current", "installed", "failed", "ambiguous", "manifest-changed", "skipped"]

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
    # Yarn 2 and later (Berry). Yarn 1 uses YARN1_INSTALL_ARGV; see yarn_install_argv.
    "yarn": ("yarn", "install", "--immutable"),
    "bun": ("bun", "install", "--frozen-lockfile"),
}
YARN1_INSTALL_ARGV: Tuple[str, ...] = ("yarn", "install", "--frozen-lockfile")

# Install-config files whose bytes change what an install does. They are hashed into manifest_sha and
# diffed by the manifest gate with package.json and the lockfiles; .yarn/releases/* is covered the same way.
# Residual, not covered by the gate: workspace package.json files in subdirectories, pnpm-workspace.yaml,
# a Yarn 1 .yarnrc, user and global config (~/.npmrc, ~/.yarnrc.yml), environment variables and the Node
# engine version. A change to any of those does not stop at manifest-changed.
INSTALL_CONFIG_FILES: Tuple[str, ...] = (".npmrc", ".yarnrc.yml", ".pnpmfile.cjs", "bunfig.toml")
YARN_RELEASES_DIR = ".yarn/releases"

DEPS_TIMEOUT_SECS = 900
TAIL_LINE_LIMIT = 40
OUTPUT_TAIL_BYTES = 64 * 1024
STAMP_DIR_NAME = "claude-helpers"
STAMP_FILE_NAME = "deps-stamp.json"
APPROVAL_KEY = "approved-manifest-sha"
DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")

EXIT_CODES: Dict[Status, int] = {
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
    (re.compile(r"(?i)((?:_authtoken|_password|password|token)\s*=\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(authorization:)[^\r\n]*"), r"\1 ***"),
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s/@]+@"), r"\1***@"),
    # Token prefixes: GitHub (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_), npm (npm_), Slack (xoxb-, xoxp-, ...).
    (re.compile(r"(\b(?:gh[pousr]_|github_pat_|npm_|xox[abposr]-))[A-Za-z0-9_\-]{16,}"), r"\1***"),
    # Environment variable colon form: GH_TOKEN: value, NPM_TOKEN: value.
    (re.compile(r"(?i)((?:GH_TOKEN|NPM_TOKEN)\s*:\s*)\S+"), r"\1***"),
]


@dataclass
class DepsResult:
    """One deps ensure outcome: a status category, its detail, and a redacted tail on failure."""
    status: Status
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


@dataclass
class InstallOutcome:
    """
    What one install run produced.

    returncode is None when the run never exited (timed out) or never started (launch_error set).
    """
    returncode: Optional[int]
    output_tail: str
    launch_error: Optional[str] = None
    timed_out: bool = False


@dataclass(frozen=True)
class _InstallPlan:
    argv: List[str]
    command_key: str


@dataclass(frozen=True)
class _Fingerprint:
    """What a stamp must record for an install to count as current."""
    command: str
    lockfile: Optional[str]
    lockfile_sha: Optional[str]
    manifest: str

    def record(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "lockfile": self.lockfile,
            "lockfile_sha256": self.lockfile_sha,
            "manifest_sha256": self.manifest,
        }

    def matches(self, stamp: Dict[str, Any]) -> bool:
        return (
            stamp.get("command") == self.command
            and stamp.get("lockfile_sha256") == self.lockfile_sha
            and stamp.get("manifest_sha256") == self.manifest
        )


def _declared_package_manager_parts(worktree: Path) -> Tuple[Optional[str], Optional[str]]:
    """(name, version) from package.json packageManager, e.g. ("yarn", "4.1.0"); a missing part is None."""
    data = read_json_object(worktree / "package.json")
    if data is None:
        return None, None
    value = data.get("packageManager")
    if not isinstance(value, str) or not value.strip():
        return None, None
    name, _, rest = value.strip().partition("@")
    version = rest.split("+", 1)[0].strip() or None
    return name.strip() or None, version


def _declared_package_manager(worktree: Path) -> Optional[str]:
    return _declared_package_manager_parts(worktree)[0]


def yarn_install_argv(version: Optional[str]) -> Optional[Tuple[str, ...]]:
    """
    The frozen install argv for a Yarn version from packageManager: --frozen-lockfile for 1.x and
    --immutable for 2 and later. None when the version cannot be read, so the caller fails closed.
    """
    match = re.match(r"(\d+)(?:\.|$)", (version or "").strip())
    if match is None:
        return None
    major = int(match.group(1))
    if major == 1:
        return YARN1_INSTALL_ARGV
    if major >= 2:
        return MANAGER_INSTALL_ARGV["yarn"]
    return None


def find_lockfile(worktree: Path) -> Tuple[Optional[Lockfile], Optional[str]]:
    """
    Select the root lockfile of a worktree from the closed table.

    Returns (lockfile, ambiguous). ambiguous is a human-readable list of the conflicting files when
    the table lockfiles name more than one manager, or when package.json packageManager names a
    different manager than the lockfile (D3). Both return None for the lockfile. Two lockfiles of the
    same manager (bun.lock and bun.lockb) are one manager, so they select the first name in sorted order.
    """
    present = [name for name in sorted(LOCKFILE_MANAGERS) if (worktree / name).is_file()]
    managers = {LOCKFILE_MANAGERS[name] for name in present}
    if len(managers) > 1:
        return None, ", ".join(present)
    if not present:
        return None, None
    name = present[0]
    manager = LOCKFILE_MANAGERS[name]
    declared = _declared_package_manager(worktree)
    if declared is not None and declared != manager:
        return None, f"{name} (package.json packageManager is {declared})"
    return Lockfile(name, manager), None


def _manifest_files(worktree: Path) -> List[str]:
    """Relative names hashed into manifest_sha, in a fixed order; a file's absence is part of the hash."""
    names = ["package.json"] + sorted(LOCKFILE_MANAGERS) + list(INSTALL_CONFIG_FILES)
    releases = worktree / YARN_RELEASES_DIR
    if releases.is_dir():
        names += sorted(f"{YARN_RELEASES_DIR}/{p.name}" for p in releases.iterdir() if p.is_file())
    return names


def _manifest_pathspecs() -> List[str]:
    """The git pathspecs the manifest gate diffs: the same files as _manifest_files, with the releases directory whole."""
    return ["package.json"] + sorted(LOCKFILE_MANAGERS) + list(INSTALL_CONFIG_FILES) + [YARN_RELEASES_DIR]


def manifest_sha(worktree: Path) -> str:
    """sha256 over package.json, every table lockfile, INSTALL_CONFIG_FILES and .yarn/releases (absence is part of the hash)."""
    digest = hashlib.sha256()
    for name in _manifest_files(worktree):
        digest.update(name.encode() + b"\0")
        path = worktree / name
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _lockfile_sha(worktree: Path, lockfile: Optional[Lockfile]) -> Optional[str]:
    """sha256 over every table lockfile that belongs to the selected manager (bun.lock and bun.lockb both count)."""
    if lockfile is None:
        return None
    digest = hashlib.sha256()
    for name in sorted(LOCKFILE_MANAGERS):
        if LOCKFILE_MANAGERS[name] == lockfile.manager and (worktree / name).is_file():
            digest.update(name.encode() + b"\0")
            digest.update((worktree / name).read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _has_install_artifact(worktree: Path) -> bool:
    """node_modules, or the Yarn Plug'n'Play .pnp.cjs (which creates no node_modules). Required for every install."""
    return (worktree / "node_modules").is_dir() or (worktree / ".pnp.cjs").is_file()


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
    """
    Delete path, preserving symlinks. If path is a symlink, delete its target instead of the link.
    A missing file is fine; any other failure is returned so the caller can stop.
    """
    try:
        # If path is a symlink, resolve and delete the target instead of the symlink
        path_str = str(path)
        if os.path.islink(path_str):
            target_str = os.path.realpath(path_str)
            os.unlink(target_str)
        else:
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
    Whether the manifest files differ between merge-base(base, HEAD) and the working tree.

    Covers committed and uncommitted changes (git diff against the merge-base commit) and untracked
    manifest files. The changed dependency names come from parsing both package.json versions as JSON.
    A base that starts with "-" is refused, so it can never be read by git as an option.
    """
    if base.startswith("-"):
        return _ManifestChange(False, [], "base ref must not start with '-'")
    merge_base = _git(["merge-base", base, "HEAD"], worktree)
    base_sha = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not base_sha:
        return _ManifestChange(False, [], f"cannot compute the merge-base of {base} and HEAD")
    paths = _manifest_pathspecs()
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
    return "lockfile, install config or package.json metadata (no dependency names changed)"


def _stop_group(proc: "subprocess.Popen[bytes]") -> None:
    """
    Kill the install's process group if the install is still running, then reap the leader.

    Runs on every exit path of an install, so an interrupt cannot leave the installer running. A
    finished install is not signalled, so a normal exit keeps its current behaviour.
    """
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait()


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


def _exit_on_term(signum: int, frame: Any) -> None:
    """SIGTERM handler: raise SystemExit so the install's cleanup runs before the process exits."""
    raise SystemExit(128 + signum)


def _execute_install(
    argv: List[str], worktree: Path, env: Dict[str, str], timeout: int, sink: IO[bytes]
) -> InstallOutcome:
    proc: Optional["subprocess.Popen[bytes]"] = None
    timed_out = False
    try:
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
            return InstallOutcome(None, "", launch_error=f"could not start {argv[0]}: {e}")
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # One try covers launch and wait, so an interrupt right after Popen returns still kills the group.
        if proc is not None:
            _stop_group(proc)
    tail = _read_output_tail(sink)
    if timed_out:
        return InstallOutcome(None, tail, timed_out=True)
    assert proc is not None  # a failed launch returned above
    return InstallOutcome(proc.returncode, tail)


def _run_install(argv: List[str], worktree: Path, timeout: int) -> InstallOutcome:
    """
    Run one install with shell=False, stdin from DEVNULL, a hard timeout and Corepack prompts off.

    The install runs in its own session, so its process group can be killed as a whole. The group is
    killed on timeout, on KeyboardInterrupt, and on SIGTERM (which is turned into SystemExit first).
    A SIGKILL to this process cannot run that cleanup.

    Deliberate residual (human decision B): the full environment is passed, not a scrubbed one, so
    lifecycle scripts see tokens such as GH_TOKEN and NPM_TOKEN. Installs already run branch-controlled
    scripts, and scrubbing the environment would break private-registry auth.
    """
    env = dict(os.environ)
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["CI"] = "1"
    previous: Any = signal.SIG_DFL
    guarded = False
    try:
        previous = signal.signal(signal.SIGTERM, _exit_on_term)
        guarded = True
    except ValueError:
        pass  # Not the main thread: signal handlers cannot be installed, so the default applies.
    try:
        with tempfile.TemporaryFile() as sink:
            return _execute_install(argv, worktree, env, timeout, sink)
    finally:
        if guarded:
            signal.signal(signal.SIGTERM, previous)


def _manifest_gate(
    worktree: Path, base: str, stamp: Dict[str, Any], current_manifest: str, ignore_approvals: bool = False
) -> Optional[DepsResult]:
    """manifest-changed or failed when the manifest differs from base and is not approved; None to proceed."""
    change = _manifest_change(worktree, base)
    if change.error is not None:
        return DepsResult("failed", change.error)
    if change.changed:
        if ignore_approvals or stamp.get(APPROVAL_KEY) != current_manifest:
            return DepsResult("manifest-changed", _describe_change(change.names))
    return None


def _lockfile_install_argv(
    worktree: Path, lockfile: Lockfile
) -> Tuple[Optional[Tuple[str, ...]], Optional[str]]:
    """The frozen argv for the selected manager. Yarn is chosen by its packageManager major, or fails closed."""
    if lockfile.manager != "yarn":
        return MANAGER_INSTALL_ARGV[lockfile.manager], None
    _name, version = _declared_package_manager_parts(worktree)
    argv = yarn_install_argv(version)
    if argv is None:
        return None, (
            "yarn version is unknown (package.json packageManager has no usable yarn version); "
            "set commands.install in the main project.yaml"
        )
    return argv, None


def _install_plan(
    worktree: Path, lockfile: Optional[Lockfile], install_command: Optional[str]
) -> Tuple[Optional[_InstallPlan], Optional[str]]:
    """The argv to run and the command string recorded in the stamp. An explicit commands.install wins."""
    if install_command is not None:
        try:
            argv = shlex.split(install_command)
        except ValueError as e:
            return None, f"commands.install is not a valid command ({e})"
        if not argv:
            return None, "commands.install is empty"
        return _InstallPlan(argv, install_command), None
    if lockfile is None:
        return None, "no lockfile and no commands.install"
    argv_tuple, error = _lockfile_install_argv(worktree, lockfile)
    if argv_tuple is None:
        return None, error or "no install argv"
    return _InstallPlan(list(argv_tuple), " ".join(argv_tuple)), None


def _install_and_record(
    worktree: Path,
    plan: _InstallPlan,
    fingerprint: _Fingerprint,
    stamp_file: Path,
    stamp: Dict[str, Any],
    timeout: int,
) -> DepsResult:
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

    outcome = _run_install(plan.argv, worktree, timeout)
    if outcome.timed_out:
        return DepsResult("failed", "timeout", redact_output(outcome.output_tail))
    if outcome.launch_error is not None:
        return DepsResult("failed", outcome.launch_error)
    if outcome.returncode != 0:
        return DepsResult("failed", f"exit {outcome.returncode}", redact_output(outcome.output_tail))

    record = fingerprint.record()
    if prior_approval is not None:
        record[APPROVAL_KEY] = prior_approval
    write_error = _write_stamp(stamp_file, record)
    if write_error is not None:
        return DepsResult("failed", write_error)
    return DepsResult("installed")


def _ensure(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str],
    approve_manifest: bool,
    timeout: int,
    ignore_approvals: bool = False,
) -> DepsResult:
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

    plan, error = _install_plan(real_worktree, lockfile, install_command)
    if plan is None:
        return DepsResult("failed", error or "no install plan")

    stamp_file, error = _stamp_path(real_worktree)
    if error is not None or stamp_file is None:
        return DepsResult("failed", error or "no stamp location")

    current_manifest = manifest_sha(real_worktree)
    fingerprint = _Fingerprint(
        command=plan.command_key,
        lockfile=lockfile.name if lockfile is not None else None,
        lockfile_sha=_lockfile_sha(real_worktree, lockfile),
        manifest=current_manifest,
    )

    stamp = _read_stamp(stamp_file)
    if approve_manifest:
        # Merged into the existing stamp, so an approval never erases the record of the current install.
        stamp = dict(stamp)
        stamp[APPROVAL_KEY] = current_manifest
        write_error = _write_stamp(stamp_file, stamp)
        if write_error is not None:
            return DepsResult("failed", write_error)

    if base is not None:
        gated = _manifest_gate(real_worktree, base, stamp, current_manifest, ignore_approvals)
        if gated is not None:
            return gated

    if fingerprint.matches(stamp) and _has_install_artifact(real_worktree):
        return DepsResult("current")

    return _install_and_record(real_worktree, plan, fingerprint, stamp_file, stamp, timeout)


def deps_currency(worktree: Path, main_worktree: Path, base: Optional[str] = None) -> str:
    """
    Read-only check whether an install is needed for the worktree, never raising.

    Args:
        worktree: The step's worktree (must sit under the worktree parent).
        main_worktree: The main worktree whose project.yaml supplies commands.install.
        base: Flow base ref (e.g. origin/main). When given, the manifest gate runs against
            merge-base(base, HEAD). Omitted at worktree time, where the branch equals its base.

    Returns:
        Exactly one of:
        - "stale": an install would run
        - "current": dependencies are current
        - "unknown": any error, ambiguous, skipped, or manifest-changed (including under
            ignore-approvals semantics, since a stored approval may be forged)

    Never raises. Any read error returns "unknown".
    """
    try:
        real_worktree, error = _validated_worktree(worktree, main_worktree)
        if error is not None or real_worktree is None:
            return "unknown"

        install_command, error = _main_install_command(main_worktree)
        if error is not None:
            return "unknown"

        lockfile, ambiguous = find_lockfile(real_worktree)
        if ambiguous is not None:
            return "unknown"
        if lockfile is None and install_command is None:
            return "unknown"

        plan, error = _install_plan(real_worktree, lockfile, install_command)
        if plan is None:
            return "unknown"

        stamp_file, error = _stamp_path(real_worktree)
        if error is not None or stamp_file is None:
            return "unknown"

        current_manifest = manifest_sha(real_worktree)
        fingerprint = _Fingerprint(
            command=plan.command_key,
            lockfile=lockfile.name if lockfile is not None else None,
            lockfile_sha=_lockfile_sha(real_worktree, lockfile),
            manifest=current_manifest,
        )

        stamp = _read_stamp(stamp_file)

        # Check manifest gate with ignore_approvals=True to detect if manifest changed.
        # Any stored approval is potentially forged, so we ignore it.
        if base is not None:
            gated = _manifest_gate(real_worktree, base, stamp, current_manifest, ignore_approvals=True)
            if gated is not None:
                # manifest-changed or failed -> unknown
                return "unknown"

        if fingerprint.matches(stamp) and _has_install_artifact(real_worktree):
            return "current"

        return "stale"
    except Exception:
        # Never raise: any exception returns "unknown"
        return "unknown"


def ensure_deps(
    worktree: Path,
    main_worktree: Path,
    base: Optional[str] = None,
    approve_manifest: bool = False,
    timeout: int = DEPS_TIMEOUT_SECS,
    ignore_approvals: bool = False,
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
        ignore_approvals: When True, ignore the stored APPROVAL_KEY in the manifest gate, so a
            manifest that differs from the merge-base returns manifest-changed even if an approval
            exists. Default False keeps the behavior unchanged.

    Returns:
        A DepsResult whose status is one of the Status categories. Every failure is returned, not raised:
        an OSError from reading or writing the worktree, its stamp or the install output comes back as
        status "failed" with a detail.
    """
    try:
        return _ensure(worktree, main_worktree, base, approve_manifest, timeout, ignore_approvals)
    except OSError as e:
        return DepsResult("failed", f"filesystem error: {e}")
