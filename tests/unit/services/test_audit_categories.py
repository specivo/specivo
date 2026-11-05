"""Every audit event belongs to exactly one viewer category, and every category has a label."""

from __future__ import annotations

import pytest

from specivo.services.security_audit_service import EVENT_CATEGORIES, AuditCategory, AuditEvent, events_in

pytestmark = pytest.mark.unit


def test_every_audit_event_has_a_category() -> None:
    assert set(EVENT_CATEGORIES) == set(AuditEvent)


def test_every_category_is_used_and_the_categories_split_the_events() -> None:
    groups = [events_in(category) for category in AuditCategory]
    assert all(groups)
    assert sorted(value for group in groups for value in group) == sorted(e.value for e in AuditEvent)


def test_every_category_has_a_translatable_label() -> None:
    from specivo.web.pages.admin import audit_category_labels

    labels = audit_category_labels()
    assert set(labels) == set(AuditCategory)
    assert all(labels.values())
