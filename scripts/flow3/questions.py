"""
The question resolver (PLAN.md section 6).

Steps do not ask the human. They return questions, and `resolve` decides who answers each one:

1. a fact is checked by a scout and never asked;
2. a risk or purpose question always goes to the human, and no principle may answer it;
3. otherwise one resolver call asks whether a principle decides it directly, and whether it is the
   driver's own bookkeeping (which unit owns a file, what runs first, a tool a worker lacks): those
   are settled by the driver's rules and never shown;
4. a detail with no deciding principle takes the step's recommendation, recorded as assumed;
5. everything else goes to the human on a self-contained card, and only if every option on it is
   one the driver can carry out.

Every outcome is recorded: a decision file, a row in the repo's decision log, and a journal line.
"""

import dataclasses
import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from scripts.flow3 import gitops, principles, prompts, record, runner, schemas, voice
from scripts.flow3.context import Ctx
from scripts.flow3.principles import Principle
from scripts.flow3.state import FlowDir
from scripts.flow3.types import Answer, CallResult, CallSpec, FlowStop
from scripts.workflow.fsutil import atomic_write_text, read_json_object

MAX_PARALLEL = 4
# A principle may never answer these (PLAN.md section 6).
NEVER_BY_PRINCIPLE = ("risk", "purpose")
FACT_TOOLS = ("Read", "Grep", "Glob")

Repair = Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]

# Who asked: {"unit", "files", "other_files", "unit_ids"} for a question from a worker on one unit.
Owner = Dict[str, Any]

# What the driver does for each kind of bookkeeping question. The text is what the worker is told.
DRIVER_RULES: Dict[str, str] = {
    "ownership": ("Keep to the files you were given. Describe any change needed in another file under "
                  "`elsewhere`; the part of the plan that owns that file makes it."),
    "ordering": ("The order of the work is fixed by the plan. Finish your own part now and describe "
                 "anything a later part must do under `elsewhere`."),
    "tooling": ("You cannot run commands and nobody will run one for you mid-task. Finish what you can "
                "with the tools you have; the driver runs the checks itself afterwards. If the work "
                "cannot be done without it, return `blocked` and say what is needed."),
}

# An option that asks the driver to change how the work is organised. It has no way to do that
# mid-flow: a unit's file list and the order of units are fixed when the plan is accepted.
_UNIT_WORD = r"(?:units?|file lists?|allowed files|ownership|scope of (?:this|the) unit)"
# Any character that does not end a sentence. A dot inside a file name (src/a.py) does not end one.
_IN = r"(?:[^.;]|\.(?=\S))"
_REORGANISE = re.compile(
    r"\b(?:widen|expand|extend|enlarge|broaden|reassign|re-assign|transfer|re-?order|swap)\b{IN}{{0,80}}\b{U}\b"
    r"|\b(?:add|move|give|hand)\b{IN}{{0,80}}\b(?:to|into)\b{IN}{{0,30}}\b{U}\b"
    r"|\b(?:let|allow)\b{IN}{{0,40}}\b(?:this|the) unit\b{IN}{{0,40}}\b(?:edit|change|touch|modify|own)\b"
    r"|\brun\b{IN}{{0,60}}\b(?:by hand|manually|for (?:me|the worker|this unit))\b"
    .format(IN=_IN, U=_UNIT_WORD), re.IGNORECASE)
_UNIT_REF = re.compile(r"\bunits?\s+[A-Za-z]{0,3}-?\d+[a-z]?\b|\b(?:this|another|a later|an earlier|the next) unit\b",
                       re.IGNORECASE)
GLOSSARY = ("A \"unit\" is one slice of this ticket's plan. Each unit has a fixed list of files and "
            "the units run one after another.")


# -- model calls -----------------------------------------------------------------------------

def _call(ctx: Ctx, spec: CallSpec) -> CallResult:
    """Run one model call through the healing wrapper when it exists. A failed call is a stop."""
    try:
        heal = importlib.import_module("scripts.flow3.heal")
    except ModuleNotFoundError as exc:
        if exc.name != "scripts.flow3.heal":
            raise
        result = runner.run(spec, ctx.flow, ctx.cfg.models)
    else:
        result = cast(CallResult, heal.call(ctx, spec))
    if not result.ok:
        raise FlowStop("runner", "the %s call failed (%s)" % (spec.role, result.subtype or "no output"),
                       evidence=(result.result_text or result.stderr or "")[:500] or None)
    return result


