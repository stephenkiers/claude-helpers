"""
Ship guard for the v2 /expert-flow driver (Layer 2 Step 4, ADR-0026, plan-doc Q4, sessions D2 and Q2 of Step 4).

Two edge adapters parse `gh` and `git` output at the boundary into typed values:

  check_pr: runs `gh pr view <branch> --json number,state,headRefOid` and compares the PR head with an
    expected SHA. The caller passes heads.verify before ship, and heads.ship after the ship receipt lands.
    MERGED is Merged and CLOSED is Closed (session D2), each only when its headRefOid equals the expected SHA
    (D4 cross-check; a mismatch is GhError); OPEN compares the head.
  check_local_head / read_head: `git -C <worktree> rev-parse HEAD`, the driver-owned HEAD source (heads.verify
    when a verify receipt lands OK, heads.ship when the ship receipt lands).

A `headRefOid` that is not a full 40- or 64-character lowercase hex SHA is a GhError, never a comparison.
Every function returns a typed result; none raises. The caller maps each result to a directive, and the
`gh` call and the resulting state commit happen in one flow-lock hold (plan, Step 4).

Ship's own --force-with-lease stays the real backstop for a push between this check and ship (Rachel).

Python 3.8 floor: typing.Optional/Union/Tuple only, no match statements, no X | Y at runtime.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Union

from .model import assert_never
from .preflight import CANNOT_START, Runner, run_command
from .state import Sha

GH_PR_FIELDS = "number,state,headRefOid"
_GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PR_STATE_VALUES = ("OPEN", "CLOSED", "MERGED")
_NO_PR_MARKER = "no pull requests found"


@dataclass(frozen=True)
class PrView:
    """One `gh pr view` answer, parsed at the edge. state is OPEN, CLOSED or MERGED."""

    number: int
    state: str
    head_ref_oid: Sha


@dataclass(frozen=True)
class NoPr:
    """No PR exists for the branch. The caller emits SPAWN ship."""


@dataclass(frozen=True)
class SameHead:
    """The PR is OPEN and its head equals the expected SHA. The caller skips ship and logs `head`."""

    number: int
    head: Sha


@dataclass(frozen=True)
class HeadDiffers:
    """The PR is OPEN and its head is not the expected SHA. The caller maps this to STOP ship-head-differs."""

    number: int
    expected: Sha
    actual: Sha


@dataclass(frozen=True)
class Closed:
    """The PR is CLOSED and unmerged, whatever its head. The caller maps this to STOP ship-pr-closed (D2)."""

    number: int


@dataclass(frozen=True)
class Merged:
    """The PR is MERGED. The caller advances to done (status done) and never constructs a merge SPAWN."""

    number: int


@dataclass(frozen=True)
class GhError:
    """gh failed, could not start, or returned output that does not parse. The caller maps this to STOP gh-error."""

    detail: str


ShipGuardResult = Union[NoPr, SameHead, HeadDiffers, Closed, Merged, GhError]


@dataclass(frozen=True)
class LocalHeadMatch:
    """The worktree HEAD equals the expected SHA."""

    head: Sha


@dataclass(frozen=True)
class LocalHeadMismatch:
    """The worktree HEAD differs from the expected SHA. The caller maps this to STOP expected-head-mismatch."""

    expected: Sha
    actual: Sha


@dataclass(frozen=True)
class GitError:
    """git could not report the HEAD of the worktree."""

    detail: str


LocalHeadResult = Union[LocalHeadMatch, LocalHeadMismatch, GitError]


def parse_sha(text: str) -> Optional[Sha]:
    """A full 40- or 64-character lowercase hex SHA, or None. Short SHAs and branch names are rejected."""
    if _GIT_SHA.fullmatch(text) is None:
        return None
    return Sha(text)


def parse_pr_view(text: str) -> Union[PrView, GhError]:
    """Parse the JSON from `gh pr view --json number,state,headRefOid`. Any shape problem is a GhError."""
    try:
        raw: object = json.loads(text)
    except ValueError:
        return GhError("gh pr view returned output that is not JSON")
    if not isinstance(raw, dict):
        return GhError("gh pr view returned a JSON value that is not an object")
    number = raw.get("number")
    state = raw.get("state")
    head = raw.get("headRefOid")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return GhError("gh pr view: number is not a positive integer")
    if not isinstance(state, str) or state not in _PR_STATE_VALUES:
        return GhError(f"gh pr view: state {state!r} is not OPEN, CLOSED or MERGED")
    if not isinstance(head, str):
        return GhError("gh pr view: headRefOid is missing or not a string")
    sha = parse_sha(head)
    if sha is None:
        return GhError("gh pr view: headRefOid is not a full 40- or 64-character lowercase hex SHA")
    return PrView(number=number, state=state, head_ref_oid=sha)


def check_pr(branch: str, expected: Sha, run: Runner = run_command, repo: Optional[str] = None) -> ShipGuardResult:
    """
    Ask gh for the PR on branch and compare it with expected (heads.verify before ship, heads.ship after).

    repo, when given (OWNER/NAME), pins the query with `-R` so gh never resolves the branch in another repo (D4).
    Order: MERGED first (Merged), then CLOSED (Closed), then OPEN compares the head. For MERGED and CLOSED the
    headRefOid is cross-checked with expected; a mismatch is GhError, so the caller stops rather than trusting
    a PR whose head is not the one that was verified (D4). No PR for the branch is NoPr. Any other gh failure,
    including a timeout, or an unparseable answer, is GhError.
    """
    if not branch or branch.startswith("-"):
        return GhError(f"invalid branch name {branch!r}")
    argv: List[str] = ["gh", "pr", "view", branch, "--json", GH_PR_FIELDS]
    if repo is not None:
        if not repo or repo.startswith("-"):
            return GhError(f"invalid repo {repo!r}")
        argv += ["-R", repo]
    result = run(argv)
    if result.returncode != 0:
        if _NO_PR_MARKER in result.stderr.lower():
            return NoPr()
        first = result.stderr.strip().splitlines()[0] if result.stderr.strip() else f"exit {result.returncode}"
        if result.returncode == CANNOT_START:
            return GhError(first)
        return GhError(f"gh pr view failed: {first}")
    parsed = parse_pr_view(result.stdout)
    if isinstance(parsed, GhError):
        return parsed
    if parsed.state in ("MERGED", "CLOSED") and parsed.head_ref_oid != expected:
        return GhError(
            f"gh pr view: PR #{parsed.number} is {parsed.state} but its head {parsed.head_ref_oid} "
            f"differs from expected {expected}"
        )
    if parsed.state == "MERGED":
        return Merged(number=parsed.number)
    if parsed.state == "CLOSED":
        return Closed(number=parsed.number)
    if parsed.head_ref_oid == expected:
        return SameHead(number=parsed.number, head=parsed.head_ref_oid)
    return HeadDiffers(number=parsed.number, expected=expected, actual=parsed.head_ref_oid)


def read_head(worktree: str, run: Runner = run_command) -> Union[Sha, GitError]:
    """The full HEAD SHA of worktree, from `git -C <worktree> rev-parse HEAD`. worktree must be absolute."""
    if not os.path.isabs(worktree):
        return GitError(f"worktree path {worktree!r} is not absolute")
    result = run(["git", "-C", worktree, "rev-parse", "HEAD"])
    if result.returncode != 0:
        first = result.stderr.strip().splitlines()[0] if result.stderr.strip() else f"exit {result.returncode}"
        return GitError(f"git rev-parse HEAD failed: {first}")
    sha = parse_sha(result.stdout.strip())
    if sha is None:
        return GitError("git rev-parse HEAD did not print a full 40- or 64-character SHA")
    return sha


def check_local_head(worktree: str, expected: Sha, run: Runner = run_command) -> LocalHeadResult:
    """Compare the worktree HEAD with expected (heads.verify before ship). Pure reads; takes no lock itself."""
    head = read_head(worktree, run)
    if isinstance(head, GitError):
        return head
    if head == expected:
        return LocalHeadMatch(head=head)
    return LocalHeadMismatch(expected=expected, actual=head)


def describe_ship_guard(result: ShipGuardResult) -> str:
    """A one-line description for the driver log and the STOP detail. Every case of the union is handled."""
    if isinstance(result, NoPr):
        return "no PR for the branch"
    if isinstance(result, SameHead):
        return f"PR #{result.number} is OPEN at the expected head {result.head}"
    if isinstance(result, HeadDiffers):
        return f"PR #{result.number} head {result.actual} differs from expected {result.expected}"
    if isinstance(result, Closed):
        return f"PR #{result.number} closed unmerged"
    if isinstance(result, Merged):
        return f"PR #{result.number} is merged"
    if isinstance(result, GhError):
        return result.detail
    assert_never(result)
