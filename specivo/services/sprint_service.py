"""Sprint service — CRUD, lifecycle, board, backlog, velocity, and burndown."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.schemas.sprint import SprintCreate, SprintUpdate

logger = logging.getLogger(__name__)


class SprintService:
    """Service layer for Sprint operations."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project: Project,
        data: SprintCreate,
    ) -> Sprint:
        """Create a new sprint for *project*."""
        sprint = Sprint(
            project_id=project.id,
            name=data.name,
            goal=data.goal,
            start_date=data.start_date,
            end_date=data.end_date,
        )
        session.add(sprint)
        await session.flush()
        await session.refresh(sprint)
        return sprint

    async def get_by_id(
        self,
        session: AsyncSession,
        sprint_id: int,
    ) -> Sprint:
        """Return a Sprint by PK; raises NotFoundError if missing."""
        result = await session.execute(select(Sprint).where(Sprint.id == sprint_id))
        sprint = result.scalar_one_or_none()
        if sprint is None:
            raise NotFoundError(f"Sprint {sprint_id} not found")
        return sprint

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
    ) -> list[Sprint]:
        """List sprints for a project, ordered by start_date ASC nulls last."""
        stmt = (
            select(Sprint)
            .where(Sprint.project_id == project_id)
            .order_by(Sprint.start_date.asc().nullslast(), Sprint.name.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self,
        session: AsyncSession,
        sprint: Sprint,
        data: SprintUpdate,
    ) -> Sprint:
        """Apply a partial update to *sprint*."""
        if data.name is not None:
            sprint.name = data.name
        if data.goal is not None:
            sprint.goal = data.goal
        if data.start_date is not None:
            sprint.start_date = data.start_date
        if data.end_date is not None:
            sprint.end_date = data.end_date
        session.add(sprint)
        await session.flush()
        await session.refresh(sprint)
        return sprint

    async def delete(
        self,
        session: AsyncSession,
        sprint: Sprint,
    ) -> None:
        """Delete *sprint*. Raises ConflictError if active."""
        if sprint.status == "active":
            raise ConflictError("Cannot delete an active sprint")
        await session.delete(sprint)
        await session.flush()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_sprint(
        self,
        session: AsyncSession,
        sprint: Sprint,
    ) -> Sprint:
        """Transition a planned sprint to active.

        Raises ConflictError if:
        - Sprint is not in ``planned`` status
        - Another sprint in the same project is already active
        """
        if sprint.status != "planned":
            raise ConflictError("Only planned sprints can be started")

        # Check no other active sprint in same project
        existing = await session.execute(
            select(Sprint).where(
                Sprint.project_id == sprint.project_id,
                Sprint.status == "active",
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("Another sprint is already active in this project")

        sprint.status = "active"
        if sprint.start_date is None:
            sprint.start_date = date.today()
        session.add(sprint)
        await session.flush()
        await session.refresh(sprint)
        return sprint

    async def complete_sprint(
        self,
        session: AsyncSession,
        sprint: Sprint,
        move_to_sprint_id: int | None = None,
    ) -> Sprint:
        """Transition an active sprint to completed.

        - Queries all issues in this sprint
        - Completed issues (status category in done/closed) stay
        - Incomplete issues are moved to *move_to_sprint_id* or backlog (NULL)
        - Builds velocity_snapshot with total_issues and completed_issues
        - Sets end_date to today if None
        """
        if sprint.status != "active":
            raise ConflictError("Only active sprints can be completed")

        # Fetch all issues in this sprint with their status loaded
        stmt = (
            select(Issue)
            .where(Issue.sprint_id == sprint.id)
            .options(selectinload(Issue.status))
        )
        result = await session.execute(stmt)
        issues = list(result.scalars().all())

        total = len(issues)
        completed = 0
        incomplete_ids = []

        for issue in issues:
            if issue.status.category in ("done", "closed"):
                completed += 1
            else:
                incomplete_ids.append(issue.id)

        # Move incomplete issues
        if incomplete_ids:
            target_sprint_id = move_to_sprint_id  # None means backlog
            await session.execute(
                update(Issue)
                .where(Issue.id.in_(incomplete_ids))
                .values(sprint_id=target_sprint_id)
            )

        sprint.velocity_snapshot = {
            "total_issues": total,
            "completed_issues": completed,
        }
        sprint.status = "completed"
        if sprint.end_date is None:
            sprint.end_date = date.today()

        session.add(sprint)
        await session.flush()
        await session.refresh(sprint)
        return sprint

    # ------------------------------------------------------------------
    # Board & backlog
    # ------------------------------------------------------------------

    async def board_data(
        self,
        session: AsyncSession,
        sprint: Sprint,
    ) -> dict[str, list[Issue]]:
        """Return issues in the sprint grouped by status name."""
        stmt = (
            select(Issue)
            .where(Issue.sprint_id == sprint.id)
            .options(
                selectinload(Issue.status),
                selectinload(Issue.tracker),
                selectinload(Issue.priority),
                selectinload(Issue.assigned_to),
            )
        )
        result = await session.execute(stmt)
        issues = list(result.scalars().all())

        board: dict[str, list[Issue]] = defaultdict(list)
        for issue in issues:
            board[issue.status.name].append(issue)
        return dict(board)

    async def backlog_issues(
        self,
        session: AsyncSession,
        project_id: int,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[Issue], int]:
        """Return issues with sprint_id IS NULL for the project.

        Returns (items, total_count) for pagination.
        """
        base_where = [Issue.project_id == project_id, Issue.sprint_id.is_(None)]

        count_result = await session.execute(
            select(func.count(Issue.id)).where(*base_where)
        )
        total = count_result.scalar_one()

        stmt = (
            select(Issue)
            .where(*base_where)
            .options(
                selectinload(Issue.status),
                selectinload(Issue.tracker),
                selectinload(Issue.priority),
                selectinload(Issue.assigned_to),
            )
            .order_by(Issue.id.asc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all()), total

    async def sprint_issue_count(
        self,
        session: AsyncSession,
        sprint_id: int,
    ) -> int:
        """Return the number of issues assigned to a sprint."""
        from sqlalchemy import func

        stmt = select(func.count()).where(Issue.sprint_id == sprint_id)
        result = await session.execute(stmt)
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Velocity & burndown
    # ------------------------------------------------------------------

    async def average_velocity(
        self,
        session: AsyncSession,
        project_id: int,
    ) -> Decimal:
        """Return the average completed-issue count across all completed sprints.

        Reads ``velocity_snapshot["completed_issues"]`` from each completed
        sprint in the project and returns the arithmetic mean as a Decimal.
        Returns ``Decimal(0)`` when there are no completed sprints.
        """
        stmt = select(Sprint).where(
            Sprint.project_id == project_id,
            Sprint.status == "completed",
        )
        result = await session.execute(stmt)
        sprints = list(result.scalars().all())

        if not sprints:
            return Decimal(0)

        total_completed = sum(
            (s.velocity_snapshot or {}).get("completed_issues", 0) for s in sprints
        )
        return Decimal(total_completed) / Decimal(len(sprints))

    async def burndown_data(
        self,
        session: AsyncSession,
        sprint: Sprint,
    ) -> dict:
        """Build burndown chart data for *sprint*.

        Returns a dict with:
        - ``total_estimated_hours`` — sum of estimated_hours for all sprint issues
        - ``completed_hours`` — sum of estimated_hours for done/closed issues
        - ``data_points`` — list of dicts with ``date``, ``remaining``, ``ideal``
        """
        # Fetch issues with their status loaded
        stmt = (
            select(Issue)
            .where(Issue.sprint_id == sprint.id)
            .options(selectinload(Issue.status))
        )
        result = await session.execute(stmt)
        issues = list(result.scalars().all())

        total_estimated = Decimal(0)
        completed_hours = Decimal(0)

        for issue in issues:
            hours = issue.estimated_hours or Decimal(0)
            total_estimated += hours
            if issue.status.category in ("done", "closed"):
                completed_hours += hours

        remaining = total_estimated - completed_hours

        # Build data points from start_date to end_date (or today)
        data_points: list[dict] = []
        if sprint.start_date:
            end = sprint.end_date or date.today()
            total_days = (end - sprint.start_date).days
            if total_days <= 0:
                total_days = 1

            current = sprint.start_date
            day_index = 0
            while current <= end:
                ideal = total_estimated - (
                    total_estimated * Decimal(day_index) / Decimal(total_days)
                )
                data_points.append({
                    "date": current,
                    "remaining": remaining,
                    "ideal": ideal,
                })
                current += timedelta(days=1)
                day_index += 1

        return {
            "total_estimated_hours": total_estimated,
            "completed_hours": completed_hours,
            "data_points": data_points,
        }
