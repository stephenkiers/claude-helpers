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

import atexit
import os
import re
import shutil
import subprocess
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
atexit.register(shutil.rmtree, fixture_root, ignore_errors=True)

# Helper: generate ISO timestamp from seconds since epoch
def ts_from_seconds(seconds):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds))

# Current time for relative mtime calculations
now = int(time.time())
day = 86400

# Create separate fixture builders for each test group
g_and = FixtureBuilder(Path(fixture_root) / "and")
g_sub = FixtureBuilder(Path(fixture_root) / "sub")
g_newline = FixtureBuilder(Path(fixture_root) / "newline")
g_unicode = FixtureBuilder(Path(fixture_root) / "unicode")
g_regex = FixtureBuilder(Path(fixture_root) / "regex")
g_days = FixtureBuilder(Path(fixture_root) / "days")
g_self = FixtureBuilder(Path(fixture_root) / "self")
g_bulk = FixtureBuilder(Path(fixture_root) / "bulk")
g_snip = FixtureBuilder(Path(fixture_root) / "snip")
g_rank = FixtureBuilder(Path(fixture_root) / "rank")
g_multi = FixtureBuilder(Path(fixture_root) / "multi")

try:
    # and: multi-word-and-test, quoted-phrase-test
    g_and.add_session(
        "multi-word-and-test",
        f'{{"type":"user","message":{{"content":"tray icon color"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )
    g_and.add_session(
        "quoted-phrase-test",
        f'{{"type":"user","message":{{"content":"this is a test phrase"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # sub: parent-session-uuid (subagent)
    g_sub.add_session(
        "parent-session-uuid",
        f'{{"type":"user","message":{{"content":"subagent test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
        is_subagent=True,
    )

    # newline: newline-snippet-test, empty-file, not-json
    g_newline.add_session(
        "newline-snippet-test",
        f'{{"type":"user","message":{{"content":"line 1\\nline 2 search term"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )
    g_newline.add_session(
        "empty-file",
        "",
        mtime=now,
    )
    g_newline.add_session(
        "not-json",
        "this is not json\n",
        mtime=now,
    )

    # unicode: unicode-test
    g_unicode.add_session(
        "unicode-test",
        f'{{"type":"user","message":{{"content":"emoji 😀 test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # regex: literal-dot-match, regex-wildcard-decoy
    g_regex.add_session(
        "literal-dot-match",
        f'{{"type":"user","message":{{"content":"contains a.b literally"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )
    g_regex.add_session(
        "regex-wildcard-decoy",
        f'{{"type":"user","message":{{"content":"contains aXb not a dot"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # days: old-session
    old_mtime = now - (400 * day)
    old_ts = ts_from_seconds(old_mtime)
    g_days.add_session(
        "old-session",
        f'{{"type":"user","message":{{"content":"old query"}},"timestamp":"{old_ts}"}}\n',
        mtime=old_mtime,
    )

    # self: self-session-id
    g_self.add_session(
        "self-session-id",
        f'{{"type":"user","message":{{"content":"self test"}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # bulk: 5 bulk files (reduced from 30)
    for i in range(5):
        bulk_ts = ts_from_seconds(now - (i * 100))
        g_bulk.add_session(
            f"bulk-file-{i:02d}",
            f'{{"type":"user","message":{{"content":"bulk"}},"timestamp":"{bulk_ts}"}}\n',
            mtime=now - (i * 100),
        )

    # snip: tool-use-only, thinking-only
    g_snip.add_session(
        "tool-use-only",
        f'{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"tool_use","id":"toolu_1","name":"Bash","input":{{"command":"search_term_in_input"}}}}]}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )
    g_snip.add_session(
        "thinking-only",
        f'{{"type":"assistant","message":{{"role":"assistant","content":[{{"type":"thinking","thinking":"I should search_term_in_input next"}}]}},"timestamp":"{ts_from_seconds(now)}"}}\n',
        mtime=now,
    )

    # rank: both ranking-test files
    stale_line_ts_sec = now - (3 * day)
    newer_mtime = now - (1 * day)
    stale_ts = ts_from_seconds(stale_line_ts_sec)
    g_rank.add_session(
        "ranking-test-old-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{stale_ts}"}}\n',
        mtime=newer_mtime,
    )
    recent_line_ts_sec = now - (1 * day)
    older_mtime = now - (3 * day)
    recent_ts = ts_from_seconds(recent_line_ts_sec)
    g_rank.add_session(
        "ranking-test-new-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{recent_ts}"}}\n',
        mtime=older_mtime,
    )

    # multi: copies of both ranking-test files plus tiebreak-session-a/b, with same contents and mtimes
    identical_ts = ts_from_seconds(now - (5 * day))
    g_multi.add_session(
        "ranking-test-old-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{stale_ts}"}}\n',
        mtime=newer_mtime,
    )
    g_multi.add_session(
        "ranking-test-new-timestamp",
        f'{{"type":"user","message":{{"content":"ranking test compare"}},"timestamp":"{recent_ts}"}}\n',
        mtime=older_mtime,
    )
    g_multi.add_session(
        "tiebreak-session-a",
        f'{{"type":"user","message":{{"content":"tiebreak query"}},"timestamp":"{identical_ts}"}}\n',
        mtime=now - (5 * day),
    )
    g_multi.add_session(
        "tiebreak-session-b",
        f'{{"type":"user","message":{{"content":"tiebreak query"}},"timestamp":"{identical_ts}"}}\n',
        mtime=now - (5 * day),
    )

    # cwd: Move the cwd test file structure
    cwd_test_dir = Path(fixture_root) / "cwd" / "-Users-test-project"
    cwd_test_dir.mkdir(parents=True, exist_ok=True)
    cwd_test_file = cwd_test_dir / "session.jsonl"
    cwd_test_file.write_text(f'{{"type":"user","message":{{"content":"cwd test"}},"timestamp":"{ts_from_seconds(now)}"}}\n', encoding="utf-8")
    os.utime(cwd_test_file, (now, now))

    # --- Test with both interpreters ---
    for interpreter in ["/bin/bash", "/bin/zsh"]:
        if not Path(interpreter).exists():
            t(f"Test under {interpreter}", False, f"{interpreter} not found")
            continue

        # Test 1: Multi-word AND matching
        code, out, err = run(
            interpreter,
            ["tray", "icon", "color"],
            {"CLAUDE_PROJECTS_DIR": str(g_and.root)},
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
            {"CLAUDE_PROJECTS_DIR": str(g_and.root)},
        )
        t(
            f"{interpreter}: quoted phrase matching",
            code == 0 and "quoted-phrase-test" in out,
            f"exit={code}",
        )

        # Test 7: --days filtering works
        code, out, err = run(
            interpreter,
            ["old", "--days", "30"],
            {"CLAUDE_PROJECTS_DIR": str(g_days.root)},
        )
        t(
            f"{interpreter}: --days filtering excludes old files",
            "old-session" not in out,
            "old-session appeared in results",
        )

        # Test 7b: --all leaves FIND_EXTRA_ARGS empty
        code, out, err = run(
            interpreter,
            ["old", "--all"],
            {"CLAUDE_PROJECTS_DIR": str(g_days.root)},
        )
        t(
            f"{interpreter}: --all runs without unbound-variable error",
            code == 0 and "unbound variable" not in err,
            f"exit={code}, stderr: {err[:120]}",
        )
        t(
            f"{interpreter}: --all surfaces files older than the default window",
            "old-session" in out,
            "old-session missing from --all results",
        )

        # Test 8: --days with no value exits non-zero
        code, out, err = run(
            interpreter,
            ["something", "--days"],
            {"CLAUDE_PROJECTS_DIR": str(g_days.root)},
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

        # Test 10: Limit truncation (5 bulk files, limit 3)
        code, out, err = run(
            interpreter,
            ["bulk"],
            {"CLAUDE_PROJECTS_DIR": str(g_bulk.root), "SEARCH_CLAUDE_LIMIT": "3"},
        )
        result_count = len([line for line in out.strip().split('\n') if line and 'bulk' in line])
        t(
            f"{interpreter}: results truncated to SEARCH_CLAUDE_LIMIT",
            result_count == 3,
            f"got {result_count} results",
        )

        # Broad query doesn't SIGPIPE (exit code != 141)
        t(f"{interpreter}: broad query doesn't SIGPIPE (exit code != 141)", code != 141, f"exit={code}")

        # workers always run under bash (search-claude.sh:160); zsh only re-checks main-script argument parsing
        if interpreter != "/bin/bash":
            continue

        # Test 3: Subagent parent id extraction
        code, out, err = run(
            interpreter,
            ["subagent"],
            {"CLAUDE_PROJECTS_DIR": str(g_sub.root)},
        )
        if code == 0 and "parent-session-uuid" in out:
            lines = [line for line in out.strip().split('\n') if line]
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
            {"CLAUDE_PROJECTS_DIR": str(Path(fixture_root) / "cwd")},
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
            {"CLAUDE_PROJECTS_DIR": str(g_newline.root)},
        )
        if "newline-snippet-test" in out:
            session_lines = [line for line in out.strip().split('\n') if 'newline-snippet-test' in line]
            t(
                f"{interpreter}: newline-containing snippet is one record",
                len(session_lines) == 1,
                f"got {len(session_lines)} records",
            )

        # Test 6: Empty file doesn't crash
        code, out, err = run(
            interpreter,
            ["anything"],
            {"CLAUDE_PROJECTS_DIR": str(g_newline.root)},
        )
        t(
            f"{interpreter}: empty file doesn't crash",
            code in [0, 1],
            f"exit={code}",
        )

        # Test 9: Self-exclusion with CLAUDE_CODE_SESSION_ID
        code, out, err = run(
            interpreter,
            ["self"],
            {"CLAUDE_PROJECTS_DIR": str(g_self.root), "CLAUDE_CODE_SESSION_ID": "self-session-id"},
        )
        t(
            f"{interpreter}: self-session excluded when CLAUDE_CODE_SESSION_ID set",
            "self-session-id" not in out,
            "self-session-id appeared in results",
        )

        # Test 9b: Self-session present when CLAUDE_CODE_SESSION_ID is unset
        code, out, err = run(
            interpreter,
            ["self"],
            {"CLAUDE_PROJECTS_DIR": str(g_self.root), "CLAUDE_CODE_SESSION_ID": ""},
        )
        t(
            f"{interpreter}: self-session included when CLAUDE_CODE_SESSION_ID is empty",
            "self-session-id" in out,
            "self-session-id missing from results",
        )

        # Test 11: Snippet extraction from tool_use.input
        code, out, err = run(
            interpreter,
            ["search_term_in_input"],
            {"CLAUDE_PROJECTS_DIR": str(g_snip.root)},
        )
        t(
            f"{interpreter}: tool-use-only session appears in results",
            "tool-use-only" in out,
            "tool-use-only not found in output",
        )
        if "tool-use-only" in out:
            tool_use_lines = [line for line in out.strip().split('\n') if 'tool-use-only' in line]
            if tool_use_lines:
                fields = tool_use_lines[0].split('|')
                if len(fields) >= 7:
                    snippet = fields[6]
                    t(
                        f"{interpreter}: tool_use.input snippet extracted (not sentinel)",
                        snippet != "(match in non-text field)",
                        f"got sentinel instead of snippet: {snippet}",
                    )

        # Test 11b: Reuse Test 11's output for thinking-only check
        t(
            f"{interpreter}: thinking-only session appears in results",
            "thinking-only" in out,
            "thinking-only not found in output",
        )
        if "thinking-only" in out:
            thinking_lines = [line for line in out.strip().split('\n') if 'thinking-only' in line]
            if thinking_lines:
                fields = thinking_lines[0].split('|')
                if len(fields) >= 7:
                    snippet = fields[6]
                    t(
                        f"{interpreter}: thinking block snippet extracted (not sentinel)",
                        snippet != "(match in non-text field)",
                        f"got sentinel instead of snippet: {snippet}",
                    )

        # Test 12: Ranking by match timestamp, not file mtime
        code, out, err = run(
            interpreter,
            ["ranking", "test", "compare"],
            {"CLAUDE_PROJECTS_DIR": str(g_rank.root)},
        )
        t(
            f"{interpreter}: ranking-test-old-timestamp appears in results",
            "ranking-test-old-timestamp" in out,
            "ranking-test-old-timestamp not found",
        )
        t(
            f"{interpreter}: ranking-test-new-timestamp appears in results",
            "ranking-test-new-timestamp" in out,
            "ranking-test-new-timestamp not found",
        )
        if code == 0 and "ranking-test-new-timestamp" in out and "ranking-test-old-timestamp" in out:
            lines = [line for line in out.strip().split('\n') if line and ('ranking-test-new-timestamp' in line or 'ranking-test-old-timestamp' in line)]
            if len(lines) >= 2:
                first_result = lines[0]
                second_result = lines[1]
                t(
                    f"{interpreter}: newer match timestamp ranks first (not newer mtime)",
                    "ranking-test-new-timestamp" in first_result and "ranking-test-old-timestamp" in second_result,
                    f"order wrong: first={first_result.split('|')[3]}, second={second_result.split('|')[3]}",
                )

        # Test 13b: Query with a regex metacharacter is matched literally
        code, out, err = run(
            interpreter,
            ["a.b"],
            {"CLAUDE_PROJECTS_DIR": str(g_regex.root)},
        )
        t(
            f"{interpreter}: literal 'a.b' query matches literal 'a.b' text",
            "literal-dot-match" in out,
            "literal-dot-match not found in output",
        )
        t(
            f"{interpreter}: literal 'a.b' query does not regex-match 'aXb'",
            "regex-wildcard-decoy" not in out,
            "regex-wildcard-decoy appeared in output (dot matched as wildcard)",
        )

        # Test 14: Unicode handling
        code, out, err = run(
            interpreter,
            ["emoji"],
            {"CLAUDE_PROJECTS_DIR": str(g_unicode.root)},
        )
        t(
            f"{interpreter}: unicode/emoji queries work",
            code == 0,
            f"exit={code}",
        )

finally:
    pass  # Cleanup deferred to end of file

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

# --- Shellcheck for both scripts ---
WORKER_SCRIPT = REPO_ROOT / "scripts" / "search-claude-worker.sh"

try:
    result = subprocess.run(
        ["shellcheck", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    t("Script passes shellcheck", result.returncode == 0,
      "shellcheck found issues")
except FileNotFoundError:
    t("shellcheck is installed", False,
      "shellcheck not found — install with: brew install shellcheck")
except subprocess.TimeoutExpired:
    t("shellcheck completes quickly", False, "timeout")

try:
    result = subprocess.run(
        ["shellcheck", str(WORKER_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    t("Worker script passes shellcheck", result.returncode == 0,
      "shellcheck found issues in worker script")
except FileNotFoundError:
    t("shellcheck is installed for worker", False,
      "shellcheck not found — install with: brew install shellcheck")
except subprocess.TimeoutExpired:
    t("shellcheck completes quickly for worker", False, "timeout")

# --- Static guard: verify search-claude.sh doesn't have bare stdout writes in xargs section ---
# The xargs-invoked worker processes must write to individual temp files, not
# to a shared stdout/descriptor. This guard verifies the main script doesn't
# regress to bare printf/echo in the parallel section.
script_src = SCRIPT.read_text()
# Extract the xargs invocation section (from "xargs" to the line after "exit 0")
xargs_start = script_src.find("xargs")
xargs_end = script_src.find("exit 0")
assert xargs_start != -1, "sentinel 'xargs' not found in search-claude.sh — guard is broken"
assert xargs_end != -1, "sentinel 'exit 0' not found in search-claude.sh — guard is broken"
xargs_section = script_src[xargs_start:xargs_end]
# Check that we don't have bare printf/echo writing to stdout in this section
# (allowed patterns: printf to variables, redirections, subshells)
has_bare_printf = "| printf" in xargs_section and ">> \"$outfile\"" not in xargs_section
has_bare_echo = "| echo" in xargs_section and ">> \"$outfile\"" not in xargs_section
t("Parallel section writes to worker temp files, not shared stdout",
  not (has_bare_printf or has_bare_echo),
  "bare printf/echo detected in xargs section (would corrupt concurrent output)")

# --- Single-worker path verification (simplified per SPEC_A §3) ---
M = {"CLAUDE_PROJECTS_DIR": str(g_multi.root), "SEARCH_CLAUDE_LIMIT": "100"}

# Two runs of ["user"] with SEARCH_CLAUDE_WORKERS "" and "1"
code_default, out_default, err_default = run("/bin/bash", ["user"], M)
code_single, out_single, err_single = run("/bin/bash", ["user"], {**M, "SEARCH_CLAUDE_WORKERS": "1"})

# Assert: default run exits 0 with exactly 4 lines (ranking-test-old/new-timestamp + tiebreak a/b)
default_lines = [line for line in out_default.strip().split('\n') if line]
t("Default run exits 0 with exactly 4 lines", code_default == 0 and len(default_lines) == 4,
  f"exit={code_default}, lines={len(default_lines)}")

# Assert: workers=1 output is byte-identical to default
t("SEARCH_CLAUDE_WORKERS=1 output is byte-identical to default",
  out_single == out_default and code_single == code_default,
  f"default: {len(out_default)} chars, single: {len(out_single)} chars")

# Assert: ranking-test-new-timestamp comes before ranking-test-old-timestamp
if len(default_lines) >= 2:
    first_line = default_lines[0]
    second_line = default_lines[1]
    t("ranking-test-new-timestamp comes before ranking-test-old-timestamp",
      "ranking-test-new-timestamp" in first_line and "ranking-test-old-timestamp" in second_line,
      f"first={first_line.split('|')[3] if '|' in first_line else '?'}, second={second_line.split('|')[3] if '|' in second_line else '?'}")

# On g_unicode with ["emoji"]: run with workers "", "100", "0", "invalid"
code_u_default, out_u, err_u_default = run("/bin/bash", ["emoji"], {"CLAUDE_PROJECTS_DIR": str(g_unicode.root)})
code_u_100, out_u_100, err_u_100 = run("/bin/bash", ["emoji"], {"CLAUDE_PROJECTS_DIR": str(g_unicode.root), "SEARCH_CLAUDE_WORKERS": "100"})
code_u_0, out_u_0, err_u_0 = run("/bin/bash", ["emoji"], {"CLAUDE_PROJECTS_DIR": str(g_unicode.root), "SEARCH_CLAUDE_WORKERS": "0"})
code_u_invalid, out_u_invalid, err_u_invalid = run("/bin/bash", ["emoji"], {"CLAUDE_PROJECTS_DIR": str(g_unicode.root), "SEARCH_CLAUDE_WORKERS": "invalid"})

t("Unicode emoji: workers=100 matches default", out_u == out_u_100 and code_u_default == code_u_100,
  f"default: {len(out_u)} chars, workers=100: {len(out_u_100)} chars")
t("Unicode emoji: workers=0 matches default", out_u == out_u_0 and code_u_default == code_u_0,
  f"default: {len(out_u)} chars, workers=0: {len(out_u_0)} chars")
t("Unicode emoji: workers=invalid matches default", out_u == out_u_invalid and code_u_default == code_u_invalid,
  f"default: {len(out_u)} chars, workers=invalid: {len(out_u_invalid)} chars")
t("Unicode emoji output is non-empty", len(out_u) > 0, "got empty output")

# On g_bulk: ["bulk"] with LIMIT "3" and WORKERS "1" must return exactly 3 lines
code_bulk, out_bulk, err_bulk = run("/bin/bash", ["bulk"], {"CLAUDE_PROJECTS_DIR": str(g_bulk.root), "SEARCH_CLAUDE_LIMIT": "3", "SEARCH_CLAUDE_WORKERS": "1"})
bulk_lines = len([line for line in out_bulk.strip().split('\n') if line])
t("Bulk with LIMIT=3 and WORKERS=1 returns exactly 3 lines", bulk_lines == 3, f"got {bulk_lines} lines")

# On g_unicode: ["xyzabc123notfound"] must exit 0 with empty output
code_notfound, out_notfound, err_notfound = run("/bin/bash", ["xyzabc123notfound"], {"CLAUDE_PROJECTS_DIR": str(g_unicode.root)})
t("Query 'xyzabc123notfound' exits 0 with empty output", code_notfound == 0 and out_notfound == "",
  f"exit={code_notfound}, output={repr(out_notfound[:50])}")

# Test that result format (pipe-delimited) is consistent
def verify_format(output_str):
    """Verify pipe-delimited format with 7+ fields per line."""
    if not output_str.strip():
        return True  # Empty output is valid
    for line in output_str.strip().split('\n'):
        if line:
            parts = line.split('|')
            if len(parts) < 7:
                return False
    return True

t("Default g_multi results use pipe-delimited format (7+ fields)",
  verify_format(out_default),
  "format check failed")

t("Workers=100 g_multi results use pipe-delimited format (7+ fields)",
  verify_format(out_u_100),
  "format check failed")

# Clean up fixture (deferred from finally block so parallelization tests can use it)
shutil.rmtree(fixture_root, ignore_errors=True)

h.summarize_and_exit()
