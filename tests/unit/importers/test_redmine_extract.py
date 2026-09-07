"""Unit tests for Redmine row to IR translation.

Every case is a literal row dict, so the translation rules are pinned without a
Redmine instance anywhere near the test run. Columns and values were taken from
a real Redmine 7.0.1 schema.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from specivo.importers.redmine.extract import (
    REDMINE_CORE_FIELDS,
    TYPE_GROUP,
    TYPE_USER,
    as_utc,
    decode_disabled_core_fields,
    display_name,
    extract_activity,
    extract_group,
    extract_priority,
    extract_role,
    extract_status,
    extract_tracker,
    extract_user,
    map_user_status,
    normalise_language,
    status_category,
)

pytestmark = pytest.mark.unit


class TestTimestamps:
    def test_naive_redmine_timestamp_is_stamped_utc(self):
        """Rails writes UTC into a column with no zone; only the marker is missing."""
        stamped = as_utc(datetime(2026, 3, 1, 12, 30))
        assert stamped == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)

    def test_none_passes_through(self):
        assert as_utc(None) is None

    def test_aware_timestamp_is_left_alone(self):
        original = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
        assert as_utc(original) is original


class TestDisabledCoreFields:
    def test_no_bits_means_nothing_disabled(self):
        assert decode_disabled_core_fields(0) == []

    def test_null_bitmask_means_nothing_disabled(self):
        assert decode_disabled_core_fields(None) == []

    def test_first_bit_disables_the_first_core_field(self):
        assert decode_disabled_core_fields(1) == ["assigned_to_id"]

    def test_bit_position_follows_redmine_field_order(self):
        """Redmine appends to CORE_FIELDS, so index is the wire format."""
        assert REDMINE_CORE_FIELDS[5] == "due_date"
        assert decode_disabled_core_fields(1 << 5) == ["due_date"]

    def test_several_bits_decode_in_field_order(self):
        bits = (1 << 4) | (1 << 6)  # start_date, estimated_hours
        assert decode_disabled_core_fields(bits) == ["start_date", "estimated_hours"]

    def test_parent_field_is_renamed_to_the_specivo_column(self):
        """Redmine calls it parent_issue_id; the Specivo column is parent_id."""
        assert decode_disabled_core_fields(1 << 3) == ["parent_id"]

    def test_all_bits_disables_every_field(self):
        every_bit = (1 << len(REDMINE_CORE_FIELDS)) - 1
        assert len(decode_disabled_core_fields(every_bit)) == len(REDMINE_CORE_FIELDS)


class TestStatusCategory:
    def test_closed_status_is_closed(self):
        assert status_category("Closed", is_closed=True) == "closed"

    def test_rejected_is_closed_when_redmine_says_so(self):
        assert status_category("Rejected", is_closed=True) == "closed"

    def test_new_is_backlog(self):
        assert status_category("New", is_closed=False) == "backlog"

    def test_resolved_is_done(self):
        assert status_category("Resolved", is_closed=False) == "done"

    def test_anything_else_is_active(self):
        assert status_category("In Progress", is_closed=False) == "active"

    def test_matching_ignores_case_and_padding(self):
        assert status_category("  NEW  ", is_closed=False) == "backlog"

    def test_override_wins_over_the_heuristic(self):
        assert status_category("In Progress", is_closed=False, overrides={"in progress": "done"}) == "done"

    def test_override_wins_over_is_closed(self):
        """The operator has the last word, including on a closing status."""
        assert status_category("Rejected", is_closed=True, overrides={"rejected": "done"}) == "done"

    def test_unrelated_override_does_not_apply(self):
        assert status_category("New", is_closed=False, overrides={"feedback": "done"}) == "backlog"


class TestUserStatus:
    def test_active(self):
        assert map_user_status(1) == "active"

    def test_registered_becomes_pending_verification(self):
        assert map_user_status(2) == "pending_verification"

    def test_locked(self):
        assert map_user_status(3) == "locked"

    def test_unknown_status_locks_the_account(self):
        """Being wrong towards refusing sign-in is the safe direction."""
        assert map_user_status(99) == "locked"

    def test_missing_status_locks_the_account(self):
        assert map_user_status(None) == "locked"


class TestLanguage:
    def test_plain_code_passes_through(self):
        assert normalise_language("en") == "en"

    def test_region_is_dropped(self):
        assert normalise_language("pt-BR") == "pt"

    def test_case_is_normalised(self):
        assert normalise_language("ZH-TW") == "zh"

    def test_empty_means_no_preference(self):
        assert normalise_language("") is None

    def test_null_means_no_preference(self):
        assert normalise_language(None) is None


class TestDisplayName:
    def test_first_and_last_are_joined(self):
        assert display_name({"id": 1, "firstname": "Alex", "lastname": "Kim", "login": "alex"}) == "Alex Kim"

    def test_group_name_lives_in_lastname(self):
        assert display_name({"id": 5, "firstname": "", "lastname": "Platform Team"}) == "Platform Team"

    def test_falls_back_to_login_when_unnamed(self):
        assert display_name({"id": 1, "firstname": "", "lastname": "", "login": "alex"}) == "alex"

    def test_falls_back_to_the_id_when_there_is_nothing(self):
        """A blank display name is worse than a synthetic one."""
        assert display_name({"id": 7, "firstname": "", "lastname": "", "login": ""}) == "user-7"


class TestExtractTracker:
    def _row(self, **overrides):
        row = {
            "id": 1,
            "name": "Bug",
            "position": 1,
            "is_in_roadmap": True,
            "fields_bits": 0,
            "default_status_id": 1,
            "description": "Something is broken",
            "private_by_default": False,
        }
        row.update(overrides)
        return row

    def test_maps_the_plain_fields(self):
        tracker = extract_tracker(self._row())
        assert tracker.source_ref == "1"
        assert tracker.name == "Bug"
        assert tracker.is_in_roadmap is True
        assert tracker.default_status_ref == "1"
        assert tracker.description == "Something is broken"

    def test_decodes_the_bitmask(self):
        tracker = extract_tracker(self._row(fields_bits=(1 << 5)))
        assert tracker.disabled_core_fields == ["due_date"]

    def test_missing_default_status_is_none(self):
        assert extract_tracker(self._row(default_status_id=None)).default_status_ref is None

    def test_blank_description_becomes_none(self):
        assert extract_tracker(self._row(description="")).description is None


class TestExtractStatus:
    def test_maps_and_categorises(self):
        status = extract_status(
            {"id": 5, "name": "Closed", "is_closed": True, "position": 5, "default_done_ratio": 100}
        )
        assert status.source_ref == "5"
        assert status.name == "Closed"
        assert status.category == "closed"
        assert status.default_done_ratio == 100

    def test_passes_overrides_through(self):
        status = extract_status(
            {"id": 3, "name": "Feedback", "is_closed": False, "position": 3, "default_done_ratio": None},
            {"feedback": "done"},
        )
        assert status.category == "done"


class TestExtractEnumerations:
    def test_priority(self):
        priority = extract_priority({"id": 4, "name": "Normal", "position": 2, "is_default": True, "active": True})
        assert priority.source_ref == "4"
        assert priority.name == "Normal"
        assert priority.is_default is True
        assert priority.active is True

    def test_activity(self):
        activity = extract_activity({"id": 9, "name": "Development", "is_default": True, "active": True})
        assert activity.source_ref == "9"
        assert activity.name == "Development"
        assert activity.is_default is True


class TestExtractRole:
    def test_maps_name_and_builtin(self):
        role = extract_role({"id": 1, "name": "Non member", "builtin": 1, "permissions": "---\n- :view_issues\n"})
        assert role.source_ref == "1"
        assert role.name == "Non member"
        assert role.builtin == 1

    def test_permissions_are_not_translated(self):
        """Redmine's permission names are its own; mapping them would be a guess."""
        role = extract_role({"id": 3, "name": "Developer", "builtin": 0, "permissions": "---\n- :add_issues\n"})
        assert role.permissions == []

    def test_ordinary_role_has_builtin_zero(self):
        assert extract_role({"id": 3, "name": "Developer", "builtin": 0}).builtin == 0


