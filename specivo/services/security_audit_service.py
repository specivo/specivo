"""Centralized security audit logging service.

Async code writes events with :meth:`SecurityAuditService.log_event`. Code
that cannot await (the SSO event functions, the session revocation primitive)
calls :func:`record_event`, which appends the event to the audit buffer of
the current request: :class:`specivo.core.middleware.AuditBatchMiddleware`
binds that buffer (:func:`bind_audit_context`) and flushes it after the
response. Outside a request, :func:`audit_scope` binds a buffer and writes it
through a DB session.

The log holds security and administrative events and everything agents do;
people's ordinary edits live in issue and wiki history instead. What an
event's ``details`` may hold is set out in ADR-0004 §18: ids, keys, slugs,
enum values, counts, account logins, group names and capped search text,
never tokens, secrets, password material, body text or claim sets.

Client-sent values are cut to their columns by :func:`_bounded` on every
write path, so an oversized ``X-Request-ID`` or ``User-Agent`` loses no event.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.client_ip import get_client_ip
from specivo.models.security_audit import SecurityAuditLog

logger = logging.getLogger(__name__)


class AuditEvent(StrEnum):
    """All valid audit event types stored in SecurityAuditLog.event_type."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    SEARCH_QUERY = "search_query"
    MEMBER_CHANGE = "member_change"
    GROUP_CHANGE = "group_change"
    ACCESS_GRANTED = "access_granted"
    ACCESS_DENIED = "access_denied"
    AUTH_FAILURE = "auth_failure"
    RESOURCE_ACCESS = "resource_access"
    ATTACHMENT_UPLOADED = "attachment_uploaded"
    ATTACHMENT_DELETED = "attachment_deleted"
    ATTACHMENT_UPDATED = "attachment_description_updated"
    PROJECT_KEY_RENAMED = "project_key_renamed"
    # A connected repository moved to another forge: new hook URL, new secret.
    REPOSITORY_PROVIDER_CHANGED = "repository_provider_changed"
    # A commit or pull request detached from an issue, or moved to another one, by a person.
    DEVELOPMENT_LINK_DETACHED = "development_link_detached"
    DEVELOPMENT_LINK_MOVED = "development_link_moved"
    ISSUE_CREATED = "issue_created"
    ISSUE_UPDATED = "issue_updated"
    ISSUE_READ = "issue_read"
    ISSUE_LISTED = "issue_listed"
    WIKI_READ = "wiki_read"
    WIKI_UPDATED = "wiki_updated"
    WIKI_LISTED = "wiki_listed"
    COMMENT_ADDED = "comment_added"
    PROJECTS_LISTED = "projects_listed"
    WIKI_CREATED = "wiki_created"
    WIKI_DELETED = "wiki_deleted"
    WIKI_RESTORED = "wiki_restored"
    MEMBERS_LISTED = "members_listed"
    LOOKUPS_READ = "lookups_read"
    TIME_LOGGED = "time_logged"
    TIMER_STARTED = "timer_started"
    TIMER_DISCARDED = "timer_discarded"
    VERSIONS_LISTED = "versions_listed"
    VERSION_CREATED = "version_created"
    VERSION_UPDATED = "version_updated"
    VERSION_DELETED = "version_deleted"
    # A sprint was started or completed. Details hold the sprint's name and
    # issue counts (and, on completion, where unfinished issues went); never
    # issue subjects.
    SPRINT_STARTED = "sprint_started"
    SPRINT_COMPLETED = "sprint_completed"
    RECURRING_PATTERNS_LISTED = "recurring_patterns_listed"
    RECURRING_PATTERN_CREATED = "recurring_pattern_created"
    RECURRING_PATTERN_UPDATED = "recurring_pattern_updated"
    RECURRING_PATTERN_DELETED = "recurring_pattern_deleted"
    RECURRENCE_OCCURRENCE_SKIPPED = "recurrence_occurrence_skipped"
    RECURRENCE_OCCURRENCES_LISTED = "recurrence_occurrences_listed"
    METADATA_SCHEMA_CREATED = "metadata_schema_created"
    METADATA_SCHEMA_UPDATED = "metadata_schema_updated"
    METADATA_SCHEMA_DELETED = "metadata_schema_deleted"
    TAGS_LISTED = "tags_listed"
    TAG_CREATED = "tag_created"
    TAG_UPDATED = "tag_updated"
    TAG_DELETED = "tag_deleted"
    TAG_ADDED = "tag_added"
    TAG_REMOVED = "tag_removed"
    RELATION_LISTED = "relation_listed"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    PASSWORD_RESET_FAILED = "password_reset_failed"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_CHANGE_FAILED = "password_change_failed"
    PASSWORD_HASH_UPGRADED = "password_hash_upgraded"
    # Self-service email change: requested (details: the new address and
    # whether another account uses it), refused (``reason``), and done, by the
    # person or by an administrator (old and new address, ``by``).
    EMAIL_CHANGE_REQUESTED = "email_change_requested"
    EMAIL_CHANGE_FAILED = "email_change_failed"
    EMAIL_CHANGED = "email_changed"
    PROJECT_ANONYMOUS_PERMISSIONS_CHANGED = "project_anonymous_permissions_changed"
    ANONYMOUS_ACCESS_SWITCH_CHANGED = "anonymous_access_switch_changed"
    # Single sign-on. Details hold the provider slug, the issuer, the hashed
    # subject (``sub_hash``), ids and counts; never tokens, codes or secrets.
    SSO_LOGIN_SUCCESS = "sso_login_success"
    SSO_LOGIN_FAILURE = "sso_login_failure"
    SSO_USER_PROVISIONED = "sso_user_provisioned"
    SSO_ACCOUNT_LINKED = "sso_account_linked"
    SSO_ACCOUNT_UNLINKED = "sso_account_unlinked"
    SSO_ROLES_SYNCED = "sso_roles_synced"
    SSO_GROUPS_OVERAGE = "sso_groups_overage"
    SSO_LOGOUT = "sso_logout"
    SSO_BACKCHANNEL_LOGOUT = "sso_backchannel_logout"
    # The IdP refused a session's refresh token (or could not be asked, with
    # ``idp_check_failure=deny``), so the session ended.
    SSO_SESSION_REJECTED = "sso_session_rejected"
    SSO_PROVIDER_CREATED = "sso_provider_created"
    SSO_PROVIDER_UPDATED = "sso_provider_updated"
    SSO_PROVIDER_DELETED = "sso_provider_deleted"
    SSO_PROVIDER_SECRET_CHANGED = "sso_provider_secret_changed"
    USER_DEPROVISIONED = "user_deprovisioned"
    # SCIM provisioning. The actor is nobody (the IdP); details name the
    # provider (``source: scim``), the SCIM id, the Specivo id and an action.
    SCIM_USER_CREATED = "scim_user_created"
    SCIM_USER_UPDATED = "scim_user_updated"
    SCIM_USER_DEPROVISIONED = "scim_user_deprovisioned"
    SCIM_GROUP_CREATED = "scim_group_created"
    SCIM_GROUP_UPDATED = "scim_group_updated"
    SCIM_GROUP_DELETED = "scim_group_deleted"
    # A SCIM change Specivo refused for a reason worth auditing (the last admin).
    SCIM_REQUEST_REFUSED = "scim_request_refused"
    # The admin generated, rotated or revoked a provider's SCIM token, or switched SCIM.
    SCIM_CONFIG_CHANGED = "scim_config_changed"
    SESSIONS_REVOKED = "sessions_revoked"
    # Money. A currency switched on or off for pickers (details: ``code``,
    # ``is_active``); the instance or a project reporting currency changed
    # (``scope`` instance/project, ``project_key``, ``old``, ``new``; a
    # project's ``None`` means it follows the instance setting).
    CURRENCY_TOGGLED = "currency_toggled"
    REPORTING_CURRENCY_CHANGED = "reporting_currency_changed"
    # Exchange rates. A rate added, edited or deleted by hand (details:
    # ``rate_id``, ``rate_date``, ``base``, ``quote``, ``rate``, ``source``;
    # an edit adds ``changes``); a CSV import applied (``rows``, ``created``,
    # ``updated``, ``unchanged``, ``date_from``, ``date_to``); the optional
    # rate fetch switched on or off (``enabled``, ``url``); the pivot, maximum
    # age or fetch URL changed (``changes``: ``{key: {old, new}}``).
    EXCHANGE_RATE_CREATED = "exchange_rate_created"
    EXCHANGE_RATE_UPDATED = "exchange_rate_updated"
    EXCHANGE_RATE_DELETED = "exchange_rate_deleted"
    EXCHANGE_RATES_IMPORTED = "exchange_rates_imported"
    EXCHANGE_RATES_FETCH_TOGGLED = "exchange_rates_fetch_toggled"
    EXCHANGE_RATE_SETTINGS_CHANGED = "exchange_rate_settings_changed"
    # AI model prices. A price period added by an administrator (details:
    # ``price_id``, ``provider``, ``model``, ``currency``, ``effective_from``,
    # ``effective_to``, ``source``, ``prices``; ``closed_price_id`` when it
    # closed the open period); a period changed in place (the same plus
    # ``changes``: ``{field: {old, new}}``, only ``effective_to``,
    # ``is_active``, ``aliases`` and ``note`` can change); a period deleted;
    # the bundled seed loaded with changes (``as_of``, ``inserted``,
    # ``closed``, ``history_inserted``, ``aliases_updated``, ``unchanged``,
    # ``skipped``).
    AI_MODEL_PRICE_CREATED = "ai_model_price_created"
    AI_MODEL_PRICE_UPDATED = "ai_model_price_updated"
    AI_MODEL_PRICE_DELETED = "ai_model_price_deleted"
    AI_MODEL_PRICES_SEEDED = "ai_model_prices_seeded"
    # AI subscriptions. An administrator added, changed or deleted another
    # user's subscription (details: ``subscription_id``, ``target_user_id``,
    # ``target_login``, ``provider``, ``plan_key``, ``plan_name``,
    # ``tool_slugs``, ``amount``, ``currency``, ``billing_period``,
    # ``starts_on``, ``ends_on``; a change adds ``changes``: ``{field: {old,
    # new}}``, the notes only as ``{"notes": {"changed": true}}``). People's
    # changes to their own subscriptions are not audited. Self-service
    # switched on or off (``enabled``).
    AI_SUBSCRIPTION_CREATED = "ai_subscription_created"
    AI_SUBSCRIPTION_UPDATED = "ai_subscription_updated"
    AI_SUBSCRIPTION_DELETED = "ai_subscription_deleted"
    AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED = "ai_subscriptions_self_service_toggled"
    # AI usage entries. Logged in another user's name (details: ``entry_id``,
    # ``target_user_id``, ``target_login``, ``source``, ``tool``,
    # ``provider``, ``model``, ``usage_date``, ``reporter_api_key_id``,
    # ``project_key``, ``issue``; a replay that changes the bucket adds
    # ``status: "updated"`` and ``revision``); changed by someone other than the entry's
    # user (the same plus ``changes``: ``{field: {old, new}}``, comments only
    # as ``{"changed": true}``); excluded from reports, or included again;
    # deleted. Costs recomputed for a range (``provider``, ``models``,
    # ``from``, ``to``, ``entries``, ``changed``, and ``before`` / ``after``
    # totals per currency of ``marginal`` and ``api_equivalent``). Never
    # comments, prompts or code.
    AI_USAGE_LOGGED_FOR_OTHER = "ai_usage_logged_for_other"
    AI_USAGE_UPDATED_BY_OTHER = "ai_usage_updated_by_other"
    AI_USAGE_EXCLUDED = "ai_usage_excluded"
    AI_USAGE_INCLUDED = "ai_usage_included"
    AI_USAGE_DELETED = "ai_usage_deleted"
    AI_USAGE_COSTS_RECOMPUTED = "ai_usage_costs_recomputed"
    # MCP tool calls on AI usage (every MCP call is audited): logging, and editing, excluding or including.
    AI_USAGE_LOGGED = "ai_usage_logged"
    AI_USAGE_CHANGED = "ai_usage_changed"


