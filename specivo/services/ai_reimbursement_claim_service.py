"""AI reimbursement claims: an employee's monthly claim, its approval, and its CSV export.

**What a claim holds.** The reimbursable amount of one person's usage in one
past month (``reimburse_share x (allocated seat cost + marginal)`` of their
employee-paid entries in projects whose payer default is ``employer``),
computed with the spend report's own rules
(:meth:`AiSpendReportService.holder_entry_spend`) and converted into the claim's
currency (the instance reporting currency unless the holder picks another
enabled one) on each entry's day. Only entries with something to reimburse are
kept. A month with nothing to reimburse, or with an amount that has no exchange
rate, is refused.

**Snapshot rule.** A claim freezes its entries: each one's id, revision, a
fingerprint of what priced and billed it (:func:`entry_fingerprint`), the
share and the amounts. Nothing writes to a claim when entries change. While
the claim is a draft, its holder refreshes it to take the current figures.
Once it is submitted, approved or paid its amounts never move; an entry that
is later edited, re-priced (``recompute_range``), re-billed (an account or
project payer default changed), moved, excluded or deleted, or whose account
share changed, is reported by :meth:`AiReimbursementClaimService.changes`,
which compares the snapshot with the entries as they are now at read time.
The claim then shows as *changed*, entry by entry, so the approver can still
approve what was asked (the frozen amount is what gets paid) or reject it so
the holder claims the month again. Late usage reported after a claim was
submitted is not added to it.

**Who does what.**

- The holder (a person, not a service account) creates, refreshes, submits,
  withdraws (submitted -> draft) and deletes (draft or rejected) their own
  claims.
- Global administrators approve or reject a submitted claim and mark an
  approved claim paid, but never their own.
- The holder and administrators see a claim (and export it); for anybody else
  it does not exist (404).
- Claims are account-wide, so a project-limited API key is refused.

Submitting, withdrawing, approving, rejecting, paying and exporting write an
``ai_reimbursement_claim_*`` audit event. Submitting mails the administrators;
a decision (approved, rejected, paid) mails the holder.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from calendar import monthrange
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from typing import Any

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_billing import PAYER_EMPLOYEE
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import format_date, recipient_locale
from specivo.core.i18n import gettext as _
from specivo.core.i18n import gettext_lazy as _l
from specivo.core.money import Money
from specivo.core.utils import utcnow
from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_reimbursement_claim import (
    CLAIM_APPROVED,
    CLAIM_DRAFT,
    CLAIM_PAID,
    CLAIM_REJECTED,
    CLAIM_STATUSES,
    CLAIM_SUBMITTED,
    AiReimbursementClaim,
    AiReimbursementClaimEntry,
)
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_billing import reimburse_factors
from specivo.services.api_key_scope import is_global_admin, require_unlimited_key
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

logger = logging.getLogger(__name__)
_audit = SecurityAuditService()

#: Longest note a holder or an approver may write.
NOTE_MAX_LENGTH = 1000
#: Why a claimed entry shows as changed, in the order they are checked.
CHANGE_DELETED = "deleted"
CHANGE_EXCLUDED = "excluded"
CHANGE_BILLING = "billing"
CHANGE_SHARE = "share"
CHANGE_REASONS: tuple[str, ...] = (CHANGE_DELETED, CHANGE_EXCLUDED, CHANGE_BILLING, CHANGE_SHARE)
#: Most administrators one submission mails.
_MAX_APPROVER_MAILS = 50

# Mail text, formatted per recipient inside ``recipient_locale``.
_SUBMITTED_SUBJECT = _l("AI reimbursement claim from {holder} for {month}")
_SUBMITTED_BODY = _l("{holder} submitted an AI reimbursement claim for {month}: {amount}, {entries} entries.")
_APPROVED_SUBJECT = _l("Your AI reimbursement claim for {month} was approved")
_APPROVED_BODY = _l("{actor} approved your AI reimbursement claim for {month}: {amount}.")
_REJECTED_SUBJECT = _l("Your AI reimbursement claim for {month} was rejected")
_REJECTED_BODY = _l("{actor} rejected your AI reimbursement claim for {month}.")
_REJECTED_REASON = _l("Reason: {reason}")
_PAID_SUBJECT = _l("Your AI reimbursement claim for {month} was paid")
_PAID_BODY = _l("{actor} marked your AI reimbursement claim for {month} as paid: {amount}.")
_VIEW_CLAIM = _l("View the claim")

#: The CSV header of a claim export.
CSV_HEADER: tuple[str, ...] = (
    "usage_date",
    "project",
    "tool",
    "model",
    "billing_account",
    "payer",
    "cost_type",
    "share",
    "marginal",
    "seat_cost",
    "reimbursable",
    "currency",
    "entry_id",
    "revision",
    "changed",
)


def _invalid(message: str, field_name: str | None = None, code: str = "ai_reimbursement_claim_invalid") -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field_name)


def _status_error(claim: AiReimbursementClaim, action: str) -> AppError:
    return AppError(
        code="ai_reimbursement_claim_status",
        message=f"A {claim.status} claim cannot be {action}",
        status_code=409,
        details={"status": claim.status},
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_month(value: object) -> date:
    """A claim month: ``YYYY-MM`` (or a date), as its first day. 422 ``ai_reimbursement_claim_invalid`` otherwise."""
    if isinstance(value, date):
        return value.replace(day=1)
    text = str(value or "").strip()
    try:
        year_text, month_text = text.split("-")[:2]
        if len(year_text) != 4 or len(month_text) != 2 or text.count("-") != 1:
            raise ValueError(text)
        return date(int(year_text), int(month_text), 1)
    except ValueError:
        raise _invalid("month must be YYYY-MM, e.g. 2026-03", "month") from None


def month_end(month: date) -> date:
    return month.replace(day=monthrange(month.year, month.month)[1])


def month_text(month: date) -> str:
    return f"{month:%Y-%m}"


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def entry_fingerprint(entry: AiUsageEntry) -> str:
    """SHA-256 (hex) of what decides an entry's reimbursable amount, apart from the account's share.

    The UTC day, project, billing account, payer, cost type, subscription and
    the stored marginal and API-equivalent costs. An entry whose fingerprint
    differs from the one a claim froze was edited, re-priced, re-billed or
    moved since. A comment edit leaves it as it was.
    """
    parts = (
        entry.usage_date.isoformat(),
        entry.project_id,
        entry.billing_account_id,
        entry.payer,
        entry.cost_type,
        entry.subscription_id,
        _decimal_text(entry.marginal_amount),
        entry.marginal_currency,
        _decimal_text(entry.api_equivalent_amount),
        entry.api_equivalent_currency,
    )
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def _factor(factors: dict[tuple[int, int], Decimal], entry: AiUsageEntry) -> Decimal:
    """The reimbursable factor of *entry* now (0 when it has none)."""
    if entry.billing_account_id is None or entry.project_id is None:
        return Decimal(0)
    return factors.get((entry.billing_account_id, entry.project_id), Decimal(0))


def _csv_cell(value: object) -> str:
    """A CSV cell; text that could start a spreadsheet formula is prefixed with ``'``."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _money_text(amount: Decimal, currency: str) -> str:
    return Money(amount, currency).quantize().amount_str


