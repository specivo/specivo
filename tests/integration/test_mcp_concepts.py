"""Concepts over MCP: ``specivo_get_links`` from a concept ref, ``types=["concept"]``, suggestions, and the guide."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.tools import _get_links
from specivo.services.concept_extraction import extract_project
from tests.integration.concept_helpers import FakeModel, _no_queued_extraction, concept, enable
from tests.integration.link_graph_helpers import World, build_world, issue

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]

assert _no_queued_extraction  # the autouse fixture, imported to be active here


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "CXM")


@pytest.fixture
async def extracted(db_session: AsyncSession, world: World):
    await enable(db_session, world.home, world.admin)
    public = await issue(db_session, world, world.home, "Payments", "The Ledger, perhaps the Scheduler.")
    await issue(db_session, world, world.home, "Secret", "Project Falcon uses the Ledger.", private=True)
    await extract_project(
        db_session,
        world.home.id,
        client=FakeModel(
            answers={
                "Payments": [
                    concept("Ledger", "The Ledger", "feature", 0.9),
                    concept("Scheduler", "the Scheduler", "term", 0.3),
                ],
                "Secret": [concept("Project Falcon", "Project Falcon"), concept("Ledger", "the Ledger")],
            }
        ),
    )
    return public


async def test_get_links_from_a_concept_lists_only_visible_mentions(db_session, world, extracted):
    out = json.loads(await _get_links(db_session, world.reader, f"concept:{world.home.key}/Ledger"))
    assert out["root"]["type"] == "concept" and out["root"]["title"] == "Ledger"
    assert [n["ref"] for n in out["nodes"]] == [extracted.display_key]
    assert "Falcon" not in json.dumps(out) and "Secret" not in json.dumps(out)

    with pytest.raises(NotFoundError):
        await _get_links(db_session, world.reader, f"concept:{world.home.key}/project falcon")


async def test_types_concept_lists_an_items_concepts_and_suggestions_when_asked(db_session, world, extracted):
    plain = json.loads(await _get_links(db_session, world.reader, extracted.display_key, types=["concept"]))
    assert [(e["kind"], e["target"], e["label"]) for e in plain["edges"]] == [
        ("mentions", f"concept:{world.home.key}/ledger", "0.90")
    ]
    ledger = next(n for n in plain["nodes"] if n["type"] == "concept")
    assert ledger["degree"] == 1  # the private issue is not counted for this reader

    asked = await _get_links(
        db_session, world.reader, extracted.display_key, types=["mentions"], include_suggestions=True, format="compact"
    )
    assert f'-mentions:0.30-> concept:{world.home.key}/scheduler "Scheduler" [term] (degree 0) (suggested)' in asked
    assert f"-mentions:0.90-> concept:{world.home.key}/ledger" in asked

    default = json.loads(await _get_links(db_session, world.reader, extracted.display_key))
    assert {e["kind"] for e in default["edges"]} == {"mentions"}
    assert all(not e.get("suggested") for e in default["edges"])


async def test_the_setup_guide_explains_concepts() -> None:
    guide = generate_setup_guide()
    assert "concept:ACME/<name>" in guide and "`mentions`" in guide
