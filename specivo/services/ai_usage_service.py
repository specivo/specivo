"""AI usage entries: who may read and write them, their costs and the range recompute.

The sibling of ``TimeEntryService``, with a reporter and an "on behalf of" user.

Reading
-------
``visible_entries_clause`` (one project) and ``visible_entries_clause_any``
(across projects) decide which entries a user reads; every reader goes
through one of them. An entry is visible when any of these holds:

1. it is logged in the reader's name (``user_id``): always, without
   ``view_ai_usage``, unassigned or not, and whatever issue it is on;
2. the reader reported it (``reporter_user_id``): a service account sees what
   it wrote;
3. the reader's roles in its project grant ``view_ai_usage`` with
   ``ai_usage_visibility = 'all'`` (``'own'`` adds nothing beyond rule 1);
4. the reader is an administrator.

Rules 2 and 3 hide entries on issues the reader cannot see. Unassigned
entries (no project) of others are visible to administrators and to their
reporter only. A project-limited API key narrows everything to its projects,
and sees unassigned entries only where it is the reporting key.

Writing
-------
``require_can_log`` decides who may log in whose name:

- for oneself: ``log_ai_usage`` in the project, or no project (unassigned);
- for another user (on behalf): ``log_ai_usage_for_others`` in the project,
  and the target must be an active member there; unassigned entries for
  another user are for administrators only.

``can_edit``: the entry's user, ``manage_ai_usage`` in its project, or an
administrator. Reporter and import entries keep their token, model and tool
columns: only the project, the issue and the comments change. People exclude
them (``excluded_at``) rather than delete them, and a later upsert keeps the
exclusion; administrators may hard-delete. Moving an entry to another project
is checked like logging it there in its user's name (``require_can_log``),
and a move (a changed project or issue) sets ``attribution_method`` to
``manual``, which a reporter's replay never changes again. A replay of an
existing bucket is authorized against the stored entry, never the project
the record names (``_replace``).

Costs
-----
Computed by the server from the tokens and the price table at write time
(:mod:`specivo.core.ai_usage`), and stored as a snapshot with ``price_id`` and
``priced_at``. A client-reported cost is stored apart (``reported_cost``) and
used as the marginal cost of ``api`` usage only. Prices added or edited later
never change an entry by themselves: :meth:`AiUsageService.recompute_range`
does, each entry at its own ``priced_at``.

Audit: logging for another user, an edit by someone other than the entry's
user, excluding and including, deleting, and recomputing costs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, false, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.ai_pricing import TOKEN_CLASSES, TokenCounts, normalize_provider
from specivo.core.ai_usage import (
    COMMENTS_MAX_LENGTH,
    CONTEXT_MAX_LENGTHS,
    EARLIEST_USAGE_DATE,
    FUTURE_TOLERANCE,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    MAX_REQUEST_COUNT,
    CostInputs,
    check_bucket_totals,
    check_tokens,
    compute_costs,
    priced_at_for,
    token_counts,
)
from specivo.core.constants import MAX_HIERARCHY_DEPTH
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.money import Money, MoneyError, format_decimal_plain
from specivo.core.utils import utcnow
from specivo.models.ai_model_price import AiModelPrice
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import (
    AI_USAGE_ATTRIBUTION_METHODS,
    AI_USAGE_COST_TYPES,
    AI_USAGE_SOURCES,
    LOCKED_SOURCES,
    MANUAL_ATTRIBUTION,
    AiUsageEntry,
)
from specivo.models.currency import Currency
from specivo.models.issue import Issue
from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.role import AI_USAGE_VISIBILITY_ALL
from specivo.models.user import User
from specivo.services.ai_model_price_service import (
    AiModelPriceService,
    PriceReference,
    model_name_for,
    register_price_reference,
)
from specivo.services.api_key_scope import (
    admin_reaches,
    is_global_admin,
    is_limited_key,
    limited_key_id,
    project_in_scope,
    scope_clause,
)
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

#: ``ai_usage_entries.price_id`` / ``priced_at`` point at prices: referenced
#: price rows cannot be deleted, nor ended before the entries priced with them.
PRICE_REFERENCE = PriceReference(table="ai_usage_entries")
register_price_reference(PRICE_REFERENCE)

_audit = SecurityAuditService()
_prices = AiModelPriceService()

#: Fields a person may change on any entry they may edit.
ATTRIBUTION_FIELDS: frozenset[str] = frozenset({"project_id", "issue_id", "comments"})
#: Fields a person may also change on manual, API and MCP entries.
USAGE_FIELDS: frozenset[str] = frozenset(
    {
        "tool",
        "provider",
        "model",
        "usage_date",
        "started_at",
        "ended_at",
        "request_count",
        "tokens",
        "long_context",
        "cost_type",
        "reported_cost",
        "external_session_id",
        "git_branch",
        "repo_hint",
    }
)
#: Usage fields whose change re-prices the entry.
_COST_FIELDS: frozenset[str] = frozenset(
    {"tool", "provider", "model", "usage_date", "started_at", "tokens", "long_context", "cost_type", "reported_cost"}
)

_KEY_RE = re.compile(r"^[\x21-\x7e]+$")

#: Computed amounts are stored with 8 decimal places (``NUMERIC(20, 8)``); totals are stated the same way.
_STORED_SCALE = Decimal("0.00000001")

#: Batch size of the range recompute.
RECOMPUTE_BATCH = 500


def _invalid(message: str, field_name: str, code: str = "ai_usage_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


#: The one answer to a refused "on behalf of" request. Whoever may not log
#: for others gets it for every target, existing or not, active or not, member
#: or not, so the answer never tells which logins exist.
ON_BEHALF_REFUSED = "You cannot log AI usage for this user here"


def issue_subtree_clause(issue_id: int) -> ColumnElement[bool]:
    """Entries on issue *issue_id* or on any issue below it (sub-issues, their sub-issues, ...).

    One recursive CTE down ``parent_id`` (the nested-set columns are not
    trusted for this), stopped at ``MAX_HIERARCHY_DEPTH`` levels so a corrupted
    cycle cannot run forever. Visibility is not part of it: callers add the
    usual visibility conditions, so entries on sub-issues the reader cannot see
    stay hidden.
    """
    child = aliased(Issue)
    tree = (
        select(Issue.id.label("id"), literal(0).label("level"))
        .where(Issue.id == issue_id)
        .cte("ai_usage_issue_tree", recursive=True)
    )
    tree = tree.union_all(
        select(child.id, tree.c.level + 1).where(child.parent_id == tree.c.id, tree.c.level < MAX_HIERARCHY_DEPTH)
    )
    return AiUsageEntry.issue_id.in_(select(tree.c.id))


def on_behalf_refused() -> PermissionDeniedError:
    """The 403 of a refused "on behalf of" request (see ``ON_BEHALF_REFUSED``)."""
    return PermissionDeniedError(ON_BEHALF_REFUSED, field="on_behalf_of")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass
class AiUsageInput:
    """One bucket of usage as a client or a person sends it (raw values; validated by the service).

    ``tokens`` and ``long_context`` map token classes to counts. ``cost_type``
    ``None`` lets the server default it from the user's subscriptions.
    """

    tool: str
    model: str
    usage_date: date
    provider: str | None = None
    tokens: Mapping[str, Any] = field(default_factory=dict)
    long_context: Mapping[str, Any] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    request_count: int = 0
    cost_type: str | None = None
    reported_cost: Money | None = None
    project_id: int | None = None
    issue_id: int | None = None
    attribution_method: str | None = None
    external_session_id: str | None = None
    git_branch: str | None = None
    repo_hint: str | None = None
    client_name: str | None = None
    client_version: str | None = None
    comments: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ModelSuggestion:
    """A model name for a report's model filter, with its provider and the other names it is priced under."""

    model: str
    provider: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """What an upsert did: ``created``, ``updated`` or ``unchanged`` (nothing written)."""

    entry: AiUsageEntry
    status: str


@dataclass(frozen=True, slots=True)
class RecomputeOutcome:
    """A range recompute: how many entries it read and changed, and the totals per currency before and after."""

    entries: int
    changed: int
    before: dict[str, dict[str, str]]
    after: dict[str, dict[str, str]]


