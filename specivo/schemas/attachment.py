"""Pydantic schemas for attachments."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from specivo.schemas.attachment_metadata import AttachmentMetadata
from specivo.schemas.common import IdName


class AttachmentUpdateSchema(BaseModel):
    """Payload for updating attachment description and/or metadata."""

    description: str | None = None
    metadata: AttachmentMetadata | None = None


class AttachmentOut(BaseModel):
    """Attachment metadata returned by the API."""

    model_config = {"from_attributes": True}

    id: int
    container_type: str
    container_id: int
    filename: str
    disk_filename: str
    content_type: str | None
    filesize: int
    description: str | None
    content_hash: str | None
    author: IdName
    created_at: datetime
    updated_at: datetime
