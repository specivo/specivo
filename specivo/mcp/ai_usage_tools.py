"""MCP tool implementations for AI usage: ``specivo_log_ai_usage`` and ``specivo_ai_usage``.

Like ``specivo.mcp.tools``: each function takes an explicit session and user,
delegates to :class:`AiUsageService` (which decides who may read and write
what, exactly as for the REST routes) and returns text for the agent.

An agent cannot see its own token counters, so the numbers it logs come
from a hook, the reporter or the tool's ``/cost`` output; the tool texts in
``server.py`` say so. Entries logged here have the source ``mcp``, the
calling key as their reporting key and the MCP client as their client.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, ValidationError
from specivo.core.money import Money, format_decimal_plain
from specivo.mcp.client_context import agent_session_id_var, mcp_api_key_id_var, mcp_client_var
from specivo.mcp.tools import _log_tool
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService, EntryReferences, money_from
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent

_usage = AiUsageService()
_issues = IssueService()
_projects = ProjectService()

AI_USAGE_OPS = ("list", "get", "update", "exclude", "include")
_MAX_LIMIT = 100

#: MCP token argument -> token class.
TOKEN_ARGS: dict[str, str] = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_write_tokens": "cache_write_5m",
    "cache_write_1h_tokens": "cache_write_1h",
    "cache_read_tokens": "cache_read",
    "reasoning_tokens": "reasoning",
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _money(value: Money | None) -> str:
    return f"{format_decimal_plain(value.amount)} {value.currency}" if value is not None else "unknown"


def _tokens(entry: AiUsageEntry) -> str:
    """Per-class usage with both buckets summed, then the long-context share when there is one."""
    counts = entry.all_tokens()
    parts = [f"in={counts['input']}", f"out={counts['output']}"]
    for name, label in (
        ("cache_write_5m", "cache_write"),
        ("cache_write_1h", "cache_write_1h"),
        ("cache_read", "cache_read"),
        ("reasoning", "reasoning"),
    ):
        if counts[name]:
            parts.append(f"{label}={counts[name]}")
    long_total = sum(entry.long_context_tokens().values())
    if long_total:
        parts.append(f"(long_context={long_total})")
    return " ".join(parts)


def _where(entry: AiUsageEntry, refs: EntryReferences) -> str:
    if entry.project_id is None:
        return "unassigned"
    project = refs.project_keys.get(entry.project_id, f"project {entry.project_id}")
    issue = refs.issue_keys.get(entry.issue_id) if entry.issue_id is not None else None
    return issue or project


def _login(refs: EntryReferences, user_id: int) -> str:
    user = refs.users.get(user_id)
    return user.login if user is not None else f"#{user_id}"


def _session(entry: AiUsageEntry, viewer: User) -> str | None:
    """The agent session id of *entry*, for its owner only (it is the owner's client session)."""
    return entry.external_session_id if entry.user_id == viewer.id else None


def _line(entry: AiUsageEntry, refs: EntryReferences, viewer: User) -> str:
    flags = []
    if entry.is_excluded:
        flags.append("excluded")
    if entry.is_locked:
        flags.append("locked")
    return (
        f"#{entry.id} {entry.usage_date.isoformat()} {_where(entry, refs)} {entry.tool_slug}/{entry.model} "
        f"user={_login(refs, entry.user_id)} {_tokens(entry)} cost_type={entry.cost_type} "
        f"marginal={_money(entry.marginal)} api_equivalent={_money(entry.api_equivalent)} source={entry.source}"
        + (f" session={session_id}" if (session_id := _session(entry, viewer)) else "")
        + (f" [{', '.join(flags)}]" if flags else "")
    )


def _details(entry: AiUsageEntry, refs: EntryReferences, viewer: User) -> str:
    lines = [
        f"AI usage entry #{entry.id} (revision {entry.revision})",
        f"Where: {_where(entry, refs)} (attribution: {entry.attribution_method})",
        f"User: {_login(refs, entry.user_id)}",
        f"Logged by: {_login(refs, entry.reporter_user_id)}"
        + (f" with API key #{entry.reporter_api_key_id}" if entry.reporter_api_key_id else "")
        + f", source {entry.source}"
        + (f", client {entry.client_name} {entry.client_version or ''}".rstrip() if entry.client_name else ""),
        f"Tool: {entry.tool_slug}  Provider: {entry.provider or '-'}  Model: {entry.model}",
        f"Date: {entry.usage_date.isoformat()} (UTC day)"
        + (f", {entry.started_at.isoformat()}" if entry.started_at else "")
        + (f" to {entry.ended_at.isoformat()}" if entry.ended_at else ""),
        f"Requests: {entry.request_count}",
        f"Tokens: {_tokens(entry)}",
    ]
    session_id = _session(entry, viewer)
    if session_id:
        lines.insert(3, f"Session: {session_id}")
    long = " ".join(f"{k}={v}" for k, v in entry.long_context_tokens().items() if v)
    if long:
        lines.append(f"Long-context tokens (included above): {long}")
    lines.append(f"Cost type: {entry.cost_type}  Basis: {entry.cost_basis}")
    lines.append(f"Marginal cost: {_money(entry.marginal)}")
    lines.append(f"API equivalent: {_money(entry.api_equivalent)}")
    if entry.reported_cost is not None:
        lines.append(f"Reported cost: {_money(entry.reported_cost)}")
    lines.append(f"Priced at: {entry.priced_at.isoformat()}")
    if entry.comments:
        lines.append(f"Comments: {entry.comments}")
    if entry.is_excluded and entry.excluded_at is not None:
        lines.append(f"Excluded from reports since {entry.excluded_at.isoformat()}")
    if entry.is_locked:
        lines.append("Reported usage: only the project, the issue and the comments can change.")
    return "\n".join(lines)


async def _show(session: AsyncSession, user: User, entry: AiUsageEntry) -> str:
    await session.flush()
    await session.refresh(entry)
    return _details(entry, await _usage.references(session, user, [entry]), user)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(message=f"{field} must be a date such as 2026-11-05", field=field) from None


def _cost(amount: Any, currency: str | None) -> Money | None:
    if amount is None or (isinstance(amount, str) and not amount.strip()):
        if currency:
            raise ValidationError(message="cost_currency needs cost_amount", field="cost_amount")
        return None
    if not currency or not currency.strip():
        raise ValidationError(message="cost_amount needs cost_currency, e.g. USD", field="cost_currency")
    return money_from(str(amount).strip(), currency)


async def _issue(session: AsyncSession, user: User, ref: str) -> Issue:
    issue = await _issues.get_by_display_key(session, ref.strip(), user=user)
    if issue.project_id is None:
        raise ValidationError(message=f"Issue {ref} has no project", field="issue_ref")
    return issue


async def _project(session: AsyncSession, user: User, key: str) -> Project:
    project = await _projects.get_by_key(session, key.strip().upper())
    await _projects.require_project_access(session, project, user)
    return project


async def _session_issue(session: AsyncSession, user: User) -> tuple[Project, Issue] | None:
    """The issue the calling agent session works on, when the caller may log usage there."""
    agent_session_id = agent_session_id_var.get()
    if agent_session_id is None:
        return None
    agent_session = await session.get(AgentSession, agent_session_id)
    if agent_session is None or agent_session.issue_id is None:
        return None
    issue = await session.get(Issue, agent_session.issue_id)
    if issue is None or issue.project_id is None:
        return None
    project = await session.get(Project, issue.project_id)
    if project is None:
        return None
    try:
        await _usage.require_can_log(session, user, user, project, issue.id)
    except AppError:
        return None
    return project, issue


# ---------------------------------------------------------------------------
# specivo_log_ai_usage
# ---------------------------------------------------------------------------


async def _log_ai_usage(
    session: AsyncSession,
    user: User,
    *,
    tool: str,
    model: str,
    tokens: dict[str, int],
    project_key: str | None = None,
    issue_ref: str | None = None,
    provider: str | None = None,
    request_count: int = 0,
    cost_type: str | None = None,
    cost_amount: Any = None,
    cost_currency: str | None = None,
    usage_date: str | None = None,
    on_behalf_of: str | None = None,
    comments: str | None = None,
    session_id: str | None = None,
) -> str:
    """Log one AI usage entry (source ``mcp``); see ``specivo_log_ai_usage`` for the rules."""
    day = _parse_date(usage_date, "usage_date") or datetime.now(UTC).date()
    project: Project | None = None
    issue: Issue | None = None
    method: str | None = None
    if issue_ref and issue_ref.strip():
        issue = await _issue(session, user, issue_ref)
        project = await session.get(Project, issue.project_id)
        if project_key and project is not None and project.key != project_key.strip().upper():
            raise ValidationError(
                message=f"Issue {issue.display_key} is not in project {project_key}", field="issue_ref"
            )
    elif project_key and project_key.strip():
        project = await _project(session, user, project_key)
    elif not (on_behalf_of and on_behalf_of.strip()):
        found = await _session_issue(session, user)
        if found is not None:
            project, issue = found
            method = "mcp_session"

    client = mcp_client_var.get()
    data = AiUsageInput(
        tool=tool,
        model=model,
        usage_date=day,
        provider=provider,
        tokens=tokens,
        request_count=request_count,
        cost_type=cost_type or None,
        reported_cost=_cost(cost_amount, cost_currency),
        issue_id=issue.id if issue is not None else None,
        attribution_method=method,
        external_session_id=session_id,
        client_name=client.name if client is not None else None,
        client_version=client.version if client is not None else None,
        comments=comments,
    )
    entry = await _usage.create_for(
        session,
        user,
        data,
        project=project,
        on_behalf_of=on_behalf_of,
        source="mcp",
        api_key_id=mcp_api_key_id_var.get(),
    )
    await session.flush()
    refs = await _usage.references(session, user, [entry])
    await _log_tool(
        session,
        user,
        AuditEvent.AI_USAGE_LOGGED,
        "log_ai_usage",
        {
            "entry_id": entry.id,
            "target_login": _login(refs, entry.user_id),
            "tool": entry.tool_slug,
            "model": entry.model,
            "usage_date": entry.usage_date.isoformat(),
            "project_key": refs.project_keys.get(entry.project_id) if entry.project_id else None,
            "issue": refs.issue_keys.get(entry.issue_id) if entry.issue_id else None,
        },
        project_id=entry.project_id,
    )
    where = _where(entry, refs)
    if method == "mcp_session":
        where += " (the issue this agent session works on)"
    for_whom = f" for {_login(refs, entry.user_id)}" if entry.user_id != user.id else ""
    return (
        f"Logged AI usage entry #{entry.id}{for_whom} on {where}: {entry.tool_slug}/{entry.model}, "
        f"{_tokens(entry)}.\n"
        f"Cost type: {entry.cost_type}. Marginal cost: {_money(entry.marginal)}. "
        f"API equivalent: {_money(entry.api_equivalent)} ({entry.cost_basis})."
    )


# ---------------------------------------------------------------------------
# specivo_ai_usage
# ---------------------------------------------------------------------------


async def _ai_usage(
    session: AsyncSession,
    user: User,
    op: str,
    *,
    entry_id: int | None = None,
    project_key: str | None = None,
    issue_ref: str | None = None,
    user_login: str | None = None,
    tool: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    cost_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    usage_date: str | None = None,
    excluded: bool | None = None,
    unassigned: bool | None = None,
    session_id: str | None = None,
    tokens: dict[str, int] | None = None,
    request_count: int | None = None,
    cost_amount: Any = None,
    cost_currency: str | None = None,
    comments: str | None = None,
    offset: int = 0,
    limit: int = 25,
) -> str:
    """List, read, change, exclude or include AI usage entries; see ``specivo_ai_usage``."""
    if op not in AI_USAGE_OPS:
        raise ValidationError(message=f"Invalid op '{op}'. Must be one of: {', '.join(AI_USAGE_OPS)}", field="op")
    if op == "list":
        return await _list(
            session,
            user,
            project_key=project_key,
            issue_ref=issue_ref,
            user_login=user_login,
            tool=tool,
            model=model,
            cost_type=cost_type,
            date_from=date_from,
            date_to=date_to,
            excluded=excluded,
            unassigned=unassigned,
            session_id=session_id,
            offset=offset,
            limit=limit,
        )
    if entry_id is None:
        raise ValidationError(message=f"op='{op}' needs entry_id", field="entry_id")
    entry = await _usage.get_visible(session, entry_id, user)
    if op == "get":
        await _log_tool(session, user, AuditEvent.RESOURCE_ACCESS, "ai_usage", {"op": op, "entry_id": entry_id})
        return await _show(session, user, entry)
    if op in ("exclude", "include"):
        entry = await _usage.set_excluded(session, user, entry, op == "exclude")
        await _log_tool(
            session,
            user,
            AuditEvent.AI_USAGE_CHANGED,
            "ai_usage",
            {"op": op, "entry_id": entry_id},
            project_id=entry.project_id,
        )
        return await _show(session, user, entry)

    changes: dict[str, Any] = {}
    if project_key is not None:
        changes["project_id"] = await _usage.project_id_for_key(session, user, project_key)
    if issue_ref is not None:
        if issue_ref.strip():
            issue = await _issue(session, user, issue_ref)
            changes["issue_id"] = issue.id
            if "project_id" not in changes:
                changes["project_id"] = issue.project_id
            elif changes["project_id"] != issue.project_id:
                raise ValidationError(message=f"Issue {issue_ref} is not in project {project_key}", field="issue_ref")
        else:
            changes["issue_id"] = None
    if comments is not None:
        changes["comments"] = comments
    for name, value in (("tool", tool), ("model", model)):
        if value is not None and value.strip():
            changes[name] = value
    if provider is not None:
        changes["provider"] = provider.strip() or None
    if cost_type is not None:
        changes["cost_type"] = cost_type or None
    day = _parse_date(usage_date, "usage_date")
    if day is not None:
        changes["usage_date"] = day
    if request_count is not None:
        changes["request_count"] = request_count
    if tokens:
        changes["tokens"] = {**entry.tokens(), **tokens}
    if cost_amount is not None or cost_currency is not None:
        cleared = (cost_amount is None or str(cost_amount).strip() == "") and not (cost_currency or "").strip()
        changes["reported_cost"] = None if cleared else _cost(cost_amount, cost_currency)
    if not changes:
        raise ValidationError(message="op='update' needs at least one field to change", field="op")
    entry = await _usage.update(session, user, entry, changes)
    await _log_tool(
        session,
        user,
        AuditEvent.AI_USAGE_CHANGED,
        "ai_usage",
        {"op": op, "entry_id": entry_id, "fields": sorted(changes)},
        project_id=entry.project_id,
    )
    return await _show(session, user, entry)


async def _list(
    session: AsyncSession,
    user: User,
    *,
    project_key: str | None,
    issue_ref: str | None,
    user_login: str | None,
    tool: str | None,
    model: str | None,
    cost_type: str | None,
    date_from: str | None,
    date_to: str | None,
    excluded: bool | None,
    unassigned: bool | None,
    offset: int,
    limit: int,
    session_id: str | None = None,
) -> str:
    """A project's entries the caller may read; without a project, the entries logged in the caller's name.

    *session_id* keeps the caller's own entries of that agent session (the
    id the reporter sends, shown on the caller's entries), in any project.
    """
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValidationError(message=f"limit must be between 1 and {_MAX_LIMIT}", field="limit")
    if offset < 0:
        raise ValidationError(message="offset must not be negative", field="offset")
    project = await _project(session, user, project_key) if project_key and project_key.strip() else None
    issue_id: int | None = None
    if issue_ref and issue_ref.strip():
        issue = await _issue(session, user, issue_ref)
        issue_id = issue.id
        if project is None:
            project = await session.get(Project, issue.project_id)
    session_id = session_id.strip() if session_id else None
    user_id: int | None
    if project is None or session_id:
        # A session is one person's client session: only the caller's own entries match.
        user_id = user.id
    elif user_login and user_login.strip():
        # An unknown login simply matches nothing: listing never says whether a login exists.
        found = await _usage.user_by_login(session, user_login)
        user_id = found.id if found is not None else -1
    else:
        user_id = None
    entries, total = await _usage.list_entries(
        session,
        user,
        project=project,
        user_id=user_id,
        date_from=_parse_date(date_from, "date_from"),
        date_to=_parse_date(date_to, "date_to"),
        tool=tool,
        model=model,
        cost_type=cost_type or None,
        issue_id=issue_id,
        excluded=excluded,
        unassigned=unassigned if project is None else None,
        external_session_id=session_id,
        offset=offset,
        limit=limit,
    )
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "ai_usage",
        {"op": "list", "project_key": project.key if project is not None else None},
        project_id=project.id if project is not None else None,
    )
    scope = f"in {project.key}" if project is not None else "logged in your name"
    if session_id:
        scope += f" from session {session_id}"
    shown = f"{offset + 1}-{offset + len(entries)}" if entries else "none"
    lines = [f"AI usage entries {scope} ({total} total, showing {shown}):"]
    if not entries:
        lines.append("  (none)")
    refs = await _usage.references(session, user, entries)
    lines.extend(f"  {_line(entry, refs, user)}" for entry in entries)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

