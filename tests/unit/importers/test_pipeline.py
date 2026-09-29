"""Unit tests for the import pipeline: ordering, transactions, and scope."""

from __future__ import annotations

import uuid

import pytest

from specivo.importers.core.ir import IRLookups
from specivo.importers.core.pipeline import (
    PHASE_ORDER,
    ImportOptions,
    ImportPhase,
    ImportPipeline,
    ImportSummary,
    PhaseFailedError,
)

pytestmark = pytest.mark.unit

# Applied per class: several test classes here are synchronous.
async_test = pytest.mark.asyncio(loop_scope="function")


class FakeSession:
    """Records commit/rollback so tests can assert the transaction shape."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.closed = True


class FakeSessionFactory:
    """Hands out a new FakeSession per call and keeps them for inspection."""

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeAdapter:
    source_system = "fake"

    def __init__(self) -> None:
        self.source_instance = "fake-instance"
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def extract_lookups(self) -> IRLookups:
        return IRLookups()


def _pipeline(factory: FakeSessionFactory, adapter: FakeAdapter, **option_kwargs) -> ImportPipeline:
    options = ImportOptions(source_instance="fake-instance", **option_kwargs)
    return ImportPipeline(adapter=adapter, session_factory=factory, options=options)


class TestPhaseOrder:
    def test_order_is_declaration_order(self):
        assert PHASE_ORDER[0] is ImportPhase.BOOTSTRAP
        assert PHASE_ORDER[-1] is ImportPhase.SEARCH_BACKFILL

    def test_dependencies_precede_dependents(self):
        """Every phase must run after the entities it references are mapped."""
        index = {phase: position for position, phase in enumerate(PHASE_ORDER)}
        assert index[ImportPhase.USERS] < index[ImportPhase.PROJECTS]
        assert index[ImportPhase.PROJECTS] < index[ImportPhase.MEMBERSHIPS]
        assert index[ImportPhase.PROJECT_LOOKUPS] < index[ImportPhase.ISSUES]
        assert index[ImportPhase.ISSUES] < index[ImportPhase.JOURNALS]
        assert index[ImportPhase.ISSUES] < index[ImportPhase.RELATIONS]
        assert index[ImportPhase.ISSUE_ATTACHMENTS] < index[ImportPhase.ISSUE_REF_REWRITE]
        assert index[ImportPhase.WIKI_PAGES] < index[ImportPhase.WIKI_LINK_GRAPH]
        assert index[ImportPhase.SEARCH_BACKFILL] == len(PHASE_ORDER) - 1

    def test_relations_run_after_every_project_issue_phase(self):
        """Cross-project relations need the full issue map, not one project's."""
        index = {phase: position for position, phase in enumerate(PHASE_ORDER)}
        assert index[ImportPhase.RELATIONS] > index[ImportPhase.ISSUES]
        assert index[ImportPhase.ISSUE_REF_REWRITE] > index[ImportPhase.RELATIONS]


@async_test
class TestRun:
    async def test_runs_registered_phases_in_order(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)
        seen: list[str] = []

        async def record(ctx):
            seen.append(str(ctx.phase))

        pipeline.register(ImportPhase.ISSUES, record)
        pipeline.register(ImportPhase.USERS, record)
        pipeline.register(ImportPhase.PROJECTS, record)

        summary = await pipeline.run()

        assert seen == ["users", "projects", "issues"]
        assert summary.phases_run == ["users", "projects", "issues"]

    async def test_unregistered_phases_are_skipped(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)

        async def noop(ctx):
            return None

        pipeline.register(ImportPhase.USERS, noop)
        summary = await pipeline.run()

        assert summary.phases_run == ["users"]
        assert pipeline.registered_phases() == [ImportPhase.USERS]

    async def test_handlers_on_one_phase_run_in_registration_order(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)
        seen: list[str] = []

        async def first(ctx):
            seen.append("first")

        async def second(ctx):
            seen.append("second")

        pipeline.register(ImportPhase.USERS, first)
        pipeline.register(ImportPhase.USERS, second)
        await pipeline.run()

        assert seen == ["first", "second"]

    async def test_adapter_is_connected_and_closed(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)
        await pipeline.run()
        assert adapter.connected is True
        assert adapter.closed is True

    async def test_adapter_is_closed_when_a_phase_raises(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)

        async def boom(ctx):
            raise PhaseFailedError("no")

        pipeline.register(ImportPhase.USERS, boom)
        with pytest.raises(PhaseFailedError):
            await pipeline.run()
        assert adapter.closed is True

    async def test_stop_after_phase_halts_the_run(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, stop_after_phase=ImportPhase.PROJECTS)
        seen: list[str] = []

        async def record(ctx):
            seen.append(str(ctx.phase))

        for phase in (ImportPhase.USERS, ImportPhase.PROJECTS, ImportPhase.ISSUES):
            pipeline.register(phase, record)

        await pipeline.run()
        assert seen == ["users", "projects"]

    async def test_resume_reuses_the_given_run_id(self):
        run_id = uuid.uuid4()
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, resume_run_id=run_id)
        summary = await pipeline.run()
        assert summary.run_id == run_id

    async def test_fresh_run_generates_a_run_id(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        summary = await _pipeline(factory, adapter).run()
        assert isinstance(summary.run_id, uuid.UUID)

    async def test_project_scope_flows_from_one_phase_to_the_next(self):
        """The projects phase discovers the scope later phases iterate."""
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)
        observed: list[str] = []

        async def discover(ctx):
            ctx.project_refs.extend(["1", "2"])

        async def consume(ctx):
            observed.extend(ctx.project_refs)

        pipeline.register(ImportPhase.PROJECTS, discover)
        pipeline.register(ImportPhase.ISSUES, consume)
        await pipeline.run()

        assert observed == ["1", "2"]

    async def test_initial_scope_comes_from_options(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, project_refs=("7",))
        observed: list[str] = []

        async def consume(ctx):
            observed.extend(ctx.project_refs)

        pipeline.register(ImportPhase.PROJECTS, consume)
        await pipeline.run()
        assert observed == ["7"]


