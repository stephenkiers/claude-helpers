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
import os
import re
from typing import Any, Dict, List, Optional, Tuple, cast

from scripts.flow3 import checks, config, gitops, heal, prompts, questions, record, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.steps import work
from scripts.flow3.types import Answer, CallResult, CallSpec, CheckRun, FlowStop, StageResult

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


def _handoffs_text(handoffs: List[Dict[str, Any]], uid: str) -> str:
    """Changes earlier units could not make because the file was not theirs, for a later unit's prompt."""
    rows = [h for h in handoffs if str(h.get("from")) != uid and str(h.get("text") or "").strip()]
    if not rows:
        return ""
    lines = ["## Left for you by earlier units", "",
             "Earlier units needed these changes in files they could not edit. Make each one that falls in "
             "your files; ignore the rest.", ""]
    lines += ["- From %s: %s" % (h["from"], str(h["text"]).strip()) for h in rows]
    return "\n".join(lines)


def _unit_prompt(ctx: Ctx, plan: Dict[str, Any], unit: Dict[str, Any],
                 handoffs: Optional[List[Dict[str, Any]]] = None) -> str:
    return prompts.render(
        "implementer", unit_id=str(unit["id"]), title=str(unit.get("title") or ""),
        files=work.list_text(unit["files"]),
        steps="\n".join("%d. %s" % (n, s) for n, s in enumerate(unit.get("steps") or [], 1)) or "(none)",
        acceptance=_acceptance_text(plan, unit), interfaces=work.interfaces_text(plan),
        protected=work.list_text(work.protected_paths(ctx)), directions=voice.directions_text(ctx.flow),
        handoffs=_handoffs_text(handoffs or [], str(unit["id"])))


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
        prompt = _unit_prompt(ctx, self.plan, unit, d.get("handoffs"))
        message = "flow3 #%d unit %s: %s" % (ctx.issue, uid, unit.get("title"))
        if pend.get("kind") == "judge" and pend.get("unit") == uid:
            return self._after_judge_answers(unit)
        if pend.get("kind") == "worker" and pend.get("unit") == uid:
            answers = questions.take_answers(ctx, STAGE)
            result, blocked = work.guard_blocked(
                work.resume_worker, ctx, "implementer", pend.get("session"), prompt,
                questions.answers_for_prompt(answers), unit["files"], message, "unit-%s-answers" % uid)
            if blocked:
                return self._blocked(unit, blocked)
            result = cast(CallResult, result)
            d["pending"] = {}
        else:
            u["base"] = gitops.head_sha(self.wt)
            u["heals_before"] = _heal_count(ctx)
            d["phase"] = "units"
            self.save()
            try:
                result = work.run_unit_checked(ctx, unit, "implementer", prompt, message, check=False)[0]
            except heal.Blocked as stop:
                return self._blocked(unit, stop)
        while work.needs_answers(result):
            u["asked"] += 1
            if u["asked"] > MAX_QUESTION_ROUNDS:
                raise FlowStop("implement", "unit %s kept asking questions instead of finishing" % uid,
                               tried=["answered %d rounds of questions" % (u["asked"] - 1)])
            qs = [q for q in (result.output or {}).get("questions") or [] if isinstance(q, dict)]
            auto, cards = questions.resolve(ctx, STAGE, qs, owner=self._owner(unit))
            if cards:
                d["pending"] = {"kind": "worker", "unit": uid, "session": result.session_id}
                self.park("waiting_unit")
                return True
            result_new, blocked = work.guard_blocked(
                work.resume_worker, ctx, "implementer", result.session_id, prompt,
                questions.answers_for_prompt(auto), unit["files"], message, "unit-%s-answers" % uid)
            if blocked:
                return self._blocked(unit, blocked)
            result = cast(CallResult, result_new)
        u["summary"] = str((result.output or {}).get("summary") or "")
        self._note_handoffs(uid, result.output)
        return self._check_after(unit, final)

    def _check_after(self, unit: Dict[str, Any], final: bool) -> bool:
        """
        The check after a unit. By default only the last unit is checked: the units before it are
        marked done unmeasured, and one check (with fixers allowed in every unit's files) covers the
        stage. With `check.per_unit` every unit is checked as it lands.
        """
        if not final and not self.ctx.cfg.check_per_unit:
            return self._green(unit)
        run, blocked = work.check_unit_or_blocked(self.ctx, self._fix_files(unit, final), raise_on_fail=False,
                                                  use_fixer=final)
        if blocked is not None:
            return self._after_blocked_fixer(unit, run, blocked)
        return self._settle(unit, final, run)

    def _after_blocked_fixer(self, unit: Dict[str, Any], run: CheckRun, blocked: "heal.Blocked") -> bool:
        """
        A check fixer said it cannot fix the red check from where it is. It is not retried. The judge
        reads the tests only when the fixer named a spec test file it may not edit and that file is
        among the failing tests, which no fixer may edit; a `human` need or any other file always raises
        the Blocked stop as a card. A green recheck does not stop the stage.
        """
        if run.ok:
            return self._green(unit)
        uid = str(unit["id"])
        failing = self._blocked_spec_files(run, blocked)
        if not self._judgeable(uid, failing):
            raise blocked
        record.journal(self.ctx.flow, "The failing tests are this flow's own spec tests (%s), which a fixer may "
                                      "not edit, so a judge reads them next." % ", ".join(failing))
        return self._judge(unit, run, failing)

    def _judgeable(self, uid: str, failing: List[str]) -> bool:
        """
        A judge call is made only when there is a failing spec file to judge and this unit has not been
        judged already.
        """
        return bool(failing) and uid not in self.d["judged"]

    def _blocked_spec_files(self, run: CheckRun, blocked: "heal.Blocked") -> List[str]:
        """
        The spec files the judge should read for a blocked fixer. Empty unless the need is judge-routable
        and names a file; otherwise the spec files that are failing, returned only when the named file is
        one of them.
        """
        if blocked.needs not in heal.JUDGE_ROUTABLE_NEEDS or not blocked.what:
            return []
        names = heal.failing_tests(run)
        # A failing name's file part is what precedes "::", a space or "(". Compare it for equality, not as a
        # substring, so "old/tests/a.py.bak" does not count as "tests/a.py".
        named = {os.path.normpath(re.split(r"::|\s|\(", n, maxsplit=1)[0]) for n in names}
        failing = [f for f in self.spec_files if os.path.normpath(f) in named]
        what = blocked.what.strip()
        if os.path.isabs(what):
            what = os.path.relpath(what, self.wt)
        needed = os.path.normpath(what)
        if any(needed == os.path.normpath(f) for f in failing):
            return failing
        return []

    def _fix_files(self, unit: Dict[str, Any], final: bool) -> List[str]:
        """Files a check fixer may edit: the unit's, or every unit's when one check covers the stage."""
        if not final or self.ctx.cfg.check_per_unit:
            return [str(f) for f in unit["files"]]
        out: List[str] = []
        for other in ordered_units(self.plan):
            out += [str(f) for f in other["files"] if str(f) not in out]
        return out

    def _owner(self, unit: Dict[str, Any]) -> Dict[str, Any]:
        """What questions.resolve needs to settle bookkeeping questions from this unit without a card."""
        uid = str(unit["id"])
        return {"unit": uid, "files": [str(f) for f in unit["files"]],
                "unit_ids": [str(x["id"]) for x in ordered_units(self.plan)]}

    def _blocked(self, unit: Dict[str, Any], stop: "heal.Blocked") -> bool:
        """
        Route a blocked unit. A file another unit still has to work on is a handoff: that unit sees
        the need in its prompt and this one is finished. Anything else is the stop the worker described.
        """
        import os
        uid = str(unit["id"])
        if stop.needs != "other_file" or not stop.what:
            raise stop
        later: List[str] = []
        seen = False
        for other in ordered_units(self.plan):
            if seen and str(other["id"]) not in self.d["done_units"]:
                later += [str(f) for f in other["files"]]
            seen = seen or str(other["id"]) == uid
        # Normalize paths for equality comparison, not substring matching
        needed_norm = os.path.normpath(stop.what)
        if not any(f and os.path.normpath(f) == needed_norm for f in later):
            raise stop
        self.d["units"][uid]["summary"] = "blocked: " + stop.worker_reason
        self._note_handoffs(uid, {"elsewhere": ["%s: %s" % (stop.what, stop.worker_reason)]})
        self.ctx.flow.event("heal", cls="blocked", action="handed to the unit that owns the file", unit=uid,
                            what=stop.what)
        return self._green(unit)

    def _note_handoffs(self, uid: str, output: Any) -> None:
        """Keep what a unit left for the owner of another file; later units see it in their prompt."""
        rows = [str(t).strip() for t in ((output or {}).get("elsewhere") or []) if str(t).strip()]
        if not rows:
            return
        kept = self.d.setdefault("handoffs", [])
        for text in rows:
            if {"from": uid, "text": text} not in kept:
                kept.append({"from": uid, "text": text})
        self.save()
        record.journal(self.ctx.flow, "Unit %s left %d change(s) for the unit that owns the file: %s"
                       % (uid, len(rows), "; ".join(rows)))

    def _spec_files_in(self, run: CheckRun) -> List[str]:
        # The trimmed output is the failure blocks when the runner marks failed tests. A runner that
        # lists every test file it ran would otherwise make every red look like a spec test's.
        text = run.trimmed
        if not any(checks.FAILED_TEST.search(ln) for ln in text.splitlines()):
            text = checks.strip_ansi(work.read_text(run.output_path, run.trimmed)) + "\n" + run.trimmed
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
            run, blocked = work.check_unit_or_blocked(ctx, self._fix_files(unit, final), raise_on_fail=False)
            if blocked is not None:
                return self._after_blocked_fixer(unit, run, blocked)
            if run.ok:
                return self._green(unit)
            failing = self._spec_files_in(run)
        if not self._judgeable(uid, failing):
            named = heal.failing_text(run)
            raise FlowStop("check", "the fast check still fails after unit %s (%s)%s"
                           % (uid, run.command, ". " + named if named else ""),
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
            allowed = self._fix_files(unit, final=True)
            protected = work.protected_paths(ctx)
            fspec = work.worker_spec(ctx, "fixer", work.fixer_prompt(ctx, task, allowed, protected),
                                     "fix-judged-%s" % uid, model=model)
            _, blocked = work.guard_blocked(heal.write_call, ctx, fspec, allowed, protected,
                                            "flow3 #%d: fix code the judge found wrong" % ctx.issue)
            if blocked is not None:
                raise blocked  # not retried, and the stage does not go on with a red check
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
        _, blocked = work.guard_blocked(heal.write_call, ctx, spec, files, protected,
                                        "flow3 #%d: correct the spec test the judge found wrong" % ctx.issue)
        verdict = "keeps_or_tightens" if (not row["edit_weakens"] and row["verdict"] == "test_wrong") else "weakens"
        for f in files:
            self.d["test_edits"].append({"file": f, "verdict": verdict, "reason": row["reason"],
                                        "applied": blocked is None})

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
        _, blocked = work.check_unit_or_blocked(ctx, allowed)
        if blocked is not None:
            raise blocked
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
