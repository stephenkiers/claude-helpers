"""Prompt templates: prompts/flow3/<name>.md with {{key}} placeholders."""

import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "flow3"
_PLACEHOLDER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


def render(name: str, **values: str) -> str:
    """Fill a template. A placeholder with no value, or a missing file, is an error.

    Values are inserted once and never re-scanned, so `{{...}}` inside a value (a diff, an issue
    body) is left as it is.
    """
    path = PROMPT_DIR / (name + ".md")
    if not path.is_file():
        raise FileNotFoundError("no prompt template: %s" % path)
    text = path.read_text(encoding="utf-8")
    missing = sorted(set(_PLACEHOLDER.findall(text)) - set(values))
    if missing:
        raise KeyError("prompt %s is missing values for: %s" % (name, ", ".join(missing)))
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