# -- small helpers ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _options(q: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [o for o in q.get("options") or [] if isinstance(o, dict)]


def _option(q: Dict[str, Any], option_id: str) -> Optional[Dict[str, Any]]:
    for opt in _options(q):
        if opt.get("id") == option_id and option_id:
            return opt
    return None


def _purpose(ctx: Ctx) -> str:
    """The first non-empty line of the issue body, else the title."""
    title = str(ctx.flow.get("title") or "")
    try:
        body = ctx.provider.get_issue(ctx.issue).body
    except Exception:  # a provider outage must not stop a question from being asked
        body = ""
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()
    return title


def _principle_view(p: Principle) -> Dict[str, str]:
    return {"id": p.id, "title": p.title, "statement": p.statement}


# -- cards -----------------------------------------------------------------------------------

def build_card(ctx: Ctx, stage: str, question: Dict[str, Any],
               bearing: Optional[List[Dict[str, str]]] = None,
               revisit: Optional[Dict[str, str]] = None,
               purpose: Optional[str] = None) -> Dict[str, Any]:
    """A question card that stands on its own for someone who has forgotten the ticket."""
    recommended = str(question.get("recommended") or "")
    context = str(question.get("context") or "")
    themes = [str(t) for t in question.get("themes") or []]
    experts: List[Dict[str, str]] = []
    for view in question.get("expert_views") or []:
        if isinstance(view, dict):
            experts.append({"who": str(view.get("expert") or ""), "favours": str(view.get("favours") or ""),
                            "why": str(view.get("why") or "")})
    if not experts:
        experts = [{"who": "%s step" % stage, "favours": recommended, "why": context}]
    prior = [
        {"date": str(row.get("ts") or "")[:10], "issue": row.get("issue"),
         "answer_text": str(row.get("answer_text") or ""), "reason": str(row.get("reason") or ""),
         "source": str(row.get("source") or "")}
        for row in principles.prior_decisions(ctx.repo, themes, 5)
    ]
    return {
        "id": str(question.get("id") or ""),
        "stage": stage,
        "kind": str(question.get("kind") or ""),
        "reversible": bool(question.get("reversible", True)),
        "ticket": {"issue": ctx.issue, "title": str(ctx.flow.get("title") or ""),
                   "purpose": purpose if purpose is not None else _purpose(ctx), "stage": stage},
        "decision": str(question.get("question") or ""),
        "why": context,
        "options": [{"id": str(o.get("id") or ""), "label": str(o.get("label") or ""),
                     "consequence": str(o.get("consequence") or "")} for o in _options(question)],
        "experts": experts,
        "before": {"principles": list(bearing or []), "decisions": prior},
        "recommended": recommended,
        "while_waiting": "This ticket is parked at %s; other tickets continue." % stage,
        "risk": str(question.get("risk") or ""),
        "revisit": dict(revisit) if revisit else None,
        "themes": themes,
        "status": "open",
        "answer": None,
    }


def unhonourable(card: Dict[str, Any], owner: Optional[Owner] = None) -> List[str]:
    """Ids of the options that ask for something the driver cannot carry out. Only checked for worker questions (owner is not None)."""
    if owner is None:
        return []
    out: List[str] = []
    for opt in card.get("options") or []:
        if isinstance(opt, dict) and _REORGANISE.search("%s. %s" % (opt.get("label") or "", opt.get("consequence") or "")):
            out.append(str(opt.get("id") or ""))
    return out


