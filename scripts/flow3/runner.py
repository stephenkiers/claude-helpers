"""
The only module that launches `claude` (PLAN.md sections 3 and 10).

`run` makes ONE launch and never retries or raises for a failed call: it returns a `CallResult`
describing what happened. Healing lives in heal.py. Concurrent calls across every flow on the machine
share a fixed number of slots (file locks) and a shared back-off file written when a usage limit shows.
"""

import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import IO, Any, Dict, List, Mapping, Optional, Protocol

from scripts.flow3 import config, schemas
from scripts.flow3.paths import flow_home
from scripts.flow3.types import CallResult, CallSpec

# Replaced in tests so waits do not take real time.
_sleep = time.sleep

_KILL_GRACE_SECS = 3.0
_SLOT_POLL_SECS = 0.05
_OK_LIMIT_STATUSES = ("allowed", "allowed_warning")


class CallSink(Protocol):
    """What `run` needs from a flow directory (state.FlowDir satisfies it)."""

    def next_call_dir(self, role: str) -> Path: ...

    def event(self, kind: str, **fields: Any) -> None: ...


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------

def _load_json(stdout: str) -> Any:
    if not stdout or not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return None


def _events(stdout: str) -> List[Dict[str, Any]]:
    data = _load_json(stdout)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def parse_events(stdout: str) -> Optional[Dict[str, Any]]:
    """The last "result" event in stdout, or None (empty, non-JSON, or no result event)."""
    found: Optional[Dict[str, Any]] = None
    for event in _events(stdout):
        if event.get("type") == "result":
            found = event
    return found


def rate_limit_info(stdout: str) -> Optional[Dict[str, Any]]:
    """The `rate_limit_info` of the last rate_limit_event, or None."""
    found: Optional[Dict[str, Any]] = None
    for event in _events(stdout):
        info = event.get("rate_limit_info")
        if event.get("type") == "rate_limit_event" and isinstance(info, dict):
            found = info
    return found


# ---------------------------------------------------------------------------------------------
# Shared back-off
# ---------------------------------------------------------------------------------------------

def _limit_path() -> Path:
    return flow_home() / "limit.json"


