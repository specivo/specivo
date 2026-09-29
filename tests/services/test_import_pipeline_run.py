"""Service tests for a whole import run.

Exercises the wired pipeline rather than one loader: that the phases are all
registered, that a dry run writes nothing while still doing the work, that a
stopped run resumes, and that references are rewritten once every issue exists.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import (
    EntityType,
    IRIssue,
    IRLookups,
    IRPriority,
    IRProject,
    IRStatus,
    IRTracker,
    IRUser,
    IRWikiPage,
    IRWikiVersion,
)
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportPipeline, ImportSummary
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.load.registry import register_all
from specivo.models.import_id_map import ImportIdMapping
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.wiki import WikiPage
from tests.services.conftest import FakeAdapter

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


class RunAdapter(FakeAdapter):
    """Adapter with its own source system, so parallel modules do not contend."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("source_system", "redminerun")
        super().__init__(**kwargs)


def _adapter() -> RunAdapter:
    return RunAdapter(
        lookups=IRLookups(
            statuses=[IRStatus(source_ref="1", name="New", category="backlog")],
            trackers=[IRTracker(source_ref="1", name="Bug", default_status_ref="1")],
            priorities=[IRPriority(source_ref="4", name="Normal", is_default=True)],
        ),
        users=[IRUser(source_ref="7", login="run_alex", display_name="Alex", email="run_alex@example.org")],
        projects=[IRProject(source_ref="1", identifier="runtest", name="Run Test")],
        issues=[
            IRIssue(
                source_ref="100",
                project_ref="1",
                tracker_ref="1",
                status_ref="1",
                priority_ref="4",
                subject="First",
                author_ref="7",
                description="Caused by #101.",
            ),
            IRIssue(
                source_ref="101",
                project_ref="1",
                tracker_ref="1",
                status_ref="1",
                priority_ref="4",
                subject="Second",
                author_ref="7",
            ),
        ],
        wiki_pages=[
            IRWikiPage(
                source_ref="10",
                project_ref="1",
                title="Home",
                versions=[IRWikiVersion(source_ref="200", version=1, text="Body", author_ref="7")],
            )
        ],
    )


class SingleSessionFactory:
    """Hands the pipeline the test's own rollback-isolated session.

    The pipeline would otherwise open its own connections, which would sit
    outside the transaction the test fixture rolls back.
    """

    def __init__(self, session) -> None:
        self._session = session

    def __call__(self):
        return _NoCloseSession(self._session)


class _NoCloseSession:
    """Wraps the shared session so the pipeline's context manager cannot close it."""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info) -> None:
        return None


@pytest_asyncio.fixture
def run_pipeline(db_session):
    """Return a factory that builds a fully wired pipeline on the test session."""

    def _build(adapter: FakeAdapter, **option_kwargs) -> ImportPipeline:
        options = ImportOptions(source_instance=adapter.source_instance, **option_kwargs)
        return register_all(
            ImportPipeline(
                adapter=adapter,
                session_factory=SingleSessionFactory(db_session),
                options=options,
                reporter=NullProgressReporter(),
            )
        )

    return _build


class TestWiring:
    async def test_every_phase_has_a_handler(self):
        """A phase with no handler is skipped silently, so this is worth pinning."""
        pipeline = register_all(
            ImportPipeline(
                adapter=_adapter(),
                session_factory=SingleSessionFactory(None),
                options=ImportOptions(source_instance="x"),
            )
        )
        assert set(pipeline.registered_phases()) == set(ImportPhase)


