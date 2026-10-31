"""SCIM representations: schema URNs, ``meta``, list responses, and the static discovery documents.

The discovery documents (``ServiceProviderConfig``, ``ResourceTypes``,
``Schemas``) describe exactly what this implementation does: PATCH yes; bulk,
sort, ETags and password change no; filtering yes, restricted to the
attributes :mod:`specivo.services.scim.filters` accepts, with ``maxResults``
from ``SCIM_MAX_RESULTS``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "GROUP_SCHEMA",
    "LIST_SCHEMA",
    "USER_SCHEMA",
    "iso",
    "list_response",
    "meta",
    "resource_types",
    "schemas",
    "service_provider_config",
]

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


def iso(value: datetime | None) -> str | None:
    """RFC 3339 timestamp in UTC with a ``Z``."""
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def meta(resource_type: str, created: datetime | None, modified: datetime | None, location: str) -> dict[str, Any]:
    return {
        "resourceType": resource_type,
        "created": iso(created),
        "lastModified": iso(modified or created),
        "location": location,
    }


def list_response(resources: list[dict[str, Any]], *, total: int, start_index: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def service_provider_config(base_url: str, max_results: int) -> dict[str, Any]:
    return {
        "schemas": [_SPC_SCHEMA],
        "documentationUri": "https://specivo.io/",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": max_results},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "The SCIM token generated on the identity provider's settings page in Specivo.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base_url}/ServiceProviderConfig"},
    }


def resource_types(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [_RESOURCE_TYPE_SCHEMA],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "description": "A Specivo account",
            "schema": USER_SCHEMA,
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
        },
        {
            "schemas": [_RESOURCE_TYPE_SCHEMA],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "description": "A Specivo user group",
            "schema": GROUP_SCHEMA,
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/Group"},
        },
    ]


def _attr(
    name: str,
    type_: str = "string",
    *,
    required: bool = False,
    multi: bool = False,
    mutability: str = "readWrite",
    uniqueness: str = "none",
    case_exact: bool = False,
    description: str = "",
    sub: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attribute: dict[str, Any] = {
        "name": name,
        "type": type_,
        "multiValued": multi,
        "description": description,
        "required": required,
        "caseExact": case_exact,
        "mutability": mutability,
        "returned": "default",
        "uniqueness": uniqueness,
    }
    if sub is not None:
        attribute["subAttributes"] = sub
    return attribute


def schemas(base_url: str) -> list[dict[str, Any]]:
    user = {
        "schemas": [_SCHEMA_SCHEMA],
        "id": USER_SCHEMA,
        "name": "User",
        "description": "A Specivo account. Other User attributes are accepted and ignored.",
        "attributes": [
            _attr("userName", required=True, uniqueness="server", description="Becomes the login, normalised."),
            _attr("name", "complex", sub=[_attr("formatted"), _attr("givenName"), _attr("familyName")]),
            _attr("displayName"),
            _attr(
                "emails",
                "complex",
                multi=True,
                required=True,
                sub=[_attr("value"), _attr("type"), _attr("primary", "boolean")],
            ),
            _attr("active", "boolean"),
            _attr("preferredLanguage"),
            _attr("locale"),
            _attr("timezone"),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{USER_SCHEMA}"},
    }
    group = {
        "schemas": [_SCHEMA_SCHEMA],
        "id": GROUP_SCHEMA,
        "name": "Group",
        "description": "A Specivo user group. Members are users provisioned by the same identity provider.",
        "attributes": [
            _attr("displayName", required=True, uniqueness="server"),
            _attr(
                "members",
                "complex",
                multi=True,
                sub=[
                    _attr("value", mutability="immutable"),
                    _attr("display", mutability="readOnly"),
                    _attr("$ref", "reference", mutability="immutable"),
                    _attr("type", mutability="immutable"),
                ],
            ),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{GROUP_SCHEMA}"},
    }
    return [user, group]