@dataclass(frozen=True, slots=True)
class ClaimChange:
    """A claimed entry that is no longer what the claim froze, and why (one of :data:`CHANGE_REASONS`)."""

    claim_entry_id: int
    entry_id: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class ClaimReferences:
    """Names a claim's pages show: project keys, billing account names and people, by id."""

    projects: dict[int, str]
    accounts: dict[int, str]
    users: dict[int, User]


@dataclass
class _Snapshot:
    currency: str
    entries: list[AiReimbursementClaimEntry]

    @property
    def total(self) -> Decimal:
        return sum((e.reimbursable_amount for e in self.entries), Decimal(0))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiReimbursementClaimService:
    """Create, read, move through the approval flow and export reimbursement claims (see the module docstring)."""

    # -- who ------------------------------------------------------------------

    @staticmethod
    def can_view(viewer: User, claim: AiReimbursementClaim) -> bool:
        """The holder and global administrators."""
        return claim.user_id == viewer.id or is_global_admin(viewer)

    @staticmethod
    def can_decide(viewer: User, claim: AiReimbursementClaim) -> bool:
        """Global administrators, on somebody else's claim."""
        return is_global_admin(viewer) and claim.user_id != viewer.id

    @staticmethod
    def can_hold(user: User) -> bool:
        """People hold claims; service and system accounts do not."""
        return not user.is_service_account and not user.is_system_principal and not user.is_anonymous

    @staticmethod
    def _require_holder(actor: User, claim: AiReimbursementClaim) -> None:
        if claim.user_id != actor.id:
            raise PermissionDeniedError(_("Only the person who made a claim can change it."))

    def _require_decider(self, actor: User, claim: AiReimbursementClaim) -> None:
        if not is_global_admin(actor):
            raise PermissionDeniedError(_("Only administrators approve, reject or pay AI reimbursement claims."))
        if claim.user_id == actor.id:
            raise PermissionDeniedError(_("You cannot decide on your own AI reimbursement claim."))

    # -- reading --------------------------------------------------------------

    async def get_visible(self, session: AsyncSession, viewer: User, claim_id: int) -> AiReimbursementClaim:
        """Claim *claim_id* if *viewer* may see it; 404 otherwise (so ids cannot be probed)."""
        require_unlimited_key()
        claim = await session.get(AiReimbursementClaim, claim_id)
        if claim is None or not self.can_view(viewer, claim):
            raise NotFoundError("AI reimbursement claim not found")
        return claim

    async def list_own(self, session: AsyncSession, user: User) -> list[AiReimbursementClaim]:
        """*user*'s claims, newest month first."""
        require_unlimited_key()
        stmt = (
            select(AiReimbursementClaim)
            .where(AiReimbursementClaim.user_id == user.id)
            .order_by(AiReimbursementClaim.month.desc(), AiReimbursementClaim.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_claims(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        status: str | None = None,
        user_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AiReimbursementClaim], int]:
        """Everyone's claims (administrators), optionally of one status or person; oldest submission first."""
        require_unlimited_key()
        if not is_global_admin(viewer):
            raise PermissionDeniedError(_("Only administrators see everyone's AI reimbursement claims."))
        conditions: list[Any] = []
        if status:
            if status not in CLAIM_STATUSES:
                raise _invalid(f"status must be one of {', '.join(CLAIM_STATUSES)}", "status")
            conditions.append(AiReimbursementClaim.status == status)
        if user_id is not None:
            conditions.append(AiReimbursementClaim.user_id == user_id)
        total = (
            await session.execute(select(func.count()).select_from(AiReimbursementClaim).where(*conditions))
        ).scalar_one()
        stmt = (
            select(AiReimbursementClaim)
            .where(*conditions)
            .order_by(
                AiReimbursementClaim.submitted_at.asc().nulls_last(),
                AiReimbursementClaim.month.desc(),
                AiReimbursementClaim.id,
            )
            .offset(offset)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all()), int(total)

    @staticmethod
    async def entries(session: AsyncSession, claim: AiReimbursementClaim) -> list[AiReimbursementClaimEntry]:
        """The entries *claim* froze, by day."""
        stmt = (
            select(AiReimbursementClaimEntry)
            .where(AiReimbursementClaimEntry.claim_id == claim.id)
            .order_by(AiReimbursementClaimEntry.usage_date, AiReimbursementClaimEntry.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def changes(session: AsyncSession, claim_ids: Iterable[int]) -> dict[int, list[ClaimChange]]:
        """Per claim, its entries that are no longer what it froze (see the module docstring).

        Three queries whatever the number of claims: the frozen entries joined
        to the entries as they are now, and the current reimbursable factors.
        Claims with no change are left out.
        """
        ids = sorted(set(claim_ids))
        if not ids:
            return {}
        stmt = (
            select(AiReimbursementClaimEntry, AiUsageEntry)
            .outerjoin(AiUsageEntry, AiUsageEntry.id == AiReimbursementClaimEntry.entry_id)
            .where(AiReimbursementClaimEntry.claim_id.in_(ids))
            .order_by(AiReimbursementClaimEntry.claim_id, AiReimbursementClaimEntry.id)
        )
        rows = list((await session.execute(stmt)).all())
        factors = await reimburse_factors(
            session,
            ((live.payer, live.billing_account_id, live.project_id) for _snap, live in rows if live is not None),
        )
        out: dict[int, list[ClaimChange]] = {}
        for snap, live in rows:
            reason: str | None = None
            if live is None:
                reason = CHANGE_DELETED
            elif live.excluded_at is not None:
                reason = CHANGE_EXCLUDED
            elif entry_fingerprint(live) != snap.fingerprint:
                reason = CHANGE_BILLING
            elif _factor(factors, live) != snap.share:
                reason = CHANGE_SHARE
            if reason is not None:
                out.setdefault(snap.claim_id, []).append(ClaimChange(snap.id, snap.entry_id, reason))
        return out

    @staticmethod
    async def references(
        session: AsyncSession, claims: Sequence[AiReimbursementClaim], entries: Sequence[AiReimbursementClaimEntry]
    ) -> ClaimReferences:
        """Project keys, billing account names and the people of *claims* (holders, deciders, payers)."""
        project_ids = {e.project_id for e in entries if e.project_id is not None}
        account_ids = {e.billing_account_id for e in entries if e.billing_account_id is not None}
        user_ids = {c.user_id for c in claims} | {
            i for c in claims for i in (c.decided_by_id, c.paid_by_id) if i is not None
        }
        projects: dict[int, str] = {}
        if project_ids:
            rows = await session.execute(select(Project.id, Project.key).where(Project.id.in_(project_ids)))
            projects = {pid: key for pid, key in rows.all()}
        accounts: dict[int, str] = {}
        if account_ids:
            rows2 = await session.execute(
                select(AiBillingAccount.id, AiBillingAccount.name).where(AiBillingAccount.id.in_(account_ids))
            )
            accounts = {aid: name for aid, name in rows2.all()}
        users: dict[int, User] = {}
        if user_ids:
            found = await session.execute(select(User).where(User.id.in_(user_ids)))
            users = {u.id: u for u in found.scalars().all()}
        return ClaimReferences(projects=projects, accounts=accounts, users=users)

    # -- making and refreshing -----------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        holder: User,
        month: object,
        *,
        currency: str | None = None,
        note: str | None = None,
    ) -> AiReimbursementClaim:
        """A draft claim of *holder* for *month* (``YYYY-MM``), from the month's reimbursable usage.

        403 for a service account or a project-limited key; 422 for a month
        that has not ended, a note over :data:`NOTE_MAX_LENGTH`, nothing to
        reimburse (``ai_reimbursement_claim_empty``) or an amount without an
        exchange rate (``ai_reimbursement_claim_unconverted``); 409
        ``ai_reimbursement_claim_exists`` when the holder has a claim for the
        month that was not rejected.
        """
        require_unlimited_key()
        if not self.can_hold(holder):
            raise PermissionDeniedError(_("Service accounts do not make AI reimbursement claims."))
        first = parse_month(month)
        if first >= utcnow().date().replace(day=1):
            raise _invalid("A claim covers a month that has ended", "month")
        text = self._note(note, "note")
        await self._require_no_claim(session, holder.id, first)
        snapshot = await self._snapshot(session, holder, first, currency)
        claim = AiReimbursementClaim(
            user_id=holder.id,
            month=first,
            status=CLAIM_DRAFT,
            total_amount=snapshot.total,
            total_currency=snapshot.currency,
            entry_count=len(snapshot.entries),
            note=text,
            computed_at=utcnow(),
        )
        session.add(claim)
        try:
            await session.flush()
        except IntegrityError:
            raise self._exists_error() from None
        for row in snapshot.entries:
            row.claim_id = claim.id
        session.add_all(snapshot.entries)
        await session.flush()
        return claim

    async def refresh(
        self, session: AsyncSession, actor: User, claim: AiReimbursementClaim, *, note: str | None = None
    ) -> AiReimbursementClaim:
        """Take the month's current figures into a draft claim (its holder only); *note* replaces the note.

        A submitted, approved or paid claim is frozen (409).
        """
        require_unlimited_key()
        self._require_holder(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_DRAFT:
            raise _status_error(claim, "refreshed")
        snapshot = await self._snapshot(session, actor, claim.month, claim.total_currency)
        await session.execute(delete(AiReimbursementClaimEntry).where(AiReimbursementClaimEntry.claim_id == claim.id))
        for row in snapshot.entries:
            row.claim_id = claim.id
        session.add_all(snapshot.entries)
        claim.total_amount = snapshot.total
        claim.entry_count = len(snapshot.entries)
        claim.computed_at = utcnow()
        if note is not None:
            claim.note = self._note(note, "note")
        await session.flush()
        return claim

    async def delete(self, session: AsyncSession, actor: User, claim: AiReimbursementClaim) -> None:
        """Delete a draft or rejected claim (its holder only); a submitted, approved or paid one stays (409)."""
        require_unlimited_key()
        self._require_holder(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status not in (CLAIM_DRAFT, CLAIM_REJECTED):
            raise _status_error(claim, "deleted")
        await session.delete(claim)
        await session.flush()

    # -- the approval flow ------------------------------------------------------

    async def submit(
        self, session: AsyncSession, actor: User, claim: AiReimbursementClaim, *, request: Request | None = None
    ) -> AiReimbursementClaim:
        """draft -> submitted (the holder). The amounts are frozen from now on; administrators are mailed."""
        require_unlimited_key()
        self._require_holder(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_DRAFT:
            raise _status_error(claim, "submitted")
        if claim.entry_count == 0:
            raise _invalid("The claim has nothing to reimburse", code="ai_reimbursement_claim_empty")
        before = claim.status
        claim.status = CLAIM_SUBMITTED
        claim.submitted_at = utcnow()
        claim.decided_at = None
        claim.decided_by_id = None
        claim.decision_note = None
        await session.flush()
        await self._log(session, AuditEvent.AI_REIMBURSEMENT_CLAIM_SUBMITTED, actor, claim, actor, before, request)
        await self._notify_submitted(session, claim, actor)
        return claim

    async def withdraw(
        self, session: AsyncSession, actor: User, claim: AiReimbursementClaim, *, request: Request | None = None
    ) -> AiReimbursementClaim:
        """submitted -> draft (the holder), so it can be refreshed or deleted."""
        require_unlimited_key()
        self._require_holder(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_SUBMITTED:
            raise _status_error(claim, "withdrawn")
        before = claim.status
        claim.status = CLAIM_DRAFT
        claim.submitted_at = None
        await session.flush()
        await self._log(session, AuditEvent.AI_REIMBURSEMENT_CLAIM_WITHDRAWN, actor, claim, actor, before, request)
        return claim

    async def approve(
        self, session: AsyncSession, actor: User, claim: AiReimbursementClaim, *, request: Request | None = None
    ) -> AiReimbursementClaim:
        """submitted -> approved (an administrator, not the holder), at the frozen amount."""
        require_unlimited_key()
        self._require_decider(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_SUBMITTED:
            raise _status_error(claim, "approved")
        changed = len((await self.changes(session, [claim.id])).get(claim.id, []))
        before = claim.status
        claim.status = CLAIM_APPROVED
        claim.decided_at = utcnow()
        claim.decided_by_id = actor.id
        claim.decision_note = None
        await session.flush()
        holder = await self._holder(session, claim)
        await self._log(
            session,
            AuditEvent.AI_REIMBURSEMENT_CLAIM_APPROVED,
            actor,
            claim,
            holder,
            before,
            request,
            changed_entries=changed,
        )
        await self._notify_holder(claim, holder, actor, _APPROVED_SUBJECT, _APPROVED_BODY)
        return claim

    async def reject(
        self,
        session: AsyncSession,
        actor: User,
        claim: AiReimbursementClaim,
        *,
        reason: str | None = None,
        request: Request | None = None,
    ) -> AiReimbursementClaim:
        """submitted -> rejected (an administrator, not the holder), with an optional reason for the holder.

        A rejected claim is final; the holder may make a new claim for the month.
        """
        require_unlimited_key()
        self._require_decider(actor, claim)
        text = self._note(reason, "reason")
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_SUBMITTED:
            raise _status_error(claim, "rejected")
        before = claim.status
        claim.status = CLAIM_REJECTED
        claim.decided_at = utcnow()
        claim.decided_by_id = actor.id
        claim.decision_note = text
        await session.flush()
        holder = await self._holder(session, claim)
        await self._log(
            session,
            AuditEvent.AI_REIMBURSEMENT_CLAIM_REJECTED,
            actor,
            claim,
            holder,
            before,
            request,
            reason_given=bool(text),
        )
        await self._notify_holder(claim, holder, actor, _REJECTED_SUBJECT, _REJECTED_BODY, reason=text)
        return claim

    async def mark_paid(
        self, session: AsyncSession, actor: User, claim: AiReimbursementClaim, *, request: Request | None = None
    ) -> AiReimbursementClaim:
        """approved -> paid (an administrator, not the holder)."""
        require_unlimited_key()
        self._require_decider(actor, claim)
        await session.refresh(claim, with_for_update=True)
        if claim.status != CLAIM_APPROVED:
            raise _status_error(claim, "marked paid")
        before = claim.status
        claim.status = CLAIM_PAID
        claim.paid_at = utcnow()
        claim.paid_by_id = actor.id
        await session.flush()
        holder = await self._holder(session, claim)
        await self._log(session, AuditEvent.AI_REIMBURSEMENT_CLAIM_PAID, actor, claim, holder, before, request)
        await self._notify_holder(claim, holder, actor, _PAID_SUBJECT, _PAID_BODY)
        return claim

    # -- export -----------------------------------------------------------------

    async def export_csv(
        self, session: AsyncSession, viewer: User, claim: AiReimbursementClaim, *, request: Request | None = None
    ) -> str:
        """The claim as CSV: one line per frozen entry, then the total. Audited.

        Amounts are in the claim's currency at its minor units; ``changed``
        names why an entry is no longer what the claim froze (empty when it
        is). Cells that could start a spreadsheet formula are prefixed with ``'``.
        """
        require_unlimited_key()
        if not self.can_view(viewer, claim):
            raise NotFoundError("AI reimbursement claim not found")
        entries = await self.entries(session, claim)
        refs = await self.references(session, [claim], entries)
        changed = {c.claim_entry_id: c.reason for c in (await self.changes(session, [claim.id])).get(claim.id, [])}
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for e in entries:
            writer.writerow(
                [
                    _csv_cell(v)
                    for v in (
                        e.usage_date.isoformat(),
                        refs.projects.get(e.project_id, "") if e.project_id is not None else "",
                        e.tool_slug,
                        e.model,
                        refs.accounts.get(e.billing_account_id, "") if e.billing_account_id is not None else "",
                        e.payer,
                        e.cost_type,
                        _decimal_text(e.share),
                        _money_text(e.marginal_amount, e.currency),
                        _money_text(e.seat_amount, e.currency),
                        _money_text(e.reimbursable_amount, e.currency),
                        e.currency,
                        e.entry_id if e.entry_id is not None else "",
                        e.revision,
                        changed.get(e.id, ""),
                    )
                ]
            )
        total_row = [""] * len(CSV_HEADER)
        total_row[0] = "total"
        total_row[CSV_HEADER.index("reimbursable")] = _money_text(claim.total_amount, claim.total_currency)
        total_row[CSV_HEADER.index("currency")] = claim.total_currency
        writer.writerow(total_row)
        holder = refs.users.get(claim.user_id)
        await self._log(
            session,
            AuditEvent.AI_REIMBURSEMENT_CLAIM_EXPORTED,
            viewer,
            claim,
            holder,
            claim.status,
            request,
            format="csv",
        )
        return buf.getvalue()

    @staticmethod
    def export_filename(claim: AiReimbursementClaim, holder: User | None) -> str:
        login = holder.login if holder is not None else f"user-{claim.user_id}"
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in login)
        return f"ai-reimbursement-claim-{safe}-{month_text(claim.month)}.csv"

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _note(value: str | None, field_name: str) -> str | None:
        text = (value or "").strip()
        if len(text) > NOTE_MAX_LENGTH:
            raise _invalid(f"{field_name} is at most {NOTE_MAX_LENGTH} characters", field_name)
        return text or None

    @staticmethod
    def _exists_error() -> AppError:
        return AppError(
            code="ai_reimbursement_claim_exists",
            message="You have a claim for this month. Refresh or delete it, or wait for its decision.",
            status_code=409,
            field="month",
        )

    async def _require_no_claim(self, session: AsyncSession, user_id: int, month: date) -> None:
        stmt = select(AiReimbursementClaim.id).where(
            AiReimbursementClaim.user_id == user_id,
            AiReimbursementClaim.month == month,
            AiReimbursementClaim.status != CLAIM_REJECTED,
        )
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise self._exists_error()

    @staticmethod
    async def _holder(session: AsyncSession, claim: AiReimbursementClaim) -> User | None:
        return await session.get(User, claim.user_id)

    @staticmethod
    async def _snapshot(session: AsyncSession, holder: User, month: date, currency: str | None) -> _Snapshot:
        """The month's reimbursable entries of *holder*, priced by the spend report's rules, as claim rows."""
        from specivo.services.ai_spend_report_service import AiSpendReportService, ReportFilters

        report = await AiSpendReportService().holder_entry_spend(
            session,
            holder,
            date_from=month,
            date_to=month_end(month),
            filters=ReportFilters(user_id=holder.id, payer=PAYER_EMPLOYEE),
            currency=currency,
        )
        target = report.currency
        rows = {int(r.key["entry"]): r for r in report.rows if r.key.get("entry") is not None and not r.seat_only}
        if not rows or not report.reimbursable_shown:
            raise _invalid("Nothing to reimburse in this month", "month", code="ai_reimbursement_claim_empty")
        loaded = await session.execute(select(AiUsageEntry).where(AiUsageEntry.id.in_(list(rows))))
        live = sorted(loaded.scalars().all(), key=lambda e: (e.usage_date, e.id))
        factors = await reimburse_factors(session, ((e.payer, e.billing_account_id, e.project_id) for e in live))
        out: list[AiReimbursementClaimEntry] = []
        missing: set[str] = set()
        for e in live:
            share: Decimal | None = None
            if e.billing_account_id is not None and e.project_id is not None:
                share = factors.get((e.billing_account_id, e.project_id))
            row = rows[e.id]
            summary = row.reimbursable
            if share is None or summary is None:
                continue
            if summary.has_unconverted:
                missing.update(g.currency for g in summary.unconverted)
                continue
            amount = summary.total.amount
            if amount <= 0:
                continue
            marginal = row.marginal.total.amount if row.marginal is not None else Decimal(0)
            seat = row.seat_allocated.total.amount if row.seat_allocated is not None else Decimal(0)
            out.append(
                AiReimbursementClaimEntry(
                    entry_id=e.id,
                    revision=e.revision,
                    fingerprint=entry_fingerprint(e),
                    usage_date=e.usage_date,
                    project_id=e.project_id,
                    billing_account_id=e.billing_account_id,
                    tool_slug=e.tool_slug,
                    model=e.model[:200],
                    payer=e.payer,
                    cost_type=e.cost_type,
                    share=share,
                    currency=target,
                    marginal_amount=marginal,
                    seat_amount=seat,
                    reimbursable_amount=amount,
                )
            )
        if missing:
            raise AppError(
                code="ai_reimbursement_claim_unconverted",
                message=(
                    f"No exchange rate into {target} for {', '.join(sorted(missing))} on some days of the month. "
                    "Add the rates, or claim in another currency."
                ),
                status_code=422,
                field="currency",
                details={"currencies": sorted(missing), "target": target},
            )
        if not out:
            raise _invalid("Nothing to reimburse in this month", "month", code="ai_reimbursement_claim_empty")
        return _Snapshot(currency=target, entries=out)

    @staticmethod
    def _details(claim: AiReimbursementClaim, holder: User | None, before: str) -> dict[str, Any]:
        return {
            "claim_id": claim.id,
            "holder_user_id": claim.user_id,
            "holder_login": holder.login if holder is not None else None,
            "month": month_text(claim.month),
            "currency": claim.total_currency,
            "total": _money_text(claim.total_amount, claim.total_currency),
            "entries": claim.entry_count,
            "status_from": before,
            "status_to": claim.status,
        }

    async def _log(
        self,
        session: AsyncSession,
        event: AuditEvent,
        actor: User,
        claim: AiReimbursementClaim,
        holder: User | None,
        before: str,
        request: Request | None,
        **extra: Any,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_ai_billing_change(
                session,
                event,
                user_id=actor.id,
                details={**self._details(claim, holder, before), **extra},
                resource_id=claim.id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)

    # -- mail -------------------------------------------------------------------

    @staticmethod
    async def _approvers(session: AsyncSession, holder: User) -> list[User]:
        stmt = (
            select(User)
            .where(
                User.is_admin.is_(True),
                User.status == "active",
                User.is_service_account.is_(False),
                User.id != holder.id,
            )
            .order_by(User.id)
            .limit(_MAX_APPROVER_MAILS)
        )
        return [u for u in (await session.execute(stmt)).scalars().all() if u.email and not u.is_system_principal]

    @staticmethod
    def _send(to: User, subject: str, lines: list[str], link: str | None, link_text: str) -> None:
        """Queue one mail; a failure is logged and never fails the step that sends it."""
        from specivo.tasks.notifications import send_notification_email

        html = "".join(f"<p>{escape(line)}</p>" for line in lines)
        plain = "\n\n".join(lines)
        if link:
            html += f'<p><a href="{escape(link)}">{escape(link_text)}</a></p>'
            plain += f"\n\n{link_text}: {link}"
        try:
            send_notification_email.delay(to.email, subject, html, plain + "\n", None)
        except Exception:
            logger.exception("Failed to queue the AI reimbursement claim mail to user %s", to.id)

    @staticmethod
    def _amount_text(claim: AiReimbursementClaim, locale: str) -> str:
        return claim.total.format(locale)

    async def _notify_submitted(self, session: AsyncSession, claim: AiReimbursementClaim, holder: User) -> None:
        from specivo.core.app_url import public_url

        link = public_url(f"/admin/ai-reimbursement-claims/{claim.id}/")
        for admin in await self._approvers(session, holder):
            with recipient_locale(admin.language) as locale:
                values = {
                    "holder": holder.display_name or holder.login,
                    "month": format_date(claim.month, "yMMMM", locale),
                    "amount": self._amount_text(claim, locale),
                    "entries": claim.entry_count,
                }
                self._send(
                    admin,
                    str(_SUBMITTED_SUBJECT).format(**values),
                    [str(_SUBMITTED_BODY).format(**values)],
                    link,
                    str(_VIEW_CLAIM),
                )

    async def _notify_holder(
        self,
        claim: AiReimbursementClaim,
        holder: User | None,
        actor: User,
        subject: Any,
        body: Any,
        *,
        reason: str | None = None,
    ) -> None:
        from specivo.core.app_url import public_url

        if holder is None or not holder.email or holder.status != "active":
            return
        link = public_url(f"/my/ai-reimbursement-claims/{claim.id}/")
        with recipient_locale(holder.language) as locale:
            values = {
                "actor": actor.display_name or actor.login,
                "month": format_date(claim.month, "yMMMM", locale),
                "amount": self._amount_text(claim, locale),
            }
            lines = [str(body).format(**values)]
            if reason:
                lines.append(str(_REJECTED_REASON).format(reason=reason))
            self._send(holder, str(subject).format(**values), lines, link, str(_VIEW_CLAIM))


__all__ = [
    "CHANGE_BILLING",
    "CHANGE_DELETED",
    "CHANGE_EXCLUDED",
    "CHANGE_REASONS",
    "CHANGE_SHARE",
    "CSV_HEADER",
    "NOTE_MAX_LENGTH",
    "AiReimbursementClaimService",
    "ClaimChange",
    "ClaimReferences",
    "entry_fingerprint",
    "month_end",
    "month_text",
    "parse_month",
]
