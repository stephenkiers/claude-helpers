"""Shared helpers for the v2 driver Layer 2 test suites (step loop, recovery, ship guard).

Everything here drives the real driver CLI in a subprocess under a temp HOME, with a fake ``gh`` on
PATH and fake step agents that only write receipts into the flow dir. No model is run.

Not a test suite by itself: run_all.py only picks up test_*.py files.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from _test_harness import REPO_ROOT  # noqa: E402
from scripts.flow_driver import receipts  # noqa: E402

MAIN = REPO_ROOT / "scripts" / "flow_driver" / "__main__.py"
NEXT_LINE_RE = re.compile(r"NEXT: (SPAWN|RESUME|WAIT|ASK|PARK|STOP|DONE|MERGE) (/\S+)")
HAPPY_STEPS = ["worktree", "plan", "track", "implement", "review", "fix", "verify", "ship"]

FAKE_GH = """#!/bin/sh
# Fake gh for driver tests: serves canned `gh pr view` output from FAKE_GH_JSON.
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  case "$FAKE_GH_MODE" in
    error) echo "fake gh failure" >&2; exit 1 ;;
    nopr) echo "no pull requests found for branch" >&2; exit 1 ;;
    *) cat "$FAKE_GH_JSON"; exit 0 ;;
  esac
fi
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi
echo "unexpected gh call: $*" >&2
exit 1
"""


class FakeGh:
    """A fake gh directory on PATH. ``env`` is the dict passed to run_cli; mutate ``env`` to change mode."""

    def __init__(self, base: Path, mode: str = "nopr", json_text: Optional[str] = None) -> None:
        self.bin_dir = base / "fake-bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        gh = self.bin_dir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.json_file = base / "fake-gh-pr.json"
        self.json_file.write_text(json_text or "{}", encoding="utf-8")
        self.mode = mode

    def env(self) -> Dict[str, str]:
        return {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_MODE": self.mode,
            "FAKE_GH_JSON": str(self.json_file),
        }


def base_env(home: Path, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    if extra:
        env.update(extra)
    return env


def run_cli(home: Path, *args: str, extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """Run the real driver entry point in a subprocess with HOME pointed at a temp dir."""
    proc = subprocess.run(
        [sys.executable, str(MAIN), *args],
        capture_output=True,
        text=True,
        env=base_env(home, extra_env),
        cwd=str(home),
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def next_line(stdout: str) -> Tuple[str, Path]:
    """The stdout must be exactly one NEXT line. Returns (verb, directive file)."""
    lines = stdout.splitlines()
    if len(lines) != 1:
        raise AssertionError(f"expected exactly one stdout line, got {lines!r}")
    match = NEXT_LINE_RE.fullmatch(lines[0])
    if match is None:
        raise AssertionError(f"stdout line does not match the NEXT contract: {lines[0]!r}")
    return match.group(1), Path(match.group(2))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def flow_of(directive: Path) -> Path:
    """Directive files sit two levels below the flow dir (the skeleton layout the CLI prints)."""
    return directive.parent.parent


def state_of(flow: Path) -> Dict[str, Any]:
    return load_json(flow / "state.json")


def pending_of(flow: Path) -> Optional[Dict[str, Any]]:
    """The live spawn record. Layer 2 keeps it in state.json; fall back to the skeleton file."""
    state = state_of(flow)
    if state.get("pending_spawn"):
        return state["pending_spawn"]
    skeleton = flow / "pending_spawn.json"
    if skeleton.exists():
        return load_json(skeleton)
    return None


def log_lines(flow: Path) -> List[str]:
    log = flow / "driver-log.jsonl"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_home(base: Path) -> Path:
    """A temp HOME whose ~/.claude/scripts and ~/.claude/agents link the repo's guard hooks and agents.

    Start's hook preflight refuses to run when the guard hooks are not installed under the HOME it sees.
    """
    home = Path(tempfile.mkdtemp(dir=str(base)))
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    for entry in (REPO_ROOT / "scripts").iterdir():
        if entry.is_file():
            os.symlink(str(entry), str(scripts / entry.name))
    agents = REPO_ROOT / "agents"
    if agents.is_dir():
        os.symlink(str(agents), str(home / ".claude" / "agents"))
    prompts = REPO_ROOT / "prompts"
    if prompts.is_dir():
        os.symlink(str(prompts), str(home / ".claude" / "prompts"))
    # The per-spawn agent preflight reads settings.json for the three guard hooks registered with `--agent auto`.
    guards = ("flow-bash-guard.py", "flow-skill-guard.py", "flow-agent-guard.py")
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": f"{scripts / g} --agent auto"} for g in guards]}
            ]
        }
    }
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (home / "wt").mkdir()
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for git_args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(home / "wt"), *git_args], check=True, capture_output=True, env=git_env)
    return home


def write_receipt(flow: Path, step: str, attempt: int, decision: str = "OK", extra: str = "") -> None:
    """A fake step agent's receipt in the Layer 1 format (ATTEMPT trailer, step-end marker)."""
    path = receipts.receipt_path(flow, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ATTEMPT: {attempt}\nDecision: {decision}\n{extra}<!-- step-end -->\nbody for {step}\n",
        encoding="utf-8",
    )


def start_flow(home: Path, extra_env: Optional[Dict[str, str]] = None) -> Tuple[Path, Dict[str, Any]]:
    """Create a v2 flow dir with init, then start it. Returns (flow dir, the worktree SPAWN directive)."""
    flow = home / ".claude" / "flows-v2" / "owner_name" / "486-probe-abc123"
    code, out, err = run_cli(
        home, "init", "--flow", str(flow), "--worktree", str(home / "wt"), "--repo", "owner/name",
        extra_env=extra_env,
    )
    if code != 0:
        raise AssertionError(f"init exited {code}: {err}")
    code, out, err = run_cli(home, "start", "--flow", str(flow), extra_env=extra_env)
    if code != 0:
        raise AssertionError(f"start exited {code}: {err}")
    verb, directive = next_line(out)
    if verb != "SPAWN":
        reason = load_json(directive).get("reason", "")
        raise AssertionError(f"start printed {verb}, expected SPAWN: {reason}")
    return flow, load_json(directive)


def directive_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def drive_happy_path(
    home: Path,
    flow: Path,
    current: Dict[str, Any],
    env: Dict[str, str],
    before_next: Optional[Dict[str, Any]] = None,
    max_steps: int = 30,
) -> Tuple[str, Path, List[str]]:
    """Answer each SPAWN with an OK receipt and call ``next --after notification``.

    ``before_next`` maps a step name to a callable run before the ``next`` call that follows that step's
    receipt (used to change the fake gh mode). Returns (last verb, last directive, steps spawned in order).
    """
    before_next = before_next or {}
    spawned: List[str] = []
    for _ in range(max_steps):
        step = current["step"]
        spawned.append(step)
        extra = f"WORKTREE: {home / 'wt'}\n" if step == "worktree" else ""
        write_receipt(flow, step, current["attempt"], extra=extra)
        if step in before_next:
            before_next[step]()
        code, out, err = run_cli(home, "next", "--flow", str(flow), "--after", "notification", extra_env=env)
        if code != 0:
            raise AssertionError(f"next exited {code} after {step}: {err}")
        verb, directive = next_line(out)
        if verb != "SPAWN":
            return verb, directive, spawned
        current = load_json(directive)
    raise AssertionError(f"the loop did not stop within {max_steps} spawns: {spawned}")
