"""Logging, editing, excluding and deleting AI usage entries."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import ACCOUNT_PAYERS, BILLING_METHOD_MANUAL
from specivo.core.ai_pricing import TOKEN_CLASSES
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.i18n import get_locale
from specivo.core.money import Money, MoneyError, currency_display_name, format_decimal_plain
from specivo.core.utils import utcnow
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES, AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_billing_account_service import AiBillingAccountService
from specivo.services.ai_model_price_service import AiModelPriceService
from specivo.services.ai_subscription_service import AiSubscriptionService
from specivo.services.ai_usage_service import BILLING_OVERRIDE, AiUsageInput, BillingOverride, EntryReferences
from specivo.services.currency_service import CurrencyService
from specivo.services.permission_service import Permission, check_permission
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.ai_usage._common import (
    MY_URL,
    _error_of,
    _FormError,
    _next_or,
    _parse_date,
    _project_for,
    _redirect,
    _service,
    _target,
    _tools,
)
from specivo.web.pages.ai_usage.rows import _rows

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)


#: Token classes behind the "more tokens" disclosure of the form.
_EXTRA_TOKENS = ("cache_read", "cache_write_5m", "cache_write_1h", "reasoning")

_accounts = AiBillingAccountService()

#: Values of the billing field of the edit form: "" (automatic), ``account:<id>`` or ``payer:<payer>``.
_ACCOUNT_PREFIX = "account:"
_PAYER_PREFIX = "payer:"


def _billing_value(entry: AiUsageEntry) -> str:
    """The billing field of *entry*: its override, or "" when the billing is resolved automatically."""
    if entry.billing_method != BILLING_METHOD_MANUAL:
        return ""
    if entry.billing_account_id is not None:
        return f"{_ACCOUNT_PREFIX}{entry.billing_account_id}"
    return f"{_PAYER_PREFIX}{entry.payer}"


def _override_of(value: str) -> BillingOverride | None:
    """The billing override a submitted billing field asks for; ``None`` clears it."""
    if not value:
        return None
    if value.startswith(_ACCOUNT_PREFIX) and value[len(_ACCOUNT_PREFIX) :].isdigit():
        return BillingOverride(account_id=int(value[len(_ACCOUNT_PREFIX) :]))
    if value.startswith(_PAYER_PREFIX) and value[len(_PAYER_PREFIX) :] in ACCOUNT_PAYERS:
        return BillingOverride(payer=value[len(_PAYER_PREFIX) :])
    raise _FormError("billing_invalid", "billing_account")


# ---------------------------------------------------------------------------
# Entry form (log and edit)
# ---------------------------------------------------------------------------


def _blank_form(project: Project | None, currency: str) -> dict[str, Any]:
    return {
        "project": project.key if project else "",
        "issue": "",
        "on_behalf_of": "",
        "tool": "claude-code",
        "provider": "",
        "model": "",
        "usage_date": utcnow().date().isoformat(),
        "tokens": {c: "" for c in TOKEN_CLASSES},
        "request_count": "",
        "cost_type": "",
        "cost_amount": "",
        "cost_currency": currency,
        "comments": "",
        "billing": "",
    }


def _form_of(entry: AiUsageEntry, refs: EntryReferences, currency: str) -> dict[str, Any]:
    reported = entry.reported_cost
    return {
        "project": refs.project_keys.get(entry.project_id, "") if entry.project_id is not None else "",
        "issue": refs.issue_keys.get(entry.issue_id, "") if entry.issue_id is not None else "",
        "on_behalf_of": "",
        "tool": entry.tool_slug,
        "provider": entry.provider or "",
        "model": entry.model,
        "usage_date": entry.usage_date.isoformat(),
        "tokens": {c: str(v) for c, v in entry.tokens().items()},
        "request_count": str(entry.request_count or ""),
        "cost_type": entry.cost_type,
        "cost_amount": format_decimal_plain(reported.amount) if reported is not None else "",
        "cost_currency": reported.currency if reported is not None else currency,
        "comments": entry.comments or "",
        "billing": _billing_value(entry),
    }


def _read_form(  # noqa: PLR0913
    *,
    project: str,
    issue: str,
    on_behalf_of: str,
    tool: str,
    provider: str,
    model: str,
    usage_date: str,
    tokens: dict[str, str],
    request_count: str,
    cost_type: str,
    cost_amount: str,
    cost_currency: str,
    comments: str,
    billing: str | None = None,
) -> dict[str, Any]:
    return {
        "project": project.strip().upper(),
        "issue": issue.strip().upper(),
        "on_behalf_of": on_behalf_of.strip(),
        "tool": tool.strip().lower(),
        "provider": provider.strip(),
        "model": model.strip(),
        "usage_date": usage_date.strip(),
        "tokens": {c: (tokens.get(c) or "").strip() for c in TOKEN_CLASSES},
        "request_count": request_count.strip(),
        "cost_type": cost_type if cost_type in AI_USAGE_COST_TYPES else "",
        "cost_amount": cost_amount.strip(),
        "cost_currency": cost_currency.strip().upper(),
        "comments": comments,
        # None: the form had no billing field (the override stays as it is).
        "billing": billing.strip() if billing is not None else None,
    }


def _usage_values(form: dict[str, Any]) -> dict[str, Any]:
    """The usage fields of a submitted form, parsed; raises ``_FormError`` on what is not a number or a date."""
    day = _parse_date(form["usage_date"])
    if day is None:
        raise _FormError("date_invalid", "usage_date")
    counts: dict[str, int] = {}
    for name, raw in form["tokens"].items():
        if not raw:
            counts[name] = 0
        elif raw.isdigit():
            counts[name] = int(raw)
        else:
            raise _FormError("tokens_invalid", name)
    if not any(counts.values()):
        raise _FormError("tokens_missing", "input")
    raw_count = form["request_count"]
    if raw_count and not raw_count.isdigit():
        raise _FormError("request_count_invalid", "request_count")
    cost: Money | None = None
    if form["cost_amount"]:
        try:
            amount = Decimal(form["cost_amount"].replace(",", "."))
            cost = Money(amount, form["cost_currency"])
        except (InvalidOperation, MoneyError, ValueError):
            raise _FormError("amount_invalid", "cost_amount") from None
    return {
        "tool": form["tool"],
        "provider": form["provider"] or None,
        "model": form["model"],
        "usage_date": day,
        "tokens": counts,
        "request_count": int(raw_count) if raw_count else 0,
        "cost_type": form["cost_type"] or None,
        "reported_cost": cost,
    }


async def _form_choices(
    db: AsyncSession, user: User, form: dict[str, Any], *, fixed_project: Project | None, entry: AiUsageEntry | None
) -> dict[str, Any]:
    """Pickers: tools, model suggestions, currencies, projects; and the subscriptions behind "Automatic"."""
    locale = get_locale()
    tools = [t for t in await _tools.list_tools(db, active_only=False) if t.is_active or t.slug == form["tool"]]
    currencies = await CurrencyService().list_currencies(db, active_only=True, include=form["cost_currency"] or None)
    projects = [] if fixed_project else await _service.loggable_projects(db, user)
    if entry is not None and entry.project_id is not None and all(p.id != entry.project_id for p in projects):
        # The entry's own project stays choosable, even where the reader may not log (a manager's edit).
        current = await db.get(Project, entry.project_id)
        if current is not None:
            projects = [*projects, current]
    owner_id = entry.user_id if entry is not None else user.id
    # Subscriptions are private to their owner and admins (the register's rule): a
    # manager editing someone else's entry sees no plan list.
    subs: list[Any] = []
    if owner_id == user.id or user.is_admin:
        subs, _total = await AiSubscriptionService().list_subscriptions(
            db, user_id=owner_id, active_on=utcnow().date(), limit=20
        )
    if fixed_project is not None:
        on_behalf = await _service.may_log_for_others(db, user, fixed_project)
    else:
        on_behalf = await _service.may_log_for_others_anywhere(db, user)
    billing_accounts = await _billing_choices(db, user, entry) if entry is not None else None
    return {
        "tools": [{"slug": t.slug, "name": t.name, "provider": t.default_provider or ""} for t in tools],
        "models": [{"provider": p, "model": m} for p, m in await AiModelPriceService().model_suggestions(db)],
        "currencies": [
            {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)} for c in currencies
        ],
        "projects": [{"key": p.key, "name": p.name} for p in projects],
        "subscriptions": [{"name": s.plan_name, "provider": s.provider} for s in subs],
        "can_log_for_others": on_behalf and entry is None,
        "cost_types": list(AI_USAGE_COST_TYPES),
        "extra_tokens": _EXTRA_TOKENS,
        "billing_accounts": billing_accounts,
        "payers": list(ACCOUNT_PAYERS),
    }


async def _billing_choices(db: AsyncSession, user: User, entry: AiUsageEntry) -> dict[str, list[dict[str, Any]]]:
    """The accounts the billing override may name: the instance's, then the entry user's own.

    Active ones, plus the entry's current account; a person's own accounts
    only for that person and administrators (a manager sees company accounts).
    """

    def keep(a: AiBillingAccount) -> bool:
        return (a.is_active or a.id == entry.billing_account_id) and _accounts.can_view(user, a)

    def choice(a: AiBillingAccount) -> dict[str, Any]:
        return {"value": f"{_ACCOUNT_PREFIX}{a.id}", "name": a.name, "slug": a.slug, "payer": a.payer}

    instance = await _accounts.instance_accounts(db, active_only=False)
    personal = await _accounts.personal_accounts(db, entry.user_id, active_only=False)
    by_name = sorted(instance, key=lambda a: a.name.lower()), sorted(personal, key=lambda a: a.name.lower())
    return {
        "instance": [choice(a) for a in by_name[0] if keep(a)],
        "personal": [choice(a) for a in by_name[1] if keep(a)],
    }


async def _render_form(  # noqa: PLR0913
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    form: dict[str, Any],
    fixed_project: Project | None,
    form_action: str,
    back_url: str,
    next_url: str,
    entry: AiUsageEntry | None = None,
    entry_row: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, Any] = {
        "user": user,
        "active_page": "ai_usage" if fixed_project else "ai-usage",
        "project": fixed_project,
        "form": form,
        "form_action": form_action,
        "back_url": back_url,
        "next_url": next_url,
        "entry": entry_row,
        "locked": bool(entry is not None and entry.is_locked),
        "error": error or {},
        **await _form_choices(db, user, form, fixed_project=fixed_project, entry=entry),
    }
    if fixed_project is not None:
        context["active_project"] = fixed_project
    return get_templates().TemplateResponse(
        request, "pages/ai_usage/form.html", context=context, status_code=status_code
    )


async def _require_can_open_form(db: AsyncSession, user: User, project: Project | None) -> None:
    """403 on the project form for whoever may log neither own usage nor others' there."""
    if project is None:
        return
    if await check_permission(user, project.id, Permission.LOG_AI_USAGE, db):
        return
    if await _service.may_log_for_others(db, user, project):
        return
    raise PermissionDeniedError("Permission 'log_ai_usage' denied for this project")


