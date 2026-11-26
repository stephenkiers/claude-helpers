"""
Answering in your own words (PLAN.md section 6): read a free-text reply against the open question
cards, and keep the directions the human gives, which bind every later step of the flow.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from scripts.flow3 import prompts, record, schemas
from scripts.flow3.context import Ctx
from scripts.flow3.state import FlowDir
from scripts.flow3.types import CallSpec, FlowStop


def _compact(card: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": card.get("id"),
        "decision": card.get("decision"),
        "options": [{"id": o.get("id"), "label": o.get("label")} for o in card.get("options") or []],
        "recommended": card.get("recommended"),
    }


def interpret(ctx: Ctx, text: str, cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One `voice` call (no tools): the reply read against the open cards. Raises FlowStop on failure."""
    from scripts.flow3.questions import _call  # questions imports this module at the top

    prompt = prompts.render(
        "voice", reply=text, cards=json.dumps([_compact(c) for c in cards], indent=2, sort_keys=True))
    spec = CallSpec(role="voice", prompt=prompt, schema=schemas.VOICE, cwd=ctx.repo_root,
                    tools=(), label="reply")
    result = _call(ctx, spec)
    if not result.ok or not isinstance(result.output, dict):
        raise FlowStop("runner", "could not read the reply against the open questions",
                       evidence=result.subtype or result.result_text[:300] or None)
    return result.output


def say(flow: FlowDir, text: str, source: str = "say") -> None:
    """Save a direction verbatim and add it to the flow's directions (reaches the next step to start)."""
    rel = record.save_human(flow, text, kind="direction")
    entry = {"text": text, "source": source, "ts": datetime.now(timezone.utc).isoformat(), "verbatim": rel}

    def apply(doc: Dict[str, Any]) -> None:
        doc.setdefault("directions", []).append(entry)

    flow.update(apply)


def directions_text(flow: FlowDir) -> str:
    """A numbered list for step prompts, or "" when the human has given no directions."""
    items = [d for d in (flow.get("directions") or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if not items:
        return ""
    lines = ["Directions from the human, binding on this step:"]
    for n, d in enumerate(items, 1):
        lines.append("%d. %s" % (n, str(d["text"]).strip()))
    return "\n".join(lines)
