"""Pull/merge request events: parsing per provider, state mapping, links, and event ordering."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from specivo.hooks.providers import PullRequestEvent, get_provider
from specivo.hooks.pull_requests import is_newer, pull_request_links
from tests.unit.hooks._forge import fixture

pytestmark = pytest.mark.unit

KEYS = frozenset({"ACME"})


def _github(**changes: Any) -> dict[str, Any]:
    payload = copy.deepcopy(fixture("github_pull_request.json"))
    payload["pull_request"].update(changes)
    return payload


def _gitlab(**changes: Any) -> dict[str, Any]:
    payload = copy.deepcopy(fixture("gitlab_merge_request.json"))
    payload["object_attributes"].update(changes)
    return payload


def _gitea(**changes: Any) -> dict[str, Any]:
    payload = copy.deepcopy(fixture("gitea_pull_request.json"))
    payload["pull_request"].update(changes)
    return payload


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_github_parse_pull_request() -> None:
    event = get_provider("github").parse_pull_request(fixture("github_pull_request.json"))
    assert event is not None
    assert event.repo_full_name == "example-org/widgets"
    assert event.number == 7
    assert event.title == "Add the login form"
    assert event.body.startswith("Fixes ACME-3")
    assert event.url == "https://github.example.com/example-org/widgets/pull/7"
    assert event.state == "merged"
    assert event.source_branch == "feat/ACME-3-login-form"
    assert event.target_branch == "main"
    assert event.default_branch == "main"
    assert event.author_login == "alex-example"
    assert event.merged_by_login == "sam-example"
    assert event.merged_at == datetime(2026, 10, 2, 15, 30, tzinfo=UTC)
    assert event.merge_commit_sha == "c48d3ae2e3a7b7f0c3b1b2ad1d9e9b5d0f3a7e21"
    assert event.updated_at == datetime(2026, 10, 2, 15, 30, tzinfo=UTC)
    assert event.action == "closed"


def test_gitlab_parse_merge_request() -> None:
    event = get_provider("gitlab").parse_pull_request(fixture("gitlab_merge_request.json"))
    assert event is not None
    assert event.repo_full_name == "example-group/widgets"
    assert event.number == 12
    assert event.title == "Draft: Add the login form"
    assert event.body == "Closes ACME-3"
    assert event.url == "https://gitlab.example.com/example-group/widgets/-/merge_requests/12"
    assert event.state == "draft"
    assert event.source_branch == "feat/ACME-3-login-form"
    assert event.target_branch == "main"
    assert event.default_branch == "main"
    assert event.author_login == "alex-example"
    assert event.updated_at == datetime(2026, 10, 1, 10, 15, tzinfo=UTC)
    assert event.action == "open"


@pytest.mark.parametrize("provider", ["gitea", "forgejo"])
def test_gitea_family_parse_pull_request(provider: str) -> None:
    event = get_provider(provider).parse_pull_request(fixture(f"{provider}_pull_request.json"))
    assert event is not None
    assert event.repo_full_name == "example-org/widgets"
    assert event.number == 4
    assert event.source_branch == "feat/ACME-3-login-form"
    assert event.target_branch == "main"
    assert event.author_login == "alex-example"
    if provider == "gitea":
        assert (event.state, event.action, event.merged_by_login) == ("open", "opened", None)
    else:
        assert (event.state, event.action, event.merged_by_login) == ("merged", "closed", "sam-example")
        assert event.merge_commit_sha == "c48d3ae2e3a7b7f0c3b1b2ad1d9e9b5d0f3a7e21"


@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea", "forgejo"])
@pytest.mark.parametrize("payload", [{}, {"pull_request": "x"}, {"object_attributes": {"iid": "seven"}}])
def test_payload_without_a_number_gives_none(provider: str, payload: dict[str, Any]) -> None:
    assert get_provider(provider).parse_pull_request(payload) is None


def test_long_fields_are_cut() -> None:
    event = get_provider("github").parse_pull_request(_github(title="t" * 5000, body="b" * 100_000))
    assert event is not None
    assert len(event.title) == 1000
    assert len(event.body) <= 10_000


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"state": "open", "draft": False, "merged": False}, "open"),
        ({"state": "open", "draft": True, "merged": False}, "draft"),
        ({"state": "closed", "draft": False, "merged": False, "merged_at": None}, "closed"),
        ({"state": "closed", "draft": False, "merged": True}, "merged"),
        # The "closed" event of a merged draft is still a merge.
        ({"state": "closed", "draft": True, "merged": True}, "merged"),
        # Some events carry merged_at without the merged flag.
        ({"state": "closed", "merged": None, "merged_at": "2026-10-02T15:30:00Z"}, "merged"),
    ],
)
def test_github_state_mapping(changes: dict[str, Any], state: str) -> None:
    event = get_provider("github").parse_pull_request(_github(**changes))
    assert event is not None and event.state == state


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"state": "opened", "work_in_progress": False, "draft": False}, "open"),
        ({"state": "opened", "work_in_progress": True, "draft": False}, "draft"),
        ({"state": "opened", "work_in_progress": False, "draft": True}, "draft"),
        # Older GitLab sends only work_in_progress.
        ({"state": "opened", "work_in_progress": True, "draft": None}, "draft"),
        ({"state": "merged", "work_in_progress": False, "draft": False}, "merged"),
        ({"state": "closed", "work_in_progress": False, "draft": False}, "closed"),
        ({"state": "locked", "work_in_progress": False, "draft": False}, "open"),
    ],
)
def test_gitlab_state_mapping(changes: dict[str, Any], state: str) -> None:
    event = get_provider("gitlab").parse_pull_request(_gitlab(**changes))
    assert event is not None and event.state == state


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"state": "open", "merged": False, "draft": False}, "open"),
        ({"state": "open", "merged": False, "draft": True}, "draft"),
        ({"state": "closed", "merged": False}, "closed"),
        ({"state": "closed", "merged": True}, "merged"),
    ],
)
@pytest.mark.parametrize("provider", ["gitea", "forgejo"])
def test_gitea_family_state_mapping(provider: str, changes: dict[str, Any], state: str) -> None:
    event = get_provider(provider).parse_pull_request(_gitea(**changes))
    assert event is not None and event.state == state


def test_gitlab_timestamps_in_both_formats() -> None:
    old = get_provider("gitlab").parse_pull_request(_gitlab(updated_at="2026-10-01 10:15:00 UTC"))
    new = get_provider("gitlab").parse_pull_request(_gitlab(updated_at="2026-10-01T10:15:00Z"))
    assert old is not None and new is not None
    assert old.updated_at == new.updated_at == datetime(2026, 10, 1, 10, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# From outside: a fork or an author who is not a collaborator
# ---------------------------------------------------------------------------


def _external(provider: str, payload: dict[str, Any]) -> bool:
    event = get_provider(provider).parse_pull_request(payload)
    assert event is not None
    return event.external


def test_github_same_repository_by_the_owner_is_not_external() -> None:
    assert _external("github", _github()) is False


def test_github_fork_is_external() -> None:
    payload = _github()
    payload["pull_request"]["head"]["repo"] = {"id": 4242, "full_name": "outsider/widgets"}
    assert _external("github", payload) is True


def test_github_fork_whose_repository_was_deleted_is_external() -> None:
    payload = _github()
    payload["pull_request"]["head"]["repo"] = None
    assert _external("github", payload) is True


@pytest.mark.parametrize(
    ("association", "external"),
    [
        ("OWNER", False),
        ("MEMBER", False),
        ("COLLABORATOR", False),
        ("CONTRIBUTOR", True),
        ("FIRST_TIME_CONTRIBUTOR", True),
        ("FIRST_TIMER", True),
        ("NONE", True),
    ],
)
def test_github_author_association(association: str, external: bool) -> None:
    assert _external("github", _github(author_association=association)) is external


# A payload that does not say where the request comes from is treated as
# external (the conservative choice): with the option on it is skipped until
# the merge, which is processed anyway.


@pytest.mark.parametrize("side", ["head", "base"])
def test_github_without_a_repository_is_external(side: str) -> None:
    payload = _github()
    payload["pull_request"][side].pop("repo")
    assert _external("github", payload) is True


def test_github_without_author_association_is_external() -> None:
    payload = _github()
    payload["pull_request"].pop("author_association")
    assert _external("github", payload) is True


@pytest.mark.parametrize("provider", ["gitea", "forgejo"])
@pytest.mark.parametrize("side", ["head", "base"])
def test_gitea_family_without_a_repository_id_is_external(provider: str, side: str) -> None:
    payload = copy.deepcopy(fixture(f"{provider}_pull_request.json"))
    payload["pull_request"][side].pop("repo_id", None)
    payload["pull_request"][side].pop("repo", None)
    assert _external(provider, payload) is True


@pytest.mark.parametrize("provider", ["gitea", "forgejo"])
def test_gitea_family_needs_no_author_association(provider: str) -> None:
    payload = copy.deepcopy(fixture(f"{provider}_pull_request.json"))
    assert "author_association" not in payload["pull_request"]
    assert _external(provider, payload) is False


@pytest.mark.parametrize("field", ["source_project_id", "target_project_id"])
def test_gitlab_without_a_project_id_is_external(field: str) -> None:
    payload = _gitlab()
    payload["object_attributes"].pop(field)
    assert _external("gitlab", payload) is True


@pytest.mark.parametrize("provider", ["github", "gitea", "forgejo"])
@pytest.mark.parametrize("head", ["not-an-object", None, 12])
def test_malformed_head_is_external_and_does_not_crash(provider: str, head: Any) -> None:
    name = "github_pull_request.json" if provider == "github" else f"{provider}_pull_request.json"
    payload = copy.deepcopy(fixture(name))
    payload["pull_request"]["head"] = head
    assert _external(provider, payload) is True


def test_github_repository_ids_beyond_32_bits_compare() -> None:
    """GitHub repository ids grow past 2**31; the comparison must not lose them."""
    big = 2**31 + 5
    same = _github()
    for side in ("head", "base"):
        same["pull_request"][side]["repo"]["id"] = big
    assert _external("github", same) is False
    fork = copy.deepcopy(same)
    fork["pull_request"]["head"]["repo"]["id"] = big + 1
    assert _external("github", fork) is True


def test_gitlab_project_ids_beyond_32_bits_compare() -> None:
    big = 2**31 + 5
    assert _external("gitlab", _gitlab(source_project_id=big, target_project_id=big)) is False
    assert _external("gitlab", _gitlab(source_project_id=big + 1, target_project_id=big)) is True


@pytest.mark.parametrize(("source", "external"), [(1, False), (4242, True)])
def test_gitlab_source_project(source: int, external: bool) -> None:
    assert _external("gitlab", _gitlab(source_project_id=source)) is external


@pytest.mark.parametrize("provider", ["gitea", "forgejo"])
@pytest.mark.parametrize(("head_repo_id", "external"), [(1, False), (4242, True)])
def test_gitea_family_head_repository(provider: str, head_repo_id: int, external: bool) -> None:
    payload = copy.deepcopy(fixture(f"{provider}_pull_request.json"))
    payload["pull_request"]["head"]["repo_id"] = head_repo_id
    assert _external(provider, payload) is external


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def _event(title: str = "", body: str = "", branch: str | None = None) -> PullRequestEvent:
    return PullRequestEvent(
        repo_full_name="example-org/widgets",
        number=1,
        title=title,
        body=body,
        url="",
        state="open",
        source_branch=branch,
        target_branch="main",
        default_branch="main",
        author_login=None,
        merged_by_login=None,
        merged_at=None,
        merge_commit_sha=None,
        updated_at=None,
        action="opened",
    )


def _links(event: PullRequestEvent) -> dict[str, tuple[str, bool]]:
    return {key: (link.source, link.closing) for key, link in pull_request_links(event, KEYS).items()}


def test_closing_detected_from_body() -> None:
    assert _links(_event(title="Login form", body="Fixes ACME-1")) == {"ACME-1": ("body", True)}


def test_reference_in_title_is_not_closing() -> None:
    assert _links(_event(title="ACME-1: login form")) == {"ACME-1": ("title", False)}


def test_fixing_keyword_in_title_is_closing() -> None:
    assert _links(_event(title="Fix ACME-1 login form")) == {"ACME-1": ("title", True)}


def test_branch_link() -> None:
    assert _links(_event(branch="feat/acme-5-slug")) == {"ACME-5": ("branch", False)}


def test_title_wins_over_body_and_branch_and_closing_is_kept() -> None:
    links = _links(_event(title="ACME-1 login", body="Closes ACME-1, refs ACME-2", branch="feat/ACME-1-x"))
    assert links == {"ACME-1": ("title", True), "ACME-2": ("body", False)}


def test_out_of_scope_references_are_ignored() -> None:
    assert _links(_event(title="OTHER-1", body="Fixes OTHER-2", branch="OTHER-3-x")) == {}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stored_at", "stored_state", "event_at", "event_state", "expected"),
    [
        (None, None, T0, "open", True),  # nothing stored yet
        (T0, "open", T0 + timedelta(seconds=1), "merged", True),
        (T0 + timedelta(seconds=1), "merged", T0, "open", False),  # older edited after merge
        (T0, "open", T0 - timedelta(hours=1), "closed", False),
        (T0, "open", T0, "merged", True),  # same second: a merge is never lost
        (T0, "merged", T0, "open", False),  # same second: a merge is never undone
        (T0, "open", T0, "open", True),  # same second, same state: title edits apply
        (T0, "open", None, "closed", False),  # no time on the event: cannot tell, keep
        (None, "open", None, "closed", True),  # neither has a time: the latest delivery wins
        (None, "merged", None, "open", False),  # but a merge is final
    ],
)
def test_is_newer(
    stored_at: datetime | None, stored_state: str | None, event_at: datetime | None, event_state: str, expected: bool
) -> None:
    assert is_newer(stored_at, stored_state, event_at, event_state) is expected


def test_links_are_capped_like_a_commit() -> None:
    from specivo.core.constants import REPOSITORY_MAX_REFS_PER_COMMIT

    keys = frozenset({"ACME"})
    body = " ".join(f"ACME-{n}" for n in range(1, 200))
    links = pull_request_links(_event(title="ACME-500", body=body, branch="feat/ACME-600-x"), keys)
    assert len(links) == REPOSITORY_MAX_REFS_PER_COMMIT
    # The title comes first, so a long body never pushes it out.
    assert "ACME-500" in links


@pytest.mark.parametrize("provider", ["github", "gitlab", "gitea", "forgejo"])
def test_number_beyond_the_column_gives_none(provider: str) -> None:
    payload = _gitlab(iid=2**31) if provider == "gitlab" else _github(number=2**31)
    if provider != "gitlab":
        payload["number"] = 2**31
    assert get_provider(provider).parse_pull_request(payload) is None


@pytest.mark.parametrize(
    ("payload", "provider", "merge"),
    [
        (lambda: _github(state="closed", merged=True), "github", True),
        (lambda: {**_github(state="closed", merged=True), "action": "edited"}, "github", False),
        (lambda: _github(state="closed", merged=False, merged_at=None), "github", False),
        (lambda: _gitlab(state="merged", action="merge"), "gitlab", True),
        (lambda: _gitlab(state="merged", action="update"), "gitlab", False),
        (lambda: {**_gitea(state="closed", merged=True), "action": "closed"}, "gitea", True),
        (lambda: {**_gitea(state="closed", merged=True), "action": "edited"}, "forgejo", False),
    ],
)
def test_is_merge_event(payload, provider: str, merge: bool) -> None:
    from specivo.hooks.pull_requests import is_merge_event

    event = get_provider(provider).parse_pull_request(payload())
    assert event is not None
    assert is_merge_event(event) is merge
