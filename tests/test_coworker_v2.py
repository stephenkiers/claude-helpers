#!/usr/bin/env python3
"""
Spec-blind test suite for issue #74: close the v1→v2 gap on /expert-review-coworker.

Written ONLY from the plan: (1) auto complexity gate parsing diff-index.md with named
tunables and a printed verdict, (2) two expert-scout agents per lens with an
agreement-promotion rule in the merge, (3) beta renamed over /expert-review-coworker.
The author has not read the implementation. Every assertion reads the real
command/prompt/doc files at runtime, and the gate's bash fences are executed against
synthetic diff-index.md fixtures written in the exact format
scripts/setup-pr-worktree.sh produces (Step F: a '## Files' section holding
`git diff --stat` output — including the singular "1 file changed" and
insertions-only/deletions-only summary forms — then a '## Hunks' section).

Run with: python3 tests/test_coworker_v2.py
"""

import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

COMMANDS = REPO_ROOT / "commands"
PROMPTS = REPO_ROOT / "prompts"

CMD_PATH = COMMANDS / "expert-review-coworker.md"
BETA_PATH = COMMANDS / "expert-review-coworker-beta.md"
SCOUT_PATH = PROMPTS / "peer-scout.md"
MERGE_PATH = PROMPTS / "peer-merge.md"
PANEL_PATH = PROMPTS / "expert-review-panel.md"
CLAUDE_PATH = REPO_ROOT / "CLAUDE.md"
README_PATH = REPO_ROOT / "README.md"


def read(path):
    """Return a file's text, or '' if it is missing — so a moved/renamed file turns into a
    failing assertion, never a suite-crashing exception."""
    try:
        return path.read_text()
    except OSError:
        return ""


def extract_bash_blocks(markdown_text):
    """Extract all ```bash fenced block contents, in document order."""
    return [m.group(1) for m in re.finditer(r"```bash\n(.*?)\n```", markdown_text, re.DOTALL)]


