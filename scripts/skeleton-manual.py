#!/usr/bin/env python3
"""Helper for the human-only v2 walking-skeleton measurements (Sub-issue S, #484).

Subcommands: setup, cells, collect <flow-dir>|--latest, teardown. Stdlib only. It never edits the findings
doc. See docs/v2-inventory/skeleton-runbook.md ("Human cells: quick path").
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKTREE = Path(__file__).resolve().parent.parent
REPO = "skeleton/measure"
REPO_KEY = "skeleton_measure"
FLAGS = f"--repo {REPO} --mode interactive --wait-mode end-turn --step-minutes 1"
# WAIT is called for by these --after events; any other WAIT is a re-check of a live record.
WAIT_CALLED_FOR = {"spawn-launched", "resume-sent"}


class SkeletonError(Exception):
    """A refusal: the requested action is unsafe or the input is not a skeleton flow dir."""


def home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def link_targets(h: Path) -> List[Any]:
    """(link path, target path) for the two temporary symlinks."""
    return [
        (h / ".claude" / "commands" / "expert-flow-v2.md", WORKTREE / "commands" / "expert-flow-v2.md"),
        (h / ".claude" / "scripts" / "flow_driver", WORKTREE / "scripts" / "flow_driver"),
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(text: str) -> str:
    """Home -> ~, worktree -> <worktree> (worktree first: it usually lies under home)."""
    out = text.replace(str(WORKTREE), "<worktree>")
    return out.replace(str(home()), "~")


def flows_root(h: Path) -> Path:
    return h / ".claude" / "flows-v2"


# ---------------------------------------------------------------- setup

def cmd_setup(_args: argparse.Namespace) -> int:
    h = home()
    pairs = link_targets(h)
    for link, _target in pairs:
        if os.path.lexists(str(link)):
            print(f"refusing: {scrub(str(link))} already exists; run teardown or remove it by hand",
                  file=sys.stderr)
            return 1
    for link, target in pairs:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(link))
        print(f"linked {scrub(str(link))} -> {scrub(str(target))}")
    try:
        version = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        version = f"unavailable ({exc})"
    scratch = tempfile.mkdtemp(prefix="skeleton-scratch-")
    print(f"links created (UTC): {utc_now()}")
    print(f"claude --version: {version}")
    print(f"scratch dir: {scratch}")
    print("next: cd into the scratch dir, start `claude`, then run `skeleton-manual.py cells`")
    return 0


# ---------------------------------------------------------------- cells

def cells_text() -> str:
    inv = f"/expert-flow-v2 {FLAGS}"
    resume = f"{inv} --flow <FLOW_DIR>"
    out = [
        "Run every cell in an interactive `claude` started from the scratch dir (cd there first).",
        "Use a fresh `claude` session per run. <FLOW_DIR> is the path the stub prints (NEXT: ... line / answer_command).",
        "After each run: `python3 scripts/skeleton-manual.py collect --latest` (or collect <FLOW_DIR>).",
        "",
        "R1 happy path (do it 3 times; informs AC1-SPAWN, AC1-RESUME, AC1-WAIT, AC1-ASK, RESUME-a)",
        f"  1. Paste: {inv}",
        "  2. Let the stub SPAWN and WAIT (end the turn). Wait for the question (ASK) to be printed.",
        "  3. Copy the printed answer_command; run it in a separate terminal (or paste it back to the session to run).",
        "  4. In the same session type: continue   (the stub runs next --after human-continue, then RESUME).",
        "  5. Wait until DONE prints (about 1 minute; heartbeat reaches `done`). Note the flow dir.",
        "",
        "R2 K1-interrupt (3 times; informs AC2-K1-interrupt, PHANTOM-LIVE, LATE-RECEIPT)",
        f"  1. Paste: {inv}",
        "  2. The moment `NEXT: SPAWN` appears, press Ctrl+C (before the agent is launched).",
        "  3. Note the flow dir (ls ~/.claude/flows-v2/skeleton_measure/).",
        f"  4. Re-run in the same session: {resume}",
        "  5. If it only WAITs and nothing progresses for ~2 minutes, run the printed clear-live command"
        " (`... clear-live --flow <FLOW_DIR> --reason interrupt-K1`) and re-run step 4 once.",
        "",
        "R2 K2-interrupt (3 times; informs AC2-K2-interrupt, PHANTOM-LIVE, ORPHAN-OVERLAP, LATE-RECEIPT)",
        f"  1. Paste: {inv}",
        "  2. Wait until `<FLOW_DIR>/spawns.jsonl` has a line (agent launched), then press Ctrl+C before the receipt.",
        f"  3. Re-run in the same session: {resume}",
        "  4. If it only WAITs for ~2 minutes with no receipt, run clear-live (reason interrupt-K2) and re-run step 3 once.",
        "  5. Let the second attempt finish (answer its question as in R1 if asked), then wait ~1 more minute so the"
        " orphaned heartbeat of attempt 1 can show up.",
        "",
        "R6 compaction (once; informs COMPACTION, RESUME-c; skip if you do not want to measure it)",
        f"  1. Paste: {inv}",
        "  2. While the stub is parked/WAITing with the question printed, type /compact and note the UTC time.",
        "  3. After compaction finishes, run the answer_command, then type: continue",
        "  4. Note whether the stub still held the agent id (RESUME succeeded) or printed RESUME FAILED.",
        "",
        "Findings rows: AC1-*, AC2-K1-interrupt, AC2-K2-interrupt, AC3, PHANTOM-LIVE, ORPHAN-OVERLAP, LATE-RECEIPT,",
        "RESUME-a, RESUME-c, COMPACTION. Record N actually run; never pad.",
    ]
    return "\n".join(out)


def cmd_cells(_args: argparse.Namespace) -> int:
    print(cells_text())
    return 0


# ---------------------------------------------------------------- collect

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _attempt_of(name: str) -> Optional[int]:
    tail = name.rsplit("-a", 1)[-1] if "-a" in name else ""
    return int(tail) if tail.isdigit() else None


def analyze(flow: Path) -> Dict[str, Any]:
    """Read-only evidence for one flow dir, as plain data."""
    spawns = read_jsonl(flow / "spawns.jsonl")
    log = read_jsonl(flow / "driver.log")
    spawn_counts: Dict[str, int] = {}
    for row in spawns:
        key = f"{row.get('step')}/a{row.get('attempt')}"
        spawn_counts[key] = spawn_counts.get(key, 0) + 1
    double_spawn = sorted(k for k, n in spawn_counts.items() if n > 1)

    verbs: Dict[str, int] = {}
    wait_excess = 0
    attempts_logged = set()
    for row in log:
        verb = row.get("verb")
        if not isinstance(verb, str):
            continue
        verbs[verb] = verbs.get(verb, 0) + 1
        if verb == "WAIT" and row.get("after") not in WAIT_CALLED_FOR:
            wait_excess += 1
        if verb == "SPAWN":
            attempts_logged.add(row.get("attempt"))
    questions = verbs.get("ASK", 0) + verbs.get("PARK", 0)
    excess = {
        "WAIT": wait_excess,
        "SPAWN": max(0, verbs.get("SPAWN", 0) - len(attempts_logged)),
        "ASK/PARK": max(0, questions - len(attempts_logged)),
        "RESUME": max(0, verbs.get("RESUME", 0) - questions),
    }
    events = sorted({str(e.get("event")) for row in log for e in (row.get("events") or []) if isinstance(e, dict)})
    clear_live = [row for row in log if row.get("subcommand") == "clear-live"]

    pending: Optional[Dict[str, Any]] = None
    ppath = flow / "pending_spawn.json"
    if ppath.is_file():
        try:
            loaded = json.loads(ppath.read_text(encoding="utf-8"))
            pending = loaded if isinstance(loaded, dict) else None
        except ValueError:
            pending = {"state": "unreadable"}
    receipts_dir = flow / "receipts"
    receipts = sorted(p.name for p in receipts_dir.iterdir() if p.is_file()) if receipts_dir.is_dir() else []
    phantom = False
    if pending is not None and pending.get("state") in ("spawned", "resumed"):
        att = pending.get("attempt")
        has_spawn = any(r.get("attempt") == att for r in spawns)
        has_receipt = f"skeleton-a{att}.md" in receipts
        phantom = (not has_spawn) and (not has_receipt)

    hb: Dict[str, Dict[str, Any]] = {}
    for p in sorted(flow.glob("heartbeat-a*")):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        hb[p.name] = {
            "lines": len(lines),
            "done": bool(lines) and lines[-1].strip() == "done",
            "mtime": p.stat().st_mtime,
            "last": lines[-1].strip() if lines else "",
        }
    orphans: List[str] = []
    for name, info in hb.items():
        m = _attempt_of(name)
        if m is None:
            continue
        for row in spawns:
            n = row.get("attempt")
            utc = row.get("utc")
            if isinstance(n, int) and n > m and isinstance(utc, str):
                try:
                    spawn_t = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                if info["mtime"] > spawn_t:
                    orphans.append(f"{name} last written {utc_from(info['mtime'])}, after a{n} spawn line {utc}")
    receipt_text = {}
    for name in receipts:
        receipt_text[name] = (receipts_dir / name).read_text(encoding="utf-8", errors="replace")
    return {
        "flow": flow, "spawn_counts": spawn_counts, "double_spawn": double_spawn, "verbs": verbs,
        "excess": excess, "events": events, "clear_live": len(clear_live), "pending": pending,
        "phantom_live": phantom, "heartbeats": hb, "orphans": sorted(set(orphans)), "receipts": receipt_text,
    }


def utc_from(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def informs(a: Dict[str, Any]) -> List[str]:
    ids = ["AC1-SPAWN", "AC1-WAIT", "AC1-ASK", "AC1-RESUME", "RESUME-a", "AC3", "SPAWN-XCHECK"]
    if a["clear_live"] or a["phantom_live"] or a["orphans"] or "late-receipt" in a["events"]:
        ids += ["AC2-K1-interrupt", "AC2-K2-interrupt", "PHANTOM-LIVE", "ORPHAN-OVERLAP", "LATE-RECEIPT"]
    ids.append("COMPACTION (only if /compact was run)")
    return ids


def render(a: Dict[str, Any]) -> str:
    L: List[str] = [f"flow dir: {a['flow']}"]
    sc = a["spawn_counts"]
    L.append("spawns.jsonl lines per (step/attempt): " + (", ".join(f"{k}={n}" for k, n in sorted(sc.items())) or "none"))
    L.append("double-spawn: " + (", ".join(a["double_spawn"]) if a["double_spawn"] else "none"))
    L.append("driver.log verbs: " + (", ".join(f"{k} {n}" for k, n in sorted(a["verbs"].items())) or "none"))
    ex = a["excess"]
    L.append("verbs beyond what --after events called for: "
             + (", ".join(f"{k} +{n}" for k, n in ex.items() if n) or "none"))
    L.append("driver.log events: " + (", ".join(a["events"]) or "none"))
    p = a["pending"]
    L.append("pending_spawn.json: " + (f"state={p.get('state')} attempt={p.get('attempt')}" if p else "absent"))
    L.append(f"phantom-live (live record, no spawn line, no receipt): {'YES' if a['phantom_live'] else 'no'}")
    L.append(f"clear-live entries: {a['clear_live']}")
    for name, info in sorted(a["heartbeats"].items()):
        L.append(f"{name}: {info['lines']} lines, done={'yes' if info['done'] else 'no'}, last={info['last']!r}")
    if not a["heartbeats"]:
        L.append("heartbeat-a*: none")
    L.append("orphan overlap: " + ("; ".join(a["orphans"]) if a["orphans"] else "none"))
    if a["receipts"]:
        for name, text in sorted(a["receipts"].items()):
            L.append(f"receipt {name}:")
            L.extend("    " + ln for ln in text.splitlines())
    else:
        L.append("receipts: none")
    L.append("informs findings rows: " + ", ".join(informs(a)))
    return scrub("\n".join(L))


def latest_flow(h: Path) -> Path:
    root = flows_root(h)
    cands = [p for key in sorted(root.glob("*")) if key.is_dir() for p in key.glob("skeleton-*") if p.is_dir()]
    if not cands:
        raise SkeletonError(f"no skeleton-* flow dirs under {scrub(str(root))}")
    return max(cands, key=lambda p: p.stat().st_mtime)


def cmd_collect(args: argparse.Namespace) -> int:
    try:
        if args.latest:
            flow = latest_flow(home())
        elif args.flow_dir:
            flow = Path(args.flow_dir).expanduser()
        else:
            raise SkeletonError("give a flow dir or --latest")
        if not flow.is_dir():
            raise SkeletonError(f"not a directory: {scrub(str(flow))}")
    except SkeletonError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(render(analyze(flow)))
    return 0


# ---------------------------------------------------------------- teardown

def skeleton_flow_dirs(h: Path) -> List[Path]:
    root = flows_root(h)
    found: List[Path] = []
    if not root.is_dir():
        return found
    for key in sorted(os.listdir(str(root))):
        kdir = root / key
        if kdir.is_symlink() or not kdir.is_dir():
            continue
        for name in sorted(os.listdir(str(kdir))):
            p = kdir / name
            if name.startswith("skeleton-") and p.is_dir() and not p.is_symlink():
                found.append(p)
    return found


def check_removable_flow(h: Path, path: Path) -> None:
    """Refuse anything that is not <flows-v2>/<repo_key>/skeleton-*, a real (non-symlink) directory."""
    root = flows_root(h)
    if path.is_symlink():
        raise SkeletonError(f"refusing symlink: {path}")
    if path.parent.parent != root or not path.name.startswith("skeleton-"):
        raise SkeletonError(f"refusing non-skeleton path: {path}")
    if ".." in path.parts:
        raise SkeletonError(f"refusing path with '..': {path}")


def cmd_teardown(_args: argparse.Namespace) -> int:
    h = home()
    rc = 0
    for link, target in link_targets(h):
        if not os.path.lexists(str(link)):
            print(f"absent already: {scrub(str(link))}")
        elif os.path.islink(str(link)) and os.path.realpath(str(link)) == os.path.realpath(str(target)):
            os.unlink(str(link))
            print(f"removed {scrub(str(link))}")
        else:
            print(f"refusing: {scrub(str(link))} is not the symlink setup created", file=sys.stderr)
            rc = 1
    for flow in skeleton_flow_dirs(h):
        try:
            check_removable_flow(h, flow)
        except SkeletonError as exc:
            print(scrub(str(exc)), file=sys.stderr)
            rc = 1
            continue
        print(f"removing {scrub(str(flow))}")
        shutil.rmtree(str(flow))
    leftovers = [str(link) for link, _t in link_targets(h) if os.path.lexists(str(link))]
    leftovers += [str(p) for p in skeleton_flow_dirs(h)]
    if leftovers:
        print("NOT removed: " + ", ".join(scrub(x) for x in leftovers), file=sys.stderr)
        rc = 1
    else:
        print("verified absent: both symlinks and all skeleton-* flow dirs")
    print(f"teardown finished (UTC): {utc_now()}")
    print("harness transcripts under ~/.claude/projects/ are left alone; remove the scratch dir by hand if wanted")
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skeleton-manual.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create the two symlinks, print time/version/scratch dir").set_defaults(fn=cmd_setup)
    sub.add_parser("cells", help="print the paste-ready steps for each human cell").set_defaults(fn=cmd_cells)
    col = sub.add_parser("collect", help="read-only evidence summary for one flow dir")
    col.add_argument("flow_dir", nargs="?")
    col.add_argument("--latest", action="store_true")
    col.set_defaults(fn=cmd_collect)
    sub.add_parser("teardown", help="remove the symlinks and skeleton-* flow dirs").set_defaults(fn=cmd_teardown)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
