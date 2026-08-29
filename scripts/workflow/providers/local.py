"""
Local provider implementation for local-plan-mode tracking.

Works with project-root array-format issues.json and plans/ directory
instead of GitHub issues.
"""

from pathlib import Path
from typing import List, Optional, Union

from ..models import IssueInfo, LocalTrackerData
from ..cache import read_local_tracker, write_local_tracker
from ..safety import Unknown


class LocalProvider:
    """Local provider for project-root issues.json and plans directory."""

    def __init__(self, tracker_path: Path, plans_dir: Path):
        """Initialize local provider with tracker and plans directory paths."""
        self.tracker_path = tracker_path
        self.plans_dir = plans_dir

    def repo_identity(self) -> Optional[tuple]:
        """Local mode has no GitHub identity."""
        return None

    def current_user(self) -> Optional[str]:
        """Local mode has no user concept."""
        return None

    def list_open_issues(self) -> List[IssueInfo]:
        """
        List open issues from project-root array-format issues.json.

        Reads the tracker file, filters entries with status "todo" or "planned",
        and returns them as IssueInfo list. Returns [] if the tracker is missing,
        unreadable, or fails schema validation (data is None). If some entries were
        dropped as malformed but others parsed fine (data is not None but err is
        set), the valid entries are still returned rather than discarding all of
        them for one bad row — duplicate detection is load-bearing and a single
        malformed entry should not silently blind it.
        """
        data, err = read_local_tracker(self.tracker_path)
        if data is None:
            return []

        result = []
        for entry in data.entries:
            if entry.status in ("todo", "planned"):
                result.append(IssueInfo(
                    number=entry.id,
                    url="",
                    title=entry.title,
                    body="",
                    state="open"
                ))
        return result

    def create_issue(self, title: str, body: str, labels: List[str], assignee: Optional[str]) -> IssueInfo:
        """
        Create a local issue entry.

        Allocates a new ID (max(existing) + 1, or 1 if empty), writes the plan file
        to plans/{id}-{slug}.md, and appends the entry to the tracker.
        Raises RuntimeError if the tracker write fails (orphaned plan file is a half-state).
        """
        from .. import track  # Import here to avoid circular import

        # Read current tracker to get next ID. Unlike list_open_issues (which now
        # tolerates dropped entries so reading past a bad row is safe), this stays
        # fail-closed: a dropped row may have held the current max id, and silently
        # allocating past it risks colliding with an id that still exists on disk.
        # "Not found" (no tracker file yet) is the one exception, since that's the
        # expected first-issue-ever state, not a malformed file.
        data, err = read_local_tracker(self.tracker_path)
        if err and "not found" not in str(err):
            raise RuntimeError(
                f"could not read tracker at {self.tracker_path}: {err}. "
                f"Fix the malformed entry in that file before creating a new issue."
            )
        if not data:
            data = LocalTrackerData()

        # Allocate next ID
        if data.entries:
            next_id = max(e.id for e in data.entries) + 1
        else:
            next_id = 1

        # Generate slug for plan file path
        slug = track.slugify(title, max_len=50)
        plan_path = self.plans_dir / f"{next_id}-{slug}.md"
        plan_path_rel = f"plans/{next_id}-{slug}.md"

        # Create plans directory if needed
        self.plans_dir.mkdir(parents=True, exist_ok=True)

        # Write plan file
        try:
            plan_path.write_text(body)
        except OSError as e:
            raise RuntimeError(f"could not write plan file {plan_path}: {e}")

        # Append entry to tracker
        from ..models import LocalTrackerEntry
        data.entries.append(LocalTrackerEntry(
            id=next_id,
            title=title,
            status="in_progress",
            plan=plan_path_rel
        ))

        # Write tracker back
        success, err = write_local_tracker(self.tracker_path, data)
        if not success:
            raise RuntimeError(f"could not write tracker (plan file was created at {plan_path}): {err}")

        return IssueInfo(
            number=next_id,
            url="",
            title=title,
            body=body,
            state="open"
        )

    def comment_issue(self, number: Union[int, str], body: str) -> None:
        """
        Local mode has no separate comment concept — documented no-op.

        In local plan mode, comments are not preserved in a separate channel.
        If the plan pivot flow is used (comment-then-edit), the old plan is
        silently lost unless the caller preserves it another way.
        """
        pass

    def edit_issue_body(self, number: Union[int, str], body: str) -> None:
        """
        Replace a local issue's body (plan file).

        Looks up the entry by ID, finds its plan file path, and rewrites the file.
        Raises RuntimeError if the entry or plan file cannot be found.
        """
        # Convert number to int for lookup
        try:
            issue_id = int(number)
        except (ValueError, TypeError):
            raise RuntimeError(f"invalid issue ID: {number}")

        # Read current tracker
        data, err = read_local_tracker(self.tracker_path)
        if err or not data:
            raise RuntimeError(f"could not read tracker: {err or 'empty'}")

        # Find the entry
        entry = None
        for e in data.entries:
            if e.id == issue_id:
                entry = e
                break

        if not entry:
            raise RuntimeError(f"issue {issue_id} not found in tracker")

        if not entry.plan:
            raise RuntimeError(f"issue {issue_id} has no plan file path")

        # Construct plan file path
        plan_path = self.plans_dir.parent / entry.plan

        # Write new body
        try:
            plan_path.write_text(body)
        except OSError as e:
            raise RuntimeError(f"could not write plan file {plan_path}: {e}")
