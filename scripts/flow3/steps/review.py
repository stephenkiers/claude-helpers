"""
The review stage (PLAN.md 7.5): sweep every review lens with cheap scouts, promote the lenses that found
something to full blind reviewers, verify every finding, triage into four buckets, ask the human only
what needs a human, and run the read-only measurements the driver can run safely.

A round's progress is saved in state.json `data.review` after each phase, so re-entry (after a park,
a stop or a crash) never redoes a finished phase:

    data.review = {"rounds": [<finished round>...], "phase": <last saved phase>, "current": {...} or absent}

`current` is the round in progress: {"round", "from", "to", then, as phases finish, "sweep", "findings",
"verified", "triage", "answers_in" (answers taken), "measurements"}.
"""

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple

from scripts.flow3 import config, gitops, heal, principles, prompts, questions, record, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.state import FlowDir
from scripts.flow3.steps import common
from scripts.flow3.types import Answer, CallSpec, StageResult

STAGE = "review"
READ_TOOLS = ("Read", "Grep", "Glob")
DIFF_CAP = 60000
MAX_PARALLEL = 6
SWEEP_GROUPS = 6
VERIFY_BATCH_LIMIT = 12
ALWAYS_PROMOTED = ("Security Sage", "North Star Nick")
BUCKETS = ("needs_you", "doing_it", "needs_measurement", "deferred")
MEASURE_TIMEOUT_SECS = 120
MEASURE_OUTPUT_CHARS = 4000

Doc = Dict[str, Any]


# ---------------------------------------------------------------------------------------------
# Lenses
# ---------------------------------------------------------------------------------------------

def lens_groups(entries: List[Doc], groups: int = SWEEP_GROUPS) -> List[List[Doc]]:
    """
    Split lens entries into about `groups` groups of near-equal size, keeping their order. Every entry
    is in exactly one group; there are never more groups than entries, and never an empty group.
    """
    if not entries:
        return []
    count = max(1, min(groups, len(entries)))
    base, extra = divmod(len(entries), count)
    out: List[List[Doc]] = []
    start = 0
    for i in range(count):
        size = base + (1 if i < extra else 0)
        out.append(list(entries[start:start + size]))
        start += size
    return out


def _lens_text(entry: Doc) -> str:
    return "- %s: %s" % (entry.get("name"), str(entry.get("useWhen") or "general review").strip())


# ---------------------------------------------------------------------------------------------
# Read-only measurement commands
# ---------------------------------------------------------------------------------------------

_FORBIDDEN_CHARS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "\0")
_GIT_READ = ("diff", "log", "show", "status", "grep", "ls-files", "rev-parse", "blame")
_PLAIN_READ = ("grep", "ls", "cat", "wc", "head", "tail")
_BAD_FIND = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls")


def _escapes_worktree(arg: str) -> bool:
    return arg.startswith("/") or arg.startswith("~") or ".." in arg.replace("\\", "/").split("/")


