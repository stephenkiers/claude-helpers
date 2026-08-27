"""
Shared "unknown state" result type for fail-closed behavior.

Decision 8: Every operation layer (inspect/plan/apply) that hits a state
it doesn't recognize must return an explicit, structured "cannot determine"
result — never guess.
"""

import functools
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

    Truthiness uses the dataclass default (always True): callers use the
    `if err:` idiom on the error/unknown channel to detect a signaled
    Unknown, and a hardcoded-False __bool__ would make that check never
    fire.
    """
    reason: str

    def __str__(self) -> str:
        return f"Unknown: {self.reason}"


def is_unknown(value) -> bool:
    """Check if a value is an Unknown result."""
    return isinstance(value, Unknown)


def fail_closed(func):
    """
    Decision 8 enforcement: wrapped functions must return a tuple whose
    last element is either None or an Unknown instance — the explicit
    "cannot determine" channel. Raises TypeError at call time if a
    function violates its own contract, catching a new instance of the
    "silently collapsed unknown" bug structurally rather than by review.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if not isinstance(result, tuple) or len(result) < 2:
            raise TypeError(
                f"{func.__name__} is decorated with @fail_closed but returned "
                f"{result!r}, which is not a tuple of at least 2 elements"
            )
        error_slot = result[-1]
        if error_slot is not None and not isinstance(error_slot, Unknown):
            raise TypeError(
                f"{func.__name__} is decorated with @fail_closed but its "
                f"error channel is {error_slot!r}, not None or Unknown"
            )
        return result
    return wrapper
