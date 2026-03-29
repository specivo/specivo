"""Common Pydantic schemas used across the API."""

from pydantic import BaseModel


class IdName(BaseModel):
    """Simple ID + name pair for lookup values (status, priority, tracker)."""

    id: int
    name: str


class EntityRef(BaseModel):
    """Universal entity reference for API responses."""

    type: str
    key: str
    id: int
    url: str
    title: str | None = None
    project_key: str | None = None
    parent_key: str | None = None


class PaginatedResponse[T](BaseModel):
    """Generic paginated list response.

    Usage::

        class IssueListResponse(PaginatedResponse[IssueOut]):
            pass

    Or directly as ``PaginatedResponse[IssueOut]`` in response_model.
    """

    total_count: int
    offset: int
    limit: int
    items: list[T]


# Error schemas — canonical definitions live here, used by exception handlers.


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    details: dict | None = None


class ErrorResponse(BaseModel):
    errors: list[ErrorDetail]


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    version: str
    tier: str = "core"
