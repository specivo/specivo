"""Import source registration: the plugin hook, the registry rules and the base class."""

from __future__ import annotations

import pytest

import specivo.importers.core.source as source_mod
import specivo.services.password_hashers as hashers_mod
from specivo.core.config import get_settings
from specivo.core.plugin import BasePluginConfig, PluginConfig
from specivo.core.plugin_manager import PluginManager, bootstrap
from specivo.importers.core.converter import PassthroughConverter
from specivo.importers.core.ir import ContainerKind, IRAttachment, IRLookups
from specivo.importers.core.source import (
    BaseSourceAdapter,
    SourceAdapter,
    SourceAdapterRegistry,
    SourceRegistrationError,
    UnknownSourceError,
    build_source_registry,
    get_source_registry,
)
from specivo.importers.redmine import RedmineSourceAdapter
from tests.fixtures.acme_import import AcmeJsonSource

pytestmark = pytest.mark.unit


class FakeRedmine(BaseSourceAdapter):
    source_system = "redmine"


class OtherAcme(BaseSourceAdapter):
    source_system = "acme"


class Squatter(BasePluginConfig):
    """A plugin claiming the built-in Redmine source's name."""

    name, tier = "squatter", "core"

    def get_import_sources(self):
        return [FakeRedmine]


class Copycat(BasePluginConfig):
    """A second plugin registering the example plugin's source name."""

    name, tier = "copycat", "core"

    def get_import_sources(self):
        return [OtherAcme]


@pytest.fixture(autouse=True)
def _restore_registries(monkeypatch):
    """Bootstrapping installs process-wide registries; put the old ones back."""
    monkeypatch.setattr(source_mod, "_registry", source_mod._registry)
    monkeypatch.setattr(hashers_mod, "_registry", hashers_mod._registry)


def _bootstrap(*plugins: str) -> PluginManager:
    return bootstrap(get_settings().model_copy(update={"installed_plugins": list(plugins)}))


class TestPluginHook:
    def test_the_base_plugin_has_no_sources(self):
        class Bare(BasePluginConfig):
            name, tier = "bare", "core"

        assert Bare().get_import_sources() == []
        assert isinstance(Bare(), PluginConfig)

    def test_the_manager_collects_sources_per_plugin(self):
        pm = PluginManager()
        pm.load_plugins(["tests.fixtures.acme_import.Plugin"])
        assert pm.collect_import_sources() == [("acme-import", [AcmeJsonSource])]


class TestBootstrap:
    def test_a_plugin_source_is_registered(self):
        _bootstrap("tests.fixtures.acme_import.Plugin")

        registry = get_source_registry()
        assert registry.get("acme") is AcmeJsonSource
        assert registry.entries()["acme"].origin == "plugin:acme-import"
        assert registry.entries()["redmine"].origin == "core"

    def test_without_the_plugin_only_the_builtin_sources_are_there(self):
        _bootstrap()
        assert get_source_registry().names() == ["bundle", "redmine"]

    def test_bootstrapping_twice_is_not_a_duplicate(self):
        """create_app() runs the bootstrap per app, and tests build many apps."""
        _bootstrap("tests.fixtures.acme_import.Plugin")
        _bootstrap("tests.fixtures.acme_import.Plugin")
        assert get_source_registry().names() == ["acme", "bundle", "redmine"]

    def test_claiming_redmine_fails(self):
        with pytest.raises(SourceRegistrationError, match="reserved"):
            _bootstrap("tests.unit.importers.test_import_sources.Squatter")

    def test_a_duplicate_name_fails(self):
        with pytest.raises(SourceRegistrationError, match="registered twice") as exc:
            _bootstrap("tests.fixtures.acme_import.Plugin", "tests.unit.importers.test_import_sources.Copycat")
        assert "plugin:acme-import" in str(exc.value)
        assert "plugin:copycat" in str(exc.value)

    def test_a_failed_bootstrap_installs_nothing(self):
        before = get_source_registry()
        with pytest.raises(SourceRegistrationError):
            _bootstrap("tests.unit.importers.test_import_sources.Squatter")
        assert get_source_registry() is before


class TestRegistry:
    def test_register_and_get(self):
        registry = SourceAdapterRegistry()
        registry.register("fake", OtherAcme)
        assert registry.get("fake") is OtherAcme
        assert registry.names() == ["fake"]

    def test_unknown_source_lists_what_is_available(self):
        registry = SourceAdapterRegistry()
        registry.register("redmine", RedmineSourceAdapter)
        with pytest.raises(UnknownSourceError) as exc:
            registry.get("jira")
        assert str(exc.value) == "Unknown import source 'jira'. Registered sources: redmine"

    def test_empty_registry_reports_none_registered(self):
        with pytest.raises(KeyError, match="none"):
            SourceAdapterRegistry().get("redmine")

    def test_registering_a_name_twice_is_an_error(self):
        registry = SourceAdapterRegistry()
        registry.register("acme", AcmeJsonSource, "plugin:a")
        with pytest.raises(SourceRegistrationError, match="registered twice"):
            registry.register("acme", OtherAcme, "plugin:b")

    def test_redmine_is_reserved_for_core(self):
        registry = SourceAdapterRegistry()
        with pytest.raises(SourceRegistrationError, match="reserved"):
            registry.register("redmine", FakeRedmine, "plugin:squatter")

    @pytest.mark.parametrize("name", ["", "a", "Acme", "acme-tracker", "9lives", "x" * 31, None])
    def test_invalid_names_are_rejected(self, name):
        with pytest.raises(SourceRegistrationError, match="Invalid import source name"):
            SourceAdapterRegistry().register(name, OtherAcme, "plugin:p")

    def test_the_built_in_registry_has_redmine(self):
        registry = build_source_registry()
        assert registry.get("redmine") is RedmineSourceAdapter

    def test_a_plugin_must_return_classes(self):
        with pytest.raises(SourceRegistrationError, match="not an adapter class"):
            build_source_registry([("p", [AcmeJsonSource("x.json")])])

    def test_a_class_without_the_contract_is_rejected(self):
        class NotAnAdapter:
            source_system = "nope"

        with pytest.raises(SourceRegistrationError, match="add_arguments, from_args"):
            build_source_registry([("p", [NotAnAdapter])])

    def test_a_class_without_a_name_is_rejected(self):
        class Nameless(BaseSourceAdapter):
            pass

        with pytest.raises(SourceRegistrationError, match="source_system"):
            build_source_registry([("p", [Nameless])])


class TestBaseSourceAdapter:
    class Empty(BaseSourceAdapter):
        source_system = "empty"

    def test_it_satisfies_the_protocol(self):
        assert isinstance(self.Empty(), SourceAdapter)
        assert isinstance(RedmineSourceAdapter("postgresql://u:p@h/redmine"), SourceAdapter)

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