def step_span(text, step_num):
    """Return the 'Step <n>' section (heading of any depth, through the next Step
    heading), or '' if that step heading is absent."""
    m = re.search(rf"^#{{2,5}}\s*Step\s+{step_num}\b.*$", text, re.MULTILINE)
    if not m:
        return ""
    nxt = re.search(r"^#{2,5}\s*Step\s+\d+\b", text[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.start():end]


def step_pos(text, keyword):
    """Position of the heading or numbered-step line naming keyword, else -1."""
    for pat in (rf"^#+\s*[^\n]*{keyword}",
                rf"^\s*\d+[\.)]\s*[^\n]*{keyword}"):
        m = re.search(pat, text, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.start()
    return -1


CMD = read(CMD_PATH)
SCOUT = read(SCOUT_PATH)
MERGE = read(MERGE_PATH)
CLAUDE = read(CLAUDE_PATH)
README = read(README_PATH)

h = Harness("COWORKER V2 SPEC-BLIND TEST SUITE (ISSUE #74)")
t = h.test_result

# ============================================================================
# INVARIANT 1: Beta renamed over /expert-review-coworker; frontmatter rewritten
# ============================================================================
print("[Invariant 1] Rename: expert-review-coworker.md exists, beta is gone, frontmatter updated")

t("commands/expert-review-coworker.md exists",
  CMD_PATH.exists(),
  "git mv target missing — the beta command must replace the old full-panel command")

t("commands/expert-review-coworker-beta.md is gone",
  not BETA_PATH.exists(),
  "the beta file must not survive the rename (git mv, not copy)")

fm = re.match(r"^---\n(.*?)\n---\n", CMD, re.DOTALL)
frontmatter = fm.group(1) if fm else ""
hint_line = next((l for l in frontmatter.splitlines()
                  if l.strip().startswith("argument-hint:")), "")
desc_line = next((l for l in frontmatter.splitlines()
                  if l.strip().startswith("description:")), "")

t("renamed command has a frontmatter block",
  bool(frontmatter),
  "no --- frontmatter block found")

t("argument-hint includes --swarm",
  "--swarm" in hint_line,
  f"plan: argument-hint gains --swarm; found: {hint_line!r}")

t("argument-hint still includes --deep",
  "--deep" in hint_line,
  f"the manual deep override survives; found: {hint_line!r}")

t("frontmatter description carries no 'beta' framing",
  "beta" not in desc_line.lower(),
  f"description must be rewritten for the final design; found: {desc_line!r}")

# ============================================================================
# INVARIANT 2: No stale beta/v1 framing; the command drives the scout pipeline
# ============================================================================
print()
print("[Invariant 2] No stale -beta/v1 framing; existing flags and pipeline wiring")

t("no 'expert-review-coworker-beta' self-reference in the renamed command",
  "expert-review-coworker-beta" not in CMD)

t("no 'beta' framing remains in the renamed command",
  re.search(r"\bbeta\b", CMD, re.IGNORECASE) is None,
  "plan testing strategy: grep the renamed command for 'beta'")

t("no 'v1' framing remains (e.g. 'No auto-gate in v1' statements removed)",
  re.search(r"\bv1\b", CMD) is None,
  "plan testing strategy: grep the renamed command for 'v1'")

t("--deep manual override is retained in the body",
  "--deep" in CMD)

t("--include-medium is retained (regression guard)",
  "--include-medium" in CMD)

t("command drives the scout pipeline (references peer-scout.md)",
  "peer-scout.md" in CMD)

t("command drives the merge step (references peer-merge.md)",
  "peer-merge.md" in CMD)

t("command does NOT delegate to the old full panel (expert-review-panel.md)",
  "expert-review-panel.md" not in CMD,
  "old panel pipeline stays in git history; panel prompt is for /expert-review author mode")

# ============================================================================
# INVARIANT 3: Auto complexity gate — Step 2 with named tunables, verdict, markers
# ============================================================================
print()
print("[Invariant 3] Gate: real Step 2, DIFF_LINE_MAX=600 / FILE_MAX=5, verdict print, markers")

step0 = step_span(CMD, 0)
step2 = step_span(CMD, 2)

t("command has a Step 0 section",
  bool(step0),
  "no 'Step 0' heading found")

t("--swarm flag parsing lands in Step 0 alongside existing --deep",
  "--swarm" in step0 and "--deep" in step0,
  "plan: add --swarm flag parsing in Step 0 alongside existing --deep")

t("command has a real Step 2 (the auto complexity gate)",
  bool(step2),
  "no 'Step 2' heading found — plan adds a real Step 2 to the renamed command")

t("DIFF_LINE_MAX exists as a named constant in the gate step",
  "DIFF_LINE_MAX" in step2,
  "plan: tunables (DIFF_LINE_MAX, FILE_MAX) as named constants at the top of the step")

t("FILE_MAX exists as a named constant in the gate step",
  "FILE_MAX" in step2)

t("conservative line threshold DIFF_LINE_MAX=600",
  re.search(r"DIFF_LINE_MAX[^\n]{0,20}600", step2) is not None,
  "plan: DIFF_LINES <= 600 stays on the swarm path")

t("conservative file threshold FILE_MAX=5",
  re.search(r"FILE_MAX[^\n]{0,20}\b5\b", step2) is not None,
  "plan: FILES_CHANGED <= 5 stays on the swarm path")

t("gate parses diff-index.md",
  "diff-index.md" in step2,
  "plan: parse diff-index.md for DIFF_LINES, FILES_CHANGED, and architectural markers")

t("gate verdict print format documented: 'gate: <N> lines / <M> files → <route>'",
  re.search(r"gate:\s*[^\n]*lines\s*/\s*[^\n]*files\s*→", CMD) is not None,
  "plan: print the gate's verdict and numbers, e.g. 'gate: 312 lines / 2 files → swarm'")

t("gate names both routes (swarm and deep)",
  re.search(r"swarm", step2, re.IGNORECASE) is not None
  and re.search(r"deep", step2, re.IGNORECASE) is not None,
  "plan: route to DEEP when over threshold; SWARM otherwise")

t("gate checks PR_TITLE for refactor/architect markers",
  "PR_TITLE" in step2
  and re.search(r"refactor", step2, re.IGNORECASE) is not None
  and re.search(r"architect", step2, re.IGNORECASE) is not None,
  "plan: architectural markers include 'refactor'/'architect' in PR_TITLE")

t("gate checks schema/migration files and new top-level modules",
  re.search(r"schema|migration", step2, re.IGNORECASE) is not None
  and re.search(r"top[- ]level", step2, re.IGNORECASE) is not None,
  "plan: architectural markers include a new top-level module and schema/migration files")

# ============================================================================
# INVARIANT 4: Gate bash — fences parse; parsing matches setup-pr-worktree.sh's format
# ============================================================================
print()
print("[Invariant 4] Gate bash: bash -n every fence; functional parse of real stat-line formats")

blocks = extract_bash_blocks(CMD)

t("renamed command contains bash fences",
  len(blocks) > 0,
  "the gate's parsing must live in bash the command can run")

bash_n_failures = []
for i, block in enumerate(blocks):
    res = subprocess.run(["bash", "-n"], input=block,
                         capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        bash_n_failures.append(f"fence #{i + 1}: {res.stderr.strip()[:200]}")

t("every bash fence in the renamed command parses under bash -n",
  not bash_n_failures,
  "; ".join(bash_n_failures))

# The gate's parsing is what Step F of scripts/setup-pr-worktree.sh writes: a '## Files'
# section holding `git diff --stat` output, then '## Hunks'. Execute every gate-related
# fence (in doc order) against synthetic diff-index.md files in that exact format.
gate_blocks = [b for b in blocks
               if any(tok in b for tok in
                      ("diff-index.md", "DIFF_LINE_MAX", "FILE_MAX",
                       "DIFF_LINES", "FILES_CHANGED"))]

t("command has a bash block that reads diff-index.md",
  any("diff-index.md" in b for b in gate_blocks),
  "plan: the gate parses diff-index.md — no bash fence references it")

FIXTURE_PLURAL = (
    "## Files\n"
    " src/foo.py | 10 +++++++---\n"
    " src/bar.py | 5 +++--\n"
    " 2 files changed, 9 insertions(+), 6 deletions(-)\n"
    "\n"
    "## Hunks\n"
    "+++ b/src/foo.py\n"
    "@@ -1,3 +1,6 @@\n"
    "+++ b/src/bar.py\n"
    "@@ -10,2 +10,3 @@\n"
)

FIXTURE_SINGULAR_DELETIONS_ONLY = (
    "## Files\n"
    " src/gone.py | 3 ---\n"
    " 1 file changed, 3 deletions(-)\n"
    "\n"
    "## Hunks\n"
    "+++ b/src/gone.py\n"
    "@@ -1,3 +0,0 @@\n"
)

FIXTURE_INSERTIONS_ONLY = (
    "## Files\n"
    " src/a.py | 300 ++++\n"
    " src/b.py | 250 +++\n"
    " src/c.py | 150 ++\n"
    " 3 files changed, 700 insertions(+)\n"
    "\n"
    "## Hunks\n"
    "+++ b/src/a.py\n"
    "@@ -0,0 +1,300 @@\n"
)

FIXTURE_SINGULAR_INFLECTIONS = (
    "## Files\n"
    " README.md | 2 +-\n"
    " 1 file changed, 1 insertion(+), 1 deletion(-)\n"
    "\n"
    "## Hunks\n"
    "+++ b/README.md\n"
    "@@ -1,1 +1,1 @@\n"
)


def run_gate(fixture_text):
    """Run the gate's real bash fences against a synthetic diff-index.md and return the
    completed process. REVIEW_DIR and PR_TITLE are set the way setup-pr-worktree.sh's
    emitted variables would set them; the fixture lands at $REVIEW_DIR/diff-index.md
    (and the cwd, for relative reads)."""
    with tempfile.TemporaryDirectory() as td:
        Path(td, "diff-index.md").write_text(fixture_text)
        script = (
            f"cd {shlex.quote(td)}\n"
            f"REVIEW_DIR={shlex.quote(td)}\n"
            "PR_TITLE='Add a small feature'\n"
            + "\n".join(gate_blocks)
            + '\necho "HARNESS_DIFF_LINES=${DIFF_LINES:-UNSET}"'
            + '\necho "HARNESS_FILES_CHANGED=${FILES_CHANGED:-UNSET}"\n'
        )
        return subprocess.run(["bash"], input=script,
                              capture_output=True, text=True, timeout=15)


def harness_value(stdout, name):
    m = re.search(rf"^{name}=(\S+)$", stdout, re.MULTILINE)
    return m.group(1) if m else None


res = run_gate(FIXTURE_PLURAL)
fc = harness_value(res.stdout, "HARNESS_FILES_CHANGED")
dl = harness_value(res.stdout, "HARNESS_DIFF_LINES")
t("gate parses plural stat block: FILES_CHANGED=2",
  fc == "2",
  f"got {fc!r}; stderr: {res.stderr.strip()[:200]}")
t("gate parses plural stat block: DIFF_LINES=15 (9 insertions + 6 deletions)",
  dl == "15",
  f"got {dl!r}; stderr: {res.stderr.strip()[:200]}")

res = run_gate(FIXTURE_SINGULAR_DELETIONS_ONLY)
fc = harness_value(res.stdout, "HARNESS_FILES_CHANGED")
dl = harness_value(res.stdout, "HARNESS_DIFF_LINES")
t("gate handles singular '1 file changed' summary: FILES_CHANGED=1",
  fc == "1",
  f"got {fc!r}; stderr: {res.stderr.strip()[:200]}")
t("gate handles deletions-only summary (no insertions group): DIFF_LINES=3",
  dl == "3",
  f"got {dl!r}; stderr: {res.stderr.strip()[:200]}")

res = run_gate(FIXTURE_INSERTIONS_ONLY)
fc = harness_value(res.stdout, "HARNESS_FILES_CHANGED")
dl = harness_value(res.stdout, "HARNESS_DIFF_LINES")
t("gate handles insertions-only summary (no deletions group): DIFF_LINES=700",
  dl == "700",
  f"got {dl!r}; stderr: {res.stderr.strip()[:200]}")
t("gate handles insertions-only summary: FILES_CHANGED=3",
  fc == "3",
  f"got {fc!r}; stderr: {res.stderr.strip()[:200]}")

res = run_gate(FIXTURE_SINGULAR_INFLECTIONS)
fc = harness_value(res.stdout, "HARNESS_FILES_CHANGED")
dl = harness_value(res.stdout, "HARNESS_DIFF_LINES")
t("gate handles singular '1 insertion(+), 1 deletion(-)' inflections: FILES_CHANGED=1",
  fc == "1",
  f"got {fc!r}; stderr: {res.stderr.strip()[:200]}")
t("gate handles singular inflections: DIFF_LINES=2",
  dl == "2",
  f"got {dl!r}; stderr: {res.stderr.strip()[:200]}")

# ============================================================================
# INVARIANT 5: Wave 1 spawns two expert-scout agents per lens (12 for --all)
# ============================================================================
print()
print("[Invariant 5] Two scouts per lens, index-differentiated, 12 total for --all")

t("command retains the Wave 1 wave structure",
  "Wave 1" in CMD)

t("Wave 1 uses expert-scout agents",
  "expert-scout" in CMD)

t("Wave 1 spawns 2 scouts per selected lens",
  re.search(r"\b2\b[^\n]{0,60}per\s+(?:selected\s+)?lens"
            r"|per\s+(?:selected\s+)?lens[^\n]{0,60}\b2\b", CMD, re.IGNORECASE) is not None,
  "plan: Wave 1 spawns 2 expert-scout (haiku) agents per selected lens")

t("--all fans out to 12 scouts total",
  re.search(r"(?:--all|scouts?)[^\n]{0,80}\b12\b"
            r"|\b12\b[^\n]{0,80}(?:--all|scouts?)", CMD, re.IGNORECASE) is not None,
  "plan: 12 total for --all")

t("scouts are differentiated by index (scout A / scout B)",
  re.search(r"scout\s*A\b", CMD, re.IGNORECASE) is not None
  and re.search(r"scout\s*B\b", CMD, re.IGNORECASE) is not None,
  "plan: differentiated only by an index in the prompt ('scout A/B — work independently')")

t("--all lens selection is retained (regression guard)",
  "--all" in CMD)

# ============================================================================
# INVARIANT 6: peer-scout.md prefers recall within the confidence bar
# ============================================================================
print()
print("[Invariant 6] peer-scout.md: recall-within-the-confidence-bar line")

t("prompts/peer-scout.md exists", SCOUT_PATH.exists())

recall_lines = [l for l in SCOUT.splitlines() if "recall" in l.lower()]
t("peer-scout.md instructs scouts to prefer recall",
  len(recall_lines) > 0,
  "plan: peer-scout.md gains one line — prefer recall within the confidence bar")

t("the recall instruction ties to the confidence bar / precision trade-off",
  any("confidence" in l.lower() or "precision" in l.lower() for l in recall_lines),
  f"plan: 'prefer recall within the confidence bar, since a second scout and the merge "
  f"provide precision'; recall lines found: {recall_lines}")

# ============================================================================
# INVARIANT 7: peer-merge.md agreement promotion between Dedup and Verify
# ============================================================================
print()
print("[Invariant 7] peer-merge.md: same-lens 2+-scout findings promoted; anchor check stays")

t("prompts/peer-merge.md exists", MERGE_PATH.exists())

merge_low = MERGE.lower()
dedup_pos = step_pos(MERGE, "dedup")
verify_pos = step_pos(MERGE, "verif")
promo_idx = merge_low.find("promot")

t("merge prompt retains a Dedup step", dedup_pos != -1)
t("merge prompt retains a Verify step", verify_pos != -1)

t("merge prompt carries an agreement-promotion rule",
  promo_idx != -1,
  "plan: same-spot findings from 2+ scouts of the same lens are promoted")

t("promotion rule is positioned between Dedup and Verify",
  dedup_pos != -1 and verify_pos != -1 and dedup_pos < promo_idx < verify_pos,
  "plan: the rule lands between Dedup and Verify")

# The rule and its parenthetical ('treated as high-confidence; anchor verification still
# required') are one unit — window the text after the first promotion mention rather than
# slicing to the Verify step, whose heading position is a separate assertion above.
window = merge_low[promo_idx:promo_idx + 800] if promo_idx != -1 else ""

t("promotion rule keys on same-spot findings",
  re.search(r"same[- ](spot|location|line|file)|identical", window) is not None,
  "plan: 'same-spot findings'")

t("promotion rule requires 2+ agreeing scouts",
  re.search(r"2\+|two|both|more than one", window) is not None,
  "plan: '2+ scouts'")

t("promotion rule keys on same-lens agreement",
  re.search(r"same[- ]lens", window) is not None,
  "plan: 'scouts of the same lens'")

t("promoted findings are treated as high-confidence",
  "high-confidence" in window or "high confidence" in window,
  "plan: 'treated as high-confidence'")

t("anchor verification remains mandatory for promoted findings",
  "anchor" in window and "verif" in window,
  "plan: 'anchor verification still required'")

# ============================================================================
# INVARIANT 8: Docs updated; old pipeline survives only in git history
# ============================================================================
print()
print("[Invariant 8] CLAUDE.md/README.md updated; panel prompt retained for author mode")

t("CLAUDE.md has no expert-review-coworker-beta reference",
  "expert-review-coworker-beta" not in CLAUDE)

t("README.md has no expert-review-coworker-beta reference",
  "expert-review-coworker-beta" not in README)

# Command-table entries wrap: the bullet's first line is followed by indented
# continuation lines. Extract the whole entry (bullet + continuations), stopping at the
# next bullet, a heading, or a blank line.
claude_lines = CLAUDE.splitlines()
entry_start = next((i for i, l in enumerate(claude_lines)
                    if "`/expert-review-coworker`" in l and "beta" not in l.lower()), -1)
t("CLAUDE.md documents /expert-review-coworker",
  entry_start != -1,
  "plan: update the CLAUDE.md command table")

entry_parts = []
if entry_start != -1:
    for l in claude_lines[entry_start:]:
        if entry_parts and (not l.strip() or l.lstrip().startswith(("- `", "#"))):
            break
        entry_parts.append(l)
entry = " ".join(p.strip() for p in entry_parts)
t("CLAUDE.md entry describes the swarm/scout design (not the old panel pipeline)",
  re.search(r"scout|swarm|gate", entry, re.IGNORECASE) is not None,
  f"entry: {entry[:240]}")

t("CLAUDE.md entry no longer routes coworker through expert-review-panel",
  "expert-review-panel" not in entry,
  f"the panel pipeline belongs to /expert-review author mode; entry: {entry[:240]}")

t("prompts/expert-review-panel.md is retained for /expert-review author mode",
  PANEL_PATH.exists(),
  "plan: the panel prompt stays; only the coworker command stops using it")

h.summarize_and_exit()
