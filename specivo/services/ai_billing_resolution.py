"""Which billing account paid for an AI usage entry, who the payer is, and the entry's cost type.

Resolution, first match wins (``billing_method`` records the step):

1. ``manual``: a person set the account and payer on the entry; they are
   kept through replays and re-resolution (only the cost type follows the
   account's default).
2. ``record``: the record's ``billing_account`` slug, when it names one of the
   user's own active accounts or an active instance account
   (:meth:`AiBillingAccountService.find_by_slug`). A slug that names nothing
   the user may use (unknown, someone else's, inactive) is skipped and
   reported (:attr:`BillingResolution.slug_unknown`). When an entry is resolved
   again without its record, the account the record named is kept while it is
   active.
3. ``instance_route``: the first active instance account, oldest first, whose
   ``route_patterns`` match the route and whose ``tool_slugs`` cover the tool.
4. ``personal_route``: the same over the user's own accounts; then
   ``subscription``: the user's subscription covering the tool (today's
   ``subscription_for``), through its billing account. Only while the record
   asks for no cost type or for ``subscription``, and only while that account
   is active (a subscription without an account still covers the usage,
   paid by the employee).
5. ``project_default``: no account; the payer is the project's
   ``ai_usage_payer_default``. Otherwise ``none`` and payer ``unknown``.

The account's ``payer`` becomes the entry's payer. The entry's cost type is
the one the record asked for, else the account's ``default_cost_type``, else
``unknown``; usage covered by a subscription (step 4) is ``subscription``. An
entry of cost type ``subscription`` is charged to a subscription only when its
account is that subscription's (the subscription is the account's fee
schedule): a record without a cost type that resolves to a corporate gateway
is no longer charged to the person's own plan.

``route`` never feeds pricing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import (
    BILLING_METHOD_INSTANCE_ROUTE,
    BILLING_METHOD_MANUAL,
    BILLING_METHOD_NONE,
    BILLING_METHOD_PERSONAL_ROUTE,
    BILLING_METHOD_PROJECT_DEFAULT,
    BILLING_METHOD_RECORD,
    BILLING_METHOD_SUBSCRIPTION,
    PAYER_EMPLOYEE,
    PAYER_UNKNOWN,
    entry_cost_type,
    payer_from_default,
    pick_routed,
    project_payer_default,
)
from specivo.core.ai_subscriptions import utc_day
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription
from specivo.services.ai_billing_account_service import AiBillingAccountService
from specivo.services.ai_subscription_service import AiSubscriptionService

_accounts = AiBillingAccountService()
_subscriptions = AiSubscriptionService()


@dataclass(frozen=True, slots=True)
class BillingRequest:
    """What resolution reads: the usage, what the record sent, and the stored entry when re-resolving."""

    user_id: int
    tool_slug: str | None
    provider: str | None
    #: The entry's ``priced_at``: subscriptions are matched by its UTC day.
    at: datetime
    route: str | None = None
    requested_cost_type: str | None = None
    #: The record's ``billing_account`` slug.
    account_slug: str | None = None
    #: The entry's project settings, for the payer default; ``None`` without a project.
    project_settings: Mapping[str, Any] | None = None
    has_project: bool = False
    #: On re-resolution: the account a record named earlier (``record``), kept while active.
    named_account_id: int | None = None
    #: The account and payer a person set (``manual``); kept as they are.
    manual: bool = False
    manual_account_id: int | None = None
    manual_payer: str = PAYER_UNKNOWN


@dataclass(frozen=True, slots=True)
class BillingResolution:
    """The resolved account, payer, cost type and subscription of an entry."""

    method: str
    account: AiBillingAccount | None
    payer: str
    cost_type: str
    subscription_id: int | None
    #: The record named a slug no account available to the user has.
    slug_unknown: bool = False

    def columns(self) -> dict[str, Any]:
        """The entry columns this resolution sets."""
        return {
            "billing_method": self.method,
            "billing_account_id": self.account.id if self.account is not None else None,
            "payer": self.payer,
            "cost_type": self.cost_type,
            "subscription_id": self.subscription_id,
        }


@dataclass
class BillingLookups:
    """Accounts read once per batch or re-resolution run."""

    instance: list[AiBillingAccount] | None = None
    personal: dict[int, list[AiBillingAccount]] = field(default_factory=dict)

    async def instance_accounts(self, session: AsyncSession) -> list[AiBillingAccount]:
        if self.instance is None:
            self.instance = await _accounts.instance_accounts(session)
        return self.instance

    async def personal_accounts(self, session: AsyncSession, user_id: int) -> list[AiBillingAccount]:
        if user_id not in self.personal:
            self.personal[user_id] = await _accounts.personal_accounts(session, user_id)
        return self.personal[user_id]


class BillingResolver:
    """Resolves the billing of one entry (see the module docstring)."""

    async def resolve(
        self, session: AsyncSession, req: BillingRequest, *, lookups: BillingLookups | None = None
    ) -> BillingResolution:
        lookups = lookups if lookups is not None else BillingLookups()
        requested = req.requested_cost_type

        if req.manual:
            manual_account = (
                await session.get(AiBillingAccount, req.manual_account_id)
                if req.manual_account_id is not None
                else None
            )
            return await self._with_account(session, req, BILLING_METHOD_MANUAL, manual_account, req.manual_payer, None)

        account: AiBillingAccount | None = None
        method = BILLING_METHOD_NONE
        slug_unknown = False
        if req.account_slug:
            account = await _accounts.find_by_slug(session, req.account_slug, user_id=req.user_id)
            slug_unknown = account is None
        elif req.named_account_id is not None:
            named = await session.get(AiBillingAccount, req.named_account_id)
            if named is not None and named.is_active and named.owner_user_id in (None, req.user_id):
                account = named
        if account is not None:
            method = BILLING_METHOD_RECORD

        if account is None:
            account = pick_routed(await lookups.instance_accounts(session), req.route, req.tool_slug)
            method = BILLING_METHOD_INSTANCE_ROUTE
        personal: list[AiBillingAccount] = []
        if account is None:
            personal = await lookups.personal_accounts(session, req.user_id)
            account = pick_routed(personal, req.route, req.tool_slug)
            method = BILLING_METHOD_PERSONAL_ROUTE

        subscription_id: int | None = None
        if account is None and requested in (None, "subscription"):
            coverage = await _subscriptions.subscription_for(session, req.user_id, req.tool_slug, req.provider, req.at)
            sub = coverage.subscription if coverage is not None else None
            if isinstance(sub, AiSubscription):
                if sub.billing_account_id is None:
                    # A subscription without an account (none should be left): the person pays for it.
                    return BillingResolution(
                        BILLING_METHOD_SUBSCRIPTION, None, PAYER_EMPLOYEE, "subscription", sub.id, slug_unknown
                    )
                account = next((a for a in personal if a.id == sub.billing_account_id), None)
                if account is not None:
                    method = BILLING_METHOD_SUBSCRIPTION
                    subscription_id = sub.id

        if account is not None:
            resolution = await self._with_account(session, req, method, account, account.payer, subscription_id)
            return replace(resolution, slug_unknown=slug_unknown)

        payer = payer_from_default(project_payer_default(req.project_settings)) if req.has_project else PAYER_UNKNOWN
        return BillingResolution(
            method=BILLING_METHOD_PROJECT_DEFAULT if payer != PAYER_UNKNOWN else BILLING_METHOD_NONE,
            account=None,
            payer=payer,
            cost_type=requested or "unknown",
            subscription_id=None,
            slug_unknown=slug_unknown,
        )

    async def _with_account(
        self,
        session: AsyncSession,
        req: BillingRequest,
        method: str,
        account: AiBillingAccount | None,
        payer: str,
        subscription_id: int | None,
    ) -> BillingResolution:
        if method == BILLING_METHOD_SUBSCRIPTION:
            cost_type = "subscription"
        else:
            cost_type = entry_cost_type(req.requested_cost_type, account.default_cost_type if account else None)
        if cost_type != "subscription":
            subscription_id = None
        elif subscription_id is None and account is not None and account.owner_user_id == req.user_id:
            subscription_id = await self._fee_schedule(session, account, req)
        return BillingResolution(method, account, payer, cost_type, subscription_id)

    @staticmethod
    async def _fee_schedule(session: AsyncSession, account: AiBillingAccount, req: BillingRequest) -> int | None:
        """The user's subscription that is *account*'s fee schedule on the usage day, if any."""
        day = utc_day(req.at)
        stmt = (
            select(AiSubscription.id)
            .where(
                AiSubscription.billing_account_id == account.id,
                AiSubscription.user_id == req.user_id,
                AiSubscription.starts_on <= day,
                or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= day),
            )
            .order_by(AiSubscription.starts_on.desc(), AiSubscription.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


__all__ = ["BillingLookups", "BillingRequest", "BillingResolution", "BillingResolver"]
