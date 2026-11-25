"""
JSON schemas for every model call's structured output, plus the small validator the driver runs on
what comes back (the CLI's own validation is not treated as a contract; see docs/v3/probes.md).

Schemas stay within a plain subset: object, array, string, boolean, integer, number, enum, required,
additionalProperties, minItems, minLength.
"""

from typing import Any, Dict, List, Optional

Schema = Dict[str, Any]

_KINDS = ["purpose", "architecture", "behaviour", "risk", "detail", "fact"]


def _obj(props: Dict[str, Schema], required: Optional[List[str]] = None) -> Schema:
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()) if required is None else required,
        "additionalProperties": False,
    }


def _arr(items: Schema, min_items: int = 0) -> Schema:
    out: Schema = {"type": "array", "items": items}
    if min_items:
        out["minItems"] = min_items
    return out


_STR: Schema = {"type": "string"}
_TEXT: Schema = {"type": "string", "minLength": 1}
_BOOL: Schema = {"type": "boolean"}
_INT: Schema = {"type": "integer"}

# A question a step returns instead of asking the human (PLAN.md section 6). The resolver decides who
# answers it. Every part of the card the human will read must be here.
QUESTION: Schema = _obj(
    {
        "id": _TEXT,
        "question": _TEXT,  # the decision, one sentence, plain words
        "context": _TEXT,  # what the step was doing when this came up, naming the file or behaviour
        "options": _arr(_obj({"id": _TEXT, "label": _TEXT, "consequence": _TEXT}), 2),
        "recommended": _TEXT,  # an option id
        "kind": {"type": "string", "enum": _KINDS},
        "reversible": _BOOL,  # can the choice be undone later
        "expert_views": _arr(_obj({"expert": _TEXT, "favours": _TEXT, "why": _TEXT})),
        "themes": _arr(_STR),  # short tags used to find earlier decisions on the same theme
        "risk": _STR,  # for kind "risk": what would no longer be protected or proven
    },
    required=["id", "question", "context", "options", "recommended", "kind", "reversible"],
)
QUESTIONS: Schema = _arr(QUESTION)

_FINDING: Schema = _obj(
    {
        "id": _TEXT,
        "lens": _TEXT,  # the reviewer name
        "file": _STR,
        "line": _INT,
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "title": _TEXT,
        "detail": _TEXT,
        "suggestion": _STR,
    },
    required=["id", "lens", "file", "severity", "title", "detail"],
)

_UNIT: Schema = _obj(
    {
        "id": _TEXT,
        "title": _TEXT,
        "files": _arr(_TEXT, 1),  # paths (relative to the repo root) the unit may create or change
        "steps": _arr(_TEXT, 1),
        "acceptance": _arr(_TEXT, 1),  # ids of acceptance criteria this unit satisfies
        "after": _arr(_STR),  # ids of units that must run first
    },
    required=["id", "title", "files", "steps", "acceptance"],
)

_PLAN: Schema = _obj(
    {
        "goal": _TEXT,
        "non_goals": _arr(_STR),
        "acceptance_criteria": _arr(_obj({"id": _TEXT, "text": _TEXT}), 1),
        "units": _arr(_UNIT, 1),
        "interfaces": _arr(_obj({"name": _TEXT, "signature": _TEXT, "file": _STR}, required=["name", "signature"])),
        "test_spec": _arr(_obj({"id": _TEXT, "behaviour": _TEXT, "test_file": _TEXT})),
        "untestable_reason": _STR,  # required non-empty when test_spec is empty
        "existing_test_edits": _arr(_obj({"file": _TEXT, "why": _TEXT})),
    },
    required=["goal", "acceptance_criteria", "units", "test_spec"],
)

PLAN_ROUTER: Schema = _obj({"experts": _arr(_TEXT, 1), "why": _STR}, required=["experts"])

PLAN_EXPERT: Schema = _obj(
    {
        "advice": _arr(_TEXT),
        "risks": _arr(_STR),
        "questions": QUESTIONS,
    },
    required=["advice"],
)

# Synthesis returns either questions to settle first, or the plan.
PLAN_SYNTHESIS: Schema = _obj(
    {
        "status": {"type": "string", "enum": ["needs_answers", "done"]},
        "questions": QUESTIONS,
        "plan": _PLAN,
    },
    required=["status"],
)

PLAN_AUDIT: Schema = _obj(
    {
        "verdict": {"type": "string", "enum": ["pass", "gaps"]},
        "gaps": _arr(_obj({"text": _TEXT, "kind": {"type": "string", "enum": ["plan_gap", "unasked_question"]}})),
        "questions": QUESTIONS,  # one per gap of kind unasked_question
    },
    required=["verdict"],
)

# A call that edits files reports what it believes it did. The driver reads git for the facts.
WORK: Schema = _obj(
    {
        "status": {"type": "string", "enum": ["done", "partial", "needs_answers"]},
        "summary": _TEXT,
        "remaining": _arr(_STR),  # what is left, when partial
        "questions": QUESTIONS,
    },
    required=["status", "summary"],
)
TEST_AUTHOR: Schema = WORK
IMPLEMENTER: Schema = WORK
FIXER: Schema = WORK
CONFLICT_RESOLVER: Schema = WORK

# Why a spec test is still red (PLAN.md section 7.4).
JUDGE: Schema = _obj(
    {
        "verdict": {"type": "string", "enum": ["code_wrong", "test_wrong", "spec_wrong"]},
        "reason": _TEXT,
        "edit_weakens": _BOOL,  # for test_wrong: would the correcting edit weaken or remove an assertion
        "test_edit": _STR,  # for test_wrong: what to change in the test, in words
    },
    required=["verdict", "reason"],
)