def is_read_only_command(cmd: str) -> bool:
    """
    Conservative check that a command only reads and stays inside the worktree. True only when it has
    no shell metacharacter (; & | > < backtick $( newline), parses with shlex, starts with an allowlisted
    program (git with diff/log/show/status/grep/ls-files/rev-parse/blame, grep, rg, ls, cat, wc, head,
    tail, find), and carries no argument that writes (--output, find -delete/-exec, ...), runs another
    program (--pre, --ext-diff), or reaches outside the worktree (absolute path, ~, ..). Test runners
    (pytest) are not allowed: tests can write. Never raises.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    if any(ch in cmd for ch in _FORBIDDEN_CHARS):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    if not words:
        return False
    prog, args = words[0], words[1:]
    if prog == "git":
        if not args or args[0] not in _GIT_READ:
            return False
        if any(a.startswith(("--output", "--ext-diff", "--textconv", "--open-files-in-pager", "--exec-path"))
               or (args[0] == "grep" and a.startswith("-O")) for a in args):
            return False
    elif prog == "find":
        if any(a in _BAD_FIND or a.startswith("-fprint") for a in args):
            return False
    elif prog == "rg":
        if any(a == "-z" or a.startswith(("--pre", "--search-zip")) for a in args):
            return False
    elif prog == "tail":
        if any(a.startswith("--follow") or (a.startswith("-") and not a.startswith("--") and "f" in a.lower())
               for a in args):
            return False
    elif prog not in _PLAIN_READ:
        return False
    return not any(_escapes_worktree(a) for a in args)


def _run_measurement(ctx: Ctx, item: Doc) -> Doc:
    fid = str(item.get("finding_id") or "")
    cmd = str(item.get("command") or "")
    if not (item.get("read_only") is True and is_read_only_command(cmd)):
        return {"finding_id": fid, "command": cmd, "exit_code": None, "output": "", "ran": False,
                "confirms_if": str(item.get("confirms_if") or "")}
    try:
        proc = subprocess.run(shlex.split(cmd), cwd=str(ctx.worktree), timeout=MEASURE_TIMEOUT_SECS,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        code: Optional[int] = proc.returncode
        out = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        code, out = None, "timed out after %d s\n%s" % (MEASURE_TIMEOUT_SECS, (exc.stdout or b"").decode("utf-8", "replace"))
    except OSError as exc:
        code, out = None, "could not run: %s" % exc
    if len(out) > MEASURE_OUTPUT_CHARS:
        out = out[:MEASURE_OUTPUT_CHARS] + "\n[output cut at %d characters]" % MEASURE_OUTPUT_CHARS
    return {"finding_id": fid, "command": cmd, "exit_code": code, "output": out, "ran": True,
            "confirms_if": str(item.get("confirms_if") or "")}


# ---------------------------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------------------------

def _save(flow: FlowDir, current: Optional[Doc], phase: str) -> None:
    data = dict(flow.data(STAGE))
    data.setdefault("rounds", [])
    if current is None:
        data.pop("current", None)
    else:
        data["current"] = current
    data["phase"] = phase
    flow.set_data(STAGE, data)


def human_fix_ids(rnd: Doc) -> List[str]:
    """Findings of a review round the human answered `fix`."""
    answers = rnd.get("answers") or {}
    return [str(k) for k, a in answers.items()
            if isinstance(a, dict) and a.get("source") == "human" and a.get("option_id") == "fix"]


def _unhandled_sendback(flow: FlowDir) -> bool:
    sb = flow.data("sendback")
    return bool(sb) and bool(str(sb.get("notes") or "").strip()) and not sb.get("handled")


def _cap_diff(diff: str) -> Tuple[str, str]:
    """(diff text for a prompt, a note about the cut or "")."""
    if len(diff) <= DIFF_CAP:
        return diff, ""
    return diff[:DIFF_CAP], ("NOTE: the diff is long and was cut at %d characters. The changed files are "
                             "listed; read them (Read, Grep, Glob) to see the rest." % DIFF_CAP)


def _call_all(ctx: Ctx, specs: List[CallSpec]) -> List[Doc]:
    """Run read-only calls in parallel; the outputs, in the order of the specs. A failed call raises."""
    if not specs:
        return []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        results = list(pool.map(lambda s: heal.call(ctx, s), specs))
    return [r.output or {} for r in results]


def _spec(ctx: Ctx, role: str, prompt: str, schema: Doc, label: str) -> CallSpec:
    return CallSpec(role=role, prompt=prompt, schema=schema, cwd=ctx.worktree, tools=READ_TOOLS,
                    allowed_tools=READ_TOOLS, label=label)


# ---------------------------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------------------------

def _sweep(ctx: Ctx, lenses: List[Doc], diff: str, files: List[str]) -> Dict[str, List[Doc]]:
    """One scout call per lens group. Returns candidates per lens name (a key for every lens)."""
    text, note = _cap_diff(diff)
    groups = lens_groups(lenses)
    specs = [
        _spec(ctx, "review_scout",
              prompts.render("review_scout", lenses="\n".join(_lens_text(e) for e in g), files="\n".join(files),
                             diff=text, note=note),
              schemas.REVIEW_SCOUT, "sweep-%d" % i)
        for i, g in enumerate(groups)
    ]
    outputs = _call_all(ctx, specs)
    found: Dict[str, List[Doc]] = {str(e["name"]): [] for e in lenses}
    for group, out in zip(groups, outputs):
        names = {str(e["name"]).casefold(): str(e["name"]) for e in group}
        for entry in out.get("lenses") or []:
            if not isinstance(entry, dict):
                continue
            name = names.get(str(entry.get("lens") or "").casefold())
            if name is None:
                continue
            for cand in entry.get("candidates") or []:
                if isinstance(cand, dict):
                    found[name].append(cand)
    return found


def _promote(ctx: Ctx, n: int, lenses: List[Doc], sweep: Dict[str, List[Doc]], diff: str,
             files: List[str]) -> Tuple[List[Doc], List[str]]:
    """
    Full blind reviewers for lenses with candidates, plus the always-on ones. Returns the driver-numbered
    findings and the names of the reviewers that ran.
    """
    text, note = _cap_diff(diff)
    chosen = [e for e in lenses if sweep.get(str(e["name"])) or str(e["name"]) in ALWAYS_PROMOTED]
    directions = voice.directions_text(ctx.flow)
    specs = []
    for e in chosen:
        name = str(e["name"])
        cands = sweep.get(name) or []
        leads = "\n".join(
            "- %s%s: %s" % (c.get("file") or "(no file)", (":%s" % c["line"]) if c.get("line") else "", c.get("note"))
            for c in cands) or "(none: the pre-pass found nothing for your lens; review the diff yourself)"
        prompt = prompts.render("reviewer", reviewer_name=name, persona=common.persona_text(e), candidates=leads,
                                files="\n".join(files), diff=text, note=note, directions=directions)
        specs.append(_spec(ctx, "reviewer", prompt, schemas.REVIEWER, name))
    outputs = _call_all(ctx, specs)
    findings: List[Doc] = []
    for e, out in zip(chosen, outputs):
        for raw in out.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            f = dict(raw)
            f["id"] = "r%d-%d" % (n, len(findings) + 1)
            f["lens"] = str(e["name"])
            if f.get("severity") not in ("critical", "high", "medium", "low"):
                f["severity"] = "medium"
            findings.append(f)
    return findings, [str(e["name"]) for e in chosen]


def _finding_view(f: Doc) -> Doc:
    keys = ("id", "lens", "file", "line", "severity", "title", "detail", "suggestion")
    return {k: f[k] for k in keys if k in f}


def _verify(ctx: Ctx, findings: List[Doc], diff: str, files: List[str]) -> None:
    """Rule on every finding in place: status, evidence, command. An unruled finding is confirmed."""
    text, note = _cap_diff(diff)
    half = (len(findings) + 1) // 2
    batches = [findings[:half], findings[half:]] if len(findings) > VERIFY_BATCH_LIMIT else [findings]
    specs = [
        _spec(ctx, "verifier",
              prompts.render("verifier", findings=json.dumps([_finding_view(f) for f in b], indent=2),
                             files="\n".join(files), diff=text, note=note),
              schemas.VERIFIER, "verify-%d" % i)
        for i, b in enumerate(batches)
    ]
    ruled: Dict[str, Doc] = {}
    for out in _call_all(ctx, specs):
        for v in out.get("verdicts") or []:
            if isinstance(v, dict):
                ruled.setdefault(str(v.get("finding_id")), v)
    for f in findings:
        v = ruled.get(str(f["id"]))
        if v is None:
            f["status"], f["evidence"] = "confirmed", "the verifier did not rule on this finding; counted as confirmed"
            continue
        f["status"] = str(v.get("verdict"))
        f["evidence"] = str(v.get("evidence") or "")
        if v.get("command"):
            f["command"] = str(v["command"])


def _fix_text(f: Doc) -> str:
    return str(f.get("suggestion") or "").strip() or "%s: %s" % (f.get("title"), f.get("detail"))


def _normalize(triage: Doc, findings: List[Doc]) -> Doc:
    """
    Every non-refuted finding in exactly one bucket. Unknown ids are dropped; a duplicate keeps its first
    bucket (needs_you, doing_it, needs_measurement, deferred); an omitted one goes to doing_it when
    critical or high, else to deferred with the reason "not triaged".
    """
    live = {str(f["id"]): f for f in findings if f.get("status") != "refuted"}
    out: Doc = {b: [] for b in BUCKETS}
    out["gut_check"] = str(triage.get("gut_check") or "")
    seen: Set[str] = set()
    for bucket in BUCKETS:
        for item in triage.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("finding_id") or "")
            if fid not in live or fid in seen:
                continue
            seen.add(fid)
            out[bucket].append(dict(item))
    for fid, f in live.items():
        if fid in seen:
            continue
        if f.get("severity") in ("critical", "high"):
            out["doing_it"].append({"finding_id": fid, "fix": _fix_text(f), "files": [str(f.get("file") or "")]})
        else:
            out["deferred"].append({"finding_id": fid, "reason": "not triaged"})
    return out


def _triage(ctx: Ctx, n: int, findings: List[Doc]) -> Doc:
    live = [f for f in findings if f.get("status") != "refuted"]
    plan = dict(ctx.flow.data("plan").get("plan") or {})
    goal, plist = principles.load_for(ctx)
    last = n >= config.MAX_REVIEW_ROUNDS - 1
    last_text = ("This is the last review round allowed: findings you put in doing_it will not be fixed "
                 "automatically; they will be listed as open at the merge gate. Triage as usual.") if last else ""
    views = []
    for f in live:
        v = _finding_view(f)
        v["status"] = f.get("status")
        v["evidence"] = f.get("evidence", "")
        if f.get("command"):
            v["command"] = f["command"]
        views.append(v)
    prompt = prompts.render(
        "triage", goal=str(plan.get("goal") or "(no plan goal recorded)"),
        non_goals="\n".join("- %s" % g for g in plan.get("non_goals") or []) or "(none)",
        principles=principles.as_text(goal, plist) or "(none recorded)",
        directions=voice.directions_text(ctx.flow), last_round=last_text,
        findings=json.dumps(views, indent=2))
    spec = CallSpec(role="triage", prompt=prompt, schema=schemas.TRIAGE, cwd=ctx.worktree, tools=READ_TOOLS,
                    allowed_tools=READ_TOOLS, label="triage-%d" % n)
    out = _call_all(ctx, [spec])[0]
    return _normalize(out, findings)


# -- needs_you -------------------------------------------------------------------------------

def _build_question(item: Doc, f: Doc) -> Doc:
    """A complete QUESTION for a needs_you item; options always include `fix` and `leave`."""
    raw = item.get("question") if isinstance(item.get("question"), dict) else {}
    q: Doc = dict(raw or {})
    q["id"] = str(f["id"])
    q["question"] = str(q.get("question") or "").strip() or "Fix this finding: %s?" % f.get("title")
    q["context"] = str(q.get("context") or "").strip() or (
        "A reviewer (%s) reported this in %s: %s" % (f.get("lens"), f.get("file"), f.get("detail")))
    options = [dict(o) for o in q.get("options") or [] if isinstance(o, dict) and o.get("id")]
    have = {str(o["id"]) for o in options}
    if "fix" not in have:
        options.insert(0, {"id": "fix", "label": "Fix it",
                           "consequence": "The suggested fix is made: %s" % _fix_text(f)})
    if "leave" not in have:
        options.append({"id": "leave", "label": "Leave as-is",
                        "consequence": "Nothing changes; the finding is deferred and listed at the merge gate."})
    for o in options:
        o["label"] = str(o.get("label") or o["id"])
        o["consequence"] = str(o.get("consequence") or "(no consequence given)")
    q["options"] = options
    ids = [str(o["id"]) for o in options]
    q["recommended"] = q.get("recommended") if q.get("recommended") in ids else "fix"
    q["kind"] = q.get("kind") if q.get("kind") in ("purpose", "architecture", "behaviour", "risk", "detail", "fact") \
        else "behaviour"
    q["reversible"] = bool(q.get("reversible", True))
    views = [v for v in q.get("expert_views") or [] if isinstance(v, dict) and v.get("expert") and v.get("why")]
    if not views:
        views = [{"expert": str(f.get("lens")), "favours": "fix", "why": str(f.get("title"))}]
    q["expert_views"] = [{"expert": str(v["expert"]), "favours": str(v.get("favours") or ""), "why": str(v["why"])}
                         for v in views]
    if q["kind"] == "risk" and not str(q.get("risk") or "").strip():
        q["risk"] = str(f.get("detail"))
    q.setdefault("themes", ["review"])
    return q


def _ask(ctx: Ctx, triage: Doc, by_id: Dict[str, Doc]) -> Tuple[Dict[str, Answer], bool]:
    """Resolve the needs_you questions. (answers reached, whether cards now wait for the human)."""
    qs = [_build_question(item, by_id[str(item["finding_id"])]) for item in triage["needs_you"]]

    def repair(incomplete: List[Doc]) -> List[Doc]:
        wanted = {str(i["id"]) for i in incomplete}
        return [_build_question({}, by_id[i]) for i in wanted if i in by_id]

    auto, cards = questions.resolve(ctx, STAGE, qs, repair=repair)
    return auto, bool(cards)


def _apply_answers(triage: Doc, answers: Dict[str, Answer], by_id: Dict[str, Doc]) -> Doc:
    """Move each needs_you item by its answer: `fix` to doing_it, anything else to deferred."""
    stored: Doc = {}
    for item in list(triage["needs_you"]):
        fid = str(item["finding_id"])
        a = answers.get(fid)
        if a is None:
            continue
        f = by_id[fid]
        stored[fid] = {"option_id": a.option_id, "text": a.text, "source": a.source}
        triage["needs_you"].remove(item)
        if a.option_id == "fix":
            note = _fix_text(f)
            if a.source == "human" and a.text.strip() and a.text.strip() != "Fix it":
                note += " Human note: %s" % a.text.strip()
            triage["doing_it"].append({"finding_id": fid, "fix": note, "files": [str(f.get("file") or "")]})
        else:
            triage["deferred"].append({"finding_id": fid, "reason": "you answered: %s" % (a.text or a.option_id)})
    return stored


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

def _bucket_map(triage: Doc) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for b in BUCKETS:
        for item in triage.get(b) or []:
            out.setdefault(str(item.get("finding_id")), b)
    return out


def _sections(rnd: Doc, lens_count: int, promoted: List[str], last: bool) -> List[Tuple[str, str]]:
    triage = rnd["triage"]
    sweep = rnd.get("sweep") or {}
    with_cands = sorted(k for k, v in sweep.items() if v)
    out: List[Tuple[str, str]] = [
        ("Round %d: what was reviewed" % rnd["round"],
         "Diff %s..%s; %d lenses swept by scouts." % (rnd["from"][:10], rnd["to"][:10], lens_count)),
        ("Round %d: lenses with candidates" % rnd["round"], ", ".join(with_cands) or "none"),
        ("Round %d: promoted to a full reviewer" % rnd["round"], ", ".join(promoted) or "none"),
    ]
    lines = ["- %s [%s, %s] %s:%s %s" % (f["id"], f.get("severity"), f.get("status"), f.get("file"),
                                         f.get("line", ""), f.get("title")) for f in rnd["findings"]]
    out.append(("Round %d: findings" % rnd["round"], "\n".join(lines) or "none"))
    for b in BUCKETS:
        items = triage.get(b) or []
        text = "\n".join("- %s: %s" % (i.get("finding_id"), i.get("fix") or i.get("reason") or i.get("command") or "")
                         for i in items) or "none"
        out.append(("Round %d: %s" % (rnd["round"], b.replace("_", " ")), text))
    if last and triage.get("doing_it"):
        out.append(("Round %d: left open" % rnd["round"],
                    "This was the last review round allowed; of the doing-it items above only those you "
                    "answered `fix` are fixed. The rest are listed as open."))
    out.append(("Round %d: gut check" % rnd["round"], str(rnd.get("gut_check") or "none")))
    ms = rnd["measurements"]
    out.append(("Round %d: measurements" % rnd["round"], "\n".join(
        "- %s: `%s` %s" % (m["finding_id"], m["command"],
                          ("exit %s" % m["exit_code"]) if m["ran"] else "not run (listed at the merge gate)")
        for m in ms) or "none"))
    ans = rnd["answers"]
    out.append(("Round %d: questions and answers" % rnd["round"], "\n".join(
        "- %s: %s (%s)" % (k, v["text"], v["source"]) for k, v in ans.items()) or "none"))
    return out


def _finish(ctx: Ctx, cur: Doc, lens_count: int, last: bool) -> StageResult:
    flow = ctx.flow
    triage = cur["triage"]
    buckets = _bucket_map(triage)
    findings = []
    for f in cur["findings"]:
        g = dict(f)
        if str(f["id"]) in buckets:
            g["bucket"] = buckets[str(f["id"])]
        findings.append(g)
    rnd: Doc = {"round": cur["round"], "from": cur["from"], "to": cur["to"], "findings": findings,
                "triage": triage, "measurements": cur.get("measurements", []),
                "gut_check": triage.get("gut_check", ""), "answers": cur.get("answers", {}),
                "sweep": cur.get("sweep", {}), "promoted": list(cur.get("promoted") or []), "last_round": last}
    data = dict(flow.data(STAGE))
    data["rounds"] = list(data.get("rounds") or []) + [rnd]
    data.pop("current", None)
    data["phase"] = "done"
    flow.set_data(STAGE, data)
    record.write_step_record(flow, STAGE, _sections(rnd, lens_count, list(cur.get("promoted") or []), last))
    doing = len(triage.get("doing_it") or [])
    record.journal(flow, "Review round %d: %d findings (%d confirmed or to measure), %d to fix, %d for you, "
                         "%d to measure, %d deferred%s." % (
                             rnd["round"], len(findings), len([f for f in findings if f.get("status") != "refuted"]),
                             doing, len(triage.get("needs_you") or []), len(triage.get("needs_measurement") or []),
                             len(triage.get("deferred") or []),
                             "; last round allowed, so only the fixes you asked for are run" if last and doing else ""))
    info: Doc = {"fix_needed": bool((doing and not last) or _unhandled_sendback(flow))}
    if last and human_fix_ids(rnd):
        info["human_fix"] = True
    return StageResult("done", info)


def _empty_round(ctx: Ctx, cur: Doc) -> StageResult:
    flow = ctx.flow
    rnd: Doc = {"round": cur["round"], "from": cur["from"], "to": cur["to"], "findings": [],
                "triage": {"doing_it": [], "needs_you": [], "needs_measurement": [], "deferred": [], "gut_check": ""},
                "measurements": [], "gut_check": "", "answers": {}, "sweep": {}, "last_round": False}
    data = dict(flow.data(STAGE))
    data["rounds"] = list(data.get("rounds") or []) + [rnd]
    data.pop("current", None)
    data["phase"] = "done"
    flow.set_data(STAGE, data)
    record.write_step_record(flow, STAGE, [("Round %d: what was reviewed" % cur["round"],
                                            "Nothing: the diff %s..%s is empty." % (cur["from"][:10], cur["to"][:10]))])
    record.journal(flow, "Review round %d: the diff was empty, so there was nothing to review." % cur["round"])
    return StageResult("done", {"fix_needed": _unhandled_sendback(flow)})


# ---------------------------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------------------------

def run(ctx: Ctx) -> StageResult:
    """
    Run one review round, or resume it. Returns done with info {"fix_needed": bool}, or needs_human when
    needs_you questions wait for the human (re-enter after they are answered).
    """
    flow = ctx.flow
    data = flow.data(STAGE)
    rounds: List[Doc] = list(data.get("rounds") or [])
    cur: Optional[Doc] = data.get("current")
    if cur is None:
        n = len(rounds)
        frm = str(rounds[-1]["to"]) if rounds else str(flow.data("worktree").get("base_sha") or "")
        cur = {"round": n, "from": frm, "to": gitops.head_sha(ctx.worktree)}
        _save(flow, cur, "start")
    n = int(cur["round"])
    last = n >= config.MAX_REVIEW_ROUNDS - 1
    wt = ctx.worktree
    diff = gitops.diff(wt, str(cur["from"]), str(cur["to"]))
    if not diff.strip():
        return _empty_round(ctx, cur)
    files = gitops.diff_names(wt, str(cur["from"]), str(cur["to"]))
    lenses = common.reviewers("review")

    if "sweep" not in cur:
        cur["sweep"] = _sweep(ctx, lenses, diff, files)
        _save(flow, cur, "swept")
    if "findings" not in cur:
        cur["findings"], cur["promoted"] = _promote(ctx, n, lenses, cur["sweep"], diff, files)
        _save(flow, cur, "promoted")
    findings: List[Doc] = cur["findings"]
    if "verified" not in cur:
        if findings:
            _verify(ctx, findings, diff, files)
        cur["verified"] = True
        _save(flow, cur, "verified")
    if "triage" not in cur:
        cur["triage"] = _triage(ctx, n, findings) if [f for f in findings if f.get("status") != "refuted"] else {
            "doing_it": [], "needs_you": [], "needs_measurement": [], "deferred": [], "gut_check": ""}
        cur["answers"] = {}
        _save(flow, cur, "triaged")
    triage: Doc = cur["triage"]
    by_id = {str(f["id"]): f for f in findings}

    if triage["needs_you"] and not cur.get("asked"):
        auto, waiting = _ask(ctx, triage, by_id)
        cur["asked"] = True
        if waiting:
            _save(flow, cur, "waiting_answers")
            return StageResult("needs_human")
        cur["answers"] = _apply_answers(triage, auto, by_id)
        _save(flow, cur, "answered")
    elif triage["needs_you"] and cur.get("asked"):
        taken = questions.take_answers(ctx, STAGE)
        cur["answers"] = _apply_answers(triage, taken, by_id)
        _save(flow, cur, "answered")

    if "measurements" not in cur:
        cur["measurements"] = [_run_measurement(ctx, item) for item in triage["needs_measurement"]]
        _save(flow, cur, "measured")
    return _finish(ctx, cur, len(lenses), last)


# ---------------------------------------------------------------------------------------------
# Readers for the fix stage and the merge gate
# ---------------------------------------------------------------------------------------------

def _fixed_ids(flow: FlowDir) -> Set[str]:
    out: Set[str] = set()
    for r in flow.data("fix").get("rounds") or []:
        out.update(str(i) for i in r.get("fixed") or [])
    return out


def open_findings(flow: FlowDir) -> List[Doc]:
    """
    Findings still unresolved across all finished review rounds. Each is the finding dict (id, lens, file,
    line, severity, title, detail, suggestion, status, evidence, bucket) plus:
      "round": the review round that found it,
      "reason": why it is open: "not fixed" (a doing_it item no fix round fixed, including one left open
        because it came in the last allowed review round) or "measurement not run" (a needs_measurement
        item the driver could not or did not run, with "command" and "confirms_if" set).
    Deferred findings, refuted findings, fixed findings and measurements that ran are not open.
    """
    fixed = _fixed_ids(flow)
    out: List[Doc] = []
    for rnd in flow.data(STAGE).get("rounds") or []:
        ran = {str(m.get("finding_id")): m for m in rnd.get("measurements") or [] if m.get("ran")}
        measure = {str(i.get("finding_id")): i for i in rnd.get("triage", {}).get("needs_measurement") or []}
        for f in rnd.get("findings") or []:
            fid, bucket = str(f["id"]), f.get("bucket")
            g = dict(f)
            g["round"] = rnd["round"]
            if bucket == "doing_it" and fid not in fixed:
                g["reason"] = "not fixed"
            elif bucket == "needs_measurement" and fid not in ran:
                g["reason"] = "measurement not run"
                g["command"] = str(measure.get(fid, {}).get("command") or g.get("command") or "")
                g["confirms_if"] = str(measure.get(fid, {}).get("confirms_if") or "")
            else:
                continue
            out.append(g)
    return out


def summary(flow: FlowDir) -> Doc:
    """
    Counts across all finished rounds:
      {"rounds": n, "findings": non-refuted total, "refuted": n, "fixed": doing_it findings a fix round
       fixed, "open": len(open_findings), "deferred": n, "needs_measurement": n found,
       "measurements_not_run": n, "needs_you_pending": 0}
    "needs_you_pending" is always 0 once a round is finished (every needs_you item is answered).
    """
    rounds = flow.data(STAGE).get("rounds") or []
    fixed = _fixed_ids(flow)
    live = [f for r in rounds for f in r.get("findings") or [] if f.get("status") != "refuted"]
    refuted = [f for r in rounds for f in r.get("findings") or [] if f.get("status") == "refuted"]
    opened = open_findings(flow)
    return {
        "rounds": len(rounds),
        "findings": len(live),
        "refuted": len(refuted),
        "fixed": len([f for f in live if f.get("bucket") == "doing_it" and str(f["id"]) in fixed]),
        "open": len(opened),
        "deferred": len([f for f in live if f.get("bucket") == "deferred"]),
        "needs_measurement": len([f for f in live if f.get("bucket") == "needs_measurement"]),
        "measurements_not_run": len([f for f in opened if f.get("reason") == "measurement not run"]),
        "needs_you_pending": 0,
    }
