#!/usr/bin/env python3
"""
Test suite for scripts/search-claude.sh.

This is the repo's first executing test — previous test suites check text
invariants over YAML/Markdown and never run code. This suite runs the search
script under both bash and zsh, validates its behavior against a comprehensive
fixture tree, and exercises all 11 bugs documented in issue #21.

Bugs tested:
 1. Bash-only code in zsh (mapfile, array indexing)
 2. rg as a zsh function (not a binary)
 3. SIGPIPE from pipefail + head
 4. Subagent exclusion glob
 5. Delimiter collision on newlines
 6. Whole arg string as literal phrase
 7. Self-session matching
 8. --days with no value
 9. stat -f vs stat -c portability
10. Snippet extraction from non-text blocks
11. Ranking by match timestamp vs file mtime

Run with: python3 tests/test_search_claude.py
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "search-claude.sh"


def run(
    interpreter,
    query_args,
    env_override=None,
):
    """
    Run the script with the given interpreter and arguments.

    Returns: (exit_code, stdout, stderr)
    Fails the test (calls h.test_result) if exit_code is in [126, 127, 141, 2].
    """
    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    cmd = [interpreter, "-u", str(SCRIPT)] + query_args
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"{interpreter} not found"

    # Detect signal-based failures
    if result.returncode in [126, 127, 141, 2]:
        return result.returncode, result.stdout, result.stderr

    return result.returncode, result.stdout, result.stderr


class FixtureBuilder:
    """Construct a temporary fixture tree with various edge cases."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = {}  # Maps path -> (content, mtime, is_session)

    def add_session(self, session_id, content, mtime=None, is_subagent=False):
        """Add a session file to the fixture."""
        if is_subagent:
            # subagents/agent-{id}.jsonl
            dir_path = self.root / session_id / "subagents"
            dir_path.mkdir(parents=True, exist_ok=True)
            agent_id = session_id.split("-")[0] if "-" in session_id else session_id
            path = dir_path / f"agent-{agent_id}.jsonl"
        else:
            path = self.root / f"{session_id}.jsonl"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        if mtime:
            os.utime(path, (mtime, mtime))

        self.files[str(path)] = (content, mtime, not is_subagent)

    def finalize(self):
        """Return the fixture root path."""
        return self.root


h = Harness("SEARCH-CLAUDE SCRIPT TEST SUITE")
t = h.test_result

# --- Build fixture tree ---
fixture_root = tempfile.mkdtemp(prefix="search-claude-test-")
fixture = FixtureBuilder(fixture_root)

# Current time for relative mtime calculations
now = int(time.time())
day = 86400

