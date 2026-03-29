"""Pydantic schemas for issue relations."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

# All 9 user-facing relation type labels
VALID_RELATION_TYPES = frozenset(
    {
        "relates",
        "duplicates",
        "duplicated",
        "blocks",
        "blocked",
        "precedes",
        "follows",
        "copied_to",
        "copied_from",
    }
)


class RelationCreate(BaseModel):
    """Payload for creating a new issue relation.

    ``issue_to_key`` is the display key of the target issue (e.g. ``ACME-15``).
    ``relation_type`` may be any of the 9 relation types; reverse types are
    normalised to their canonical form before storage.
    ``delay`` is optional and only meaningful for ``precedes`` / ``follows``.
    """

    issue_to_key: str
    relation_type: str
    delay: int | None = None

    @field_validator("relation_type")
    @classmethod
    def validate_relation_type(cls, v: str) -> str:
        if v not in VALID_RELATION_TYPES:
            raise ValueError(f"Invalid relation_type {v!r}. Must be one of: {', '.join(sorted(VALID_RELATION_TYPES))}")
        return v


class RelationOut(BaseModel):
    """Response schema for a single issue relation.

    ``relation_type`` is always shown relative to the issue that was queried:
    if the queried issue is ``issue_from``, the stored type is returned as-is;
    if it is ``issue_to``, the symmetric (reverse) label is returned instead.
    """

    id: int
    issue_from_key: str
    issue_to_key: str
    relation_type: str
    delay: int | None

    model_config = {"from_attributes": True}
