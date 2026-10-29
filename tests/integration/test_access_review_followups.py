"""Billing rates need a permission; counts, sums and widgets leave hidden things out.

The world is the one ``test_issue_visibility_listings`` builds: a public
project P holding a public and a private issue (both in a sprint and a
version) and a public and a private backlog issue, read by

- the manager, a member whose role grants everything and sees every issue;
- the reporter, a member who sees issues but not other people's private
  ones, and holds neither ``manage_project``, ``view_time_entries`` nor
  ``view_wiki``;
- the outsider, a member of another project only, reading P as a non-member;
- anonymous visitors, once the instance switch is on;
- the manager's key limited to another project S.

Changing P's billing rates takes ``manage_project`` and reading them
``view_time_entries``. Version progress, sprint burndown hours, agent cost
summaries and the dashboard's version, sprint and project counts cover only
the issues the reader may see. The dashboard shows wiki pages and wiki edits
only where the reader may read the wiki, and agent notes only when public.
The recurring pattern page links its issues by their key.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.agent_costs import (
    create_billing_rate,
    delete_billing_rate,
    get_cost_summary,
    list_billing_rates,
    update_billing_rate,
)
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.models.agent_cost import AgentTokenLog
from specivo.models.journal import Journal
from specivo.models.lookups import IssueStatus
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.schemas.agent_cost import BillingRateCreate, BillingRateUpdate
from specivo.services.agent_cost_service import AgentCostService
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.billing_rate_service import BillingRateService
from specivo.services.permission_service import clear_role_cache
from specivo.services.sprint_service import SprintService
from specivo.services.version_service import VersionService
from tests.factories.recurring_pattern import RecurringPatternFactory
from tests.integration.test_issue_visibility_listings import (
    World,
    _agent_session,
    _bearer,
    _cookie,
    build_world,
    go_live,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    clear_role_cache(db_session)
    w = await build_world(db_session, "ARF")
    yield w
    clear_role_cache(db_session)


async def _anonymous(db: AsyncSession, w: World) -> User:
    await go_live(db, w)
    anonymous = await get_anonymous_user(db)
    assert anonymous is not None
    return anonymous


# ---------------------------------------------------------------------------
# Billing rates
# ---------------------------------------------------------------------------
# The router is mounted by an edition plugin, not by core, so the route
# functions are called directly.


async def _rate(db: AsyncSession, w: World) -> int:
    rate = await BillingRateService().create_rate(
        db,
        w.p.id,
        BillingRateCreate(user_id=None, hourly_rate=Decimal("80"), currency="USD", effective_from=date(2026, 1, 1)),
    )
    await db.commit()
    return rate.id


async def _assert_rates_refused(db: AsyncSession, w: World, user: User, error: type[Exception]) -> None:
    rate_id = await _rate(db, w)
    new = BillingRateCreate(user_id=None, hourly_rate=Decimal("1"), currency="USD", effective_from=date(2026, 2, 1))
    with pytest.raises(error):
        await create_billing_rate(w.p.key, new, request=None, current_user=user, db=db)
    with pytest.raises(error):
        await update_billing_rate(
            w.p.key, rate_id, BillingRateUpdate(hourly_rate=Decimal("1")), request=None, current_user=user, db=db
        )
    with pytest.raises(error):
        await delete_billing_rate(w.p.key, rate_id, request=None, current_user=user, db=db)
    with pytest.raises(error):
        await list_billing_rates(w.p.key, request=None, current_user=user, db=db)
    rates = await BillingRateService().list_rates(db, w.p.id)
    assert [(r.id, r.hourly_rate) for r in rates] == [(rate_id, Decimal("80.00"))]


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_billing_rates_refuse_a_reader_without_the_permissions(
    db_session: AsyncSession, world: World, reader: str
) -> None:
    await _assert_rates_refused(db_session, world, getattr(world, reader), PermissionDeniedError)


async def test_billing_rates_refuse_an_anonymous_visitor(db_session: AsyncSession, world: World) -> None:
    anonymous = await _anonymous(db_session, world)
    await _assert_rates_refused(db_session, world, anonymous, PermissionDeniedError)


async def test_billing_rates_are_not_found_for_a_key_limited_to_another_project(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        await _assert_rates_refused(db_session, w, w.manager, NotFoundError)


async def test_a_project_manager_manages_billing_rates(db_session: AsyncSession, world: World) -> None:
    w = world
    m = w.manager
    new = BillingRateCreate(user_id=None, hourly_rate=Decimal("90"), currency="USD", effective_from=date(2026, 3, 1))
    created = await create_billing_rate(w.p.key, new, request=None, current_user=m, db=db_session)
    updated = await update_billing_rate(
        w.p.key, created.id, BillingRateUpdate(hourly_rate=Decimal("95")), request=None, current_user=m, db=db_session
    )
    assert updated.hourly_rate == Decimal("95")
    listed = await list_billing_rates(w.p.key, request=None, current_user=m, db=db_session)
    assert [r.id for r in listed] == [created.id]
    await delete_billing_rate(w.p.key, created.id, request=None, current_user=m, db=db_session)
    assert await BillingRateService().list_rates(db_session, w.p.id) == []


# ---------------------------------------------------------------------------
# Agent cost summary
# ---------------------------------------------------------------------------


async def _costs(db: AsyncSession, w: World) -> None:
    for issue, tokens in ((w.pub, 100), (w.priv, 7000)):
        session = await _agent_session(db, w.agent, issue)
        db.add(
            AgentTokenLog(
                session_id=session.id,
                model_name="claude-test",
                input_tokens=tokens,
                output_tokens=1,
                cost=Decimal(tokens) / 100,
            )
        )
    await db.commit()


async def _summary(db: AsyncSession, w: World, user: User) -> list[tuple[int, Decimal]]:
    items = await get_cost_summary(w.p.key, date_from=None, date_to=None, current_user=user, db=db)
    return [(i.total_input_tokens, i.total_cost) for i in items]


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_cost_summary_leaves_out_hidden_issues(db_session: AsyncSession, world: World, reader: str) -> None:
    await _costs(db_session, world)
    assert await _summary(db_session, world, getattr(world, reader)) == [(100, Decimal("1"))]


async def test_cost_summary_counts_everything_for_a_member_who_sees_all(db_session: AsyncSession, world: World) -> None:
    await _costs(db_session, world)
    assert await _summary(db_session, world, world.manager) == [(7100, Decimal("71"))]


async def test_cost_summary_follows_the_anonymous_rules_and_the_key_limit(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    await _costs(db_session, w)
    anonymous = await _anonymous(db_session, w)
    assert await _summary(db_session, w, anonymous) == [(100, Decimal("1"))]

    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        with pytest.raises(NotFoundError):
            await _summary(db_session, w, w.manager)
        assert await AgentCostService().get_cost_summary(db_session, w.p, w.manager) == []


# ---------------------------------------------------------------------------
# Version progress (roadmap and version page)
# ---------------------------------------------------------------------------


async def _roadmap_total(db: AsyncSession, w: World, user: User) -> int:
    entries = await VersionService().roadmap(db, w.p, user)
    entry = next(e for e in entries if e.version.id == w.version.id)
    assert entry.open_count + entry.closed_count == entry.total
    return entry.total


async def test_roadmap_counts_only_the_issues_the_reader_may_see(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    assert await _roadmap_total(db_session, w, w.reporter) == 1
    assert await _roadmap_total(db_session, w, w.outsider) == 1
    assert await _roadmap_total(db_session, w, w.manager) == 2

    for user, total in ((w.reporter, 1), (w.outsider, 1), (w.manager, 2)):
        resp = await client.get(f"/api/v1/projects/{w.p.key}/roadmap/", headers=await _bearer(client, user))
        assert resp.status_code == 200, resp.text
        assert [e["total"] for e in resp.json() if e["version"]["id"] == w.version.id] == [total]

    anonymous = await _anonymous(db_session, w)
    assert await _roadmap_total(db_session, w, anonymous) == 1
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        assert await _roadmap_total(db_session, w, w.manager) == 0


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_version_page_counts_only_the_issues_the_reader_may_see(
    client: AsyncClient, world: World, reader: str
) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/versions/{w.version.id}/", cookies=_cookie(getattr(w, reader)))
    assert resp.status_code == 200, resp.text
    page = " ".join(resp.text.split())
    assert w.priv.subject not in page
    assert "<strong>1</strong> <span>total</span>" in page
    assert "<strong>2</strong> <span>total</span>" not in page

    roadmap = await client.get(f"/projects/{w.p.key}/roadmap/", cookies=_cookie(getattr(w, reader)))
    assert roadmap.status_code == 200
    assert '<span class="count-open">1 open</span>' in roadmap.text
    assert '<span class="count-open">2 open</span>' not in roadmap.text


# ---------------------------------------------------------------------------
# Sprint burndown
# ---------------------------------------------------------------------------


async def _estimates(db: AsyncSession, w: World) -> None:
    w.pub.estimated_hours = Decimal("3")
    w.priv.estimated_hours = Decimal("5")
    await db.commit()


async def test_burndown_sums_only_the_issues_the_reader_may_see(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await _estimates(db_session, w)
    for user, hours in ((w.reporter, 3), (w.outsider, 3), (w.manager, 8)):
        resp = await client.get(
            f"/api/v1/projects/{w.p.key}/sprints/{w.sprint.id}/burndown/", headers=await _bearer(client, user)
        )
        assert resp.status_code == 200, resp.text
        assert Decimal(str(resp.json()["total_estimated_hours"])) == hours, user.login
        assert Decimal(str(resp.json()["completed_hours"])) == 0


async def test_burndown_follows_the_anonymous_rules_and_the_key_limit(db_session: AsyncSession, world: World) -> None:
    w = world
    await _estimates(db_session, w)
    done = IssueStatus(name="ARF Done", position=2, category="done")
    db_session.add(done)
    await db_session.flush()
    w.priv.status_id = done.id
    await db_session.commit()

    svc = SprintService()
    anonymous = await _anonymous(db_session, w)
    data = await svc.burndown_data(db_session, w.sprint, anonymous)
    assert (data["total_estimated_hours"], data["completed_hours"]) == (3, 0)
    data = await svc.burndown_data(db_session, w.sprint, w.manager)
    assert (data["total_estimated_hours"], data["completed_hours"]) == (8, 5)
    with bound_scope(ProjectScope(frozenset({w.s.id}))):
        data = await svc.burndown_data(db_session, w.sprint, w.manager)
        assert (data["total_estimated_hours"], data["completed_hours"]) == (0, 0)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _open_count(page: str, key: str) -> int:
    match = re.search(rf'<span class="key">{key}</span>.*?<span class="count">(\d+) open</span>', page, re.S)
    assert match is not None, key
    return int(match.group(1))


@pytest.mark.parametrize("reader", ["reporter", "outsider"])
async def test_dashboard_counts_only_the_issues_the_reader_may_see(
    client: AsyncClient, world: World, reader: str
) -> None:
    w = world
    resp = await client.get("/", cookies=_cookie(getattr(w, reader)))
    assert resp.status_code == 200
    text = " ".join(resp.text.split())
    # The sprint and the version each hold one visible and one hidden issue.
    assert "0 of 1 issues closed" in text
    assert "0 of 2 issues closed" not in text
    # P holds two visible and two hidden open issues.
    assert _open_count(resp.text, w.p.key) == 2


async def test_dashboard_counts_everything_for_a_member_who_sees_all(client: AsyncClient, world: World) -> None:
    w = world
    resp = await client.get("/", cookies=_cookie(w.manager))
    text = " ".join(resp.text.split())
    assert "0 of 2 issues closed" in text
    assert _open_count(resp.text, w.p.key) == 4


async def _wiki_edit_and_private_note(db: AsyncSession, w: World) -> None:
    page_id = (await db.execute(select(WikiPage.id).where(WikiPage.slug == w.wiki_slug))).scalar_one()
    db.add(Journal(wiki_page_id=page_id, project_id=w.p.id, user_id=w.agent.id, notes="agent wiki edit", sequence=1))
    seq = (
        await db.execute(select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == w.pub.id))
    ).scalar_one()
    db.add(
        Journal(
            issue_id=w.pub.id,
            project_id=w.p.id,
            user_id=w.agent.id,
            notes="private agent remark",
            is_private=True,
            sequence=seq + 1,
        )
    )
    await db.commit()


async def test_dashboard_shows_wiki_entries_only_to_readers_of_the_wiki(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    """The reporter may see P's issues but not its wiki: no page title, no wiki edit."""
    w = world
    await _wiki_edit_and_private_note(db_session, w)

    resp = await client.get("/", cookies=_cookie(w.reporter))
    assert resp.status_code == 200
    assert "Marmot notes" not in resp.text

    resp = await client.get("/", cookies=_cookie(w.manager))
    assert resp.text.count("Marmot notes") >= 2  # recent pages and agent activity


