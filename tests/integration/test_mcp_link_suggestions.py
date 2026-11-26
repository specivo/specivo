"""Suggested links over MCP: ``Possibly related`` on show_issue / read_wiki, ``include_suggestions`` on
specivo_get_links, and ``specivo_link_suggestion`` (accept, dismiss, permissions, audit)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.mcp import server
from specivo.mcp.annotations import TOOL_ANNOTATIONS
from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.tools import _get_links, _link_suggestion, _read_wiki, _show_issue
from specivo.models.link_suggestion import EntitySuggestion
from specivo.models.search import EmbeddingModel
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.link_suggestion_service import compute_for_project, default_model_id
from tests.factories.user import UserFactory
from tests.integration.link_graph_helpers import World, build_world, issue, page, wiki_ref
from tests.integration.link_suggestion_helpers import (
    _no_queued_tasks,
    add_permission,
    cos,
    default_model,
    embed,
    fill,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]
SCORE = f"{cos(A, NEAR_A):.3f}"


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGM")


async def _suggest(db_session, world, *items_and_vectors) -> None:
    """Embed the items with these vectors (default model) and compute the home project's suggestions."""
    model = await db_session.get(EmbeddingModel, await default_model_id(db_session))
    for item, vector in items_and_vectors:
        await embed(db_session, model, item, vector)
    await compute_for_project(db_session, world.home.id)
    await db_session.commit()


@pytest.fixture(autouse=True)
async def _model(db_session: AsyncSession, world: World):
    model = await default_model(db_session, "sgm-mock")
    await fill(db_session, world, model, world.home)  # enough items for the project to get suggestions
    return model


async def test_show_issue_and_read_wiki_list_possibly_related(db_session, world, monkeypatch):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    hidden_twin = await issue(db_session, world, world.home, "Hidden twin", private=True)
    await _suggest(db_session, world, (root, A), (near, NEAR_A), (hidden_twin, A))

    shown = await _show_issue(db_session, world.reader, root.display_key, metadata_only=True)
    line = next(li for li in shown.splitlines() if li.startswith("Possibly related"))
    assert (
        line
        == f"Possibly related (suggested, not linked): {wiki_ref(world.home, 'near-page')} Near page (score {SCORE})"
    )
    assert "Hidden twin" not in shown

    read = await _read_wiki(db_session, world.reader, world.home.key, "near-page", metadata_only=True)
    assert f"Possibly related (suggested, not linked): {root.display_key} Root" in read

    monkeypatch.setattr("specivo.core.constants.SIMILAR_LINKS_INLINE_LIMIT", 1)
    admin_view = await _show_issue(db_session, world.admin, root.display_key, metadata_only=True)
    line = next(li for li in admin_view.splitlines() if li.startswith("Possibly related"))
    assert line.count("(score ") == 1


async def test_show_issue_has_no_line_without_suggestions(db_session, world):
    root = await issue(db_session, world, world.home, "Lonely")
    shown = await _show_issue(db_session, world.reader, root.display_key, metadata_only=True)
    assert "Possibly related" not in shown


async def test_get_links_returns_suggestions_only_when_asked(db_session, world):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    await _suggest(db_session, world, (root, A), (near, NEAR_A))

    plain = json.loads(await _get_links(db_session, world.reader, root.display_key))
    assert plain["edges"] == []
    for kwargs in ({"include_suggestions": True}, {"types": ["similar"]}):
        data = json.loads(await _get_links(db_session, world.reader, root.display_key, **kwargs))
        assert [(e["kind"], e["suggested"], e["label"]) for e in data["edges"]] == [("similar", True, SCORE)]
    compact = await _get_links(db_session, world.reader, root.display_key, include_suggestions=True, format="compact")
    assert f"{root.display_key} -similar:{SCORE}-> {wiki_ref(world.home, 'near-page')}" in compact
    assert "(suggested)" in compact


async def test_link_suggestion_accepts_and_audits(db_session, world):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    await _suggest(db_session, world, (root, A), (near, NEAR_A))

    out = await _link_suggestion(
        db_session, world.admin, "accept", wiki_ref(world.home, "near-page"), root.display_key, "documents"
    )
    assert out == f"Accepted: {wiki_ref(world.home, 'near-page')} documents {root.display_key} (typed link)"
    row = (await db_session.execute(select(EntitySuggestion))).scalar_one()
    assert row.status == "accepted"
    log = (
        await db_session.execute(
            select(SecurityAuditLog).where(SecurityAuditLog.event_type == "link_suggestion_accepted")
        )
    ).scalar_one()
    assert log.details["tool"] == "link_suggestion"
    assert log.details["link_type"] == "documents"
    assert log.project_id == world.home.id

    data = json.loads(await _get_links(db_session, world.reader, root.display_key))
    assert [(e["kind"], e["label"]) for e in data["edges"]] == [("typed_link", "documents")]


async def test_link_suggestion_dismisses_and_refuses_without_permission(db_session, world):
    root = await issue(db_session, world, world.home, "Root")
    other = await issue(db_session, world, world.home, "Other")
    await _suggest(db_session, world, (root, A), (other, A))

    with pytest.raises(PermissionDeniedError):
        await _link_suggestion(db_session, world.reader, "dismiss", root.display_key, other.display_key)
    stranger = UserFactory.build(login="sgm_stranger", status="active")
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await _link_suggestion(db_session, stranger, "dismiss", root.display_key, other.display_key)
    with pytest.raises(ValidationError):
        await _link_suggestion(db_session, world.admin, "maybe", root.display_key, other.display_key)

    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    out = await _link_suggestion(db_session, world.reader, "dismiss", root.display_key, other.display_key)
    assert "will not be suggested again" in out
    assert (await db_session.execute(select(EntitySuggestion.status))).scalar_one() == "dismissed"
    logged = await db_session.scalar(
        select(SecurityAuditLog.id).where(SecurityAuditLog.event_type == "link_suggestion_dismissed")
    )
    assert logged is not None


async def test_link_suggestion_cannot_reach_an_item_the_caller_cannot_see(db_session, world):
    root = await issue(db_session, world, world.home, "Root")
    private = await issue(db_session, world, world.home, "Private", private=True)
    await _suggest(db_session, world, (root, A), (private, A))
    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    with pytest.raises(NotFoundError):
        await _link_suggestion(db_session, world.reader, "accept", root.display_key, private.display_key)
    assert (await db_session.execute(select(EntitySuggestion.status))).scalar_one() == "suggested"


async def test_the_tool_is_registered_hinted_and_documented():
    assert server.specivo_link_suggestion.__doc__
    hints = TOOL_ANNOTATIONS["specivo_link_suggestion"]
    assert hints.read_only_hint is False and hints.destructive_hint is False
    guide = generate_setup_guide(fmt="generic", mcp_server=server.mcp)
    assert "specivo_link_suggestion" in guide
    assert "Possibly related" in guide
    assert "include_suggestions" in (server.specivo_get_links.__doc__ or "")
