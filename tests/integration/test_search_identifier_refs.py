"""Search by issue identifier (``KEY-N``) across keyword, semantic and hybrid modes.

PostgreSQL's default text-search parser reads ``KEY-42`` as the word ``key``
followed by the *signed integer* ``-42``, while the query side used to strip
hyphens first and look for ``42``. The two never met, so an identifier query
returned nothing in every mode.

Two mechanisms fix it, and both are covered here:

- every indexed text also carries each ``key-n`` token as one lowercase
  lexeme, and the query side matches identifier tokens against that lexeme,
  so identifiers *mentioned* in issues, wiki pages and comments are found;
- a query naming an issue's reference — current, from before a move, or
  under a renamed project's old key — resolves it directly and ranks it
  first, through the same visibility SQL as the rest of search.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue, IssueRefAlias
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project, ProjectKeyAlias
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.schemas.search import SearchFilters, SearchResult
from specivo.services.search_reindex_service import reindex_fts
from specivo.services.search_service import SearchService
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MODES = ("keyword", "semantic", "hybrid")

# Project keys are unique to this module (tests run with --dist loadfile).
KEY = "IDRA"
OTHER_KEY = "IDRB"
HIDDEN_KEY = "IDRH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class World:
    member: User
    outsider: User
    tracker: Tracker
    status: IssueStatus
    priority: IssuePriority
    project: Project
    other_project: Project
    hidden_project: Project


async def _run(
    session: AsyncSession,
    mode: str,
    query: str,
    user: User,
    *,
    scope: str = "all",
    offset: int = 0,
    limit: int = 25,
    project_id: int | None = None,
    filters: SearchFilters | None = None,
) -> tuple[list[SearchResult], int]:
    svc = SearchService()
    if mode == "keyword":
        results, total, _ = await svc.search(
            session,
            query,
            user=user,
            scope=scope,
            offset=offset,
            limit=limit,
            project_id=project_id,
            filters=filters,
        )
    elif mode == "semantic":
        results, total = await svc.semantic_search(
            session, query, user=user, offset=offset, limit=limit, project_id=project_id, filters=filters
        )
    else:
        results, total, _ = await svc.hybrid_search(
            session,
            query,
            user=user,
            scope=scope,
            offset=offset,
            limit=limit,
            project_id=project_id,
            filters=filters,
        )
    return results, total


def _issue_titles(results: list[SearchResult]) -> list[str]:
    return [r.title for r in results if r.result_type == "issue"]


async def _grant(db: AsyncSession, project: Project, user: User, *, visibility: str = "default") -> None:
    role = Role(
        name=f"IdRef-{project.key}-{user.id}",
        permissions=["view_issues", "view_wiki"],
        builtin=0,
        issues_visibility=visibility,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def _issue(
    db: AsyncSession,
    w: World,
    project: Project,
    seq: int,
    subject: str,
    description: str | None = None,
    *,
    is_private: bool = False,
    author: User | None = None,
) -> Issue:
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=w.tracker.id,
        status_id=w.status.id,
        priority_id=w.priority.id,
        author_id=(author or w.member).id,
        subject=subject,
        description=description,
        is_private=is_private,
    )
    db.add(issue)
    await db.flush()
    return issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add_all([tracker, priority])

    member = UserFactory.build(login="idref_member", status="active")
    outsider = UserFactory.build(login="idref_outsider", status="active")
    db.add_all([member, outsider])

    project = ProjectFactory.build(key=KEY, identifier="idref-main", is_public=False)
    other_project = ProjectFactory.build(key=OTHER_KEY, identifier="idref-other", is_public=False)
    hidden_project = ProjectFactory.build(key=HIDDEN_KEY, identifier="idref-hidden", is_public=False)
    db.add_all([project, other_project, hidden_project])
    await db.flush()

    await _grant(db, project, member)
    await _grant(db, other_project, member)
    # The member holds nothing on the hidden project; the outsider nothing anywhere.
    await db.commit()
    return World(member, outsider, tracker, status, priority, project, other_project, hidden_project)


# ---------------------------------------------------------------------------
# Index side: identifier lexeme
# ---------------------------------------------------------------------------


class TestIdentifierLexeme:
    async def test_tsvector_holds_identifier_as_one_lexeme(self, db_session: AsyncSession, world: World) -> None:
        issue = await _issue(db_session, world, world.project, 42, "IDRA-42 login fails", "See [DO-8795] too.")
        vector = await db_session.scalar(
            text("SELECT CAST(search_vector AS text) FROM issues WHERE id = :id"), {"id": issue.id}
        )
        assert "'idra-42'" in vector
        # The foreign reference survives even though "do" is an English stopword.
        assert "'do-8795'" in vector

    async def test_identifier_extraction(self) -> None:
        from specivo.services.search_service import extract_identifiers

        assert extract_identifiers("see [DO-8795] and acme-42, x-ray, e-mail, 2026-10-26") == ["DO-8795", "ACME-42"]
        assert extract_identifiers("ACME-42 acme-42") == ["ACME-42"]
        assert extract_identifiers("jwt-rotation v1.2-rc3") == []

    async def test_search_chunks_carry_identifier_lexeme(self, db_session: AsyncSession, world: World) -> None:
        source = SearchSource(source_type="journal", entity_id=1, project_id=world.project.id)
        db_session.add(source)
        await db_session.flush()
        chunk = SearchChunk(source_id=source.id, chunk_index=0, content="Duplicate of IDRA-42, closing.")
        db_session.add(chunk)
        await db_session.flush()
        matched = await db_session.scalar(
            text("SELECT search_vector @@ CAST('''idra-42''' AS tsquery) FROM search_chunks WHERE id = :id"),
            {"id": chunk.id},
        )
        assert matched is True

    async def test_reindex_backfills_identifier_lexeme(self, db_session: AsyncSession, world: World) -> None:
        mention = await _issue(db_session, world, world.project, 2, "Follow-up", "Regression from IDRA-42.")
        # Simulate a vector built before identifier lexemes existed: writing the
        # column directly does not fire the trigger (it watches subject/description).
        await db_session.execute(
            text("UPDATE issues SET search_vector = to_tsvector('english', description) WHERE id = :id"),
            {"id": mention.id},
        )
        results, _ = await _run(db_session, "keyword", "IDRA-42", world.member)
        assert f"{KEY}-2" not in _issue_titles(results)

        await reindex_fts(db_session, project_id=world.project.id)

        results, _ = await _run(db_session, "keyword", "IDRA-42", world.member)
        assert f"{KEY}-2" in _issue_titles(results)


# ---------------------------------------------------------------------------
# Query side, all three modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
class TestIdentifierQueries:
    async def test_exact_identifier_is_ranked_first(self, db_session: AsyncSession, world: World, mode: str) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        await _issue(db_session, world, world.project, 7, "Unrelated issue about login")
        results, total = await _run(db_session, mode, "IDRA-42", world.member)
        assert results, "identifier query returned nothing"
        assert results[0].result_type == "issue"
        assert results[0].title == f"{KEY}-42"
        assert total >= 1

    async def test_identifier_is_case_insensitive(self, db_session: AsyncSession, world: World, mode: str) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        results, _ = await _run(db_session, mode, "idra-42", world.member)
        assert results and results[0].title == f"{KEY}-42"

    async def test_identifier_mentioned_in_issue_and_wiki(
        self, db_session: AsyncSession, world: World, mode: str
    ) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        await _issue(db_session, world, world.other_project, 3, "Customer report", "Probably caused by IDRA-42.")
        await WikiService().create_page(
            db_session,
            world.project.id,
            "Release notes",
            "Fixed the login regression (IDRA-42) in this release.",
            world.member,
            skip_search_index=True,
            skip_link_rebuild=True,
        )
        await db_session.flush()

        results, _ = await _run(db_session, mode, "IDRA-42", world.member)
        titles = [r.title for r in results]
        assert titles[0] == f"{KEY}-42"
        if mode == "semantic":
            # The semantic leg has no lexemes; only the exact hit is guaranteed.
            return
        assert f"{OTHER_KEY}-3" in titles
        assert "Release notes" in [r.title for r in results if r.result_type == "wiki"]

    async def test_mixed_query(self, db_session: AsyncSession, world: World, mode: str) -> None:
        await _issue(db_session, world, world.project, 42, "Password reset broken")
        await _issue(db_session, world, world.project, 5, "Login timeout", "Same root cause as IDRA-42.")
        await _issue(db_session, world, world.project, 6, "Login page styling")
        await _issue(db_session, world, world.project, 8, "Export fails", "Mentions IDRA-42 but not the other word.")

        results, _ = await _run(db_session, mode, "IDRA-42 login", world.member)
        titles = _issue_titles(results)
        assert titles[0] == f"{KEY}-42"
        if mode == "semantic":
            return
        # Both terms must match for the full-text leg: the identifier lexeme and "login".
        assert f"{KEY}-5" in titles
        if mode == "keyword":
            assert f"{KEY}-6" not in titles
            assert f"{KEY}-8" not in titles

    async def test_moved_issue_old_ref(self, db_session: AsyncSession, world: World, mode: str) -> None:
        moved = await _issue(db_session, world, world.other_project, 11, "Moved over from the main project")
        db_session.add(IssueRefAlias(old_project_key=KEY, old_sequence_number=90, issue_id=moved.id))
        await db_session.flush()

        results, _ = await _run(db_session, mode, "IDRA-90", world.member)
        assert results and results[0].title == f"{OTHER_KEY}-11"

    async def test_renamed_project_old_key(self, db_session: AsyncSession, world: World, mode: str) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        db_session.add(ProjectKeyAlias(old_key="IDROLD", project_id=world.project.id, renamed_by_id=world.member.id))
        await db_session.flush()

        results, _ = await _run(db_session, mode, "IDROLD-42", world.member)
        assert results and results[0].title == f"{KEY}-42"

    async def test_hidden_issue_returns_nothing(self, db_session: AsyncSession, world: World, mode: str) -> None:
        await _issue(db_session, world, world.hidden_project, 5, "Secret roadmap item IDRH-5")
        moved = await _issue(db_session, world, world.hidden_project, 6, "Secret moved item")
        db_session.add(IssueRefAlias(old_project_key=KEY, old_sequence_number=91, issue_id=moved.id))
        await db_session.flush()

        for query in ("IDRH-5", "IDRA-91", "IDRH-6"):
            for user in (world.member, world.outsider):
                results, total = await _run(db_session, mode, query, user)
                assert results == [], (query, user.login)
                assert total == 0, (query, user.login)

    async def test_private_issue_hidden_from_non_author(
        self, db_session: AsyncSession, world: World, mode: str
    ) -> None:
        author = UserFactory.build(login="idref_private_author", status="active")
        db_session.add(author)
        await db_session.flush()
        await _issue(db_session, world, world.project, 77, "Private item", is_private=True, author=author)

        results, total = await _run(db_session, mode, "IDRA-77", world.member)
        assert _issue_titles(results) == []
        assert total == 0


# ---------------------------------------------------------------------------
# Behaviour that must not change
# ---------------------------------------------------------------------------


class TestUnchangedBehaviour:
    @pytest.mark.parametrize("query", ["e-mail", "x-ray", "e-mail x-ray", "jwt-rotation"])
    async def test_hyphenated_words_still_match(self, db_session: AsyncSession, world: World, query: str) -> None:
        await _issue(db_session, world, world.project, 1, "Fix e-mail x-ray report", "Covers jwt-rotation as well.")
        results, _ = await _run(db_session, "keyword", query, world.member)
        assert _issue_titles(results) == [f"{KEY}-1"]

    async def test_unknown_identifier_returns_nothing(self, db_session: AsyncSession, world: World) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        for mode in MODES:
            results, total = await _run(db_session, mode, "IDRA-4242", world.member)
            assert results == [] and total == 0, mode

    async def test_wiki_scope_has_no_pinned_issue(self, db_session: AsyncSession, world: World) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        results, _ = await _run(db_session, "keyword", "IDRA-42", world.member, scope="wiki")
        assert _issue_titles(results) == []

    async def test_project_filter_respected(self, db_session: AsyncSession, world: World) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        results, _ = await _run(db_session, "keyword", "IDRA-42", world.member, project_id=world.other_project.id)
        assert _issue_titles(results) == []

    async def test_metadata_filter_respected(self, db_session: AsyncSession, world: World) -> None:
        await _issue(db_session, world, world.project, 42, "Login fails after password reset")
        filters = SearchFilters(status_id=world.status.id + 1000)
        results, _ = await _run(db_session, "keyword", "IDRA-42", world.member, filters=filters)
        assert _issue_titles(results) == []


class TestPagination:
    async def test_pinned_hit_is_not_repeated_on_later_pages(self, db_session: AsyncSession, world: World) -> None:
        await _issue(db_session, world, world.project, 42, "Root cause", "IDRA-42 is the root cause.")
        for seq in range(100, 104):
            await _issue(db_session, world, world.project, seq, f"Mention {seq}", "Linked to IDRA-42.")

        first, total = await _run(db_session, "keyword", "IDRA-42", world.member, limit=2)
        second, _ = await _run(db_session, "keyword", "IDRA-42", world.member, offset=2, limit=2)
        third, _ = await _run(db_session, "keyword", "IDRA-42", world.member, offset=4, limit=2)

        assert total == 5
        assert first[0].title == f"{KEY}-42"
        seen = [r.title for r in first + second + third]
        assert len(seen) == 5
        assert len(set(seen)) == 5

    async def test_keyword_pages_without_identifiers(self, db_session: AsyncSession, world: World) -> None:
        # Each UNION part used to be capped at one page, so page two came back empty.
        for seq in range(200, 205):
            await _issue(db_session, world, world.project, seq, f"Quarterly budget review {seq}")

        seen: list[str] = []
        for offset in (0, 2, 4):
            page, total = await _run(db_session, "keyword", "budget", world.member, offset=offset, limit=2)
            assert total == 5
            seen += [r.title for r in page]
        assert len(seen) == 5
        assert len(set(seen)) == 5


class TestHybridWithEmbeddings:
    async def test_pinned_hit_beats_semantic_neighbours(self, db_session: AsyncSession, world: World) -> None:
        from specivo.services.embedding_service import EmbeddingService

        await db_session.execute(
            update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False)
        )
        model = EmbeddingModel(
            name="idref-mock", provider="mock", model_name="mock-1536", dimensions=1536, is_default=True
        )
        db_session.add(model)
        await db_session.flush()

        target = await _issue(db_session, world, world.project, 42, "Password reset broken")
        decoy = await _issue(db_session, world, world.project, 43, "IDRA-42")
        emb = EmbeddingService()
        # The decoy's text is the query itself, so it is the semantic nearest neighbour.
        await emb.embed_source(db_session, "issue", decoy.id, world.project.id, ["IDRA-42"])
        await emb.embed_source(db_session, "issue", target.id, world.project.id, ["Password reset broken"])
        await db_session.flush()

        for mode in ("semantic", "hybrid"):
            results, _ = await _run(db_session, mode, "IDRA-42", world.member)
            titles = _issue_titles(results)
            assert titles[0] == f"{KEY}-42", mode
            assert titles.count(f"{KEY}-42") == 1, mode
