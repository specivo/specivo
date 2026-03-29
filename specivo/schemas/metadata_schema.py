"""Pydantic schemas for MetadataSchema resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MetadataSchemaCreate(BaseModel):
    """Payload for creating a new metadata schema."""

    name: str = Field(min_length=1, max_length=255)
    tracker_id: int | None = None
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
    name: str
    description: str | None
    schema_definition: dict
    created_at: datetime
    updated_at: datetime