#: Most rows the report tool writes out (the totals line always follows).
REPORT_MAX_ROWS = 200


def _report_amount(summary: Any) -> str:
    """A report amount with what could not be converted, e.g. ``12.50 USD (+ 3.00 EUR no_rate)``."""
    if summary is None:
        return "-"
    text = f"{format_decimal_plain(summary.total.quantize().amount)} {summary.currency}"
    extra = [f"{format_decimal_plain(g.amount.amount)} {g.currency} {g.reason}" for g in summary.unconverted]
    return text + (f" (+ not converted: {', '.join(extra)})" if extra else "")


def _report_line(row: Any, report: Any) -> str:
    from specivo.services.ai_spend_report_service import HIDDEN_ISSUE

    parts: list[str] = []
    if row.period is not None:
        parts.append(row.period.isoformat())
    for dim in report.group_by:
        if row.seat_only and row.key.get(dim) is None:
            label = "(unallocated seat)"
        elif dim == "issue" and row.key.get(dim) == HIDDEN_ISSUE:
            label = "(issue you cannot see)"
        else:
            label = row.labels.get(dim) or f"(no {dim})"
        parts.append(f"{dim}={label}")
    head = " ".join(parts) or "all"
    measures = [
        f"entries={row.entries}",
        f"tokens={row.total_tokens}",
        f"spend={_report_amount(row.total_spend)}",
        f"api_equivalent={_report_amount(row.api_equivalent)}",
    ]
    if report.seat_cost_shown:
        measures.insert(2, f"marginal={_report_amount(row.marginal)}")
        measures.insert(3, f"seat={_report_amount(row.seat_allocated)}")
        if row.seat_unallocated is not None and row.seat_unallocated.total.amount:
            measures.insert(4, f"unallocated_seat={_report_amount(row.seat_unallocated)}")
        if row.leverage is not None:
            measures.append(f"leverage={format_decimal_plain(row.leverage)}x")
    if row.unpriced_entries:
        measures.append(f"unpriced={row.unpriced_entries}")
    return f"{head}: " + " ".join(measures)