def note_limit(until_epoch: float) -> None:
    """Record that no call should start before `until_epoch`. Keeps the later of old and new."""
    path = _limit_path()
    lock_path = path.with_name("limit.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = _read_limit(path)
            until = max(current, float(until_epoch))
            tmp = path.with_name("limit.json.%d.tmp" % os.getpid())
            tmp.write_text(json.dumps({"until": until}))
            os.replace(str(tmp), str(path))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_limit(path: Path) -> float:
    try:
        data = json.loads(path.read_text())
        return float(data.get("until", 0.0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


def limit_wait_remaining() -> float:
    """Seconds until the shared back-off ends; 0.0 when there is none."""
    return max(0.0, _read_limit(_limit_path()) - time.time())


# ---------------------------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------------------------

def _slot_count() -> int:
    try:
        n = int(os.environ.get("FLOW3_SLOTS", ""))
    except ValueError:
        n = config.DEFAULT_SLOTS
    return n if n > 0 else config.DEFAULT_SLOTS


def _take_slot() -> IO[str]:
    """Block until one of the shared slots is free; return the open, locked file."""
    slots = flow_home() / "slots"
    slots.mkdir(parents=True, exist_ok=True)
    count = _slot_count()
    while True:
        for i in range(count):
            fh = open(slots / ("slot-%d.lock" % i), "a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                fh.close()
        _sleep(_SLOT_POLL_SECS)


def _release_slot(fh: IO[str]) -> None:
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# ---------------------------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------------------------

def _binary() -> str:
    return os.environ.get("FLOW3_CLAUDE_BIN") or "claude"


def build_argv(spec: CallSpec, model: str) -> List[str]:
    """The full argument vector, prompt last."""
    max_turns = spec.max_turns if spec.max_turns is not None else config.limits_for(spec.role)[0]
    argv = [_binary(), "-p", "--model", model, "--output-format", "json", "--safe-mode",
            "--strict-mcp-config", "--disable-slash-commands", "--permission-mode", "dontAsk",
            "--max-turns", str(max_turns)]
    if spec.schema:
        argv += ["--json-schema", json.dumps(spec.schema, separators=(",", ":"))]
    argv += ["--tools", ",".join(spec.tools)]
    for entry in spec.allowed_tools:
        argv += ["--allowedTools", entry]
    if spec.resume_session:
        argv += ["--resume", spec.resume_session]
    argv.append(spec.prompt)
    return argv


def _kill_group(proc: "subprocess.Popen[str]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + _KILL_GRACE_SECS
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _launch(argv: List[str], spec: CallSpec, timeout: int) -> Dict[str, Any]:
    env = dict(os.environ)
    env["FLOW3_ROLE"] = spec.role
    env["FLOW3_LABEL"] = spec.label
    started = time.time()
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(spec.cwd), env=env, text=True, start_new_session=True)
    except OSError as exc:
        return {"stdout": "", "stderr": "could not launch %s: %s" % (argv[0], exc), "code": 127,
                "timed_out": False, "wall_ms": 0}
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        out, err = proc.communicate()
    return {"stdout": out or "", "stderr": err or "", "code": proc.returncode,
            "timed_out": timed_out, "wall_ms": int((time.time() - started) * 1000)}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run(spec: CallSpec, flow: Optional[CallSink] = None,
        overrides: Optional[Mapping[str, str]] = None) -> CallResult:
    """One launch of `claude`. Never retries; never raises for a failed call."""
    model = spec.model or config.model_for(spec.role, overrides or {})
    default_turns, default_timeout = config.limits_for(spec.role)
    timeout = spec.timeout_secs if spec.timeout_secs is not None else default_timeout
    argv = build_argv(spec, model)

    wait = limit_wait_remaining()
    if wait > 0:
        _sleep(wait)
    slot = _take_slot()
    try:
        launched = _launch(argv, spec, timeout)
    finally:
        _release_slot(slot)

    stdout: str = launched["stdout"]
    event = parse_events(stdout)
    info = rate_limit_info(stdout)
    if info is not None:
        resets = info.get("resetsAt")
        if info.get("status") not in _OK_LIMIT_STATUSES and isinstance(resets, (int, float)):
            note_limit(float(resets))

    errors: List[str] = []
    output: Optional[Dict[str, Any]] = None
    structured: Any = None
    if event is not None:
        structured = event.get("structured_output")
        if spec.schema:
            if isinstance(structured, dict):
                errors = schemas.validate(structured, spec.schema)
                output = structured
            else:
                errors = ["no structured output returned"]
    is_error = bool(event.get("is_error")) if event else False
    ok = (launched["code"] == 0 and event is not None and not is_error and not launched["timed_out"]
          and (not spec.schema or (isinstance(structured, dict) and not errors)))
    denials = event.get("permission_denials") if event else None
    api_status = event.get("api_error_status") if event else None
    duration = _to_int(event.get("duration_ms")) if event else launched["wall_ms"]
    result_text = event.get("result") if event else ""

    result = CallResult(
        ok=ok, role=spec.role, model=model, output=output,
        session_id=(event.get("session_id") if event else None) or None,
        exit_code=launched["code"] if launched["code"] is not None else -1,
        subtype=str(event.get("subtype") or "") if event else "",
        is_error=is_error, terminal_reason=str(event.get("terminal_reason") or "") if event else "",
        api_error_status=_to_int(api_status) if api_status is not None else None,
        num_turns=_to_int(event.get("num_turns")) if event else 0,
        cost_usd=_to_float(event.get("total_cost_usd")) if event else 0.0,
        duration_ms=duration,
        permission_denials=tuple(d for d in denials if isinstance(d, dict)) if isinstance(denials, list) else (),
        rate_limit=info, result_text=result_text if isinstance(result_text, str) else "",
        stderr=launched["stderr"], timed_out=launched["timed_out"], parsed=event is not None,
        schema_errors=tuple(errors))

    if flow is not None:
        result = _record(flow, spec, argv, stdout, structured, result)
    return result


def _record(flow: CallSink, spec: CallSpec, argv: List[str], stdout: str, structured: Any,
            result: CallResult) -> CallResult:
    call_dir = flow.next_call_dir(spec.role)
    shown = list(argv)
    shown[-1] = "<prompt.md>"
    (call_dir / "prompt.md").write_text(spec.prompt)
    (call_dir / "argv.json").write_text(json.dumps(shown, indent=2))
    if _load_json(stdout) is not None:
        (call_dir / "raw.json").write_text(stdout)
    else:
        (call_dir / "raw.txt").write_text(stdout)
    (call_dir / "output.json").write_text(json.dumps(structured if isinstance(structured, dict) else None, indent=2))
    (call_dir / "stderr.txt").write_text(result.stderr)
    meta: Dict[str, Any] = {"label": spec.label}
    for name in ("ok", "role", "model", "session_id", "exit_code", "subtype", "is_error",
                 "terminal_reason", "api_error_status", "num_turns", "cost_usd", "duration_ms",
                 "timed_out", "parsed"):
        meta[name] = getattr(result, name)
    meta["schema_errors"] = list(result.schema_errors)
    (call_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    flow.event("call", role=spec.role, label=spec.label, model=result.model, cost_usd=result.cost_usd,
               duration_ms=result.duration_ms, num_turns=result.num_turns, ok=result.ok,
               subtype=result.subtype, session_id=result.session_id, call_dir=call_dir.name)
    return CallResult(**{**result.__dict__, "call_dir": call_dir.name})
