"""MCP tool implementation for AI reimbursement claims: ``specivo_ai_reimbursement_claims``.

Like ``specivo.mcp.ai_billing_account_tools``: the function takes an explicit
session and user, delegates every rule to :class:`AiReimbursementClaimService`
(who may do what, the snapshot rule, the audit of each step, the mails) and
returns text for the agent. Claims belong to a person, not a project, so a
project-limited key is refused.

Reads write ``resource_access``; every change writes
``ai_reimbursement_claims_changed`` (category Agents) next to the service's
own ``ai_reimbursement_claim_*`` event.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.i18n import gettext as _
from specivo.mcp.tools._common import _log_tool
from specivo.models.ai_reimbursement_claim import CLAIM_STATUSES, AiReimbursementClaim
from specivo.models.user import User
from specivo.schemas.ai_billing_account import share_text
from specivo.services.ai_reimbursement_claim_service import (
    AiReimbursementClaimService,
    ClaimChange,
    ClaimReferences,
    month_text,
)
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.api_key_scope import is_global_admin, require_unlimited_key
from specivo.services.security_audit_service import AuditEvent

_service = AiReimbursementClaimService()

AI_REIMBURSEMENT_CLAIM_OPS = (
    "list",
    "get",
    "create",
    "refresh",
    "submit",
    "withdraw",
    "approve",
    "reject",
    "paid",
    "delete",
    "export",
)
#: The ``user`` value of ``list`` for everyone's claims (administrators).
EVERYONE = "all"
#: Most entry lines ``get`` prints.
_MAX_ENTRY_LINES = 200


def _amount(claim: AiReimbursementClaim) -> str:
    return f"{claim.total.quantize().amount_str} {claim.total_currency}"


def _line(claim: AiReimbursementClaim, refs: ClaimReferences, changes: Sequence[ClaimChange]) -> str:
    holder = refs.users.get(claim.user_id)
    parts = [
        f"#{claim.id} {month_text(claim.month)} {claim.status}",
        _amount(claim),
        f"{claim.entry_count} entries",
        f"holder {holder.login if holder is not None else claim.user_id}",
    ]
    if claim.submitted_at is not None:
        parts.append(f"submitted {claim.submitted_at:%Y-%m-%d}")
    decider = refs.users.get(claim.decided_by_id) if claim.decided_by_id is not None else None
    if claim.decided_at is not None:
        parts.append(f"decided {claim.decided_at:%Y-%m-%d} by {decider.login if decider else 'a former user'}")
    if claim.paid_at is not None:
        parts.append(f"paid {claim.paid_at:%Y-%m-%d}")
    if changes:
        parts.append(f"CHANGED: {len(changes)} entries edited, re-priced or removed since the snapshot")
    return "; ".join(parts)


async def _lines(session: AsyncSession, claims: Sequence[AiReimbursementClaim]) -> list[str]:
    refs = await _service.references(session, claims, [])
    changes = await _service.changes(session, (c.id for c in claims))
    return [_line(c, refs, changes.get(c.id, [])) for c in claims]


async def _detail(session: AsyncSession, claim: AiReimbursementClaim) -> str:
    await session.refresh(claim)
    entries = await _service.entries(session, claim)
    refs = await _service.references(session, [claim], entries)
    changes = (await _service.changes(session, [claim.id])).get(claim.id, [])
    reasons = {c.claim_entry_id: c.reason for c in changes}
    lines = [_line(claim, refs, changes)]
    if claim.note:
        lines.append(f"Note: {claim.note}")
    if claim.decision_note:
        lines.append(f"Reason: {claim.decision_note}")
    lines.append("Entries (day, project, account, tool/model, share, seat, marginal, reimbursable):")
    for e in entries[:_MAX_ENTRY_LINES]:
        project = refs.projects.get(e.project_id, "-") if e.project_id is not None else "-"
        account = refs.accounts.get(e.billing_account_id, "-") if e.billing_account_id is not None else "-"
        text = (
            f"  {e.usage_date.isoformat()} {project} {account} {e.tool_slug}/{e.model} share {share_text(e.share)} "
            f"seat {e.seat_amount.normalize():f} marginal {e.marginal_amount.normalize():f} "
            f"reimbursable {e.reimbursable_amount.normalize():f} {e.currency} (entry {e.entry_id or 'deleted'})"
        )
        if e.id in reasons:
            text += f" CHANGED: {reasons[e.id]}"
        lines.append(text)
    if len(entries) > _MAX_ENTRY_LINES:
        lines.append(f"  ... and {len(entries) - _MAX_ENTRY_LINES} more (op='export' gives them all)")
    return "\n".join(lines)


async def _log_change(session: AsyncSession, caller: User, op: str, claim_id: int, holder_id: int) -> None:
    holder = caller if holder_id == caller.id else await session.get(User, holder_id)
    details: dict[str, Any] = {
        "op": op,
        "claim_ids": [claim_id],
        "holder": holder.login if holder is not None else str(holder_id),
    }
    await _log_tool(session, caller, AuditEvent.AI_REIMBURSEMENT_CLAIMS_CHANGED, "ai_reimbursement_claims", details)


async def _ai_reimbursement_claims(
    session: AsyncSession,
    caller: User,
    op: str,
    *,
    claim_id: int | None = None,
    month: str | None = None,
    currency: str | None = None,
    note: str | None = None,
    reason: str | None = None,
    user: str | None = None,
    status: str | None = None,
) -> str:
    """List, read, make, move through approval and export AI reimbursement claims."""
    if op not in AI_REIMBURSEMENT_CLAIM_OPS:
        raise ValidationError(
            message=f"Invalid op '{op}'. Must be one of: {', '.join(AI_REIMBURSEMENT_CLAIM_OPS)}", field="op"
        )
    require_unlimited_key()
    if op == "list":
        return await _list(session, caller, user, status)
    if op == "create":
        if not month:
            raise ValidationError(message="op='create' needs month (YYYY-MM)", field="month")
        claim = await _service.create(session, caller, month, currency=currency, note=note)
        await _log_change(session, caller, op, claim.id, claim.user_id)
        return "Created a draft claim. Check it, then op='submit'.\n" + await _detail(session, claim)

    if claim_id is None:
        raise ValidationError(message=f"op='{op}' needs claim_id", field="claim_id")
    claim = await _service.get_visible(session, caller, claim_id)
    if op == "get":
        await _log_tool(
            session, caller, AuditEvent.RESOURCE_ACCESS, "ai_reimbursement_claims", {"op": op, "claim_ids": [claim.id]}
        )
        return await _detail(session, claim)
    if op == "export":
        await _log_tool(
            session, caller, AuditEvent.RESOURCE_ACCESS, "ai_reimbursement_claims", {"op": op, "claim_ids": [claim.id]}
        )
        return await _service.export_csv(session, caller, claim)

    holder_id = claim.user_id
    if op == "delete":
        await _service.delete(session, caller, claim)
        await _log_change(session, caller, op, claim_id, holder_id)
        return f"Deleted claim #{claim_id}."
    if op == "refresh":
        claim = await _service.refresh(session, caller, claim, note=note)
        done = "Refreshed the claim with the month's current figures."
    elif op == "submit":
        claim = await _service.submit(session, caller, claim)
        done = "Submitted the claim for approval. Its amounts are frozen from now on."
    elif op == "withdraw":
        claim = await _service.withdraw(session, caller, claim)
        done = "Withdrew the claim; it is a draft again."
    elif op == "approve":
        claim = await _service.approve(session, caller, claim)
        done = "Approved the claim at its frozen amount."
    elif op == "reject":
        claim = await _service.reject(session, caller, claim, reason=reason)
        done = "Rejected the claim."
    else:
        claim = await _service.mark_paid(session, caller, claim)
        done = "Marked the claim paid."
    await _log_change(session, caller, op, claim.id, holder_id)
    [line] = await _lines(session, [claim])
    return f"{done}\n{line}"


async def _list(session: AsyncSession, caller: User, user: str | None, status: str | None) -> str:
    status = (status or "").strip().lower() or None
    if status is not None and status not in CLAIM_STATUSES:
        raise ValidationError(message=f"status must be one of {', '.join(CLAIM_STATUSES)}", field="status")
    text = (user or "").strip()
    if not text or text.lower() == caller.login.lower():
        claims = await _service.list_own(session, caller)
        if status:
            claims = [c for c in claims if c.status == status]
        title = "Your AI reimbursement claims"
        target = caller.login
    else:
        if not is_global_admin(caller):
            raise PermissionDeniedError(_("Only administrators see other people's AI reimbursement claims."))
        user_id: int | None = None
        if text.lower() != EVERYONE:
            person = await AiUsageService.user_by_login(session, text)
            if person is None:
                raise NotFoundError(f"No user with the login '{text}'")
            user_id = person.id
        claims, _total = await _service.list_claims(session, caller, status=status, user_id=user_id, limit=200)
        title = "AI reimbursement claims" if user_id is None else f"{text}'s AI reimbursement claims"
        target = text
    await _log_tool(
        session,
        caller,
        AuditEvent.RESOURCE_ACCESS,
        "ai_reimbursement_claims",
        {"op": "list", "user": target, "status": status},
    )
    lines = [f"{title} ({len(claims)}{', ' + status if status else ''}):"]
    lines += [f"  {line}" for line in await _lines(session, claims)] or ["  (none)"]
    return "\n".join(lines)


__all__ = ["AI_REIMBURSEMENT_CLAIM_OPS", "EVERYONE", "_ai_reimbursement_claims"]
