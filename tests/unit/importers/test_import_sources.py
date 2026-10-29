"""Import sources: the registry holds ``bundle`` only, and the base class every source extends."""

from __future__ import annotations

from pathlib import Path

import pytest

from specivo.importers.bundle import BundleSourceAdapter
from specivo.importers.core.converter import PassthroughConverter
from specivo.importers.core.ir import ContainerKind, IRAttachment, IRLookups
from specivo.importers.core.source import (
    BaseSourceAdapter,
    SourceAdapter,
    SourceAdapterRegistry,
    UnknownSourceError,
    build_source_registry,
    get_source_registry,
)

pytestmark = pytest.mark.unit


class Other(BaseSourceAdapter):
    source_system = "other"


def test_bundle_is_the_only_source():
    registry = build_source_registry()
    assert registry.names() == ["bundle"]
    assert registry.get("bundle") is BundleSourceAdapter


def test_the_process_wide_registry_is_the_same_single_source():
    assert get_source_registry().names() == ["bundle"]


class TestRegistry:
    def test_register_and_get(self):
        registry = SourceAdapterRegistry()
        registry.register("other", Other)
        assert registry.get("other") is Other
        assert registry.names() == ["other"]

    def test_unknown_source_lists_what_is_available(self):
        with pytest.raises(UnknownSourceError) as exc:
            build_source_registry().get("jira")
        assert str(exc.value) == "Unknown import source 'jira'. Registered sources: bundle"

    def test_empty_registry_reports_none_registered(self):
        with pytest.raises(KeyError, match="none"):
            SourceAdapterRegistry().get("bundle")

    def test_descriptions_come_from_the_adapters(self):
        assert "import bundle" in build_source_registry().descriptions()["bundle"]


class TestBaseSourceAdapter:
    class Empty(BaseSourceAdapter):
        source_system = "empty"

    def test_it_satisfies_the_protocol(self):
        assert isinstance(self.Empty(), SourceAdapter)
        assert isinstance(BundleSourceAdapter(Path("bundle")), SourceAdapter)

    def test_instance_defaults_to_the_system_name(self):
        assert self.Empty().source_instance == "empty"
        assert self.Empty("eu").source_instance == "eu"

    def test_no_passwords_and_no_conversion(self):
        adapter = self.Empty()
        assert adapter.credential_algorithms == ()
        assert isinstance(adapter.content_converter(), PassthroughConverter)

    async def test_every_stream_is_empty(self):
        adapter = self.Empty()
        await adapter.connect()
        assert await adapter.extract_lookups() == IRLookups()
        for stream in (
            adapter.extract_users(),
            adapter.extract_groups(),
            adapter.extract_projects(),
            adapter.extract_custom_fields(),
        ):
            assert [item async for item in stream] == []
        for method in (
            adapter.extract_memberships,
            adapter.extract_versions,
            adapter.extract_categories,
            adapter.extract_issues,
            adapter.extract_journals,
            adapter.extract_relations,
            adapter.extract_watchers,
            adapter.extract_attachments,
            adapter.extract_wiki_pages,
            adapter.extract_time_entries,
        ):
            assert [item async for item in method("1")] == [], method.__name__
        await adapter.close()

    def test_attachment_files_are_refused_with_a_value_error(self):
        """The attachment loader reports and skips a ValueError."""
        attachment = IRAttachment(
            source_ref="1", container_kind=ContainerKind.ISSUE, container_ref="1", filename="a.txt", storage_key="a"
        )
        with pytest.raises(ValueError, match="empty source does not provide attachment files"):
            self.Empty().resolve_attachment_path(attachment)