def _glossary(card: Dict[str, Any], owner: Optional[Owner]) -> str:
    """Plain words for any driver vocabulary the card uses, or "" when it uses none."""
    parts = [str(card.get("decision") or ""), str(card.get("why") or ""), str(card.get("risk") or "")]
    for opt in card.get("options") or []:
        if isinstance(opt, dict):
            parts += [str(opt.get("label") or ""), str(opt.get("consequence") or "")]
    text = " ".join(parts)
    ids = [str(i) for i in (owner or {}).get("unit_ids") or []]
    named = [i for i in ids if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(i), text)]
    if not named and not _UNIT_REF.search(text):
        return ""
    out = GLOSSARY
    if owner and owner.get("unit"):
        out += " This question came from unit %s, which may edit: %s." % (
            owner["unit"], ", ".join(str(f) for f in owner.get("files") or []) or "(no files listed)")
    return out


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def card_missing(card: Dict[str, Any]) -> List[str]:
    """Names of the parts a card lacks. An empty list means it may be shown."""
    missing: List[str] = []
    ticket = card.get("ticket")
    if not (isinstance(ticket, dict) and _filled(ticket.get("title")) and _filled(ticket.get("purpose"))
            and _filled(ticket.get("stage"))):
        missing.append("ticket")
    if not _filled(card.get("decision")):
        missing.append("decision")
    if not _filled(card.get("why")):
        missing.append("why")
    options = card.get("options")
    ids: List[str] = []
    ok_options = isinstance(options, list) and len(options) >= 2
    if isinstance(options, list):
        for opt in options:
            if not (isinstance(opt, dict) and _filled(opt.get("id")) and _filled(opt.get("label"))
                    and _filled(opt.get("consequence"))):
                ok_options = False
            elif isinstance(opt, dict):
                ids.append(str(opt["id"]))
    if not ok_options:
        missing.append("options")
    experts = card.get("experts")
    if not (isinstance(experts, list) and experts
            and all(isinstance(e, dict) and _filled(e.get("who")) and _filled(e.get("why")) for e in experts)):
        missing.append("experts")
    if card.get("recommended") not in ids:
        missing.append("recommended")
    if not _filled(card.get("while_waiting")):
        missing.append("while_waiting")
    if card.get("kind") == "risk" and not _filled(card.get("risk")):
        missing.append("risk")
    bad = unhonourable(card)
    if bad:
        missing.append("an option the driver cannot carry out (%s)" % ", ".join(bad))
    return missing


# -- deciding who answers --------------------------------------------------------------------

@dataclasses.dataclass
class _Outcome:
    question: Dict[str, Any]
    answer: Optional[Answer] = None
    bearing: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    revisit: Optional[Dict[str, str]] = None


def _assume(q: Dict[str, Any], reason: str) -> Optional[Answer]:
    rec = _option(q, str(q.get("recommended") or ""))
    if rec is None:
        return None
    return Answer(str(q["id"]), str(rec["id"]), str(rec.get("label") or ""), "assumed", reason=reason)


def _fact(ctx: Ctx, q: Dict[str, Any]) -> Optional[Answer]:
    """One scout call. Answers when the fact is found; otherwise the recommendation, as assumed."""
    qid = str(q["id"])
    raw = str(ctx.flow.get("worktree") or "")
    cwd = ctx.repo_root
    if raw:
        if Path(raw).is_dir():
            cwd = Path(raw)
    prompt = prompts.render("fact_scout", question=json.dumps(_prompt_question(q), indent=2, sort_keys=True))
    spec = CallSpec(role="fact_scout", prompt=prompt, schema=schemas.FACT_SCOUT, cwd=cwd, tools=FACT_TOOLS,
                    allowed_tools=FACT_TOOLS, label=qid)
    out = _call(ctx, spec).output or {}
    entries = [e for e in out.get("answers") or [] if isinstance(e, dict)]
    found = next((e for e in entries if e.get("question_id") == qid), entries[0] if len(entries) == 1 else None)
    if found and found.get("found"):
        opt = _option(q, str(found.get("option_id") or ""))
        text = str(opt.get("label") if opt else found.get("answer") or "")
        if opt or text:
            return Answer(qid, str(opt["id"]) if opt else "", text, "fact",
                          reason=str(found.get("evidence") or found.get("answer") or "checked in the repository"))
    return _assume(q, "the fact could not be checked, so the step's recommended option was taken")