async def test_dashboard_agent_activity_leaves_out_private_notes(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    await _wiki_edit_and_private_note(db_session, w)
    for reader in (w.reporter, w.manager):
        resp = await client.get("/", cookies=_cookie(reader))
        assert "private agent remark" not in resp.text, reader.login
    resp = await client.get("/", cookies=_cookie(w.admin))
    assert "private agent remark" in resp.text


# ---------------------------------------------------------------------------
# Recurring pattern instances
# ---------------------------------------------------------------------------


async def test_recurring_pattern_page_links_instances_by_their_key(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    w = world
    pattern = RecurringPatternFactory.build(
        project_id=w.p.id,
        author_id=w.manager.id,
        template_tracker_id=w.pub.tracker_id,
        freq="daily",
        dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    db_session.add(pattern)
    await db_session.flush()
    w.pub.recurring_pattern_id = pattern.id
    w.pub.original_occurrence_at = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    await db_session.commit()

    resp = await client.get(f"/projects/{w.p.key}/recurring-patterns/{pattern.id}/", cookies=_cookie(w.manager))
    assert resp.status_code == 200
    assert f'href="/issue/{w.pub.display_key}/"' in resp.text
    assert f"/issues/{w.pub.id}/" not in resp.text

    target = await client.get(f"/issue/{w.pub.display_key}/", cookies=_cookie(w.manager))
    assert target.status_code == 200