_VALID_EVENTS = frozenset(e.value for e in AuditEvent)

_EXCHANGE_RATE_ROW_EVENTS = frozenset(
    {
        AuditEvent.EXCHANGE_RATE_CREATED,
        AuditEvent.EXCHANGE_RATE_UPDATED,
        AuditEvent.EXCHANGE_RATE_DELETED,
        AuditEvent.EXCHANGE_RATES_IMPORTED,
    }
)
_EXCHANGE_RATE_EVENTS = _EXCHANGE_RATE_ROW_EVENTS | {
    AuditEvent.EXCHANGE_RATES_FETCH_TOGGLED,
    AuditEvent.EXCHANGE_RATE_SETTINGS_CHANGED,
}

_AI_MODEL_PRICE_EVENTS = frozenset(
    {
        AuditEvent.AI_MODEL_PRICE_CREATED,
        AuditEvent.AI_MODEL_PRICE_UPDATED,
        AuditEvent.AI_MODEL_PRICE_DELETED,
        AuditEvent.AI_MODEL_PRICES_SEEDED,
    }
)


_AI_SUBSCRIPTION_ROW_EVENTS = frozenset(
    {
        AuditEvent.AI_SUBSCRIPTION_CREATED,
        AuditEvent.AI_SUBSCRIPTION_UPDATED,
        AuditEvent.AI_SUBSCRIPTION_DELETED,
    }
)
_AI_SUBSCRIPTION_EVENTS = _AI_SUBSCRIPTION_ROW_EVENTS | {AuditEvent.AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED}