@async_test
class TestTransactions:
    async def test_each_phase_gets_its_own_session_and_commits(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)

        async def noop(ctx):
            return None

        pipeline.register(ImportPhase.USERS, noop)
        pipeline.register(ImportPhase.PROJECTS, noop)
        await pipeline.run()

        assert len(factory.sessions) == 2
        assert [s.commits for s in factory.sessions] == [1, 1]
        assert [s.rollbacks for s in factory.sessions] == [0, 0]

    async def test_failing_phase_rolls_back_only_its_own_session(self):
        """An earlier phase stays committed so --resume can continue from it."""
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)

        async def noop(ctx):
            return None

        async def boom(ctx):
            raise PhaseFailedError("no")

        pipeline.register(ImportPhase.USERS, noop)
        pipeline.register(ImportPhase.PROJECTS, boom)

        with pytest.raises(PhaseFailedError):
            await pipeline.run()

        assert factory.sessions[0].commits == 1
        assert factory.sessions[1].commits == 0
        assert factory.sessions[1].rollbacks == 1

    async def test_dry_run_shares_one_session_and_never_commits(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, dry_run=True)
        sessions: list[object] = []

        async def record(ctx):
            sessions.append(ctx.session)

        pipeline.register(ImportPhase.USERS, record)
        pipeline.register(ImportPhase.PROJECTS, record)
        summary = await pipeline.run()

        assert len(factory.sessions) == 1
        assert sessions[0] is sessions[1]
        assert factory.sessions[0].commits == 0
        assert factory.sessions[0].rollbacks == 1
        assert summary.dry_run is True

    async def test_dry_run_rolls_back_even_when_a_phase_raises(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, dry_run=True)

        async def boom(ctx):
            raise PhaseFailedError("no")

        pipeline.register(ImportPhase.USERS, boom)
        with pytest.raises(PhaseFailedError):
            await pipeline.run()

        assert factory.sessions[0].rollbacks == 1
        assert factory.sessions[0].commits == 0


@async_test
class TestPhaseContextWarnings:
    async def test_warn_records_on_the_summary(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter)

        async def warn(ctx):
            ctx.warn("Attachment file missing", attachment="8")

        pipeline.register(ImportPhase.ISSUE_ATTACHMENTS, warn)
        summary = await pipeline.run()

        assert len(summary.warnings) == 1
        assert summary.warnings[0].phase == "issue_attachments"
        assert summary.warnings[0].context == {"attachment": "8"}

    async def test_strict_turns_a_warning_into_a_failure(self):
        adapter, factory = FakeAdapter(), FakeSessionFactory()
        pipeline = _pipeline(factory, adapter, strict=True)

        async def warn(ctx):
            ctx.warn("Attachment file missing", attachment="8")

        pipeline.register(ImportPhase.ISSUE_ATTACHMENTS, warn)
        with pytest.raises(PhaseFailedError):
            await pipeline.run()


class TestSummary:
    def _summary(self) -> ImportSummary:
        return ImportSummary(
            run_id=uuid.uuid4(),
            source_system="fake",
            source_instance="fake-instance",
            dry_run=False,
        )

    def test_counts_accumulate(self):
        summary = self._summary()
        summary.record_created("issue", 3)
        summary.record_created("issue")
        summary.record_skipped("issue", 2)
        assert summary.created["issue"] == 4
        assert summary.skipped["issue"] == 2

    def test_notes_group_by_category(self):
        summary = self._summary()
        summary.add_note("accounts_needing_attention", "alex")
        summary.add_note("accounts_needing_attention", "sam")
        assert summary.notes["accounts_needing_attention"] == ["alex", "sam"]

    def test_as_dict_is_json_serialisable(self):
        import json

        summary = self._summary()
        summary.record_created("issue", 2)
        summary.add_warning("issues", "skipped one", {"issue": "7"})
        summary.add_note("accounts_needing_attention", "alex")
        payload = json.dumps(summary.as_dict())
        assert "accounts_needing_attention" in payload
        assert "skipped one" in payload

    def test_text_report_flags_a_dry_run(self):
        summary = ImportSummary(
            run_id=uuid.uuid4(),
            source_system="fake",
            source_instance="fake-instance",
            dry_run=True,
        )
        summary.record_created("issue", 2)
        text = summary.format_text()
        assert "DRY RUN" in text
        assert "nothing was written" in text
        assert "issue" in text

    def test_text_report_lists_warnings_with_context(self):
        summary = self._summary()
        summary.add_warning("issue_attachments", "File missing", {"attachment": "8"})
        text = summary.format_text()
        assert "File missing" in text
        assert "attachment=8" in text
