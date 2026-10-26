"""Markdown that Python-Markdown renders in super-linear time cannot stall a page.

Every entry point that renders stored or submitted Markdown (the editor
preview, wiki pages, issue descriptions, comments and description history)
shows pathological text as plain text, in well under a second. The services
that store Markdown reject text over the shared length limit, and the preview
accepts exactly what can be stored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.schemas.issue import IssueUpdate
from specivo.services.issue_service import IssueService
from specivo.services.markdown_budget import MAX_MARKDOWN_CHARS
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.fixtures.markdown_inputs import PATHOLOGICAL, RECURSION, large_page

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_PREVIEW_URL = "/api/v1/markdown/preview/"
_FALLBACK = 'class="sp-markdown-fallback"'
_wiki_svc = WikiService()

# A cross-section of the catalogue for the slower full-page tests.
_PAGE_CASES = [
    "unclosed-link-target",
    "bare-brackets",
    "wiki-link-openers",
    "unclosed-comments",
    "duplicate-setext-headings",
    "nested-list-markers",
    "issue-refs",
]


@dataclass
class World:
    project: Project
    admin: User
    token: str
    tracker_id: int
    priority_id: int
    issue: Issue


@pytest_asyncio.fixture
async def world(admin_client: AsyncClient, db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="MRB New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="MRB Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="MRB Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()
    project = ProjectFactory.build(key="MRB", identifier="markdown-render-budget", issue_sequence=1)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    admin = admin_client.state.user
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=admin.id,
        subject="Render budget",
        description="Short description.",
    )
    db.add(issue)
    await db.commit()
    return World(project, admin, admin_client.state.token, tracker.id, priority.id, issue)


def _text(name: str) -> str:
    """The catalogue input, with issue refs pointing at an issue that exists, so they are linked."""
    return PATHOLOGICAL[name].replace("ACME-1", "MRB-1")


async def _timed_get(client: AsyncClient, path: str, token: str) -> tuple[str, float]:
    started = time.perf_counter()
    resp = await client.get(path, cookies={"access_token": token})
    elapsed = time.perf_counter() - started
    assert resp.status_code == 200, resp.text[:500]
    return resp.text, elapsed


# ---------------------------------------------------------------------------
# Rendering entry points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [*PATHOLOGICAL, "recursion"])
async def test_preview_shows_pathological_text_as_plain_text_quickly(
    admin_client: AsyncClient, world: World, name: str
) -> None:
    text = RECURSION if name == "recursion" else _text(name)
    started = time.perf_counter()
    resp = await admin_client.post(
        _PREVIEW_URL, json={"text": text, "context": "wiki", "project_key": world.project.key}
    )
    elapsed = time.perf_counter() - started
    assert resp.status_code == 200, resp.text[:500]
    assert _FALLBACK in resp.json()["html"]
    assert elapsed < 1.0, f"{name} took {elapsed:.2f}s"


@pytest.mark.parametrize("name", _PAGE_CASES)
async def test_wiki_page_shows_pathological_text_as_plain_text_quickly(
    admin_client: AsyncClient, db_session: AsyncSession, world: World, name: str
) -> None:
    page, _content = await _wiki_svc.create_page(db_session, world.project.id, "Budget", _text(name), world.admin)
    await db_session.commit()

    html, elapsed = await _timed_get(admin_client, f"/projects/{world.project.key}/wiki/{page.slug}/", world.token)
    assert _FALLBACK in html
    assert elapsed < 1.0, f"{name} took {elapsed:.2f}s"


@pytest.mark.parametrize("name", _PAGE_CASES)
async def test_issue_page_shows_pathological_description_and_comment_quickly(
    admin_client: AsyncClient, db_session: AsyncSession, world: World, name: str
) -> None:
    world.issue.description = _text(name)
    comment = Journal(
        issue_id=world.issue.id, project_id=world.project.id, user_id=world.admin.id, sequence=1, notes=_text(name)
    )
    db_session.add(comment)
    await db_session.commit()

    html, elapsed = await _timed_get(admin_client, f"/issue/{world.project.key}-1/", world.token)
    assert html.count(_FALLBACK) >= 2
    assert elapsed < 1.0, f"{name} took {elapsed:.2f}s"


async def test_description_history_shows_pathological_text_quickly(
    admin_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    journal = Journal(issue_id=world.issue.id, project_id=world.project.id, user_id=world.admin.id, sequence=1)
    db_session.add(journal)
    await db_session.flush()
    db_session.add(
        JournalDetail(
            journal_id=journal.id,
            property="attr",
            prop_key="description",
            old_value=PATHOLOGICAL["unclosed-link-target"],
            new_value=PATHOLOGICAL["bare-brackets"],
        )
    )
    await db_session.commit()

    html, elapsed = await _timed_get(admin_client, f"/issue/{world.project.key}-1/versions/{journal.id}/", world.token)
    assert html.count(_FALLBACK) >= 2
    assert elapsed < 1.0


async def test_large_wiki_page_renders_normally(
    admin_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    page, _content = await _wiki_svc.create_page(db_session, world.project.id, "Large", large_page(), world.admin)
    await db_session.commit()

    html, _elapsed = await _timed_get(admin_client, f"/projects/{world.project.key}/wiki/{page.slug}/", world.token)
    assert _FALLBACK not in html
    assert '<a href="https://example.com/p/0"' in html
    assert '<h2 id="section-0">Section 0</h2>' in html


# ---------------------------------------------------------------------------
# Stored-text limits
# ---------------------------------------------------------------------------


def _assert_too_long(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text[:500]
    error = resp.json()["errors"][0]
    assert error["code"] == "validation_error"
    assert error["field"] == field
    assert error["details"] == {"max_length": MAX_MARKDOWN_CHARS}


async def test_issue_description_over_the_limit_is_rejected(admin_client: AsyncClient, world: World) -> None:
    url = f"/api/v1/projects/{world.project.key}/issues/"
    body = {
        "project_key": world.project.key,
        "tracker_id": world.tracker_id,
        "priority_id": world.priority_id,
        "subject": "Long",
    }

    resp = await admin_client.post(url, json={**body, "description": "a" * (MAX_MARKDOWN_CHARS + 1)})
    _assert_too_long(resp, "description")

    resp = await admin_client.post(url, json={**body, "description": "a" * MAX_MARKDOWN_CHARS})
    assert resp.status_code == 201, resp.text[:500]


async def test_issue_update_over_the_limit_is_rejected(
    admin_client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    url = f"/api/v1/issues/{world.project.key}-1/"
    resp = await admin_client.patch(
        url, json={"lock_version": world.issue.lock_version, "description": "a" * (MAX_MARKDOWN_CHARS + 1)}
    )
    _assert_too_long(resp, "description")

    # Notes passed along with an update (the web form's comment box) share the limit.
    with pytest.raises(ValidationError) as exc_info:
        await IssueService().update(
            db_session,
            world.issue,
            IssueUpdate(lock_version=world.issue.lock_version),
            world.admin,
            notes="a" * (MAX_MARKDOWN_CHARS + 1),
        )
    assert exc_info.value.field == "notes"


async def test_comment_over_the_limit_is_rejected(admin_client: AsyncClient, world: World) -> None:
    url = f"/api/v1/issues/{world.project.key}-1/journals/"

    resp = await admin_client.post(url, json={"notes": "a" * (MAX_MARKDOWN_CHARS + 1)})
    _assert_too_long(resp, "notes")

    resp = await admin_client.post(url, json={"notes": "a" * MAX_MARKDOWN_CHARS})
    assert resp.status_code == 201, resp.text[:500]


async def test_wiki_text_over_the_limit_is_rejected(admin_client: AsyncClient, world: World) -> None:
    url = f"/api/v1/projects/{world.project.key}/wiki/"

    resp = await admin_client.post(url, json={"title": "Long", "text": "a" * (MAX_MARKDOWN_CHARS + 1)})
    _assert_too_long(resp, "text")

    resp = await admin_client.post(url, json={"title": "Long", "text": "a" * MAX_MARKDOWN_CHARS})
    assert resp.status_code == 201, resp.text[:500]
    lock_version = resp.json()["lock_version"]

    resp = await admin_client.patch(
        f"{url}long/", json={"text": "a" * (MAX_MARKDOWN_CHARS + 1), "lock_version": lock_version}
    )
    _assert_too_long(resp, "text")


async def test_preview_accepts_what_can_be_stored(auth_client: AsyncClient) -> None:
    # The limit counts characters: Thai text at the limit is three times as many bytes.
    resp = await auth_client.post(_PREVIEW_URL, json={"text": "ก" * MAX_MARKDOWN_CHARS, "context": "wiki"})
    assert resp.status_code == 200, resp.text[:500]

    resp = await auth_client.post(_PREVIEW_URL, json={"text": "ก" * (MAX_MARKDOWN_CHARS + 1), "context": "wiki"})
    assert resp.status_code == 413
    assert resp.json()["errors"][0]["details"] == {"max_length": MAX_MARKDOWN_CHARS}