# Classify each change to an existing test (PLAN.md section 12). Unsure must be "weakens".
EDIT_JUDGE: Schema = _obj(
    {
        "edits": _arr(
            _obj(
                {
                    "file": _TEXT,
                    "verdict": {"type": "string", "enum": ["keeps_or_tightens", "weakens"]},
                    "reason": _TEXT,
                    "no_longer_proven": _STR,
                },
                required=["file", "verdict", "reason"],
            )
        )
    }
)

REVIEW_SCOUT: Schema = _obj(
    {
        "lenses": _arr(
            _obj({"lens": _TEXT, "candidates": _arr(_obj({"file": _STR, "line": _INT, "note": _TEXT}, required=["note"]))}),
            1,
        )
    }
)

REVIEWER: Schema = _obj({"findings": _arr(_FINDING)})

VERIFIER: Schema = _obj(
    {
        "verdicts": _arr(
            _obj(
                {
                    "finding_id": _TEXT,
                    "verdict": {"type": "string", "enum": ["confirmed", "refuted", "needs_measurement"]},
                    "evidence": _TEXT,
                    "command": _STR,  # for needs_measurement: a command that would settle it
                },
                required=["finding_id", "verdict", "evidence"],
            )
        )
    }
)

TRIAGE: Schema = _obj(
    {
        "doing_it": _arr(_obj({"finding_id": _TEXT, "fix": _TEXT, "files": _arr(_STR)}, required=["finding_id", "fix"])),
        "needs_you": _arr(_obj({"finding_id": _TEXT, "question": QUESTION})),
        "needs_measurement": _arr(
            _obj(
                {"finding_id": _TEXT, "command": _TEXT, "read_only": _BOOL, "confirms_if": _TEXT},
                required=["finding_id", "command", "read_only", "confirms_if"],
            )
        ),
        "deferred": _arr(_obj({"finding_id": _TEXT, "reason": _TEXT})),
        "gut_check": _STR,
    },
    required=["doing_it", "needs_you", "needs_measurement", "deferred"],
)

FIX_VERIFY: Schema = _obj(
    {"results": _arr(_obj({"finding_id": _TEXT, "addressed": _BOOL, "evidence": _TEXT}))}
)

FACT_SCOUT: Schema = _obj(
    {
        "answers": _arr(
            _obj(
                {
                    "question_id": _TEXT,
                    "found": _BOOL,  # false when it could not be checked; the question then goes on
                    "option_id": _STR,
                    "answer": _STR,
                    "evidence": _STR,
                },
                required=["question_id", "found"],
            )
        )
    }
)

# One entry per question. "decides" answers it; "revisit" means a principle applies but this case looks
# like its "Revisit if"; "none" means no principle decides it.
PRINCIPLE_RESOLVER: Schema = _obj(
    {
        "results": _arr(
            _obj(
                {
                    "question_id": _TEXT,
                    "outcome": {"type": "string", "enum": ["decides", "revisit", "none"]},
                    "principle_id": _STR,
                    "option_id": _STR,
                    "reason": _STR,
                },
                required=["question_id", "outcome"],
            )
        )
    }
)

# Interpret a free-text or dictated reply against the open cards (PLAN.md section 6).
VOICE: Schema = _obj(
    {
        "answers": _arr(
            _obj(
                {
                    "question_id": _TEXT,
                    "option_id": _STR,
                    "answer": _TEXT,
                    "reason_quote": _STR,  # the human's own words
                    "confidence": {"type": "string", "enum": ["high", "low"]},
                },
                required=["question_id", "answer", "confidence"],
            )
        ),
        "directions": _arr(_obj({"text": _TEXT, "quote": _STR}, required=["text"])),
        "unplaced": _arr(_STR),  # things said that could not be placed, or contradictions
    },
    required=["answers", "directions"],
)

PR_TEXT: Schema = _obj({"title": _TEXT, "body": _TEXT})

# Proposed principles from the decision log.
PRINCIPLE_SUGGEST: Schema = _obj(
    {
        "proposals": _arr(
            _obj(
                {"id": _TEXT, "title": _TEXT, "statement": _TEXT, "spirit": _TEXT, "revisit_if": _TEXT,
                 "from_decisions": _arr(_STR)}
            )
        )
    }
)

BY_ROLE: Dict[str, Schema] = {
    "plan_router": PLAN_ROUTER,
    "plan_expert": PLAN_EXPERT,
    "plan_synthesis": PLAN_SYNTHESIS,
    "plan_auditor": PLAN_AUDIT,
    "test_author": TEST_AUTHOR,
    "implementer": IMPLEMENTER,
    "fixer": FIXER,
    "conflict_resolver": CONFLICT_RESOLVER,
    "judge": JUDGE,
    "edit_judge": EDIT_JUDGE,
    "review_scout": REVIEW_SCOUT,
    "reviewer": REVIEWER,
    "verifier": VERIFIER,
    "triage": TRIAGE,
    "fix_verify": FIX_VERIFY,
    "fact_scout": FACT_SCOUT,
    "resolver": PRINCIPLE_RESOLVER,
    "voice": VOICE,
    "pr_text": PR_TEXT,
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate(instance: Any, schema: Schema, path: str = "$") -> List[str]:
    """Errors (empty when valid) for instance against the schema subset this module uses."""
    errors: List[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_ok(instance, expected):
        return [f"{path}: expected {expected}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, str) and len(instance) < int(schema.get("minLength", 0)):
        errors.append(f"{path}: string is shorter than {schema['minLength']}")
    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                errors.extend(validate(item, items, f"{path}[{index}]"))
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        for name, value in instance.items():
            if name in props:
                errors.extend(validate(value, props[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")
    return errors
