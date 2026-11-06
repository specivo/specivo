"""AI usage ingest v1: the batch upsert behind ``POST /api/v1/ai-usage/ingest``.

The contract (``docs/contracts/ai-usage/v1/README.md``) is the reference; this
module implements it on top of :class:`AiUsageService`, which authorizes and
writes every record, so the on-behalf rules, the cost snapshot, the
``revision`` and the audit events are the same as for any other entry.

Each record runs in a savepoint of its own: a rejected record leaves nothing
behind and never affects the others.

Attribution
-----------
Tried in this order; the first step that yields something usable wins and is
stored as ``attribution_method``:

1. ``attribution.issue_ref`` (``explicit``, or ``env`` when the client says
   it came from ``SPECIVO_ISSUE``);
2. the issue of an MCP agent session overlapping ``[started_at, ended_at]``
   (``mcp_session``): the reporting account's sessions first, then, for an
   on-behalf record, the target user's;
3. ``attribution.commits`` linked to an issue (``commit``): the issue most of
   them are linked to;
4. an issue key in ``attribution.git_branch`` (``branch``), among the keys of
   projects the reporting account reaches;
5. ``attribution.project_key`` (``explicit``), otherwise ``attribution.repo``
   matched to a connected repository's ``repo_full_name`` (``repo``);
6. nothing (``none``).

Explicit hints (``issue_ref``, ``project_key``) that cannot be used reject
the record. Inferred ones (steps 2 to 4 and the repository) that the target
user cannot log to are dropped with an ``attribution_dropped`` warning, and
the next step is tried. With a ``project_key``, inferred issues of other
projects are dropped too.

A replay of a bucket that is already attributed keeps its attribution: an
explicit hint naming something else rejects the record
(``attribution_conflict``); an inferred one is not applied and the record
gets ``attribution_dropped``.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_usage_ingest import (
    SCHEMA_V1,
    IngestRecord,
    Problem,
    RecordRejectedError,
    check_schema,
    parse_record,
    record_validator,
    repo_full_name,
    strip_unknown,
    unknown_fields_warning,
)
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.money import Money, MoneyError
from specivo.core.utils import utcnow
from specivo.hooks.refs import extract_branch_refs
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.repository import IssueCommitLink, RepositoryCommit, RepositoryIntegration
from specivo.models.user import User
from specivo.services.ai_usage_service import ON_BEHALF_REFUSED, AiUsageInput, AiUsageService
from specivo.services.api_key_scope import is_global_admin

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

_usage = AiUsageService()

#: How many inferred issues one step tries before giving up.
_MAX_CANDIDATES = 5

#: Error codes of :class:`AiUsageService` (and the common ones) → contract codes.
_ERROR_CODES: dict[str, str] = {
    "ai_usage_invalid": "invalid_record",
    "ai_tool_invalid": "invalid_record",
    "validation_error": "invalid_record",
    "currency_unknown": "currency_unknown",
    "ai_usage_target_unknown": "target_unknown",
    "ai_usage_target_invalid": "target_invalid",
    "ai_usage_target_not_member": "target_not_member",
    "permission_denied": "forbidden",
    "not_found": "not_found",
    "key_conflict": "key_conflict",
    "ai_usage_attribution_conflict": "attribution_conflict",
}


#: Rejections that tell something about the ``on_behalf_of`` user; a sender who may not
#: tell targets apart gets :func:`_refused` instead.
_TARGET_CODES: frozenset[str] = frozenset(
    {"forbidden", "target_unknown", "target_invalid", "target_not_member", "key_conflict"}
)
_ATTRIBUTION_WARNINGS: frozenset[str] = frozenset({"attribution_dropped", "attribution_ambiguous"})


def _refused() -> RecordRejectedError:
    """The one rejection of an ``on_behalf_of`` record the sender may not tell apart from any other."""
    return RecordRejectedError("forbidden", ON_BEHALF_REFUSED, "on_behalf_of")


def contract_error(exc: AppError) -> Problem:
    """An :class:`AppError` from the services as a contract error code."""
    code = _ERROR_CODES.get(exc.code)
    if code is None:
        if exc.status_code == 403:
            code = "forbidden"
        elif exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 409:
            code = "conflict"
        else:
            code = "invalid_record"
    if code == "currency_unknown":
        return Problem(code, exc.message, "reported_cost.currency")
    return Problem(code, exc.message, _FIELDS.get(exc.field or "", exc.field))


#: Service field names → record field paths.
_FIELDS: dict[str, str] = {
    "project_id": "attribution.project_key",
    "issue_id": "attribution.issue_ref",
    "external_session_id": "session_id",
    "repo_hint": "attribution.repo",
    "git_branch": "attribution.git_branch",
    "long_context": "long_context_tokens",
}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class RecordResult:
    index: int
    idempotency_key: str | None
    status: str = "rejected"
    entry_id: int | None = None
    revision: int | None = None
    attribution: dict[str, Any] | None = None
    warnings: list[Problem] = field(default_factory=list)
    error: Problem | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "entry_id": self.entry_id,
            "revision": self.revision,
            "attribution": self.attribution,
            "warnings": [w.as_dict() for w in self.warnings],
            "error": self.error.as_dict() if self.error is not None else None,
        }


@dataclass
class _Target:
    """Where a record ends up: project, issue and the method that found them."""

    project: Project | None = None
    issue: Issue | None = None
    method: str = "none"


# ---------------------------------------------------------------------------
# Per-batch caches
# ---------------------------------------------------------------------------


@dataclass
class _Batch:
    actor: User
    api_key_id: int | None
    client_name: str | None
    client_version: str | None
    request: Request | None
    now: datetime
    users: dict[str, User | None] = field(default_factory=dict)
    projects: dict[str, Project | None] = field(default_factory=dict)
    known_keys: frozenset[str] | None = None
    repos: dict[str, list[Project]] = field(default_factory=dict)
    seen: set[tuple[str, str]] = field(default_factory=set)
    for_others_anywhere: bool | None = None
    for_others_in: dict[int, bool] = field(default_factory=dict)


class AiUsageIngestService:
    """Validates, attributes and upserts a batch of usage records."""

    async def ingest(
        self,
        session: AsyncSession,
        actor: User,
        records: list[Any],
        *,
        client_name: str | None,
        client_version: str | None,
        api_key_id: int | None = None,
        request: Request | None = None,
    ) -> list[RecordResult]:
        """One :class:`RecordResult` per record, in order. Never raises for a bad record."""
        batch = _Batch(
            actor=actor,
            api_key_id=api_key_id,
            client_name=client_name,
            client_version=client_version,
            request=request,
            now=utcnow(),
        )
        results: list[RecordResult] = []
        for index, raw in enumerate(records):
            results.append(await self._one(session, batch, index, raw))
        return results

    # -- one record ----------------------------------------------------------------

    async def _one(self, session: AsyncSession, batch: _Batch, index: int, raw: Any) -> RecordResult:
        key = raw.get("idempotency_key") if isinstance(raw, dict) else None
        result = RecordResult(index=index, idempotency_key=key if isinstance(key, str) else None)
        unknown, problem = check_schema(record_validator(), raw)
        if unknown:
            result.warnings.append(unknown_fields_warning(unknown))
        if problem is not None:
            result.error = problem
            return result
        try:
            record = parse_record(strip_unknown(raw, unknown), batch.now)
        except RecordRejectedError as exc:
            result.error = exc.problem
            return result

        savepoint = await session.begin_nested()
        try:
            await self._write(session, batch, record, result)
        except RecordRejectedError as exc:
            await savepoint.rollback()
            result.error = exc.problem
        except AppError as exc:
            await savepoint.rollback()
            result.error = contract_error(exc)
        except (IntegrityError, DBAPIError):
            await savepoint.rollback()
            logger.exception("AI usage ingest: record %d failed in the database", index)
            result.error = Problem("internal_error", "The record could not be stored; send it again later")
        else:
            if savepoint.is_active:
                await savepoint.commit()
            return result
        result.status = "rejected"
        result.entry_id = result.revision = None
        result.attribution = None
        return result

    async def _write(self, session: AsyncSession, batch: _Batch, record: IngestRecord, result: RecordResult) -> None:
        actor = batch.actor
        login = (record.on_behalf_of or "").strip() or None
        if login is not None and login.lower() == actor.login.lower():
            login = None
        # Keyed by the login as sent, so a repeated key is found the same way for logins that exist and do not.
        seen = (login.lower() if login is not None else "", record.idempotency_key)
        if seen in batch.seen:
            raise RecordRejectedError(
                "duplicate_key", "The batch already holds a record with this idempotency key", "idempotency_key"
            )
        batch.seen.add(seen)

        reported: Money | None = None
        if record.reported_amount is not None and record.reported_currency is not None:
            try:
                reported = Money(record.reported_amount, record.reported_currency)
            except (MoneyError, ValueError):
                raise RecordRejectedError(
                    "currency_unknown", f"Unknown currency '{record.reported_currency}'", "reported_cost.currency"
                ) from None
        data = self._input(batch, record, reported)
        # The content is checked before the target, so its errors never depend on whether the login exists.
        await _usage.validate(session, data, source="reporter")

        if login is None:
            await self._upsert(session, batch, record, result, data, actor)
            return
        target, distinct = await self._target_user(session, batch, record, login)
        if distinct:
            await self._upsert(session, batch, record, result, data, target)
            return
        try:
            await self._upsert(session, batch, record, result, data, target)
        except (RecordRejectedError, AppError) as exc:
            problem = exc.problem if isinstance(exc, RecordRejectedError) else contract_error(exc)
            if problem.code in _TARGET_CODES:
                # Attribution warnings may name what was found from the target's own sessions.
                result.warnings = [w for w in result.warnings if w.code not in _ATTRIBUTION_WARNINGS]
                raise _refused() from None
            raise

    @staticmethod
    def _input(batch: _Batch, record: IngestRecord, reported: Money | None) -> AiUsageInput:
        """The record's usage, without its project and issue (attribution fills them)."""
        return AiUsageInput(
            tool=record.tool,
            model=record.model,
            usage_date=record.usage_date,
            provider=record.provider,
            tokens=record.tokens,
            long_context=record.long_context,
            started_at=record.started_at,
            ended_at=record.ended_at,
            request_count=record.request_count,
            cost_type=record.cost_type,
            reported_cost=reported,
            external_session_id=record.session_id,
            git_branch=record.attribution.git_branch,
            repo_hint=record.attribution.repo,
            client_name=batch.client_name,
            client_version=batch.client_version,
            idempotency_key=record.idempotency_key,
        )

    async def _upsert(
        self,
        session: AsyncSession,
        batch: _Batch,
        record: IngestRecord,
        result: RecordResult,
        data: AiUsageInput,
        target: User,
    ) -> None:
        actor = batch.actor
        existing = (
            await session.execute(
                select(AiUsageEntry).where(
                    AiUsageEntry.user_id == target.id, AiUsageEntry.idempotency_key == record.idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.reporter_user_id != actor.id:
            raise RecordRejectedError(
                "key_conflict", "This idempotency key belongs to an entry another account reported", "idempotency_key"
            )

        attributed = existing is not None and existing.project_id is not None
        resolved = await self._attribute(
            session, batch, record, target, result, stored=existing if attributed else None
        )
        if attributed:
            # The stored attribution stays; the service checks the stored project.
            resolved = _Target()

        data = replace(
            data,
            project_id=resolved.project.id if resolved.project is not None else None,
            issue_id=resolved.issue.id if resolved.issue is not None else None,
            attribution_method=resolved.method,
        )
        outcome = await _usage.upsert(
            session,
            actor,
            data,
            target=target,
            source="reporter",
            api_key_id=batch.api_key_id,
            request=batch.request,
        )
        entry = outcome.entry
        result.status = outcome.status
        result.entry_id = entry.id
        result.revision = entry.revision
        result.attribution = await self._attribution_of(session, actor, entry)

    async def _target_user(
        self, session: AsyncSession, batch: _Batch, record: IngestRecord, login: str
    ) -> tuple[User, bool]:
        """The ``on_behalf_of`` user, and whether the sender may learn why that user is refused.

        Against login enumeration, only a sender who may log for others in
        the record's explicit project (``project_key``, or the project of
        ``issue_ref``), or an administrator, gets ``target_unknown``,
        ``target_invalid`` and ``target_not_member``. Every other sender gets
        the one ``forbidden`` of :func:`_refused` for an unknown login, and
        (``_write``) for every refusal about a known one. A sender who may
        not log for others anywhere is refused here, before anything else.
        """
        actor = batch.actor
        if batch.for_others_anywhere is None:
            batch.for_others_anywhere = await _usage.may_log_for_others_anywhere(session, actor)
        if not batch.for_others_anywhere:
            raise _refused()
        cache_key = login.lower()
        if cache_key not in batch.users:
            batch.users[cache_key] = await _usage.user_by_login(session, login)
        user = batch.users[cache_key]
        distinct = await self._may_tell_targets_apart(session, batch, record)
        if user is None:
            if distinct:
                raise RecordRejectedError("target_unknown", f"No user with the login '{login}'", "on_behalf_of")
            raise _refused()
        return user, distinct

    async def _may_tell_targets_apart(self, session: AsyncSession, batch: _Batch, record: IngestRecord) -> bool:
        from specivo.services.issue_service import IssueService

        actor = batch.actor
        if is_global_admin(actor):
            return True
        project: Project | None = None
        hints = record.attribution
        if hints.issue_ref is not None:
            try:
                issue = await IssueService().get_by_display_key(session, hints.issue_ref, actor)
            except AppError:
                return False
            project = await session.get(Project, issue.project_id) if issue.project_id is not None else None
        elif hints.project_key is not None:
            project = (
                await session.execute(select(Project).where(Project.key == hints.project_key))
            ).scalar_one_or_none()
        if project is None:
            return False
        if project.id not in batch.for_others_in:
            batch.for_others_in[project.id] = await _usage.may_log_for_others(session, actor, project)
        return batch.for_others_in[project.id]

    async def _attribution_of(self, session: AsyncSession, actor: User, entry: AiUsageEntry) -> dict[str, Any]:
        """The entry's attribution as the result shows it; an issue the actor cannot see is left out."""
        from specivo.services.issue_service import IssueService

        project_key: str | None = None
        issue_ref: str | None = None
        if entry.project_id is not None:
            project = await session.get(Project, entry.project_id)
            project_key = project.key if project is not None else None
        if entry.issue_id is not None:
            issue = await session.get(Issue, entry.issue_id)
            if issue is not None and await IssueService().is_visible(session, issue, actor):
                issue_ref = issue.display_key
        return {"method": entry.attribution_method, "project_key": project_key, "issue_ref": issue_ref}

    # -- attribution ------------------------------------------------------------------

    async def _attribute(
        self,
        session: AsyncSession,
        batch: _Batch,
        record: IngestRecord,
        target: User,
        result: RecordResult,
        *,
        stored: AiUsageEntry | None,
    ) -> _Target:
        """Resolve the record's project and issue (see the module docstring); raises for an unusable explicit hint.

        *stored* is the already attributed entry the record replays, or None.
        Its attribution wins: a conflicting explicit hint is refused, a
        conflicting inferred one is reported with ``attribution_dropped``.
        """
        hints = record.attribution
        explicit_project: Project | None = None
        if hints.project_key is not None:
            explicit_project = await self._project_by_key(session, batch, hints.project_key)

        if hints.issue_ref is not None:
            issue = await self._explicit_issue(session, batch, hints.issue_ref)
            if explicit_project is not None and issue.project_id != explicit_project.id:
                raise RecordRejectedError(
                    "attribution_invalid",
                    f"Issue {hints.issue_ref} is not in project {hints.project_key}",
                    "attribution.issue_ref",
                )
            project = await session.get(Project, issue.project_id)
            if stored is not None:
                self._require_same(stored, project, issue, hints.issue_ref)
                return _Target()
            await _usage.require_can_log(session, batch.actor, target, project, issue.id, request=batch.request)
            return _Target(project, issue, hints.issue_method)

        if explicit_project is not None:
            if stored is not None:
                self._require_same(stored, explicit_project, None, explicit_project.key)
            else:
                await _usage.require_can_log(
                    session, batch.actor, target, explicit_project, None, request=batch.request
                )

        found = await self._inferred(session, batch, record, target, result, explicit_project)
        if found is None and explicit_project is not None:
            found = _Target(explicit_project, None, "explicit")
        if found is None:
            found = await self._by_repo(session, batch, record, target, result)
        if found is None:
            return _Target()
        if stored is not None and found.method != "explicit":
            changes_project = found.project is not None and found.project.id != stored.project_id
            changes_issue = found.issue is not None and found.issue.id != stored.issue_id
            if changes_project or changes_issue:
                result.warnings.append(
                    Problem(
                        "attribution_dropped",
                        "The entry is already attributed; the attribution found from "
                        f"{_METHOD_LABELS[found.method]} was not applied",
                        "attribution",
                    )
                )
            return _Target()
        return found

    @staticmethod
    def _require_same(stored: AiUsageEntry, project: Project | None, issue: Issue | None, named: str) -> None:
        same_project = project is not None and project.id == stored.project_id
        same_issue = issue is None or issue.id == stored.issue_id
        if not (same_project and same_issue):
            raise RecordRejectedError(
                "attribution_conflict",
                f"The entry is attributed elsewhere than {named}; send it without attribution, or move it in Specivo",
                "attribution.issue_ref" if issue is not None else "attribution.project_key",
            )

    async def _project_by_key(self, session: AsyncSession, batch: _Batch, key: str) -> Project:
        from specivo.services.project_service import ProjectService

        if key not in batch.projects:
            batch.projects[key] = (
                await session.execute(select(Project).where(Project.key == key))
            ).scalar_one_or_none()
        project = batch.projects[key]
        if project is None:
            raise RecordRejectedError("not_found", f"Project '{key}' not found", "attribution.project_key")
        try:
            await ProjectService().require_project_access(session, project, batch.actor)
        except AppError:
            raise RecordRejectedError("not_found", f"Project '{key}' not found", "attribution.project_key") from None
        return project

    async def _explicit_issue(self, session: AsyncSession, batch: _Batch, ref: str) -> Issue:
        from specivo.services.issue_service import IssueService

        try:
            issue = await IssueService().get_by_display_key(session, ref, batch.actor)
        except NotFoundError:
            raise RecordRejectedError("not_found", f"Issue '{ref}' not found", "attribution.issue_ref") from None
        if issue.project_id is None:
            raise RecordRejectedError("not_found", f"Issue '{ref}' not found", "attribution.issue_ref")
        return issue

    async def _usable(
        self,
        session: AsyncSession,
        batch: _Batch,
        target: User,
        project: Project,
        issue: Issue | None,
        method: str,
        result: RecordResult,
        only_project: Project | None,
    ) -> bool:
        """Whether an inferred project/issue may be used; warns ``attribution_dropped`` when not."""
        from specivo.services.issue_service import IssueService

        reason: str | None = None
        if only_project is not None and project.id != only_project.id:
            reason = f"it is not in project {only_project.key}"
        else:
            try:
                await _usage.require_can_log(session, batch.actor, target, project, issue.id if issue else None)
            except AppError:
                reason = "the usage cannot be logged there"
        if reason is None:
            return True
        name = project.key
        if issue is not None:
            visible = await IssueService().is_visible(session, issue, batch.actor)
            name = issue.display_key if visible else "an issue"
        result.warnings.append(
            Problem(
                "attribution_dropped",
                f"{name}, found from {_METHOD_LABELS[method]}, was skipped: {reason}",
                "attribution",
            )
        )
        return False

    async def _inferred(
        self,
        session: AsyncSession,
        batch: _Batch,
        record: IngestRecord,
        target: User,
        result: RecordResult,
        only_project: Project | None,
    ) -> _Target | None:
        """Steps 2 to 4: an issue from agent sessions, commits, then the branch."""
        tried: set[int] = set()
        steps = (
            ("mcp_session", self._session_issues),
            ("commit", self._commit_issues),
            ("branch", self._branch_issues),
        )
        for method, finder in steps:
            for issue_id in await finder(session, batch, record, target):
                if issue_id in tried:
                    continue
                tried.add(issue_id)
                issue = await session.get(Issue, issue_id)
                if issue is None or issue.project_id is None:
                    continue
                project = await session.get(Project, issue.project_id)
                if project is None:
                    continue
                if await self._usable(session, batch, target, project, issue, method, result, only_project):
                    return _Target(project, issue, method)
        return None

    async def _session_issues(
        self, session: AsyncSession, batch: _Batch, record: IngestRecord, target: User
    ) -> list[int]:
        from specivo.services.agent_session_service import AgentSessionService

        start = record.started_at or record.ended_at
        end = record.ended_at or record.started_at
        if start is None or end is None:
            return []
        found: list[int] = []
        # The reporting account's sessions first, then the target user's.
        for user_id in dict.fromkeys((batch.actor.id, target.id)):
            issue_id = await AgentSessionService().issue_for(session, user_id, start, end)
            if issue_id is not None:
                found.append(issue_id)
        return found

    async def _commit_issues(
        self, session: AsyncSession, batch: _Batch, record: IngestRecord, target: User
    ) -> list[int]:
        shas = record.attribution.commits
        if not shas:
            return []
        rows = (
            await session.execute(
                select(IssueCommitLink.issue_id, RepositoryCommit.sha)
                .join(RepositoryCommit, RepositoryCommit.id == IssueCommitLink.commit_id)
                .where(func.lower(RepositoryCommit.sha).in_(shas))
            )
        ).all()
        counts: Counter[int] = Counter()
        for issue_id, _sha in {(row[0], row[1].lower()) for row in rows}:
            counts[issue_id] += 1
        ranked = sorted(counts, key=lambda issue_id: (-counts[issue_id], issue_id))
        return ranked[:_MAX_CANDIDATES]

    async def _branch_issues(
        self, session: AsyncSession, batch: _Batch, record: IngestRecord, target: User
    ) -> list[int]:
        from specivo.services.project_service import ProjectService

        branch = record.attribution.git_branch
        if not branch:
            return []
        if batch.known_keys is None:
            stmt = select(Project.key)
            clause = await ProjectService().accessible_projects_clause(session, batch.actor)
            if clause is not None:
                stmt = stmt.where(clause)
            batch.known_keys = frozenset((await session.execute(stmt)).scalars().all())
        refs = extract_branch_refs(branch, batch.known_keys)[:_MAX_CANDIDATES]
        found: list[int] = []
        for ref in refs:
            issue_id = (
                await session.execute(
                    select(Issue.id).where(Issue.project_key == ref.key, Issue.sequence_number == ref.number)
                )
            ).scalar_one_or_none()
            if issue_id is not None:
                found.append(issue_id)
        return found

    async def _by_repo(
        self, session: AsyncSession, batch: _Batch, record: IngestRecord, target: User, result: RecordResult
    ) -> _Target | None:
        """Step 5b: the one project whose connected repository matches ``attribution.repo``."""
        from specivo.services.project_service import ProjectService

        name = repo_full_name(record.attribution.repo)
        if name is None:
            return None
        if name not in batch.repos:
            stmt = (
                select(Project)
                .join(RepositoryIntegration, RepositoryIntegration.project_id == Project.id)
                .where(func.lower(RepositoryIntegration.repo_full_name) == name, RepositoryIntegration.is_active)
                .distinct()
                .order_by(Project.id)
            )
            clause = await ProjectService().accessible_projects_clause(session, batch.actor)
            if clause is not None:
                stmt = stmt.where(clause)
            batch.repos[name] = list((await session.execute(stmt)).scalars().all())
        usable = [
            project
            for project in batch.repos[name]
            if await self._usable(session, batch, target, project, None, "repo", result, None)
        ]
        if len(usable) == 1:
            return _Target(usable[0], None, "repo")
        if len(usable) > 1:
            keys = ", ".join(project.key for project in usable)
            result.warnings.append(
                Problem(
                    "attribution_ambiguous",
                    f"The repository is connected to several projects ({keys}); the entry stays unassigned",
                    "attribution.repo",
                )
            )
        return None


_METHOD_LABELS: dict[str, str] = {
    "mcp_session": "an MCP agent session",
    "commit": "the commits",
    "branch": "the branch name",
    "repo": "the repository",
    "explicit": "the record",
    "env": "the record",
}

__all__ = ["SCHEMA_V1", "AiUsageIngestService", "RecordResult", "contract_error"]