_AI_USAGE_EVENTS = frozenset(
    {
        AuditEvent.AI_USAGE_LOGGED_FOR_OTHER,
        AuditEvent.AI_USAGE_UPDATED_BY_OTHER,
        AuditEvent.AI_USAGE_EXCLUDED,
        AuditEvent.AI_USAGE_INCLUDED,
        AuditEvent.AI_USAGE_DELETED,
        AuditEvent.AI_USAGE_COSTS_RECOMPUTED,
    }
)


class AuditCategory(StrEnum):
    """The groups the admin viewer filters by; every :class:`AuditEvent` belongs to one."""

    SIGN_IN = "sign_in"
    ACCESS = "access"
    AGENTS = "agents"
    ISSUES_AND_WIKI = "issues_and_wiki"
    ADMINISTRATION = "administration"
    SSO = "sso"
    PROVISIONING = "provisioning"
    SESSIONS = "sessions"
    AI_USAGE = "ai_usage"


#: The category of each event. "Agents" holds the events only MCP tools write;
#: what agents do through actions people can take as well (tags, wiki deletes,
#: timers) sits in that action's category, with ``details.source = "mcp"``.
EVENT_CATEGORIES: dict[AuditEvent, AuditCategory] = {
    **dict.fromkeys(
        [
            AuditEvent.LOGIN_SUCCESS,
            AuditEvent.LOGIN_FAILURE,
            AuditEvent.AUTH_FAILURE,
            AuditEvent.PASSWORD_RESET_REQUESTED,
            AuditEvent.PASSWORD_RESET_COMPLETED,
            AuditEvent.PASSWORD_RESET_FAILED,
            AuditEvent.PASSWORD_CHANGED,
            AuditEvent.PASSWORD_CHANGE_FAILED,
            AuditEvent.PASSWORD_HASH_UPGRADED,
            AuditEvent.EMAIL_CHANGE_REQUESTED,
            AuditEvent.EMAIL_CHANGE_FAILED,
            AuditEvent.EMAIL_CHANGED,
        ],
        AuditCategory.SIGN_IN,
    ),
    **dict.fromkeys(
        [
            AuditEvent.ACCESS_GRANTED,
            AuditEvent.ACCESS_DENIED,
            AuditEvent.RESOURCE_ACCESS,
            AuditEvent.SEARCH_QUERY,
            AuditEvent.MEMBER_CHANGE,
            AuditEvent.GROUP_CHANGE,
            AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED,
            AuditEvent.ANONYMOUS_ACCESS_SWITCH_CHANGED,
        ],
        AuditCategory.ACCESS,
    ),
    **dict.fromkeys(
        [
            AuditEvent.ISSUE_CREATED,
            AuditEvent.ISSUE_UPDATED,
            AuditEvent.ISSUE_READ,
            AuditEvent.ISSUE_LISTED,
            AuditEvent.WIKI_READ,
            AuditEvent.WIKI_UPDATED,
            AuditEvent.WIKI_LISTED,
            AuditEvent.WIKI_CREATED,
            AuditEvent.COMMENT_ADDED,
            AuditEvent.PROJECTS_LISTED,
            AuditEvent.MEMBERS_LISTED,
            AuditEvent.LOOKUPS_READ,
            AuditEvent.TIME_LOGGED,
            AuditEvent.TIMER_STARTED,
            AuditEvent.VERSIONS_LISTED,
            AuditEvent.VERSION_CREATED,
            AuditEvent.VERSION_UPDATED,
            AuditEvent.VERSION_DELETED,
            AuditEvent.RECURRING_PATTERNS_LISTED,
            AuditEvent.RECURRING_PATTERN_CREATED,
            AuditEvent.RECURRING_PATTERN_UPDATED,
            AuditEvent.RECURRING_PATTERN_DELETED,
            AuditEvent.RECURRENCE_OCCURRENCE_SKIPPED,
            AuditEvent.RECURRENCE_OCCURRENCES_LISTED,
            AuditEvent.TAGS_LISTED,
            AuditEvent.RELATION_LISTED,
            AuditEvent.RELATION_ADDED,
            AuditEvent.RELATION_REMOVED,
        ],
        AuditCategory.AGENTS,
    ),
    **dict.fromkeys(
        [
            AuditEvent.ATTACHMENT_UPLOADED,
            AuditEvent.ATTACHMENT_DELETED,
            AuditEvent.ATTACHMENT_UPDATED,
            AuditEvent.WIKI_DELETED,
            AuditEvent.WIKI_RESTORED,
            AuditEvent.TIMER_DISCARDED,
            AuditEvent.SPRINT_STARTED,
            AuditEvent.SPRINT_COMPLETED,
            AuditEvent.TAG_CREATED,
            AuditEvent.TAG_UPDATED,
            AuditEvent.TAG_DELETED,
            AuditEvent.TAG_ADDED,
            AuditEvent.TAG_REMOVED,
            AuditEvent.DEVELOPMENT_LINK_DETACHED,
            AuditEvent.DEVELOPMENT_LINK_MOVED,
        ],
        AuditCategory.ISSUES_AND_WIKI,
    ),
    **dict.fromkeys(
        [
            AuditEvent.PROJECT_KEY_RENAMED,
            AuditEvent.REPOSITORY_PROVIDER_CHANGED,
            AuditEvent.METADATA_SCHEMA_CREATED,
            AuditEvent.METADATA_SCHEMA_UPDATED,
            AuditEvent.METADATA_SCHEMA_DELETED,
            AuditEvent.USER_DEPROVISIONED,
            AuditEvent.CURRENCY_TOGGLED,
            AuditEvent.REPORTING_CURRENCY_CHANGED,
            AuditEvent.EXCHANGE_RATE_CREATED,
            AuditEvent.EXCHANGE_RATE_UPDATED,
            AuditEvent.EXCHANGE_RATE_DELETED,
            AuditEvent.EXCHANGE_RATES_IMPORTED,
            AuditEvent.EXCHANGE_RATES_FETCH_TOGGLED,
            AuditEvent.EXCHANGE_RATE_SETTINGS_CHANGED,
            AuditEvent.AI_MODEL_PRICE_CREATED,
            AuditEvent.AI_MODEL_PRICE_UPDATED,
            AuditEvent.AI_MODEL_PRICE_DELETED,
            AuditEvent.AI_MODEL_PRICES_SEEDED,
            AuditEvent.AI_SUBSCRIPTION_CREATED,
            AuditEvent.AI_SUBSCRIPTION_UPDATED,
            AuditEvent.AI_SUBSCRIPTION_DELETED,
            AuditEvent.AI_SUBSCRIPTIONS_SELF_SERVICE_TOGGLED,
        ],
        AuditCategory.ADMINISTRATION,
    ),
    **dict.fromkeys(
        [
            AuditEvent.SSO_LOGIN_SUCCESS,
            AuditEvent.SSO_LOGIN_FAILURE,
            AuditEvent.SSO_USER_PROVISIONED,
            AuditEvent.SSO_ACCOUNT_LINKED,
            AuditEvent.SSO_ACCOUNT_UNLINKED,
            AuditEvent.SSO_ROLES_SYNCED,
            AuditEvent.SSO_GROUPS_OVERAGE,
            AuditEvent.SSO_PROVIDER_CREATED,
            AuditEvent.SSO_PROVIDER_UPDATED,
            AuditEvent.SSO_PROVIDER_DELETED,
            AuditEvent.SSO_PROVIDER_SECRET_CHANGED,
        ],
        AuditCategory.SSO,
    ),
    **dict.fromkeys(
        [
            AuditEvent.SCIM_USER_CREATED,
            AuditEvent.SCIM_USER_UPDATED,
            AuditEvent.SCIM_USER_DEPROVISIONED,
            AuditEvent.SCIM_GROUP_CREATED,
            AuditEvent.SCIM_GROUP_UPDATED,
            AuditEvent.SCIM_GROUP_DELETED,
            AuditEvent.SCIM_REQUEST_REFUSED,
            AuditEvent.SCIM_CONFIG_CHANGED,
        ],
        AuditCategory.PROVISIONING,
    ),
    **dict.fromkeys(
        [
            AuditEvent.SESSIONS_REVOKED,
            AuditEvent.SSO_LOGOUT,
            AuditEvent.SSO_BACKCHANNEL_LOGOUT,
            AuditEvent.SSO_SESSION_REJECTED,
        ],
        AuditCategory.SESSIONS,
    ),
    **dict.fromkeys(
        [
            AuditEvent.AI_USAGE_LOGGED_FOR_OTHER,
            AuditEvent.AI_USAGE_UPDATED_BY_OTHER,
            AuditEvent.AI_USAGE_EXCLUDED,
            AuditEvent.AI_USAGE_INCLUDED,
            AuditEvent.AI_USAGE_DELETED,
            AuditEvent.AI_USAGE_COSTS_RECOMPUTED,
            AuditEvent.AI_USAGE_LOGGED,
            AuditEvent.AI_USAGE_CHANGED,
        ],
        AuditCategory.AI_USAGE,
    ),
}