class TestExtractUser:
    def _row(self, **overrides):
        row = {
            "id": 12,
            "login": "alex",
            "firstname": "Alex",
            "lastname": "Kim",
            "admin": False,
            "status": 1,
            "last_login_on": datetime(2026, 3, 1, 8, 0),
            "language": "en",
            "created_on": datetime(2025, 1, 5, 9, 0),
            "updated_on": datetime(2026, 2, 2, 10, 0),
            "type": TYPE_USER,
        }
        row.update(overrides)
        return row

    def test_maps_the_account(self):
        user = extract_user(self._row(), email="alex@example.org")
        assert user.source_ref == "12"
        assert user.login == "alex"
        assert user.display_name == "Alex Kim"
        assert user.email == "alex@example.org"
        assert user.status == "active"
        assert user.is_admin is False

    def test_timestamps_become_utc_aware(self):
        user = extract_user(self._row())
        assert user.created_at == datetime(2025, 1, 5, 9, 0, tzinfo=UTC)
        assert user.last_login_at == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)

    def test_missing_address_is_none(self):
        """An account can exist with no default address; the loader decides."""
        assert extract_user(self._row()).email is None

    def test_blank_address_is_none(self):
        assert extract_user(self._row(), email="   ").email is None

    def test_admin_flag_carries_over(self):
        assert extract_user(self._row(admin=True)).is_admin is True

    def test_locked_account_stays_locked(self):
        assert extract_user(self._row(status=3)).status == "locked"

    def test_no_credential_is_carried(self):
        """Redmine hashes with SHA1 and Specivo with bcrypt; nothing is portable."""
        user = extract_user(self._row())
        assert not hasattr(user, "password_hash")


class TestExtractGroup:
    def test_maps_name_and_members(self):
        group = extract_group(
            {"id": 20, "firstname": "", "lastname": "Platform Team", "login": "", "type": TYPE_GROUP},
            ["1", "2"],
        )
        assert group.source_ref == "20"
        assert group.name == "Platform Team"
        assert group.member_refs == ["1", "2"]

    def test_empty_group_is_valid(self):
        group = extract_group({"id": 21, "firstname": "", "lastname": "Empty", "login": ""}, [])
        assert group.member_refs == []

    def test_member_list_is_copied_not_aliased(self):
        members = ["1"]
        group = extract_group({"id": 22, "firstname": "", "lastname": "Team", "login": ""}, members)
        members.append("2")
        assert group.member_refs == ["1"]
