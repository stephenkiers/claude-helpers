"""
The fix stage (PLAN.md section 7.6).

Input: the latest review round's `doing_it` findings that no earlier fix round fixed, plus unhandled
send-back notes from the merge gate. Findings that share a file are fixed together as one unit; a finding
with no file is a unit of its own with no file limit (protected paths still hold). Send-back notes become
one more unit with no file limit, because a human's free-text note rarely names files reliably.

Each unit is one `fixer` write call through work.run_unit_checked (the fast check is healed by the
fixer rounds in work.check_fixer). Then ONE read-only `fix_verify` call rules on every finding. Findings
it does not call addressed get one more round (units rebuilt from just those, then one more verify). A
finding still unaddressed is left out of `fixed`, which is what review.open_findings reads to list it at
the merge gate.

The stage writes data.fix = {"rounds": [{review_round, fixed, open, head}]}, one entry per run. Its
working state lives in data.fix_progress, which is empty between runs, so a resumed run does not redo a
finished unit or a finished verify.
"""

import json
from typing import Any, Dict, List, Tuple

from scripts.flow3 import gitops, heal, prompts, questions, record, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.steps import work
from scripts.flow3.types import CallSpec, FlowStop, StageResult

STAGE = "fix"
PROGRESS = "fix_progress"
SENDBACK_UNIT = "sendback"
MAX_ATTEMPTS = 2
MAX_QUESTION_ROUNDS = 3

Doc = Dict[str, Any]


# -- reading what to fix ----------------------------------------------------------------------

def fixed_ids(ctx: Ctx) -> List[str]:
    """Finding ids an earlier fix round fixed."""
    out: List[str] = []
    for r in ctx.flow.data(STAGE).get("rounds") or []:
        out.extend(str(i) for i in r.get("fixed") or [])
    return out


def _clean_files(files: Any) -> List[str]:
    out: List[str] = []
    for f in files or []:
        s = str(f).strip()
        if s and s not in out:
            out.append(s)
    return out


def candidates(ctx: Ctx) -> Tuple[int, List[Doc], List[str]]:
    """
    The latest review round's number and its doing_it findings not yet fixed, as items:
    {id, title, detail, file, line, severity, fix, files}. `fix` and `files` come from the triage item
    (what review stored), falling back to the finding's own suggestion and file.
    """
    rounds = ctx.flow.data("review").get("rounds") or []
    if not rounds:
        return 0, [], []
    rnd = rounds[-1]
    done = set(fixed_ids(ctx))
    triage_items = {str(i.get("finding_id")): i for i in (rnd.get("triage") or {}).get("doing_it") or []
                    if isinstance(i, dict)}
    items: List[Doc] = []
    blocked: List[str] = []
    for f in rnd.get("findings") or []:
        fid = str(f.get("id"))
        if f.get("bucket") != "doing_it" or fid in done:
            continue
        t = triage_items.get(fid, {})
        named = _clean_files(t.get("files")) or _clean_files([f.get("file")])
        # A protected path (a spec test, a configured path) is never a fixer's to change. A finding
        # that names only protected files is left open for the human instead of handing a fixer a
        # unit that contradicts its own protected list.
        guarded = work.protected_paths(ctx)
        files = [x for x in named if not gitops.match_any(x, guarded)]
        if named and not files:
            blocked.append(fid)
            continue
        items.append({"id": fid, "title": str(f.get("title") or ""), "detail": str(f.get("detail") or ""),
                      "file": str(f.get("file") or ""), "line": f.get("line"),
                      "severity": str(f.get("severity") or ""),
                      "fix": str(t.get("fix") or f.get("suggestion") or ""), "files": files})
    return int(rnd.get("round", len(rounds) - 1)), items, blocked


