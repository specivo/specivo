"""AI usage administration: the recompute route and task, the offer on the price page, and the role editor."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.money import Money
from specivo.models.ai_model_price import AiModelPrice
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService
from specivo.services.auth_service import _make_access_token
from specivo.services.security_audit_service import AuditEvent
from specivo.tasks import ai_usage as ai_usage_tasks
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_usage = AiUsageService()
_prices = AiModelPriceService()


class _Queue:
    """Stands in for the Celery task's ``apply_async``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def __call__(self, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.calls.append(kwargs)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    fake = _Queue()
    monkeypatch.setattr(ai_usage_tasks.recompute_ai_usage_costs, "apply_async", fake)
    return fake


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user: User = AdminUserFactory.build(login="aiua_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def alice(db_session: AsyncSession) -> User:
    user: User = UserFactory.build(login="aiua_alice", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _post_form(client: AsyncClient, user: User, url: str, data: Mapping[str, object], page: str):
    resp = await client.get(page, cookies=_cookies(user))
    csrf = next(
        value.split("=", 1)[1].split(";")[0].strip()
        for key, value in resp.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith("csrf_token=")
    )
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


async def _unpriced_entries(db: AsyncSession, alice: User) -> list[AiUsageEntry]:
    entries = [
        await _usage.log(
            db,
            alice,
            AiUsageInput(
                tool="codex",
                provider="testlab",
                model="lab-late",
                usage_date=day,
                tokens={"input": 1_000_000},
                cost_type="api",
            ),
        )
        for day in (date(2026, 2, 10), date(2026, 3, 10))
    ]
    await db.commit()
    return entries


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_rest_recompute_queues_the_task(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, queue: _Queue
) -> None:
    await _unpriced_entries(db_session, alice)
    resp = await client.post(
        "/api/v1/admin/ai-usage/recompute/",
        json={"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z", "provider": "testlab", "model": "lab-late"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["entries"] == 1
    assert queue.calls[0]["task_id"] == body["task_id"]
    assert queue.calls[0]["args"] == [
        admin.id,
        "2026-01-01T00:00:00+00:00",
        "2026-03-01T00:00:00+00:00",
        "testlab",
        "lab-late",
    ]


async def test_rest_recompute_is_for_administrators(client: AsyncClient, alice: User, queue: _Queue) -> None:
    resp = await client.post(
        "/api/v1/admin/ai-usage/recompute/",
        json={"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"},
        headers=_bearer(alice),
    )
    assert resp.status_code == 403
    assert queue.calls == []


async def test_rest_recompute_validates_the_range(client: AsyncClient, admin: User, queue: _Queue) -> None:
    resp = await client.post(
        "/api/v1/admin/ai-usage/recompute/",
        json={"from": "2026-03-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 422
    naive = await client.post(
        "/api/v1/admin/ai-usage/recompute/",
        json={"from": "2026-01-01T00:00:00", "to": "2026-03-01T00:00:00"},
        headers=_bearer(admin),
    )
    assert naive.status_code == 422
    assert queue.calls == []


async def test_rest_recompute_reports_a_missing_queue(
    client: AsyncClient, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_usage_tasks.recompute_ai_usage_costs, "apply_async", _Queue(fail=True))
    resp = await client.post(
        "/api/v1/admin/ai-usage/recompute/",
        json={"from": "2026-01-01T00:00:00Z", "to": "2026-03-01T00:00:00Z"},
        headers=_bearer(admin),
    )
    assert resp.status_code == 503
    assert resp.json()["errors"][0]["code"] == "queue_unavailable"


async def test_task_recomputes_and_audits(
    db_session: AsyncSession, admin: User, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = await _unpriced_entries(db_session, alice)
    await _prices.create(
        db_session,
        PriceInput(
            provider="testlab",
            model="lab-late",
            currency="USD",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            prices={"input": "2", "output": "8"},
        ),
        actor=admin,
    )
    await db_session.commit()

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("specivo.tasks._async.task_session", _session)
    await ai_usage_tasks.recompute_ai_usage_costs_async(
        admin.id, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00", "testlab", "lab-late"
    )
    for entry in entries:
        await db_session.refresh(entry)
        assert entry.api_equivalent == Money("2", "USD")
        assert entry.marginal == Money("2", "USD")
        assert entry.revision == 2
    events = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(
                    SecurityAuditLog.event_type == AuditEvent.AI_USAGE_COSTS_RECOMPUTED.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].details["before"]["api_equivalent"] == {}
    assert events[0].details["after"]["api_equivalent"] == {"USD": "4.00000000"}


# ---------------------------------------------------------------------------
# The price page offer
# ---------------------------------------------------------------------------


async def test_price_page_offers_to_recompute_a_past_period(
    client: AsyncClient, db_session: AsyncSession, admin: User, alice: User, queue: _Queue
) -> None:
    await _unpriced_entries(db_session, alice)
    resp = await _post_form(
        client,
        admin,
        "/admin/ai-model-prices/",
        {
            "provider": "testlab",
            "model": "lab-late",
            "currency": "USD",
            "effective_from": "2026-01-01T00:00",
            "effective_to": "2026-03-01T00:00",
            "input": "2",
            "output": "8",
            "return_to": "model",
        },
        "/admin/ai-model-prices/",
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    price = (await db_session.execute(select(AiModelPrice).where(AiModelPrice.model == "lab-late"))).scalar_one()
    assert f"recompute={price.id}" in location

    page = await client.get(location, cookies=_cookies(admin))
    assert page.status_code == 200
    assert 'data-testid="recompute-offer"' in page.text
    assert "2026-01-01 00:00 UTC" in page.text and "2026-03-01 00:00 UTC" in page.text

    queued = await _post_form(
        client,
        admin,
        "/admin/ai-model-prices/recompute/",
        {
            "provider": "testlab",
            "model": "lab-late",
            "from": "2026-01-01T00:00:00+00:00",
            "to": "2026-03-01T00:00:00+00:00",
        },
        location,
    )
    assert queued.status_code == 303
    assert "notice=recompute_queued" in queued.headers["location"]
    assert "entries=1" in queued.headers["location"]
    assert len(queue.calls) == 1
    done = await client.get(queued.headers["location"], cookies=_cookies(admin))
    assert "Recomputing the costs of 1 usage entry" in done.text


async def test_no_offer_for_a_future_period_or_without_entries(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    created = await _prices.create(
        db_session,
        PriceInput(
            provider="testlab",
            model="lab-future",
            currency="USD",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            prices={"input": "1", "output": "1"},
        ),
        actor=admin,
    )
    await db_session.commit()
    page = await client.get(
        f"/admin/ai-model-prices/model/?provider=testlab&model=lab-future&recompute={created.price.id}",
        cookies=_cookies(admin),
    )
    assert page.status_code == 200
    assert 'data-testid="recompute-offer"' not in page.text


# ---------------------------------------------------------------------------
# Role editor
# ---------------------------------------------------------------------------


async def test_role_editor_saves_ai_usage_visibility(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    role = Role(name="Aiua Lead", permissions=["view_ai_usage"], builtin=0, issues_visibility="default")
    db_session.add(role)
    await db_session.commit()
    assert role.ai_usage_visibility == "own"

    page = await client.get("/admin/roles/", cookies=_cookies(admin))
    assert f'id="ai_usage_visibility_{role.id}"' in page.text

    resp = await _post_form(client, admin, f"/admin/roles/{role.id}/", {"ai_usage_visibility": "all"}, "/admin/roles/")
    assert resp.status_code == 303
    await db_session.refresh(role)
    assert role.ai_usage_visibility == "all"

    await _post_form(client, admin, f"/admin/roles/{role.id}/", {"ai_usage_visibility": "everyone"}, "/admin/roles/")
    await db_session.refresh(role)
    assert role.ai_usage_visibility == "all"
