"""
Driver-side preflights for the v2 /expert-flow driver (Layer 2 Step 4, ADR-0026, sessions D1 and D6).

Three edge adapters, each returning a typed result and never raising out of `next`:

  check_hook_preflight: derives the restricted flow agents from the installed agent definition files
    (agents/flow-*.md that declare a `hooks:` block) and checks that each one registers the three guard
    hooks v1's preflight checks, and that the guard scripts are installed. A miss is Stop("hook-preflight").
    Session D6: one Python function, #407 stays open for v1 ("v2 half done, v1 still open").

  north_star_files: runs only the `files` phase of scripts/north-star-preflight.py, never `snapshot`,
    because only `files` can run before a spawn (session D1). OK is Ok. BLIND is Warn, which the caller
    turns into the `NORTH STAR BLIND:` directive line, `allow_degraded: true` and `--allow-degraded`.
    Any hard failure is Stop("north-star-preflight"), never a retry.

run_command is the one subprocess seam. It takes a list argv (never a shell string), and a start failure
comes back as a non-zero CommandResult, so an adapter maps it to its own Stop. Tests inject a fake Runner.

Python 3.8 floor: typing.Optional/Union/Tuple only, no match statements, no X | Y at runtime.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Literal, NamedTuple, Optional, Sequence, Set, Tuple, Union, get_args

from .model import GENERAL_PURPOSE_AGENT
from .steps import AgentType, RestrictedAgent


class CommandResult(NamedTuple):
    """The captured outcome of one external command. returncode -1 means the command could not start."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]

CANNOT_START = -1
# Every preflight subprocess runs while the flow lock is held (D5), so each one is bounded.
COMMAND_TIMEOUT_SECS = 60.0


