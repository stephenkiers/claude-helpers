"""
Shared "unknown state" result type for fail-closed behavior.

Decision 8: Every operation layer (inspect/plan/apply) that hits a state
it doesn't recognize must return an explicit, structured "cannot determine"
result — never guess.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Unknown:
    """
    Explicit "cannot determine" result.

    Returned by any module that encounters a state it cannot resolve.
    The wrapper (Markdown command + model) receives this signal and either
    asks the user or makes the judgment call itself; the CLI's job stops
    at reporting "I don't know what this is."
    """
    reason: str

    def __bool__(self) -> bool:
        """Unknown is always falsy in boolean contexts."""
        return False

    def __str__(self) -> str:
        return f"Unknown: {self.reason}"


def is_unknown(value) -> bool:
    """Check if a value is an Unknown result."""
    return isinstance(value, Unknown)
