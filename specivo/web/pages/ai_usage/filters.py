"""The AI usage list filters: query parsing, choices and the person filter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import gettext as _
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES
from specivo.models.project import Project
from specivo.models.role import AI_USAGE_VISIBILITY_ALL
from specivo.models.user import User
from specivo.services.permission_service import ai_usage_visibility
from specivo.web.pages.ai_usage._common import MY_URL, NONE_VALUE, _issues, _parse_date, _service, _tools


@dataclass
class Filters:
    """The list filters as typed (strings) and as the service takes them."""

    user_id: str = ""
    tool: str = ""
    model: str = ""
    cost_type: str = ""
    issue: str = ""
    from_date: str = ""
    to_date: str = ""
    status: str = ""
    project: str = ""
    provider: str = ""
    branch: str = ""
    errors: list[str] = field(default_factory=list)

    def query(self) -> str:
        """The filters as an urlencoded query string, for pagination and return links."""
        values = {
            "user_id": self.user_id,
            "tool": self.tool,
            "model": self.model,
            "cost_type": self.cost_type,
            "issue": self.issue,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "status": self.status,
            "project": self.project,
            "provider": self.provider,
            "branch": self.branch,
        }
        return urlencode({k: v for k, v in values.items() if v})

    @property
    def excluded(self) -> bool | None:
        return {"counted": False, "excluded": True}.get(self.status)

    def extra(self) -> dict[str, Any]:
        """The provider and branch filters as ``list_entries`` takes them ("-": entries without a branch)."""
        return {
            "provider": self.provider or None,
            "branch": self.branch if self.branch and self.branch != NONE_VALUE else None,
            "without_branch": self.branch == NONE_VALUE,
        }


def _filters(  # noqa: PLR0913
    *,
    user_id: str = "",
    tool: str = "",
    model: str = "",
    cost_type: str = "",
    issue: str = "",
    from_date: str = "",
    to_date: str = "",
    status: str = "",
    project: str = "",
    provider: str = "",
    branch: str = "",
) -> Filters:
    return Filters(
        user_id=user_id.strip() if user_id.strip().isdigit() else "",
        tool=tool.strip().lower()[:50],
        model=model.strip()[:200],
        cost_type=cost_type if cost_type in AI_USAGE_COST_TYPES else "",
        issue=issue.strip().upper()[:50],
        from_date=from_date if _parse_date(from_date) else "",
        to_date=to_date if _parse_date(to_date) else "",
        status=status if status in ("counted", "excluded") else "",
        project=project.strip().upper()[:50],
        provider=provider.strip().lower()[:50],
        branch=branch.strip()[:255],
    )


async def _issue_filter(db: AsyncSession, user: User, key: str, project: Project | None) -> int | None:
    """The id of the issue *key* the reader may see (in *project* when given); -1 for none, so nothing matches.

    ``"-"`` (entries without an issue) is handled by the caller (``without_issue``).
    """
    if not key or key == NONE_VALUE:
        return None
    try:
        issue = await _issues.get_by_display_key(db, key, user=user)
    except NotFoundError:
        return -1
    if project is not None and issue.project_id != project.id:
        return -1
    return issue.id


def _bucket_links(filters: Filters, chosen: str, url: str = MY_URL) -> list[dict[str, Any]]:
    """The chart's bucket switch: automatic, or a size the reader picks (the server still caps the points)."""
    base = filters.query()
    out = []
    for value, label in (("", _("Auto")), ("day", _("Day")), ("week", _("Week")), ("month", _("Month"))):
        query = "&".join(q for q in (base, urlencode({"bucket": value}) if value else "") if q)
        out.append(
            {"value": value, "label": label, "url": url + (f"?{query}" if query else ""), "active": value == chosen}
        )
    return out


async def sees_everyone(db: AsyncSession, user: User, project: Project) -> bool:
    """Whether *user* may read everyone's AI usage in *project*: the pages then offer the person filter."""
    return bool(user.is_admin) or await ai_usage_visibility(db, user, project) == AI_USAGE_VISIBILITY_ALL


async def person_filter_chip(
    db: AsyncSession, viewer: User, project: Project | None, user_id: str, login: str, clear_url: str
) -> dict[str, Any] | None:
    """The chip of the person filter (``filter_combobox``): the person by name, or what was typed.

    The person is named only when the filter may name them for *viewer*
    (``AiUsageService.people_clause``: people with entries the reader may see
    in *project*, everyone with usage or a plan on the instance report), so
    an id in a shared link never tells more than the list does.
    """
    from specivo.web.pages.issues import person_chip

    if not user_id:
        return None
    person = None
    if user_id != "0":
        person = await _service.person_in_scope(db, viewer, project, user_id=int(user_id))
    chip = person_chip(person) if person is not None else {"label": login or _("Unknown person")}
    chip.update(value=user_id, clear_url=clear_url)
    return chip


async def _filter_choices(db: AsyncSession) -> dict[str, Any]:
    return {
        "tools": [{"slug": t.slug, "name": t.name} for t in await _tools.list_tools(db, active_only=False)],
        "cost_types": list(AI_USAGE_COST_TYPES),
    }