async def _ai_usage_report(
    session: AsyncSession,
    user: User,
    *,
    project_key: str | None,
    date_from: str | None,
    date_to: str | None,
    preset: str | None,
    group_by: str | None,
    period: str | None,
    currency: str | None,
    rate_policy: str | None,
    include_seat_cost: bool,
    user_login: str | None,
    tool: str | None,
    model: str | None,
    cost_type: str | None,
    sort: str,
    issue_ref: str | None = None,
    include_sub_issues: bool = False,
) -> str:
    """The AI spend report as text: a project's (what the caller may read) or the instance's (administrators)."""
    from specivo.core.ai_report_limits import ai_report_allowed
    from specivo.services.ai_spend_report_service import (
        PRESETS,
        AiSpendReportService,
        ReportFilters,
        default_range,
        parse_group_by,
    )

    if preset and preset not in PRESETS[:-1]:
        raise ValidationError(message=f"preset must be one of {', '.join(PRESETS[:-1])}", field="preset")
    allowed, retry_after = await ai_report_allowed(user)
    if not allowed:
        raise AppError(
            code="rate_limit_exceeded",
            message=f"Too many AI reports in the last minute. Try again in {retry_after} second(s).",
            status_code=429,
            details={"retry_after": retry_after},
        )
    first, last = default_range(preset)
    project = await _project(session, user, project_key) if project_key and project_key.strip() else None
    issue: Issue | None = None
    if issue_ref and issue_ref.strip():
        issue = await _issue(session, user, issue_ref)
        if project is None:
            # An issue's report is its project's report narrowed to the issue.
            project = await session.get(Project, issue.project_id)
        elif issue.project_id != project.id:
            raise ValidationError(
                message=f"Issue {issue.display_key} is not in project {project.key}", field="issue_ref"
            )
    user_id: int | None = None
    if user_login and user_login.strip():
        # Like listing: an unknown login matches nothing rather than saying it does not exist.
        found = await _usage.user_by_login(session, user_login)
        user_id = found.id if found is not None else -1
    report = await AiSpendReportService().report(
        session,
        user,
        project=project,
        date_from=_parse_date(date_from, "date_from") or first,
        date_to=_parse_date(date_to, "date_to") or last,
        group_by=parse_group_by(group_by if group_by is not None else "user"),
        period=period or None,
        currency=currency or None,
        rate_policy=rate_policy or None,
        include_seat_cost=include_seat_cost,
        filters=ReportFilters(
            user_id=user_id,
            tool=tool or None,
            model=model or None,
            cost_type=cost_type or None,
            issue_id=issue.id if issue is not None and not include_sub_issues else None,
            issue_tree_id=issue.id if issue is not None and include_sub_issues else None,
        ),
        sort=sort or "key",
    )
    await _log_tool(
        session,
        user,
        AuditEvent.RESOURCE_ACCESS,
        "ai_usage",
        {"op": "report", "project_key": project.key if project is not None else None},
        project_id=project.id if project is not None else None,
    )
    scope = f"project {project.key}" if project is not None else "the whole instance"
    if issue is not None:
        scope = f"issue {issue.display_key}" + (" and its sub-issues" if include_sub_issues else "") + f" in {scope}"
    lines = [
        f"AI spend report for {scope}, {report.date_from} to {report.date_to} (UTC days), "
        f"amounts in {report.currency}, rate policy {report.rate_policy}.",
        "spend = marginal spend + seat cost (subscription fees spread over the entries that used them); "
        "api_equivalent = the same tokens at pay-per-use prices, not a bill.",
    ]
    if report.seat_cost_hidden:
        lines.append("Seat cost is left out: you do not read every entry of this project in the period.")
    elif not report.include_seat_cost:
        lines.append("Seat cost is left out (include_seat_cost=false).")
    if report.rate_dates:
        lines.append("Exchange rates used from: " + ", ".join(d.isoformat() for d in report.rate_dates))
    lines.append(f"Rows ({len(report.rows)}):")
    if not report.rows:
        lines.append("  (none)")
    for row in report.rows[:REPORT_MAX_ROWS]:
        lines.append(f"  {_report_line(row, report)}")
    if len(report.rows) > REPORT_MAX_ROWS:
        from specivo.services.ai_spend_report_service import fold_rows

        rest = report.rows[REPORT_MAX_ROWS:]
        summed = fold_rows(rest, report.currency, report.seat_cost_shown)
        measures = _report_line(summed, report).split(": ", 1)[1]
        lines.append(
            f"  ... and {len(rest)} more rows, together: {measures} "
            "(narrow the period or the grouping, sort=spend for the largest first, or use the CSV export)"
        )
    lines.append(f"Total: {_report_line(report.totals, report).split(': ', 1)[1]}")
    return "\n".join(lines)
