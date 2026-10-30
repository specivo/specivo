"""Pull/merge requests: which issues one references, and whether an event is newer than what is stored.

Pure functions, no database, like :mod:`specivo.hooks.refs`.

A pull request links to an issue from its title, its body (description) or
its source branch, limited to the keys in the integration's scope and, like a
commit, to ``REPOSITORY_MAX_REFS_PER_COMMIT`` issues (title first, then body,
then branch). The link is *closing* when the title or body uses a fixing
keyword (``Fixes KEY-1``): merge automation acts only on closing links. When
an issue is named in more than one place, the source is the first of title,
body, branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from specivo.core.constants import REPOSITORY_MAX_REFS_PER_COMMIT
from specivo.hooks.providers.base import PullRequestEvent
from specivo.hooks.refs import extract_branch_refs, extract_refs

_MERGED = "merged"


@dataclass(frozen=True)
class PullRequestLink:
    """How a pull request references one issue."""

    key: str
    number: int
    source: str  # "title", "body" or "branch"
    closing: bool


def pull_request_links(event: PullRequestEvent, known_keys: frozenset[str]) -> dict[str, PullRequestLink]:
    """display key → link, for the in-scope issues *event* references."""
    links: dict[str, PullRequestLink] = {}
    for source, refs in (
        ("title", extract_refs(event.title, known_keys)),
        ("body", extract_refs(event.body, known_keys)),
        ("branch", extract_branch_refs(event.source_branch or "", known_keys)),
    ):
        for ref in refs:
            closing = ref.keyword == "fixes"
            current = links.get(ref.display_key)
            if current is None:
                links[ref.display_key] = PullRequestLink(ref.key, ref.number, source, closing)
            elif closing and not current.closing:
                links[ref.display_key] = PullRequestLink(ref.key, ref.number, current.source, True)
    return dict(list(links.items())[:REPOSITORY_MAX_REFS_PER_COMMIT])


def is_merge_event(event: PullRequestEvent) -> bool:
    """Whether *event* is the merge itself, not a later event on a merged request.

    GitHub, Gitea and Forgejo send ``closed`` with the request merged; GitLab
    sends ``merge``. An edit, label or other event on an already merged
    request is not a merge.
    """
    return event.state == _MERGED and event.action in ("closed", "merge")


def is_newer(
    stored_updated_at: datetime | None,
    stored_state: str | None,
    event_updated_at: datetime | None,
    event_state: str,
) -> bool:
    """Whether an event should replace the stored pull request.

    Applied when it is newer by the forge's update time. A merge is final: a
    merged pull request never goes back to another state, and an event in the
    same second as the stored one never undoes a merge but may record one.
    An event without a time is applied only when the stored version has none
    either (the latest delivery wins).
    """
    if stored_state is None:
        return True
    if stored_state == _MERGED and event_state != _MERGED:
        return False
    if stored_updated_at is None:
        return True
    if event_updated_at is None:
        return False
    return event_updated_at >= stored_updated_at
