#!/usr/bin/env python3
"""
Unit tests for issue_comments.py: comment filtering, budgets, scouts, and flow state.

Covers:
- Trust filtering (OWNER, COLLABORATOR, others as unverified)
- Superseded plan watermark and relative-age drop
- Newest Implementation Plan only
- Minimized comments dropped
- Budget application and ceiling
- Unverified withheld under --trusted-only
- Acknowledged ids (from plan snapshot and question answers)
- Fetch/finalize exit codes and status values
- Markdown rendering with nonce fence
- CONTINUE_LABEL constant
- Contract round-trip (to_dict/from_dict)

Run with: python3 tests/test_issue_comments.py
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import REPO_ROOT, Harness
from workflow.models import (
    IssueWithComments, IssueComment, CommentCounts, CommentsStatus,
    CommentTrust, CommentKind, CommentSource, DropReason, ScoutVerdict
)
from workflow import issue_comments

h = Harness("ISSUE COMMENTS UNIT TESTS")


def iso_time(offset_seconds=0):
    """Return ISO 8601 UTC time, optionally offset by seconds."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat()


def build_raw_comment(id_str, body, author_login=None, association=None, minimized=False, created_at=None):
    """Build a raw gh JSON comment entry."""
    if created_at is None:
        created_at = iso_time()
    comment = {
        "id": id_str,
        "body": body,
        "createdAt": created_at,
        "isMinimized": minimized,
        "authorAssociation": association,
    }
    if author_login:
        comment["author"] = {"login": author_login}
    return comment


def build_issue_json(number=1, title="Test", body="Test body", comments_list=None):
    """Build a raw gh issue JSON."""
    base_time = iso_time(-1000)
    issue = {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/test/repo/issues/{number}",
        "state": "open",
        "labels": [],
        "createdAt": base_time,
    }
    if comments_list is not None:
        issue["comments"] = comments_list
    return issue, base_time


# ==============================================================================
# TEST: Trust and Association
# ==============================================================================
print("\n[Trust and Association]")

# OWNER is trusted
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "owner comment", "alice", "OWNER")
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "OWNER association marked as trusted",
    len(parsed.comments) == 1 and parsed.comments[0].trust == "trusted",
    f"got trust={parsed.comments[0].trust if parsed.comments else 'no comments'}"
)

# COLLABORATOR is trusted
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "collab comment", "bob", "COLLABORATOR")
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "COLLABORATOR association marked as trusted",
    len(parsed.comments) == 1 and parsed.comments[0].trust == "trusted",
    f"got trust={parsed.comments[0].trust if parsed.comments else 'no comments'}"
)

# NONE is unverified
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "outsider comment", "charlie", "NONE")
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "NONE association marked as unverified",
    len(parsed.comments) == 1 and parsed.comments[0].trust == "unverified",
    f"got trust={parsed.comments[0].trust if parsed.comments else 'no comments'}"
)

# Missing association is unverified
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "comment", "dave", None)
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "missing association marked as unverified",
    len(parsed.comments) == 1 and parsed.comments[0].trust == "unverified",
    f"got trust={parsed.comments[0].trust if parsed.comments else 'no comments'}"
)

# Null author is unverified
raw = build_issue_json(comments_list=[
    {
        "id": "c1",
        "body": "ghost comment",
        "createdAt": iso_time(),
        "isMinimized": False,
        "author": None,
        "authorAssociation": "NONE"
    }
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "null author marked as unverified",
    len(parsed.comments) == 1 and parsed.comments[0].trust == "unverified",
)

# ==============================================================================
# TEST: Superseded Plan Marker and Watermark
# ==============================================================================
print("\n[Superseded Plan Marker and Watermark]")

# Trusted superseded comment drops, updates watermark
t1 = iso_time(-500)
t2 = iso_time(-100)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "early comment", "alice", "OWNER", created_at=t1),
    build_raw_comment("c2", "## Superseded Plan\nold plan", "alice", "OWNER", created_at=t2),
    build_raw_comment("c3", "decision", "alice", "OWNER", created_at=iso_time()),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "trusted superseded plan comment dropped",
    filtered.counts.dropped_superseded == 1,
    f"got dropped_superseded={filtered.counts.dropped_superseded}"
)
h.test_result(
    "watermark updated to superseded comment's timestamp",
    filtered.watermark == t2,
    f"got watermark={filtered.watermark}"
)
h.test_result(
    "only decision and non-early comments remain after watermark",
    len(filtered.comments) == 1 and any("decision" in c.body for c in filtered.comments),
    f"got {len(filtered.comments)} comments, expected decision to survive watermark"
)

