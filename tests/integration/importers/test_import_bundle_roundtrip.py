"""A bundle carries everything a direct import does.

The Redmine fixture is imported twice, each time into its own freshly migrated
database: once directly, once by exporting it to a bundle (Redmine adapter ->
IR -> writer -> zip) and loading the bundle. Both are real imports, committing
phase by phase as the command does, so ``now()`` differs between them and any
row an import touches after creating it shows up as a different ``updated_at``.
The two results are compared row by row, by natural key rather than id: counts
per table, every issue field, history with its authors and dates, attachments
by hash, wiki pages with every revision, and the timestamps of issues, journals
and wiki revisions.

Differences must be none, or explained here:

* **The import account.** Records without a resolvable author belong to an
  account named after the source system, ``redmine-import`` for both (a bundle
  is keyed by the system its manifest names). It is compared as "the import
  account", and its own row is left out.
* **Issue references.** A direct Redmine import rewrites ``#2`` to a display
  key after loading. A bundle uses placeholders instead, so the export writes
  ``{{issue:2}}`` wherever the direct import would rewrite, which is in issue
  descriptions and comments, using the direct import's own matching rule.
* **A known bug of the direct import.** It leaves ``#2`` as written in the
  description an issue's creation history records, where the bundle import
  expands it. Nothing is normalised for it: the comparison reports the
  difference, and the test checks that it is exactly this one.
  ``test_the_direct_import_rewrites_references_in_creation_history`` is the
  bug's own test, expected to fail until it is fixed.

Requires the fixture: ``make redmine-fixture-up && make redmine-fixture-seed``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from specivo.importers.bundle.writer import BundleWriter, TextField, export_source, source_converter
from specivo.importers.load.reference_rewrite import rewrite_text
from specivo.models.journal import JournalDetail
from specivo.models.user import User
from tests.fixtures.import_compare import COUNTED, import_into_fresh_database, known_direct_bug, raw_differences
from tests.integration.importers.conftest import _argv, _flags

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.redmine,
    pytest.mark.serial,
    pytest.mark.slow,
]


async def _export(adapter, target) -> None:
    """Write the fixture as a bundle, as an importer for Redmine would."""
    issue_refs: set[str] = set()
    async for project in adapter.extract_projects():
        async for issue in adapter.extract_issues(project.source_ref):
            issue_refs.add(issue.source_ref)
    placeholders = {ref: f"{{{{issue:{ref}}}}}" for ref in issue_refs}
    markdown = source_converter(adapter)

    def convert(text: str, field: TextField) -> str:
        converted = markdown(text, field)
        if field is TextField.WIKI_TEXT:
            # The direct import leaves #123 in wiki text as written.
            return converted
        return rewrite_text(converted, placeholders)

    with BundleWriter(target, source_system="redmine", source_instance="pytest-fixture") as writer:
        await export_source(adapter, writer, convert)


async def test_a_bundle_import_matches_a_direct_import(redmine_profile, redmine_adapter, tmp_path, monkeypatch):
    import specivo.services.attachment_service as attachment_service

    monkeypatch.setattr(attachment_service, "_upload_dir", attachment_service._upload_dir)
    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    reference_fields = {
        field.key: field.field_format
        async for field in redmine_adapter.extract_custom_fields()
        if field.field_format in ("user", "version")
    }

    direct_summary, direct = await import_into_fresh_database(
        _argv(redmine_profile, merge_duplicate_statuses=True), reference_fields, tmp_path / "direct"
    )
    bundle_argv = ["--source", "bundle", "--path", str(bundle), *_flags(merge_duplicate_statuses=True)]
    bundle_summary, via_bundle = await import_into_fresh_database(bundle_argv, reference_fields, tmp_path / "bundle")

    assert direct["counts"]["Issue"] > 0
    differences = raw_differences(direct, via_bundle)
    known = known_direct_bug(differences)
    unexplained = {key: value for key, value in differences.items() if key not in known}
    assert unexplained == {}, "\n".join(f"{k}:\n  direct {d!r}\n  bundle {b!r}" for k, (d, b) in unexplained.items())
    # The known bug is there to see, on the one issue whose description has a reference.
    assert sorted(known) == ["journals[ACMEAPP-2]"]
    assert sorted(w.message for w in bundle_summary.warnings) == sorted(w.message for w in direct_summary.warnings)


@pytest.mark.xfail(
    strict=True,
    reason="Known bug: the direct Redmine import leaves #N in the description an issue's creation history records",
)
async def test_the_direct_import_rewrites_references_in_creation_history(db_session, build_import):
    await build_import(merge_duplicate_statuses=True).run()
    recorded = (
        await db_session.execute(
            select(JournalDetail.new_value).where(
                JournalDetail.property == "attr",
                JournalDetail.prop_key == "description",
                JournalDetail.new_value.like("%Related to%"),
            )
        )
    ).scalar_one()
    assert "Related to #2" not in recorded


async def test_the_exported_bundle_is_valid(redmine_adapter, tmp_path):
    from specivo.importers.bundle import BundleSourceAdapter

    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    report = await BundleSourceAdapter(bundle).validate()
    assert report.ok, report.format_text()
    assert report.problems == []


async def test_reloading_the_exported_bundle_creates_nothing(
    db_session, redmine_adapter, build_bundle_import, tmp_path
):
    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)
    await build_bundle_import(bundle, merge_duplicate_statuses=True).run()
    before = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}

    second = await build_bundle_import(bundle, merge_duplicate_statuses=True, continue_import=True).run()

    after = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}
    assert after == before
    assert sum(second.created.values()) == 0


async def test_a_bundle_after_a_direct_import_creates_nothing(
    db_session, build_import, redmine_adapter, build_bundle_import, tmp_path
):
    """The upgrade path: a database imported directly takes the tool's bundle as a delta.

    The direct import runs with ``--source-instance pytest-fixture`` and the
    bundle's manifest names the same instance, so both land in one scope of
    the identifier map and the bundle finds everything already there.
    """
    await build_import(merge_duplicate_statuses=True).run()
    before = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}
    bundle = tmp_path / "redmine.zip"
    await _export(redmine_adapter, bundle)

    summary = await build_bundle_import(bundle, merge_duplicate_statuses=True, continue_import=True).run()

    after = {m.__name__: (await db_session.execute(select(func.count()).select_from(m))).scalar_one() for m in COUNTED}
    assert after == before
    assert {entity: count for entity, count in summary.created.items() if count} == {}
    assert (summary.source_system, summary.source_instance) == ("redmine", "pytest-fixture")
    import_accounts = set((await db_session.execute(select(User.login).where(User.is_service_account))).scalars())
    assert "bundle-import" not in import_accounts
    assert "redmine-import" in import_accounts
