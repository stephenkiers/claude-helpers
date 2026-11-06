"""Shared agent/command markdown frontmatter parser for the flow guard test suites.

``tools`` and ``allowed-tools`` always come back as a list of strings (comma-split, so
``Bash(git diff:*)`` entries stay intact). Other keys come back as parsed by PyYAML, or, when
PyYAML is unavailable or the block is invalid YAML, as strings (``[...]``/``{...}`` literals are
evaluated with ``ast.literal_eval`` when they parse).
"""

import ast
import re

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_LIST_KEYS = ('tools', 'allowed-tools')


def parse_markdown_frontmatter(text):
    """Return the leading ``---`` frontmatter block of ``text`` as a dict ({} when absent)."""
    match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not match:
        return {}
    fm_text = match.group(1)

    result = {}
    if HAS_YAML:
        try:
            result = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            result = {}

    if result:
        for key in _LIST_KEYS:
            if isinstance(result.get(key), str):
                result[key] = [v.strip() for v in result[key].split(',')]
        return result

    for line in fm_text.split('\n'):
        if ':' not in line or line.strip().startswith('#'):
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()
        if key in _LIST_KEYS:
            result[key] = [v.strip() for v in value.split(',')]
        elif value.startswith(('[', '{')):
            try:
                result[key] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                result[key] = value
        else:
            result[key] = value
    return result