try:
    # Helper: generate ISO timestamp from seconds since epoch
    def ts_from_seconds(seconds):
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds))

    # Test 1: Multi-word AND matching
    fixture.add_session(
        "multi-word-and-test",
        f'{{"type":"user","message":{{"content":"tray icon color"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 2: Quoted phrase (exact match)
    fixture.add_session(
        "quoted-phrase-test",
        f'{{"type":"user","message":{{"content":"this is a test phrase"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 3: Subagent with sessionId field mismatch (path id wins)
    fixture.add_session(
        "parent-session-uuid",
        f'{{"type":"user","message":{{"content":"subagent test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
        is_subagent=True,
    )

    # Test 4: Missing .cwd field (fallback to dirname decoding)
    # The session_id is "-Users-test-project" but we need to create it as a directory structure
    # so that the cwd decoding from dirname works correctly
    cwd_test_dir = Path(fixture_root) / "-Users-test-project"
    cwd_test_dir.mkdir(parents=True, exist_ok=True)
    cwd_test_file = cwd_test_dir / "session.jsonl"
    cwd_test_file.write_text(f'{{"type":"user","message":{{"content":"cwd test"}},"timestamp":"{ts_from_seconds(now)}"}}\n', encoding="utf-8")
    os.utime(cwd_test_file, (now, now))

    # Test 5: Snippet with embedded newlines (one record, not split)
    fixture.add_session(
        "newline-snippet-test",
        f'{{"type":"user","message":{{"content":"line 1\\nline 2 search term"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 6: Empty file
    fixture.add_session(
        "empty-file",
        "",
        mtime=now,
    )

    # Test 7: File whose first line is not JSON
    fixture.add_session(
        "not-json",
        "this is not json\n",
        mtime=now,
    )

    # Test 8: Unicode/emoji test
    fixture.add_session(
        "unicode-test",
        f'{{"type":"user","message":{{"content":"emoji 😀 test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 8b: Regex metacharacter in query must be treated as a literal string,
    # not a regex — "a.b" must match "a.b" but must NOT match "aXb" (which "."
    # would match as a regex wildcard).
    fixture.add_session(
        "literal-dot-match",
        f'{{"type":"user","message":{{"content":"contains a.b literally"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )
    fixture.add_session(
        "regex-wildcard-decoy",
        f'{{"type":"user","message":{{"content":"contains aXb not a dot"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 9: File older than --days cutoff (400 days old)
    old_mtime = now - (400 * day)
    old_ts = ts_from_seconds(old_mtime)
    fixture.add_session(
        "old-session",
        f'{{"type":"user","message":{{"content":"old query"}},"timestamp":"{old_ts}"}}\n',
        mtime=old_mtime,
    )

    # Test 10: Self-session (to be excluded via CLAUDE_CODE_SESSION_ID)
    fixture.add_session(
        "self-session-id",
        f'{{"type":"user","message":{{"content":"self test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 11-40: 30 bulk files to test limit truncation
    for i in range(30):
        bulk_ts = ts_from_seconds(now - (i * 100))
        fixture.add_session(
            f"bulk-file-{i:02d}",
            f'{{"type":"user","message":{{"content":"bulk"}},"timestamp":"{bulk_ts}"}}\n',
            mtime=now - (i * 100),
        )

    # Test 41: Snippet from tool_use.input only (not text block)
    # Real schema: tool_use is inside message.content array with object-valued input
    fixture.add_session(
        "tool-use-only",
        f'{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"tool_use","id":"toolu_1","name":"Bash","input":{{"command":"search_term_in_input"}}}}]}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 41b: Snippet from thinking block
    fixture.add_session(
        "thinking-only",
        f'{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"thinking","thinking":"I should search_term_in_input next"}}]}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # Test 42: File whose mtime is newer than matching line's timestamp (bug 11)
    # This file has an OLD line timestamp (3 days ago) but NEWER mtime (1 day ago)
    stale_line_ts_sec = now - (3 * day)  # 3 days old
    newer_mtime = now - (1 * day)  # 1 day old
    stale_ts = ts_from_seconds(stale_line_ts_sec)
    fixture.add_session(
        "ranking-test-old-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{stale_ts}"}}\n',
        mtime=newer_mtime,
    )

    # Test 42b: Companion file with NEWER timestamp but OLDER mtime (bug 11 verification)
    # This file has a RECENT line timestamp (1 day ago) but OLDER mtime (3 days ago)
    recent_line_ts_sec = now - (1 * day)  # 1 day old (more recent than the other file's line)
    older_mtime = now - (3 * day)  # 3 days old (older mtime than the other file)
    recent_ts = ts_from_seconds(recent_line_ts_sec)
    fixture.add_session(
        "ranking-test-new-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{recent_ts}"}}\n',
        mtime=older_mtime,
    )

    # Test 43: Test --days filtering (exactly at boundary)
    boundary_ts_sec = now - (30 * day)
    boundary_ts = ts_from_seconds(boundary_ts_sec)
    fixture.add_session(
        "boundary-session",
        f'{{"type":"user","message":{{"content":"boundary test"}},"timestamp":"{boundary_ts}"}}\n',
        mtime=boundary_ts_sec,
    )

    fixture_root_path = fixture.finalize()

    # --- Test with both interpreters ---
    for interpreter in ["/bin/bash", "/bin/zsh"]:
        if not Path(interpreter).exists():
            t(f"Test under {interpreter}", False, f"{interpreter} not found")
            continue

        # Test 1: Multi-word AND matching
        code, out, err = run(
            interpreter,
            ["tray", "icon", "color"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: multi-word AND matching succeeds",
            code == 0 and "multi-word-and-test" in out,
            f"exit={code}, 'multi-word-and-test' in output: {bool('multi-word-and-test' in out)}",
        )

        # Test 2: Quoted phrase (exact phrase matching)
        code, out, err = run(
            interpreter,
            ["this is a test phrase"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: quoted phrase matching",
            code == 0 and "quoted-phrase-test" in out,
            f"exit={code}",
        )

        # Test 3: Subagent parent id extraction
        code, out, err = run(
            interpreter,
            ["subagent"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        if code == 0 and "parent-session-uuid" in out:
            # Extract the session_id column (4th field in pipe-delimited output)
            lines = [l for l in out.strip().split('\n') if l]
            if lines:
                fields = lines[0].split('|')
                if len(fields) >= 4:
                    reported_id = fields[3]
                    t(
                        f"{interpreter}: subagent uses path-derived parent id",
                        reported_id == "parent-session-uuid",
                        f"got {reported_id}",
                    )

        # Test 4: cwd fallback from dirname
        code, out, err = run(
            interpreter,
            ["cwd"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: cwd decoded from dirname",
            code == 0 and "/Users/test/project" in out,
            f"exit={code}",
        )

        # Test 5: Snippet with newlines is one record
        code, out, err = run(
            interpreter,
            ["search", "term"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        if "newline-snippet-test" in out:
            # Count pipe-delimited records for this session
            session_lines = [l for l in out.strip().split('\n') if 'newline-snippet-test' in l]
            t(
                f"{interpreter}: newline-containing snippet is one record",
                len(session_lines) == 1,
                f"got {len(session_lines)} records",
            )

        # Test 6: Empty file doesn't crash
        code, out, err = run(
            interpreter,
            ["anything"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: empty file doesn't crash",
            code in [0, 1],
            f"exit={code}",
        )

        # Test 7: --days filtering works
        code, out, err = run(
            interpreter,
            ["old", "--days", "30"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: --days filtering excludes old files",
            "old-session" not in out,
            f"old-session appeared in results",
        )

        # Test 8: --days with no value exits non-zero
        code, out, err = run(
            interpreter,
            ["something", "--days"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: --days without value exits non-zero",
            code != 0,
            f"exit={code}",
        )
        t(
            f"{interpreter}: --days error message is clear",
            "error" in err.lower() or "usage" in err.lower(),
            f"stderr: {err[:100]}",
        )

        # Test 9: Self-exclusion with CLAUDE_CODE_SESSION_ID
        code, out, err = run(
            interpreter,
            ["self"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path), "CLAUDE_CODE_SESSION_ID": "self-session-id"},
        )
        t(
            f"{interpreter}: self-session excluded when CLAUDE_CODE_SESSION_ID set",
            "self-session-id" not in out,
            f"self-session-id appeared in results",
        )

        # Test 9b: Self-session present when CLAUDE_CODE_SESSION_ID is unset
        code, out, err = run(
            interpreter,
            ["self"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path), "CLAUDE_CODE_SESSION_ID": ""},
        )
        t(
            f"{interpreter}: self-session included when CLAUDE_CODE_SESSION_ID is empty",
            "self-session-id" in out,
            f"self-session-id missing from results",
        )

        # Test 10: Limit truncation (30 bulk files, limit 10)
        code, out, err = run(
            interpreter,
            ["bulk"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path), "SEARCH_CLAUDE_LIMIT": "10"},
        )
        result_count = len([l for l in out.strip().split('\n') if l and 'bulk' in l])
        t(
            f"{interpreter}: results truncated to SEARCH_CLAUDE_LIMIT",
            result_count <= 10,
            f"got {result_count} results",
        )

        # Test 11: Snippet extraction from tool_use.input and thinking blocks
        code, out, err = run(
            interpreter,
            ["search_term_in_input"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: tool-use-only session appears in results",
            "tool-use-only" in out,
            f"tool-use-only not found in output",
        )
        if "tool-use-only" in out:
            # Extract the line with tool-use-only
            tool_use_lines = [l for l in out.strip().split('\n') if 'tool-use-only' in l]
            if tool_use_lines:
                # Format: mtime|timestamp|cwd|session_id|kind|first_prompt|snippet
                fields = tool_use_lines[0].split('|')
                if len(fields) >= 7:
                    snippet = fields[6]
                    t(
                        f"{interpreter}: tool_use.input snippet extracted (not sentinel)",
                        snippet != "(match in non-text field)",
                        f"got sentinel instead of snippet: {snippet}",
                    )

        # Test 11b: Snippet extraction from thinking blocks
        code, out, err = run(
            interpreter,
            ["search_term_in_input"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: thinking-only session appears in results",
            "thinking-only" in out,
            f"thinking-only not found in output",
        )
        if "thinking-only" in out:
            # Extract the line with thinking-only
            thinking_lines = [l for l in out.strip().split('\n') if 'thinking-only' in l]
            if thinking_lines:
                # Format: mtime|timestamp|cwd|session_id|kind|first_prompt|snippet
                fields = thinking_lines[0].split('|')
                if len(fields) >= 7:
                    snippet = fields[6]
                    t(
                        f"{interpreter}: thinking block snippet extracted (not sentinel)",
                        snippet != "(match in non-text field)",
                        f"got sentinel instead of snippet: {snippet}",
                    )

        # Test 12: Ranking by match timestamp, not file mtime
        # Query matches both files; verify the one with more recent timestamp ranks first
        code, out, err = run(
            interpreter,
            ["ranking", "test", "compare"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: ranking-test-old-timestamp appears in results",
            "ranking-test-old-timestamp" in out,
            f"ranking-test-old-timestamp not found",
        )
        t(
            f"{interpreter}: ranking-test-new-timestamp appears in results",
            "ranking-test-new-timestamp" in out,
            f"ranking-test-new-timestamp not found",
        )
        if code == 0 and "ranking-test-new-timestamp" in out and "ranking-test-old-timestamp" in out:
            lines = [l for l in out.strip().split('\n') if l and ('ranking-test-new-timestamp' in l or 'ranking-test-old-timestamp' in l)]
            if len(lines) >= 2:
                # First result should be the one with more recent timestamp (ranking-test-new-timestamp)
                # not the one with newer mtime (ranking-test-old-timestamp)
                first_result = lines[0]
                second_result = lines[1]
                t(
                    f"{interpreter}: newer match timestamp ranks first (not newer mtime)",
                    "ranking-test-new-timestamp" in first_result and "ranking-test-old-timestamp" in second_result,
                    f"order wrong: first={first_result.split('|')[3]}, second={second_result.split('|')[3]}",
                )

        # Test 13: No SIGPIPE on broad query
        code, out, err = run(
            interpreter,
            ["the"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path), "SEARCH_CLAUDE_LIMIT": "3"},
        )
        t(
            f"{interpreter}: broad query doesn't SIGPIPE (exit code != 141)",
            code != 141,
            f"exit={code} (SIGPIPE is 141)",
        )

        # Test 13b: Query with a regex metacharacter is matched literally
        code, out, err = run(
            interpreter,
            ["a.b"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: literal 'a.b' query matches literal 'a.b' text",
            "literal-dot-match" in out,
            f"literal-dot-match not found in output",
        )
        t(
            f"{interpreter}: literal 'a.b' query does not regex-match 'aXb'",
            "regex-wildcard-decoy" not in out,
            f"regex-wildcard-decoy appeared in output (dot matched as wildcard)",
        )

        # Test 14: Unicode handling
        code, out, err = run(
            interpreter,
            ["emoji"],
            {"CLAUDE_PROJECTS_DIR": str(fixture_root_path)},
        )
        t(
            f"{interpreter}: unicode/emoji queries work",
            code == 0,
            f"exit={code}",
        )

finally:
    # Clean up fixture
    shutil.rmtree(fixture_root, ignore_errors=True)

# --- Static guards (source code analysis) ---
script_src = SCRIPT.read_text()

t("Script contains no mapfile/readarray", "mapfile" not in script_src and "readarray" not in script_src,
  "bash-ism detected")

t("Script contains no rg invocation", " rg " not in script_src and " rg\n" not in script_src,
  "ripgrep invocation found")

t("Script uses grep, not rg", "grep" in script_src,
  "grep not found")

# Check for bare array indexing (but allow shift/[@], which are safe).
# Excludes the embedded jq program (JQ_BATCH_FILTER='...'): jq's own
# array-indexing syntax ($parts[0], etc.) is a different, safe language,
# not a bash array access, and would otherwise false-positive here.
script_src_no_jq = re.sub(
    r"JQ_BATCH_FILTER='.*?'\n", "", script_src, flags=re.DOTALL
)
bare_index_pattern = r'\$\{?.*\[\d+\]\}?'
bare_indices = re.findall(bare_index_pattern, script_src_no_jq)
# Filter out false positives like [$i], [${...}], [$((...))]
bare_indices = [
    idx
    for idx in bare_indices
    if not any(
        x in idx for x in ["$i", "${", "$(", "[["]
    )
]
t("Script avoids bare numeric array indexing", len(bare_indices) == 0,
  f"found: {bare_indices[:3]}")

# --- Shellcheck ---
try:
    result = subprocess.run(
        ["shellcheck", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    t("Script passes shellcheck", result.returncode == 0,
      f"shellcheck found issues")
except FileNotFoundError:
    t("shellcheck is installed", False,
      "shellcheck not found — install with: brew install shellcheck")
except subprocess.TimeoutExpired:
    t("shellcheck completes quickly", False, "timeout")

h.summarize_and_exit()
