"""``MemberAdd`` and ``Principal``: exactly one principal, checked before the DB.

A ``members`` row is held by a user or by a user group, never both and never
neither (``ck_members_one_principal``). Both the request schema and the value
object the service takes enforce that themselves, so an impossible request is
a readable 422 rather than an IntegrityError from the CHECK constraint.
"""

from __future__ import annotations

import pytest

from specivo.core.exceptions import ValidationError
from specivo.schemas.project import MemberAdd, MemberOut
from specivo.services.project_service import Principal

pytestmark = pytest.mark.unit


class TestMemberAdd:
    def test_rejects_both_principals(self):
        with pytest.raises(ValueError, match="not both"):
            MemberAdd(user_id=1, group_id=2, role_ids=[1])

    def test_rejects_neither_principal(self):
        with pytest.raises(ValueError, match="either user_id or group_id"):
            MemberAdd(role_ids=[1])

    def test_accepts_a_user_alone(self):
        data = MemberAdd(user_id=7, role_ids=[1])
        assert (data.user_id, data.group_id) == (7, None)

    def test_accepts_a_group_alone(self):
        data = MemberAdd(group_id=7, role_ids=[1])
        assert (data.user_id, data.group_id) == (None, 7)

    def test_still_requires_at_least_one_role(self):
        with pytest.raises(ValueError, match="at least one role"):
            MemberAdd(user_id=1, role_ids=[])


class TestPrincipal:
    def test_of_rejects_both(self):
        with pytest.raises(ValidationError, match="Exactly one"):
            Principal.of(user_id=1, group_id=2)

    def test_of_rejects_neither(self):
        with pytest.raises(ValidationError, match="Exactly one"):
            Principal.of()

    def test_of_builds_a_user(self):
        principal = Principal.of(user_id=4)
        assert (principal.kind, principal.id) == ("user", 4)
        assert (principal.user_id, principal.group_id) == (4, None)

    def test_of_builds_a_group(self):
        principal = Principal.of(group_id=4)
        assert (principal.kind, principal.id) == ("group", 4)
        assert (principal.user_id, principal.group_id) == (None, 4)

    def test_parse_rejects_an_unknown_kind(self):
        with pytest.raises(ValidationError, match="principal_type"):
            Principal.parse("groups", 1)

    @pytest.mark.parametrize("kind", ["user", "group"])
    def test_parse_accepts_both_kinds(self, kind: str):
        assert Principal.parse(kind, 9).id == 9

    def test_a_user_and_a_group_with_the_same_id_are_different_principals(self):
        assert Principal.user(3) != Principal.group(3)


class TestMemberOut:
    def test_a_user_row_leaves_the_group_fields_empty(self):
        row = MemberOut(principal_type="user", user_id=1, login="alex", display_name="Alex", roles=["Dev"])
        assert (row.group_id, row.name, row.user_count) == (None, None, None)

    def test_a_group_row_leaves_the_user_fields_empty(self):
        row = MemberOut(principal_type="group", group_id=2, name="Developers", user_count=3, roles=["Dev"])
        assert (row.user_id, row.login, row.display_name) == (None, None, None)

    def test_the_discriminator_is_constrained(self):
        with pytest.raises(ValueError):
            MemberOut(principal_type="team", roles=[])
