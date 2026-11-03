"""
Corpus helper for the flow-ship / flow-merge Bash guard (#359, plan Step 9).

Extracts the shell command lines each restricted agent can actually run: every fenced bash
block and inline `git push` span in the agent's Skill closure (entry command plus the
commands and the prompts/worktree-reference.md blocks it pulls in). The guard is then run over
every extracted line, so a rule that would deny a sanctioned command is found by test rather
than by a failed post-merge cleanup.
"""

import importlib.util
import re

from _test_harness import REPO_ROOT

CLOSURE_DOCS = {
    "flow-ship": ["commands/shipit.md", "commands/stack-sync.md", "prompts/worktree-reference.md"],
    "flow-merge": ["commands/merge-and-cleanup.md", "commands/queued-merge.md",
                   "commands/cleanup.md", "commands/stack-sync.md",
                   "prompts/worktree-reference.md"],
}

FENCE_RE = re.compile(r"```(?:bash|sh|zsh)\n(.*?)\n```", re.DOTALL)
INLINE_PUSH_RE = re.compile(r"`(git (?:-C \S+ )?push[^`]*)`")


def load_guard():
    spec = importlib.util.spec_from_file_location(
        "flow_bash_guard", REPO_ROOT / "scripts" / "flow-bash-guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def logical_lines(block):
    """Yield (offset, line) for each runnable single line of a bash block.

    Skipped, deliberately: comments and blanks, heredoc bodies, backslash continuations
    (joined into one line), and lines that only open or close a compound statement
    (then/else/fi/do/done/esac/}) or are `echo`/`printf` text, which print a runbook rather
    than run it.
    """
    lines = block.split("\n")
    out, i = [], 0
    heredoc_end = None
    while i < len(lines):
        raw = lines[i]
        i += 1
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        line = raw.strip()
        while line.endswith("\\") and i < len(lines):
            line = line[:-1].rstrip() + " " + lines[i].strip()
            i += 1
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\s*$", line)
        if m:
            heredoc_end = m.group(1)
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"(then|else|fi|do|done|esac|\}|\)|;;)", line):
            continue
        if re.match(r"(echo|printf)\b", line):
            continue
        out.append(line)
    return out


def corpus(agent):
    """Return a list of (doc, line_number, command) for the agent's closure."""
    items = []
    for rel in CLOSURE_DOCS[agent]:
        text = (REPO_ROOT / rel).read_text()
        for m in FENCE_RE.finditer(text):
            base = text[:m.start()].count("\n") + 2
            for cmd in logical_lines(m.group(1)):
                items.append((rel, base, cmd))
        for m in INLINE_PUSH_RE.finditer(text):
            items.append((rel, text[:m.start()].count("\n") + 1, m.group(1)))
    return items