def _prompt_question(q: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": q.get("id"), "question": q.get("question"), "context": q.get("context"),
            "kind": q.get("kind"),
            "options": [{"id": o.get("id"), "label": o.get("label"), "consequence": o.get("consequence")}
                        for o in _options(q)],
            "recommended": q.get("recommended")}


def _verdict(ctx: Ctx, q: Dict[str, Any], text: str) -> Dict[str, Any]:
    qid = str(q["id"])
    prompt = prompts.render("principle_resolver", principles=text,
                            question=json.dumps(_prompt_question(q), indent=2, sort_keys=True))
    spec = CallSpec(role="resolver", prompt=prompt, schema=schemas.PRINCIPLE_RESOLVER, cwd=ctx.repo_root,
                    tools=(), label=qid)
    out = _call(ctx, spec).output or {}
    entries = [e for e in out.get("results") or [] if isinstance(e, dict)]
    for entry in entries:
        if entry.get("question_id") == qid:
            return entry
    return entries[0] if len(entries) == 1 else {"outcome": "none"}


_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.*/-]+")


def _protected_named(ctx: Ctx, q: Dict[str, Any]) -> List[str]:
    """Protected paths named by the question or its options (the context is not read: it often cites a spec test)."""
    protected = [str(p) for p in list(ctx.cfg.protected_paths) + list(ctx.flow.get("protected") or [])]
    if not protected:
        return []
    parts = [str(q.get("question") or "")]
    for opt in q.get("options") or []:
        if isinstance(opt, dict):
            parts += [str(opt.get("label") or ""), str(opt.get("consequence") or "")]
    named: List[str] = []
    for token in _PATH_TOKEN.findall(" ".join(parts)):
        token = token.strip(".,;:")
        if ("/" in token or "." in token) and token not in named and gitops.match_any(token, protected):
            named.append(token)
    return named


def _settle(ctx: Ctx, q: Dict[str, Any], driver_kind: str, why: str) -> Optional[Answer]:
    """
    The driver's own answer to a bookkeeping question. A fact goes to the scout. Anything else gets
    the driver's standing rule; no option is picked, because the options were written by a worker
    that did not know the rule.
    """
    if driver_kind == "fact":
        return _fact(ctx, q)
    rule = DRIVER_RULES.get(driver_kind) or DRIVER_RULES["ownership"]
    return Answer(str(q["id"]), "", rule, "driver",
                  reason="%s question, settled by the driver's rule without a card. %s" % (driver_kind or "ownership", why))


def _decide(ctx: Ctx, q: Dict[str, Any], text: str, by_id: Dict[str, Principle],
            owner: Optional[Owner] = None) -> _Outcome:
    out = _Outcome(question=q)
    kind = str(q.get("kind") or "")
    named = [] if kind in NEVER_BY_PRINCIPLE else _protected_named(ctx, q)
    if named:
        # Whether a protected file may change is never a detail: it goes to the human as a risk.
        kind = "risk"
        q["kind"] = kind
        if not str(q.get("risk") or "").strip():
            q["risk"] = "This question is about a protected path (%s); only you can allow a change there." % ", ".join(named)
    if kind == "fact":
        out.answer = _fact(ctx, q)  # None only when the step gave no usable recommendation
        return out
    if kind not in NEVER_BY_PRINCIPLE and owner is not None:
        bad = unhonourable({"options": _options(q)}, owner)
        if bad:
            # A person cannot be asked to pick something the driver cannot do. An option like this
            # is the mark of a bookkeeping question, so it is settled, not shown.
            out.answer = _settle(ctx, q, "ownership", "Option %s asks to change how the work is split, "
                                 "which cannot be done mid-flow." % ", ".join(bad))
            return out
    # The resolver is asked when a principle might decide it, or when a worker on a unit asked
    # something that would otherwise become a card (its question may be bookkeeping).
    if kind not in NEVER_BY_PRINCIPLE and (by_id or (owner is not None and kind != "detail")):
        verdict = _verdict(ctx, q, text or "(this project has no written principles)")
        if verdict.get("who") == "driver":
            driver_kind = str(verdict.get("driver_kind") or "")
            if driver_kind in schemas.DRIVER_KINDS:
                out.answer = _settle(ctx, q, driver_kind, str(verdict.get("reason") or ""))
                if out.answer is not None:
                    return out
        pid = str(verdict.get("principle_id") or "")
        principle = by_id.get(pid)
        outcome = verdict.get("outcome")
        if principle is not None and outcome == "revisit":
            out.bearing = [_principle_view(principle)]
            out.revisit = {"principle_id": pid, "why": str(verdict.get("reason") or "")}
            return out
        opt = _option(q, str(verdict.get("option_id") or ""))
        if principle is not None and outcome == "decides" and opt is not None:
            out.bearing = [_principle_view(principle)]
            out.answer = Answer(str(q["id"]), str(opt["id"]), str(opt.get("label") or ""), "principle",
                                principle_id=pid,
                                reason="; ".join(p for p in (principle.statement, str(verdict.get("reason") or "")) if p))
            assert kind not in NEVER_BY_PRINCIPLE, "a principle answered a %s question" % kind
            return out
    if kind == "detail":
        out.answer = _assume(q, "a detail no principle decides; the step's recommended option was taken")
    return out


