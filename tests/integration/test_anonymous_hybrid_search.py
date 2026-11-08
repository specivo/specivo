"""Hybrid and semantic search for visitors without an account.

Anonymous search runs the same modes as signed-in search, and its semantic
side goes through the same visibility CTE as everything else. These tests pin
what that means in practice: a match found by meaning alone is served from an
opted-in project, and nothing else is, whichever mode is asked for.

Embeddings are deterministic: every text naming a vehicle maps to one unit
vector and everything else to an orthogonal one, so a query for "vehicle"
finds the automobile issue by meaning while sharing no word with it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import SEMANTIC_INDEX_DIMENSIONS
from specivo.models.issue import Issue
from specivo.models.project import EnabledModule, Project
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.embedding_service import EmbeddingService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DIMS = SEMANTIC_INDEX_DIMENSIONS
_VEHICLE_WORDS = ("vehicle", "automobile", "car", "truck")

# The query shares no word with any indexed text, so every hit below is a
# semantic one: keyword search alone finds nothing.
QUERY = "vehicle"


def _unit(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index] = 1.0
    return vector


async def _fake_embedding(self, text_: str, model_: EmbeddingModel, intent: str = "passage") -> list[float]:
    words = text_.lower().split()
    return _unit(0) if any(word.strip(".,") in _VEHICLE_WORDS for word in words) else _unit(1)


@dataclass(frozen=True)
class World:
    admin: User
    member: User
    opted_in: Project
    not_opted_in: Project
    private: Project
    public_issue: Issue
    private_issue: Issue
    not_opted_in_issue: Issue
    private_project_issue: Issue
    unrelated_issue: Issue
    wiki_title: str


async def _project(db: AsyncSession, key: str, *, is_public: bool = True) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    await db.commit()
    return project


async def _issue(db: AsyncSession, project: Project, lookups: tuple, author: User, subject: str, **kw) -> Issue:
    tracker, status, priority = lookups
    seq = (
        await db.execute(
            select(func.coalesce(func.max(Issue.sequence_number), 0)).where(Issue.project_id == project.id)
        )
    ).scalar_one()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq + 1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=None,
        subject=subject,
        description="",
        **kw,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    await EmbeddingService().index_source(db, "issue", issue.id)
    await db.commit()
    return issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(EmbeddingService, "generate_embedding", _fake_embedding)
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", 0.5)
    # Searches here are not what is being metered.
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)

    admin = AdminUserFactory.build(login="anonhyb_admin", status="active")
    member = UserFactory.build(login="anonhyb_member", status="active")
    db_session.add_all([admin, member])
    await db_session.commit()

    status = StatusFactory.build(name="AnonHybNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="AnonHybBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="AnonHybNormal", is_default=True, position=1)
    db_session.add_all([tracker, priority])
    await db_session.execute(update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False))
    db_session.add(
        EmbeddingModel(name="anonhyb-mock", provider="mock", model_name="mock-384", dimensions=DIMS, is_default=True)
    )
    await db_session.commit()
    lookups = (tracker, status, priority)

    opted_in = await _project(db_session, "ANONHYB")
    not_opted_in = await _project(db_session, "ANONHYBPUB")
    private = await _project(db_session, "ANONHYBPRIV", is_public=False)

    public_issue = await _issue(db_session, opted_in, lookups, member, "Automobile inspection is overdue")
    private_issue = await _issue(db_session, opted_in, lookups, member, "Car crash report", is_private=True)
    unrelated_issue = await _issue(db_session, opted_in, lookups, member, "Kitchen tap is leaking")
    not_opted_in_issue = await _issue(db_session, not_opted_in, lookups, member, "Truck fleet renewal")
    private_project_issue = await _issue(db_session, private, lookups, member, "Automobile lease terms")

    comment = await JournalService().add_comment(
        db_session, public_issue, member, "A private remark: the automobile was never insured properly."
    )
    comment.is_private = True
    await db_session.commit()
    await EmbeddingService().index_source(db_session, "journal", comment.id)

    wiki_page, _ = await WikiService().create_page(
        db_session, opted_in.id, "Automobile handbook", "How to look after the automobile fleet.", admin
    )
    await db_session.commit()
    await EmbeddingService().index_source(db_session, "wiki_page", wiki_page.id)
    await db_session.commit()

    await set_anonymous_permissions(db_session, opted_in, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[opted_in.key])
    await db_session.commit()

    return World(
        admin=admin,
        member=member,
        opted_in=opted_in,
        not_opted_in=not_opted_in,
        private=private,
        public_issue=public_issue,
        private_issue=private_issue,
        not_opted_in_issue=not_opted_in_issue,
        private_project_issue=private_project_issue,
        unrelated_issue=unrelated_issue,
        wiki_title="Automobile handbook",
    )


def _ref(issue: Issue) -> str:
    return f"{issue.project_key}-{issue.sequence_number}"


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
async def test_anonymous_search_finds_public_content_by_meaning(client: AsyncClient, world: World, mode: str) -> None:
    resp = await client.get(f"/api/v1/search/?q={QUERY}&mode={mode}")

    assert resp.status_code == 200, resp.text
    titles = {item["title"] for item in resp.json()["items"]}
    assert _ref(world.public_issue) in titles
    assert world.wiki_title in titles


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
async def test_anonymous_semantic_side_never_serves_hidden_content(
    client: AsyncClient, world: World, mode: str
) -> None:
    """The nearest neighbours include all of these; visibility must drop them."""
    resp = await client.get(f"/api/v1/search/?q={QUERY}&mode={mode}&scope=all")

    assert resp.status_code == 200, resp.text
    body = resp.text
    for hidden in (world.private_issue, world.not_opted_in_issue, world.private_project_issue):
        assert _ref(hidden) not in body
        assert hidden.subject not in body
    assert "never insured" not in body
    assert world.not_opted_in.key not in body
    assert world.private.key not in body


async def test_keyword_search_alone_finds_nothing_for_the_query(client: AsyncClient, world: World) -> None:
    """Guards the premise of the tests above: their hits are semantic."""
    resp = await client.get(f"/api/v1/search/?q={QUERY}&mode=keyword")

    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_unrelated_content_stays_below_the_cut_off(client: AsyncClient, world: World) -> None:
    resp = await client.get(f"/api/v1/search/?q={QUERY}&mode=hybrid")

    assert _ref(world.unrelated_issue) not in {item["title"] for item in resp.json()["items"]}


async def test_a_signed_in_member_of_nothing_sees_the_same_public_issue(client: AsyncClient, world: World) -> None:
    """The comparison point: anonymous never sees more than a signed-in non-member."""
    from specivo.services.auth_service import _make_access_token

    headers = {"Authorization": f"Bearer {_make_access_token(world.member, get_settings())}"}
    signed_in = await client.get(f"/api/v1/search/?q={QUERY}&mode=hybrid", headers=headers)
    anonymous = await client.get(f"/api/v1/search/?q={QUERY}&mode=hybrid")

    signed_in_titles = {item["title"] for item in signed_in.json()["items"]}
    anonymous_titles = {item["title"] for item in anonymous.json()["items"]}
    assert anonymous_titles <= signed_in_titles | {world.wiki_title}


@pytest.mark.parametrize("query", ["limit=26", "offset=501"])
async def test_the_anonymous_page_bounds_still_hold_in_hybrid_mode(
    client: AsyncClient, world: World, query: str
) -> None:
    resp = await client.get(f"/api/v1/search/?q={QUERY}&mode=hybrid&{query}")

    assert resp.status_code == 401


async def test_the_search_page_runs_hybrid_for_an_anonymous_visitor(client: AsyncClient, world: World) -> None:
    resp = await client.get(f"/search/?q={QUERY}")

    assert resp.status_code == 200
    assert world.public_issue.subject in resp.text
    for hidden in (world.private_issue, world.not_opted_in_issue, world.private_project_issue):
        assert hidden.subject not in resp.text
    # The visitor may switch between the modes like everyone else.
    assert "mode-toggle-btn" in resp.text


async def test_the_search_page_keyword_mode_is_still_keyword(client: AsyncClient, world: World) -> None:
    resp = await client.get(f"/search/?q={QUERY}&mode=keyword")

    assert resp.status_code == 200
    assert world.public_issue.subject not in resp.text