@router.get("/projects/{project_key}/ai-usage/new/", response_class=HTMLResponse)
async def project_ai_usage_form(
    project_key: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    issue: str = Query(""),
    next: str = Query(""),
) -> Response:
    """The manual entry form in a project (``?issue=`` fills the issue)."""
    project = await _project_for(db, user, project_key)
    await _require_can_open_form(db, user, project)
    currency = await CurrencyService().reporting_currency_for(db, project)
    form = _blank_form(project, currency)
    form["issue"] = issue.strip().upper()[:50]
    back = _next_or(next, f"/projects/{project.key}/ai-usage/")
    return await _render_form(
        request,
        db,
        user,
        form=form,
        fixed_project=project,
        form_action=f"/projects/{project.key}/ai-usage/new/",
        back_url=back,
        next_url=back,
    )


@router.get(MY_URL + "new/", response_class=HTMLResponse)
async def my_ai_usage_form(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The manual entry form for one's own usage, in any project one may log in or unassigned."""
    currency = await CurrencyService().reporting_currency_for(db, None)
    return await _render_form(
        request,
        db,
        user,
        form=_blank_form(None, currency),
        fixed_project=None,
        form_action=MY_URL + "new/",
        back_url=MY_URL,
        next_url=MY_URL,
    )


async def _create(
    request: Request,
    db: AsyncSession,
    user: User,
    form: dict[str, Any],
    *,
    fixed_project: Project | None,
    form_action: str,
    next_url: str,
) -> Response:
    try:
        if fixed_project is not None:
            form["project"] = fixed_project.key
        project, issue = await _target(db, user, form["project"], form["issue"])
        values = _usage_values(form)
        data = AiUsageInput(**values, issue_id=issue.id if issue else None, comments=form["comments"])
        async with db.begin_nested():
            await _service.create_for(
                db,
                user,
                data,
                project=project,
                on_behalf_of=form["on_behalf_of"] or None,
                source="manual",
                request=request,
            )
    except (AppError, _FormError) as exc:
        return await _render_form(
            request,
            db,
            user,
            form=form,
            fixed_project=fixed_project,
            form_action=form_action,
            back_url=next_url,
            next_url=next_url,
            error=_error_of(exc),
            status_code=422,
        )
    return _redirect(next_url, {"notice": "logged"})


_FORM_FIELDS = (
    "project",
    "issue",
    "on_behalf_of",
    "tool",
    "provider",
    "model",
    "usage_date",
    "request_count",
    "cost_type",
    "cost_amount",
    "cost_currency",
    "comments",
)


async def _submitted(request: Request) -> tuple[dict[str, Any], str]:
    """The entry form as posted (every field optional) and its ``next``."""
    data = await request.form()

    def text(name: str) -> str:
        value = data.get(name, "")
        return value if isinstance(value, str) else ""

    form = _read_form(
        **{name: text(name) for name in _FORM_FIELDS},
        tokens={c: text(f"tokens_{c}") for c in TOKEN_CLASSES},
        billing=text("billing") if "billing" in data else None,
    )
    return form, text("next")


@router.post("/projects/{project_key}/ai-usage/new/", response_model=None)
async def project_ai_usage_create(
    project_key: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    project = await _project_for(db, user, project_key)
    form, raw_next = await _submitted(request)
    next_url = _next_or(raw_next, f"/projects/{project.key}/ai-usage/")
    return await _create(
        request,
        db,
        user,
        form,
        fixed_project=project,
        form_action=f"/projects/{project.key}/ai-usage/new/",
        next_url=next_url,
    )


@router.post(MY_URL + "new/", response_model=None)
async def my_ai_usage_create(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    form, raw_next = await _submitted(request)
    return await _create(
        request, db, user, form, fixed_project=None, form_action=MY_URL + "new/", next_url=_next_or(raw_next, MY_URL)
    )


async def _editable_entry(db: AsyncSession, user: User, entry_id: int) -> AiUsageEntry:
    """Entry *entry_id* if *user* may read it (else 404) and change it (else 403)."""
    entry = await _service.get_visible(db, entry_id, user)
    if not await _service.can_edit(db, user, entry):
        raise PermissionDeniedError("You do not have permission to edit this AI usage entry")
    return entry


async def _edit_context(db: AsyncSession, user: User, entry: AiUsageEntry) -> tuple[EntryReferences, dict[str, Any]]:
    refs = await _service.references(db, user, [entry])
    return refs, (await _rows(db, user, [entry], refs))[0]


@router.get("/ai-usage/{entry_id}/edit/", response_class=HTMLResponse)
async def ai_usage_edit_form(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Query(""),
) -> Response:
    """Edit an entry: everything on manual, API and MCP entries; project, issue and comments on reported ones."""
    entry = await _editable_entry(db, user, entry_id)
    refs, row = await _edit_context(db, user, entry)
    currency = await CurrencyService().reporting_currency_for(db, None)
    project_key = refs.project_keys.get(entry.project_id or 0)
    fallback = MY_URL if entry.user_id == user.id or not project_key else f"/projects/{project_key}/ai-usage/"
    back = _next_or(next, fallback)
    return await _render_form(
        request,
        db,
        user,
        form=_form_of(entry, refs, currency),
        fixed_project=None,
        form_action=f"/ai-usage/{entry.id}/edit/",
        back_url=back,
        next_url=back,
        entry=entry,
        entry_row=row,
    )


@router.post("/ai-usage/{entry_id}/edit/", response_model=None)
async def ai_usage_update(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    entry = await _editable_entry(db, user, entry_id)
    form, raw_next = await _submitted(request)
    next_url = _next_or(raw_next, MY_URL)
    try:
        project, issue = await _target(db, user, form["project"], form["issue"])
        changes: dict[str, Any] = {
            "project_id": project.id if project else None,
            "issue_id": issue.id if issue else None,
            "comments": form["comments"],
        }
        if not entry.is_locked:
            changes.update(_usage_values(form))
        if form["billing"] is not None and form["billing"] != _billing_value(entry):
            changes[BILLING_OVERRIDE] = _override_of(form["billing"])
        async with db.begin_nested():
            await _service.update(db, user, entry, changes, request=request)
    except (AppError, _FormError) as exc:
        entry = await _service.get_visible(db, entry_id, user)
        _refs, row = await _edit_context(db, user, entry)
        return await _render_form(
            request,
            db,
            user,
            form=form,
            fixed_project=None,
            form_action=f"/ai-usage/{entry.id}/edit/",
            back_url=next_url,
            next_url=next_url,
            entry=entry,
            entry_row=row,
            error=_error_of(exc),
            status_code=422,
        )
    return _redirect(next_url, {"notice": "saved"})


# ---------------------------------------------------------------------------
# Row actions
# ---------------------------------------------------------------------------


async def _set_excluded(
    request: Request, db: AsyncSession, user: User, entry_id: int, excluded: bool, next_raw: str
) -> Response:
    entry = await _service.get_visible(db, entry_id, user)
    await _service.set_excluded(db, user, entry, excluded, request=request)
    return _redirect(_next_or(next_raw, MY_URL), {"notice": "excluded" if excluded else "included"})


@router.post("/ai-usage/{entry_id}/exclude/", response_model=None)
async def ai_usage_exclude(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),
) -> Response:
    """Leave an entry out of totals and reports; a reporter's later updates keep it excluded."""
    return await _set_excluded(request, db, user, entry_id, True, next)


@router.post("/ai-usage/{entry_id}/include/", response_model=None)
async def ai_usage_include(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),
) -> Response:
    """Count an excluded entry again."""
    return await _set_excluded(request, db, user, entry_id, False, next)


@router.post("/ai-usage/{entry_id}/delete/", response_model=None)
async def ai_usage_delete(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),
) -> Response:
    """Delete an entry: manual, API and MCP entries by whoever may edit them; reported ones by administrators."""
    entry = await _service.get_visible(db, entry_id, user)
    await _service.delete(db, user, entry, request=request)
    return _redirect(_next_or(next, MY_URL), {"notice": "deleted"})
