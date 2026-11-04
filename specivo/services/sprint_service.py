"""Sprint service — CRUD, lifecycle, board, backlog, velocity, and burndown."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from specivo.core.exceptions import AppError, ConflictError, NotFoundError
from specivo.core.i18n import gettext as _
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.schemas.sprint import SprintCreate, SprintUpdate
from specivo.services.api_key_scope import project_in_scope
from specivo.services.journal_service import JournalService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.webhook_events import emit_issue_updated

if TYPE_CHECKING:
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.models.user import User

logger = logging.getLogger(__name__)

_journal = JournalService()
_audit = SecurityAuditService()


async def _visibility(session: AsyncSession, user: User) -> ColumnElement[bool] | None:
    """Return the predicate selecting the issues *user* may see (``IssueService.visible_issues_clause``)."""
    from specivo.services.issue_service import IssueService

    return await IssueService().visible_issues_clause(session, user)


async def _visible_to(session: AsyncSession, user: User, stmt: Select[Any]) -> Select[Any]:
    """Narrow an issue query *stmt* to the issues *user* may see."""
    visibility = await _visibility(session, user)
    return stmt if visibility is None else stmt.where(visibility)


def count_done(board: dict[str, list[Issue]]) -> int:
    """Return how many issues of a ``board_data`` board are done.

    Done means a status in the done or closed category (``IssueStatus.is_done``),
    whatever the status is called; the board's status-name keys are labels only.
    """
    return sum(1 for issues in board.values() for issue in issues if issue.status.is_done)


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

    async def search_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        query: str | None = None,
        limit: int = 20,
    ) -> list[Sprint]:
        """Search the sprints of a project an issue may be put in, for the issue sidebar picker.

        Completed sprints are left out: they take no new issues
        (``IssueService.validate_sprint``). The picker still shows the sprint
        an issue is already in, from the page. Read views use
        :meth:`list_for_project`, which keeps every status.

        - Empty query returns the 10 most recent active or planned sprints
          ordered by ``start_date DESC NULLS LAST, name DESC``.
        - Non-empty query performs a case-insensitive substring match on name,
          capped at ``limit`` rows.
        """
        stmt = select(Sprint).where(Sprint.project_id == project_id, Sprint.status != "completed")
        q = (query or "").strip()
        if q:
            stmt = stmt.where(Sprint.name.ilike(f"%{q}%"))
            stmt = stmt.order_by(
                Sprint.start_date.desc().nullslast(),
                Sprint.name.desc(),
            ).limit(limit)
        else:
            stmt = stmt.order_by(
                Sprint.start_date.desc().nullslast(),
                Sprint.name.desc(),
            ).limit(10)
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
        actor: User,
        api_key_id: int | None = None,
    ) -> Sprint:
        """Transition a planned sprint to active.

        Raises ConflictError if:
        - Sprint is not in ``planned`` status
        - Another sprint in the same project is already active

        Each issue in the sprint gets a "sprint started" history entry by
        *actor*, and the start is audited (``SPRINT_STARTED``). Nothing is
        written when the start is refused.
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

        issues = await self._issues_in(session, sprint)
        for issue in issues:
            await _journal.record_sprint_event(session, issue, actor, sprint, "started", api_key_id=api_key_id)

        await _audit.log_event(
            session,
            AuditEvent.SPRINT_STARTED,
            user_id=actor.id,
            resource_type="sprint",
            resource_id=sprint.id,
            project_id=sprint.project_id,
            details={"name": sprint.name, "issue_count": len(issues)},
        )
        await session.flush()
        await session.refresh(sprint)
        return sprint

    async def complete_sprint(
        self,
        session: AsyncSession,
        sprint: Sprint,
        actor: User,
        move_to_sprint_id: int | None = None,
        api_key_id: int | None = None,
    ) -> Sprint:
        """Transition an active sprint to completed.

        - Queries all issues in this sprint
        - Completed issues (status category in done/closed) stay
        - Incomplete issues are moved to *move_to_sprint_id* or backlog (NULL)
        - Builds velocity_snapshot with total_issues and completed_issues
        - Sets end_date to today if None

        *move_to_sprint_id* must be another active or planned sprint of the
        same project (``IssueService.validate_sprint``); anything else is
        refused with a 422 before anything changes. An issue edited while this
        runs raises ``ConflictError`` and nothing is saved.

        Each issue that stays gets a "sprint completed" history entry by
        *actor*; each moved issue gets a normal ``sprint_id`` change, set
        through the ORM so its ``lock_version`` and ``updated_at`` advance, and
        emits ``issue.updated`` as any sprint change does. The completion is
        audited (``SPRINT_COMPLETED``). No notification is sent.
        """
        if sprint.status != "active":
            raise ConflictError("Only active sprints can be completed")

        if move_to_sprint_id is not None:
            if move_to_sprint_id == sprint.id:
                raise AppError(
                    code="validation_error",
                    message=_("Cannot move issues to the sprint being completed"),
                    status_code=422,
                )
            from specivo.services.issue_service import IssueService

            await IssueService().validate_sprint(session, move_to_sprint_id, sprint.project_id)

        issues = await self._issues_in(session, sprint)
        stay = [issue for issue in issues if issue.status.is_done]
        moved = [issue for issue in issues if not issue.status.is_done]

        # The ids let each reader be shown only the issues they may see
        # (``velocity_for``); the counts are what was committed overall.
        sprint.velocity_snapshot = {
            "total_issues": len(issues),
            "completed_issues": len(stay),
            "issue_ids": sorted(issue.id for issue in issues),
            "completed_issue_ids": sorted(issue.id for issue in stay),
        }
        sprint.status = "completed"
        if sprint.end_date is None:
            sprint.end_date = date.today()
        session.add(sprint)

        # Issues in id order: each journal takes the issue's advisory lock
        # until commit, so a fixed order keeps two writers from deadlocking.
        for issue in issues:
            if issue.status.is_done:
                await _journal.record_sprint_event(session, issue, actor, sprint, "completed", api_key_id=api_key_id)
                continue
            was_private = issue.is_private
            issue.sprint_id = move_to_sprint_id
            try:
                await session.flush()
            except StaleDataError as exc:
                # Someone edited the issue after it was loaded above. The whole
                # completion rolls back, so it can simply be run again.
                raise ConflictError(_("Issue was modified by another request. Reload and retry.")) from exc
            journal = await _journal.record_change(
                session,
                issue,
                actor,
                {"sprint_id": sprint.id},
                {"sprint_id": move_to_sprint_id},
                api_key_id=api_key_id,
            )
            if journal is not None:
                await emit_issue_updated(session, issue, journal, actor, was_private=was_private)

        await _audit.log_event(
            session,
            AuditEvent.SPRINT_COMPLETED,
            user_id=actor.id,
            resource_type="sprint",
            resource_id=sprint.id,
            project_id=sprint.project_id,
            details={
                "name": sprint.name,
                "issue_count": len(issues),
                "completed_count": len(stay),
                "moved_count": len(moved),
                "move_to_sprint_id": move_to_sprint_id,
            },
        )
        await session.flush()
        await session.refresh(sprint)
        return sprint

    async def _issues_in(self, session: AsyncSession, sprint: Sprint) -> list[Issue]:
        """Every issue in *sprint*, whoever may see it, in id order, with its status loaded."""
        stmt = (
            select(Issue)
            .where(Issue.sprint_id == sprint.id)
            .options(selectinload(Issue.status))
            .order_by(Issue.id.asc())
        )
        return list((await session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    # Board & backlog
    # ------------------------------------------------------------------

    async def board_data(
        self,
        session: AsyncSession,
        sprint: Sprint,
        user: User,
    ) -> dict[str, list[Issue]]:
        """Return the issues in the sprint that *user* may see, grouped by status name."""
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
        stmt = await _visible_to(session, user, stmt)
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
        user: User,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[Issue], int]:
        """Return the issues of the project with sprint_id IS NULL that *user* may see.

        Returns (items, total_count) for pagination; the count covers the
        visible issues only.
        """
        base_where = [Issue.project_id == project_id, Issue.sprint_id.is_(None)]
        visibility = await _visibility(session, user)
        if visibility is not None:
            base_where.append(visibility)

        count_result = await session.execute(select(func.count(Issue.id)).where(*base_where))
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
        user: User,
    ) -> int:
        """Return the number of issues assigned to a sprint that *user* may see."""
        stmt = await _visible_to(session, user, select(func.count(Issue.id)).where(Issue.sprint_id == sprint_id))
        count: int = (await session.execute(stmt)).scalar_one()
        return count

    # ------------------------------------------------------------------
    # Velocity & burndown
    # ------------------------------------------------------------------

    async def velocity_for(
        self,
        session: AsyncSession,
        sprints: list[Sprint],
        reader: User,
    ) -> dict[int, dict[str, int] | None]:
        """Return each sprint's velocity snapshot as *reader* may see it, keyed by sprint id.

        A value is ``{"total_issues": n, "completed_issues": m}``, or None for
        a sprint without a snapshot or whose snapshot *reader* may not see.

        A snapshot is taken when a sprint is completed, because its unfinished
        issues then leave the sprint and could not be counted later. It keeps
        the ids of the issues it counted, and a reader is shown only those
        they may see now (``IssueService.visible_issues_clause``), so the
        numbers follow each reader's visibility, including later changes to
        an issue's privacy, and never reveal hidden issues. A reader who is
        not filtered at all (an administrator) gets the stored counts.

        A snapshot taken before the ids were kept holds bare counts that may
        include hidden issues. It is shown only to a reader who sees every
        issue in the sprint's project today, and is None for anyone else.

        One query for the tracked snapshots, one for the bare ones.
        """
        visibility = await _visibility(session, reader)
        result: dict[int, dict[str, int] | None] = {s.id: None for s in sprints}
        tracked: list[Sprint] = []
        bare: list[Sprint] = []
        for sprint in sprints:
            snap = sprint.velocity_snapshot
            if not snap:
                continue
            if visibility is None:
                result[sprint.id] = {
                    "total_issues": int(snap.get("total_issues", 0)),
                    "completed_issues": int(snap.get("completed_issues", 0)),
                }
            elif "issue_ids" in snap:
                tracked.append(sprint)
            else:
                bare.append(sprint)

        if tracked:
            wanted = {i for s in tracked for i in s.velocity_snapshot["issue_ids"]}  # type: ignore[index]
            visible = set()
            if wanted:
                stmt = select(Issue.id).where(Issue.id.in_(sorted(wanted)))
                if visibility is not None:
                    stmt = stmt.where(visibility)
                visible = set((await session.execute(stmt)).scalars().all())
            for sprint in tracked:
                snap = sprint.velocity_snapshot or {}
                result[sprint.id] = {
                    "total_issues": len(visible.intersection(snap.get("issue_ids", []))),
                    "completed_issues": len(visible.intersection(snap.get("completed_issue_ids", []))),
                }

        if bare and visibility is not None:
            project_ids = sorted({s.project_id for s in bare})
            seen_stmt = (
                select(Issue.project_id, func.count(), func.count().filter(visibility))
                .where(Issue.project_id.in_(project_ids))
                .group_by(Issue.project_id)
            )
            counts = {row[0]: (row[1], row[2]) for row in (await session.execute(seen_stmt)).all()}
            for sprint in bare:
                total, seen = counts.get(sprint.project_id, (0, 0))
                if total == seen and project_in_scope(sprint.project_id):
                    snap = sprint.velocity_snapshot or {}
                    result[sprint.id] = {
                        "total_issues": int(snap.get("total_issues", 0)),
                        "completed_issues": int(snap.get("completed_issues", 0)),
                    }
        return result

    async def average_velocity(
        self,
        session: AsyncSession,
        project_id: int,
        reader: User,
    ) -> Decimal:
        """Return the average completed-issue count across the completed sprints *reader* may see.

        Uses :meth:`velocity_for`, so each sprint counts only the issues
        *reader* may see; a sprint whose snapshot is hidden from *reader* is
        left out. Returns ``Decimal(0)`` when there is none.
        """
        stmt = select(Sprint).where(
            Sprint.project_id == project_id,
            Sprint.status == "completed",
        )
        result = await session.execute(stmt)
        sprints = list(result.scalars().all())

        shown = [v for v in (await self.velocity_for(session, sprints, reader)).values() if v is not None]
        if not shown:
            return Decimal(0)
        return Decimal(sum(v["completed_issues"] for v in shown)) / Decimal(len(shown))

    async def burndown_data(
        self,
        session: AsyncSession,
        sprint: Sprint,
        reader: User,
    ) -> dict:
        """Build burndown chart data for *sprint* over the issues *reader* may see.

        Returns a dict with:
        - ``total_estimated_hours`` — sum of estimated_hours for the visible sprint issues
        - ``completed_hours`` — sum of estimated_hours for the visible done/closed ones
        - ``data_points`` — list of dicts with ``date``, ``remaining``, ``ideal``

        Hidden issues (private ones, or every issue for a key limited to other
        projects) are left out of both sums, so the chart cannot reveal them.
        """
        from specivo.models.lookups import IssueStatus

        hours = func.coalesce(Issue.estimated_hours, 0)
        stmt = (
            select(
                func.coalesce(func.sum(hours), 0),
                func.coalesce(func.sum(hours).filter(IssueStatus.category.in_(("done", "closed"))), 0),
            )
            .join(IssueStatus, IssueStatus.id == Issue.status_id)
            .where(Issue.sprint_id == sprint.id)
        )
        visibility = await _visibility(session, reader)
        if visibility is not None:
            stmt = stmt.where(visibility)
        total_row, completed_row = (await session.execute(stmt)).one()
        total_estimated = Decimal(total_row)
        completed_hours = Decimal(completed_row)

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
                ideal = total_estimated - (total_estimated * Decimal(day_index) / Decimal(total_days))
                data_points.append(
                    {
                        "date": current,
                        "remaining": remaining,
                        "ideal": ideal,
                    }
                )
                current += timedelta(days=1)
                day_index += 1

        return {
            "total_estimated_hours": total_estimated,
            "completed_hours": completed_hours,
            "data_points": data_points,
        }
