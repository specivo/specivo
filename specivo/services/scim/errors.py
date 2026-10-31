"""SCIM errors: the RFC 7644 section 3.12 error response.

Every refusal of the SCIM endpoint is a :class:`ScimError`; the router turns it
into ``{"schemas": [ERROR_SCHEMA], "status": "400", "scimType": ..., "detail": ...}``
with the matching HTTP status. ``scimType`` is set only where the RFC defines
one (``invalidFilter``, ``tooMany``, ``uniqueness``, ``mutability``,
``invalidSyntax``, ``invalidPath``, ``noTarget``, ``invalidValue``,
``invalidVers``, ``sensitive``). ``detail`` is English and never echoes a
secret; SCIM clients log it for the IdP administrator.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ERROR_SCHEMA", "ScimError", "error_body"]

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


class ScimError(Exception):
    """A SCIM refusal with its HTTP status, optional ``scimType`` and ``detail``."""

    def __init__(
        self,
        status: int,
        detail: str,
        scim_type: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.headers = headers or {}

    # Constructors for the common cases, so call sites read as the RFC does.

    @classmethod
    def invalid_filter(cls, detail: str) -> ScimError:
        return cls(400, detail, "invalidFilter")

    @classmethod
    def invalid_syntax(cls, detail: str) -> ScimError:
        return cls(400, detail, "invalidSyntax")

    @classmethod
    def invalid_path(cls, detail: str) -> ScimError:
        return cls(400, detail, "invalidPath")

    @classmethod
    def invalid_value(cls, detail: str) -> ScimError:
        return cls(400, detail, "invalidValue")

    @classmethod
    def no_target(cls, detail: str) -> ScimError:
        return cls(400, detail, "noTarget")

    @classmethod
    def uniqueness(cls, detail: str) -> ScimError:
        return cls(409, detail, "uniqueness")

    @classmethod
    def mutability(cls, detail: str) -> ScimError:
        return cls(400, detail, "mutability")

    @classmethod
    def not_found(cls, detail: str = "Resource not found") -> ScimError:
        return cls(404, detail)

    @classmethod
    def unauthorized(cls) -> ScimError:
        # One answer for a missing, wrong or revoked token, an unknown provider
        # and a provider with SCIM off: the response is no oracle.
        return cls(401, "Authentication failed", headers={"WWW-Authenticate": 'Bearer realm="scim"'})


def error_body(error: ScimError) -> dict[str, Any]:
    """The JSON body of ``error``."""
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(error.status), "detail": error.detail}
    if error.scim_type is not None:
        body["scimType"] = error.scim_type
    return body