# -- recording -------------------------------------------------------------------------------

def _decision_row(ctx: Ctx, stage: str, card: Dict[str, Any], answer: Answer) -> Dict[str, Any]:
    source = "principle:%s" % answer.principle_id if answer.source == "principle" else answer.source
    return {
        "ts": _now(), "flow": ctx.flow.name, "issue": ctx.issue, "stage": stage, "id": answer.question_id,
        "question": card.get("decision", ""), "kind": card.get("kind", ""), "themes": card.get("themes", []),
        "options": card.get("options", []), "answer": answer.option_id, "answer_text": answer.text,
        "source": source, "reason": answer.reason, "verbatim": answer.verbatim_path, "overruled": False,
    }


def _record(ctx: Ctx, stage: str, card: Dict[str, Any], answer: Answer) -> None:
    record.write_decision(ctx.flow, card, answer, stage)
    principles.append_decision(ctx.repo, _decision_row(ctx, stage, card, answer))


def _by_who(answer: Answer) -> str:
    if answer.source == "principle":
        return "principle %s" % answer.principle_id
    if answer.source == "driver":
        return "the driver (not a question for a person)"
    return answer.source


def _journal_auto(ctx: Ctx, card: Dict[str, Any], answer: Answer) -> None:
    record.journal(ctx.flow, "Question %s (%s) answered by %s: %s. %s"
                   % (answer.question_id, card.get("kind", ""), _by_who(answer), answer.text, answer.reason))


# -- the batch -------------------------------------------------------------------------------

def _batch_path(flow: FlowDir) -> Path:
    return flow.path / "questions.json"


def _save_batch(flow: FlowDir, batch: Dict[str, Any]) -> None:
    atomic_write_text(_batch_path(flow), json.dumps(batch, indent=2, sort_keys=True) + "\n")
    atomic_write_text(flow.path / "questions.md", render_md(batch))


def _load_batch(flow: FlowDir) -> Optional[Dict[str, Any]]:
    return read_json_object(_batch_path(flow))


def open_batch(flow: FlowDir) -> Optional[Dict[str, Any]]:
    """The batch waiting for the human, or None."""
    batch = _load_batch(flow)
    if batch is not None and batch.get("status") == "open":
        return batch
    return None


