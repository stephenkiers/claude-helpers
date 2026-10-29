#!/usr/bin/env python3
"""anti-cheat-scan.py: mechanical anti-cheat scan over a git diff range.

Usage:
    anti-cheat-scan.py --repo PATH --base SHA --head SHA
                       --profile {implement,heal,fix-now} [--revert-base SHA]

The script runs `git diff` itself (read-only) over BASE..HEAD in REPO and prints one
JSON object on stdout:

    {"verdict": "pass" | "fail", "reasons": ["<named reason>: <detail>", ...]}

Exit status: 0 on pass, 1 on fail. Every failure to read the diff, resolve a ref, or
validate arguments fails closed (verdict "fail", exit 1). Argparse usage errors (a
missing required flag, an unknown profile) exit 2 as usual.

Profiles:
  implement  Port of /implement-with-haiku Gate step 2: test-path changes (test-edit,
             test-removed), neutered verification in build/config files
             (neutered-verification), stub/placeholder markers on added lines
             (stub-body). "Wired but never called" stays judgment prose in the doc.
  fix-now    The implement profile minus the test-edit rule (test-removed still
             applies), and nothing more. It is no stricter than the fix step it re-runs.
  heal       The check-heal profile. Everything in implement, plus gate-config-edit,
             added-suppression, env-guard and revert-to-pass. Requires --revert-base,
             the start of the work being healed (bases[implement]). revert-to-pass
             fails a hunk only if (a) it removes a line that was added in
             REVERT_BASE..BASE and that line is neither an import line nor blank, or
             (b) its post-image restores the pre-REVERT_BASE text of a region that
             changed in REVERT_BASE..BASE. Deleting an unused import added by the
             work being healed therefore passes.

Named reasons: test-edit, test-removed, gate-config-edit, added-suppression, env-guard,
revert-to-pass, neutered-verification, stub-body, bad-ref, missing-revert-base,
revert-base-not-allowed, diff-unreadable.
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

PROFILES = ("implement", "heal", "fix-now")

# --- path classification ---------------------------------------------------

_TEST_BASENAME_GLOBS = ("*.test.*", "*.spec.*", "*_test.*")
_TEST_PREFIX_EXTS = ("ts", "js", "py", "rs", "swift")
_TEST_DIRS = ("tests", "__tests__", "spec")

_CONFIG_BASENAMES = ("package.json", "Makefile", "Cargo.toml", "pyproject.toml", "build.gradle")
_CONFIG_PREFIXES = (".github/workflows/",)

_GATE_BASENAMES = ("ruff.toml", "mypy.ini", "justfile", "Makefile", "pyproject.toml", "package.json")
_GATE_PREFIXES = (
    ".claude/",
    ".github/",
    ".githooks/",
    ".husky/",
    ".git/hooks/",
    "scripts/flow-",
)
_GATE_EXACT = ("scripts/anti-cheat-scan.py", "scripts/heal-check.py")

_CODE_EXTS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".swift", ".go",
    ".java", ".kt", ".rb", ".sh", ".c", ".cc", ".cpp", ".h", ".m",
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_test_path(path: str) -> bool:
    base = _basename(path)
    for glob in _TEST_BASENAME_GLOBS:
        if fnmatch.fnmatchcase(base, glob):
            return True
    for ext in _TEST_PREFIX_EXTS:
        if fnmatch.fnmatchcase(base, "test_*." + ext):
            return True
    parts = path.split("/")[:-1]
    return any(p in _TEST_DIRS for p in parts)


def is_config_path(path: str) -> bool:
    if _basename(path) in _CONFIG_BASENAMES:
        return True
    return any(path.startswith(p) for p in _CONFIG_PREFIXES)


def is_gate_path(path: str) -> bool:
    if _basename(path) in _GATE_BASENAMES:
        return True
    if path in _GATE_EXACT:
        return True
    return any(path.startswith(p) for p in _GATE_PREFIXES)


def is_impl_path(path: str) -> bool:
    return path.endswith(_CODE_EXTS) and not is_test_path(path)


# --- patterns (marker words are split so this file does not trip its own scan) ---

_STUB_RE = re.compile(
    r"\bTO" r"DO\b|\bFIX" r"ME\b|unimplemented!\(\)|NotImplemented" r"Error|"
    r"raise\s+NotImplemented\b|throw\s+new\s+NotImplemented"
)
_STUB_SAME_LINE_RE = re.compile(
    r"^\s*def\s.*:\s*(pass|\.\.\.|return)\s*$|"
    r"\bfn\s+\w+[^{]*\{\s*\}\s*$|"
    r"\bfunction\s+\w*\s*\([^)]*\)\s*\{\s*\}\s*$"
)
_PY_DEF_RE = re.compile(r"^(\s*)def\s.*:\s*$")

_NEUTER_RE = re.compile(
    r"\|\|\s*true\b|--no-verify|\bit\.skip\b|\bxit\(|\bxfail\b|pytest\.mark\.skip|#\[ignore\]|"
    r"^\s*#\s*-\s*(run|name|uses)\b|"
    r"\"test\"\s*:\s*\"\s*(echo\b|:\s*\")|"
    r"\brun:\s*(echo\b|:\s*$)"
)

_SUPPRESS_RE = re.compile(
    r"#\s*noqa|#\s*type:\s*ignore|eslint-disable|@ts-ignore|shellcheck\s+disable|"
    r"\bit\.skip\b|\bxit\(|\bxfail\b|pytest\.mark\.skip|pytest\.skip\(|@unittest\.skip|#\[ignore\]|"
    r"\.skip\("
)

_ENV_RE = re.compile(r"os\.environ|os\.getenv|process\.env")
_GUARD_RE = re.compile(r"\breturn\b|pytest\.skip|\bskip|sys\.exit|\bcontinue\b|SystemExit")
_IF_RE = re.compile(r"\bif\b")

_IMPORT_RE = re.compile(
    r"^\s*(import\s+\S|from\s+\S+\s+import\s|require\(|(const|let|var)\s+.*=\s*require\()"
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --- git plumbing ----------------------------------------------------------


class ScanError(Exception):
    """Raised when git cannot give a trustworthy answer (fail closed)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


