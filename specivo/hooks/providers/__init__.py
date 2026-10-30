"""Forge provider adapters for incoming repository webhooks.

``get_provider(name)`` returns the adapter for ``github``, ``gitlab``,
``gitea`` or ``forgejo``; see :class:`ForgeProvider` for what each does.
"""

from __future__ import annotations

from specivo.hooks.providers.base import CommitData, ForgeProvider, PushEvent, SignatureError
from specivo.hooks.providers.gitea import ForgejoProvider, GiteaProvider
from specivo.hooks.providers.github import GitHubProvider
from specivo.hooks.providers.gitlab import GitLabProvider

_PROVIDERS: dict[str, ForgeProvider] = {
    p.name: p for p in (GitHubProvider(), GitLabProvider(), GiteaProvider(), ForgejoProvider())
}

#: Provider names, in the order the UI lists them.
PROVIDER_NAMES: tuple[str, ...] = tuple(_PROVIDERS)


def get_provider(name: str) -> ForgeProvider:
    """The adapter for *name*; ``KeyError`` for an unknown provider."""
    return _PROVIDERS[name]


__all__ = [
    "PROVIDER_NAMES",
    "CommitData",
    "ForgeProvider",
    "PushEvent",
    "SignatureError",
    "get_provider",
]