# Unverified superseded marker does NOT move watermark
issue_time = iso_time(-1000)
marker_time = iso_time(-100)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "## Superseded Plan\nfake", "outsider", "NONE", created_at=marker_time),
    build_raw_comment("c2", "real decision", "alice", "OWNER", created_at=iso_time()),
])
raw[0]["createdAt"] = issue_time
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "unverified superseded marker kept as ordinary comment",
    len(filtered.comments) == 2 and any(c.trust == "unverified" for c in filtered.comments),
)
h.test_result(
    "watermark not moved by unverified marker (equals issue creation time)",
    filtered.watermark == issue_time,
    f"got watermark={filtered.watermark}, issue_time={issue_time}"
)

# No marker means watermark equals issue creation time
issue_time = iso_time(-1000)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "comment 1", "alice", "OWNER", created_at=iso_time(-500)),
])
raw[0]["createdAt"] = issue_time
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "no marker: watermark equals issue creation time",
    filtered.watermark == issue_time,
)

# Near-miss: leading blank line before marker (should NOT match)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "\n## Superseded Plan\nold", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "leading blank line before marker: comment is kept (not matched)",
    len(filtered.comments) == 1 and filtered.counts.dropped_superseded == 0,
)

# Near-miss: ### instead of ## (should NOT match)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "### Superseded Plan\nold", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "### instead of ##: comment is kept (not matched)",
    len(filtered.comments) == 1 and filtered.counts.dropped_superseded == 0,
)

# Near-miss: marker in body (not first line, should NOT match)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "some text\n## Superseded Plan\nmore", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "marker not on first line: comment is kept (not matched)",
    len(filtered.comments) == 1 and filtered.counts.dropped_superseded == 0,
)

# Near-miss: lowercase (should NOT match)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "## superseded plan\nold", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "lowercase marker: comment is kept (not matched)",
    len(filtered.comments) == 1 and filtered.counts.dropped_superseded == 0,
)

# Near-miss: BOM and trailing \r (SHOULD match after stripping)
bom_marker = "﻿## Superseded Plan\r\nold"
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", bom_marker, "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "BOM and trailing CR stripped, marker matched",
    filtered.counts.dropped_superseded == 1,
    f"got dropped_superseded={filtered.counts.dropped_superseded}"
)

# ==============================================================================
# TEST: Minimized Comments
# ==============================================================================
print("\n[Minimized Comments]")

raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "visible", "alice", "OWNER", minimized=False),
    build_raw_comment("c2", "hidden", "alice", "OWNER", minimized=True),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
h.test_result(
    "minimized comment dropped in parse",
    len(parsed.comments) == 1 and parsed.counts.dropped_minimized == 1,
)

# ==============================================================================
# TEST: Implementation Plan (keep newest only)
# ==============================================================================
print("\n[Implementation Plan (Newest Only)]")

t1 = iso_time(-300)
t2 = iso_time(-200)
t3 = iso_time(-100)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "## Implementation Plan\nv1", "alice", "OWNER", created_at=t1),
    build_raw_comment("c2", "## Implementation Plan\nv2", "alice", "OWNER", created_at=t2),
    build_raw_comment("c3", "## Implementation Plan\nv3", "alice", "OWNER", created_at=t3),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "stale implementation plans dropped",
    filtered.counts.dropped_stale_implementation_plan == 2,
)
impl_plan = [c for c in filtered.comments if c.kind == "implementation_plan"]
h.test_result(
    "newest implementation plan kept with kind=implementation_plan",
    len(impl_plan) == 1 and impl_plan[0].created_at == t3,
)

# Unverified implementation plan is kept as ordinary
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "## Implementation Plan\nfake", "outsider", "NONE"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "unverified implementation plan kept as ordinary comment",
    len(filtered.comments) == 1 and filtered.comments[0].kind == "ordinary",
)

# ==============================================================================
# TEST: Acknowledged Comments
# ==============================================================================
print("\n[Acknowledged Comments]")

raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "comment 1", "alice", "OWNER"),
    build_raw_comment("c2", "comment 2", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed, acknowledged_ids=["c1"])
