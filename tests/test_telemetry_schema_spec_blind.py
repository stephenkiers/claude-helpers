#!/usr/bin/env python3
"""
Spec-blind test suite for telemetry_schema.parse_transcript_tokens and _content_chars
(issue #218: explain missing subagent output tokens).

Written from the plan specification alone, without reading the implementation. Covers:
1. Unfinalized rule is "LAST transcript line for an id has stop_reason null":
   null-null-end_turn excludes the id's content chars; end_turn-then-null counts them.
2. Content chars accumulate per id across per-line deltas (unfinalized path).
3. Unfinalized accounting never raises on a non-dict usage (treated as {}).
4. unfinalized_output_tokens_recorded rejects bool; headline tokens["output"] does not.
5. _content_chars: text counts codepoints, thinking counts its text, tool_use counts
   len(json.dumps(input)), set() tool input -> 0, non-list content -> 0, malformed blocks
   contribute 0 while the rest are summed, never raises for a dict argument.
6. TranscriptParseResult is a TypedDict carrying the documented keys.

Run with: python3 tests/test_telemetry_schema_spec_blind.py
"""

import json
import sys
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import telemetry_schema  # noqa: E402

h = Harness("TELEMETRY SCHEMA TRANSCRIPT PARSE SPEC-BLIND TEST SUITE (#218)")
t = h.test_result


def _asst(msg_id, stop, out=5, inp=1, content=None, usage=None):
    """Build one assistant transcript line."""
    if usage is None:
        usage = {"input_tokens": inp, "output_tokens": out}
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "stop_reason": stop,
            "usage": usage,
            "content": content if content is not None else [],
        },
    }


def _text(s):
    return {"type": "text", "text": s}


