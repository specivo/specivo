"""The Hermes event receiver: signed Hermes webhooks become agent sessions and audit rows.

Security first (Hermes treats every 4xx as final and retries a 5xx once):

- a missing or wrong signature is 401 ``invalid_signature``, writes one
  rejected row and nothing else; a rotated secret stops the old one at once;
- a stale or missing timestamp is 401 ``stale``; a redelivered id is 200
  ``duplicate`` and writes nothing;
- an unknown, inactive or deactivated-account token is 404, audited at most
  once a minute per address;
- a body over the limit is 413, by Content-Length and while streaming;
- an event naming another account's session id never attaches to it, and a
  subagent never links to another account's parent;
- tool inputs, results, commands, goals and summaries leave no trace, and no
  secret or token is logged.

Then the mapping: session start, tool calls (count), approvals, subagents
(child linked, stopped), session end (``ended_at``), ignored events (no AI
usage), the ``doctor`` GET, the bare URL, and the agent sessions page showing
the events of a session that names an issue. Rate limiting is in
``test_hermes_events_rate_limit.py`` (serial).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.hermes_receiver import HermesDelivery, HermesReceiver
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.hermes_event_service import purge_old_deliveries
from specivo.services.hermes_receiver_service import HermesReceiverService, MintedReceiver
from specivo.services.permission_service import clear_role_cache
from tests.factories.user import ServiceAccountFactory
from tests.fixtures.hermes_events import CANARY, event_path, headers, iso, payload, post_event
from tests.integration.agent_session_world import SessionWorld, build_session_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_service = HermesReceiverService()


async def _account(db: AsyncSession, login: str, **fields) -> User:
    user = ServiceAccountFactory.build(login=login, status="active", **fields)
    db.add(user)
    await db.flush()
    return user


async def _receiver(db: AsyncSession, owner: User, name: str = "build host") -> MintedReceiver:
    minted = await _service.create(db, owner=owner, name=name, actor=owner)
    await db.commit()
    return minted


@pytest_asyncio.fixture
async def alpha(db_session: AsyncSession) -> MintedReceiver:
    return await _receiver(db_session, await _account(db_session, "hrev_alpha"))


async def _rows(db: AsyncSession, model, *where) -> list:
    stmt = select(model).where(*where).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


async def _audit(db: AsyncSession, event_type: str) -> list[SecurityAuditLog]:
    return await _rows(db, SecurityAuditLog, SecurityAuditLog.event_type == event_type)


async def _sessions(db: AsyncSession, user_id: int) -> list[AgentSession]:
    stmt = (
        select(AgentSession)
        .where(AgentSession.user_id == user_id)
        .order_by(AgentSession.id)
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Signature, timestamp, redelivery
# ---------------------------------------------------------------------------


async def test_a_signed_session_start_opens_a_webhook_session_and_an_audit_row(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    body = payload("on_session_start", "sess-start-1", extra={"model": "lab-model-1", "platform": "cli"})
    resp = await post_event(client, alpha.token, body, alpha.secret)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    [session] = await _sessions(db_session, alpha.receiver.user_id)
    assert data["agent_session_id"] == session.id
    assert session.client_type == "webhook"
    assert session.client_name == "hermes-agent"
    assert session.api_key_id is None
    assert session.receiver_id == alpha.receiver.id
    assert session.external_session_id == "sess-start-1"
    [row] = await _audit(db_session, "agent_session_started")
    assert row.user_id == alpha.receiver.user_id
    assert row.agent_session_id == session.id
    assert row.details["source"] == "hermes"
    assert row.details["delivery_id"] == body["delivery_id"]
    assert row.details["model"] == "lab-model-1"
    assert row.details["profile"] == "default"
    [receiver] = await _rows(db_session, HermesReceiver, HermesReceiver.id == alpha.receiver.id)
    assert receiver.last_event_at is not None


async def test_a_missing_signature_is_401_and_one_rejected_row(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    resp = await post_event(client, alpha.token, payload("on_session_start", "sess-unsigned"), None)

    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "invalid_signature"
    [row] = await _audit(db_session, "hermes_event_rejected")
    assert row.details["reason"] == "missing_signature"
    assert row.details["receiver_id"] == alpha.receiver.id
    assert await _sessions(db_session, alpha.receiver.user_id) == []
    assert await _rows(db_session, HermesDelivery) == []


@pytest.mark.parametrize("secret", ["a-guessed-secret", ""])
async def test_a_wrong_signature_is_401_and_writes_nothing_else(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, secret: str
) -> None:
    resp = await post_event(client, alpha.token, payload("post_tool_call", "sess-forged"), secret)

    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "invalid_signature"
    [row] = await _audit(db_session, "hermes_event_rejected")
    assert row.details["reason"] == "invalid_signature"
    assert await _sessions(db_session, alpha.receiver.user_id) == []
    assert await _audit(db_session, "agent_tool_called") == []


async def test_a_signature_over_another_body_is_refused(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    signed = payload("post_tool_call", "sess-tamper", tool_name="read_file")
    raw = json.dumps(signed).encode()
    tampered = raw.replace(b"read_file", b"terminal")
    resp = await client.post(event_path(alpha.token), content=tampered, headers=headers(raw, alpha.secret))
    assert resp.status_code == 401


async def test_another_receivers_secret_does_not_verify(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    beta = await _receiver(db_session, await _account(db_session, "hrev_beta"))
    resp = await post_event(client, alpha.token, payload("on_session_start", "sess-cross"), beta.secret)
    assert resp.status_code == 401


async def test_a_rotated_secret_replaces_the_old_one_at_once(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    owner = await db_session.get(User, alpha.receiver.user_id)
    new_secret = await _service.update(db_session, alpha.receiver, owner=owner, actor=owner, rotate_secret=True)
    await db_session.commit()
    assert new_secret and new_secret != alpha.secret

    assert (
        await post_event(client, alpha.token, payload("on_session_start", "s-rot"), alpha.secret)
    ).status_code == 401
    assert (await post_event(client, alpha.token, payload("on_session_start", "s-rot"), new_secret)).status_code == 200


async def test_a_redelivered_event_is_a_duplicate_and_written_once(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    body = payload("post_tool_call", "sess-dup", tool_name="terminal", extra={"status": "ok"})
    first = await post_event(client, alpha.token, body, alpha.secret)
    second = await post_event(client, alpha.token, body, alpha.secret)

    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(await _audit(db_session, "agent_tool_called")) == 1
    [session] = await _sessions(db_session, alpha.receiver.user_id)
    assert session.tool_call_count == 1


@pytest.mark.parametrize(
    "stamp",
    [
        {"age": timedelta(minutes=11)},
        {"age": -timedelta(minutes=11)},
        {"timestamp": ""},
        {"timestamp": "2026-11-23 12:00:00"},
        {"timestamp": "not a time"},
    ],
    ids=["old", "future", "missing", "naive", "garbage"],
)
async def test_a_stale_or_missing_timestamp_is_401(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, stamp: dict
) -> None:
    resp = await post_event(client, alpha.token, payload("on_session_start", "sess-stale", **stamp), alpha.secret)

    assert resp.status_code == 401
    assert resp.json()["errors"][0]["code"] == "stale"
    [row] = await _audit(db_session, "hermes_event_rejected")
    assert row.details["reason"] == "stale"
    assert await _sessions(db_session, alpha.receiver.user_id) == []
    assert await _rows(db_session, HermesDelivery) == []


async def test_a_replay_inside_the_window_is_a_duplicate(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    body = payload("on_session_start", "sess-replay", age=timedelta(minutes=9))
    assert (await post_event(client, alpha.token, body, alpha.secret)).json()["status"] == "accepted"
    assert (await post_event(client, alpha.token, body, alpha.secret)).json()["status"] == "duplicate"


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b'{"hook_event_name": "on_session_start", "session_id": "x"}'],
    ids=["not-json", "not-object", "no-delivery-id"],
)
async def test_a_signed_but_malformed_body_is_400(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, raw: bytes
) -> None:
    if raw.startswith(b"{"):
        raw = json.dumps({**json.loads(raw), "timestamp": iso(utcnow())}).encode()
    resp = await post_event(client, alpha.token, raw, alpha.secret)
    assert resp.status_code == 400
    assert await _sessions(db_session, alpha.receiver.user_id) == []


# ---------------------------------------------------------------------------
# Unknown receivers, body size
# ---------------------------------------------------------------------------


@pytest.mark.serial  # the once-a-minute throttle lives in Redis, which other workers flush
async def test_an_unknown_token_is_404_and_audited_once_a_minute(client: AsyncClient, db_session: AsyncSession) -> None:
    from specivo.core.redis import get_redis

    redis = await get_redis()
    for key in await redis.keys("audit:auth_failure:*:hermes_unknown_receiver"):
        await redis.delete(key)
    token = "x" * 43
    first = await post_event(client, token, payload("on_session_start", "sess-unknown"), "whatever")
    second = await post_event(client, token, payload("on_session_start", "sess-unknown"), "whatever")
    short = await post_event(client, "short", payload("on_session_start", "sess-unknown"), "whatever")

    assert [first.status_code, second.status_code, short.status_code] == [404, 404, 404]
    rows = await _audit(db_session, "hermes_event_rejected")
    assert [r.details["reason"] for r in rows] == ["unknown_receiver"]
    assert token not in str(rows[0].details)


async def test_an_inactive_receiver_and_a_deactivated_account_are_unknown(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    alpha.receiver.is_active = False
    await db_session.commit()
    assert (await post_event(client, alpha.token, payload("on_session_start", "s1"), alpha.secret)).status_code == 404

    alpha.receiver.is_active = True
    owner = await db_session.get(User, alpha.receiver.user_id)
    owner.status = "deactivated"
    await db_session.commit()
    assert (await post_event(client, alpha.token, payload("on_session_start", "s1"), alpha.secret)).status_code == 404

    owner.status = "locked"  # a locked account keeps its receivers, as it keeps its API keys
    await db_session.commit()
    assert (await post_event(client, alpha.token, payload("on_session_start", "s1"), alpha.secret)).status_code == 200


async def test_a_body_over_the_limit_is_413(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "hermes_event_max_body_bytes", 2048)
    big = payload("post_tool_call", "sess-big", tool_input={"content": "x" * 4000})
    assert (await post_event(client, alpha.token, big, alpha.secret)).status_code == 413

    async def _stream():
        yield b'{"padding": "'
        for _ in range(10):
            yield b"y" * 512
        yield b'"}'

    streamed = await client.post(event_path(alpha.token), content=_stream(), headers={"X-Hermes-Event": "x"})
    assert streamed.status_code == 413
    assert await _sessions(db_session, alpha.receiver.user_id) == []
    assert await _rows(db_session, HermesDelivery) == []


async def test_the_default_limit_takes_a_large_tool_result(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    """A post_tool_call carries the raw tool output (up to ~50 KB in Hermes); it is read and dropped."""
    body = payload("post_tool_call", "sess-large", tool_name="terminal", extra={"result": "z" * 120_000})
    assert (await post_event(client, alpha.token, body, alpha.secret)).json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Other accounts
# ---------------------------------------------------------------------------


async def test_an_event_naming_another_accounts_session_never_attaches_to_it(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    other = await _account(db_session, "hrev_other")
    theirs = AgentSession(
        user_id=other.id,
        client_type="webhook",
        client_name="hermes-agent",
        external_session_id="shared-sess",
        started_at=utcnow(),
        last_activity_at=utcnow(),
    )
    db_session.add(theirs)
    await db_session.commit()

    tool = payload("post_tool_call", "shared-sess", tool_name="terminal")
    resp = await post_event(client, alpha.token, tool, alpha.secret)
    spawn = payload("subagent_start", "shared-sess", extra={"child_session_id": "child-of-shared"})
    await post_event(client, alpha.token, spawn, alpha.secret)
    end = payload("on_session_end", "shared-sess", extra={"completed": True})
    await post_event(client, alpha.token, end, alpha.secret)

    assert resp.json()["agent_session_id"] != theirs.id
    [their_row] = await _sessions(db_session, other.id)
    assert their_row.tool_call_count == 0
    assert their_row.ended_at is None
    their_audit = await _rows(db_session, SecurityAuditLog, SecurityAuditLog.agent_session_id == theirs.id)
    assert their_audit == []
    ours = await _sessions(db_session, alpha.receiver.user_id)
    assert {s.external_session_id for s in ours} == {"shared-sess", "child-of-shared"}
    child = next(s for s in ours if s.external_session_id == "child-of-shared")
    assert child.parent_session_id != theirs.id
    assert child.parent_session_id == next(s.id for s in ours if s.external_session_id == "shared-sess")


async def test_a_subagent_never_links_to_another_accounts_parent(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    other = await _account(db_session, "hrev_parent")
    theirs = AgentSession(
        user_id=other.id,
        client_type="webhook",
        external_session_id="their-parent",
        started_at=utcnow(),
        last_activity_at=utcnow(),
    )
    db_session.add(theirs)
    await db_session.commit()

    # The child's own first event, naming its parent through the subagent_start of "their-parent".
    spawn = payload("subagent_start", "their-parent", extra={"child_session_id": "my-child"})
    await post_event(client, alpha.token, spawn, alpha.secret)

    for session in await _sessions(db_session, alpha.receiver.user_id):
        assert session.parent_session_id != theirs.id


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


async def test_a_tool_call_keeps_metadata_only(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    body = payload(
        "post_tool_call",
        "sess-tool",
        tool_name="terminal",
        tool_input={"command": f"echo {CANARY}"},
        extra={
            "result": f'{{"output": "{CANARY}"}}',
            "error_message": CANARY,
            "status": "ok",
            "error_type": None,
            "duration_ms": 42,
            "tool_call_id": "call-1",
            "turn_id": "turn-1",
            "task_id": "task-1",
            "middleware_trace": [CANARY],
        },
    )
    resp = await post_event(client, alpha.token, body, alpha.secret)
    assert resp.json()["status"] == "accepted"

    [row] = await _audit(db_session, "agent_tool_called")
    assert row.details["tool"] == "terminal"
    assert row.details["status"] == "ok"
    assert row.details["duration_ms"] == 42
    assert row.details["tool_call_id"] == "call-1"
    assert row.details["turn_id"] == "turn-1"
    assert row.details["event"] == "post_tool_call"
    [session] = await _sessions(db_session, alpha.receiver.user_id)
    assert session.tool_call_count == 1
    assert session.last_activity_at is not None

    traces = await db_session.execute(
        text(
            "SELECT count(*) FROM security_audit_logs WHERE details::text LIKE :c "
            "UNION ALL SELECT count(*) FROM agent_sessions WHERE row_to_json(agent_sessions)::text LIKE :c "
            "UNION ALL SELECT count(*) FROM hermes_deliveries WHERE delivery_id LIKE :c "
            "UNION ALL SELECT count(*) FROM hermes_receivers WHERE row_to_json(hermes_receivers)::text LIKE :c"
        ),
        {"c": f"%{CANARY}%"},
    )
    assert [n for (n,) in traces] == [0, 0, 0, 0]


async def test_a_hermes_tool_named_like_a_project_free_mcp_tool_is_prefixed(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    await post_event(client, alpha.token, payload("post_tool_call", "sess-who", tool_name="whoami"), alpha.secret)
    [row] = await _audit(db_session, "agent_tool_called")
    assert row.details["tool"] == "hermes:whoami"


async def test_approvals_keep_pattern_keys_but_never_the_command(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    common = {
        "command": f"rm -rf {CANARY}",
        "description": CANARY,
        "pattern_key": "recursive_delete",
        "pattern_keys": ["recursive_delete"],
        "surface": "cli",
        "session_key": "cli-1",
    }
    await post_event(client, alpha.token, payload("pre_approval_request", "sess-appr", extra=common), alpha.secret)
    answer = {**common, "choice": "once", "decided_by": "user"}
    await post_event(client, alpha.token, payload("post_approval_response", "sess-appr", extra=answer), alpha.secret)

    [asked] = await _audit(db_session, "agent_approval_requested")
    [resolved] = await _audit(db_session, "agent_approval_resolved")
    assert asked.details["pattern_keys"] == ["recursive_delete"]
    assert asked.details["surface"] == "cli"
    assert resolved.details["choice"] == "once"
    assert resolved.details["decided_by"] == "user"
    assert CANARY not in str(asked.details) + str(resolved.details)
    assert asked.agent_session_id == resolved.agent_session_id


async def test_subagents_link_to_their_parent_and_stop(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    await post_event(client, alpha.token, payload("on_session_start", "parent-1"), alpha.secret)
    start = payload(
        "subagent_start",
        "parent-1",
        extra={
            "parent_turn_id": "t1",
            "child_session_id": "child-1",
            "child_subagent_id": "sa-1",
            "child_role": "researcher",
            "child_goal": CANARY,
        },
    )
    started = await post_event(client, alpha.token, start, alpha.secret)
    child_call = payload("post_tool_call", "child-1", tool_name="read_file")
    await post_event(client, alpha.token, child_call, alpha.secret)
    stop = payload(
        "subagent_stop",
        "parent-1",
        extra={
            "child_session_id": "child-1",
            "child_role": "researcher",
            "child_summary": CANARY,
            "child_status": "completed",
            "tool_call_history": [
                {"tool_name": "read_file", "tool_input": {"targets": {"path": CANARY}}, "status": "ok"},
                {"tool_name": "write_file", "status": "ok"},
            ],
            "duration_ms": 1234,
        },
    )
    await post_event(client, alpha.token, stop, alpha.secret)

    sessions = {s.external_session_id: s for s in await _sessions(db_session, alpha.receiver.user_id)}
    parent, child = sessions["parent-1"], sessions["child-1"]
    assert len(sessions) == 2
    assert child.parent_session_id == parent.id
    assert child.parent_external_session_id == "parent-1"
    assert child.tool_call_count == 1
    assert child.ended_at is not None
    assert parent.ended_at is None
    assert started.json()["agent_session_id"] == parent.id
    [spawned] = await _audit(db_session, "agent_subagent_started")
    [stopped] = await _audit(db_session, "agent_subagent_stopped")
    assert spawned.agent_session_id == parent.id
    assert spawned.details["child_agent_session_id"] == child.id
    assert spawned.details["child_role"] == "researcher"
    assert stopped.details["tools"] == ["read_file", "write_file"]
    assert stopped.details["child_status"] == "completed"
    assert CANARY not in str(spawned.details) + str(stopped.details)


async def test_session_end_sets_ended_at_and_a_later_event_opens_a_new_session(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    await post_event(client, alpha.token, payload("on_session_start", "sess-end"), alpha.secret)
    end = payload("on_session_end", "sess-end", extra={"completed": True, "interrupted": False, "failed": False})
    ended = await post_event(client, alpha.token, end, alpha.secret)

    [session] = await _sessions(db_session, alpha.receiver.user_id)
    assert ended.json()["agent_session_id"] == session.id
    assert session.ended_at is not None
    [row] = await _audit(db_session, "agent_session_ended")
    assert row.details["completed"] is True
    assert row.details["interrupted"] is False

    await post_event(client, alpha.token, payload("on_session_start", "sess-end"), alpha.secret)
    assert len(await _sessions(db_session, alpha.receiver.user_id)) == 2


@pytest.mark.parametrize("event", ["post_api_request", "pre_llm_call", "pre_tool_call", "on_session_reset"])
async def test_other_events_are_ignored_and_never_create_usage(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, event: str
) -> None:
    body = payload(
        event,
        "sess-ignored",
        extra={"model": "lab-model-1", "provider": "testlab", "usage": {"input_tokens": 2048, "output_tokens": 512}},
    )
    resp = await post_event(client, alpha.token, body, alpha.secret)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert await _sessions(db_session, alpha.receiver.user_id) == []
    assert await _rows(db_session, AiUsageEntry, AiUsageEntry.user_id == alpha.receiver.user_id) == []
    # Remembered all the same, so a redelivery is a duplicate.
    assert (await post_event(client, alpha.token, body, alpha.secret)).json() == {"status": "duplicate"}


@pytest.mark.parametrize("session_id", ["", "has spaces in it", "-leading-dash"])
async def test_an_event_without_a_usable_session_id_is_ignored(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, session_id: str
) -> None:
    resp = await post_event(client, alpha.token, payload("pre_approval_request", session_id), alpha.secret)
    assert resp.json() == {"status": "ignored"}
    assert await _sessions(db_session, alpha.receiver.user_id) == []


async def test_the_url_without_a_trailing_slash_works(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    resp = await post_event(client, alpha.token, payload("on_session_start", "sess-bare"), alpha.secret, slash=False)
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


async def test_get_answers_the_receiver_name_for_doctor(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver
) -> None:
    assert (await client.get(event_path(alpha.token))).json() == {"ok": True, "receiver": "build host"}
    assert (await client.get(event_path(alpha.token, slash=False))).status_code == 200
    assert (await client.get(event_path("y" * 43))).status_code == 404


async def test_the_secret_is_never_logged_nor_audited(
    client: AsyncClient, db_session: AsyncSession, alpha: MintedReceiver, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    await post_event(client, alpha.token, payload("on_session_start", "sess-log"), alpha.secret)
    await post_event(client, alpha.token, payload("on_session_start", "sess-log"), "wrong")
    await post_event(client, alpha.token, payload("on_session_start", "sess-log"), None)
    # The token is the URL path, so access logs carry it like any webhook URL; the secret never appears.
    assert alpha.secret not in caplog.text
    rows = await _rows(db_session, SecurityAuditLog)
    assert all(alpha.secret not in str(r.details) and alpha.token not in str(r.details) for r in rows)


async def test_old_delivery_ids_are_pruned(db_session: AsyncSession, alpha: MintedReceiver) -> None:
    now = utcnow()
    db_session.add_all(
        [
            HermesDelivery(receiver_id=alpha.receiver.id, delivery_id="old", received_at=now - timedelta(days=8)),
            HermesDelivery(receiver_id=alpha.receiver.id, delivery_id="new", received_at=now - timedelta(days=6)),
        ]
    )
    await db_session.flush()
    assert await purge_old_deliveries(db_session) == 1
    assert [d.delivery_id for d in await _rows(db_session, HermesDelivery)] == ["new"]


# ---------------------------------------------------------------------------
# The agent sessions page shows the events of a session on an issue
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_session_world(db_session, "HEV")
    clear_role_cache(db_session)


async def test_events_of_a_session_on_an_issue_show_on_its_project_page(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    """The MCP connection named the issue under Hermes' session id; its events join that session."""
    minted = await _receiver(db_session, world.agent, name="agent host")
    body = payload("post_tool_call", "hev-live", tool_name="terminal_hev", extra={"status": "ok"})
    resp = await post_event(client, minted.token, body, minted.secret)
    agent_session_id = resp.json()["agent_session_id"]

    session = await db_session.get(AgentSession, agent_session_id)
    await db_session.refresh(session)
    assert session.id == world.live.id  # one conversation, one session
    assert session.issue_id == world.pub.id
    [row] = await _audit(db_session, "agent_tool_called")
    assert row.project_id == world.p.id

    cookies = {"access_token": _make_access_token(world.manager, get_settings())}
    page = await client.get(f"/projects/{world.p.key}/agent-sessions/{agent_session_id}/", cookies=cookies)
    assert page.status_code == 200, page.text
    assert "terminal_hev" in page.text
    assert "agent_tool_called" in page.text
    listing = await client.get(f"/projects/{world.p.key}/agent-sessions/", cookies=cookies)
    assert f"/agent-sessions/{agent_session_id}/" in listing.text


