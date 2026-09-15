"""Pydantic schemas for the instance-wide anonymous access switch (admin-only)."""

from __future__ import annotations

from pydantic import BaseModel


class AnonymousAccessProject(BaseModel):
    """A project that carries anonymous permissions."""

    key: str
    name: str
    anonymous_permissions: list[str]


class AnonymousAccessOut(BaseModel):
    """The switch, and the projects that are readable without an account while it is on."""

    enabled: bool
    projects: list[AnonymousAccessProject]


class AnonymousAccessUpdate(BaseModel):
    """Turn the switch on or off.

    Turning it on must carry ``confirm=true``; without it the request is
    refused with ``confirmation_required`` and the projects that would become
    readable are listed in the error details. Turning it off needs no
    confirmation.
    """

    enabled: bool
    confirm: bool = False