def group_units(items: List[Doc], prefix: str) -> List[Doc]:
    """
    Fix units: findings that touch a common file go together (transitively); a finding with no file is its
    own unit with files []. Unit order follows the first finding in each group.
    """
    groups: List[Doc] = []
    for item in items:
        files = list(item["files"])
        if not files:
            groups.append({"files": [], "items": [item]})
            continue
        hits = [g for g in groups if g["files"] and set(g["files"]) & set(files)]
        if not hits:
            groups.append({"files": files, "items": [item]})
            continue
        first = hits[0]
        for other in hits[1:]:
            first["files"].extend(f for f in other["files"] if f not in first["files"])
            first["items"].extend(other["items"])
            groups.remove(other)
        first["files"].extend(f for f in files if f not in first["files"])
        first["items"].append(item)
    return [{"id": "%s-%d" % (prefix, n), "files": g["files"], "ids": [i["id"] for i in g["items"]]}
            for n, g in enumerate(groups, 1)]


# -- the stage --------------------------------------------------------------------------------

def _item_text(item: Doc, note: str) -> str:
    where = item["file"] + (":%s" % item["line"] if item.get("line") else "") if item["file"] else "(no file)"
    rows = ["- Finding %s [%s] %s at %s" % (item["id"], item.get("severity"), item["title"], where),
            "  Problem: %s" % item["detail"], "  Fix: %s" % item["fix"]]
    if note:
        rows.append("  An earlier attempt did not fix this. The checker said: %s" % note)
    return "\n".join(rows)