def resolve(ctx: Ctx, stage: str, questions: List[Dict[str, Any]], repair: Optional[Repair] = None,
            owner: Optional[Owner] = None) -> Tuple[Dict[str, Answer], List[Dict[str, Any]]]:
    """
    Resolve a step's questions. Returns the answers reached without the human and the cards that need
    the human; when there are cards, questions.json and questions.md are already written. `owner`
    says which unit's worker asked, so bookkeeping questions can be told from real ones.
    """
    if not questions:
        return {}, []
    goal, plist = principles.load_for(ctx)
    text = principles.as_text(goal, plist)
    by_id = {p.id: p for p in plist}
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_PARALLEL, len(questions)))) as pool:
        outcomes = list(pool.map(lambda q: _decide(ctx, q, text, by_id, owner), questions))
    purpose = _purpose(ctx)

    def card_of(o: _Outcome) -> Dict[str, Any]:
        card = build_card(ctx, stage, o.question, o.bearing, o.revisit, purpose)
        card["glossary"] = _glossary(card, owner)
        return card

    waiting = [o for o in outcomes if o.answer is None]
    cards = {str(o.question["id"]): card_of(o) for o in waiting}
    incomplete = [{"id": cid, "missing": card_missing(c)} for cid, c in cards.items() if card_missing(c)]
    if incomplete and repair is not None:
        replacements = {str(r.get("id")): r for r in repair(incomplete) if isinstance(r, dict)}
        for o in waiting:
            cid = str(o.question["id"])
            if cid in replacements and card_missing(cards[cid]):
                o.question = replacements[cid]
                cards[cid] = card_of(o)
        incomplete = [{"id": cid, "missing": card_missing(c)} for cid, c in cards.items() if card_missing(c)]
    if incomplete:
        # A risk question whose author left out the risk text is still shown: the human can judge it
        # from the decision and the reason, and a stop here would cost more than the gap.
        for o in waiting:
            cid = str(o.question["id"])
            if card_missing(cards[cid]) == ["risk"]:
                o.question = dict(o.question)
                o.question["risk"] = ("The step that asked did not say what would no longer be protected. "
                                      "Its reason: %s" % str(o.question.get("context") or "(none given)"))
                cards[cid] = card_of(o)
        incomplete = [{"id": cid, "missing": card_missing(c)} for cid, c in cards.items() if card_missing(c)]
    if incomplete:
        first = incomplete[0]
        raise FlowStop("runner", "question card %s is missing: %s" % (first["id"], ", ".join(first["missing"])),
                       evidence="; ".join("%s: %s" % (i["id"], ", ".join(i["missing"])) for i in incomplete))

    auto: Dict[str, Answer] = {}
    for o in outcomes:
        if o.answer is None:
            continue
        card = card_of(o)
        _record(ctx, stage, card, o.answer)
        _journal_auto(ctx, card, o.answer)
        auto[o.answer.question_id] = o.answer
    human_cards = list(cards.values())
    if human_cards:
        batch = {"stage": stage, "created": _now(), "status": "open", "cards": human_cards,
                 "auto": {qid: dataclasses.asdict(a) for qid, a in auto.items()}}
        _save_batch(ctx.flow, batch)
        record.journal(ctx.flow, "Waiting for you at %s: %s." % (
            stage, ", ".join("%s (%s)" % (c["id"], c["kind"]) for c in human_cards)))
    return auto, human_cards


# -- markdown --------------------------------------------------------------------------------