@dataclass
class EntryReferences:
    """What a page of entries names: users by id, project keys by id, and the issue keys the reader may see."""

    users: dict[int, User] = field(default_factory=dict)
    project_keys: dict[int, str] = field(default_factory=dict)
    issue_keys: dict[int, str] = field(default_factory=dict)
    #: Names of the API keys that reported entries for someone else ("logged by X via key Y").
    key_names: dict[int, str] = field(default_factory=dict)


#: How many unpriced models a summary or report names.
UNPRICED_MODELS_SHOWN = 5


async def unpriced_models(
    session: AsyncSession, conditions: Sequence[ColumnElement[bool]], limit: int = UNPRICED_MODELS_SHOWN
) -> list[tuple[str, int]]:
    """The models of the unpriced entries matching *conditions*, most entries first."""
    stmt = (
        select(AiUsageEntry.model, func.count())
        .where(*conditions, AiUsageEntry.cost_basis == "unpriced")
        .group_by(AiUsageEntry.model)
        .order_by(func.count().desc(), AiUsageEntry.model)
        .limit(limit)
    )
    return [(model or "", int(count)) for model, count in (await session.execute(stmt)).all()]


@dataclass
class UsageSummary:
    """Totals of a filtered set of entries, excluded ones left out (see ``summarize_entries``).

    ``tokens`` holds each class summed over both buckets (base and long-context).
    ``marginal`` and ``api_equivalent`` hold one amount per day and currency,
    ready for ``ExchangeRateService.summarize``.
    """

    entries: int = 0
    excluded: int = 0
    #: Counted entries without a price for their model and date (they add 0).
    unpriced: int = 0
    #: The models of those entries, most entries first (at most ``UNPRICED_MODELS_SHOWN``).
    unpriced_models: list[tuple[str, int]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    marginal: list[tuple[Money, date]] = field(default_factory=list)
    api_equivalent: list[tuple[Money, date]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())


class _Totals:
    def __init__(self) -> None:
        self.sums: dict[str, dict[str, Decimal]] = {"marginal": {}, "api_equivalent": {}}

    def add(self, entry: AiUsageEntry) -> None:
        for name in ("marginal", "api_equivalent"):
            amount = getattr(entry, f"{name}_amount")
            currency = getattr(entry, f"{name}_currency")
            if amount is not None and currency is not None:
                bucket = self.sums[name]
                bucket[currency] = bucket.get(currency, Decimal(0)) + amount

    def as_details(self) -> dict[str, dict[str, str]]:
        return {
            name: {code: format_decimal_plain(total.quantize(_STORED_SCALE)) for code, total in sorted(values.items())}
            for name, values in self.sums.items()
        }


def _cost_snapshot(entry: AiUsageEntry) -> tuple[Any, ...]:
    return (
        entry.price_id,
        entry.priced_at,
        entry.marginal_amount,
        entry.marginal_currency,
        entry.api_equivalent_amount,
        entry.api_equivalent_currency,
        entry.cost_basis,
    )


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiUsageService:
    """Visibility, authorization, writes and costs of AI usage entries."""

    # -- visibility -------------------------------------------------------------

    @staticmethod
    def _issue_visible(issues: ColumnElement[bool] | None) -> ColumnElement[bool] | None:
        """Entries without an issue or on an issue matching *issues* (None: every issue)."""
        if issues is None:
            return None
        return or_(AiUsageEntry.issue_id.is_(None), AiUsageEntry.issue_id.in_(select(Issue.id).where(issues)))

    async def visible_entries_clause(
        self, session: AsyncSession, user: User, project: Project
    ) -> ColumnElement[bool] | None:
        """Return the predicate limiting *project*'s entries to those *user* may read, or None for all of them.

        See the module docstring for the rules. Callers still limit the query
        to ``project_id = project.id``.
        """
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import ai_usage_visibility

        if user.is_anonymous or not project_in_scope(project.id):
            return false()
        if admin_reaches(user, project.id):
            return None
        own = AiUsageEntry.user_id == user.id
        visibility = await ai_usage_visibility(session, user, project)
        issues = await IssueService().project_visible_issues_clause(session, user, project)
        if issues is not None:
            issues = and_(Issue.project_id == project.id, issues)
        issue_ok = self._issue_visible(issues)
        if visibility == AI_USAGE_VISIBILITY_ALL:
            if issue_ok is None:
                return None
            return or_(own, issue_ok)
        reported: ColumnElement[bool] = AiUsageEntry.reporter_user_id == user.id
        if issue_ok is not None:
            reported = and_(reported, issue_ok)
        return or_(own, reported)

    async def visible_entries_clause_any(self, session: AsyncSession, user: User) -> ColumnElement[bool] | None:
        """Return the cross-project form of ``visible_entries_clause``, or None for no limit.

        The membership roles granting ``view_ai_usage`` are read in one query
        (``permission_service.entry_visibility_projects``, shared with time
        entries). Narrowed to the authenticating API key's project limit, under
        which unassigned entries are visible only where that key reported them.
        """
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import Permission, entry_visibility_projects

        if user.is_anonymous:
            return false()
        clause: ColumnElement[bool] | None
        if user.is_admin:
            clause = None
        else:
            projects = await entry_visibility_projects(session, user, Permission.VIEW_AI_USAGE, "ai_usage_visibility")
            others: ColumnElement[bool] = or_(
                AiUsageEntry.reporter_user_id == user.id, projects.clause(AiUsageEntry.project_id)
            )
            issue_ok = self._issue_visible(await IssueService().visible_issues_clause(session, user))
            if issue_ok is not None:
                others = and_(others, issue_ok)
            clause = or_(AiUsageEntry.user_id == user.id, others)
        if not is_limited_key():
            return clause
        key_id = limited_key_id()
        in_scope = scope_clause(AiUsageEntry.project_id)
        unassigned_by_key: ColumnElement[bool] = (
            and_(AiUsageEntry.project_id.is_(None), AiUsageEntry.reporter_api_key_id == key_id)
            if key_id is not None
            else false()
        )
        limit = or_(in_scope if in_scope is not None else false(), unassigned_by_key)
        return limit if clause is None else and_(clause, limit)

    async def get_visible(self, session: AsyncSession, entry_id: int, user: User) -> AiUsageEntry:
        """Return entry *entry_id* if *user* may read it; otherwise 404, so ids cannot be probed."""
        clause = await self.visible_entries_clause_any(session, user)
        stmt = select(AiUsageEntry).where(AiUsageEntry.id == entry_id)
        if clause is not None:
            stmt = stmt.where(clause)
        entry = (await session.execute(stmt)).scalar_one_or_none()
        if entry is None:
            raise NotFoundError(f"AI usage entry {entry_id} not found")
        return entry

    async def list_entries(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None = None,
        user_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        tool: str | None = None,
        model: str | None = None,
        cost_type: str | None = None,
        issue_id: int | None = None,
        source: str | None = None,
        provider: str | None = None,
        branch: str | None = None,
        without_issue: bool = False,
        without_branch: bool = False,
        excluded: bool | None = None,
        unassigned: bool | None = None,
        issue_tree_id: int | None = None,
        external_session_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiUsageEntry], int]:
        """Entries *viewer* may read, newest day first; in *project* when given. Returns ``(page, total)``.

        Filters: *model* matches the aliases of its price rows too;
        *provider* and *branch* match exactly (``without_issue`` /
        ``without_branch`` keep entries without one); *excluded* ``True`` /
        ``False`` keeps only excluded / counted entries (None: both);
        *unassigned* ``True`` / ``False`` keeps only entries without / with a
        project; *issue_tree_id* keeps the entries of that issue and of every
        issue below it (:func:`issue_subtree_clause`); *external_session_id*
        keeps the entries of one agent session (the id the reporter sends).
        """
        conditions = await self._list_conditions(
            session,
            viewer,
            project=project,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            tool=tool,
            model=model,
            cost_type=cost_type,
            issue_id=issue_id,
            source=source,
            provider=provider,
            branch=branch,
            without_issue=without_issue,
            without_branch=without_branch,
            excluded=excluded,
            unassigned=unassigned,
            issue_tree_id=issue_tree_id,
            external_session_id=external_session_id,
        )
        total = await session.scalar(select(func.count()).select_from(AiUsageEntry).where(*conditions)) or 0
        rows = (
            (
                await session.execute(
                    select(AiUsageEntry)
                    .where(*conditions)
                    .order_by(AiUsageEntry.usage_date.desc(), AiUsageEntry.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def summarize_entries(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None = None,
        user_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        tool: str | None = None,
        model: str | None = None,
        cost_type: str | None = None,
        issue_id: int | None = None,
        source: str | None = None,
        provider: str | None = None,
        branch: str | None = None,
        without_issue: bool = False,
        without_branch: bool = False,
        unassigned: bool | None = None,
    ) -> UsageSummary:
        """Totals of the entries ``list_entries`` would return with the same filters, excluded ones left out.

        Two grouped queries: the token sums, and the costs per day and
        currency (so the caller converts each day at its own rate, see
        ``ExchangeRateService.summarize``). Each class sums both token buckets
        (base and long-context). The number of excluded entries matching the
        filters is counted apart.
        """
        conditions = await self._list_conditions(
            session,
            viewer,
            project=project,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            tool=tool,
            model=model,
            cost_type=cost_type,
            issue_id=issue_id,
            source=source,
            provider=provider,
            branch=branch,
            without_issue=without_issue,
            without_branch=without_branch,
            unassigned=unassigned,
        )
        counted = AiUsageEntry.excluded_at.is_(None)
        row = (
            await session.execute(
                select(
                    func.count().filter(counted),
                    func.count().filter(AiUsageEntry.excluded_at.is_not(None)),
                    func.count().filter(counted, AiUsageEntry.cost_basis == "unpriced"),
                    *[func.coalesce(func.sum(AiUsageEntry.class_total(c)).filter(counted), 0) for c in TOKEN_CLASSES],
                ).where(*conditions)
            )
        ).one()
        summary = UsageSummary(
            entries=int(row[0]),
            excluded=int(row[1]),
            unpriced=int(row[2]),
            tokens={c: int(v) for c, v in zip(TOKEN_CLASSES, row[3:], strict=True)},
        )
        if summary.unpriced:
            summary.unpriced_models = await unpriced_models(session, [*conditions, counted])
        for name in ("marginal", "api_equivalent"):
            amount = getattr(AiUsageEntry, f"{name}_amount")
            currency = getattr(AiUsageEntry, f"{name}_currency")
            stmt = (
                select(AiUsageEntry.usage_date, currency, func.sum(amount))
                .where(*conditions, counted, amount.is_not(None), currency.is_not(None))
                .group_by(AiUsageEntry.usage_date, currency)
                .order_by(AiUsageEntry.usage_date, currency)
            )
            items = [(Money(total, code), day) for day, code, total in (await session.execute(stmt)).all()]
            setattr(summary, name, items)
        return summary

    async def _list_conditions(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None = None,
        user_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        tool: str | None = None,
        model: str | None = None,
        cost_type: str | None = None,
        issue_id: int | None = None,
        source: str | None = None,
        provider: str | None = None,
        branch: str | None = None,
        without_issue: bool = False,
        without_branch: bool = False,
        excluded: bool | None = None,
        unassigned: bool | None = None,
        issue_tree_id: int | None = None,
        external_session_id: str | None = None,
    ) -> list[ColumnElement[bool]]:
        """The WHERE conditions of ``list_entries`` and ``summarize_entries``, visibility included."""
        conditions: list[ColumnElement[bool]] = []
        if project is not None:
            conditions.append(AiUsageEntry.project_id == project.id)
            visible = await self.visible_entries_clause(session, viewer, project)
        else:
            visible = await self.visible_entries_clause_any(session, viewer)
        if visible is not None:
            conditions.append(visible)
        if user_id is not None:
            conditions.append(AiUsageEntry.user_id == user_id)
        if date_from is not None:
            conditions.append(AiUsageEntry.usage_date >= date_from)
        if date_to is not None:
            conditions.append(AiUsageEntry.usage_date <= date_to)
        if tool:
            conditions.append(AiUsageEntry.tool_slug == tool.strip().lower())
        if model and model.strip():
            conditions.append(AiUsageEntry.model.in_(await self.model_names(session, None, model)))
        if cost_type:
            if cost_type not in AI_USAGE_COST_TYPES:
                raise _invalid(f"cost_type must be one of {', '.join(AI_USAGE_COST_TYPES)}", "cost_type")
            conditions.append(AiUsageEntry.cost_type == cost_type)
        if source:
            if source not in AI_USAGE_SOURCES:
                raise _invalid(f"source must be one of {', '.join(AI_USAGE_SOURCES)}", "source")
            conditions.append(AiUsageEntry.source == source)
        if issue_id is not None:
            conditions.append(AiUsageEntry.issue_id == issue_id)
        if without_issue:
            conditions.append(AiUsageEntry.issue_id.is_(None))
        if issue_tree_id is not None:
            conditions.append(issue_subtree_clause(issue_tree_id))
        if external_session_id is not None and external_session_id.strip():
            conditions.append(AiUsageEntry.external_session_id == external_session_id.strip())
        if provider and provider.strip():
            conditions.append(AiUsageEntry.provider == (normalize_provider(provider) or provider.strip().lower()))
        if branch and branch.strip():
            conditions.append(AiUsageEntry.git_branch == branch.strip())
        if without_branch:
            conditions.append(AiUsageEntry.git_branch.is_(None))
        if excluded is not None:
            conditions.append(AiUsageEntry.excluded_at.is_not(None) if excluded else AiUsageEntry.excluded_at.is_(None))
        if unassigned is not None:
            conditions.append(AiUsageEntry.project_id.is_(None) if unassigned else AiUsageEntry.project_id.is_not(None))
        return conditions

    # -- authorization ------------------------------------------------------------

    @staticmethod
    def require_valid_target(target: User) -> None:
        """Refuse a target nobody can log usage for: the anonymous user, system accounts, inactive accounts."""
        if target.is_anonymous or target.is_system_principal or target.status != "active":
            raise _invalid("AI usage can be logged only for an active user", "on_behalf_of", "ai_usage_target_invalid")

    @staticmethod
    async def is_active_member(session: AsyncSession, target: User, project: Project) -> bool:
        """Whether *target* is active and a member of *project*, directly or through a user group."""
        from specivo.services.permission_service import member_principal_clause

        if target.status != "active" or target.is_anonymous:
            return False
        found = await session.scalar(
            select(exists().where(Member.project_id == project.id, member_principal_clause(target.id)))
        )
        return bool(found)

    async def require_can_log(
        self,
        session: AsyncSession,
        actor: User,
        target: User,
        project: Project | None,
        issue_id: int | None = None,
        *,
        request: Request | None = None,
    ) -> None:
        """Raise unless *actor* may log AI usage in *target*'s name in *project* (None: unassigned).

        - 404 for a project the actor cannot reach, and for an issue that does
          not exist or that the actor cannot see;
        - 403 without ``log_ai_usage`` (own usage) or
          ``log_ai_usage_for_others`` (someone else's) there, and for
          unassigned usage in someone else's name unless the actor is an
          administrator;
        - 422 ``ai_usage_target_not_member`` when the target is not an active
          member of the project; 422 for an issue of another project, or an
          issue without a project.
        """
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import Permission, check_permission
        from specivo.services.project_service import ProjectService

        if actor.is_anonymous:
            raise PermissionDeniedError("Sign in to log AI usage")
        on_behalf = target.id != actor.id
        if not on_behalf:
            self.require_valid_target(target)
        if project is None:
            if issue_id is not None:
                raise ValidationError(message="An issue needs its project", field="issue_id")
            if on_behalf:
                if not is_global_admin(actor):
                    raise on_behalf_refused()
                self.require_valid_target(target)
            return
        await ProjectService().require_project_access(session, project, actor)
        permission = Permission.LOG_AI_USAGE_FOR_OTHERS if on_behalf else Permission.LOG_AI_USAGE
        if not await check_permission(actor, project.id, permission, session, request=request):
            # On behalf: the same refusal whatever the target, before anything about it is checked.
            raise (
                on_behalf_refused()
                if on_behalf
                else PermissionDeniedError(f"Permission '{permission}' denied for this project")
            )
        if on_behalf:
            self.require_valid_target(target)
        if on_behalf and not await self.is_active_member(session, target, project):
            raise _invalid(
                "The user is not an active member of this project",
                "on_behalf_of",
                "ai_usage_target_not_member",
            )
        if issue_id is not None:
            issue = await IssueService().get_by_id(session, issue_id, user=actor)
            if issue.project_id != project.id:
                raise ValidationError(message="The issue belongs to another project", field="issue_id")

    async def may_log_for_others(self, session: AsyncSession, actor: User, project: Project | None) -> bool:
        """Whether *actor* may log someone else's usage in *project* (None: unassigned, administrators only)."""
        from specivo.services.permission_service import Permission, check_permission

        if actor.is_anonymous:
            return False
        if project is None:
            return is_global_admin(actor)
        return await check_permission(actor, project.id, Permission.LOG_AI_USAGE_FOR_OTHERS, session)

    async def may_log_for_others_anywhere(self, session: AsyncSession, actor: User) -> bool:
        """Whether *actor* may log someone else's usage in at least one project (or unassigned, as an admin).

        Narrowed to the authenticating API key's project limit, like
        ``check_permission``.
        """
        from specivo.services.permission_service import (
            Permission,
            member_principal_clause,
            projects_granting,
        )

        if actor.is_anonymous:
            return False
        if is_global_admin(actor):
            return True
        stmt = select(Project)
        if not actor.is_admin:
            stmt = stmt.where(Project.id.in_(select(Member.project_id).where(member_principal_clause(actor.id))))
        projects = (await session.execute(stmt)).scalars().all()
        return bool(await projects_granting(session, actor, projects, Permission.LOG_AI_USAGE_FOR_OTHERS))

    async def loggable_projects(self, session: AsyncSession, actor: User) -> list[Project]:
        """The projects *actor* may log their own AI usage in (``log_ai_usage``), by name.

        For pickers: the manual entry form and the bulk reassignment.
        """
        from specivo.services.permission_service import Permission, projects_granting
        from specivo.services.project_service import ProjectService

        if actor.is_anonymous:
            return []
        projects, _total = await ProjectService().list_projects(session, actor, limit=1000)
        granted = await projects_granting(session, actor, projects, Permission.LOG_AI_USAGE)
        return [p for p in projects if p.id in granted]

    async def resolve_target(
        self, session: AsyncSession, actor: User, login: str | None, project: Project | None
    ) -> User:
        """The user named by *login* (``on_behalf_of``) for usage logged in *project*; the actor when empty.

        Login enumeration: whoever may not log for others in *project*
        (``may_log_for_others``) gets the one refusal of
        :func:`on_behalf_refused` for every login but their own, whether it
        exists, is inactive, is not a member or is fine. Only those who may
        learn the difference get ``ai_usage_target_unknown``; the target's
        validity and membership are checked by ``require_can_log``.
        """
        name = (login or "").strip()
        if not name:
            return actor
        if name.lower() == actor.login.lower():
            return actor
        if not await self.may_log_for_others(session, actor, project):
            raise on_behalf_refused()
        user = await self.user_by_login(session, name)
        if user is None:
            raise _invalid(f"No user with the login '{name}'", "on_behalf_of", "ai_usage_target_unknown")
        return user

    @staticmethod
    async def user_by_login(session: AsyncSession, login: str) -> User | None:
        """The user with *login* (any case), or None."""
        stmt = select(User).where(func.lower(User.login) == login.strip().lower())
        return (await session.execute(stmt)).scalar_one_or_none()

    # -- people of a report ------------------------------------------------------------

    async def people_clause(self, session: AsyncSession, viewer: User, project: Project | None) -> ColumnElement[bool]:
        """The users a report's person filter may name for *viewer*: never a system account.

        With *project*: the people with an entry there that *viewer* may read
        (excluded ones too, the AI usage tab lists them), so the filter never
        names more people than the report or the entry list could show. Without
        one (the instance report, administrators only): everyone with an entry
        or a subscription, whose fee the instance report counts.
        """
        from specivo.models.ai_subscription import AiSubscription
        from specivo.services.anonymous_user_service import real_users_clause

        if project is None:
            if not is_global_admin(viewer):
                raise PermissionDeniedError("Only administrators can see the instance AI spend report")
            held = or_(
                exists().where(AiUsageEntry.user_id == User.id),
                exists().where(AiSubscription.user_id == User.id),
            )
            return and_(real_users_clause(), held)
        conditions = await self._list_conditions(session, viewer, project=project)
        return and_(real_users_clause(), exists().where(AiUsageEntry.user_id == User.id, *conditions))

    async def search_people(
        self,
        session: AsyncSession,
        viewer: User,
        project: Project | None,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[User]:
        """A page of :meth:`people_clause`'s users, by display name; *query* matches login or name (substring)."""
        stmt = select(User).where(await self.people_clause(session, viewer, project))
        q = (query or "").strip()
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(
                or_(User.login.ilike(pattern, escape="\\"), User.display_name.ilike(pattern, escape="\\"))
            )
        display = func.lower(func.coalesce(func.nullif(User.display_name, ""), User.login))
        stmt = stmt.order_by(display, func.lower(User.login), User.id).offset(offset).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def search_models(
        self,
        session: AsyncSession,
        viewer: User,
        project: Project | None,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ModelSuggestion]:
        """A page of model names for a report's model filter, by name: the price list's and the usage's.

        *query* matches the name or an alias (substring, any case). Usage
        names count only from the entries *viewer* may read: in *project*, or
        on the instance report (administrators only, 403 otherwise). Each
        name carries the provider and the other names the price list knows it
        by, which the filter also matches (:meth:`model_names`).
        """
        if project is None and not is_global_admin(viewer):
            raise PermissionDeniedError("Only administrators can see the instance AI spend report")
        q = (query or "").strip().lower()
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        priced = select(AiModelPrice.model.label("model"), AiModelPrice.provider.label("provider"))
        used = select(AiUsageEntry.model.label("model"), AiUsageEntry.provider.label("provider")).where(
            AiUsageEntry.model != "", *await self._list_conditions(session, viewer, project=project)
        )
        if q:
            alias_text = func.lower(func.array_to_string(AiModelPrice.aliases, " "))
            priced = priced.where(
                or_(func.lower(AiModelPrice.model).like(pattern, escape="\\"), alias_text.like(pattern, escape="\\"))
            )
            used = used.where(func.lower(AiUsageEntry.model).like(pattern, escape="\\"))
        names = priced.union(used).subquery()
        stmt = (
            select(names.c.model, func.min(names.c.provider))
            .group_by(names.c.model)
            .order_by(func.lower(names.c.model), names.c.model)
            .offset(offset)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        aliases: dict[str, set[str]] = {}
        if rows:
            alias_rows = await session.execute(
                select(AiModelPrice.model, AiModelPrice.aliases).where(AiModelPrice.model.in_([m for m, _p in rows]))
            )
            for model, names_ in alias_rows.all():
                aliases.setdefault(model, set()).update(names_ or [])
        return [
            ModelSuggestion(model=m, provider=p or "", aliases=tuple(sorted(aliases.get(m, set()) - {m})))
            for m, p in rows
        ]

    async def person_in_scope(
        self,
        session: AsyncSession,
        viewer: User,
        project: Project | None,
        *,
        user_id: int | None = None,
        login: str = "",
    ) -> User | None:
        """The user *user_id* (or *login*, any case) when the person filter may name them for *viewer*, else None."""
        stmt = select(User).where(await self.people_clause(session, viewer, project))
        if user_id is not None:
            stmt = stmt.where(User.id == user_id)
        elif login.strip():
            stmt = stmt.where(func.lower(User.login) == login.strip().lower())
        else:
            return None
        return (await session.execute(stmt.limit(1))).scalar_one_or_none()

    async def can_edit(self, session: AsyncSession, actor: User, entry: AiUsageEntry) -> bool:
        """Whether *actor* may change, exclude or include *entry*: its user, ``manage_ai_usage`` or an admin."""
        from specivo.services.permission_service import Permission, check_permission

        if actor.is_anonymous:
            return False
        if entry.project_id is None:
            if is_global_admin(actor):
                return True
        elif admin_reaches(actor, entry.project_id):
            return True
        if entry.user_id == actor.id:
            return True
        if entry.project_id is None:
            return False
        return await check_permission(actor, entry.project_id, Permission.MANAGE_AI_USAGE, session)

    def can_delete_locked(self, actor: User, entry: AiUsageEntry) -> bool:
        """Reporter and import entries are hard-deleted by administrators only."""
        if entry.project_id is None:
            return is_global_admin(actor)
        return admin_reaches(actor, entry.project_id)

    # -- validation ---------------------------------------------------------------

    async def validate(self, session: AsyncSession, data: AiUsageInput, *, source: str) -> None:
        """Raise the 422 the content of *data* would get (tool, model, dates, tokens, cost, texts); no authorization."""
        await self._validated(session, data, source=source)

    async def _validated(self, session: AsyncSession, data: AiUsageInput, *, source: str) -> dict[str, Any]:
        from specivo.services.ai_subscription_service import AiToolService

        if source not in AI_USAGE_SOURCES:
            raise _invalid(f"Unknown source {source!r}", "source")
        tool = await AiToolService().ensure(session, data.tool)
        provider = normalize_provider(data.provider) if data.provider else None
        if data.provider and not provider:
            raise _invalid("Enter a provider such as anthropic or openai", "provider")
        provider = provider or tool.default_provider
        model = model_name_for(data.model or "", provider or "")
        if not model or len(model) > 200:
            raise _invalid("Enter a model name of at most 200 characters", "model")
        if not isinstance(data.usage_date, date) or isinstance(data.usage_date, datetime):
            raise _invalid("usage_date must be a date", "usage_date")
        latest = (utcnow() + FUTURE_TOLERANCE).date()
        if data.usage_date < EARLIEST_USAGE_DATE or data.usage_date > latest:
            raise _invalid(f"usage_date must lie between {EARLIEST_USAGE_DATE} and {latest}", "usage_date")
        started = self._instant(data.started_at, "started_at")
        ended = self._instant(data.ended_at, "ended_at")
        if started is not None and started.date() != data.usage_date:
            raise _invalid("started_at must fall on usage_date (a UTC day)", "started_at")
        if started is not None and ended is not None and ended < started:
            raise _invalid("ended_at must not be before started_at", "ended_at")
        try:
            tokens = check_tokens(dict(data.tokens or {}), "tokens")
            long_context = check_tokens(dict(data.long_context), "long_context") if data.long_context else None
        except ValueError as exc:
            raise _invalid(str(exc), "tokens") from None
        try:
            check_bucket_totals(tokens, long_context)
        except ValueError as exc:
            raise _invalid(str(exc), "long_context") from None
        if long_context is not None and not any(long_context.values()):
            long_context = None
        count = data.request_count
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > MAX_REQUEST_COUNT:
            raise _invalid(f"request_count must be between 0 and {MAX_REQUEST_COUNT}", "request_count")
        if data.cost_type is not None and data.cost_type not in AI_USAGE_COST_TYPES:
            raise _invalid(f"cost_type must be one of {', '.join(AI_USAGE_COST_TYPES)}", "cost_type")
        reported = await self._reported_cost(session, data.reported_cost)
        method = data.attribution_method or ("explicit" if data.project_id is not None else "none")
        if method not in AI_USAGE_ATTRIBUTION_METHODS:
            raise _invalid("Unknown attribution method", "attribution_method")
        values: dict[str, Any] = {
            "tool_slug": tool.slug,
            "provider": provider,
            "model": model,
            "usage_date": data.usage_date,
            "started_at": started,
            "ended_at": ended,
            "request_count": count,
            **tokens,
            "long_context": long_context,
            "reported_cost_amount": reported.amount if reported is not None else None,
            "reported_cost_currency": reported.currency if reported is not None else None,
            "attribution_method": method,
        }
        for name, max_length in CONTEXT_MAX_LENGTHS.items():
            values[name] = self._text(getattr(data, name), name, max_length)
        values["comments"] = self._text(data.comments, "comments", COMMENTS_MAX_LENGTH)
        key = (data.idempotency_key or "").strip() or None
        if key is not None and (len(key) > IDEMPOTENCY_KEY_MAX_LENGTH or not _KEY_RE.match(key)):
            raise _invalid(
                f"idempotency_key is at most {IDEMPOTENCY_KEY_MAX_LENGTH} printable characters without spaces",
                "idempotency_key",
            )
        values["idempotency_key"] = key
        return values

    @staticmethod
    def _instant(value: datetime | None, field_name: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise _invalid(f"{field_name} needs a time zone offset, for example 2026-11-06T09:30:00Z", field_name)
        return value.astimezone(UTC)

    @staticmethod
    def _text(value: str | None, field_name: str, max_length: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise _invalid(f"{field_name} must be text", field_name)
        text = value.strip()
        if "\x00" in text:
            raise _invalid(f"{field_name} must not contain NUL characters", field_name)
        if len(text) > max_length:
            raise _invalid(f"{field_name} can have at most {max_length} characters", field_name)
        return text or None

    @staticmethod
    async def _reported_cost(session: AsyncSession, value: Money | None) -> Money | None:
        if value is None:
            return None
        if not isinstance(value, Money):
            raise _invalid("reported_cost must be an amount with a currency", "reported_cost")
        if value.amount < 0:
            raise _invalid("reported_cost must not be negative", "reported_cost")
        if await session.get(Currency, value.currency) is None:
            raise _invalid(f"Unknown currency '{value.currency}'", "reported_cost", "currency_unknown")
        return value

    async def _project(self, session: AsyncSession, project_id: int | None) -> Project | None:
        if project_id is None:
            return None
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        return project

    # -- costs ----------------------------------------------------------------------

    async def _default_cost_type(
        self, session: AsyncSession, user_id: int, requested: str | None, values: Mapping[str, Any]
    ) -> tuple[str, int | None]:
        """The cost type and covering subscription: the requested type, else the user's subscription, else unknown."""
        from specivo.services.ai_subscription_service import AiSubscriptionService

        if requested is not None and requested != "subscription":
            return requested, None
        at = priced_at_for(values["usage_date"], values["started_at"])
        coverage = await AiSubscriptionService().subscription_for(
            session, user_id, values["tool_slug"], values["provider"], at
        )
        if coverage is not None:
            return "subscription", coverage.subscription.id
        return (requested or "unknown"), None

    async def apply_costs(self, session: AsyncSession, entry: AiUsageEntry) -> None:
        """Price *entry* at its ``priced_at`` with the prices valid then, and store the snapshot.

        Uses the stored cost type and subscription. The price row comes from
        ``AiModelPriceService.resolve`` (admin before seed, never the latest
        price); a local model's row prices the marginal cost and its baseline
        model, resolved at the same instant, the API equivalent.
        """
        from specivo.services.currency_service import CurrencyService

        priced_at = priced_at_for(entry.usage_date, entry.started_at)
        resolved = await _prices.resolve(session, entry.model, priced_at, provider=entry.provider)
        price_row: AiModelPrice | None = resolved.price if resolved is not None else None
        baseline: AiModelPrice | None = None
        if price_row is not None and price_row.is_local and price_row.baseline_model:
            baseline_resolved = await _prices.resolve(session, price_row.baseline_model, priced_at)
            baseline = baseline_resolved.price if baseline_resolved is not None else None
        subscription_currency: str | None = None
        if entry.subscription_id is not None:
            subscription = await session.get(AiSubscription, entry.subscription_id)
            subscription_currency = subscription.currency_code if subscription is not None else None
        costs = compute_costs(
            CostInputs(
                cost_type=entry.cost_type,
                tokens=TokenCounts.from_mapping(entry.tokens()),
                long_context=token_counts(entry.long_context),
                price=price_row.to_price() if price_row is not None else None,
                is_local_price=bool(price_row is not None and price_row.is_local),
                baseline_price=baseline.to_price() if baseline is not None else None,
                reported_cost=entry.reported_cost,
                reported_by_person=entry.source == "manual",
                subscription_currency=subscription_currency,
                zero_currency=await CurrencyService().get_instance_reporting_currency(session),
            )
        )
        entry.priced_at = priced_at
        entry.price_id = price_row.id if price_row is not None else None
        entry.marginal_amount = costs.marginal.amount if costs.marginal is not None else None
        entry.marginal_currency = costs.marginal.currency if costs.marginal is not None else None
        entry.api_equivalent_amount = costs.api_equivalent.amount if costs.api_equivalent is not None else None
        entry.api_equivalent_currency = costs.api_equivalent.currency if costs.api_equivalent is not None else None
        entry.cost_basis = costs.cost_basis

    # -- writes ----------------------------------------------------------------------

    async def log(
        self,
        session: AsyncSession,
        actor: User,
        data: AiUsageInput,
        *,
        target: User | None = None,
        source: str = "manual",
        api_key_id: int | None = None,
        request: Request | None = None,
    ) -> AiUsageEntry:
        """Log one entry written by a person or a client (``manual``, ``api``, ``mcp``), in *target*'s name.

        *target* defaults to the actor. Reporter and import entries go
        through :meth:`upsert`. Audited when the target is someone else.
        """
        if source in LOCKED_SOURCES:
            raise _invalid("Reporter and import entries are written with upsert", "source")
        target = target or actor
        values = await self._validated(session, data, source=source)
        project = await self._project(session, data.project_id)
        await self.require_can_log(session, actor, target, project, data.issue_id, request=request)
        return await self._insert(session, actor, target, data, values, source, api_key_id, request)

    async def upsert(
        self,
        session: AsyncSession,
        actor: User,
        data: AiUsageInput,
        *,
        target: User | None = None,
        source: str = "reporter",
        api_key_id: int | None = None,
        request: Request | None = None,
        reattribute: bool = False,
    ) -> UpsertResult:
        """Create or replace the bucket ``(target, idempotency_key)`` with the totals sent.

        - Identical content: ``unchanged``, nothing written.
        - Otherwise the usage, tokens, context and costs are replaced, the
          entry re-priced at its ``priced_at`` and ``revision`` bumped. The
          project and issue are taken from the record only while the entry is
          unassigned: an attributed entry (by the reporter or a person) keeps
          its attribution, unless *reattribute* says the ingest decided to
          refine or replace it (never for an entry a person moved, whose
          ``attribution_method`` is ``manual``). Comments and the exclusion
          are never touched.
        - An existing entry is authorized against its stored project and user
          (see ``_replace``); a record naming another project than the stored
          one is 409 ``ai_usage_attribution_conflict``.
        - A key the target holds from another reporter: 409 ``key_conflict``.
        """
        target = target or actor
        if not (data.idempotency_key or "").strip():
            raise _invalid("An idempotency key is required", "idempotency_key")
        values = await self._validated(session, data, source=source)
        project = await self._project(session, data.project_id)
        existing = await self._by_key(session, target.id, values["idempotency_key"])
        if existing is None:
            await self.require_can_log(session, actor, target, project, data.issue_id, request=request)
            savepoint = await session.begin_nested()
            try:
                entry = await self._insert(session, actor, target, data, values, source, api_key_id, request)
            except IntegrityError:
                await savepoint.rollback()
                existing = await self._by_key(session, target.id, values["idempotency_key"])
                if existing is None:
                    raise
            else:
                if savepoint.is_active:
                    await savepoint.commit()
                return UpsertResult(entry, "created")
        return await self._replace(
            session, actor, target, existing, data, values, project, request, reattribute=reattribute
        )

    async def _by_key(self, session: AsyncSession, user_id: int, key: str) -> AiUsageEntry | None:
        stmt = (
            select(AiUsageEntry)
            .where(AiUsageEntry.user_id == user_id, AiUsageEntry.idempotency_key == key)
            .with_for_update()
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _insert(
        self,
        session: AsyncSession,
        actor: User,
        target: User,
        data: AiUsageInput,
        values: dict[str, Any],
        source: str,
        api_key_id: int | None,
        request: Request | None,
    ) -> AiUsageEntry:
        cost_type, subscription_id = await self._default_cost_type(session, target.id, data.cost_type, values)
        entry = AiUsageEntry(
            **values,
            project_id=data.project_id,
            issue_id=data.issue_id,
            user_id=target.id,
            reporter_user_id=actor.id,
            reporter_api_key_id=api_key_id,
            source=source,
            cost_type=cost_type,
            subscription_id=subscription_id,
            revision=1,
        )
        await self.apply_costs(session, entry)
        session.add(entry)
        await session.flush()
        if target.id != actor.id:
            details = await self._details(session, entry, target)
            details["reporter_api_key_id"] = api_key_id
            await self._audit(session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER, actor, entry, details, request)
        return entry

    async def _replace(
        self,
        session: AsyncSession,
        actor: User,
        target: User,
        entry: AiUsageEntry,
        data: AiUsageInput,
        values: dict[str, Any],
        project: Project | None,
        request: Request | None,
        *,
        reattribute: bool = False,
    ) -> UpsertResult:
        """Replace the totals of an existing bucket; authorized against the **stored** entry.

        The gate is ``require_can_log`` in the entry's own project and its
        user's name, so a replay cannot rewrite an entry in a project where
        the actor may not log. A record naming a project while the entry is
        unassigned attributes it, after the same check in that project. A
        record naming another project than the one the entry has is refused
        (409 ``ai_usage_attribution_conflict``): reporters never move entries,
        people do (``update``). A record without a project leaves the stored
        attribution as it is. An update by someone other than the entry's user
        is audited like logging in their name.

        With *reattribute* (the ingest's decision to refine a project-only
        entry or replace an inferred attribution), the record's project, issue
        and method replace the stored ones, after ``require_can_log`` in both
        the stored and the new project. An entry a person moved
        (``attribution_method`` ``manual``) is never reattributed or assigned.
        """
        if entry.reporter_user_id != actor.id:
            raise AppError(
                code="key_conflict",
                message="This idempotency key belongs to an entry another account reported",
                status_code=409,
                field="idempotency_key",
            )
        manual = entry.attribution_method == MANUAL_ATTRIBUTION
        moves = reattribute and not manual and project is not None and entry.project_id is not None
        assigns = (entry.project_id is None and project is not None and not manual) or moves
        if not moves and project is not None and entry.project_id is not None and project.id != entry.project_id:
            raise AppError(
                code="ai_usage_attribution_conflict",
                message="This entry belongs to another project; send it without a project, or move it by hand",
                status_code=409,
                field="project_id",
            )
        if moves:
            stored = await self._project(session, entry.project_id)
            await self.require_can_log(session, actor, target, stored, None, request=request)
        if assigns:
            await self.require_can_log(session, actor, target, project, data.issue_id, request=request)
        else:
            stored = await self._project(session, entry.project_id)
            await self.require_can_log(session, actor, target, stored, None, request=request)

        cost_type, subscription_id = await self._default_cost_type(session, entry.user_id, data.cost_type, values)
        incoming = {k: v for k, v in values.items() if k not in ("comments", "attribution_method", "idempotency_key")}
        incoming["cost_type"] = cost_type
        incoming["subscription_id"] = subscription_id
        if assigns:
            incoming["project_id"] = data.project_id
            incoming["issue_id"] = data.issue_id
            incoming["attribution_method"] = values["attribution_method"]
        changed = {name: value for name, value in incoming.items() if getattr(entry, name) != value}
        if not changed:
            return UpsertResult(entry, "unchanged")
        for name, value in changed.items():
            setattr(entry, name, value)
        await self.apply_costs(session, entry)
        entry.revision += 1
        await session.flush()
        if actor.id != entry.user_id:
            details = await self._details(session, entry, target)
            details["reporter_api_key_id"] = entry.reporter_api_key_id
            details["status"] = "updated"
            details["revision"] = entry.revision
            await self._audit(session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER, actor, entry, details, request)
        return UpsertResult(entry, "updated")

    async def update(
        self,
        session: AsyncSession,
        actor: User,
        entry: AiUsageEntry,
        changes: Mapping[str, Any],
        *,
        request: Request | None = None,
    ) -> AiUsageEntry:
        """Change an entry; see ``can_edit`` for who may, and the module docstring for what.

        *changes* maps field names (``project_id``, ``issue_id``, ``comments``,
        and for manual, API and MCP entries the fields of :class:`AiUsageInput`)
        to new values. A locked field on a reporter or import entry is a 422
        ``ai_usage_field_locked``. Audited when the actor is not the entry's user.
        """
        if not await self.can_edit(session, actor, entry):
            raise PermissionDeniedError("You do not have permission to edit this AI usage entry")
        unknown = set(changes) - ATTRIBUTION_FIELDS - USAGE_FIELDS
        if unknown:
            raise _invalid(f"Unknown fields: {', '.join(sorted(unknown))}", sorted(unknown)[0])
        if entry.is_locked:
            locked = sorted(set(changes) & USAGE_FIELDS)
            if locked:
                raise _invalid(
                    "Reported usage keeps its tokens, model and tool; only the project, the issue and the comments "
                    "can change",
                    locked[0],
                    "ai_usage_field_locked",
                )

        before = self._editable_state(entry)
        project_id = changes.get("project_id", entry.project_id)
        issue_id = changes.get("issue_id", entry.issue_id if project_id == entry.project_id else None)
        if "project_id" in changes or "issue_id" in changes:
            await self._require_can_move(session, actor, entry, project_id, issue_id, request=request)
            if (project_id, issue_id) != (entry.project_id, entry.issue_id):
                # A person moved it: replays of the bucket never change it again.
                entry.project_id = project_id
                entry.issue_id = issue_id
                entry.attribution_method = MANUAL_ATTRIBUTION
        if "comments" in changes:
            entry.comments = self._text(changes["comments"], "comments", COMMENTS_MAX_LENGTH)

        usage_changes = set(changes) & USAGE_FIELDS
        if usage_changes:
            data = self._input_of(entry)
            for name in usage_changes:
                setattr(data, name, changes[name])
            values = await self._validated(session, data, source=entry.source)
            for name in (
                "tool_slug",
                "provider",
                "model",
                "usage_date",
                "started_at",
                "ended_at",
                "request_count",
                *TOKEN_CLASSES,
                "long_context",
                "reported_cost_amount",
                "reported_cost_currency",
                "external_session_id",
                "git_branch",
                "repo_hint",
            ):
                setattr(entry, name, values[name])
            if usage_changes & _COST_FIELDS:
                requested = changes.get("cost_type", entry.cost_type)
                if requested is not None and requested not in AI_USAGE_COST_TYPES:
                    raise _invalid(f"cost_type must be one of {', '.join(AI_USAGE_COST_TYPES)}", "cost_type")
                if "cost_type" in changes and changes["cost_type"] is None:
                    requested = None
                cost_type, subscription_id = await self._default_cost_type(session, entry.user_id, requested, values)
                entry.cost_type = cost_type
                entry.subscription_id = subscription_id
                await self.apply_costs(session, entry)

        after = self._editable_state(entry)
        diff = {name: {"old": before[name], "new": after[name]} for name in before if before[name] != after[name]}
        if not diff:
            return entry
        if "comments" in diff:
            diff["comments"] = {"changed": True}
        entry.revision += 1
        await session.flush()
        if actor.id != entry.user_id:
            target = await session.get(User, entry.user_id)
            details = await self._details(session, entry, target)
            details["changes"] = diff
            await self._audit(session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER, actor, entry, details, request)
        return entry

    async def _require_can_move(
        self,
        session: AsyncSession,
        actor: User,
        entry: AiUsageEntry,
        project_id: int | None,
        issue_id: int | None,
        *,
        request: Request | None = None,
    ) -> None:
        """Raise unless *actor* may set the entry's project to *project_id* and its issue to *issue_id*.

        Moving to another project (or making the entry unassigned) is checked
        exactly like logging the entry there in its user's name
        (``require_can_log``): ``log_ai_usage`` for one's own entry,
        ``log_ai_usage_for_others`` plus an active membership of the entry's
        user for someone else's, administrators only for someone else's
        unassigned usage. Within the same project only the issue is checked.
        """
        from specivo.services.issue_service import IssueService
        from specivo.services.project_service import ProjectService

        project = await self._project(session, project_id)
        if project_id != entry.project_id:
            target = await session.get(User, entry.user_id)
            if target is None:
                raise NotFoundError(f"User {entry.user_id} not found")
            await self.require_can_log(session, actor, target, project, issue_id, request=request)
            return
        if project is None:
            if issue_id is not None:
                raise ValidationError(message="An issue needs its project", field="issue_id")
            return
        await ProjectService().require_project_access(session, project, actor)
        if issue_id is not None:
            issue = await IssueService().get_by_id(session, issue_id, user=actor)
            if issue.project_id != project.id:
                raise ValidationError(message="The issue belongs to another project", field="issue_id")

    @staticmethod
    def _input_of(entry: AiUsageEntry) -> AiUsageInput:
        return AiUsageInput(
            tool=entry.tool_slug,
            model=entry.model,
            usage_date=entry.usage_date,
            provider=entry.provider,
            tokens=entry.tokens(),
            long_context=entry.long_context,
            started_at=entry.started_at,
            ended_at=entry.ended_at,
            request_count=entry.request_count,
            cost_type=entry.cost_type,
            reported_cost=entry.reported_cost,
            project_id=entry.project_id,
            issue_id=entry.issue_id,
            attribution_method=entry.attribution_method,
            external_session_id=entry.external_session_id,
            git_branch=entry.git_branch,
            repo_hint=entry.repo_hint,
            client_name=entry.client_name,
            client_version=entry.client_version,
            comments=entry.comments,
            idempotency_key=entry.idempotency_key,
        )

    @staticmethod
    def _editable_state(entry: AiUsageEntry) -> dict[str, Any]:
        reported = entry.reported_cost
        return {
            "project_id": entry.project_id,
            "issue_id": entry.issue_id,
            "comments": entry.comments,
            "tool": entry.tool_slug,
            "provider": entry.provider,
            "model": entry.model,
            "usage_date": _iso(entry.usage_date),
            "started_at": _iso(entry.started_at),
            "ended_at": _iso(entry.ended_at),
            "request_count": entry.request_count,
            "tokens": entry.tokens(),
            "long_context": entry.long_context,
            "cost_type": entry.cost_type,
            "reported_cost": (
                {"amount": format_decimal_plain(reported.amount), "currency": reported.currency} if reported else None
            ),
            "external_session_id": entry.external_session_id,
            "git_branch": entry.git_branch,
            "repo_hint": entry.repo_hint,
        }

    async def set_excluded(
        self,
        session: AsyncSession,
        actor: User,
        entry: AiUsageEntry,
        excluded: bool,
        *,
        request: Request | None = None,
    ) -> AiUsageEntry:
        """Exclude *entry* from reports (or include it again); audited. Later upserts keep the exclusion."""
        if not await self.can_edit(session, actor, entry):
            raise PermissionDeniedError("You do not have permission to change this AI usage entry")
        if excluded == entry.is_excluded:
            return entry
        entry.excluded_at = utcnow() if excluded else None
        entry.excluded_by_id = actor.id if excluded else None
        await session.flush()
        target = await session.get(User, entry.user_id)
        event = AuditEvent.AI_USAGE_EXCLUDED if excluded else AuditEvent.AI_USAGE_INCLUDED
        await self._audit(session, event, actor, entry, await self._details(session, entry, target), request)
        return entry

    async def delete(
        self, session: AsyncSession, actor: User, entry: AiUsageEntry, *, request: Request | None = None
    ) -> None:
        """Delete *entry*; audited.

        Manual, API and MCP entries: whoever may edit them. Reporter and import
        entries: administrators only (everyone else excludes them).
        """
        if entry.is_locked:
            if not self.can_delete_locked(actor, entry):
                raise PermissionDeniedError("Reported AI usage cannot be deleted; exclude it instead")
        elif not await self.can_edit(session, actor, entry):
            raise PermissionDeniedError("You do not have permission to delete this AI usage entry")
        target = await session.get(User, entry.user_id)
        details = await self._details(session, entry, target)
        details["tokens"] = entry.tokens()
        details["long_context"] = entry.long_context
        details["cost_type"] = entry.cost_type
        await session.delete(entry)
        await session.flush()
        await self._audit(session, AuditEvent.AI_USAGE_DELETED, actor, entry, details, request)

    # -- REST and MCP surfaces ------------------------------------------------------

    async def create_for(
        self,
        session: AsyncSession,
        actor: User,
        data: AiUsageInput,
        *,
        project: Project | None,
        on_behalf_of: str | None = None,
        source: str = "manual",
        api_key_id: int | None = None,
        request: Request | None = None,
    ) -> AiUsageEntry:
        """Log *data* in *project* (None: unassigned), in the name of the login *on_behalf_of* or the actor's.

        The login is resolved by :meth:`resolve_target`, so whoever may not log
        for others there gets the same refusal for every login.
        """
        target = await self.resolve_target(session, actor, on_behalf_of, project)
        data.project_id = project.id if project is not None else None
        if data.project_id is None:
            data.attribution_method = "none"
        return await self.log(
            session, actor, data, target=target, source=source, api_key_id=api_key_id, request=request
        )

    async def project_id_for_key(self, session: AsyncSession, actor: User, key: str | None) -> int | None:
        """The id of the project *key* the actor can reach (404 otherwise); None for no key (unassigned)."""
        from specivo.services.project_service import ProjectService

        if key is None or not key.strip():
            return None
        project = await ProjectService().get_by_key(session, key.strip().upper())
        await ProjectService().require_project_access(session, project, actor)
        return project.id

    async def references(self, session: AsyncSession, viewer: User, entries: list[AiUsageEntry]) -> EntryReferences:
        """The users, project keys and issue keys *entries* name, in up to four queries.

        Issues *viewer* cannot see are left out. Key names are loaded only for
        entries reported for someone else.
        """
        from specivo.models.auth import ApiKey
        from specivo.services.issue_service import IssueService

        user_ids = {e.user_id for e in entries} | {e.reporter_user_id for e in entries}
        project_ids = {e.project_id for e in entries if e.project_id is not None}
        issue_ids = {e.issue_id for e in entries if e.issue_id is not None}
        key_ids = {
            e.reporter_api_key_id
            for e in entries
            if e.reporter_api_key_id is not None and e.reporter_user_id != e.user_id
        }
        refs = EntryReferences()
        if key_ids:
            rows_k = await session.execute(select(ApiKey.id, ApiKey.name).where(ApiKey.id.in_(key_ids)))
            refs.key_names = {kid: name for kid, name in rows_k.all()}
        if user_ids:
            rows = (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
            refs.users = {u.id: u for u in rows}
        if project_ids:
            rows_p = await session.execute(select(Project.id, Project.key).where(Project.id.in_(project_ids)))
            refs.project_keys = {pid: key for pid, key in rows_p.all()}
        if issue_ids:
            stmt = select(Issue.id, Issue.project_key, Issue.sequence_number).where(Issue.id.in_(issue_ids))
            clause = await IssueService().visible_issues_clause(session, viewer)
            if clause is not None:
                stmt = stmt.where(clause)
            refs.issue_keys = {iid: f"{key}-{number}" for iid, key, number in (await session.execute(stmt)).all()}
        return refs

    # -- range recompute ------------------------------------------------------------

    async def model_names(self, session: AsyncSession, provider: str | None, model: str) -> list[str]:
        """The normalised *model* and every alias its price rows carry (entries may use either name)."""
        provider_slug = normalize_provider(provider) if provider else None
        name = model_name_for(model, provider_slug or "")
        names = {name}
        stmt = select(AiModelPrice.aliases).where(AiModelPrice.model == name)
        if provider_slug:
            stmt = stmt.where(AiModelPrice.provider == provider_slug)
        for (aliases,) in (await session.execute(stmt)).all():
            names.update(aliases or [])
        return sorted(n for n in names if n)

    async def _range_conditions(
        self,
        session: AsyncSession,
        start: datetime,
        end: datetime,
        provider: str | None,
        model: str | None,
    ) -> list[ColumnElement[bool]]:
        start = self._instant(start, "from") or start
        end = self._instant(end, "to") or end
        if end <= start:
            raise _invalid("The end of the range must be after its start", "to")
        conditions: list[ColumnElement[bool]] = [AiUsageEntry.priced_at >= start, AiUsageEntry.priced_at < end]
        provider_slug = normalize_provider(provider) if provider else None
        if provider and not provider_slug:
            raise _invalid("Enter a provider such as anthropic or openai", "provider")
        if provider_slug:
            conditions.append(AiUsageEntry.provider == provider_slug)
        if model and model.strip():
            conditions.append(AiUsageEntry.model.in_(await self.model_names(session, provider_slug, model)))
        return conditions

    async def count_in_range(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        """How many entries a recompute of this range would read."""
        conditions = await self._range_conditions(session, start, end, provider, model)
        return int(await session.scalar(select(func.count()).select_from(AiUsageEntry).where(*conditions)) or 0)

    async def recompute_range(
        self,
        session: AsyncSession,
        *,
        actor_id: int | None,
        start: datetime,
        end: datetime,
        provider: str | None = None,
        model: str | None = None,
        request: Request | None = None,
    ) -> RecomputeOutcome:
        """Re-price the entries whose ``priced_at`` lies in ``[start, end)``; one audit event.

        Each entry is resolved at its **own** ``priced_at`` (never the latest
        price), keeping its cost type and subscription. Entries whose costs
        change get ``revision`` bumped. *model* also matches the aliases of its
        price rows. Runs in batches by id; the caller commits.
        """
        conditions = await self._range_conditions(session, start, end, provider, model)
        before, after = _Totals(), _Totals()
        entries = changed = 0
        last_id = 0
        while True:
            batch = list(
                (
                    await session.execute(
                        select(AiUsageEntry)
                        .where(*conditions, AiUsageEntry.id > last_id)
                        .order_by(AiUsageEntry.id)
                        .limit(RECOMPUTE_BATCH)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if not batch:
                break
            for entry in batch:
                entries += 1
                before.add(entry)
                snapshot = _cost_snapshot(entry)
                await self.apply_costs(session, entry)
                if _cost_snapshot(entry) != snapshot:
                    entry.revision += 1
                    changed += 1
                after.add(entry)
            last_id = batch[-1].id
            await session.flush()
        outcome = RecomputeOutcome(entries, changed, before.as_details(), after.as_details())
        details: dict[str, Any] = {
            "provider": normalize_provider(provider) if provider else None,
            "models": await self.model_names(session, provider, model) if model and model.strip() else None,
            "from": _iso(start),
            "to": _iso(end),
            "entries": entries,
            "changed": changed,
            "before": outcome.before,
            "after": outcome.after,
        }
        try:
            await _audit.log_ai_usage_change(
                session, AuditEvent.AI_USAGE_COSTS_RECOMPUTED, user_id=actor_id, details=details, request=request
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", AuditEvent.AI_USAGE_COSTS_RECOMPUTED)
        return outcome

    def queue_recompute(
        self,
        *,
        actor_id: int,
        start: datetime,
        end: datetime,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Start the range recompute as a Celery task; returns the task id. 503 when the queue is down."""
        import uuid

        from specivo.tasks.ai_usage import recompute_ai_usage_costs

        task_id = str(uuid.uuid4())
        try:
            recompute_ai_usage_costs.apply_async(
                args=[actor_id, _iso(start), _iso(end), provider or None, model or None],
                task_id=task_id,
                retry=False,
            )
        except Exception as exc:
            raise AppError(
                code="queue_unavailable",
                message="Cannot start the recompute right now: the task queue is unavailable.",
                status_code=503,
            ) from exc
        return task_id

    # -- audit ----------------------------------------------------------------------

    async def _details(self, session: AsyncSession, entry: AiUsageEntry, target: User | None) -> dict[str, Any]:
        project_key: str | None = None
        if entry.project_id is not None:
            project = await session.get(Project, entry.project_id)
            project_key = project.key if project is not None else None
        issue_key: str | None = None
        if entry.issue_id is not None:
            issue = await session.get(Issue, entry.issue_id)
            issue_key = issue.display_key if issue is not None else None
        return {
            "entry_id": entry.id,
            "target_user_id": entry.user_id,
            "target_login": target.login if target is not None else None,
            "source": entry.source,
            "tool": entry.tool_slug,
            "provider": entry.provider,
            "model": entry.model,
            "usage_date": _iso(entry.usage_date),
            "project_key": project_key,
            "issue": issue_key,
        }

    async def _audit(
        self,
        session: AsyncSession,
        event: AuditEvent,
        actor: User,
        entry: AiUsageEntry,
        details: dict[str, Any],
        request: Request | None,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_ai_usage_change(
                session,
                event,
                user_id=actor.id,
                details=details,
                resource_id=entry.id,
                project_id=entry.project_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)


def money_from(amount: object, currency: object) -> Money:
    """A ``Money`` from client values (amount as a decimal string), or a 422 on ``reported_cost``."""
    try:
        return Money(amount, str(currency).strip().upper())  # type: ignore[arg-type]
    except (MoneyError, TypeError, ValueError):
        raise _invalid("reported_cost needs a decimal amount and a currency code", "reported_cost") from None
