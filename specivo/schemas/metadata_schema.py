"""Pydantic schemas for MetadataSchema resources."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# A preset slug ("identifier") must be URL-safe: lowercase letters, digits and
# dashes, starting with an alphanumeric. Uniqueness is enforced case-insensitively
# at the service and DB layers, so we normalize to lowercase here.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _normalize_preset_slug(value: str) -> str:
    """Normalize and validate a preset slug, raising ValueError if invalid."""
    normalized = value.strip().lower().replace(" ", "-")
    if not normalized:
        raise ValueError("Identifier is required.")
    if not _SLUG_RE.match(normalized):
        raise ValueError("Use lowercase letters, numbers and dashes only.")
    return normalized


class MetadataSchemaCreate(BaseModel):
    """Payload for creating a new metadata schema."""

    name: str = Field(min_length=1, max_length=255)
    tracker_id: int | None = None
    content_type: str = Field(default="issue", max_length=32)
    description: str | None = None
    schema_definition: dict


class MetadataSchemaUpdate(BaseModel):
    """Payload for partial update of a metadata schema (PATCH semantics)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    tracker_id: int | None = None
    description: str | None = None
    schema_definition: dict | None = None


class MetadataSchemaOut(BaseModel):
    """Metadata schema representation returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    project_id: int
    tracker_id: int | None
    content_type: str = "issue"
    name: str
    description: str | None
    schema_definition: dict
    preset_slug: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Preset schemas
# ---------------------------------------------------------------------------


class MetadataPresetOut(BaseModel):
    """Metadata preset representation returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    slug: str
    name: str
    description: str | None
    icon: str
    schema_definition: dict
    is_builtin: bool
    created_at: datetime


class PresetEnableRequest(BaseModel):
    """Optional body when enabling a preset on a project."""

    tracker_id: int | None = Field(
        default=None,
        description="Scope preset to a specific tracker. NULL = all trackers.",
    )


class MetadataSchemaUsageOut(BaseModel):
    """Usage count for a metadata schema."""

    schema_id: int
    name: str
    usage_count: int


class MetadataPresetCreate(BaseModel):
    """Payload for creating a custom metadata preset."""

    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    icon: str = Field(default="default", max_length=50)
    schema_definition: dict

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        return _normalize_preset_slug(value)


class MetadataPresetUpdate(BaseModel):
    """Payload for partial update of a metadata preset."""

    slug: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=50)
    schema_definition: dict | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        return _normalize_preset_slug(value) if value is not None else None
