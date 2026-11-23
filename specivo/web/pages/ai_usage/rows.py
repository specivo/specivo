"""AI usage table rows and totals."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exchange import ConversionSummary
from specivo.core.i18n import get_locale
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.services.ai_usage_service import EntryReferences, UsageSummary
from specivo.services.exchange_rate_service import ExchangeRateService
from specivo.web.pages.ai_usage._common import _fmt_int, _service, _tools


async def _tool_names(db: AsyncSession) -> dict[str, str]:
    return {t.slug: t.name for t in await _tools.list_tools(db, active_only=False)}


def _user_label(refs: EntryReferences, user_id: int) -> str:
    user = refs.users.get(user_id)
    if user is None:
        return ""
    return user.display_name or user.login


async def _rows(
    db: AsyncSession, viewer: User, entries: list[AiUsageEntry], refs: EntryReferences
) -> list[dict[str, Any]]:
    """The entries as the templates show them, with what *viewer* may do to each."""
    locale = get_locale()
    names = await _tool_names(db)
    rows = []
    for e in entries:
        editable = await _service.can_edit(db, viewer, e)
        deletable = _service.can_delete_locked(viewer, e) if e.is_locked else editable
        # Per-class numbers include the long-context bucket; its share is shown apart as well.
        tokens = e.all_tokens()
        long_total = sum(e.long_context_tokens().values())
        cache = tokens["cache_read"] + tokens["cache_write_5m"] + tokens["cache_write_1h"]
        issue_key = refs.issue_keys.get(e.issue_id) if e.issue_id is not None else None
        marginal, api_eq, reported = e.marginal, e.api_equivalent, e.reported_cost
        rows.append(
            {
                "id": e.id,
                "usage_date": e.usage_date.isoformat(),
                "user_id": e.user_id,
                "user_name": _user_label(refs, e.user_id),
                "on_behalf": e.reporter_user_id != e.user_id,
                "reporter_name": _user_label(refs, e.reporter_user_id),
                "key_name": refs.key_names.get(e.reporter_api_key_id or 0, ""),
                "source": e.source,
                "locked": e.is_locked,
                "tool": names.get(e.tool_slug, e.tool_slug),
                "provider": e.provider or "",
                "model": e.model,
                "total_tokens": _fmt_int(sum(tokens.values()), locale),
                "input": _fmt_int(tokens["input"], locale),
                "output": _fmt_int(tokens["output"], locale),
                "cache": _fmt_int(cache, locale),
                "reasoning": _fmt_int(tokens["reasoning"], locale),
                "long_context": _fmt_int(long_total, locale) if long_total else "",
                "cost_type": e.cost_type,
                "cost_basis": e.cost_basis,
                "marginal": marginal.format(locale) if marginal is not None else "",
                "api_equivalent": api_eq.format(locale) if api_eq is not None else "",
                "reported": reported.format(locale) if reported is not None else "",
                "project_key": refs.project_keys.get(e.project_id, "") if e.project_id is not None else "",
                "issue_key": issue_key or "",
                "branch": e.git_branch or "",
                "unattended": e.unattended,
                "profile": e.profile or "",
                "payer": e.payer,
                "comments": e.comments or "",
                "excluded": e.is_excluded,
                "editable": editable,
                "deletable": deletable,
            }
        )
    return rows


def _money_view(summary: ConversionSummary, locale: str) -> dict[str, Any]:
    return {
        "total": summary.total.format(locale),
        "converted": summary.converted_count,
        "unconverted": [
            {"amount": g.amount.format(locale), "reason": str(g.reason), "count": g.count} for g in summary.unconverted
        ],
        "rate_dates": [d.isoformat() for d in summary.rate_dates],
    }


async def _totals(db: AsyncSession, summary: UsageSummary, currency: str) -> dict[str, Any]:
    """Token sums and both costs in *currency*; amounts without a usable rate are listed, never dropped."""
    locale = get_locale()
    rates = ExchangeRateService()
    spend = await rates.summarize(db, summary.marginal, currency)
    api_equivalent = await rates.summarize(db, summary.api_equivalent, currency)
    tokens = summary.tokens
    return {
        "entries": summary.entries,
        "excluded": summary.excluded,
        "unpriced": summary.unpriced,
        "unpriced_models": [{"model": m, "count": n} for m, n in summary.unpriced_models],
        "currency": currency,
        "total_tokens": _fmt_int(summary.total_tokens, locale),
        "input": _fmt_int(tokens.get("input", 0), locale),
        "output": _fmt_int(tokens.get("output", 0), locale),
        "cache": _fmt_int(
            tokens.get("cache_read", 0) + tokens.get("cache_write_5m", 0) + tokens.get("cache_write_1h", 0), locale
        ),
        "spend": _money_view(spend, locale),
        "api_equivalent": _money_view(api_equivalent, locale),
    }