def run_command(argv: Sequence[str], timeout: float = COMMAND_TIMEOUT_SECS) -> CommandResult:
    """
    Run argv as a list (no shell) and capture its text output, bounded by timeout seconds.

    A start failure and a timeout are both returned as CANNOT_START, never raised, so the adapters map them to
    their own Stop (a gh-error for gh calls).
    """
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(CANNOT_START, "", f"timed out after {timeout:g}s running {argv[0]}")
    except OSError as exc:
        return CommandResult(CANNOT_START, "", f"could not start {argv[0]}: {exc}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class Ok:
    """The preflight passed; nothing is carried into the directive."""


@dataclass(frozen=True)
class Warn:
    """The preflight passed degraded. `line` is the `NORTH STAR BLIND:` text the directive carries (D1)."""

    line: str


@dataclass(frozen=True)
class Stop:
    """The preflight failed. `code` is the StopCode prefix; the reason is built as `<code>: <detail>`."""

    code: Literal["hook-preflight", "north-star-preflight", "run-dir-unresolved"]
    detail: str

    def reason(self) -> str:
        return f"{self.code}: {self.detail}"


PreflightResult = Union[Ok, Warn, Stop]

GUARD_SCRIPTS: Tuple[str, ...] = ("flow-agent-guard.py", "flow-skill-guard.py", "flow-bash-guard.py")
BASH_GUARD = "flow-bash-guard.py"
# Restricted agents whose role carries no Bash guard (ADR-0008 2026-10-26: flow-review's Skill-only role). The
# agent files declare no bash-guard hook for them, so the check must not demand one. Every other agent needs all three.
BASH_GUARD_EXEMPT: FrozenSet[str] = frozenset({"flow-review"})
_FRONTMATTER_FENCE = "---"
_NAME_LINE = re.compile(r"^name:\s*(\S+)\s*$")
_HOOKS_LINE = re.compile(r"^hooks:\s*$")
# A guard registration, e.g.  command: "$HOME/.claude/scripts/flow-bash-guard.py --agent flow-implement"
_GUARD_COMMAND = re.compile(r'^\s*command:\s*"?\$HOME/\.claude/scripts/(\S+?)\s+--agent\s+(\S+?)"?\s*$')
_BLIND_PREFIX = "BLIND "
_NORTH_STAR_BLIND = "NORTH STAR BLIND: "


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def default_claude_dir() -> Path:
    return _home().joinpath(".claude")


def _frontmatter(text: str) -> Optional[List[str]]:
    """The lines between the leading `---` fences, or None when the file has no frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            return lines[1:index]
    return None


def _agent_name(front: List[str], fallback: str) -> str:
    for line in front:
        match = _NAME_LINE.match(line)
        if match:
            return match.group(1)
    return fallback


def _guard_registrations(front: List[str]) -> Set[Tuple[str, str]]:
    """The (guard script, agent) pairs the frontmatter registers as hook commands."""
    found: Set[Tuple[str, str]] = set()
    for line in front:
        match = _GUARD_COMMAND.match(line)
        if match:
            found.add((match.group(1), match.group(2)))
    return found


def check_hook_preflight(agents_dir: Optional[Path] = None, scripts_dir: Optional[Path] = None) -> Union[Ok, Stop]:
    """
    Derive the restricted flow agents from agents_dir and check their guard-hook registrations (session D6).

    A restricted agent is an agents/flow-*.md file whose frontmatter declares a `hooks:` block. Each one must
    register every GUARD_SCRIPTS entry with `--agent <its name>`, and each guard script must be installed as a
    file under scripts_dir (a dangling symlink counts as missing). Pure reads; writes nothing.
    """
    agents = agents_dir if agents_dir is not None else default_claude_dir().joinpath("agents")
    scripts = scripts_dir if scripts_dir is not None else default_claude_dir().joinpath("scripts")
    for guard in GUARD_SCRIPTS:
        if not (scripts / guard).is_file():
            return Stop("hook-preflight", f"guard hook {guard} is not installed under {scripts}")
    if not agents.is_dir():
        return Stop("hook-preflight", f"agent definitions directory {agents} does not exist")
    restricted = 0
    for path in sorted(agents.glob("flow-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return Stop("hook-preflight", f"cannot read agent definition {path.name} ({exc.__class__.__name__})")
        front = _frontmatter(text)
        if front is None or not any(_HOOKS_LINE.match(line) for line in front):
            continue
        name = _agent_name(front, path.stem)
        registered = _guard_registrations(front)
        for guard in GUARD_SCRIPTS:
            if guard == BASH_GUARD and name in BASH_GUARD_EXEMPT:
                continue
            if (guard, name) not in registered:
                return Stop("hook-preflight", f"restricted agent {name} does not register {guard} in {path.name}")
        restricted += 1
    if restricted == 0:
        return Stop("hook-preflight", f"no restricted flow agents with hooks found under {agents}")
    return Ok()


def north_star_script() -> Path:
    """scripts/north-star-preflight.py, beside this package's parent directory."""
    return Path(__file__).resolve().parent.parent / "north-star-preflight.py"


def check_north_star_files(
    claude_dir: Optional[Path] = None,
    run: Runner = run_command,
    script: Optional[Path] = None,
) -> PreflightResult:
    """
    Run the `files` phase of north-star-preflight.py without --allow-degraded (session D1).

    stdout `OK` is Ok. stdout `BLIND <tokens>` with exit 0 is Warn, carrying the `NORTH STAR BLIND:` line.
    Any other outcome, including the hard-failure exit 3 and a start failure, is Stop("north-star-preflight").
    The caller forwards --allow-degraded only on a Warn.
    """
    target = claude_dir if claude_dir is not None else default_claude_dir()
    preflight = script if script is not None else north_star_script()
    result = run([sys.executable, str(preflight), "files", "--claude-dir", str(target)])
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if result.returncode == 0 and first == "OK":
        return Ok()
    if result.returncode == 0 and first.startswith(_BLIND_PREFIX):
        return Warn(_NORTH_STAR_BLIND + first[len(_BLIND_PREFIX):])
    detail = first or result.stderr.strip() or f"exit {result.returncode}"
    return Stop("north-star-preflight", f"files phase did not pass: {detail}")


# The guard hooks a restricted flow agent needs in settings.json (install.sh's flow_hook_command): the Bash guard, the
# Skill guard and the agent-allowlist guard, each registered with `--agent auto`.
REQUIRED_SETTINGS_GUARDS: Tuple[str, ...] = ("flow-bash-guard.py", "flow-skill-guard.py", "flow-agent-guard.py")
_SETUP_HINT = "run /setup-local"


def _registered_guard_commands(settings: object) -> Optional[List[str]]:
    """The hook command strings under hooks.PreToolUse of a parsed settings.json, or None when the shape is wrong."""
    if not isinstance(settings, dict):
        return None
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get("PreToolUse", [])
    if not isinstance(entries, list):
        return None
    commands: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            return None
        for hook in inner:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands


def _registered_script(command: str, name: str) -> Optional[str]:
    """The registered script path of a `<script> --agent auto` command for guard `name`, or None when it is not that."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) != 3 or os.path.basename(tokens[0]) != name or tokens[1:] != ["--agent", "auto"]:
        return None
    return tokens[0]


def check_agent_preflight(
    agent_type: AgentType,
    agents_dir: Path,
    prompts_dir: Path,
    settings_path: Path,
) -> Optional[Stop]:
    """
    The per-spawn file and registration preflight for a flow agent (plan Step 3). None for general-purpose. For a
    flow agent: its definition file and the step contract must exist; for a RestrictedAgent, settings.json must register
    the three guard hooks with `--agent auto`, and each registered script must be an installed regular file. The
    registration is read from settings.json, never from agent frontmatter. An unreadable or malformed settings.json fails
    closed. A miss is Stop("hook-preflight") and records no step failure.
    """
    if agent_type == GENERAL_PURPOSE_AGENT:
        return None
    agent_file = agents_dir / f"{agent_type}.md"
    if not agent_file.is_file():
        return Stop("hook-preflight", f"agent file missing: {agent_file} — {_SETUP_HINT}")
    contract = prompts_dir / "flow-step-contract.md"
    if not contract.is_file():
        return Stop("hook-preflight", f"agent file missing: {contract} — {_SETUP_HINT}")
    if agent_type not in get_args(RestrictedAgent):
        return None
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Stop("hook-preflight", f"cannot read {settings_path} ({exc.__class__.__name__}) — {_SETUP_HINT}")
    commands = _registered_guard_commands(settings)
    if commands is None:
        return Stop("hook-preflight", f"{settings_path} has an unexpected hooks shape — {_SETUP_HINT}")
    for name in REQUIRED_SETTINGS_GUARDS:
        scripts = [path for path in (_registered_script(command, name) for command in commands) if path is not None]
        if not scripts:
            return Stop("hook-preflight", f"settings.json does not register guard hook {name} with --agent auto — {_SETUP_HINT}")
        for path in scripts:
            installed = Path(path.replace("$HOME", os.path.expanduser("~"), 1))
            if not installed.is_file():
                return Stop("hook-preflight", f"guard hook {installed} is not installed — {_SETUP_HINT}")
    return None


def _worktree_entries(text: str) -> List[Dict[str, str]]:
    """The records of the porcelain worktree listing: each a dict of its `worktree` and `branch` lines."""
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key in ("worktree", "branch"):
            current[key] = value
    if current:
        records.append(current)
    return records


def resolve_main_worktree(worktree: str) -> Union[str, Stop]:
    """
    The main (or master) worktree that is the sibling of `worktree`, for a merge run directory (plan Step 3). The entry
    is found by its branch, main before master; the first entry is never used. It is accepted only when its realpath
    is an existing directory other than the worktree's and shares the worktree's parent. A miss is Stop("run-dir-unresolved").
    """
    result = run_command(["git", "-C", worktree, "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return Stop("run-dir-unresolved", f"{worktree}: git worktree list failed ({detail})")
    records = _worktree_entries(result.stdout)
    chosen: Optional[str] = None
    for wanted in ("refs/heads/main", "refs/heads/master"):
        for record in records:
            if record.get("branch") == wanted and "worktree" in record:
                chosen = record["worktree"]
                break
        if chosen is not None:
            break
    if chosen is None:
        return Stop("run-dir-unresolved", f"{worktree}: no main or master worktree found")
    main = os.path.realpath(chosen)
    here = os.path.realpath(worktree)
    if not os.path.isdir(main) or main == here or os.path.dirname(main) != os.path.dirname(here):
        return Stop("run-dir-unresolved", f"{worktree}: the main or master worktree {chosen} is not its sibling checkout")
    return main