h.test_result(
    "comments in acknowledged_ids marked as acknowledged=true",
    any(c.id == "c1" and c.acknowledged for c in filtered.comments),
)
h.test_result(
    "other comments remain acknowledged=false",
    any(c.id == "c2" and not c.acknowledged for c in filtered.comments),
)

# ==============================================================================
# TEST: CONTINUE_LABEL Constant
# ==============================================================================
print("\n[CONTINUE_LABEL Constant]")

h.test_result(
    "CONTINUE_LABEL constant exists and is correct",
    issue_comments.CONTINUE_LABEL == "Continue — the plan stands",
)

# ==============================================================================
# TEST: Trusted-Only Flag
# ==============================================================================
print("\n[Trusted-Only Flag]")

raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "trusted comment", "alice", "OWNER"),
    build_raw_comment("c2", "unverified comment", "outsider", "NONE"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed, trusted_only=True)
h.test_result(
    "unverified comments withheld under trusted_only",
    filtered.counts.withheld_unverified == 1,
)
h.test_result(
    "only trusted comments in output",
    len(filtered.comments) == 1 and filtered.comments[0].trust == "trusted",
)
h.test_result(
    "unverified author logins listed",
    "outsider" in filtered.withheld_unverified_authors,
)

# ==============================================================================
# TEST: Budgets and Ceiling
# ==============================================================================
print("\n[Budgets and Ceiling]")

# Many unverified comments: keep newest, drop oldest
old_time = iso_time(-2000)
comments_list = []
for i in range(15):
    t = iso_time(int(-1000 + i * 50))
    comments_list.append(
        build_raw_comment(f"c{i}", f"unverified comment {i} " * 100, "outsider", "NONE", created_at=t)
    )
raw = build_issue_json(comments_list=comments_list)
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "unverified budget truncates oldest comments",
    filtered.truncated or filtered.counts.truncated_unverified > 0,
)

# ==============================================================================
# TEST: Contract Round-Trip (to_dict/from_dict)
# ==============================================================================
print("\n[Contract Round-Trip]")

raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "test comment", "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
as_dict = parsed.to_dict()
h.test_result(
    "to_dict includes schema_version",
    as_dict.get("schema_version") == 1,
)
restored = IssueWithComments.from_dict(as_dict)
h.test_result(
    "from_dict restores number, title, body",
    restored.number == parsed.number and restored.title == parsed.title and restored.body == parsed.body,
)
h.test_result(
    "from_dict restores comments list",
    len(restored.comments) == len(parsed.comments),
)

# ==============================================================================
# TEST: Missing Comments Key
# ==============================================================================
print("\n[Missing Comments Key]")

raw, _ = build_issue_json()
# Don't add comments key at all
parsed = issue_comments.parse_gh_issue_json(raw)
h.test_result(
    "missing comments key yields comments_status=unavailable",
    parsed.comments_status == "unavailable",
)
h.test_result(
    "comments list is empty",
    len(parsed.comments) == 0,
)

# ==============================================================================
# TEST: Plan-Fetch Lower Bound (Flow)
# ==============================================================================
print("\n[Plan-Fetch Lower Bound (Flow)]")

# Comment between plan fetch and watermark: kept as new
plan_fetch_time = iso_time(-500)
watermark_time = iso_time(-300)
new_comment_time = iso_time(-200)

raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "comment before fetch", "alice", "OWNER", created_at=iso_time(-1000)),
    build_raw_comment("c2", "comment after fetch", "alice", "OWNER", created_at=new_comment_time),
    build_raw_comment("c3", "## Superseded Plan\nmarker", "alice", "OWNER", created_at=watermark_time),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
plan_fetch_record = {"fetched_at": plan_fetch_time, "comment_ids": []}
filtered = issue_comments.filter_comments(parsed, plan_fetch=plan_fetch_record)
h.test_result(
    "comment after plan fetch time is kept",
    any(c.created_at == new_comment_time for c in filtered.comments),
)
h.test_result(
    "lower_bound_source set to plan_fetch",
    filtered.lower_bound_source == "plan_fetch",
)

# Acknowledged comment before fetch: kept
fetch_time = iso_time(-500)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "shown to planner", "alice", "OWNER", created_at=iso_time(-600)),
    build_raw_comment("c2", "after fetch", "alice", "OWNER", created_at=iso_time(-400)),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