def events_in(category: AuditCategory) -> list[str]:
    """The event type values of *category*, in declaration order."""
    return [event.value for event, cat in EVENT_CATEGORIES.items() if cat is category]


def is_audit_event(value: str) -> bool:
    """Whether *value* is an :class:`AuditEvent` value."""
    return value in _VALID_EVENTS


#: Widths of the ``security_audit_logs`` columns a client's headers end up in.
_REQUEST_ID_MAX = 36
_USER_AGENT_MAX = 512
_IP_ADDRESS_MAX = 45
#: The longest search text kept in ``details.query``.
SEARCH_QUERY_MAX = 500


def _cut(value: Any, width: int) -> str | None:
    return str(value).replace("\x00", "")[:width] if value else None


def _without_nul(value: Any) -> Any:
    """*value* with NUL characters removed from every string in it; JSONB refuses them."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_without_nul(k): _without_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_without_nul(v) for v in value]
    return value


def _bounded(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` with the client-sent columns cut to their widths, search text capped and NUL removed.

    Applied on every write path: a longer value or a NUL would fail the INSERT,
    and with it every other event of the batch (or the transaction it shares).
    """
    details = _without_nul(event.get("details") or {})
    query = details.get("query")
    if isinstance(query, str) and len(query) > SEARCH_QUERY_MAX:
        details = {**details, "query": query[:SEARCH_QUERY_MAX]}
    return {
        **event,
        "request_id": _cut(event.get("request_id"), _REQUEST_ID_MAX),
        "user_agent": _cut(event.get("user_agent"), _USER_AGENT_MAX),
        "ip_address": _cut(event.get("ip_address"), _IP_ADDRESS_MAX),
        "details": details,
    }


def bounded_log(event: dict[str, Any]) -> SecurityAuditLog:
    """A :class:`SecurityAuditLog` for the event dict, client-sent columns cut to fit."""
    return SecurityAuditLog(**_bounded(event))


@dataclass(slots=True)
class _AuditContext:
    """The audit buffer of one request (or one :func:`audit_scope`) and who made the request."""

    events: list[dict[str, Any]]
    ip_address: str | None = None
    user_agent: str | None = None
    # The ASGI scope state: the request id is set there by an inner middleware,
    # so it is read when an event is recorded, not when the context is bound.
    state: Mapping[str, Any] = field(default_factory=dict)

    @property
    def request_id(self) -> str | None:
        return _cut(self.state.get("request_id"), _REQUEST_ID_MAX)


_audit_context: ContextVar[_AuditContext | None] = ContextVar("security_audit_context", default=None)