async def test_events_of_a_session_on_no_issue_stay_off_project_timelines(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    """Project-less rows name no project; a project's timeline shows only its own (ADR-0004 section 18)."""
    minted = await _receiver(db_session, world.agent, name="agent host")
    body = payload("post_tool_call", "hev-nowhere", tool_name="terminal_nowhere")
    resp = await post_event(client, minted.token, body, minted.secret)
    [row] = await _audit(db_session, "agent_tool_called")
    assert row.project_id is None

    cookies = {"access_token": _make_access_token(world.manager, get_settings())}
    page = await client.get(
        f"/projects/{world.p.key}/agent-sessions/{resp.json()['agent_session_id']}/", cookies=cookies
    )
    assert page.status_code == 200, page.text
    assert "terminal_nowhere" not in page.text


async def test_events_joining_an_mcp_session_stop_naming_a_project_the_account_left(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    from sqlalchemy import delete

    from specivo.models.member import Member

    minted = await _receiver(db_session, world.agent, name="agent host")
    await db_session.execute(delete(Member).where(Member.user_id == world.agent.id, Member.project_id == world.p.id))
    await db_session.commit()
    clear_role_cache(db_session)

    resp = await post_event(client, minted.token, payload("post_tool_call", "hev-live", tool_name="x"), minted.secret)
    assert resp.json()["agent_session_id"] == world.live.id
    [row] = await _audit(db_session, "agent_tool_called")
    assert row.project_id is None
    # The run ending does not end the MCP session: the conversation goes on over it.
    await post_event(client, minted.token, payload("on_session_end", "hev-live"), minted.secret)
    await db_session.refresh(world.live)
    assert world.live.ended_at is None


async def test_a_session_does_not_attach_to_an_issue_its_account_can_no_longer_log_on(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    """The issue found through the Hermes session id is used only where the account may still log usage."""
    from sqlalchemy import delete

    from specivo.models.member import Member

    # The MCP session that named the issue has ended: the events open a webhook session of their own.
    world.live.ended_at = utcnow()
    await db_session.commit()
    minted = await _receiver(db_session, world.agent, name="agent host")
    first = await post_event(client, minted.token, payload("on_session_start", "hev-live"), minted.secret)
    attached = await db_session.get(AgentSession, first.json()["agent_session_id"])
    await db_session.refresh(attached)
    assert attached.issue_id == world.pub.id  # sanity: while a member, it attaches

    await db_session.execute(delete(Member).where(Member.user_id == world.agent.id, Member.project_id == world.p.id))
    await db_session.commit()
    clear_role_cache(db_session)

    # The open session stops naming the issue, and its rows name no project.
    call = payload("post_tool_call", "hev-live", tool_name="terminal_lost")
    resp = await post_event(client, minted.token, call, minted.secret)
    assert resp.json()["agent_session_id"] == attached.id
    await db_session.refresh(attached)
    assert attached.issue_id is None
    [row] = await _audit(db_session, "agent_tool_called")
    assert row.project_id is None

    # A later session with the same id does not attach either.
    await post_event(client, minted.token, payload("on_session_end", "hev-live"), minted.secret)
    later = await post_event(client, minted.token, payload("on_session_start", "hev-live"), minted.secret)
    assert later.json()["agent_session_id"] != attached.id
    fresh = await db_session.get(AgentSession, later.json()["agent_session_id"])
    await db_session.refresh(fresh)
    assert fresh.issue_id is None
    started = await _audit(db_session, "agent_session_started")
    assert all(r.project_id is None for r in started if r.agent_session_id == fresh.id)
