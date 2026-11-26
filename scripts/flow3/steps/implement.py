"""
The implement stage (PLAN.md section 7.4 and the section 12 test-edit rule).

1. Existing-test edits the plan named come first, one write call restricted to those files. An edit that
   weakens a test is reverted and put to the human as a `risk` question; one that keeps or tightens is
   applied and recorded.
2. Units run one by one in dependency order, each through work.run_unit_checked.
3. A spec test still red after the heal budget goes to one judge call per unit.

The stage keeps its progress in data.implement and is safe to re-enter. It parks (needs_human) when a
card is open and picks up from the saved phase when the answers arrive.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from scripts.flow3 import config, gitops, heal, prompts, questions, record, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.steps import work
from scripts.flow3.types import Answer, CallSpec, CheckRun, FlowStop, StageResult

STAGE = "implement"
MAX_QUESTION_ROUNDS = 3
EDITS_UNIT = "test-edits"


# -- plan helpers -----------------------------------------------------------------------------

def ordered_units(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Units in dependency order (`after`), ties broken by plan order."""
    units = [u for u in plan.get("units") or [] if isinstance(u, dict)]
    ids = {str(u["id"]) for u in units}
    done: List[str] = []
    out: List[Dict[str, Any]] = []
    pending = list(units)
    while pending:
        pick = None
        for u in pending:
            after = [str(a) for a in u.get("after") or [] if str(a) in ids]
            if all(a in done for a in after):
                pick = u
                break
        if pick is None:  # a cycle: plan validation forbids it; fall back to plan order
            pick = pending[0]
        pending.remove(pick)
        done.append(str(pick["id"]))
        out.append(pick)
    return out


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower() or "x"


def _acceptance_text(plan: Dict[str, Any], unit: Dict[str, Any]) -> str:
    by_id = {str(c.get("id")): str(c.get("text")) for c in plan.get("acceptance_criteria") or []
             if isinstance(c, dict)}
    rows = ["- %s" % by_id.get(str(a), str(a)) for a in unit.get("acceptance") or []]
    return "\n".join(rows) or "(none)"


def _unit_prompt(ctx: Ctx, plan: Dict[str, Any], unit: Dict[str, Any]) -> str:
    return prompts.render(
        "implementer", unit_id=str(unit["id"]), title=str(unit.get("title") or ""),
        files=work.list_text(unit["files"]),
        steps="\n".join("%d. %s" % (n, s) for n, s in enumerate(unit.get("steps") or [], 1)) or "(none)",
        acceptance=_acceptance_text(plan, unit), interfaces=work.interfaces_text(plan),
        protected=work.list_text(work.protected_paths(ctx)), directions=voice.directions_text(ctx.flow))


def _heal_count(ctx: Ctx) -> int:
    return len([e for e in ctx.flow.events() if e.get("kind") == "heal"])


def _question(qid: str, text: str, context: str, risk: str, options: List[Tuple[str, str, str]],
              recommended: str) -> Dict[str, Any]:
    return {"id": qid, "question": text, "context": context, "kind": "risk", "reversible": True,
            "risk": risk, "recommended": recommended,
            "options": [{"id": i, "label": label, "consequence": cons} for i, label, cons in options],
            "themes": ["tests"]}


# -- the stage --------------------------------------------------------------------------------