class _Stage:
    def __init__(self, ctx: Ctx, p: Doc) -> None:
        self.ctx = ctx
        self.wt = ctx.worktree
        self.p = p

    def save(self) -> None:
        self.ctx.flow.set_data(PROGRESS, self.p)

    def log(self, text: str) -> None:
        self.p["log"].append(text)

    # -- units ------------------------------------------------------------------------------

    def units(self) -> List[Doc]:
        p = self.p
        out = group_units([p["items"][i] for i in p["todo"]], "a%d" % p["attempt"])
        if p["sendback"] and p["attempt"] == 1:
            out.append({"id": SENDBACK_UNIT, "files": [], "ids": []})
        return out

    def prompt(self, unit: Doc) -> str:
        p = self.p
        if unit["id"] == SENDBACK_UNIT:
            body = ("- Notes from the human reviewing this change before merge. Act on them:\n%s"
                    % work.cap(p["sendback"], 8000))
        else:
            body = "\n".join(_item_text(p["items"][i], p["evidence"].get(i, "") if p["attempt"] > 1 else "")
                             for i in unit["ids"])
        allowed = work.list_text(unit["files"], "Any file except the protected paths below.")
        return prompts.render("fix_unit", items=body, allowed=allowed,
                              protected=work.list_text(work.protected_paths(self.ctx)),
                              directions=voice.directions_text(self.ctx.flow))

    def run_unit(self, unit: Doc) -> bool:
        """Run one unit. True when the stage parked for the human."""
        ctx, p = self.ctx, self.p
        uid = str(unit["id"])
        u = p["units"].setdefault(uid, {"asked": 0})
        prompt = self.prompt(unit)
        message = "flow3 #%d fix %s: %s" % (ctx.issue, uid, ", ".join(unit["ids"]) or "notes from the merge gate")
        pend = p["pending"]
        if pend.get("unit") == uid:
            answers = questions.take_answers(ctx, STAGE)
            result = work.resume_worker(ctx, "fixer", pend.get("session"), prompt,
                                        questions.answers_for_prompt(answers), unit["files"], message,
                                        "unit-%s-answers" % uid)
            p["pending"] = {}
            run_check = True
        else:
            u["base"] = gitops.head_sha(self.wt)
            self.save()
            try:
                result, run = work.run_unit_checked(ctx, unit, "fixer", prompt, message)
            except heal.UnitUnfinished as stop:
                # A fixer that could not finish is not a stop: its work so far is committed, the
                # verifier rules on each finding, and what it left stays open for the merge gate.
                self.log("Fix %s was left unfinished after a retry on a stronger model. Left: %s" % (
                    uid, "; ".join(stop.remaining) or "(not stated)"))
                ctx.flow.event("heal", cls="unit-shortfall", action="fix left open for the merge gate", unit=uid)
                work.check_unit(ctx, unit["files"])
                u["done"] = True
                u["unfinished"] = True
                head = gitops.head_sha(self.wt)
                u["commit"] = head if head != u.get("base") else None
                u["summary"] = "unfinished: " + ("; ".join(stop.remaining) or "(not stated)")
                if uid == SENDBACK_UNIT:
                    ctx.flow.set_data("sendback", {"notes": p["sendback"], "handled": True})
                    p["sendback_done"] = True
                self.save()
                return False
            run_check = run is None
        while work.needs_answers(result):
            u["asked"] += 1
            if u["asked"] > MAX_QUESTION_ROUNDS:
                raise FlowStop("implement", "the fix for %s kept asking questions instead of finishing" % uid,
                               tried=["answered %d rounds of questions" % (u["asked"] - 1)])
            qs = [q for q in (result.output or {}).get("questions") or [] if isinstance(q, dict)]
            auto, cards = questions.resolve(ctx, STAGE, qs)
            if cards:
                p["pending"] = {"unit": uid, "session": result.session_id}
                p["phase"] = "waiting_unit"
                self.save()
                return True
            result = work.resume_worker(ctx, "fixer", result.session_id, prompt,
                                        questions.answers_for_prompt(auto), unit["files"], message,
                                        "unit-%s-answers" % uid)
            run_check = True
        if run_check:
            work.check_unit(ctx, unit["files"])
        head = gitops.head_sha(self.wt)
        u["done"] = True
        u["commit"] = head if head != u.get("base") else None
        u["summary"] = str((result.output or {}).get("summary") or "")
        p["phase"] = "units"
        if uid == SENDBACK_UNIT:
            ctx.flow.set_data("sendback", {"notes": p["sendback"], "handled": True})
            p["sendback_done"] = True
        self.save()
        return False

    # -- verify -----------------------------------------------------------------------------

    def verify(self, ids: List[str]) -> Dict[str, Tuple[bool, str]]:
        ctx, p = self.ctx, self.p
        diff = work.cap(gitops.git(["diff", p["start_head"], "HEAD"], self.wt))
        files = [f for f in gitops.git(["diff", "--name-only", p["start_head"], "HEAD"], self.wt).splitlines() if f]
        view = [{k: p["items"][i][k] for k in ("id", "title", "detail", "file", "line", "fix")} for i in ids]
        prompt = prompts.render("fix_verify", findings=json.dumps(view, indent=2),
                                files=work.list_text(files, "(the fixers changed no files)"), diff=diff or "(empty diff)")
        spec = CallSpec(role="fix_verify", prompt=prompt, schema=schemas.FIX_VERIFY, cwd=self.wt,
                        tools=work.READ_TOOLS, allowed_tools=work.READ_TOOLS, label="fix-verify-%d" % p["attempt"])
        result = heal.call(ctx, spec)
        if not result.ok or not isinstance(result.output, dict):
            raise FlowStop("runner", "the fix verifier returned no usable result",
                           evidence=(result.result_text or result.subtype or "")[:300] or None)
        out: Dict[str, Tuple[bool, str]] = {}
        for r in result.output.get("results") or []:
            if isinstance(r, dict) and str(r.get("finding_id")) in ids:
                out.setdefault(str(r["finding_id"]), (r.get("addressed") is True, str(r.get("evidence") or "")))
        return out

    # -- the loop ---------------------------------------------------------------------------

    def run(self) -> StageResult:
        p = self.p
        while True:
            for unit in self.units():
                if p["units"].get(str(unit["id"]), {}).get("done"):
                    continue
                if self.run_unit(unit):
                    return StageResult("needs_human", {})
            attempt = str(p["attempt"])
            if p["todo"]:
                if attempt not in p["verified"]:
                    p["verified"][attempt] = {i: list(v) for i, v in self.verify(list(p["todo"])).items()}
                    self.save()
                results = p["verified"][attempt]
                missed: List[str] = []
                for i in p["todo"]:
                    ok, ev = results.get(i, [False, "the verifier did not rule on this finding"])
                    p["evidence"][i] = ev
                    if ok:
                        p["fixed"].append(i)
                    else:
                        missed.append(i)
                good = [i for i in p["todo"] if i not in missed]
                self.log("Attempt %s: addressed %s; not addressed %s." % (
                    attempt, ", ".join(good) or "none", ", ".join(missed) or "none"))
                p["todo"] = missed
            if p["todo"] and p["attempt"] < MAX_ATTEMPTS:
                p["attempt"] += 1
                self.save()
                continue
            return self.finish()

    def finish(self) -> StageResult:
        ctx, p = self.ctx, self.p
        entry = {"review_round": p["review_round"], "fixed": list(p["fixed"]), "open": list(p["todo"]) + list(p.get("blocked") or []),
                 "head": gitops.head_sha(self.wt)}
        p["result"] = entry
        self.save()
        return publish(ctx, p)


