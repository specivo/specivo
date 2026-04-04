"""Issue service — create, retrieve, and manage issues."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services.journal_service import _JOURNALIZED_ATTRS, JournalService
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.nested_set_service import MAX_DEPTH, NestedSetService
from specivo.services.watcher_service import WatcherService

logger = logging.getLogger(__name__)

# Allowed sort columns — prevents SQL injection via user-supplied sort params
_ALLOWED_SORT_FIELDS = frozenset(
    {
        "id",
        "subject",
        "status_id",
        "priority_id",
        "tracker_id",
        "assigned_to_id",
        "created_at",
        "updated_at",
        "due_date",
        "done_ratio",
    }
)


class IssueService:
    """Service layer for issue operations."""

    _nested_set = NestedSetService()
    _journal_service = JournalService()
    _watcher_service = WatcherService()
    _metadata_schema_service = MetadataSchemaService()
    _workflow_service: Any = None  # Lazy import to avoid circular dependency

    @property
    def workflow_service(self) -> Any:
        if self._workflow_service is None:
            from specivo.services.workflow_service import WorkflowService

            self._workflow_service = WorkflowService()
        return self._workflow_service

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_default_status(self, session: AsyncSession, tracker_id: int) -> int:
        """Return the tracker's default_status_id, or the first status by position."""
        result = await session.execute(select(Tracker).where(Tracker.id == tracker_id))
        tracker = result.scalar_one_or_none()
        if tracker is None:
            raise NotFoundError(f"Tracker {tracker_id} not found")
        if tracker.default_status_id is not None:
            return tracker.default_status_id
        # Fall back to first status ordered by position
        fallback = await session.execute(select(IssueStatus).order_by(IssueStatus.position).limit(1))
        status = fallback.scalar_one_or_none()
        if status is None:
            raise AppError(
                code="validation_error",
                message=_("No issue statuses found; run seed data first"),
                status_code=422,
            )
        return status.id

    async def _resolve_default_priority(self, session: AsyncSession) -> int:
        """Return the is_default=True priority, or the first active one."""
        result = await session.execute(
            select(IssuePriority).where(IssuePriority.is_default.is_(True), IssuePriority.active.is_(True)).limit(1)
        )
        priority = result.scalar_one_or_none()
        if priority is not None:
            return priority.id
        # Fall back to first active priority by position
        fallback = await session.execute(
            select(IssuePriority).where(IssuePriority.active.is_(True)).order_by(IssuePriority.position).limit(1)
        )
        priority = fallback.scalar_one_or_none()
        if priority is None:
            raise AppError(
                code="validation_error",
                message=_("No issue priorities found; run seed data first"),
                status_code=422,
            )
        return priority.id

    # ------------------------------------------------------------------
    # Visibility helpers
    # ------------------------------------------------------------------

    async def _get_best_visibility(self, session: AsyncSession, user: User, project_id: int) -> str | None:
        """Return the most permissive issues_visibility across user's roles.

        Returns None when the user is not a member of the project.
        Visibility precedence: "all" > "default" > "own".
        """
        stmt = (
            select(Role.issues_visibility)
            .join(MemberRole, MemberRole.role_id == Role.id)
            .join(Member, Member.id == MemberRole.member_id)
            .where(Member.project_id == project_id, Member.user_id == user.id)
        )
        result = await session.execute(stmt)
        visibilities = [row[0] for row in result.all()]
        if not visibilities:
            return None
        # Most permissive wins
        if "all" in visibilities:
            return "all"
        if "default" in visibilities:
            return "default"
        return "own"

    def _apply_visibility_filter(self, stmt, user: User, visibility: str | None, is_public: bool):
        """Add WHERE clauses to filter issues by user's visibility level.

        Admin: sees everything (caller should skip this method).
        Role visibility "all": non-private + private where author/assignee.
        Role visibility "default": non-private + own (author/assignee).
        Role visibility "own": only own (author/assignee).
        Non-member on public project: sees non-private issues only.
        Non-member on private project: impossible (caller handles with 404).
        """
        if visibility == "all":
            # See everything except private issues not authored/assigned to them
            stmt = stmt.where(
                or_(
                    Issue.is_private.is_(False),
                    Issue.author_id == user.id,
                    Issue.assigned_to_id == user.id,
                )
            )
        elif visibility == "default":
            # Non-private + own (private or not)
            stmt = stmt.where(
                or_(
                    Issue.is_private.is_(False),
                    Issue.author_id == user.id,
                    Issue.assigned_to_id == user.id,
                )
            )
        elif visibility == "own":
            # Only issues where user is author or assignee
            stmt = stmt.where(
                or_(
                    Issue.author_id == user.id,
                    Issue.assigned_to_id == user.id,
                )
            )
        else:
            # Non-member on public project: non-private only
            if is_public:
                stmt = stmt.where(Issue.is_private.is_(False))
            else:
                # Should not reach here — caller should 404 for private projects
                # Use an impossible condition to return no results
                stmt = stmt.where(Issue.id < 0)
        return stmt

    async def _check_visible(self, session: AsyncSession, issue: Issue, user: User) -> bool:
        """Check if a single issue is visible to user.

        Returns False if not visible. Caller should raise NotFoundError.
        """
        if user.is_admin:
            return True

        # Check project access
        project_result = await session.execute(select(Project).where(Project.id == issue.project_id))
        project = project_result.scalar_one_or_none()
        if project is None:
            return False

        visibility = await self._get_best_visibility(session, user, issue.project_id)

        # Non-member on private project: cannot see anything
        if visibility is None and not project.is_public:
            return False

        if visibility == "all":
            # Can see all non-private + private where author/assignee
            if issue.is_private:
                return issue.author_id == user.id or issue.assigned_to_id == user.id
            return True
        elif visibility == "default":
            if issue.is_private:
                return issue.author_id == user.id or issue.assigned_to_id == user.id
            return True
        elif visibility == "own":
            return issue.author_id == user.id or issue.assigned_to_id == user.id
        else:
            # Non-member on public project: can see non-private only
            return not issue.is_private

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project: Project,
        data: IssueCreate,
        author: User,
    ) -> Issue:
        """Create an issue with an atomic per-project sequence number.

        Sequence assignment uses UPDATE … RETURNING so the increment and
        read are a single atomic database operation — no race condition
        is possible even under high concurrency.
        """
        # Resolve optional fields to defaults before writing anything
        status_id = data.status_id
        if status_id is None:
            status_id = await self._resolve_default_status(session, data.tracker_id)

        priority_id = data.priority_id
        if priority_id is None:
            priority_id = await self._resolve_default_priority(session)

        # Validate metadata against schemas (if any exist for this project/tracker)
        await self._metadata_schema_service.validate_metadata(session, project.id, data.tracker_id, data.metadata)

        # Atomic sequence increment — guarantees no duplicate sequence_numbers
        # for this project under concurrent inserts.
        result = await session.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(issue_sequence=Project.issue_sequence + 1)
            .returning(Project.issue_sequence, Project.key)
        )
        seq, key = result.one()

        # Resolve parent if provided
        parent: Issue | None = None
        if data.parent_id is not None:
            parent_result = await session.execute(select(Issue).where(Issue.id == data.parent_id))
            parent = parent_result.scalar_one_or_none()
            if parent is None:
                raise NotFoundError(f"Parent issue {data.parent_id} not found")
            # Validate parent before creating the child
            # (issue not yet created — depth check only against parent's ancestors)
            parent_ancestors = await self._nested_set.get_ancestors(session, parent)
            new_depth = len(parent_ancestors) + 1
            if new_depth >= MAX_DEPTH:
                raise ValidationError(
                    message=f"Maximum hierarchy depth of {MAX_DEPTH} would be exceeded.",
                    field="parent_id",
                    details={"max_depth": MAX_DEPTH, "current_depth": new_depth},
                )

        issue = Issue(
            project_id=project.id,
            project_key=key,
            sequence_number=seq,
            tracker_id=data.tracker_id,
            status_id=status_id,
            priority_id=priority_id,
            category_id=data.category_id,
            author_id=author.id,
            assigned_to_id=data.assigned_to_id,
            subject=data.subject,
            description=data.description,
            issue_metadata=data.metadata,
            start_date=data.start_date,
            due_date=data.due_date,
            estimated_hours=data.estimated_hours,
            done_ratio=data.done_ratio,
            is_private=data.is_private,
        )
        session.add(issue)
        await session.flush()  # obtain issue.id

        # Initialize nested set position
        if parent is None:
            await self._nested_set.insert_root(session, issue)
        else:
            await self._nested_set.insert_child(session, parent, issue)
            # Recalculate parent's derived attributes
            await self._nested_set.recalculate_parent_attributes(session, parent)

        # Auto-watch: issue author always watches their own issue
        await self._watcher_service.auto_watch(session, issue, author)

        # Auto-watch: assigned user watches the issue (if assigned at creation)
        if data.assigned_to_id is not None and data.assigned_to_id != author.id:
            assignee_result = await session.execute(select(User).where(User.id == data.assigned_to_id))
            assignee = assignee_result.scalar_one_or_none()
            if assignee is not None:
                await self._watcher_service.auto_watch(session, issue, assignee)

        logger.info(
            "Created issue %s (project=%s, tracker=%d, parent=%s)",
            issue.display_key,
            key,
            data.tracker_id,
            data.parent_id,
        )

        # Generate search embeddings (inline, non-blocking on failure)
        try:
            from specivo.services.chunking_service import ChunkingService
            from specivo.services.embedding_service import EmbeddingService

            chunks = ChunkingService().chunk_issue(issue.subject, issue.description)
            await EmbeddingService().embed_source(session, "issue", issue.id, project.id, chunks)
        except Exception:
            logger.debug("Embedding generation skipped for %s (no model or error)", issue.display_key)

        return issue

    async def get_by_display_key(self, session: AsyncSession, display_key: str, user: User | None = None) -> Issue:
        """Resolve a display key like 'ACME-42' to an Issue.

        Also accepts a bare numeric string (e.g. ``"42"``), which is treated
        as an internal ID lookup for backward compatibility.

        When ``user`` is provided, a visibility check is applied.
        Invisible issues raise ``NotFoundError`` (404, not 403).

        Raises ``NotFoundError`` when no matching issue exists.
        """
        if "-" in display_key:
            # Project key format: ACME-42
            parts = display_key.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    project_key = parts[0].upper()
                    seq = int(parts[1])
                except ValueError:
                    raise NotFoundError(f"Invalid issue reference: {display_key!r}")

                result = await session.execute(
                    select(Issue).where(Issue.project_key == project_key).where(Issue.sequence_number == seq)
                )
                issue = result.scalar_one_or_none()
                if issue is None:
                    raise NotFoundError(f"Issue {display_key!r} not found")
                if user is not None and not await self._check_visible(session, issue, user):
                    raise NotFoundError(f"Issue {display_key!r} not found")
                return issue

        # Bare numeric ID fallback
        try:
            issue_id = int(display_key)
        except ValueError:
            raise NotFoundError(f"Invalid issue reference: {display_key!r}")
        return await self.get_by_id(session, issue_id, user=user)

    async def get_by_id(self, session: AsyncSession, issue_id: int, user: User | None = None) -> Issue:
        """Get an issue by its internal primary key.

        When ``user`` is provided, a visibility check is applied.
        Raises ``NotFoundError`` when the issue does not exist or is not visible.
        """
        result = await session.execute(select(Issue).where(Issue.id == issue_id))
        issue = result.scalar_one_or_none()
        if issue is None:
            raise NotFoundError(f"Issue {issue_id} not found")
        if user is not None and not await self._check_visible(session, issue, user):
            raise NotFoundError(f"Issue {issue_id} not found")
        return issue

    async def get_with_relations(self, session: AsyncSession, issue_id: int, user: User | None = None) -> Issue:
        """Load an issue with all display relationships eagerly loaded.

        When ``user`` is provided, a visibility check is applied.
        Raises ``NotFoundError`` when the issue does not exist or is not visible.
        """
        result = await session.execute(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(
                selectinload(Issue.tracker),
                selectinload(Issue.status),
                selectinload(Issue.priority),
                selectinload(Issue.category),
                selectinload(Issue.author),
                selectinload(Issue.assigned_to),
            )
        )
        issue = result.scalar_one_or_none()
        if issue is None:
            raise NotFoundError(f"Issue {issue_id} not found")
        if user is not None and not await self._check_visible(session, issue, user):
            raise NotFoundError(f"Issue {issue_id} not found")
        return issue

    async def get_by_display_key_with_relations(
        self, session: AsyncSession, display_key: str, user: User | None = None
    ) -> Issue:
        """Resolve a display key or numeric ID, eager-loading all relations.

        When ``user`` is provided, a visibility check is applied.
        Raises ``NotFoundError`` when no matching issue exists or is not visible.
        """
        if "-" in display_key:
            parts = display_key.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    project_key = parts[0].upper()
                    seq = int(parts[1])
                except ValueError:
                    raise NotFoundError(f"Invalid issue reference: {display_key!r}")

                result = await session.execute(
                    select(Issue)
                    .where(Issue.project_key == project_key)
                    .where(Issue.sequence_number == seq)
                    .options(
                        selectinload(Issue.tracker),
                        selectinload(Issue.status),
                        selectinload(Issue.priority),
                        selectinload(Issue.category),
                        selectinload(Issue.author),
                        selectinload(Issue.assigned_to),
                    )
                )
                issue = result.scalar_one_or_none()
                if issue is None:
                    raise NotFoundError(f"Issue {display_key!r} not found")
                if user is not None and not await self._check_visible(session, issue, user):
                    raise NotFoundError(f"Issue {display_key!r} not found")
                return issue

        try:
            issue_id = int(display_key)
        except ValueError:
            raise NotFoundError(f"Invalid issue reference: {display_key!r}")
        return await self.get_with_relations(session, issue_id, user=user)

    async def update(
        self,
        session: AsyncSession,
        issue: Issue,
        data: IssueUpdate,
        user: User,
        notes: str | None = None,
        api_key_id: int | None = None,
    ) -> Issue:
        """Apply a partial update to an issue with optimistic locking.

        ``data.lock_version`` must match the current ``issue.lock_version``
        in the database; otherwise ``ConflictError`` (409) is raised with the
        current version in ``details`` so the client can retry.

        Only fields that are not ``None`` in ``data`` are applied.

        A journal entry is created for every call that changes at least one
        journalized attribute or provides ``notes``.  The journal records
        field-level diffs (full text for description) and attributes the change
        to ``user`` / ``api_key_id``.
        """
        if data.lock_version != issue.lock_version:
            raise ConflictError(
                message=_("Issue was modified by another request. Reload and retry."),
                details={"current_lock_version": issue.lock_version},
            )

        # Workflow validation — before applying changes
        if data.status_id is not None and data.status_id != issue.status_id:
            await self.workflow_service.validate_transition(session, issue, data.status_id, user)
            # Validate field rules for the target status
            await self.workflow_service.validate_field_rules(session, issue, data, user, data.status_id)

        # Snapshot current values BEFORE applying changes
        old_attrs: dict = {attr: getattr(issue, attr) for attr, _ in _JOURNALIZED_ATTRS}

        if data.tracker_id is not None:
            issue.tracker_id = data.tracker_id
        if data.status_id is not None:
            issue.status_id = data.status_id
        if data.priority_id is not None:
            issue.priority_id = data.priority_id
        if data.subject is not None:
            issue.subject = data.subject
        if data.description is not None:
            issue.description = data.description
        if data.assigned_to_id is not None:
            issue.assigned_to_id = data.assigned_to_id
        if data.category_id is not None:
            issue.category_id = data.category_id
        if data.start_date is not None:
            issue.start_date = data.start_date
        if data.due_date is not None:
            issue.due_date = data.due_date
        if data.estimated_hours is not None:
            issue.estimated_hours = data.estimated_hours
        if data.done_ratio is not None:
            issue.done_ratio = data.done_ratio
        if data.is_private is not None:
            issue.is_private = data.is_private
        if data.metadata is not None:
            # Validate against schemas using the effective tracker_id
            # (may have been changed in this same update)
            effective_tracker_id = data.tracker_id if data.tracker_id is not None else issue.tracker_id
            await self._metadata_schema_service.validate_metadata(
                session, issue.project_id, effective_tracker_id, data.metadata
            )
            issue.issue_metadata = data.metadata

        # --- Hierarchy move (parent_id provided) ---
        # Convention: parent_id=0 means "move to root"; parent_id=N means "move to N"
        old_parent_id = issue.parent_id
        if data.parent_id is not None:
            target_parent_id = data.parent_id if data.parent_id != 0 else None
            new_parent: Issue | None = None
            if target_parent_id is not None:
                parent_result = await session.execute(select(Issue).where(Issue.id == target_parent_id))
                new_parent = parent_result.scalar_one_or_none()
                if new_parent is None:
                    raise NotFoundError(f"Parent issue {target_parent_id} not found")
                await self._nested_set.validate_parent(session, issue, new_parent)

            await self._nested_set.move_to_parent(session, issue, new_parent)

            # Recalculate old parent's attributes (it lost a child)
            if old_parent_id is not None:
                old_parent_result = await session.execute(select(Issue).where(Issue.id == old_parent_id))
                old_parent = old_parent_result.scalar_one_or_none()
                if old_parent is not None:
                    await session.refresh(old_parent)
                    await self._nested_set.recalculate_parent_attributes(session, old_parent)

            # Recalculate new parent's attributes (it gained a child)
            if new_parent is not None:
                await session.refresh(new_parent)
                await self._nested_set.recalculate_parent_attributes(session, new_parent)
        elif any(
            field is not None for field in [data.done_ratio, data.start_date, data.due_date, data.estimated_hours]
        ):
            # Scalar changes that affect parent derivation
            await self._nested_set.recalculate_ancestors(session, issue)

        await session.flush()

        # Snapshot new values AFTER applying changes
        new_attrs: dict = {attr: getattr(issue, attr) for attr, _ in _JOURNALIZED_ATTRS}

        # Record journal entry (returns None when nothing changed and no notes)
        await self._journal_service.record_change(
            session=session,
            issue=issue,
            user=user,
            old_attrs=old_attrs,
            new_attrs=new_attrs,
            notes=notes,
            api_key_id=api_key_id,
        )

        # Auto-watch: if assignee changed, add the new assignee as watcher
        if data.assigned_to_id is not None:
            assignee_result = await session.execute(select(User).where(User.id == data.assigned_to_id))
            assignee = assignee_result.scalar_one_or_none()
            if assignee is not None:
                await self._watcher_service.auto_watch(session, issue, assignee)

        logger.info(
            "Updated issue %s by user %d",
            issue.display_key,
            user.id,
        )
        return issue

    async def delete(self, session: AsyncSession, issue: Issue) -> None:
        """Delete an issue permanently.

        When the deleted issue has a parent, recalculate the parent's derived
        attributes after deletion since one child has been removed.
        """
        parent_id = issue.parent_id
        display_key = issue.display_key
        issue_id = issue.id

        await session.delete(issue)
        await session.flush()
        logger.info("Deleted issue id=%d key=%s", issue_id, display_key)

        # Recalculate parent attributes since a child was removed
        if parent_id is not None:
            parent_result = await session.execute(select(Issue).where(Issue.id == parent_id))
            parent = parent_result.scalar_one_or_none()
            if parent is not None:
                await self._nested_set.recalculate_parent_attributes(session, parent)

    async def list_issues(
        self,
        session: AsyncSession,
        project_id: int | None,
        filters: dict,
        sort: str,
        offset: int,
        limit: int,
        user: User | None = None,
    ) -> tuple[list[Issue], int]:
        """List issues with filtering, sorting, and pagination.

        Returns a ``(issues, total_count)`` tuple.

        Parameters
        ----------
        project_id:
            Scope results to a specific project when provided.
        filters:
            Dict matching IssueFilters fields (status, tracker_id, …).
        sort:
            Comma-separated ``field:direction`` pairs, e.g.
            ``"priority_id:desc,updated_at:desc"``.
            Defaults to ``"created_at:desc"``.
        offset, limit:
            Pagination parameters.
        user:
            When provided, visibility filtering is applied based on the
            user's roles and ``issues_visibility`` setting.
        """
        stmt = select(Issue).options(
            selectinload(Issue.tracker),
            selectinload(Issue.status),
            selectinload(Issue.priority),
            selectinload(Issue.category),
            selectinload(Issue.author),
            selectinload(Issue.assigned_to),
        )

        if project_id is not None:
            stmt = stmt.where(Issue.project_id == project_id)

        # ------------------------------------------------------------------
        # Visibility filter
        # ------------------------------------------------------------------
        if user is not None and not user.is_admin and project_id is not None:
            project_result = await session.execute(select(Project).where(Project.id == project_id))
            project = project_result.scalar_one_or_none()
            if project is not None:
                visibility = await self._get_best_visibility(session, user, project_id)
                stmt = self._apply_visibility_filter(stmt, user, visibility, project.is_public)

        # ------------------------------------------------------------------
        # Status filter
        # ------------------------------------------------------------------
        status_filter = filters.get("status", "open")
        if status_filter == "open":
            stmt = stmt.join(IssueStatus, Issue.status_id == IssueStatus.id).where(IssueStatus.is_closed.is_(False))
        elif status_filter == "closed":
            stmt = stmt.join(IssueStatus, Issue.status_id == IssueStatus.id).where(IssueStatus.is_closed.is_(True))
        elif status_filter not in (None, "all"):
            # Treat as numeric status ID
            try:
                sid = int(status_filter)
                stmt = stmt.where(Issue.status_id == sid)
            except (ValueError, TypeError):
                pass  # ignore invalid value; return all

        # ------------------------------------------------------------------
        # Optional FK filters
        # ------------------------------------------------------------------
        if filters.get("tracker_id") is not None:
            stmt = stmt.where(Issue.tracker_id == filters["tracker_id"])
        if filters.get("assigned_to_id") is not None:
            stmt = stmt.where(Issue.assigned_to_id == filters["assigned_to_id"])
        if filters.get("priority_id") is not None:
            stmt = stmt.where(Issue.priority_id == filters["priority_id"])
        if filters.get("category_id") is not None:
            stmt = stmt.where(Issue.category_id == filters["category_id"])
        if filters.get("author_id") is not None:
            stmt = stmt.where(Issue.author_id == filters["author_id"])

        # ------------------------------------------------------------------
        # Text search
        # ------------------------------------------------------------------
        if filters.get("subject_contains"):
            escaped = filters["subject_contains"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(Issue.subject.ilike(f"%{escaped}%", escape="\\"))

        # ------------------------------------------------------------------
        # Date range filters
        # ------------------------------------------------------------------
        if filters.get("created_after") is not None:
            stmt = stmt.where(Issue.created_at >= filters["created_after"])
        if filters.get("created_before") is not None:
            stmt = stmt.where(Issue.created_at <= filters["created_before"])
        if filters.get("updated_after") is not None:
            stmt = stmt.where(Issue.updated_at >= filters["updated_after"])
        if filters.get("updated_before") is not None:
            stmt = stmt.where(Issue.updated_at <= filters["updated_before"])

        # ------------------------------------------------------------------
        # Total count (before pagination)
        # ------------------------------------------------------------------
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await session.execute(count_stmt)
        total_count = total_result.scalar_one()

        # ------------------------------------------------------------------
        # Sorting
        # ------------------------------------------------------------------
        if not sort:
            sort = "created_at:desc"

        order_clauses = []
        for part in sort.split(","):
            part = part.strip()
            if ":" in part:
                field, direction = part.rsplit(":", 1)
            else:
                field, direction = part, "asc"

            field = field.strip().lower()
            direction = direction.strip().lower()

            if field not in _ALLOWED_SORT_FIELDS:
                continue

            col = getattr(Issue, field, None)
            if col is None:
                continue

            if direction == "desc":
                order_clauses.append(col.desc())
            else:
                order_clauses.append(col.asc())

        if not order_clauses:
            order_clauses = [Issue.created_at.desc()]

        stmt = stmt.order_by(*order_clauses).offset(offset).limit(limit)

        result = await session.execute(stmt)
        issues = list(result.scalars().all())

        return issues, total_count

    async def get_detail_tab_context(
        self,
        session: AsyncSession,
        issue_id: int,
    ) -> dict[str, Any]:
        """Load tab-related data for the issue detail page.

        Returns counts (time entries, attachments, relations),
        time logged sum, time entries list, activities list, and attachments list.
        """
        from specivo.models.attachment import Attachment
        from specivo.models.relation import IssueRelation
        from specivo.models.time_entry import TimeEntry, TimeEntryActivity

        # Tab counts
        time_entry_count = (
            await session.execute(select(func.count()).select_from(TimeEntry).where(TimeEntry.issue_id == issue_id))
        ).scalar_one()

        attachment_count = (
            await session.execute(
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.container_type == "Issue", Attachment.container_id == issue_id)
            )
        ).scalar_one()

        relation_count = (
            await session.execute(
                select(func.count())
                .select_from(IssueRelation)
                .where(or_(IssueRelation.issue_from_id == issue_id, IssueRelation.issue_to_id == issue_id))
            )
        ).scalar_one()

        # Time logged sum
        time_logged = (
            await session.execute(
                select(func.coalesce(func.sum(TimeEntry.hours), 0)).where(TimeEntry.issue_id == issue_id)
            )
        ).scalar_one()

        # Time entries
        time_entries = list(
            (
                await session.execute(
                    select(TimeEntry)
                    .where(TimeEntry.issue_id == issue_id)
                    .options(selectinload(TimeEntry.user), selectinload(TimeEntry.activity))
                    .order_by(TimeEntry.spent_on.desc(), TimeEntry.id.desc())
                )
            )
            .scalars()
            .all()
        )

        # Activities for log-time dropdown
        activities = list(
            (
                await session.execute(
                    select(TimeEntryActivity)
                    .where(TimeEntryActivity.active.is_(True))
                    .order_by(TimeEntryActivity.position)
                )
            )
            .scalars()
            .all()
        )

        # Attachments
        attachments = list(
            (
                await session.execute(
                    select(Attachment)
                    .where(Attachment.container_type == "Issue", Attachment.container_id == issue_id)
                    .options(selectinload(Attachment.author))
                    .order_by(Attachment.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        return {
            "time_entry_count": time_entry_count,
            "attachment_count": attachment_count,
            "relation_count": relation_count,
            "time_logged": time_logged,
            "time_entries": time_entries,
            "activities": activities,
            "attachments": attachments,
        }

    async def autocomplete(
        self,
        session: AsyncSession,
        query: str,
        user: User,
        limit: int = 10,
    ) -> list[dict]:
        """Search issues by key or subject for autocomplete.

        Only returns issues in projects the user has access to.
        """
        q = query.strip()
        if not q:
            return []

        # Escape LIKE wildcards in user input
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        # display_key is a Python property (project_key + '-' + sequence_number),
        # so we construct it as a SQL expression for filtering and selection.
        display_key_expr = func.concat(Issue.project_key, "-", Issue.sequence_number)

        # Build base query
        stmt = (
            select(
                display_key_expr.label("display_key"),
                Issue.subject,
                IssueStatus.name.label("status_name"),
            )
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                or_(
                    display_key_expr.ilike(f"%{escaped}%", escape="\\"),
                    Issue.subject.ilike(f"%{escaped}%", escape="\\"),
                )
            )
            .order_by(Issue.updated_at.desc())
            .limit(limit)
        )

        # Filter by accessible projects if not admin
        if not user.is_admin:
            from sqlalchemy import and_

            accessible_project_ids = (select(Member.project_id).where(Member.user_id == user.id)).scalar_subquery()
            stmt = stmt.where(
                or_(
                    Issue.project_id.in_(accessible_project_ids),
                    and_(
                        Issue.project_id.in_(select(Project.id).where(Project.is_public.is_(True))),
                        Issue.is_private.is_(False),
                    ),
                )
            )

            # Hide private issues the user doesn't own/isn't assigned to
            stmt = stmt.where(
                or_(
                    Issue.is_private.is_(False),
                    Issue.author_id == user.id,
                    Issue.assigned_to_id == user.id,
                )
            )

        rows = (await session.execute(stmt)).all()
        return [{"key": row.display_key, "subject": row.subject, "status": row.status_name} for row in rows]
