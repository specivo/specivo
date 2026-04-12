"""Unit tests for the MetadataTargetRegistry singleton and Protocol."""

from __future__ import annotations

from specivo.core.metadata_targets import (
    IssueMetadataTarget,
    MetadataTarget,
    MetadataTargetRegistry,
    get_metadata_target_registry,
)


class _DummyTarget:
    scheme = "dummy"
    content_type = "dummy"
    permission = "view_issues"

    async def resolve(self, session, ref, user):  # pragma: no cover - trivial
        return {"ref": ref}

    def get_metadata(self, entity):
        return {}

    async def set_metadata(self, session, entity, metadata, user, api_key_id=None):
        entity["metadata"] = metadata
        return entity

    def project_id_of(self, entity):
        return 1

    def display_ref(self, entity):
        return entity.get("ref", "dummy")


class TestRegistry:
    def test_singleton_preserves_state(self):
        r1 = get_metadata_target_registry()
        r2 = get_metadata_target_registry()
        assert r1 is r2

    def test_core_registers_issue_target(self):
        reg = get_metadata_target_registry()
        target = reg.get("issue")
        assert target is not None
        assert target.scheme == "issue"
        assert target.content_type == "issue"
        assert isinstance(target, IssueMetadataTarget)

    def test_parse_ref_with_scheme(self):
        reg = MetadataTargetRegistry()
        assert reg.parse_ref("issue:ACME-12") == ("issue", "ACME-12")
        assert reg.parse_ref("wiki:home") == ("wiki", "home")

    def test_parse_ref_defaults_to_issue(self):
        reg = MetadataTargetRegistry()
        assert reg.parse_ref("ACME-12") == ("issue", "ACME-12")

    def test_register_and_unregister(self):
        reg = MetadataTargetRegistry()
        reg.register(_DummyTarget())
        assert reg.get("dummy") is not None
        assert "dummy" in reg.schemes()
        reg.unregister("dummy")
        assert reg.get("dummy") is None

    def test_register_overwrites(self):
        reg = MetadataTargetRegistry()
        reg.register(_DummyTarget())
        first = reg.get("dummy")
        reg.register(_DummyTarget())
        second = reg.get("dummy")
        assert first is not second

    def test_protocol_is_runtime_checkable(self):
        # runtime_checkable allows isinstance checks against structural type
        assert isinstance(_DummyTarget(), MetadataTarget)
        assert isinstance(IssueMetadataTarget(), MetadataTarget)

    def test_unknown_scheme_returns_none(self):
        reg = MetadataTargetRegistry()
        assert reg.get("nope") is None