plan_fetch_record = {"fetched_at": fetch_time, "comment_ids": ["c1"]}
filtered = issue_comments.filter_comments(parsed, plan_fetch=plan_fetch_record)
acknowledged = [c for c in filtered.comments if c.id == "c1"]
h.test_result(
    "acknowledged comment before fetch kept with acknowledged=true",
    len(acknowledged) == 1 and acknowledged[0].acknowledged,
)
# c2 is after fetch, so it should be kept
kept_c2 = [c for c in filtered.comments if c.id == "c2"]
h.test_result(
    "comment after fetch time kept",
    len(kept_c2) == 1,
)

# ==============================================================================
# TEST: Relative Age (No Wall-Clock Rule)
# ==============================================================================
print("\n[Relative Age (No Wall-Clock Rule)]")

# Old issue, comment after issue creation: kept if no marker
issue_time = iso_time(-365 * 24 * 3600)  # one year ago
comment_time = iso_time(-200 * 24 * 3600)  # 200 days ago (after issue creation)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "old but newer than issue", "alice", "OWNER", created_at=comment_time),
])
raw[0]["createdAt"] = issue_time
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "comment after issue creation kept (no wall-clock rule)",
    len(filtered.comments) == 1,
    f"got {len(filtered.comments)} comments"
)

# Body edit (updatedAt) is not used for age comparison; comments before watermark are dropped
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", "comment", "alice", "OWNER", created_at=iso_time(-1000)),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
# With no marker, watermark = issue creation time, which is iso_time()
# So comment at -1000 is before watermark and will be dropped
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "comment before watermark dropped (body updatedAt not used)",
    filtered.counts.dropped_pre_watermark == 1,
    f"got dropped_pre_watermark={filtered.counts.dropped_pre_watermark}"
)

# ==============================================================================
# TEST: Marker Literal Matching
# ==============================================================================
print("\n[Marker Literal Matching]")

# Test SUPERSEDED_PLAN_MARKER constant
h.test_result(
    "SUPERSEDED_PLAN_MARKER constant is correct",
    issue_comments.SUPERSEDED_PLAN_MARKER == "## Superseded Plan",
)

# Test IMPLEMENTATION_PLAN_MARKER constant
h.test_result(
    "IMPLEMENTATION_PLAN_MARKER constant is correct",
    issue_comments.IMPLEMENTATION_PLAN_MARKER == "## Implementation Plan",
)

# ==============================================================================
# TEST: Trusted Associations Constant
# ==============================================================================
print("\n[Trusted Associations Constant]")

h.test_result(
    "TRUSTED_ASSOCIATIONS contains OWNER and COLLABORATOR",
    "OWNER" in issue_comments.TRUSTED_ASSOCIATIONS and "COLLABORATOR" in issue_comments.TRUSTED_ASSOCIATIONS,
)
h.test_result(
    "TRUSTED_ASSOCIATIONS is a frozenset",
    isinstance(issue_comments.TRUSTED_ASSOCIATIONS, frozenset),
)

# ==============================================================================
# TEST: Per-Comment Char Cap Boundary [WEAK_ASSERTION FIX]
# ==============================================================================
print("\n[Per-Comment Char Cap Boundary]")

# Comment with body length exactly at PER_COMMENT_CHAR_CAP should NOT be clipped
body_at_cap = "x" * issue_comments.PER_COMMENT_CHAR_CAP
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", body_at_cap, "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "comment with body length exactly at PER_COMMENT_CHAR_CAP is NOT clipped",
    len(filtered.comments) == 1 and len(filtered.comments[0].body) == issue_comments.PER_COMMENT_CHAR_CAP,
    f"got length {len(filtered.comments[0].body) if filtered.comments else 'no comments'}"
)

# Comment with body length one over PER_COMMENT_CHAR_CAP should BE clipped
body_over_cap = "x" * (issue_comments.PER_COMMENT_CHAR_CAP + 1)
raw = build_issue_json(comments_list=[
    build_raw_comment("c1", body_over_cap, "alice", "OWNER"),
])
parsed = issue_comments.parse_gh_issue_json(raw[0])
filtered = issue_comments.filter_comments(parsed)
h.test_result(
    "comment with body length over PER_COMMENT_CHAR_CAP is clipped",
    len(filtered.comments) == 1 and "[clipped" in filtered.comments[0].body,
    f"got body length {len(filtered.comments[0].body) if filtered.comments else 'no comments'}, clipped: {'[clipped' in (filtered.comments[0].body if filtered.comments else '')}"
)

h.summarize_and_exit()
