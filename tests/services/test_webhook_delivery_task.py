"""One attempt at a webhook delivery: signing, outcomes, retries and the delivery-time address check."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.constants import WEBHOOK_MAX_ATTEMPTS, WEBHOOK_RESPONSE_MAX_BYTES
from specivo.models.webhook import WebhookDelivery
from specivo.schemas.webhook import WebhookEvent
from specivo.services.webhook_delivery import deliver
from specivo.services.webhook_events import emit_to_webhook
from specivo.services.webhook_service import WebhookService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_SECRET = "delivery-test-secret-0123456789"
_PUBLIC_IP = "93.184.216.34"
_HOST = "hooks.example.com"


async def _public(_host: str, _port: int) -> list[str]:
    return [_PUBLIC_IP]


def _no_jitter() -> float:
    return 0.0


@pytest_asyncio.fixture
async def setup(db_session: AsyncSession):
    project = ProjectFactory.build(key="WHDLV", name="Webhook Delivery", identifier="whdlv")
    admin = AdminUserFactory.build(login="whdlv_admin", status="active")
    db_session.add_all([project, admin])
    await db_session.commit()
    webhook, _ = await WebhookService().register(
        db_session, project.id, f"https://{_HOST}/in?x=1", events=["issue.created"], secret=_SECRET
    )
    await db_session.commit()
    return webhook, admin


async def _pending(db_session: AsyncSession, webhook, admin) -> WebhookDelivery:
    with patch("specivo.tasks.webhooks.deliver_webhook.apply_async"):
        row = await emit_to_webhook(db_session, webhook, WebhookEvent.PING, admin, {"hello": "world"})
        await db_session.commit()
    return row


def _transport(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen), len(responses)) - 1]

    return httpx.MockTransport(handler)


async def _attempt(db_session, row, transport, attempts=None, resolver=_public):
    return await deliver(
        db_session,
        row.id,
        row.attempts if attempts is None else attempts,
        transport=transport,
        resolver=resolver,
        rand=_no_jitter,
    )


async def test_2xx_succeeds_with_signed_request(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []

    outcome = await _attempt(db_session, row, _transport([httpx.Response(200, text="ok")], seen))

    assert outcome is not None and outcome.status == "succeeded"
    await db_session.refresh(row)
    assert (row.status, row.attempts, row.status_code, row.response_body) == ("succeeded", 1, 200, "ok")
    assert row.next_attempt_at is None

    (request,) = seen
    # Connected to the checked address; the name stays in Host and is used for TLS.
    assert request.url.host == _PUBLIC_IP
    assert request.url.path == "/in" and request.url.query == b"x=1"
    assert request.headers["host"] == _HOST
    assert request.extensions["sni_hostname"] == _HOST
    assert request.headers["x-specivo-event"] == "ping"
    assert request.headers["x-specivo-delivery"] == str(row.delivery_uuid)
    expected = hmac.new(_SECRET.encode(), request.content, hashlib.sha256).hexdigest()
    assert request.headers["x-specivo-signature"] == f"sha256={expected}"
    assert json.loads(request.content)["delivery_id"] == str(row.delivery_uuid)


async def test_5xx_retries_with_the_same_delivery_uuid(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(503, text="busy"), httpx.Response(204)], seen)

    first = await _attempt(db_session, row, transport)
    assert first is not None and first.status == "pending"
    assert first.retry_in == 30
    await db_session.refresh(row)
    assert (row.status, row.attempts, row.status_code, row.response_body) == ("pending", 1, 503, "busy")
    assert row.last_error and "503" in row.last_error
    assert row.next_attempt_at is not None

    second = await _attempt(db_session, row, transport)
    assert second is not None and second.status == "succeeded"
    assert len(seen) == 2
    assert seen[0].headers["x-specivo-delivery"] == seen[1].headers["x-specivo-delivery"] == str(row.delivery_uuid)


async def test_stale_attempt_number_does_nothing(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(500)], seen)
    await _attempt(db_session, row, transport)

    # A duplicate of the first task (attempts=0) arrives after the first attempt.
    assert await _attempt(db_session, row, transport, attempts=0) is None
    assert len(seen) == 1


async def test_4xx_fails_without_retry(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []

    outcome = await _attempt(db_session, row, _transport([httpx.Response(404, text="nope")], seen))

    assert outcome is not None and outcome.status == "failed" and outcome.retry_in is None
    await db_session.refresh(row)
    assert (row.status, row.attempts, row.status_code) == ("failed", 1, 404)
    assert await _attempt(db_session, row, _transport([httpx.Response(200)], seen)) is None
    assert len(seen) == 1


async def test_429_is_retried(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    outcome = await _attempt(db_session, row, _transport([httpx.Response(429)], []))
    assert outcome is not None and outcome.status == "pending"


async def test_redirect_is_not_followed(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    redirect = httpx.Response(302, headers={"Location": "https://elsewhere.example.com/"})
    outcome = await _attempt(db_session, row, _transport([redirect], seen))
    assert outcome is not None and outcome.status == "failed"
    assert len(seen) == 1


async def test_connection_error_retries_then_fails_after_last_attempt(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(refuse)
    delays = []
    for _ in range(WEBHOOK_MAX_ATTEMPTS):
        outcome = await _attempt(db_session, row, transport)
        assert outcome is not None
        delays.append(outcome.retry_in)
        await db_session.refresh(row)
    assert row.status == "failed"
    assert row.attempts == WEBHOOK_MAX_ATTEMPTS
    assert row.status_code is None
    assert "ConnectError" in (row.last_error or "")
    assert delays[-1] is None
    assert delays[:3] == [30, 60, 120]


async def test_loopback_at_delivery_time_fails_without_a_request(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    loopback = str(ipaddress.ip_network("127.0.0.0/8")[1])

    async def rebound(_host: str, _port: int) -> list[str]:
        return [loopback]

    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], seen), resolver=rebound)

    assert outcome is not None and outcome.status == "failed"
    assert seen == []
    await db_session.refresh(row)
    assert row.status_code is None
    assert "private" in (row.last_error or "")


async def test_private_range_among_records_fails_without_a_request(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    private = str(ipaddress.IPv4Network((10 << 24, 8))[7])

    async def mixed(_host: str, _port: int) -> list[str]:
        return [_PUBLIC_IP, private]

    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], seen), resolver=mixed)
    assert outcome is not None and outcome.status == "failed"
    assert seen == []


async def test_unresolvable_host_is_retried(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)

    async def nxdomain(_host: str, _port: int) -> list[str]:
        raise OSError("Name or service not known")

    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], []), resolver=nxdomain)
    assert outcome is not None and outcome.status == "pending"


async def test_response_excerpt_is_capped(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    await _attempt(db_session, row, _transport([httpx.Response(200, text="x" * (WEBHOOK_RESPONSE_MAX_BYTES * 3))], []))
    await db_session.refresh(row)
    assert len(row.response_body or "") == WEBHOOK_RESPONSE_MAX_BYTES


async def test_disabled_webhook_still_gets_ping_but_not_events(db_session, setup):
    webhook, admin = setup
    webhook.is_active = False
    await db_session.commit()

    ping = await _pending(db_session, webhook, admin)
    outcome = await _attempt(db_session, ping, _transport([httpx.Response(200)], []))
    assert outcome is not None and outcome.status == "succeeded"

    with patch("specivo.tasks.webhooks.deliver_webhook.apply_async"):
        row = await emit_to_webhook(db_session, webhook, WebhookEvent.ISSUE_CREATED, admin, {})
        await db_session.commit()
    seen: list[httpx.Request] = []
    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], seen))
    assert outcome is not None and outcome.status == "failed"
    assert seen == []


async def test_unreadable_secret_fails(db_session, setup):
    webhook, admin = setup
    webhook.secret = secret_box.encrypt(_SECRET, "webhook-secret", secret_key="x" * 64)
    await db_session.commit()
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], seen))
    assert outcome is not None and outcome.status == "failed"
    assert seen == []
    await db_session.refresh(row)
    assert "secret" in (row.last_error or "")


def _closing(result):
    """A stand-in for run_async that closes the coroutine it is given and returns *result*."""

    def run(coro):
        coro.close()
        return result

    return run


async def test_task_queues_retry_with_backoff(db_session, setup):
    """The Celery task wrapper re-queues a pending outcome with the attempt count and delay."""
    from specivo.services.webhook_delivery import DeliveryOutcome
    from specivo.tasks import webhooks as tasks

    with (
        patch.object(
            tasks, "run_async", side_effect=_closing(DeliveryOutcome(status="pending", attempts=2, retry_in=61.5))
        ),
        patch.object(tasks.deliver_webhook, "apply_async") as apply_async,
    ):
        tasks.deliver_webhook.run(42, 1)
    apply_async.assert_called_once_with(args=[42, 2], countdown=61.5, retry=False)


# ---------------------------------------------------------------------------
# Attempts that would otherwise raise: each must count and settle the row
# ---------------------------------------------------------------------------


async def test_nul_bytes_in_the_response_are_dropped(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    outcome = await _attempt(db_session, row, _transport([httpx.Response(200, content=b"ok\x00done")], []))
    assert outcome is not None and outcome.status == "succeeded"
    await db_session.refresh(row)
    assert row.response_body == "okdone"


async def test_invalid_port_fails_without_a_request(db_session, setup):
    webhook, admin = setup
    webhook.url = f"https://{_HOST}:99999/in"  # stored before the save-time check existed
    await db_session.commit()
    row = await _pending(db_session, webhook, admin)
    seen: list[httpx.Request] = []
    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], seen))
    assert outcome is not None and outcome.status == "failed" and outcome.attempts == 1
    assert seen == []


async def test_host_name_too_long_to_encode_is_unresolvable(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)

    async def bad_label(_host: str, _port: int) -> list[str]:
        raise UnicodeError("label too long")

    outcome = await _attempt(db_session, row, _transport([httpx.Response(200)], []), resolver=bad_label)
    assert outcome is not None and outcome.attempts == 1 and outcome.retry_in is not None


async def test_unexpected_error_counts_and_fails(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)

    def boom(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected")

    outcome = await _attempt(db_session, row, httpx.MockTransport(boom))
    assert outcome is not None and outcome.status == "failed" and outcome.attempts == 1
    await db_session.refresh(row)
    assert row.last_error == "Delivery failed: RuntimeError."


async def test_next_address_is_tried_when_the_first_refuses(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)
    second_ip = "93.184.216.35"

    async def two(_host: str, _port: int) -> list[str]:
        return [_PUBLIC_IP, second_ip]

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == _PUBLIC_IP:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200)

    outcome = await _attempt(db_session, row, httpx.MockTransport(handler), resolver=two)
    assert outcome is not None and outcome.status == "succeeded"
    assert seen == [_PUBLIC_IP, second_ip]


async def test_slow_receiver_hits_the_attempt_deadline(db_session, setup):
    webhook, admin = setup
    row = await _pending(db_session, webhook, admin)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    with patch("specivo.services.webhook_delivery.WEBHOOK_ATTEMPT_DEADLINE_SECONDS", 0.05):
        outcome = await _attempt(db_session, row, httpx.MockTransport(slow))
    assert outcome is not None and outcome.retry_in is not None and outcome.attempts == 1
    await db_session.refresh(row)
    assert row.last_error is not None and row.last_error.startswith("Timed out")