class TestFullRun:
    async def test_a_run_imports_everything(self, db_session, run_pipeline):
        summary = await run_pipeline(_adapter()).run()

        assert summary.created[EntityType.PROJECT] == 1
        assert summary.created[EntityType.ISSUE] == 2
        assert summary.created[EntityType.WIKI_PAGE] == 1
        assert summary.created[EntityType.USER] == 1

    async def test_references_are_rewritten_after_every_issue_exists(self, db_session, run_pipeline):
        """The reference points at an issue created after the one naming it."""
        await run_pipeline(_adapter()).run()

        description = (await db_session.execute(select(Issue.description).where(Issue.subject == "First"))).scalar_one()
        assert "#101" not in description
        assert "RUNTEST-" in description

    async def test_a_second_run_creates_nothing(self, db_session, run_pipeline):
        await run_pipeline(_adapter()).run()
        summary = await run_pipeline(_adapter()).run()

        assert summary.created[EntityType.ISSUE] == 0
        assert summary.skipped[EntityType.ISSUE] == 2
        count = (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one()
        assert count == 2

    async def test_the_report_is_json_serialisable(self, db_session, run_pipeline):
        import json

        summary = await run_pipeline(_adapter()).run()
        assert json.loads(json.dumps(summary.as_dict()))["source_system"] == "redminerun"


class TestDryRun:
    async def test_nothing_is_written(self, db_session, run_pipeline):
        """The point of a dry run: exercise the real writes, keep none of them."""
        summary = await run_pipeline(_adapter(), dry_run=True).run()

        assert summary.created[EntityType.ISSUE] == 2
        await db_session.rollback()

        for model in (Project, Issue, WikiPage, ImportIdMapping):
            count = (await db_session.execute(select(func.count()).select_from(model))).scalar_one()
            assert count == 0, model.__name__

    async def test_the_report_says_so(self, db_session, run_pipeline):
        summary = await run_pipeline(_adapter(), dry_run=True).run()
        assert summary.dry_run is True
        assert "DRY RUN" in summary.format_text()


class TestResume:
    async def test_a_stopped_run_leaves_earlier_phases_in_place(self, db_session, run_pipeline):
        await run_pipeline(_adapter(), stop_after_phase=ImportPhase.PROJECTS).run()

        projects = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
        issues = (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one()
        assert projects == 1
        assert issues == 0

    async def test_resuming_finishes_the_import(self, db_session, run_pipeline):
        first = await run_pipeline(_adapter(), stop_after_phase=ImportPhase.PROJECTS).run()
        await run_pipeline(_adapter(), resume_run_id=first.run_id).run()

        issues = (await db_session.execute(select(func.count()).select_from(Issue))).scalar_one()
        assert issues == 2

    async def test_resuming_does_not_duplicate_earlier_phases(self, db_session, run_pipeline):
        first = await run_pipeline(_adapter(), stop_after_phase=ImportPhase.PROJECTS).run()
        second = await run_pipeline(_adapter(), resume_run_id=first.run_id).run()

        projects = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
        assert projects == 1
        assert second.skipped[EntityType.PROJECT] == 1

    async def test_resuming_before_projects_still_creates_them(self, db_session, run_pipeline):
        """Regression: the import account is found rather than created on a
        resumed run, and the code that needed it looked for something the
        find-path had never recorded. Stopping after users and resuming is the
        case that exposed it — stopping after projects hides it, because the
        projects are then skipped and never ask for the account."""
        first = await run_pipeline(_adapter(), stop_after_phase=ImportPhase.USERS).run()
        second = await run_pipeline(_adapter(), resume_run_id=first.run_id).run()

        assert second.created[EntityType.PROJECT] == 1
        projects = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
        assert projects == 1

    async def test_the_import_account_is_not_duplicated_across_runs(self, db_session, run_pipeline):
        from specivo.models.user import User

        await run_pipeline(_adapter(), stop_after_phase=ImportPhase.USERS).run()
        await run_pipeline(_adapter()).run()

        count = (
            await db_session.execute(select(func.count()).select_from(User).where(User.is_service_account.is_(True)))
        ).scalar_one()
        assert count == 1

    async def test_a_fresh_run_id_also_recognises_earlier_work(self, db_session, run_pipeline):
        """The identifier map is keyed on the source, not on the run."""
        await run_pipeline(_adapter(), stop_after_phase=ImportPhase.PROJECTS).run()
        summary = await run_pipeline(_adapter()).run()

        assert summary.skipped[EntityType.PROJECT] == 1
        projects = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
        assert projects == 1


class TestScope:
    async def test_only_the_named_project_is_imported(self, db_session, run_pipeline):
        adapter = _adapter()
        adapter.projects.append(IRProject(source_ref="2", identifier="other", name="Other"))
        await run_pipeline(adapter, project_refs=("1",)).run()

        identifiers = (await db_session.execute(select(Project.identifier))).scalars().all()
        assert "runtest" in identifiers
        assert "other" not in identifiers

    async def test_the_id_map_is_scoped_to_the_source(self, db_session, run_pipeline):
        summary = await run_pipeline(_adapter()).run()

        other = ImportIdMap("jira", summary.source_instance, uuid.uuid4())
        assert await other.get(db_session, EntityType.PROJECT, "1") is None


class TestSummaryShape:
    async def test_counts_and_notes_are_present(self, db_session, run_pipeline):
        summary = await run_pipeline(_adapter()).run()
        assert isinstance(summary, ImportSummary)
        assert summary.phases_run
        assert "accounts_that_will_be_asked_to_set_a_password_at_first_sign_in" in summary.notes
        assert "project_keys_assigned" in summary.notes
