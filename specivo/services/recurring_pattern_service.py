"""RecurringPatternService — CRUD, occurrence materialisation, and edit-scope.

This is the stateful counterpart to the pure ``recurrence`` engine: it adapts an
ORM :class:`~specivo.models.recurring_pattern.RecurringPattern` into the engine's
frozen :class:`~specivo.services.recurrence.RecurrenceSpec`, then drives issue
generation through the existing :class:`~specivo.services.issue_service.IssueService`
so generated issues are first-class (sequence numbers, watchers, journal, search
embeddings — all for free, no duplicate logic).

Key design decisions (also documented inline where they bite):

Look-ahead window
    Generation never expands an infinite series. The window upper bound is
    ``now + creation_lead_time_days``. The lower bound depends on anchor mode
    (see :meth:`materialize`).

Idempotency
    Each generated issue carries ``(recurring_pattern_id, original_occurrence_at)``.
    A partial unique index on the issues table makes a duplicate insert impossible;
    we additionally diff against already-materialised occurrences before inserting
    so a re-run is a cheap no-op rather than a caught IntegrityError.

Date offsets (start_date / due_date)
    Each occurrence is a UTC instant; issue start/due are calendar dates. We take
    the occurrence's **local date** (in the pattern timezone) as the anchor.

    - ``due_offset_days`` is None  -> due_date = the occurrence date itself
      (the most intuitive default: "the thing is due on its occurrence day").
    - ``due_offset_days`` is an int -> due_date = occurrence date + offset.
    - ``start_offset_days`` is None -> start_date is left unset (None).
    - ``start_offset_days`` is an int -> start_date = occurrence date + offset.

    Rationale: a due date is the natural meaning of a recurring task's
    occurrence, so we default it on; a start date is optional planning detail,
    so we leave it off unless an offset is configured.

Carry-over / reset
    A generated instance is always born *fresh*: ``done_ratio=0``,
    ``closed_on=None``, and a non-terminal status (template status or tracker
    default — never a closed status). ``carry_over`` toggles whether the template
    values for description / assignee / metadata / estimated_hours are applied
    (True, the default behaviour) or dropped to defaults (False). For 0.2.0 the
    template *is* the carry-over source, so these toggles govern template
    application; cross-instance relation/attachment cloning is OUT OF SCOPE
    (relations/attachments are treated as no-ops — see TODOs).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.exceptions import ConflictError, NotFoundError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.recurrence_exception import RecurrenceException
from specivo.models.recurring_pattern import RecurringPattern
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.schemas.recurring_pattern import RecurringPatternCreate, RecurringPatternUpdate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.recurrence import (
    RecurrenceSpec,
    expand_macros,
    expand_occurrences,
    next_assignee,
)

logger = logging.getLogger(__name__)

# A short validation window used purely to surface incoherent recurrence rules
# at create/update time (the engine raises ValueError on a bad spec).
_VALIDATION_WINDOW_DAYS = 1


class RecurringPatternService:
    """Service layer for recurring patterns and their generated issues."""

    def __init__(self) -> None:
        # Composed sub-services, mirroring how IssueService composes its own.
        self._issue_service = IssueService()
        self._journal_service = JournalService()
        self._metadata_schema_service = MetadataSchemaService()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project: Project,
        data: RecurringPatternCreate,
        author: User,
    ) -> RecurringPattern:
        """Create a recurring pattern after validating its rule and metadata.

        - ``template_metadata`` is validated against the project/tracker schemas.
        - The recurrence rule is validated for coherence (engine ValueError ->
          ValidationError).
        - ``assignee_rotation.user_ids`` is filtered to actual project members.
        """
        await self._metadata_schema_service.validate_metadata(
            session, project.id, data.template_tracker_id, data.template_metadata
        )
        dtstart = self._anchor_dtstart(data.dtstart, data.timezone)
        self._validate_rule(
            freq=data.freq,
            dtstart=dtstart,
            timezone=data.timezone,
            interval=data.rrule_interval,
            byday=data.byday,
            bymonthday=data.bymonthday,
            bymonth=data.bymonth,
            bysetpos=data.bysetpos,
            count=data.rrule_count,
            until=data.until,
            working_day_adjustment=data.working_day_adjustment,
            working_days=data.working_days,
            holiday_calendar=data.holiday_calendar,
            rrule_raw=data.rrule_raw,
        )

        rotation = await self._filter_rotation(session, project.id, data.assignee_rotation)

        pattern = RecurringPattern(
            project_id=project.id,
            author_id=author.id,
            name=data.name,
            enabled=data.enabled,
            template_tracker_id=data.template_tracker_id,
            template_status_id=data.template_status_id,
            template_priority_id=data.template_priority_id,
            template_category_id=data.template_category_id,
            template_assigned_to_id=data.template_assigned_to_id,
            template_fixed_version_id=data.template_fixed_version_id,
            template_sprint_id=data.template_sprint_id,
            template_subject=data.template_subject,
            template_description=data.template_description,
            template_estimated_hours=data.template_estimated_hours,
            template_metadata=data.template_metadata,
            is_private=data.is_private,
            freq=data.freq,
            rrule_interval=data.rrule_interval,
            byday=data.byday,
            bymonthday=data.bymonthday,
            bymonth=data.bymonth,
            bysetpos=data.bysetpos,
            rrule_count=data.rrule_count,
            until=data.until,
            rrule_raw=data.rrule_raw,
            anchor_mode=data.anchor_mode,
            base_date_strategy=data.base_date_strategy,
            dtstart=dtstart,
            timezone=data.timezone,
            working_day_adjustment=data.working_day_adjustment,
            working_days=data.working_days,
            holiday_calendar=data.holiday_calendar,
            creation_lead_time_days=data.creation_lead_time_days,
            carry_over=data.carry_over,
            reset_checklist=data.reset_checklist,
            assignee_rotation=rotation,
            rotation_index=0,
            start_offset_days=data.start_offset_days,
            due_offset_days=data.due_offset_days,
        )
        session.add(pattern)
        await session.flush()
        logger.info("Created recurring pattern %d (%s) for project %d", pattern.id, pattern.name, project.id)
        return pattern

    async def get_by_id(self, session: AsyncSession, pattern_id: int) -> RecurringPattern:
        """Return a pattern by PK; raises NotFoundError if missing."""
        result = await session.execute(
            select(RecurringPattern).where(RecurringPattern.id == pattern_id)
        )
        pattern = result.scalar_one_or_none()
        if pattern is None:
            raise NotFoundError(f"Recurring pattern {pattern_id} not found")
        return pattern

    async def list_for_project(
        self, session: AsyncSession, project_id: int
    ) -> list[RecurringPattern]:
        """List patterns for a project, newest first."""
        result = await session.execute(
            select(RecurringPattern)
            .where(RecurringPattern.project_id == project_id)
            .order_by(RecurringPattern.id.desc())
        )
        return list(result.scalars().all())

    async def update(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        data: RecurringPatternUpdate,
    ) -> RecurringPattern:
        """Apply a partial update to the pattern's template / rule.

        IMPORTANT — "edit affects future only": a plain update mutates the
        template and recurrence rule, but it does NOT rewrite issues that have
        already been materialised (open or otherwise). Already-generated
        instances keep the values they were born with; only occurrences not yet
        materialised pick up the new template. To change an in-flight series'
        going-forward behaviour while preserving history, use
        :meth:`split_from` (this-and-future). To touch a single occurrence, use
        :meth:`override_occurrence`.

        Optimistic locking: ``data.lock_version`` must match the pattern's
        current ``lock_version`` or a :class:`ConflictError` (409) is raised —
        mirroring :meth:`IssueService.update`. This makes concurrent edits
        safe: the second writer sees a clear conflict instead of clobbering the
        first writer's change.
        """
        if data.lock_version != pattern.lock_version:
            raise ConflictError(
                message=_(
                    "This pattern was changed by someone else. "
                    "Reload and try again."
                ),
                details={"current_lock_version": pattern.lock_version},
            )

        # ``lock_version`` is the optimistic-locking column managed by
        # SQLAlchemy's ``version_id_col``; it is never a writable field.
        updates = data.model_dump(exclude_unset=True, exclude={"lock_version"})

        # Validate metadata if the template metadata or tracker changed.
        if "template_metadata" in updates or "template_tracker_id" in updates:
            tracker_id = updates.get("template_tracker_id", pattern.template_tracker_id)
            metadata = updates.get("template_metadata", pattern.template_metadata)
            await self._metadata_schema_service.validate_metadata(
                session, pattern.project_id, tracker_id, metadata
            )

        # Anchor a newly-supplied naive dtstart to the effective timezone (the
        # new one if it is also changing, otherwise the pattern's current one).
        if updates.get("dtstart") is not None:
            effective_tz = updates.get("timezone", pattern.timezone)
            updates["dtstart"] = self._anchor_dtstart(updates["dtstart"], effective_tz)

        # Re-validate the recurrence rule against the merged state.
        self._validate_rule(
            freq=updates.get("freq", pattern.freq),
            dtstart=updates.get("dtstart", pattern.dtstart),
            timezone=updates.get("timezone", pattern.timezone),
            interval=updates.get("rrule_interval", pattern.rrule_interval),
            byday=updates.get("byday", pattern.byday),
            bymonthday=updates.get("bymonthday", pattern.bymonthday),
            bymonth=updates.get("bymonth", pattern.bymonth),
            bysetpos=updates.get("bysetpos", pattern.bysetpos),
            count=updates.get("rrule_count", pattern.rrule_count),
            until=updates.get("until", pattern.until),
            working_day_adjustment=updates.get("working_day_adjustment", pattern.working_day_adjustment),
            working_days=updates.get("working_days", pattern.working_days),
            holiday_calendar=updates.get("holiday_calendar", pattern.holiday_calendar),
            rrule_raw=updates.get("rrule_raw", pattern.rrule_raw),
        )

        # Filter the rotation roster to project members if it changed.
        if "assignee_rotation" in updates:
            updates["assignee_rotation"] = await self._filter_rotation(
                session, pattern.project_id, updates["assignee_rotation"]
            )

        for field, value in updates.items():
            setattr(pattern, field, value)
        session.add(pattern)
        await session.flush()
        return pattern

    async def delete(self, session: AsyncSession, pattern: RecurringPattern) -> None:
        """Delete a pattern.

        Generated issues survive (``recurring_pattern_id`` is SET NULL on the
        issues FK); the pattern's RecurrenceExceptions cascade-delete.
        """
        await session.delete(pattern)
        await session.flush()

    # ------------------------------------------------------------------
    # Spec adapter
    # ------------------------------------------------------------------

    def build_spec(self, pattern: RecurringPattern) -> RecurrenceSpec:
        """Adapt an ORM pattern to the engine's frozen RecurrenceSpec.

        This is the ``from_pattern`` adapter the DB-free engine deliberately
        omitted: it maps ORM column names (``rrule_interval`` / ``rrule_count``)
        onto the spec's ``interval`` / ``count`` and copies the BY* parts.
        """
        return RecurrenceSpec(
            freq=pattern.freq,
            dtstart=pattern.dtstart,
            timezone=pattern.timezone,
            interval=pattern.rrule_interval,
            byday=list(pattern.byday) if pattern.byday else None,
            bymonthday=list(pattern.bymonthday) if pattern.bymonthday else None,
            bymonth=list(pattern.bymonth) if pattern.bymonth else None,
            bysetpos=list(pattern.bysetpos) if pattern.bysetpos else None,
            count=pattern.rrule_count,
            until=pattern.until,
            working_day_adjustment=pattern.working_day_adjustment,
            working_days=list(pattern.working_days),
            holiday_calendar=list(pattern.holiday_calendar) if pattern.holiday_calendar else None,
            rrule_raw=pattern.rrule_raw,
        )

    # ------------------------------------------------------------------
    # Materialisation — the heart of generation
    # ------------------------------------------------------------------

    async def materialize(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        now: datetime,
        *,
        locale: str = "en",
    ) -> list[Issue]:
        """Generate any due issues for *pattern* up to the look-ahead horizon.

        Returns the list of newly created issues (empty if nothing was due).

        Fixed mode generates every in-window occurrence that has no issue yet
        (so overdue occurrences stack / catch up). Flexible mode generates at
        most one issue per run: the next occurrence, and only once the previous
        instance is closed.

        ``locale`` is the workspace language used to localize ``{{month}}`` /
        ``{{weekday}}`` template macros on generated issues.
        """
        if now.tzinfo is None:
            raise ValidationError(message="now must be timezone-aware")

        project = await self._resolve_project(session, pattern.project_id)
        author = await self._resolve_author(session, pattern.author_id)

        horizon = now + timedelta(days=pattern.creation_lead_time_days)

        # Load this pattern's exceptions once: EXDATEs (skip) and override map.
        exdates, overrides = await self._load_exceptions(session, pattern.id)

        created: list[Issue] = []
        if pattern.anchor_mode == "fixed":
            created = await self._materialize_fixed(
                session, pattern, project, author, now, horizon, exdates, overrides, locale=locale
            )
        else:
            created = await self._materialize_flexible(
                session, pattern, project, author, now, horizon, exdates, overrides, locale=locale
            )

        # Bookkeeping (observability only — never used for dedupe).
        pattern.last_run_at = now
        if created:
            max_occ = max(
                (i.original_occurrence_at for i in created if i.original_occurrence_at is not None),
                default=None,
            )
            if max_occ is not None and (
                pattern.last_generated_occurrence_at is None
                or max_occ > pattern.last_generated_occurrence_at
            ):
                pattern.last_generated_occurrence_at = max_occ
        session.add(pattern)
        await session.flush()
        return created

    async def _materialize_fixed(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        project: Project,
        author: User,
        now: datetime,
        horizon: datetime,
        exdates: set[datetime],
        overrides: dict[datetime, RecurrenceException],
        *,
        locale: str = "en",
    ) -> list[Issue]:
        """Fixed-mode generation: fill in every missing in-window occurrence.

        Window lower bound = ``dtstart`` so a first run (or a run after a long
        gap) regenerates the whole overdue stack inside the horizon.
        """
        window_start = pattern.dtstart
        occurrences = expand_occurrences(self.build_spec(pattern), window_start, horizon, exdates)
        if not occurrences:
            return []

        # One round-trip: which of these occurrences already have an issue?
        existing = await self._existing_occurrences(session, pattern.id)

        created: list[Issue] = []
        for occ in occurrences:
            if occ in existing:
                continue
            issue = await self._generate_issue(
                session, pattern, project, author, occ, overrides.get(occ), locale=locale
            )
            created.append(issue)
        return created

    async def _materialize_flexible(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        project: Project,
        author: User,
        now: datetime,
        horizon: datetime,
        exdates: set[datetime],
        overrides: dict[datetime, RecurrenceException],
        *,
        locale: str = "en",
    ) -> list[Issue]:
        """Flexible-mode generation: at most one next occurrence per run.

        - No instance yet  -> generate the first occurrence (if within horizon).
        - Latest instance open (not done/closed) -> generate nothing.
        - Latest instance closed -> generate the NEXT occurrence after it.

        The "next" anchor depends on ``base_date_strategy``:

        - ``scheduled``  -> the next scheduled occurrence strictly after the
          latest instance's ``original_occurrence_at``.
        - ``completion`` -> the next scheduled occurrence strictly after the
          instance's ``closed_on`` (when the work actually finished). This lets
          a late completion push the whole series forward.
        """
        latest = await self._latest_instance(session, pattern.id)

        if latest is None:
            # No instance yet: generate the first occurrence within the horizon.
            occurrences = expand_occurrences(self.build_spec(pattern), pattern.dtstart, horizon, exdates)
            if not occurrences:
                return []
            first = occurrences[0]
            issue = await self._generate_issue(
                session, pattern, project, author, first, overrides.get(first), locale=locale
            )
            return [issue]

        # An instance exists — only advance once it is done/closed.
        status = await session.get(IssueStatus, latest.status_id)
        if status is None or status.category not in ("done", "closed"):
            return []

        if pattern.base_date_strategy == "completion" and latest.closed_on is not None:
            anchor = latest.closed_on
        else:
            # 'scheduled' strategy, or no closed_on recorded: anchor on the
            # latest instance's scheduled occurrence.
            anchor = latest.original_occurrence_at or latest.created_at

        # Expand from the anchor forward and take the first occurrence strictly
        # after it. We widen the window start a touch before the anchor so the
        # engine's inclusive lower bound never drops the boundary occurrence.
        spec = self.build_spec(pattern)
        candidates = expand_occurrences(spec, anchor - timedelta(seconds=1), horizon, exdates)
        next_occ = next((o for o in candidates if o > anchor), None)
        if next_occ is None:
            return []

        # Guard against a duplicate if this occurrence somehow already exists.
        existing = await self._existing_occurrences(session, pattern.id)
        if next_occ in existing:
            return []

        issue = await self._generate_issue(
            session, pattern, project, author, next_occ, overrides.get(next_occ), locale=locale
        )
        return [issue]

    # ------------------------------------------------------------------
    # Per-occurrence issue generation
    # ------------------------------------------------------------------

    async def _generate_issue(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        project: Project,
        author: User,
        occurrence: datetime,
        override: RecurrenceException | None,
        *,
        locale: str = "en",
    ) -> Issue:
        """Build one IssueCreate from the template and persist via IssueService."""
        carry = pattern.carry_over or {}

        # Carry-over toggles: when True (default for a configured group), apply
        # the template value; when explicitly False, fall back to the default.
        def carried(group: str) -> bool:
            # Default True: a freshly configured pattern with an empty carry_over
            # dict still applies its template values (the intuitive behaviour).
            return bool(carry.get(group, True))

        description = pattern.template_description if carried("description") else None
        estimated_hours: Decimal | None = (
            pattern.template_estimated_hours if carried("estimated_hours") else None
        )
        metadata: dict = dict(pattern.template_metadata) if carried("metadata") else {}

        # Reset checklist: if the template metadata carries checklist-style
        # boolean fields, reset them to False on the generated instance
        # (best-effort: any top-level bool key is treated as a checklist item).
        # TODO(0.3.0): subtask/checklist *cloning* (creating child issues from a
        # template checklist) is out of scope — only top-level bool reset here.
        if pattern.reset_checklist and metadata:
            metadata = {k: (False if isinstance(v, bool) else v) for k, v in metadata.items()}

        # Assignee: rotation takes precedence over the template assignee.
        assigned_to_id = pattern.template_assigned_to_id if carried("assignee") else None
        if pattern.assignee_rotation:
            chosen, new_index = next_assignee(pattern.assignee_rotation, pattern.rotation_index)
            if chosen is not None:
                assigned_to_id = chosen
                # Persist the advanced rotation index back onto the pattern so
                # the next generation picks the following roster entry.
                pattern.rotation_index = new_index

        subject = pattern.template_subject

        # Apply the per-occurrence override payload (shallow-merge over template).
        # NOTE: relations/attachments carry-over (carry_over["relations"] /
        # ["attachments"]) is intentionally a no-op for 0.2.0.
        # TODO(0.3.0): clone relations / copy attachment files when those
        # carry_over groups are enabled.
        if override is not None and override.override_payload:
            payload = override.override_payload
            if "subject" in payload:
                subject = payload["subject"]
            if "description" in payload:
                description = payload["description"]
            if "assigned_to_id" in payload:
                assigned_to_id = payload["assigned_to_id"]
            if "estimated_hours" in payload and payload["estimated_hours"] is not None:
                estimated_hours = Decimal(str(payload["estimated_hours"]))
            if "metadata" in payload and isinstance(payload["metadata"], dict):
                metadata = {**metadata, **payload["metadata"]}
            if "priority_id" in payload:
                pass  # handled below via override_priority

        # Expand {{date}} macros against this occurrence's local date, so each
        # generated issue is distinct (e.g. "{{month}} {{year}}" review).
        local_date = self._occurrence_local_date(pattern, occurrence)
        subject = expand_macros(subject, local_date, locale) or subject
        description = expand_macros(description, local_date, locale)

        start_date, due_date = self._compute_dates(pattern, occurrence)

        issue_create = IssueCreate(
            project_key=project.key,
            tracker_id=pattern.template_tracker_id,
            subject=subject,
            description=description,
            # status_id: never carry a closed status. Use the template status
            # when set, else let IssueService resolve the tracker default. Both
            # paths yield a non-terminal status for a brand-new instance.
            status_id=pattern.template_status_id,
            priority_id=self._override_priority(override) or pattern.template_priority_id,
            assigned_to_id=assigned_to_id,
            category_id=pattern.template_category_id,
            fixed_version_id=pattern.template_fixed_version_id,
            # By default generated issues are NOT pinned to a sprint (avoids
            # skewing velocity); template_sprint_id is stored but not applied.
            sprint_id=None,
            start_date=start_date,
            due_date=due_date,
            estimated_hours=estimated_hours,
            done_ratio=0,  # always reset
            is_private=pattern.is_private,
            metadata=metadata,
        )

        issue = await self._issue_service.create(
            session,
            project,
            issue_create,
            author,
            recurring_pattern_id=pattern.id,
            original_occurrence_at=occurrence,
        )

        # Record provenance in the activity log: "Created from recurring pattern".
        await self._journal_service.record_recurring_source(
            session, issue, author, pattern.id, pattern.name
        )

        # If this occurrence had an override exception row, link the new issue.
        if override is not None:
            override.materialized_issue_id = issue.id
            session.add(override)

        return issue

    @staticmethod
    def _occurrence_local_date(pattern: RecurringPattern, occurrence: datetime) -> date:
        """Return *occurrence*'s calendar date as seen in the pattern timezone."""
        try:
            tz = ZoneInfo(pattern.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        return occurrence.astimezone(tz).date()

    def _compute_dates(
        self, pattern: RecurringPattern, occurrence: datetime
    ) -> tuple[Any, Any]:
        """Derive (start_date, due_date) calendar dates from an occurrence.

        See the module docstring for the offset rules. The occurrence's *local*
        date (in the pattern timezone) is the anchor.
        """
        local_date = self._occurrence_local_date(pattern, occurrence)

        if pattern.due_offset_days is None:
            due_date = local_date
        else:
            due_date = local_date + timedelta(days=pattern.due_offset_days)

        if pattern.start_offset_days is None:
            start_date = None
        else:
            start_date = local_date + timedelta(days=pattern.start_offset_days)

        return start_date, due_date

    @staticmethod
    def _override_priority(override: RecurrenceException | None) -> int | None:
        """Extract a priority_id override from an exception payload, if any."""
        if override is not None and override.override_payload:
            value = override.override_payload.get("priority_id")
            if isinstance(value, int):
                return value
        return None

    # ------------------------------------------------------------------
    # Edit-scope methods
    # ------------------------------------------------------------------

    async def skip_occurrence(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        occurrence_at: datetime,
    ) -> RecurrenceException:
        """Mark a single occurrence as skipped (EXDATE).

        A skip is never generated and never counts as a completion. If an issue
        was already materialised for the occurrence and is *untouched* (still in
        its default/open status with done_ratio 0), it is deleted; otherwise the
        existing issue is left alone and only the skip is recorded.
        """
        exc = await self._upsert_exception(session, pattern, occurrence_at, kind="skip", payload=None)

        existing = await self._issue_for_occurrence(session, pattern.id, occurrence_at)
        if existing is not None and await self._is_untouched(session, existing):
            await self._issue_service.delete(session, existing)
            exc.materialized_issue_id = None
            session.add(exc)
            await session.flush()
        return exc

    async def override_occurrence(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        occurrence_at: datetime,
        payload: dict,
    ) -> RecurrenceException:
        """Override a single occurrence's field values (RECURRENCE-ID).

        The payload is shallow-merged over the template at generation time. If
        the occurrence was already materialised, the payload is applied to that
        issue immediately via :meth:`IssueService.update`.
        """
        exc = await self._upsert_exception(
            session, pattern, occurrence_at, kind="override", payload=payload
        )

        existing = await self._issue_for_occurrence(session, pattern.id, occurrence_at)
        if existing is not None:
            update = IssueUpdate(
                subject=payload.get("subject"),
                description=payload.get("description"),
                assigned_to_id=payload.get("assigned_to_id"),
                priority_id=payload.get("priority_id"),
                metadata=payload.get("metadata"),
                # estimated_hours / done_ratio omitted from override scope; pass
                # their None defaults explicitly (PATCH semantics: no change).
                estimated_hours=None,
                done_ratio=None,
                lock_version=existing.lock_version,
            )
            author = await self._resolve_author(session, pattern.author_id)
            await self._issue_service.update(session, existing, update, author)
            exc.materialized_issue_id = existing.id
            session.add(exc)
            await session.flush()
        return exc

    async def split_from(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        occurrence_at: datetime,
        new_data: RecurringPatternCreate,
    ) -> RecurringPattern:
        """Split a series at *occurrence_at* ("this-and-future").

        Terminates the old series just before the boundary by setting
        ``pattern.until`` to one second before ``occurrence_at``, then creates a
        brand-new pattern (from *new_data*) anchored at the boundary. The new
        pattern carries the edited template/rule going forward; history on the
        old series is preserved untouched.
        """
        # Terminate the old series at the boundary. Use COUNT/UNTIL exclusivity:
        # clear any COUNT so UNTIL is valid, then set UNTIL just before the split.
        pattern.rrule_count = None
        pattern.until = occurrence_at - timedelta(seconds=1)
        session.add(pattern)
        await session.flush()

        # The new series starts exactly at the boundary occurrence.
        new_data = new_data.model_copy(update={"dtstart": occurrence_at})

        project = await self._resolve_project(session, pattern.project_id)
        author = await self._resolve_author(session, pattern.author_id)
        return await self.create(session, project, new_data, author)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _anchor_dtstart(dtstart: datetime, timezone: str) -> datetime:
        """Make a naive ``dtstart`` timezone-aware by anchoring it to ``timezone``.

        The web form and JSON API carry ``dtstart`` as a naive wall-clock value
        (an HTML ``datetime-local`` input has no offset) alongside a separate
        IANA ``timezone``. The recurrence engine requires a timezone-aware
        anchor, so a naive value is interpreted as local wall-clock in that
        timezone. Already-aware values denote an instant and pass through
        unchanged (e.g. MCP/API callers that send an explicit offset).
        """
        if dtstart.tzinfo is None:
            return dtstart.replace(tzinfo=ZoneInfo(timezone))
        return dtstart

    def _validate_rule(self, **spec_fields: Any) -> None:
        """Validate recurrence-rule coherence by expanding a tiny window.

        Builds a RecurrenceSpec from the given fields and expands one day; the
        engine raises ValueError on an incoherent spec, which we surface as a
        ValidationError.
        """
        try:
            spec = RecurrenceSpec(**spec_fields)
            start = spec_fields["dtstart"]
            expand_occurrences(spec, start, start + timedelta(days=_VALIDATION_WINDOW_DAYS))
        except ValueError as exc:
            raise ValidationError(message=f"Invalid recurrence rule: {exc}", field="recurrence")

    async def _filter_rotation(
        self,
        session: AsyncSession,
        project_id: int,
        rotation: dict | None,
    ) -> dict | None:
        """Filter ``assignee_rotation.user_ids`` to actual project members.

        The DB-free rotation engine assumes ``user_ids`` is already vetted; the
        service is responsible for dropping non-members. Order is preserved.
        Returns the rotation dict with a filtered ``user_ids`` list, or the
        original value when there is nothing to filter.
        """
        if not rotation:
            return rotation
        user_ids = rotation.get("user_ids")
        if not user_ids:
            return rotation

        result = await session.execute(
            select(Member.user_id).where(
                Member.project_id == project_id,
                Member.user_id.in_(list(user_ids)),
            )
        )
        members = set(result.scalars().all())
        # Preserve the caller's ordering; drop non-members / duplicates.
        seen: set[int] = set()
        filtered: list[int] = []
        for uid in user_ids:
            if uid in members and uid not in seen:
                filtered.append(uid)
                seen.add(uid)
        return {**rotation, "user_ids": filtered}

    async def _load_exceptions(
        self, session: AsyncSession, pattern_id: int
    ) -> tuple[set[datetime], dict[datetime, RecurrenceException]]:
        """Return (skip EXDATE set, {occurrence_at: override exception})."""
        result = await session.execute(
            select(RecurrenceException).where(
                RecurrenceException.recurring_pattern_id == pattern_id
            )
        )
        exdates: set[datetime] = set()
        overrides: dict[datetime, RecurrenceException] = {}
        for exc in result.scalars().all():
            if exc.kind == "skip":
                exdates.add(exc.occurrence_at)
            elif exc.kind == "override":
                overrides[exc.occurrence_at] = exc
        return exdates, overrides

    async def _existing_occurrences(
        self, session: AsyncSession, pattern_id: int
    ) -> set[datetime]:
        """Set of occurrence instants that already have a materialised issue."""
        result = await session.execute(
            select(Issue.original_occurrence_at).where(
                Issue.recurring_pattern_id == pattern_id,
                Issue.original_occurrence_at.is_not(None),
            )
        )
        return {row for row in result.scalars().all() if row is not None}

    async def _issue_for_occurrence(
        self, session: AsyncSession, pattern_id: int, occurrence_at: datetime
    ) -> Issue | None:
        """Return the issue materialised for a specific occurrence, if any."""
        result = await session.execute(
            select(Issue).where(
                Issue.recurring_pattern_id == pattern_id,
                Issue.original_occurrence_at == occurrence_at,
            )
        )
        return result.scalar_one_or_none()

    async def _latest_instance(self, session: AsyncSession, pattern_id: int) -> Issue | None:
        """Most-recently-scheduled materialised instance of a pattern."""
        result = await session.execute(
            select(Issue)
            .where(
                Issue.recurring_pattern_id == pattern_id,
                Issue.original_occurrence_at.is_not(None),
            )
            .order_by(Issue.original_occurrence_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _is_untouched(self, session: AsyncSession, issue: Issue) -> bool:
        """Simple heuristic: the issue is still in a default/open state.

        Treated as untouched when its status is not in a done/closed category
        and ``done_ratio`` is 0. (Comment/journal inspection is intentionally
        skipped for 0.2.0 — kept simple per spec.)
        """
        if issue.done_ratio != 0:
            return False
        status = await session.get(IssueStatus, issue.status_id)
        if status is not None and status.category in ("done", "closed"):
            return False
        return True

    async def _upsert_exception(
        self,
        session: AsyncSession,
        pattern: RecurringPattern,
        occurrence_at: datetime,
        kind: str,
        payload: dict | None,
    ) -> RecurrenceException:
        """Insert or update the (pattern, occurrence) exception row."""
        result = await session.execute(
            select(RecurrenceException).where(
                RecurrenceException.recurring_pattern_id == pattern.id,
                RecurrenceException.occurrence_at == occurrence_at,
            )
        )
        exc = result.scalar_one_or_none()
        if exc is None:
            exc = RecurrenceException(
                recurring_pattern_id=pattern.id,
                occurrence_at=occurrence_at,
                kind=kind,
                override_payload=payload,
            )
            session.add(exc)
        else:
            exc.kind = kind
            exc.override_payload = payload
            session.add(exc)
        await session.flush()
        return exc

    async def _resolve_project(self, session: AsyncSession, project_id: int) -> Project:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        return project

    async def _resolve_author(self, session: AsyncSession, author_id: int) -> User:
        author = await session.get(User, author_id)
        if author is None:
            raise NotFoundError(f"User {author_id} not found")
        return author

    # ------------------------------------------------------------------
    # Generator entrypoint helpers (for the future scheduler / Celery beat)
    # ------------------------------------------------------------------

    async def list_enabled(
        self,
        session: AsyncSession,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[RecurringPattern]:
        """Return enabled patterns ordered by id, optionally paginated.

        The Celery beat poller iterates this in fixed-size batches so a large
        number of patterns is never loaded into memory at once. The partial
        index ``ix_recurring_patterns_enabled WHERE enabled = true`` backs the
        filter. Ordering by ``id`` gives a stable, gap-free pagination cursor.
        Exceptions are eager-loaded so :meth:`materialize` does not issue an
        extra round-trip per pattern.
        """
        stmt = (
            select(RecurringPattern)
            .where(RecurringPattern.enabled.is_(True))
            .options(selectinload(RecurringPattern.exceptions))
            .order_by(RecurringPattern.id.asc())
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_due_patterns(self, session: AsyncSession) -> list[RecurringPattern]:
        """Return all enabled patterns (the scheduler iterates and materialises).

        Kept deliberately simple for 0.2.0: the look-ahead window inside
        :meth:`materialize` bounds the work, so the scheduler can call this and
        materialise each without further pre-filtering.
        """
        result = await session.execute(
            select(RecurringPattern)
            .where(RecurringPattern.enabled.is_(True))
            .options(selectinload(RecurringPattern.exceptions))
        )
        return list(result.scalars().all())

    async def run_due(self, session: AsyncSession, now: datetime | None = None) -> list[Issue]:
        """Materialise every enabled pattern. Returns all created issues."""
        when = now or utcnow()
        created: list[Issue] = []
        for pattern in await self.list_due_patterns(session):
            created.extend(await self.materialize(session, pattern, when))
        return created
