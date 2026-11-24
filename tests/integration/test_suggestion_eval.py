"""The recall evaluation of suggested links (``specivo.cli.eval_suggestions``) on fixture data.

Rankings are computed from the chunk embeddings on the fly; nothing is
written, so the whole evaluation runs inside a read-only transaction.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli import eval_suggestions
from specivo.models.link_suggestion import EntitySuggestion, EntityVector
from specivo.services import suggestion_eval
from specivo.services.suggestion_eval import Case, evaluate
from tests.integration.link_graph_helpers import World, build_world, issue, page, rebuild, wiki_ref
from tests.integration.link_suggestion_helpers import _no_queued_tasks, default_model, embed, unit

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]  # 0.95: the page only the embeddings find
FAR = [0.0, 0.0, 0.0, 1.0]
FILLER = unit(0.3, 0.0, 0.954, 0.0)  # 0.30 to A


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGE")


@pytest.fixture
async def labelled(db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch):
    """One issue, the page close to it in meaning, the page sharing its words, and six unrelated pages.

    Search is replaced by a fixed answer (the keyword page and four fillers),
    so the test decides what search found; the embeddings decide the rest.
    """
    model = await default_model(db_session, "sge-mock")
    root = await issue(db_session, world, world.home, "Login timeout on mobile", "See [[Session handling]]")
    meaning = await page(db_session, world, world.home, "Session handling")
    words = await page(db_session, world, world.home, "Login timeout troubleshooting")
    fillers = [await page(db_session, world, world.home, f"Filler {i}") for i in range(6)]
    await rebuild(db_session, root)
    await embed(db_session, model, root, A)
    await embed(db_session, model, meaning, NEAR_A)
    # Mostly about something else, with one chunk exactly on the issue: only the best chunk finds it.
    await embed(db_session, model, words, FAR, FAR, FAR, FAR, FAR, A)
    for filler in fillers:
        await embed(db_session, model, filler, FILLER)

    async def fake_search(session, user, subject, project_id, issue_id, scope, n):
        assert subject == "Login timeout on mobile"
        return [("wiki_page", words.id), *(("wiki_page", f.id) for f in fillers[:4])]

    monkeypatch.setattr(suggestion_eval, "_search_ranked", fake_search)
    return root, meaning, words


async def test_recall_per_method_and_what_only_suggestions_found(db_session, world, labelled):
    root, meaning, words = labelled
    meaning_ref, words_ref = (
        wiki_ref(world.home, "session-handling"),
        wiki_ref(world.home, "login-timeout-troubleshooting"),
    )
    cases = [
        Case(root.display_key, ["session-handling", words_ref, "no-such-page"]),
        Case(f"{world.hidden.key}-1", ["anything"]),
    ]
    report = await evaluate(db_session, world.reader, cases, k=5)

    first, hidden = report.cases
    assert hidden.error is not None and hidden.error.startswith("issue not found")
    assert first.expected == sorted([meaning_ref, words_ref])
    assert first.missing_pages == ["no-such-page"]
    assert first.already_linked == [meaning_ref]
    for scope in ("all", "wiki"):
        assert report.recall(scope, "chunk_mean") == 0.5
        assert report.recall(scope, "chunk_mean_threshold") == 0.5
        assert report.recall(scope, "best_chunk") == 1.0
        assert report.recall(scope, "search") == 0.5
        assert report.found_only_by_suggestions(scope) == [(root.display_key, meaning_ref)]
        assert first.top[scope]["chunk_mean"][0] == meaning_ref
        assert first.top[scope]["chunk_mean_threshold"] == [meaning_ref]
        assert first.top[scope]["best_chunk"][:2] == [words_ref, meaning_ref]
    assert report.model == "sge-mock"
    assert "pages found by suggestions that search missed: 1" in report.as_text()


async def test_rankings_skip_items_the_user_cannot_see(db_session, world, labelled):
    from specivo.models.search import EmbeddingModel

    root, _meaning, _words = labelled
    assert await db_session.scalar(select(func.count()).select_from(EntityVector)) == 0  # none needed
    twin = await issue(db_session, world, world.home, "Private twin", private=True)
    model = (await db_session.execute(select(EmbeddingModel).where(EmbeddingModel.is_default))).scalar_one()
    await embed(db_session, model, twin, A)
    case = [Case(root.display_key, ["session-handling"])]

    admin = await evaluate(db_session, world.admin, case, k=1)
    assert admin.cases[0].top["all"]["chunk_mean"] == [twin.display_key]
    reader = await evaluate(db_session, world.reader, case, k=1)
    assert reader.cases[0].top["all"]["chunk_mean"] == [wiki_ref(world.home, "session-handling")]


async def test_search_ranking_uses_hybrid_search(db_session, world):
    root = await issue(db_session, world, world.home, "Webhook retries")
    target = await page(db_session, world, world.home, "Webhook retries explained", "How retries work.")
    found = await suggestion_eval._search_ranked(
        db_session, world.admin, root.subject, world.home.id, root.id, "all", 10
    )
    assert ("wiki_page", target.id) in found
    assert ("issue", root.id) not in found


async def test_the_command_reads_only(db_session, world, labelled, _test_connection, monkeypatch, tmp_path, capsys):
    root, _meaning, _words = labelled
    from sqlalchemy.ext.asyncio import AsyncSession as Session

    monkeypatch.setattr(
        "specivo.core.database.get_session_factory",
        lambda: lambda: Session(bind=_test_connection, join_transaction_mode="create_savepoint"),
    )
    labels = f"{root.display_key}: session-handling\n"
    code = await eval_suggestions._main(labels, world.admin.login, 5, True)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recall"]["wiki"]["chunk_mean"] == 1.0
    # Read-only: nothing was stored, and the test's own transaction is writable again afterwards.
    assert await db_session.scalar(select(func.count()).select_from(EntityVector)) == 0
    assert await db_session.scalar(select(func.count()).select_from(EntitySuggestion)) == 0

    assert await eval_suggestions._main(labels, "nobody-here", 5, False) == 2


async def test_the_command_checks_its_arguments(tmp_path):
    with pytest.raises(SystemExit) as exc:
        eval_suggestions.main([str(tmp_path / "missing.txt")])
    assert exc.value.code == 2
    labels = tmp_path / "labels.txt"
    labels.write_text("ACME-1: page\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        eval_suggestions.main([str(labels), "--k", "0"])
    assert exc.value.code == 2
