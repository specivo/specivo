"""Read the groups (or roles) claim of an SSO login.

The claim is named by ``claim_config.groups_claim``, a dotted path into the
claims (default ``groups``): ``realm_access.roles`` and
``resource_access.specivo.roles`` for Keycloak roles, ``roles`` for Entra app
roles. A key that itself contains dots (an Auth0-style namespaced claim such
as ``https://example.com/groups``) is matched literally first. The value must
be a list of strings or a single string; anything else is treated as missing
and logged. We use this small resolver rather than JMESPath to avoid a
dependency.

:func:`read_groups` returns one of three results:

* :class:`Known`: the set of group names (case-sensitive, possibly empty);
* :class:`Missing`: the claim is absent or unusable (Google sends none);
* :class:`Overage`: Entra ID sent a group overage marker instead of the
  groups (``_claim_names`` names the claim, or ``hasgroups: true``), so the
  groups are unknown. Reading them from Microsoft Graph is not supported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from specivo.models.sso import SsoProvider
from specivo.services.sso.log import logger, sanitize_idp_text

__all__ = [
    "DEFAULT_GROUPS_CLAIM",
    "GroupsResult",
    "Known",
    "Missing",
    "Overage",
    "groups_claim_path",
    "read_groups",
    "resolve_path",
]

DEFAULT_GROUPS_CLAIM = "groups"

_ABSENT = object()


@dataclass(frozen=True)
class Known:
    """The groups claim was present: these are the user's groups."""

    groups: frozenset[str]


@dataclass(frozen=True)
class Missing:
    """No usable groups claim: treated as an empty set, but never removes memberships."""


@dataclass(frozen=True)
class Overage:
    """Entra ID group overage: the groups are unknown, so nothing is synced."""


GroupsResult = Known | Missing | Overage


def _lookup(claims: Mapping[str, Any], path: str) -> Any:
    """The value at ``path``, or ``_ABSENT``. A literal key wins over a dotted walk."""
    if path in claims:
        return claims[path]
    node: Any = claims
    for segment in path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return _ABSENT
        node = node[segment]
    return node


def resolve_path(claims: Mapping[str, Any], path: str) -> list[str] | None:
    """The list of strings at the dotted ``path`` of ``claims``, or ``None`` when missing.

    A single string becomes a one-item list. ``null``, an absent key, or a
    value of any other type (number, boolean, object, a list holding
    non-strings) is missing; a present value of the wrong type is logged.
    """
    value = _lookup(claims, path)
    if value is _ABSENT or value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    logger.warning(
        "sso.claims.invalid_type path=%s type=%s",
        sanitize_idp_text(path, 128),
        type(value).__name__,
    )
    return None


def groups_claim_path(provider: SsoProvider) -> str:
    """The configured groups claim path of ``provider`` (default ``groups``)."""
    path = (provider.claim_config or {}).get("groups_claim")
    return path.strip() if isinstance(path, str) and path.strip() else DEFAULT_GROUPS_CLAIM


def _is_overage(claims: Mapping[str, Any], path: str) -> bool:
    """Entra's overage markers concern the claim at ``path``.

    ``_claim_names`` maps a claim name to a ``_claim_sources`` entry when the
    value was too large for the token; ``hasgroups`` is the implicit-flow form
    for the ``groups`` claim.
    """
    top = path if path in claims else path.split(".", 1)[0]
    names = claims.get("_claim_names")
    if isinstance(names, Mapping) and top in names:
        return True
    return top == DEFAULT_GROUPS_CLAIM and claims.get("hasgroups") is True


def read_groups(claims: Mapping[str, Any], provider: SsoProvider) -> GroupsResult:
    """The user's groups according to ``provider``'s groups claim."""
    path = groups_claim_path(provider)
    if _is_overage(claims, path):
        return Overage()
    values = resolve_path(claims, path)
    if values is None:
        return Missing()
    return Known(frozenset(values))
