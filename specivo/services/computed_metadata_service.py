"""Computed (project-derived) issue metadata.

Some metadata fields are a strict function of the issue's project — e.g. an
"area"/cabinet field where one project maps to exactly one value. Such fields
are configured once per project in ``project.settings["computed_metadata"]``
(a ``{key: value}`` map) and are **never stored** on the issue:

- on **write** they are stripped, so they cannot drift and a client cannot
  override them;
- on **read** they are overlaid onto the issue's stored metadata.

Because the value is derived, not stored, it is always correct and recomputes
automatically when an issue moves between projects.

The presence of a key in a project's ``computed_metadata`` map is what makes it
a computed key — there is no separate registration step and no feature flag.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

COMPUTED_METADATA_SETTINGS_KEY = "computed_metadata"


def computed_values(project_settings: dict | None) -> dict[str, Any]:
    """Return the project's computed metadata map (key -> value).

    Tolerant of missing / malformed config: returns ``{}`` when there is no
    ``computed_metadata`` entry or it is not a dict.
    """
    if not project_settings:
        return {}
    values = project_settings.get(COMPUTED_METADATA_SETTINGS_KEY)
    if not isinstance(values, dict):
        return {}
    return dict(values)


def merge_computed(stored: dict | None, project_settings: dict | None) -> dict:
    """Read view: stored issue metadata overlaid with computed values.

    Computed values always win over any (stale) stored value for the same key.
    Returns a new dict — never the input alias.
    """
    return {**(stored or {}), **computed_values(project_settings)}


def strip_computed(metadata: dict | None, project_settings: dict | None) -> dict:
    """Return *metadata* without any computed keys, so they are never persisted."""
    computed = computed_values(project_settings)
    if not computed:
        return dict(metadata or {})
    return {k: v for k, v in (metadata or {}).items() if k not in computed}


async def load_project_settings(session: AsyncSession, project_id: int) -> dict:
    """Load a project's ``settings`` blob by id (for paths that hold only the id)."""
    from specivo.models.project import Project

    result = await session.execute(select(Project.settings).where(Project.id == project_id))
    return result.scalar_one_or_none() or {}
