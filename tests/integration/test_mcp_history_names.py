"""``specivo_list_comments(include_changes=True)`` names a change's records by the issue page's rules."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp.tools import _list_comments
from tests.factories.user import UserFactory
from tests.integration.test_journal_detail_names import build_history

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


async def test_history_names_only_what_the_user_may_see(db_session: AsyncSession) -> None:
    reader = UserFactory.build(login="jdm_reader", display_name="JDM Reader", status="active")
    db_session.add(reader)
    await db_session.flush()
    world = await build_history(db_session, reader, prefix="JDM")
    first, second = world["statuses"]

    text = await _list_comments(db_session, reader, world["issue"].display_key, include_changes=True)

    assert f"status_id: {first.name} (id {first.id}) -> {second.name} (id {second.id})" in text
    own = world["own_version"]
    assert f"fixed_version_id: {world['their_version'].id} -> {own.name} (id {own.id})" in text
    assert f"assigned_to_id: {world['stranger'].id} -> JDM Reader (id {reader.id})" in text
    # A custom field by its name (its key humanised, the schema giving no title), and its key.
    assert f"Reviewer (reviewer, custom field): {world['stranger'].id} -> JDM Reader (id {reader.id})" in text
    assert f"parent_id: {world['own_parent'].display_key} -> (none)" in text
    for secret in (world["their_version"].name, "JDM Stranger", world["their_parent"].display_key):
        assert secret not in text


async def test_without_changes_only_comments_are_listed(db_session: AsyncSession) -> None:
    reader = UserFactory.build(login="jdm_plain", display_name="JDM Plain", status="active")
    db_session.add(reader)
    await db_session.flush()
    world = await build_history(db_session, reader, prefix="JDQ")

    text = await _list_comments(db_session, reader, world["issue"].display_key)
    assert "status_id" not in text
    assert "(0 total" in text