def publish(ctx: Ctx, p: Doc) -> StageResult:
    """Append the round entry (once), write the record, and clear the working state."""
    flow = ctx.flow
    data = flow.data(STAGE)
    rounds = list(data.get("rounds") or [])
    entry = p["result"]
    if len(rounds) == p["stage_run"]:
        data["rounds"] = rounds + [entry]
        flow.set_data(STAGE, data)
    given = [_item_text(i, "") for i in p["items"].values()]
    if p["sendback"]:
        given.append("- Send-back notes from the merge gate:\n%s" % work.cap(p["sendback"], 2000))
    units = ["%s: commit %s. %s" % (uid, u.get("commit") or "(no change)", u.get("summary", ""))
             for uid, u in p["units"].items()]
    sections = [("Given", "\n".join(given) or "(nothing to fix)"),
                ("Did", "%s\n\n%s" % (work.list_text(units, "(no units ran)"), work.list_text(p["log"], ""))),
                ("Fixed", work.list_text(entry["fixed"])),
                ("Left open", work.list_text(entry["open"]))]
    record.write_step_record(flow, STAGE, sections)
    record.journal(flow, "Fix round for review round %d: %d fixed, %d left open%s." % (
        entry["review_round"], len(entry["fixed"]), len(entry["open"]),
        "; send-back notes handled" if p["sendback"] else ""))
    flow.set_data(PROGRESS, {})
    return StageResult("done", {})


def _fresh(ctx: Ctx) -> Doc:
    """The working state for a new run; it carries its own result when there is nothing to fix."""
    flow = ctx.flow
    rnd, items, blocked = candidates(ctx)
    sb = flow.data("sendback")
    notes = str(sb.get("notes") or "").strip() if sb and not sb.get("handled") else ""
    p: Doc = {"review_round": rnd, "stage_run": len(flow.data(STAGE).get("rounds") or []),
              "start_head": gitops.head_sha(ctx.worktree), "attempt": 1, "phase": "start",
              "items": {i["id"]: i for i in items}, "todo": [i["id"] for i in items], "sendback": notes,
              "units": {}, "pending": {}, "verified": {}, "evidence": {}, "fixed": [], "blocked": blocked,
              "log": ["Left open, the finding is in a protected file a fixer may not edit: %s." % ", ".join(blocked)]
              if blocked else []}
    if not items and not notes:
        p["result"] = {"review_round": rnd, "fixed": [], "open": list(blocked), "head": p["start_head"]}
        return p
    return p


def run(ctx: Ctx) -> StageResult:
    """Run the fix stage. Safe to re-enter; returns needs_human when a fixer's question needs the human."""
    p = ctx.flow.data(PROGRESS)
    if not p:
        p = _fresh(ctx)
        ctx.flow.set_data(PROGRESS, p)
    if p.get("result"):
        return publish(ctx, p)
    return _Stage(ctx, p).run()
