"""
Defaults for Flow v3: which model each role runs on and the per-call limits (PLAN.md section 7).

A repo overrides models in its flow.yaml under `models:` (role -> model). The key "*" replaces
every role's model at once, which is how a sandbox run is kept cheap.
"""

from typing import Dict, Mapping, Tuple

# role -> default model alias
MODELS: Dict[str, str] = {
    "plan_router": "haiku",
    "plan_expert": "sonnet",
    "plan_synthesis": "opus",
    "plan_auditor": "sonnet",
    "test_author": "sonnet",
    "implementer": "haiku",
    "fixer": "haiku",
    "judge": "sonnet",
    "edit_judge": "sonnet",
    "conflict_resolver": "sonnet",
    "review_scout": "haiku",
    "reviewer": "sonnet",
    "verifier": "sonnet",
    "triage": "opus",
    "fix_verify": "sonnet",
    "resolver": "sonnet",
    "fact_scout": "haiku",
    "voice": "sonnet",
    "pr_text": "haiku",
}

# The model a role escalates to when the cheap one falls short (PLAN.md section 5).
STRONGER: Dict[str, str] = {"haiku": "sonnet", "sonnet": "opus", "opus": "opus"}

# role -> (max_turns, timeout_secs)
LIMITS: Dict[str, Tuple[int, int]] = {
    "plan_router": (6, 180),
    "plan_expert": (30, 900),
    "plan_synthesis": (30, 1200),
    "plan_auditor": (25, 900),
    "test_author": (40, 1200),
    "implementer": (60, 1800),
    "fixer": (40, 1200),
    "judge": (20, 600),
    "edit_judge": (10, 300),
    "conflict_resolver": (30, 900),
    "review_scout": (15, 600),
    "reviewer": (25, 900),
    "verifier": (30, 900),
    "triage": (15, 900),
    "fix_verify": (20, 600),
    "resolver": (10, 300),
    "fact_scout": (15, 300),
    "voice": (6, 300),
    "pr_text": (6, 180),
}
DEFAULT_LIMITS: Tuple[int, int] = (20, 600)

# Shared cap on concurrent model calls across every flow on this machine (PLAN.md section 10).
DEFAULT_SLOTS = 6
# How long a `limit` failure may wait before the flow stops (seconds).
LIMIT_WAIT_CEILING_SECS = 6 * 3600
# Notional spend per ticket (USD): soft adds an inbox line, hard is a `budget` stop.
SOFT_BUDGET_USD = 15.0
HARD_BUDGET_USD = 40.0
# Review rounds: the first, plus at most two more after fixes (PLAN.md section 7.6).
MAX_REVIEW_ROUNDS = 3
# Attempts at the merge when base keeps moving (PLAN.md section 7.8).
MAX_MERGE_ATTEMPTS = 3


def model_for(role: str, overrides: Mapping[str, str]) -> str:
    """The model for a role: the repo's override for that role, else its "*" override, else the default."""
    if role in overrides:
        return overrides[role]
    if "*" in overrides:
        return overrides["*"]
    if role not in MODELS:
        raise KeyError(f"unknown role: {role}")
    return MODELS[role]


def stronger(model: str) -> str:
    """The next model up, for escalation. An unknown model escalates to sonnet."""
    return STRONGER.get(model, "sonnet")


def limits_for(role: str) -> Tuple[int, int]:
    """(max_turns, timeout_secs) for a role."""
    return LIMITS.get(role, DEFAULT_LIMITS)
