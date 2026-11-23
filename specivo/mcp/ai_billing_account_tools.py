"""MCP tool implementation for AI billing accounts: ``specivo_ai_billing_accounts``.

Like ``specivo.mcp.ai_subscription_tools``: the function takes an explicit
session and user, delegates every rule to :class:`AiBillingAccountService`
(who may change which account, the self-service setting, the audit of changes
to instance accounts and to somebody else's) and returns text for the agent.

Accounts belong to the instance or to a person, not to a project, so a
project-limited key is refused, as on the REST routes. ``owner`` picks whose
accounts: left out, the caller's own (and, for ``list``, the instance
accounts too); ``instance`` the company accounts; a login somebody else's
(administrators only).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.mcp.tools._common import _log_tool
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.user import User
from specivo.schemas.ai_billing_account import share_text
from specivo.services.ai_billing_account_service import (
    INSTANCE_OWNER,
    AiBillingAccountService,
    BillingAccountInput,
)
from specivo.services.ai_subscription_service import AiSubscriptionService
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.api_key_scope import is_global_admin, is_limited_key
from specivo.services.security_audit_service import AuditEvent

_service = AiBillingAccountService()
_subscriptions = AiSubscriptionService()

AI_BILLING_ACCOUNT_OPS = ("list", "create", "update", "deactivate")
#: Fields ``create`` and ``update`` take (the service's own names).
ACCOUNT_ARGS = (
    "slug",
    "name",
    "kind",
    "payer",
    "reimburse_share",
    "route_patterns",
    "tool_slugs",
    "default_cost_type",
    "is_active",
)
#: Fields ``''`` clears on ``update``.
_CLEARABLE = ("reimburse_share", "default_cost_type")


def _require_unlimited_key() -> None:
    if is_limited_key():
        raise PermissionDeniedError(
            _(
                "AI billing accounts belong to the instance or to a person, not to a project: "
                "connect with an unlimited API key to manage them."
            )
        )


async def _owner(session: AsyncSession, caller: User, owner: str | None) -> User | None:
    """Whose accounts: ``None`` for the instance, else the caller or the person *owner* names (administrators)."""
    text = (owner or "").strip()
    if text.lower() == INSTANCE_OWNER:
        return None
    if not text or text.lower() == caller.login.lower():
        return caller
    if not is_global_admin(caller):
        raise PermissionDeniedError(_("Only administrators manage other people's AI billing accounts."))
    user = await AiUsageService.user_by_login(session, text)
    if user is None:
        raise NotFoundError(f"No user with the login '{text}'")
    return user


def _share(value: object) -> str | None:
    """A share for the service: a JSON number is read as the decimal it was written as."""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value  # type: ignore[return-value]


def _line(account: AiBillingAccount, owner_login: str | None, subscription_ids: Sequence[int]) -> str:
    whose = "instance" if account.owner_user_id is None else (owner_login or f"user {account.owner_user_id}")
    parts = [
        f"#{account.id} {account.name} [{account.slug}] {account.kind}, {whose}",
        f"payer {account.payer}",
    ]
    share = share_text(account.reimburse_share)
    if share is not None:
        parts.append(f"reimbursed {share}")
    parts.append(f"routes: {' '.join(account.route_patterns) if account.route_patterns else 'none'}")
    parts.append(f"tools: {', '.join(account.tool_slugs) if account.tool_slugs else 'every tool'}")
    if account.default_cost_type:
        parts.append(f"default cost type {account.default_cost_type}")
    if subscription_ids:
        parts.append("fee schedule of subscription " + ", ".join(f"#{i}" for i in subscription_ids))
    if not account.is_active:
        parts.append("OFF")
    return "; ".join(parts)


async def _lines(session: AsyncSession, accounts: Sequence[AiBillingAccount]) -> list[str]:
    owners = await _subscriptions.owners(session, (a.owner_user_id for a in accounts if a.owner_user_id is not None))
    links = await _service.subscription_links(session, (a.id for a in accounts))
    return [
        _line(
            a,
            owners[a.owner_user_id].login if a.owner_user_id is not None and a.owner_user_id in owners else None,
            links.get(a.id, []),
        )
        for a in accounts
    ]


def _values(changes: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    """The service's changes from the tool's arguments: ``None`` leaves a field, ``''`` clears a clearable one."""
    values: dict[str, Any] = {}
    for name in ACCOUNT_ARGS:
        value = changes.get(name)
        if value is None:
            continue
        if name in _CLEARABLE and value == "":
            values[name] = None
        elif name == "reimburse_share":
            values[name] = _share(value)
        else:
            values[name] = value
    if not creating and not values:
        raise ValidationError(
            message=f"op='update' needs at least one field to change: {', '.join(ACCOUNT_ARGS)}", field="op"
        )
    return values


async def _log_change(
    session: AsyncSession, caller: User, op: str, account: AiBillingAccount, *, fields: list[str] | None = None
) -> None:
    if account.owner_user_id is None:
        owner = INSTANCE_OWNER
    elif account.owner_user_id == caller.id:
        owner = caller.login
    else:
        user = await session.get(User, account.owner_user_id)
        owner = user.login if user is not None else str(account.owner_user_id)
    details: dict[str, Any] = {"op": op, "account_ids": [account.id], "owner": owner}
    if fields:
        details["fields"] = fields
    await _log_tool(session, caller, AuditEvent.AI_BILLING_ACCOUNTS_CHANGED, "ai_billing_accounts", details)


async def _ai_billing_accounts(
    session: AsyncSession,
    caller: User,
    op: str,
    *,
    owner: str | None = None,
    account_id: int | None = None,
    changes: dict[str, Any] | None = None,
    include_inactive: bool = False,
) -> str:
    """List, create, change or deactivate AI billing accounts; see ``specivo_ai_billing_accounts``."""
    if op not in AI_BILLING_ACCOUNT_OPS:
        raise ValidationError(
            message=f"Invalid op '{op}'. Must be one of: {', '.join(AI_BILLING_ACCOUNT_OPS)}", field="op"
        )
    _require_unlimited_key()
    if op == "list":
        return await _list(session, caller, owner, include_inactive)
    if op == "create":
        return await _create(session, caller, owner, changes or {})

    if account_id is None:
        raise ValidationError(message=f"op='{op}' needs account_id", field="account_id")
    account = await _service.get_visible(session, caller, account_id)
    if op == "deactivate":
        account = await _service.deactivate(session, account, actor=caller)
        await session.flush()
        await session.refresh(account)
        await _log_change(session, caller, op, account)
        [line] = await _lines(session, [account])
        return f"Switched off {line}. Usage already paid through it keeps pointing at it."
    values = _values(changes or {}, creating=False)
    account = await _service.update(session, account, values, actor=caller)
    await session.flush()
    await session.refresh(account)
    await _log_change(session, caller, op, account, fields=sorted(values))
    [line] = await _lines(session, [account])
    return f"Updated {line}"


async def _list(session: AsyncSession, caller: User, owner: str | None, include_inactive: bool) -> str:
    whose = await _owner(session, caller, owner)
    if owner is None or not owner.strip():
        accounts = await _service.list_visible(session, caller, active_only=not include_inactive)
        title = "AI billing accounts: the instance's, then yours"
        target = caller.login
    elif whose is None:
        accounts = await _service.instance_accounts(session, active_only=not include_inactive)
        title = "Instance AI billing accounts"
        target = INSTANCE_OWNER
    else:
        accounts = await _service.personal_accounts(session, whose.id, active_only=not include_inactive)
        title = "Your AI billing accounts" if whose.id == caller.id else f"{whose.login}'s AI billing accounts"
        target = whose.login
    await _log_tool(session, caller, AuditEvent.RESOURCE_ACCESS, "ai_billing_accounts", {"op": "list", "owner": target})
    lines = [f"{title} ({len(accounts)}{'' if include_inactive else ', active only'}):"]
    lines += [f"  {line}" for line in await _lines(session, accounts)] or ["  (none)"]
    if whose is not None and whose.id == caller.id and not await _service.can_manage(session, caller, caller.id):
        lines.append("You cannot change your accounts now: self-service is off, or this is a service account.")
    return "\n".join(lines)


async def _create(session: AsyncSession, caller: User, owner: str | None, changes: dict[str, Any]) -> str:
    whose = await _owner(session, caller, owner)
    values = _values(changes, creating=True)
    missing = [name for name in ("slug", "name", "kind", "payer") if name not in values]
    if missing:
        raise ValidationError(message=f"op='create' needs {', '.join(missing)}", field=missing[0])
    account = await _service.create(session, BillingAccountInput(**values), owner=whose, actor=caller)
    await session.flush()
    await session.refresh(account)
    await _log_change(session, caller, "create", account)
    [line] = await _lines(session, [account])
    return f"Created {line}"