def bind_audit_context(
    events: list[dict[str, Any]],
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> Token[_AuditContext | None]:
    """Make ``events`` the buffer :func:`record_event` appends to in this context.

    Returns the token for :func:`reset_audit_context`.
    """
    return _audit_context.set(
        _AuditContext(
            events=events,
            ip_address=ip_address,
            user_agent=_cut(user_agent, _USER_AGENT_MAX),
            state=state if state is not None else {},
        )
    )


def reset_audit_context(token: Token[_AuditContext | None]) -> None:
    """Undo :func:`bind_audit_context`."""
    _audit_context.reset(token)


def current_request_id() -> str | None:
    """The id of the request being handled, when an audit context is bound."""
    context = _audit_context.get()
    return context.request_id if context is not None else None


def current_request_info() -> dict[str, str | None]:
    """Where the request being handled came from: ``ip_address``, ``request_id``, ``user_agent``.

    All ``None`` when no audit context is bound (a CLI command, a job, or a
    call the request's context does not reach).
    """
    context = _audit_context.get()
    if context is None:
        return {"ip_address": None, "request_id": None, "user_agent": None}
    return {"ip_address": context.ip_address, "request_id": context.request_id, "user_agent": context.user_agent}


def record_event(
    event_type: AuditEvent,
    *,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
    resource_type: str | None = None,
    resource_id: int | None = None,
) -> bool:
    """Append an event to the bound audit buffer; ``True`` when it was buffered.

    Synchronous, for code that cannot await. Nothing is buffered when no
    buffer is bound (the caller's log line is then the only trail). Raises
    ``ValueError`` for a value that is not an :class:`AuditEvent`.
    """
    if str(event_type) not in _VALID_EVENTS:
        raise ValueError(f"Unknown audit event type: {event_type!r}. Use AuditEvent enum.")
    context = _audit_context.get()
    if context is None:
        return False
    context.events.append(
        {
            "event_type": str(event_type),
            "user_id": user_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "project_id": None,
            "permission": None,
            "ip_address": context.ip_address,
            "request_id": context.request_id,
            "user_agent": context.user_agent,
            "details": details or {},
        }
    )
    return True


@asynccontextmanager
async def audit_scope(session: AsyncSession) -> AsyncIterator[None]:
    """Collect :func:`record_event` calls outside an HTTP request and write them on success.

    For CLI commands and background jobs. Inside a request the request's
    buffer is already bound and is used as is. The write goes through a
    savepoint and never raises: a failing audit write is logged, the caller's
    work stands.
    """
    if _audit_context.get() is not None:
        yield
        return
    events: list[dict[str, Any]] = []
    token = bind_audit_context(events)
    try:
        yield
    finally:
        reset_audit_context(token)
    if not events:
        return
    try:
        async with session.begin_nested():
            session.add_all(bounded_log(event) for event in events)
    except Exception:
        logger.warning("Security audit write failed for %d event(s)", len(events), exc_info=True)


class MemberAction(StrEnum):
    """Valid actions for member_change audit events (details.action)."""

    ADDED = "added"
    REMOVED = "removed"
    ROLES_CHANGED = "roles_changed"
    PERMISSION_DENIED = "permission_denied"


class GroupAction(StrEnum):
    """Valid actions for group_change audit events (details.action).

    A user group is a membership principal, so changing one changes who can
    reach which projects — the same class of event as a member change, but
    not project-scoped, which is why it has its own event type.
    """

    CREATED = "created"
    RENAMED = "renamed"
    DELETED = "deleted"
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"


class SecurityAuditService:
    """Stateless service for security audit event logging and querying."""

    # ------------------------------------------------------------------
    # Core INSERT
    # ------------------------------------------------------------------

    @staticmethod
    async def _persist(session: AsyncSession, *, savepoint: bool = False, **fields: Any) -> SecurityAuditLog:
        """Write one event now, in the caller's transaction.

        With *savepoint* the insert runs in a savepoint, so a failing audit
        write cannot poison the transaction of the work it records.
        """
        fields.setdefault("details", {})
        log = bounded_log(fields)
        if savepoint:
            async with session.begin_nested():
                session.add(log)
        else:
            session.add(log)
            await session.flush()
        return log

    async def log_event(
        self,
        session: AsyncSession,
        event_type: str | AuditEvent,
        user_id: int | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        project_id: int | None = None,
        permission: str | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Persist a single audit log entry.

        If a ``request`` with an ``audit_events`` buffer on its state is provided,
        the event is appended to the buffer for batch INSERT by AuditBatchMiddleware.
        Otherwise, falls back to a direct INSERT (e.g. CLI or Celery contexts).

        ``ip_address``, ``request_id`` and ``user_agent`` not given are taken
        from ``request``, or else from the bound audit context.
        """
        if str(event_type) not in _VALID_EVENTS:
            raise ValueError(f"Unknown audit event type: {event_type!r}. Use AuditEvent enum.")
        origin = self.request_info(request) if request is not None else current_request_info()
        ip_address = ip_address or origin["ip_address"]
        request_id = request_id or origin["request_id"]
        user_agent = user_agent or origin["user_agent"]

        event_data = _bounded(
            {
                "event_type": str(event_type),
                "user_id": user_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "project_id": project_id,
                "permission": permission,
                "ip_address": ip_address,
                "request_id": request_id,
                "user_agent": user_agent,
                "details": details or {},
            }
        )

        # Batch mode: append to request buffer for later flush.
        # Check for list type specifically to avoid MagicMock false positives.
        if request is not None and isinstance(getattr(request.state, "audit_events", None), list):
            request.state.audit_events.append(event_data)
            # Return an unsaved model instance for callers that inspect the result
            return SecurityAuditLog(**event_data)

        # Direct INSERT fallback (no request context)
        return await self._persist(session, **event_data)

    @classmethod
    async def flush_events(cls, session: AsyncSession, events: list[dict[str, Any]]) -> list[SecurityAuditLog]:
        """Batch INSERT a list of event dicts into the database."""
        logs = []
        for event_data in events:
            log = bounded_log(event_data)
            session.add(log)
            logs.append(log)
        await session.flush()
        return logs

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    @staticmethod
    def request_info(request: Request | None) -> dict[str, str | None]:
        """Where *request* came from: ``ip_address``, ``request_id``, ``user_agent``, cut to their columns."""
        if request is None:
            return {"ip_address": None, "request_id": None, "user_agent": None}
        # The id RequestIDMiddleware settled on (the client's or a new one), as on the response.
        request_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id")
        return {
            "ip_address": _cut(get_client_ip(request), _IP_ADDRESS_MAX),
            "request_id": _cut(request_id, _REQUEST_ID_MAX),
            "user_agent": _cut(request.headers.get("user-agent"), _USER_AGENT_MAX),
        }

    async def log_access_denied(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        resource: str | None = None,
        resource_id: int | None = None,
        project_id: int | None = None,
        permission: str | None = None,
    ) -> SecurityAuditLog:
        """Log an access denial.

        Successful checks are not logged (``access_granted`` is kept only so
        old rows stay readable): they say nothing a resource view or history
        does not, and would be one row per page view.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {}
        if resource:
            details["resource"] = resource
        if resource_id is not None:
            details["resource_id"] = resource_id
        if permission:
            details["permission"] = permission
        return await self.log_event(
            session=session,
            event_type=AuditEvent.ACCESS_DENIED,
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            permission=permission,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
            request=request,
        )

    async def log_auth_failure(
        self,
        session: AsyncSession,
        reason: str,
        ip_address: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SecurityAuditLog:
        """Log an authentication failure (user_id is NULL)."""
        merged_details: dict[str, Any] = {"reason": reason}
        if details:
            merged_details.update(details)
        return await self.log_event(
            session=session,
            event_type=AuditEvent.AUTH_FAILURE,
            user_id=None,
            ip_address=ip_address,
            request_id=request_id,
            details=merged_details,
        )

    async def log_refused_mcp_key(self, session: AsyncSession, reason: str) -> SecurityAuditLog:
        """Log an API key the MCP endpoint refused, written now with where the request came from."""
        info = current_request_info()
        return await self._persist(
            session,
            event_type=AuditEvent.AUTH_FAILURE,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={"reason": reason, "source": "mcp"},
        )

    async def log_login_success(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        details: dict[str, Any] | None = None,
    ) -> SecurityAuditLog:
        """Log a successful login. Core feature — always persisted."""
        info = self.request_info(request)
        merged: dict[str, Any] = {"method": "password"}
        if details:
            merged.update(details)
        return await self._persist(
            session,
            event_type=AuditEvent.LOGIN_SUCCESS,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=merged,
        )

    async def log_login_failure(
        self,
        session: AsyncSession,
        request: Request | None = None,
        login_hint: str | None = None,
        reason: str = "invalid_credentials",
    ) -> SecurityAuditLog:
        """Log a failed login attempt. Core feature — always persisted.

        *login_hint* is the login of the account the attempt named, and only
        that: what was typed is never stored, because a password typed into
        the login field would otherwise become readable by admins. Without one
        the row records ``login_known: false``.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {"reason": reason}
        if login_hint:
            details["login_hint"] = login_hint
        else:
            details["login_known"] = False
        return await self._persist(
            session,
            event_type=AuditEvent.LOGIN_FAILURE,
            user_id=None,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_sso_login_success(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        provider_slug: str,
        issuer: str,
        sub_hash: str,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a single sign-on login. Core feature, always persisted (like :meth:`log_login_success`).

        ``sub_hash`` is the hashed subject, never the raw ``sub``.
        """
        return await self._log_sso_login(
            session,
            AuditEvent.SSO_LOGIN_SUCCESS,
            user_id=user_id,
            details={"method": "sso", "provider": provider_slug, "iss": issuer, "sub_hash": sub_hash},
            request=request,
        )

    async def log_sso_login_failure(
        self,
        session: AsyncSession,
        *,
        provider_slug: str,
        issuer: str,
        reason: str,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a refused single sign-on login; ``reason`` is the ``sso_*`` error code. Core feature."""
        return await self._log_sso_login(
            session,
            AuditEvent.SSO_LOGIN_FAILURE,
            user_id=None,
            details={"method": "sso", "provider": provider_slug, "iss": issuer, "reason": reason},
            request=request,
        )

    async def _log_sso_login(
        self,
        session: AsyncSession,
        event: AuditEvent,
        *,
        user_id: int | None,
        details: dict[str, Any],
        request: Request | None,
    ) -> SecurityAuditLog:
        info = self.request_info(request)
        # A savepoint: a failed audit insert must not poison the login's transaction.
        return await self._persist(
            session,
            savepoint=True,
            event_type=event,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=current_request_id() or info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_member_change(
        self,
        session: AsyncSession,
        action: MemberAction,
        user_id: int,
        project_id: int,
        target_user_id: int | None = None,
        target_login: str | None = None,
        target_group_id: int | None = None,
        target_group_name: str | None = None,
        roles: list[str] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a project member change. Core feature — always persisted.

        A membership is held by a user or by a user group, and granting a
        group roles on a project is the same class of event as granting them
        to a user: it changes who can reach this project.  Both therefore land
        here, as a project-scoped ``member_change`` row, rather than a group
        grant going to :meth:`log_group_change` — that one is about the group
        itself, leaves ``project_id`` NULL and so could not name the project
        whose access changed.

        ``details.principal_type`` says which kind the row is about, and the
        holder is named by ``target_user_id``/``target_login`` for a user or
        ``target_group_id``/``target_group_name`` for a group.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {"action": str(action)}
        if target_group_id is not None:
            details["principal_type"] = "group"
            details["target_group_id"] = target_group_id
            details["target_group_name"] = target_group_name or ""
        else:
            details["principal_type"] = "user"
            details["target_user_id"] = target_user_id if target_user_id is not None else 0
            details["target_login"] = target_login or ""
        if roles:
            details["roles"] = roles
        return await self._persist(
            session,
            event_type=AuditEvent.MEMBER_CHANGE,
            user_id=user_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_group_change(
        self,
        session: AsyncSession,
        action: GroupAction,
        user_id: int,
        group_id: int,
        group_name: str,
        target_user_id: int | None = None,
        target_login: str | None = None,
        extra: dict[str, Any] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a user group change. Core feature — always persisted.

        Unlike :meth:`log_member_change` this is not project-scoped: a group
        exists outside any project and can grant access to several at once.
        ``project_id`` is therefore left NULL and the group is identified by
        ``resource_type``/``resource_id``.

        *target_user_id* / *target_login* identify the user moved in or out of
        the group, and are None for events about the group itself.  *extra*
        carries action-specific facts — the old name on a rename, and what the
        group was granting on a delete, which cannot be recovered afterwards.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {
            "action": str(action),
            "group_id": group_id,
            "group_name": group_name,
        }
        if target_user_id is not None:
            details["target_user_id"] = target_user_id
        if target_login is not None:
            details["target_login"] = target_login
        if extra:
            details.update(extra)
        return await self._persist(
            session,
            event_type=AuditEvent.GROUP_CHANGE,
            user_id=user_id,
            resource_type="user_group",
            resource_id=group_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_search_query(
        self,
        session: AsyncSession,
        user_id: int,
        query: str,
        mode: str,
        scope: str | None = None,
        filters: dict[str, Any] | None = None,
        result_count: int = 0,
        type_counts: dict[str, int] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a search query. Core feature — always persisted."""
        info = self.request_info(request)
        details: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "result_count": result_count,
        }
        if scope:
            details["scope"] = scope
        if filters:
            details["filters"] = filters
        if type_counts:
            details["type_counts"] = type_counts
        return await self._persist(
            session,
            event_type=AuditEvent.SEARCH_QUERY,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_resource_viewed(
        self,
        session: AsyncSession,
        user_id: int,
        resource: str,
        resource_key: str,
        resource_id: int | None = None,
        project_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a resource view (issue, wiki page)."""
        info = self.request_info(request)
        details: dict[str, Any] = {
            "resource": resource,
            "resource_key": resource_key,
        }
        return await self.log_event(
            session=session,
            event_type=AuditEvent.RESOURCE_ACCESS,
            user_id=user_id,
            resource_type=resource,
            resource_id=resource_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_password_reset_requested(
        self,
        session: AsyncSession,
        user_id: int | None,
        request: Request | None = None,
        email_hint: str | None = None,
    ) -> SecurityAuditLog:
        """Log a password reset request. Core feature — always persisted.

        *user_id* is set when the email matched a real account, None otherwise.
        Text that matched no account is not stored: it may be anything, including
        a password typed into the wrong field.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {}
        if user_id is None:
            details["email_known"] = False
        elif email_hint:
            details["email_hint"] = email_hint
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_RESET_REQUESTED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_password_reset_completed(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a successful password reset. Core feature — always persisted."""
        info = self.request_info(request)
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_RESET_COMPLETED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={},
        )

    async def log_password_reset_failed(
        self,
        session: AsyncSession,
        reason: str,
        request: Request | None = None,
        user_id: int | None = None,
    ) -> SecurityAuditLog:
        """Log a failed password reset attempt. Core feature — always persisted."""
        info = self.request_info(request)
        details: dict[str, Any] = {"reason": reason}
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_RESET_FAILED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_password_changed(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        revoked_sessions: int = 0,
    ) -> SecurityAuditLog:
        """Log a successful self-service password change. Core feature.

        *revoked_sessions* records how many of the user's other sessions were
        revoked as part of the change. No password material is ever recorded.
        """
        info = self.request_info(request)
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_CHANGED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={"revoked_sessions": revoked_sessions},
        )

    async def log_password_hash_upgraded(
        self,
        session: AsyncSession,
        user_id: int,
        request: Request | None = None,
        from_algorithm: str = "unknown",
    ) -> SecurityAuditLog:
        """Log that a stored password hash was replaced with a native one. Core feature.

        Written when a sign-in (or a password change) verified a legacy or
        outdated hash and re-hashed it. *from_algorithm* is the id it was
        stored under; the hash itself is never recorded.
        """
        info = self.request_info(request)
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_HASH_UPGRADED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={"from_algorithm": from_algorithm},
        )

    async def log_password_change_failed(
        self,
        session: AsyncSession,
        reason: str,
        user_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a rejected self-service password change. Core feature.

        *reason* is the ``AppError`` code of the rejection — never the
        submitted password or any part of it.
        """
        info = self.request_info(request)
        return await self._persist(
            session,
            event_type=AuditEvent.PASSWORD_CHANGE_FAILED,
            user_id=user_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={"reason": reason},
        )

    async def log_project_anonymous_permissions_change(
        self,
        session: AsyncSession,
        user_id: int | None,
        project_id: int,
        project_key: str,
        old: list[str],
        new: list[str],
        reason: str,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a change to what anonymous visitors may read in a project. Core feature.

        *reason* says what caused it: an administrator editing the list, the
        project being made private, or an import carrying the setting over. *user_id* is None only when the change
        was made outside a request by nobody in particular.
        """
        info = self.request_info(request)
        return await self._persist(
            session,
            event_type=AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED,
            user_id=user_id,
            resource_type="project",
            resource_id=project_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={"project_key": project_key, "old": old, "new": new, "reason": str(reason)},
        )

    async def log_repository_provider_change(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int,
        integration_id: int,
        old_provider: str,
        new_provider: str,
        old_repository: str,
        new_repository: str,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a repository integration moving to another provider. Core feature.

        The change gives the integration a new hook URL and secret, so the old
        forge's webhook stops working at once; the row keeps which forge and
        repository it pointed at before and after. The secret is never logged.
        """
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=AuditEvent.REPOSITORY_PROVIDER_CHANGED,
            user_id=user_id,
            resource_type="repository_integration",
            resource_id=integration_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details={
                "old_provider": old_provider,
                "new_provider": new_provider,
                "old_repository": old_repository,
                "new_repository": new_repository,
                "secret_rotated": True,
            },
        )

    async def log_development_link_change(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        project_id: int,
        issue_id: int,
        issue_ref: str,
        item_type: str,
        item_id: int,
        item_ref: str,
        repository: str,
        target_issue_ref: str | None = None,
        target_project_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a commit or pull request detached from an issue, or moved to another. Core feature.

        ``development_link_moved`` when *target_issue_ref* is given, else
        ``development_link_detached``. The row names the source issue; details
        hold the item (``commit`` with its SHA, or ``pull_request`` with its
        number), the repository and, for a move, the target issue. Never the
        commit message or the request's title.
        """
        info = self.request_info(request) if request is not None else current_request_info()
        details: dict[str, Any] = {
            "issue": issue_ref,
            "item_type": item_type,
            "item_id": item_id,
            "item": item_ref,
            "repository": repository,
        }
        if target_issue_ref is not None:
            details["target_issue"] = target_issue_ref
            details["target_project_id"] = target_project_id
        return await self._persist(
            session,
            savepoint=True,
            event_type=(
                AuditEvent.DEVELOPMENT_LINK_MOVED
                if target_issue_ref is not None
                else AuditEvent.DEVELOPMENT_LINK_DETACHED
            ),
            user_id=user_id,
            resource_type="issue",
            resource_id=issue_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_anonymous_access_switch_change(
        self,
        session: AsyncSession,
        user_id: int,
        setting_key: str,
        old: bool,
        new: bool,
        opted_in_projects: list[str],
        confirmed_projects: list[str] | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log the instance-wide anonymous access switch being turned on or off. Core feature.

        *opted_in_projects* records the keys of the projects carrying anonymous
        permissions at the moment of the change. *confirmed_projects* records
        the keys the administrator confirmed when turning the switch on, and is
        omitted when turning it off.
        """
        info = self.request_info(request)
        details: dict[str, Any] = {
            "setting": setting_key,
            "old": old,
            "new": new,
            "opted_in_projects": opted_in_projects,
        }
        if confirmed_projects is not None:
            details["confirmed_projects"] = confirmed_projects
        return await self._persist(
            session,
            event_type=AuditEvent.ANONYMOUS_ACCESS_SWITCH_CHANGED,
            user_id=user_id,
            resource_type="setting",
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_currency_change(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        user_id: int | None,
        details: dict[str, Any],
        project_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log a currency switched on or off, or a reporting currency change. Core feature.

        Written in the caller's transaction (in a savepoint, so a failed audit
        insert cannot undo the change), so the change and its record commit
        together.
        """
        if event_type not in (AuditEvent.CURRENCY_TOGGLED, AuditEvent.REPORTING_CURRENCY_CHANGED):
            raise ValueError(f"Not a currency event: {event_type!r}")
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=event_type,
            user_id=user_id,
            resource_type="currency" if event_type is AuditEvent.CURRENCY_TOGGLED else "setting",
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_exchange_rate_change(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        user_id: int | None,
        details: dict[str, Any],
        resource_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log an exchange rate added, edited, deleted or imported, or a change of the rate settings.

        Written in the caller's transaction, in a savepoint, like
        :meth:`log_currency_change`.
        """
        if event_type not in _EXCHANGE_RATE_EVENTS:
            raise ValueError(f"Not an exchange rate event: {event_type!r}")
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=event_type,
            user_id=user_id,
            resource_type="exchange_rate" if event_type in _EXCHANGE_RATE_ROW_EVENTS else "setting",
            resource_id=resource_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_ai_model_price_change(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        user_id: int | None,
        details: dict[str, Any],
        resource_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log an AI model price added, changed or deleted, or a seed load that changed prices.

        Written in the caller's transaction, in a savepoint, like
        :meth:`log_exchange_rate_change`.
        """
        if event_type not in _AI_MODEL_PRICE_EVENTS:
            raise ValueError(f"Not an AI model price event: {event_type!r}")
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=event_type,
            user_id=user_id,
            resource_type="ai_model_price",
            resource_id=resource_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_ai_subscription_change(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        user_id: int | None,
        details: dict[str, Any],
        resource_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log an administrator's change to another user's AI subscription, or the self-service switch.

        Written in the caller's transaction, in a savepoint, like
        :meth:`log_exchange_rate_change`.
        """
        if event_type not in _AI_SUBSCRIPTION_EVENTS:
            raise ValueError(f"Not an AI subscription event: {event_type!r}")
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=event_type,
            user_id=user_id,
            resource_type="ai_subscription" if event_type in _AI_SUBSCRIPTION_ROW_EVENTS else "setting",
            resource_id=resource_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    async def log_ai_usage_change(
        self,
        session: AsyncSession,
        event_type: AuditEvent,
        user_id: int | None,
        details: dict[str, Any],
        resource_id: int | None = None,
        project_id: int | None = None,
        request: Request | None = None,
    ) -> SecurityAuditLog:
        """Log an AI usage entry written for someone else, changed, excluded or deleted, or a cost recompute.

        Written in the caller's transaction, in a savepoint, like
        :meth:`log_exchange_rate_change`.
        """
        if event_type not in _AI_USAGE_EVENTS:
            raise ValueError(f"Not an AI usage event: {event_type!r}")
        info = self.request_info(request) if request is not None else current_request_info()
        return await self._persist(
            session,
            savepoint=True,
            event_type=event_type,
            user_id=user_id,
            resource_type="ai_usage_entry",
            resource_id=resource_id,
            project_id=project_id,
            ip_address=info["ip_address"],
            request_id=info["request_id"],
            user_agent=info["user_agent"],
            details=details,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_events(
        self,
        session: AsyncSession,
        event_type: str | None = None,
        user_id: int | None = None,
        project_id: int | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        offset: int = 0,
        limit: int = 25,
        *,
        event_types: Sequence[str] | None = None,
        ip_address: str | None = None,
    ) -> tuple[list[SecurityAuditLog], int]:
        """Query audit logs with optional filters, newest first. Returns (items, total_count).

        ``event_type`` and ``event_types`` both narrow the event type (a row
        must match each one given); an empty ``event_types`` matches nothing.
        ``after`` is inclusive and ``before`` exclusive; a time range lets
        PostgreSQL skip the partitions outside it.
        """
        conditions: list[ColumnElement[bool]] = []
        if event_type is not None:
            conditions.append(SecurityAuditLog.event_type == event_type)
        if event_types is not None:
            conditions.append(SecurityAuditLog.event_type.in_(list(event_types)))
        if user_id is not None:
            conditions.append(SecurityAuditLog.user_id == user_id)
        if project_id is not None:
            conditions.append(SecurityAuditLog.project_id == project_id)
        if ip_address is not None:
            conditions.append(SecurityAuditLog.ip_address == ip_address)
        if after is not None:
            conditions.append(SecurityAuditLog.created_at >= after)
        if before is not None:
            conditions.append(SecurityAuditLog.created_at < before)

        stmt = (
            select(SecurityAuditLog)
            .where(*conditions)
            .order_by(SecurityAuditLog.created_at.desc(), SecurityAuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        items = list((await session.execute(stmt)).scalars().all())
        total = (
            await session.execute(select(func.count()).select_from(SecurityAuditLog).where(*conditions))
        ).scalar_one()
        return items, total

    @staticmethod
    async def user_id_for_login(session: AsyncSession, login: str) -> int | None:
        """The id of the user with *login* (case-insensitive), or ``None``."""
        from specivo.models.user import User

        return await session.scalar(select(User.id).where(func.lower(User.login) == login.strip().lower()))

    @staticmethod
    async def names_for(
        session: AsyncSession, logs: Sequence[SecurityAuditLog]
    ) -> tuple[dict[int, str], dict[int, str]]:
        """The logins of the users and the keys of the projects *logs* name, one query each.

        ``user_id`` and ``project_id`` are not foreign keys: a user or project
        deleted since is simply missing from the maps.
        """
        from specivo.models.project import Project
        from specivo.models.user import User

        user_ids = {log.user_id for log in logs if log.user_id is not None}
        project_ids = {log.project_id for log in logs if log.project_id is not None}
        logins: dict[int, str] = {}
        keys: dict[int, str] = {}
        if user_ids:
            rows = await session.execute(select(User.id, User.login).where(User.id.in_(user_ids)))
            logins = {row.id: row.login for row in rows}
        if project_ids:
            rows = await session.execute(select(Project.id, Project.key).where(Project.id.in_(project_ids)))
            keys = {row.id: row.key for row in rows}
        return logins, keys