def _parse(lines):
    """Write lines as JSONL to a temp file and run parse_transcript_tokens on it."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "transcript.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        return telemetry_schema.parse_transcript_tokens(p)


# =============================================================================
# Part 1: "last line decides" unfinalized rule
# =============================================================================
print("\n[Part 1] unfinalized: last transcript line per id decides")

# Test 1.1: null, null, end_turn for one id -> finalized; its chars are excluded.
try:
    r = _parse([
        _asst("msg_a", None, out=3, content=[_text("aaa")]),
        _asst("msg_a", None, out=3, content=[_text("bbbb")]),
        _asst("msg_a", "end_turn", out=9, content=[_text("cc")]),
    ])
    ok = (
        r["unfinalized_messages"] == 0
        and r["unfinalized_content_chars"] == 0
        and r["unfinalized_output_tokens_recorded"] == 0
    )
    t(
        "null-null-end_turn for one id: unfinalized_messages == 0 and chars excluded",
        ok,
        f"got messages={r['unfinalized_messages']} chars={r['unfinalized_content_chars']} "
        f"recorded={r['unfinalized_output_tokens_recorded']}",
    )
except Exception as e:  # noqa: BLE001
    t("null-null-end_turn for one id: unfinalized_messages == 0 and chars excluded", False, f"raised {e!r}")

# Test 1.2: end_turn then null for one id -> unfinalized; its chars are counted.
try:
    r = _parse([
        _asst("msg_b", "end_turn", out=4, content=[_text("abc")]),
        _asst("msg_b", None, out=4, content=[_text("de")]),
    ])
    t(
        "end_turn-then-null for one id: unfinalized_messages == 1",
        r["unfinalized_messages"] == 1,
        f"got {r['unfinalized_messages']}",
    )
    t(
        "end_turn-then-null for one id: its content chars are counted (3 + 2 == 5)",
        r["unfinalized_content_chars"] == 5,
        f"got {r['unfinalized_content_chars']}",
    )
except Exception as e:  # noqa: BLE001
    t("end_turn-then-null for one id counts as unfinalized", False, f"raised {e!r}")

# =============================================================================
# Part 2: per-line delta accumulation for unfinalized ids
# =============================================================================
print("\n[Part 2] content chars accumulate per id across per-line deltas")

# Test 2.1: two null lines of the same id, each carrying its own block -> summed.
try:
    r = _parse([
        _asst("msg_c", None, out=2, content=[_text("xx")]),
        _asst("msg_c", None, out=2, content=[_text("yyy")]),
    ])
    t(
        "two null lines for one id: unfinalized_messages == 1 (not per-line)",
        r["unfinalized_messages"] == 1,
        f"got {r['unfinalized_messages']}",
    )
    t(
        "two null lines for one id: content chars summed across lines (2 + 3 == 5)",
        r["unfinalized_content_chars"] == 5,
        f"got {r['unfinalized_content_chars']}",
    )
except Exception as e:  # noqa: BLE001
    t("two null lines for one id accumulate content chars", False, f"raised {e!r}")

# =============================================================================
# Part 3: unfinalized accounting tolerates non-dict usage
# =============================================================================
print("\n[Part 3] unfinalized accounting never raises on non-dict usage")

# Test 3.1: stop_reason null with usage [] -> treated as {}; still unfinalized, chars counted.
try:
    r = _parse([
        _asst("msg_d", None, content=[_text("abcd")], usage=[]),
    ])
    t(
        "null stop_reason with usage=[] does not raise and counts as unfinalized",
        r["unfinalized_messages"] == 1,
        f"got {r['unfinalized_messages']}",
    )
    t(
        "null stop_reason with usage=[] contributes its content chars (4)",
        r["unfinalized_content_chars"] == 4,
        f"got {r['unfinalized_content_chars']}",
    )
    t(
        "null stop_reason with usage=[] records 0 unfinalized output tokens",
        r["unfinalized_output_tokens_recorded"] == 0,
        f"got {r['unfinalized_output_tokens_recorded']}",
    )
except Exception as e:  # noqa: BLE001
    t("null stop_reason with usage=[] does not raise", False, f"raised {e!r}")

# =============================================================================
# Part 4: bool handling in output token sums
# =============================================================================
print("\n[Part 4] bool output_tokens: counted in headline sum (unfinalized-sum rejection is pinned by the named suite)")

# Test 4.2: headline tokens["output"] does NOT reject bool (True contributes 1).
try:
    r = _parse([
        _asst("msg_g", "end_turn", out=True),
        _asst("msg_h", "end_turn", out=4),
    ])
    t(
        "headline tokens['output'] counts bool True as 1 (1 + 4 == 5)",
        r["tokens"]["output"] == 5,
        f"got {r['tokens']['output']}",
    )
except Exception as e:  # noqa: BLE001
    t("headline tokens['output'] counts bool True as 1", False, f"raised {e!r}")

# =============================================================================
# Part 5: _content_chars direct contract
# =============================================================================
print("\n[Part 5] _content_chars direct contract")

cc = telemetry_schema._content_chars

# Test 5.1: text blocks count raw codepoints (not UTF-8 bytes).
try:
    got = cc({"content": [_text("héllo\U0001F600")]})
    t("text block counts codepoints: 'héllo😀' == 6", got == 6, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("text block counts codepoints", False, f"raised {e!r}")

# Test 5.2: thinking blocks count their thinking text.
try:
    got = cc({"content": [{"type": "thinking", "thinking": "abcd"}]})
    t("thinking block counts its 'thinking' text (4)", got == 4, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("thinking block counts its thinking text", False, f"raised {e!r}")

# Test 5.3: tool_use counts len(json.dumps(input)) with default ASCII escaping.
try:
    inp = {"k": "é"}
    got = cc({"content": [{"type": "tool_use", "id": "t1", "name": "x", "input": inp}]})
    # json.dumps({"k": "é"}) == '{"k": "\\u00e9"}' -> 15 chars
    t(
        "tool_use counts len(json.dumps(input)) with ASCII escapes (15)",
        got == len(json.dumps(inp)) == 15,
        f"got {got}",
    )
except Exception as e:  # noqa: BLE001
    t("tool_use counts len(json.dumps(input))", False, f"raised {e!r}")

# Test 5.4: set() tool input -> 0 via the TypeError branch; neighbouring text still counts.
try:
    got = cc({"content": [
        _text("ab"),
        {"type": "tool_use", "id": "t2", "name": "x", "input": set([1, 2])},
    ]})
    t("set() tool input contributes 0 while text block still counts (2)", got == 2, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("set() tool input contributes 0 and does not raise", False, f"raised {e!r}")

# Test 5.5: non-list content -> 0, never raises.
try:
    got = [cc({"content": c}) for c in ("abc", None, {"type": "text", "text": "zz"}, 7)]
    t("non-list content contributes 0 for str/None/dict/int", got == [0, 0, 0, 0], f"got {got}")
except Exception as e:  # noqa: BLE001
    t("non-list content contributes 0 and does not raise", False, f"raised {e!r}")

# Test 5.6: malformed blocks contribute 0, the rest are summed, nothing raises.
try:
    got = cc({"content": [
        None,
        "str-block",
        5,
        {"type": "text"},
        {"type": "text", "text": 42},
        {"type": "tool_use", "id": "t3", "name": "x"},
        _text("xyz"),
    ]})
    t("malformed blocks contribute 0 and valid text still sums (3)", got == 3, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("malformed blocks do not raise", False, f"raised {e!r}")

# Test 5.7: per-block sum across mixed block types.
try:
    got = cc({"content": [
        _text("ab"),
        {"type": "tool_use", "id": "t4", "name": "x", "input": {"a": 1}},
        {"type": "thinking", "thinking": "c"},
    ]})
    expected = 2 + len(json.dumps({"a": 1})) + 1
    t(f"mixed blocks sum per block type ({expected})", got == expected, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("mixed blocks sum per block type", False, f"raised {e!r}")

# Test 5.8: empty message dict -> 0 (missing content key), no raise.
try:
    got = cc({})
    t("empty message dict contributes 0 without raising", got == 0, f"got {got}")
except Exception as e:  # noqa: BLE001
    t("empty message dict contributes 0 without raising", False, f"raised {e!r}")

# =============================================================================
# Part 6: TranscriptParseResult shape
# =============================================================================
print("\n[Part 6] TranscriptParseResult TypedDict shape")

try:
    expected_keys = {
        "tokens", "turns", "lines_parsed", "lines_skipped", "cost_state",
        "first_assistant_event", "unfinalized_messages",
        "unfinalized_output_tokens_recorded", "unfinalized_content_chars",
    }
    rtd = telemetry_schema.TranscriptParseResult
    ann = set(getattr(rtd, "__annotations__", {}).keys())
    t(
        "TranscriptParseResult declares exactly the documented keys",
        ann == expected_keys,
        f"missing={expected_keys - ann} extra={ann - expected_keys}",
    )
except Exception as e:  # noqa: BLE001
    t("TranscriptParseResult is importable and declares documented keys", False, f"raised {e!r}")

# =============================================================================
# Part 7: cost_state selection
# =============================================================================
print("\n[Part 7] cost_state is the last cost-state line, verbatim")

# Test 7.1: two cost-state lines -> the later one wins; assistant lines are never chosen.
try:
    r = _parse([
        {"type": "cost-state", "modelUsage": {"outputTokens": 1}},
        _asst("msg_i", "end_turn", out=4),
        {"type": "cost-state", "modelUsage": {"outputTokens": 9}},
    ])
    t(
        "cost_state is the last cost-state event, verbatim",
        r["cost_state"] == {"type": "cost-state", "modelUsage": {"outputTokens": 9}},
        f"got {r['cost_state']!r}",
    )
except Exception as e:  # noqa: BLE001
    t("cost_state is the last cost-state event", False, f"raised {e!r}")

h.summarize_and_exit()
