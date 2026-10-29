"""What an issue's metadata fields are called, and what their values name.

A metadata schema describes each field as a JSON Schema property. Two
annotations on the property say how the field reads:

- ``title``: the field's name, as people see it (``"QA owner"``,
  ``"ระดับความเสี่ยง"``). Without one the key reads as a name: ``qa_owner`` is
  "Qa Owner", as the issue page has always shown it.
- ``x-specivo-ref``: ``"user"`` or ``"version"`` for a field whose values are
  the ids of people or of versions (an imported user or version picker). Such
  a value is shown by the name of what it names, to a reader who may see it,
  by the rules the issue history follows (:mod:`specivo.services.activity_lookups`);
  otherwise as stored.

JSON Schema ignores annotations it does not know, so neither changes what a
value may be.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.metadata_schema import MetadataSchema

# The annotation marking a field whose values are ids of people or versions.
REF_KEYWORD = "x-specivo-ref"
REF_KINDS: tuple[str, ...] = ("user", "version")

type RefKind = Literal["user", "version"]

_WORD_START = re.compile(r"\b\w")


def humanise(key: str) -> str:
    """A key as the issue page has always labelled it: ``qa_owner`` reads "Qa Owner"."""
    return _WORD_START.sub(lambda match: match.group(0).upper(), key.replace("_", " "))


def field_label(key: str, prop: Mapping[str, Any] | None) -> str:
    """The name of field *key*: its schema's ``title``, else the key humanised."""
    title = prop.get("title") if isinstance(prop, Mapping) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return humanise(key)


def _properties(schemas: Iterable[MetadataSchema]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for schema in schemas:
        definition = schema.schema_definition if isinstance(schema.schema_definition, Mapping) else {}
        properties = definition.get("properties")
        if isinstance(properties, Mapping):
            for key, prop in properties.items():
                if isinstance(prop, Mapping):
                    yield str(key), prop


def field_labels(schemas: Iterable[MetadataSchema]) -> dict[str, str]:
    """Every field the schemas define, by key, with its name. A title given anywhere wins over none."""
    labels: dict[str, str] = {}
    titled: set[str] = set()
    for key, prop in _properties(schemas):
        has_title = isinstance(prop.get("title"), str) and bool(str(prop["title"]).strip())
        if key not in labels or (has_title and key not in titled):
            labels[key] = field_label(key, prop)
        if has_title:
            titled.add(key)
    return labels


def field_refs(schemas: Iterable[MetadataSchema]) -> dict[str, RefKind]:
    """The fields whose values name people or versions, by key."""
    refs: dict[str, RefKind] = {}
    for key, prop in _properties(schemas):
        kind = prop.get(REF_KEYWORD)
        if kind in REF_KINDS:
            refs[key] = kind  # type: ignore[assignment]
    return refs


def referenced_ids(metadata: Mapping[str, Any] | None, refs: Mapping[str, RefKind]) -> dict[RefKind, set[int]]:
    """The ids an issue's metadata holds in its user and version fields, by kind."""
    found: dict[RefKind, set[int]] = {"user": set(), "version": set()}
    for key, kind in refs.items():
        value = (metadata or {}).get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, int) and not isinstance(item, bool):
                found[kind].add(item)
            elif isinstance(item, str) and item.isdigit():
                found[kind].add(int(item))
    return found


async def issue_schemas(session: AsyncSession, project_ids: Iterable[int]) -> list[MetadataSchema]:
    """The issue metadata schemas of the projects, in a stable order."""
    ids = set(project_ids)
    if not ids:
        return []
    stmt = (
        select(MetadataSchema)
        .where(MetadataSchema.project_id.in_(ids), MetadataSchema.content_type == "issue")
        .order_by(MetadataSchema.project_id, MetadataSchema.tracker_id.is_not(None), MetadataSchema.name)
    )
    return list((await session.execute(stmt)).scalars().all())