def _render_card(card: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    ticket = card.get("ticket") or {}
    kind = str(card.get("kind") or "")
    if kind == "risk":
        lines += ["## [%s] RISK" % card["id"], "", "**Risk:** %s" % card.get("risk", ""), ""]
    else:
        lines += ["## [%s] %s" % (card["id"], kind), ""]
    lines += ["**Ticket:** #%s %s. %s (stage: %s)" % (ticket.get("issue"), ticket.get("title", ""),
                                                     ticket.get("purpose", ""), ticket.get("stage", "")), ""]
    lines += ["**Decision:** %s" % card.get("decision", ""), ""]
    lines += ["**Why it has come up:** %s" % card.get("why", ""), ""]
    if card.get("glossary"):
        lines += ["**Terms:** %s" % card["glossary"], ""]
    lines += ["**What changes under each option:**"]
    for opt in card.get("options") or []:
        lines.append("- `%s` %s: %s" % (opt.get("id"), opt.get("label"), opt.get("consequence")))
    lines.append("- Reversible later: %s" % ("yes" if card.get("reversible") else "no"))
    lines += ["", "**What the experts think:**"]
    for e in card.get("experts") or []:
        lines.append("- %s favours `%s`: %s" % (e.get("who"), e.get("favours"), e.get("why")))
    before = card.get("before") or {}
    lines += ["", "**What we decided before** (context only, nothing is pre-selected):"]
    items = before.get("principles") or []
    prior = before.get("decisions") or []
    for p in items:
        lines.append("- Principle %s (%s): %s" % (p.get("id"), p.get("title"), p.get("statement")))
    for d in prior:
        lines.append("- %s, #%s: \"%s\" (%s). %s" % (d.get("date"), d.get("issue"), d.get("answer_text"),
                                                    d.get("source"), d.get("reason")))
    if not items and not prior:
        lines.append("- Nothing relevant on record.")
    revisit = card.get("revisit")
    if revisit:
        lines += ["", "**A principle applies but this may be the exception:** %s. %s"
                  % (revisit.get("principle_id"), revisit.get("why"))]
    lines += ["", "**Recommendation:** `%s`. %s" % (card.get("recommended"), card.get("while_waiting", "")), ""]
    return lines


def render_md(batch: Dict[str, Any]) -> str:
    """The batch as one readable document, one section per card."""
    cards = [c for c in batch.get("cards") or [] if isinstance(c, dict)]
    lines = ["# Questions waiting for you at %s" % batch.get("stage", ""), "",
             "Reply in your own words, or one line per card as `<card id>: <option id>`.", ""]
    for card in cards:
        if card.get("status") == "answered":
            ans = card.get("answer") or {}
            lines += ["## [%s] answered" % card["id"], "", "%s: %s" % (card.get("decision", ""), ans.get("text", "")), ""]
        else:
            lines += _render_card(card)
    auto = batch.get("auto") or {}
    if auto:
        lines += ["## Settled without you", ""]
        for qid, a in auto.items():
            who = a.get("source", "")
            if who == "principle":
                who = "principle %s" % a.get("principle_id", "")
            lines.append("- %s: %s (%s). %s" % (qid, a.get("text", ""), who, a.get("reason", "")))
        lines.append("")
    return "\n".join(lines)


# -- replies ---------------------------------------------------------------------------------

_FAST_LINE = re.compile(r"^\s*([^\s:=]+)\s*[:=]\s*(\S+)\s*$")


def _fast_path(text: str, cards: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, str]]:
    picked: Dict[str, str] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    for line in lines:
        m = _FAST_LINE.match(line)
        if not m or m.group(1) not in cards:
            return None
        cid, oid = m.group(1), m.group(2)
        if oid not in [o["id"] for o in cards[cid]["options"]]:
            return None
        if picked.get(cid, oid) != oid:
            return None
        picked[cid] = oid
    return picked