class _Stage:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.wt = ctx.worktree
        self.plan: Dict[str, Any] = ctx.flow.data("plan").get("plan") or {}
        d = ctx.flow.data(STAGE)
        for key, default in (("done_units", []), ("test_edits", []), ("judgements", []), ("judged", []),
                             ("units", {}), ("pending", {}), ("pending_edits", [])):
            d.setdefault(key, default)
        d.setdefault("phase", "start")
        d.setdefault("start_head", gitops.head_sha(self.wt))
        self.d = d
        self.spec_files: List[str] = [str(f) for f in ctx.flow.data("spec_tests").get("files") or []]
        # A protected path is never a unit's to change, and never an "existing test edit": the spec
        # tests there are already written. Without this a plan that lists one sends a worker a unit
        # that contradicts its own protected list.
        guarded = work.protected_paths(ctx)
        for unit in self.plan.get("units") or []:
            if isinstance(unit, dict):
                unit["files"] = [f for f in unit.get("files") or [] if not gitops.match_any(str(f), guarded)]
        self.plan["existing_test_edits"] = [
            e for e in self.plan.get("existing_test_edits") or []
            if isinstance(e, dict) and not gitops.match_any(str(e.get("file")), guarded)]

    def save(self) -> None:
        self.ctx.flow.set_data(STAGE, self.d)

    def park(self, phase: str) -> StageResult:
        self.d["phase"] = phase
        self.save()
        return StageResult("needs_human", {})

    # -- step 1: edits to existing tests -----------------------------------------------------

    def edits(self) -> bool:
        """Handle the plan's existing_test_edits. True when the stage parked for the human."""
        ctx, d, wt = self.ctx, self.d, self.wt
        wanted = [e for e in self.plan.get("existing_test_edits") or [] if isinstance(e, dict)]
        if not wanted or d.get("edits_done"):
            return False
        if d["phase"] == "waiting_edits":
            answers = questions.take_answers(ctx, STAGE)
            self._settle_edits(answers)
            return False
        if "edit_range" not in d:
            files = [str(e["file"]) for e in wanted]
            unit = {"id": EDITS_UNIT, "title": "Edit existing tests as the plan says", "files": files,
                    "steps": ["%s: %s" % (e["file"], e.get("why")) for e in wanted], "acceptance": []}
            pre = gitops.head_sha(wt)
            spec = work.worker_spec(ctx, "implementer", _unit_prompt(ctx, self.plan, unit), EDITS_UNIT)
            result = heal.write_call(ctx, spec, files, work.protected_paths(ctx),
                                     "flow3 #%d: edit existing tests" % ctx.issue)
            if work.needs_answers(result):
                raise FlowStop("implement", "the implementer asked questions while editing existing tests",
                               evidence=json.dumps((result.output or {}).get("questions"))[:500])
            d["edit_range"] = [pre, gitops.head_sha(wt)]
            self.save()
        pre, post = d["edit_range"]
        if pre == post:
            d["edits_done"] = True
            self.save()
            return False
        verdicts = self._judge_edits(pre, post, wanted)
        pending: List[Dict[str, Any]] = []
        changed = [f for f in gitops.git(["diff", "--name-only", pre, post], wt).splitlines() if f]
        for f in changed:
            v = verdicts.get(f)
            if v is not None and v.get("verdict") == "keeps_or_tightens":
                d["test_edits"].append({"file": f, "verdict": "keeps_or_tightens",
                                        "reason": str(v.get("reason") or ""), "applied": True})
                continue
            reason = str((v or {}).get("reason") or "the edit judge did not classify this edit")
            lost = str((v or {}).get("no_longer_proven") or "").strip() or (
                "Nothing confirms that the edited %s still proves what it did before (%s)." % (f, reason))
            pending.append({"file": f, "reason": reason, "no_longer_proven": lost, "edit_commit": post,
                            "qid": "edit-%d-%s" % (len(pending) + 1, _slug(f))})
        if pending:
            for p in pending:
                self._restore(pre, p["file"])
            heal.commit(ctx, "flow3 #%d: revert test edits that may weaken tests" % ctx.issue)
        d["pending_edits"] = pending
        d["edits_done"] = not pending
        self.save()
        if not pending:
            return False
        qs = [_question(
            p["qid"], "%s Should the edit to %s be applied?" % (p["no_longer_proven"].rstrip(), p["file"]),
            "The plan said %s must change. The edit was made and then reverted because it may weaken the "
            "test: %s" % (p["file"], p["reason"]), p["no_longer_proven"],
            [("apply", "Apply the edit", "The test changes as the implementer wrote it; what it proved before is no longer checked."),
             ("keep", "Keep the test as it is", "The test is untouched; the code must satisfy it as written.")],
            "keep") for p in pending]
        auto, cards = questions.resolve(ctx, STAGE, qs)
        if cards:
            self.park("waiting_edits")
            return True
        self._settle_edits(auto)
        return False

    def _judge_edits(self, pre: str, post: str, wanted: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        ctx = self.ctx
        diff = work.cap(gitops.git(["diff", pre, post], self.wt))
        why = work.list_text(["%s: %s" % (e.get("file"), e.get("why")) for e in wanted])
        spec = CallSpec(role="edit_judge", prompt=prompts.render("edit_judge", edits=why, diff=diff),
                        schema=schemas.EDIT_JUDGE, cwd=self.wt, tools=(), label="edit-judge")
        result = heal.call(ctx, spec)
        if not result.ok or not isinstance(result.output, dict):
            raise FlowStop("runner", "the test-edit judge returned no usable result",
                           evidence=(result.result_text or result.subtype or "")[:300] or None)
        return {str(e.get("file")): e for e in result.output.get("edits") or [] if isinstance(e, dict)}

    def _restore(self, ref: str, path: str) -> None:
        if gitops.git(["ls-tree", ref, "--", path], self.wt):
            gitops.git(["checkout", ref, "--", path], self.wt)
        else:
            gitops.git(["rm", "-q", "-f", "--", path], self.wt, check=False)

    def _settle_edits(self, answers: Dict[str, Answer]) -> None:
        ctx, d, wt = self.ctx, self.d, self.wt
        for p in d["pending_edits"]:
            ans = answers.get(p["qid"])
            apply = ans is not None and ans.option_id == "apply"
            if apply:
                ref, path = p["edit_commit"], p["file"]
                if gitops.git(["ls-tree", ref, "--", path], wt):
                    gitops.git(["checkout", ref, "--", path], wt)
                else:
                    gitops.git(["rm", "-q", "-f", "--", path], wt, check=False)
                heal.commit(ctx, "flow3 #%d: apply approved edit to %s" % (ctx.issue, path), [path])
            d["test_edits"].append({"file": p["file"], "verdict": "weakens", "reason": p["reason"],
                                    "applied": apply})
            record.journal(ctx.flow, "Edit to %s %s." % (p["file"], "applied after approval" if apply
                                                           else "not applied; the test is unchanged"))
        d["pending_edits"] = []
        d["edits_done"] = True
        d["phase"] = "units"
        self.save()

    # -- step 2: units ----------------------------------------------------------------------

    def units(self) -> bool:
        """Run every unit not yet done. True when the stage parked for the human."""
        order = ordered_units(self.plan)
        if not order:
            raise FlowStop("implement", "the plan has no units to implement")
        last = str(order[-1]["id"])
        for unit in order:
            uid = str(unit["id"])
            if uid in self.d["done_units"]:
                continue
            if self.one_unit(unit, final=(uid == last)):
                return True
        return False

    def one_unit(self, unit: Dict[str, Any], final: bool) -> bool:
        ctx, d = self.ctx, self.d
        uid = str(unit["id"])
        u = d["units"].setdefault(uid, {"asked": 0})
        pend = d["pending"]
        prompt = _unit_prompt(ctx, self.plan, unit)
        message = "flow3 #%d unit %s: %s" % (ctx.issue, uid, unit.get("title"))
        run: Optional[CheckRun]
        if pend.get("kind") == "judge" and pend.get("unit") == uid:
            return self._after_judge_answers(unit)
        if pend.get("kind") == "worker" and pend.get("unit") == uid:
            answers = questions.take_answers(ctx, STAGE)
            result = work.resume_worker(ctx, "implementer", pend.get("session"), prompt,
                                        questions.answers_for_prompt(answers), unit["files"], message,
                                        "unit-%s-answers" % uid)
            d["pending"] = {}
            run = None
        else:
            u["base"] = gitops.head_sha(self.wt)
            u["heals_before"] = _heal_count(ctx)
            d["phase"] = "units"
            self.save()
            result, run = work.run_unit_checked(ctx, unit, "implementer", prompt, message,
                                                raise_on_fail=False, use_fixer=final)
        while work.needs_answers(result):
            u["asked"] += 1
            if u["asked"] > MAX_QUESTION_ROUNDS:
                raise FlowStop("implement", "unit %s kept asking questions instead of finishing" % uid,
                               tried=["answered %d rounds of questions" % (u["asked"] - 1)])
            qs = [q for q in (result.output or {}).get("questions") or [] if isinstance(q, dict)]
            auto, cards = questions.resolve(ctx, STAGE, qs)
            if cards:
                d["pending"] = {"kind": "worker", "unit": uid, "session": result.session_id}
                self.park("waiting_unit")
                return True
            result = work.resume_worker(ctx, "implementer", result.session_id, prompt,
                                        questions.answers_for_prompt(auto), unit["files"], message,
                                        "unit-%s-answers" % uid)
        if run is None:
            run = work.check_unit(ctx, unit["files"], raise_on_fail=False, use_fixer=final)
        u["summary"] = str((result.output or {}).get("summary") or "")
        return self._settle(unit, final, run)

    def _spec_files_in(self, run: CheckRun) -> List[str]:
        text = work.read_text(run.output_path, run.trimmed) + "\n" + run.trimmed
        return [f for f in self.spec_files if f in text]

    def _settle(self, unit: Dict[str, Any], final: bool, run: CheckRun) -> bool:
        ctx, d = self.ctx, self.d
        uid = str(unit["id"])
        if run.ok:
            return self._green(unit)
        failing = self._spec_files_in(run)
        if not final:
            if failing:  # tests for the units still to come are red by design
                d["units"][uid]["deferred_red"] = failing
                return self._green(unit)
            run = work.check_unit(ctx, unit["files"], raise_on_fail=False, use_fixer=True)
            if run.ok:
                return self._green(unit)
            failing = self._spec_files_in(run)
        if not failing or uid in d["judged"]:
            raise FlowStop("check", "the fast check still fails after unit %s (%s)" % (uid, run.command),
                           evidence=run.output_path or None,
                           tried=["re-ran the check", "two fixer rounds"] + (["a judge call"] if uid in d["judged"] else []))
        return self._judge(unit, run, failing)

    def _judge(self, unit: Dict[str, Any], run: CheckRun, failing: List[str]) -> bool:
        ctx, d, wt = self.ctx, self.d, self.wt
        uid = str(unit["id"])
        diff = work.cap(gitops.git(["diff", d["start_head"], "HEAD"], wt))
        spec_rows = [str(t.get("behaviour")) for t in self.plan.get("test_spec") or [] if isinstance(t, dict)]
        prompt = prompts.render("judge", spec=work.list_text(spec_rows), test_files=work.list_text(failing),
                                failure=work.cap(run.trimmed or work.read_text(run.output_path), 8000), diff=diff)
        spec = CallSpec(role="judge", prompt=prompt, schema=schemas.JUDGE, cwd=wt, tools=work.READ_TOOLS,
                        allowed_tools=work.READ_TOOLS, label="judge-%s" % uid)
        result = heal.call(ctx, spec)
        if not result.ok or not isinstance(result.output, dict):
            raise FlowStop("runner", "the judge returned no usable result for unit %s" % uid,
                           evidence=(result.result_text or result.subtype or "")[:300] or None)
        out = result.output
        verdict = str(out["verdict"])
        weakens = bool(out.get("edit_weakens"))
        row = {"unit": uid, "verdict": verdict, "reason": str(out.get("reason") or ""), "files": failing,
               "edit_weakens": weakens, "test_edit": str(out.get("test_edit") or "")}
        d["judgements"].append(row)
        d["judged"].append(uid)
        self.save()
        record.journal(ctx.flow, "Judge on unit %s: %s. %s" % (uid, verdict, row["reason"]))
        if verdict == "code_wrong":
            base = config.model_for("fixer", ctx.cfg.models)
            model = config.stronger(config.stronger(base))
            task = ("The behaviour tests %s still fail after earlier fixes. A judge found the tests are right "
                    "and the code is wrong: %s\n\nCheck output (trimmed):\n%s"
                    % (", ".join(failing), row["reason"], run.trimmed or "(none)"))
            allowed = [str(f) for f in unit["files"]]
            protected = work.protected_paths(ctx)
            fspec = work.worker_spec(ctx, "fixer", work.fixer_prompt(ctx, task, allowed, protected),
                                     "fix-judged-%s" % uid, model=model)
            heal.write_call(ctx, fspec, allowed, protected, "flow3 #%d: fix code the judge found wrong" % ctx.issue)
            return self._recheck(unit)
        if verdict == "test_wrong" and not weakens:
            self._edit_tests(row, approved=False)
            return self._recheck(unit)
        risk = ("The edit to %s would weaken the test: %s" % (", ".join(failing), row["reason"])
                if verdict == "test_wrong" else
                "The spec itself looks wrong, so %s cannot pass as written: %s" % (", ".join(failing), row["reason"]))
        q = _question(
            "judge-%s" % _slug(uid), risk + " Should the test be changed?",
            "Unit %s is finished but %s is still red after the fixes. The judge said: %s"
            % (uid, ", ".join(failing), row["reason"]), risk,
            [("apply", "Change the test", "The test is edited as the judge describes; what it checked before is no longer checked."),
             ("keep", "Keep the test as it is", "The test is untouched; the flow stops at the check unless the code can satisfy it.")],
            "keep")
        auto, cards = questions.resolve(ctx, STAGE, [q])
        d["pending"] = {"kind": "judge", "unit": uid, "row": row, "qid": q["id"]}
        if cards:
            self.park("waiting_judge")
            return True
        return self._apply_judge_answers(unit, auto)

    def _after_judge_answers(self, unit: Dict[str, Any]) -> bool:
        return self._apply_judge_answers(unit, questions.take_answers(self.ctx, STAGE))

    def _apply_judge_answers(self, unit: Dict[str, Any], answers: Dict[str, Answer]) -> bool:
        d = self.d
        pend = d["pending"]
        ans = answers.get(str(pend["qid"]))
        apply = ans is not None and ans.option_id == "apply"
        if apply:
            self._edit_tests(pend["row"], approved=True)
        else:
            for f in pend["row"]["files"]:
                d["test_edits"].append({"file": f, "verdict": "weakens", "reason": pend["row"]["reason"],
                                        "applied": False})
        d["pending"] = {}
        d["phase"] = "units"
        self.save()
        return self._recheck(unit)

    def _edit_tests(self, row: Dict[str, Any], approved: bool) -> None:
        ctx = self.ctx
        files = [str(f) for f in row["files"]]
        task = ("The test file(s) %s are wrong, not the code. Make this change to the test and nothing else: %s\n"
                "Why: %s" % (", ".join(files), row["test_edit"] or "correct the test so it matches the ticket",
                             row["reason"]))
        protected = [p for p in work.protected_paths(ctx) if p not in files]
        spec = work.worker_spec(ctx, "fixer", work.fixer_prompt(ctx, task, files, protected),
                                "fix-test-%s" % row["unit"])
        heal.write_call(ctx, spec, files, protected, "flow3 #%d: correct the spec test the judge found wrong" % ctx.issue)
        verdict = "keeps_or_tightens" if (not row["edit_weakens"] and row["verdict"] == "test_wrong") else "weakens"
        for f in files:
            self.d["test_edits"].append({"file": f, "verdict": verdict, "reason": row["reason"], "applied": True})

    def _recheck(self, unit: Dict[str, Any]) -> bool:
        heal.check(self.ctx, "fast", fixer=None, raise_on_fail=True)
        return self._green(unit)

    def _green(self, unit: Dict[str, Any]) -> bool:
        ctx, d, wt = self.ctx, self.d, self.wt
        uid = str(unit["id"])
        u = d["units"][uid]
        head = gitops.head_sha(wt)
        base = str(u.get("base") or d["start_head"])
        u["commit"] = head if head != base else None
        u["files"] = [f for f in gitops.git(["diff", "--name-only", base, head], wt).splitlines() if f]
        u["heals"] = _heal_count(ctx) - int(u.get("heals_before") or 0)
        if uid not in d["done_units"]:
            d["done_units"].append(uid)
        self.save()
        return False

    # -- step 5: wrap up --------------------------------------------------------------------

    def finish(self) -> None:
        ctx, d = self.ctx, self.d
        allowed: List[str] = []
        for u in ordered_units(self.plan):
            allowed += [str(f) for f in u["files"] if str(f) not in allowed]
        # A recorded pass for an unchanged tree is reused, so this only runs when something changed.
        heal.check(ctx, "fast", fixer=work.check_fixer(ctx, allowed), raise_on_fail=True)
        d["phase"] = "done"
        self.save()
        sections: List[Tuple[str, str]] = []
        for unit in ordered_units(self.plan):
            uid = str(unit["id"])
            u = d["units"].get(uid, {})
            deferred = " Spec tests for later units were still red here: %s." % ", ".join(u["deferred_red"]) \
                if u.get("deferred_red") else ""
            sections.append(("Unit %s: %s" % (uid, unit.get("title")),
                             "Questions asked: %d. Commit: %s. Heals: %s. Files: %s. %s%s" % (
                                 u.get("asked", 0), u.get("commit") or "(none)", u.get("heals", 0),
                                 ", ".join(u.get("files") or []) or "(none)", u.get("summary", ""), deferred)))
        edits = ["%s: %s (%s). %s" % (e["file"], e["verdict"], "applied" if e["applied"] else "not applied",
                                      e["reason"]) for e in d["test_edits"]]
        sections.append(("Test edits", work.list_text(edits, "(none)")))
        sections.append(("Judgements", work.list_text(
            ["unit %s: %s. %s" % (j["unit"], j["verdict"], j["reason"]) for j in d["judgements"]], "(none)")))
        record.write_step_record(ctx.flow, STAGE, sections)
        record.journal(ctx.flow, "Implemented %d unit(s)." % len(d["done_units"]))


def run(ctx: Ctx) -> StageResult:
    """Run the implement stage. Safe to re-enter; returns needs_human when a card is open."""
    stage = _Stage(ctx)
    if stage.d.get("phase") == "done":
        return StageResult("done", {})
    if stage.edits():
        return StageResult("needs_human", {})
    if stage.units():
        return StageResult("needs_human", {})
    stage.finish()
    return StageResult("done", {})
