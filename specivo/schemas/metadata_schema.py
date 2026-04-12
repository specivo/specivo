"""Pydantic schemas for MetadataSchema resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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


class MetadataPresetUpdate(BaseModel):
    """Payload for partial update of a metadata preset."""

    slug: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=50)
    schema_definition: dict | None = None