def _interpret_items(out: Dict[str, Any], cards: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Confident, unambiguous placements from a voice result, and what was left unplaced."""
    unplaced = [str(u) for u in out.get("unplaced") or []]
    seen: Dict[str, List[Dict[str, Any]]] = {}
    for item in out.get("answers") or []:
        seen.setdefault(str(item.get("question_id")), []).append(item)
    placed: Dict[str, Dict[str, str]] = {}
    for cid, items in seen.items():
        card = cards.get(cid)
        if card is None:
            unplaced.append("an answer for an unknown question %s" % cid)
            continue
        keys = {(str(i.get("option_id") or ""), str(i.get("answer") or "")) for i in items}
        if len(keys) > 1:
            unplaced.append("%s: the reply gives conflicting answers" % cid)
            continue
        item = items[0]
        if item.get("confidence") != "high":
            unplaced.append("%s: not sure which answer was meant" % cid)
            continue
        oid = str(item.get("option_id") or "")
        if oid and oid not in [o["id"] for o in card["options"]]:
            unplaced.append("%s: option %s does not exist" % (cid, oid))
            continue
        placed[cid] = {"option_id": oid, "text": str(item.get("answer") or ""),
                       "reason": str(item.get("reason_quote") or "")}
    return placed, unplaced


def apply_reply(ctx: Ctx, text: str) -> Dict[str, Any]:
    """
    Take the human's reply. It is saved verbatim first. Confident answers close their cards;
    anything unplaced or contradictory leaves its card open; directions bind every later step.
    """
    flow = ctx.flow
    rel = record.save_human(flow, text)
    batch = open_batch(flow)
    if batch is None:
        raise RuntimeError("there is no open question batch to answer")
    cards = {str(c["id"]): c for c in batch["cards"] if c.get("status") == "open"}
    if not cards:
        voice.say(flow, text, source="reply")
        return {"answered": [], "still_open": [], "directions": [text],
                "summary": "No questions were open; your words were kept as a direction.", "complete": True}

    directions: List[str] = []
    unplaced: List[str] = []
    fast = _fast_path(text, cards)
    if fast is not None:
        placed = {cid: {"option_id": oid, "text": next(o["label"] for o in cards[cid]["options"] if o["id"] == oid),
                        "reason": ""} for cid, oid in fast.items()}
        record.save_interpretation(flow, rel, {
            "fast_path": True, "directions": [], "unplaced": [],
            "answers": [{"question_id": cid, "option_id": p["option_id"], "answer": p["text"],
                         "reason_quote": "", "confidence": "high"} for cid, p in placed.items()]})
    else:
        out = voice.interpret(ctx, text, list(cards.values()))
        record.save_interpretation(flow, rel, out)
        placed, unplaced = _interpret_items(out, cards)
        directions = [str(d.get("text")) for d in out.get("directions") or [] if isinstance(d, dict) and d.get("text")]

    answered: List[str] = []
    stage = str(batch.get("stage") or "")
    for cid, p in placed.items():
        card = cards[cid]
        answer = Answer(cid, p["option_id"], p["text"], "human", reason=p["reason"], verbatim_path=rel)
        card["status"] = "answered"
        card["answer"] = dataclasses.asdict(answer)
        _record(ctx, stage, card, answer)
        record.journal(flow, "Question %s answered by you: %s. %s" % (cid, answer.text, answer.reason))
        answered.append(cid)
    for d in directions:
        voice.say(flow, d, source="reply")
        record.journal(flow, "Direction from you: %s" % d)
    _save_batch(flow, batch)

    still_open = [cid for cid, c in cards.items() if c.get("status") == "open"]
    lines: List[str] = []
    for cid in answered:
        ans = cards[cid]["answer"]
        quote = ' (you said: "%s")' % ans["reason"] if ans.get("reason") else ""
        lines.append("Recorded %s: %s%s" % (cid, ans["text"], quote))
    for d in directions:
        lines.append("Direction noted, binding on later steps: %s" % d)
    for u in unplaced:
        lines.append("Could not place: %s" % u)
    for cid in still_open:
        lines.append("Still open: %s" % cid)
    if not lines:
        lines.append("Nothing in the reply could be placed.")
    return {"answered": answered, "still_open": still_open, "directions": directions,
            "summary": "\n".join(lines), "complete": not still_open}


# -- consuming -------------------------------------------------------------------------------

def take_answers(ctx: Ctx, stage: str) -> Dict[str, Answer]:
    """Every answer for a stage's batch, auto and human. Marks the batch consumed."""
    batch = open_batch(ctx.flow)
    if batch is None or batch.get("stage") != stage:
        return {}
    waiting = [str(c["id"]) for c in batch["cards"] if c.get("status") == "open"]
    if waiting:
        raise RuntimeError("questions still open at %s: %s" % (stage, ", ".join(waiting)))
    known = {f.name for f in dataclasses.fields(Answer)}

    def make(raw: Dict[str, Any]) -> Answer:
        return Answer(**{k: v for k, v in raw.items() if k in known})

    answers: Dict[str, Answer] = {}
    for qid, raw in (batch.get("auto") or {}).items():
        answers[str(qid)] = make(raw)
    for card in batch["cards"]:
        answers[str(card["id"])] = make(card["answer"])
    batch["status"] = "consumed"
    _save_batch(ctx.flow, batch)
    return answers


def answers_for_prompt(answers: Dict[str, Answer]) -> str:
    """Plain text for resuming a step: one block per question."""
    blocks: List[str] = []
    for qid, a in answers.items():
        blocks.append("\n".join([
            "Question %s" % qid,
            "  Chosen option: %s" % (a.option_id or "(none; free-text answer)"),
            "  Answer: %s" % a.text,
            "  Decided by: %s" % _by_who(a),
            "  Reason: %s" % (a.reason or "(none given)"),
        ]))
    return "\n\n".join(blocks)
