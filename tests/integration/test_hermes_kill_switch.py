"""The kill switch stops an agent's Hermes receivers as it stops its API keys, and unkill restores them.

- ``kill_agent`` stops that account's receivers: their events answer 404;
- ``kill_all`` stops the receivers of every service account, not a person's;
- ``unkill_agent`` restores them, but never a receiver its owner had switched
  off themselves, before or after the kill.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.hermes_receiver import HermesReceiver
from specivo.models.user import User
from specivo.services.hermes_receiver_service import HermesReceiverService, MintedReceiver
from specivo.services.kill_switch_service import KillSwitchService
from tests.factories.user import ServiceAccountFactory, UserFactory
from tests.fixtures.hermes_events import payload, post_event

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_receivers = HermesReceiverService()
_kill = KillSwitchService()


async def _account(db: AsyncSession, login: str, factory=ServiceAccountFactory) -> User:
    user = factory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    return user


async def _receiver(db: AsyncSession, owner: User, name: str = "host") -> MintedReceiver:
    minted = await _receivers.create(db, owner=owner, name=name, actor=owner)
    await db.commit()
    return minted


async def _status(client: AsyncClient, minted: MintedReceiver, session_id: str = "k-sess") -> int:
    return (await post_event(client, minted.token, payload("on_session_start", session_id), minted.secret)).status_code


async def _fresh(db: AsyncSession, receiver: HermesReceiver) -> HermesReceiver:
    await db.refresh(receiver)
    return receiver


async def test_kill_agent_stops_its_receivers_and_unkill_restores_them(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    robot = await _account(db_session, "hkill_robot")
    other = await _account(db_session, "hkill_other")
    mine = await _receiver(db_session, robot)
    theirs = await _receiver(db_session, other)

    await _kill.kill_agent(db_session, robot.id, triggered_by="admin", reason="test")
    await db_session.commit()
    assert await _status(client, mine) == 404
    assert await _status(client, theirs) == 200
    assert (await client.get(f"/api/v1/hermes/events/{mine.token}/")).status_code == 404

    await _kill.unkill_agent(db_session, robot.id)
    await db_session.commit()
    assert await _status(client, mine, "k-after") == 200
    assert (await _fresh(db_session, mine.receiver)).is_active is True


async def test_kill_all_stops_service_accounts_only(client: AsyncClient, db_session: AsyncSession) -> None:
    robot = await _account(db_session, "hkill_all_robot")
    person = await _account(db_session, "hkill_all_person", factory=UserFactory)
    robots = await _receiver(db_session, robot)
    persons = await _receiver(db_session, person)

    await _kill.kill_all(db_session, triggered_by="admin", reason="test")
    await db_session.commit()
    assert await _status(client, robots) == 404
    assert await _status(client, persons) == 200

    await _kill.unkill_agent(db_session, robot.id)
    await db_session.commit()
    assert await _status(client, robots, "k-after") == 200


async def test_unkill_never_switches_on_a_receiver_its_owner_switched_off(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    robot = await _account(db_session, "hkill_owner")
    off_before = await _receiver(db_session, robot, "off before the kill")
    off_after = await _receiver(db_session, robot, "off after the kill")
    await _receivers.update(db_session, off_before.receiver, owner=robot, actor=robot, is_active=False)
    await db_session.commit()

    await _kill.kill_agent(db_session, robot.id, triggered_by="admin", reason="test")
    await _receivers.update(db_session, off_after.receiver, owner=robot, actor=robot, is_active=False)
    await db_session.commit()
    await _kill.unkill_agent(db_session, robot.id)
    await db_session.commit()

    assert (await _fresh(db_session, off_before.receiver)).is_active is False
    assert (await _fresh(db_session, off_after.receiver)).is_active is False
    assert await _status(client, off_before) == 404
    assert await _status(client, off_after) == 404

    # A second unkill later still leaves them off.
    await _kill.unkill_agent(db_session, robot.id)
    await db_session.commit()
    assert (await _fresh(db_session, off_before.receiver)).is_active is False


async def test_a_killed_receiver_cannot_be_switched_back_on_by_its_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Switching it off and on again does not get round the kill; only unkill lifts it."""
    robot = await _account(db_session, "hkill_toggle")
    minted = await _receiver(db_session, robot)
    await _kill.kill_agent(db_session, robot.id, triggered_by="admin", reason="test")
    await _receivers.update(db_session, minted.receiver, owner=robot, actor=robot, is_active=False)
    await _receivers.update(db_session, minted.receiver, owner=robot, actor=robot, is_active=True)
    await db_session.commit()
    assert await _status(client, minted) == 404