def _git(repo: str, args: List[str]) -> bytes:
    cmd = ["git", "--literal-pathspecs", "-C", repo] + args
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ScanError("diff-unreadable", "cannot run git: %s" % exc)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScanError("diff-unreadable", msg[0] if msg else "git exited %d" % proc.returncode)
    return proc.stdout


def resolve_ref(repo: str, ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise ScanError("bad-ref", repr(ref))
    cmd = ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ScanError("diff-unreadable", "cannot run git: %s" % exc)
    sha = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not sha:
        raise ScanError("bad-ref", ref)
    return sha


def changed_files(repo: str, base: str, head: str) -> List[Tuple[str, str]]:
    """Return [(status letter, path)] for base..head, renames split into D + A."""
    out = _git(repo, ["diff", "--raw", "-z", "--no-renames", "--no-ext-diff", base, head])
    tokens = out.decode("utf-8", "replace").split("\0")
    files = []  # type: List[Tuple[str, str]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith(":"):
            fields = tok.split()
            status = fields[-1][:1] if fields else "M"
            if i + 1 >= len(tokens):
                raise ScanError("diff-unreadable", "truncated raw diff")
            files.append((status, tokens[i + 1]))
            i += 2
        else:
            i += 1
    return files


class Hunk:
    def __init__(self, old_start: int, old_len: int, new_start: int, new_len: int) -> None:
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.removed = []  # type: List[Tuple[int, str]]
        self.added = []  # type: List[Tuple[int, str]]


def file_hunks(repo: str, base: str, head: str, path: str) -> List[Hunk]:
    out = _git(
        repo,
        ["diff", "-U0", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
         base, head, "--", path],
    )
    text = out.decode("utf-8", "replace")
    hunks = []  # type: List[Hunk]
    cur = None  # type: Optional[Hunk]
    old_n = 0
    new_n = 0
    for line in text.split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            cur = Hunk(int(m.group(1)), old_len, int(m.group(3)), new_len)
            hunks.append(cur)
            old_n = cur.old_start
            new_n = cur.new_start
            continue
        if cur is None or line.startswith("\\"):
            continue
        if line.startswith("-"):
            cur.removed.append((old_n, line[1:]))
            old_n += 1
        elif line.startswith("+"):
            cur.added.append((new_n, line[1:]))
            new_n += 1
    return hunks


# --- checks ----------------------------------------------------------------


def _range(start: int, length: int) -> Tuple[int, int]:
    """Closed interval in doubled coordinates; zero-length is a point between lines."""
    if length > 0:
        return (2 * start, 2 * (start + length) - 1)
    return (2 * start + 1, 2 * start + 1)


def _overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return max(a[0], b[0]) <= min(a[1], b[1])


def _lines_label(nums: List[int]) -> str:
    if not nums:
        return "?"
    return "%d-%d" % (min(nums), max(nums)) if len(nums) > 1 else str(nums[0])


def check_neutered(path: str, hunks: List[Hunk]) -> List[str]:
    reasons = []  # type: List[str]
    for h in hunks:
        for n, text in h.added:
            if _NEUTER_RE.search(text):
                reasons.append("neutered-verification: %s:%d" % (path, n))
    return reasons


def check_stub(path: str, hunks: List[Hunk]) -> List[str]:
    reasons = []  # type: List[str]
    for h in hunks:
        added = h.added
        for idx, (n, text) in enumerate(added):
            if _STUB_RE.search(text) or _STUB_SAME_LINE_RE.search(text):
                reasons.append("stub-body: %s:%d" % (path, n))
                continue
            dm = _PY_DEF_RE.match(text)
            if dm and idx + 1 < len(added):
                body = added[idx + 1][1].strip()
                if body in ("pass", "...", "return"):
                    after = added[idx + 2][1] if idx + 2 < len(added) else ""
                    indent = len(dm.group(1))
                    after_indent = len(after) - len(after.lstrip())
                    if not after.strip() or after_indent <= indent:
                        reasons.append("stub-body: %s:%d" % (path, n))
    return reasons


def check_suppressions(path: str, hunks: List[Hunk]) -> List[str]:
    reasons = []  # type: List[str]
    for h in hunks:
        for n, text in h.added:
            if _SUPPRESS_RE.search(text):
                reasons.append("added-suppression: %s:%d" % (path, n))
    return reasons


def check_env_guard(path: str, hunks: List[Hunk]) -> List[str]:
    reasons = []  # type: List[str]
    for h in hunks:
        added = h.added
        for idx, (n, text) in enumerate(added):
            if not (_ENV_RE.search(text) and _IF_RE.search(text)):
                continue
            window = [text] + [t for _, t in added[idx + 1: idx + 3]]
            m = _ENV_RE.search(text)
            tail = text[m.end():] if m else ""
            if _GUARD_RE.search(tail) or any(_GUARD_RE.search(t) for t in window[1:]):
                reasons.append("env-guard: %s:%d" % (path, n))
    return reasons


def check_revert_to_pass(
    repo: str, revert_base: str, base: str, path: str, hunks: List[Hunk]
) -> List[str]:
    """Narrowed revert-to-pass rule (a)/(b); see the module docstring."""
    prior = file_hunks(repo, revert_base, base, path)
    if not prior:
        return []
    prior_added = set()  # type: Set[int]
    for ph in prior:
        for n, _ in ph.added:
            prior_added.add(n)
    reasons = []  # type: List[str]
    for h in hunks:
        # (a) removes a non-import, non-blank line that revert_base..base added
        hit = [
            n for n, text in h.removed
            if n in prior_added and text.strip() and not _IMPORT_RE.match(text)
        ]
        if hit:
            reasons.append("revert-to-pass: %s:%s" % (path, _lines_label(hit)))
            continue
        # (b) post-image restores the pre-revert_base text of a region it changed
        post = [t.strip() for _, t in h.added]
        if not any(post):
            continue
        h_range = _range(h.old_start, h.old_len)
        for ph in prior:
            if not ph.removed:
                continue
            if not _overlap(h_range, _range(ph.new_start, ph.new_len)):
                continue
            if post == [t.strip() for _, t in ph.removed]:
                reasons.append(
                    "revert-to-pass: %s:%s" % (path, _lines_label([n for n, _ in h.added]))
                )
                break
    return reasons


# --- driver ----------------------------------------------------------------


def scan(
    repo: str, base: str, head: str, profile: str, revert_base: Optional[str]
) -> List[str]:
    reasons = []  # type: List[str]
    if profile == "heal" and not revert_base:
        raise ScanError("missing-revert-base", "--revert-base is required for profile heal")
    if profile != "heal" and revert_base:
        raise ScanError(
            "revert-base-not-allowed", "--revert-base is only valid for profile heal"
        )
    base_sha = resolve_ref(repo, base)
    head_sha = resolve_ref(repo, head)
    rb_sha = resolve_ref(repo, revert_base) if revert_base else None

    files = changed_files(repo, base_sha, head_sha)
    hunk_cache = {}  # type: Dict[str, List[Hunk]]

    def hunks_for(p: str) -> List[Hunk]:
        if p not in hunk_cache:
            hunk_cache[p] = file_hunks(repo, base_sha, head_sha, p)
        return hunk_cache[p]

    for status, path in files:
        test_path = is_test_path(path)
        if test_path:
            if status == "D":
                reasons.append("test-removed: %s" % path)
            elif profile in ("implement", "heal"):
                reasons.append("test-edit: %s" % path)
        if profile == "heal" and is_gate_path(path):
            reasons.append("gate-config-edit: %s" % path)
        if status == "D":
            continue
        hunks = hunks_for(path)
        if is_config_path(path):
            reasons.extend(check_neutered(path, hunks))
        if is_impl_path(path):
            reasons.extend(check_stub(path, hunks))
        if profile == "heal":
            reasons.extend(check_suppressions(path, hunks))
            reasons.extend(check_env_guard(path, hunks))
    if profile == "heal" and rb_sha:
        for status, path in files:
            if status == "A":
                continue
            reasons.extend(check_revert_to_pass(repo, rb_sha, base_sha, path, hunks_for(path)))
    seen = set()  # type: Set[str]
    unique = []  # type: List[str]
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Anti-cheat scan over a git diff range.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--revert-base", default=None)
    args = parser.parse_args(argv)

    try:
        reasons = scan(args.repo, args.base, args.head, args.profile, args.revert_base)
    except ScanError as exc:
        reasons = ["%s: %s" % (exc.reason, exc.detail)]
    except Exception as exc:  # fail closed on anything unexpected
        reasons = ["diff-unreadable: %s: %s" % (type(exc).__name__, exc)]

    verdict = "fail" if reasons else "pass"
    sys.stdout.write(json.dumps({"verdict": verdict, "reasons": reasons}) + "\n")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
