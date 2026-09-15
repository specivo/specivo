"""Pydantic schemas for the instance-wide anonymous access switch (admin-only)."""

from __future__ import annotations

from pydantic import BaseModel


class AnonymousAccessProject(BaseModel):
    """A project that carries anonymous permissions."""

    key: str
    name: str
    anonymous_permissions: list[str]


class AnonymousAccessOut(BaseModel):
    """The switch, and the projects opted in to anonymous reading."""

    enabled: bool
    projects: list[AnonymousAccessProject]


class AnonymousAccessUpdate(BaseModel):
    """Turn the switch on or off.

    Turning it on must carry ``confirmed_projects``: the keys from
    ``details.projects`` of the 409 the first attempt returned. Omitted, the
    request is refused with ``confirmation_required``; if the opted-in projects
    no longer match as a set, with ``confirmation_stale``. An empty list is
    valid when nothing is opted in. Turning the switch off ignores the field.
    """

    enabled: bool
    confirmed_projects: list[str] | None = None
