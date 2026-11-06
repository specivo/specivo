"""The AI usage web pages: the project tab, the issue tab, My AI usage, the entry form and the row actions.

The world of the REST tests (``test_ai_usage_api.build_world``) with its own
prefix: private projects ``AUWA`` (a public and a private issue) and ``AUWB``;
``mgr`` manages both, ``alice`` / ``bob`` log their own usage in AUWA,
``runner`` logs for others there, ``carol`` sees everyone's usage in AUWA but
not the private issue, ``dave`` is a member without AI usage permissions.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.money import Money
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService
from specivo.services.auth_service import _make_access_token
from tests.integration.test_ai_usage_api import World, build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DAY = date(2026, 3, 10)
_service = AiUsageService()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session, "auw")


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _get(client: AsyncClient, user: User, url: str) -> Response:
    return await client.get(url, cookies=_cookies(user), follow_redirects=False)


async def _csrf(client: AsyncClient, user: User, page: str) -> str:
    resp = await client.get(page, cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError(f"no csrf_token cookie on {page}")


async def _post(
    client: AsyncClient, user: User, url: str, data: Mapping[str, Any], page: str = "/my/ai-usage/"
) -> Response:
    csrf = await _csrf(client, user, page)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


async def _log(
    db: AsyncSession,
    w: World,
    actor: str,
    *,
    target: str | None = None,
    project: str | None = "a",
    issue_id: int | None = None,
    comments: str | None = None,
    reported: bool = False,
    key: str | None = None,
    **kw: Any,
) -> AiUsageEntry:
    """Write one entry through the service (1M input and 100k output tokens of lab-model-1: 3 USD)."""
    data = AiUsageInput(
        tool="claude-code",
        provider="testlab",
        model="lab-model-1",
        usage_date=DAY,
        tokens={"input": 1_000_000, "output": 100_000},
        cost_type=kw.pop("cost_type", "api"),
        project_id=getattr(w, project).id if project else None,
        issue_id=issue_id,
        comments=comments,
        **kw,
    )
    target_user = w.users[target] if target else None
    key_id = w.key_ids[key] if key else None
    if reported:
        data.idempotency_key = f"bucket-{actor}-{target}-{issue_id}-{comments}"
        result = await _service.upsert(
            db, w.users[actor], data, target=target_user, source="reporter", api_key_id=key_id
        )
        entry = result.entry
    else:
        entry = await _service.log(
            db, w.users[actor], data, target=target_user, source="api" if key_id else "manual", api_key_id=key_id
        )
    await db.commit()
    return entry


async def _entry(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    stmt = select(AiUsageEntry).where(AiUsageEntry.id == entry_id).execution_options(populate_existing=True)
    found = (await db.execute(stmt)).scalar_one()
    return found


def _form(**kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tool": "claude-code",
        "provider": "testlab",
        "model": "lab-model-1",
        "usage_date": DAY.isoformat(),
        "tokens_input": "1000000",
        "tokens_output": "100000",
        "cost_type": "api",
        "comments": "",
    }
    values.update(kw)
    return values


# ---------------------------------------------------------------------------
# Project tab
# ---------------------------------------------------------------------------


async def test_project_tab_lists_what_the_reader_may_see(client: AsyncClient, db_session: AsyncSession, w: World):
    own = await _log(db_session, w, "alice", comments="alice public", issue_id=w.public_issue.id)
    hidden = await _log(db_session, w, "mgr", target="bob", comments="bob private", issue_id=w.private_issue.id)
    other = await _log(db_session, w, "bob", comments="bob plain")

    resp = await _get(client, w.users["mgr"], "/projects/AUWA/ai-usage/")
    assert resp.status_code == 200
    for entry in (own, hidden, other):
        assert f'data-entry-id="{entry.id}"' in resp.text
    assert 'data-testid="aiu-log-link"' in resp.text
    assert "$9.00" in resp.text  # three entries of 3 USD

    resp = await _get(client, w.users["carol"], "/projects/AUWA/ai-usage/")
    assert resp.status_code == 200
    assert f'data-entry-id="{own.id}"' in resp.text and f'data-entry-id="{other.id}"' in resp.text
    assert f'data-entry-id="{hidden.id}"' not in resp.text and "bob private" not in resp.text
    assert 'data-testid="aiu-log-link"' not in resp.text  # carol may not log
    assert 'data-testid="aiu-edit"' not in resp.text and 'data-testid="aiu-exclude"' not in resp.text
    assert "$6.00" in resp.text

    resp = await _get(client, w.users["alice"], "/projects/AUWA/ai-usage/")
    assert f'data-entry-id="{own.id}"' in resp.text and f'data-entry-id="{other.id}"' not in resp.text
    assert 'data-testid="aiu-edit"' in resp.text and 'data-testid="aiu-exclude"' in resp.text
    assert 'name="user_id"' not in resp.text  # own visibility: no user filter

    resp = await _get(client, w.users["dave"], "/projects/AUWA/ai-usage/")
    assert resp.status_code == 200 and 'data-testid="aiu-empty"' in resp.text

    resp = await _get(client, w.users["outsider"], "/projects/AUWA/ai-usage/")
    assert resp.status_code in (403, 404)


async def test_project_tab_requires_sign_in(client: AsyncClient, w: World):
    resp = await client.get("/projects/AUWA/ai-usage/", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"].startswith("/login/")


async def test_project_tab_filters(client: AsyncClient, db_session: AsyncSession, w: World):
    a = await _log(db_session, w, "alice", comments="first", issue_id=w.public_issue.id)
    b = await _log(db_session, w, "bob", comments="second", cost_type="free")
    mgr = w.users["mgr"]

    resp = await _get(client, mgr, f"/projects/AUWA/ai-usage/?user_id={w.users['bob'].id}")
    assert f'data-entry-id="{b.id}"' in resp.text and f'data-entry-id="{a.id}"' not in resp.text
    resp = await _get(client, mgr, "/projects/AUWA/ai-usage/?cost_type=free")
    assert f'data-entry-id="{b.id}"' in resp.text and f'data-entry-id="{a.id}"' not in resp.text
    resp = await _get(client, mgr, "/projects/AUWA/ai-usage/?issue=AUWA-1")
    assert f'data-entry-id="{a.id}"' in resp.text and f'data-entry-id="{b.id}"' not in resp.text
    resp = await _get(client, mgr, "/projects/AUWA/ai-usage/?from_date=2026-03-11")
    assert 'data-testid="aiu-empty"' in resp.text
    resp = await _get(client, mgr, "/projects/AUWA/ai-usage/?tool=claude-code&model=lab-model-1")
    assert f'data-entry-id="{a.id}"' in resp.text and f'data-entry-id="{b.id}"' in resp.text

    # An issue the reader cannot see filters to nothing, without naming it.
    await _log(db_session, w, "mgr", target="bob", comments="secret-note", issue_id=w.private_issue.id)
    resp = await _get(client, w.users["carol"], "/projects/AUWA/ai-usage/?issue=AUWA-2")
    assert "secret-note" not in resp.text and 'data-testid="aiu-empty"' in resp.text


async def test_logged_by_someone_else_via_a_key(client: AsyncClient, db_session: AsyncSession, w: World):
    entry = await _log(db_session, w, "runner", target="alice", key="runner", reported=True, comments="ci")
    resp = await _get(client, w.users["mgr"], "/projects/AUWA/ai-usage/")
    assert f'data-entry-id="{entry.id}"' in resp.text
    assert f"logged by {w.users['runner'].display_name or 'auw_runner'} via key auw-runner" in resp.text

    resp = await _get(client, w.users["alice"], "/my/ai-usage/")
    assert "via key auw-runner" in resp.text


async def test_unconverted_amounts_are_listed_not_dropped(client: AsyncClient, db_session: AsyncSession, w: World):
    await _log(db_session, w, "alice", reported_cost=Money("4", "EUR"))
    await _log(db_session, w, "alice")
    resp = await _get(client, w.users["mgr"], "/projects/AUWA/ai-usage/")
    assert 'data-testid="aiu-unconverted"' in resp.text
    assert "€4.00" in resp.text and "no exchange rate" in resp.text


async def test_excluded_entries_leave_the_totals(client: AsyncClient, db_session: AsyncSession, w: World):
    kept = await _log(db_session, w, "alice")
    dropped = await _log(db_session, w, "alice")
    await _service.set_excluded(db_session, w.users["alice"], dropped, True)
    await db_session.commit()
    resp = await _get(client, w.users["alice"], "/projects/AUWA/ai-usage/")
    assert "$3.00" in resp.text and "1 excluded entry not counted" in resp.text
    resp = await _get(client, w.users["alice"], "/projects/AUWA/ai-usage/?status=excluded")
    assert f'data-entry-id="{dropped.id}"' in resp.text and f'data-entry-id="{kept.id}"' not in resp.text
    assert 'data-testid="aiu-include"' in resp.text


# ---------------------------------------------------------------------------
# Issue tab
# ---------------------------------------------------------------------------


async def test_issue_tab(client: AsyncClient, db_session: AsyncSession, w: World):
    own = await _log(db_session, w, "alice", issue_id=w.public_issue.id, comments="on issue")
    elsewhere = await _log(db_session, w, "alice", comments="no issue")

    page = await _get(client, w.users["alice"], "/issue/AUWA-1/")
    assert page.status_code == 200 and 'data-testid="ai-usage-tab"' in page.text
    resp = await _get(client, w.users["alice"], "/partials/issues/AUWA-1/ai-usage/")
    assert resp.status_code == 200
    assert f'data-entry-id="{own.id}"' in resp.text and f'data-entry-id="{elsewhere.id}"' not in resp.text
    assert 'data-testid="aiu-issue-log"' in resp.text and "issue=AUWA-1" in resp.text

    # Dave may neither view nor log AI usage: no tab.
    page = await _get(client, w.users["dave"], "/issue/AUWA-1/")
    assert page.status_code == 200 and 'data-testid="ai-usage-tab"' not in page.text

    # Carol may view but not log, and cannot see the private issue at all.
    resp = await _get(client, w.users["carol"], "/partials/issues/AUWA-1/ai-usage/")
    assert f'data-entry-id="{own.id}"' in resp.text and 'data-testid="aiu-issue-log"' not in resp.text
    resp = await _get(client, w.users["carol"], "/partials/issues/AUWA-2/ai-usage/")
    assert resp.status_code == 404


async def test_project_overview_links_the_tab(client: AsyncClient, w: World):
    resp = await _get(client, w.users["alice"], "/projects/AUWA/")
    assert 'data-testid="project-ai-usage-tab"' in resp.text
    resp = await _get(client, w.users["dave"], "/projects/AUWA/")
    assert 'data-testid="project-ai-usage-tab"' not in resp.text


# ---------------------------------------------------------------------------
# Manual entry form
# ---------------------------------------------------------------------------


async def test_log_from_the_project_form(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    page = await _get(client, alice, "/projects/AUWA/ai-usage/new/?issue=AUWA-1")
    assert page.status_code == 200
    assert 'value="AUWA-1"' in page.text and 'id="aiu-models"' in page.text and "lab-model-1" in page.text
    assert 'data-testid="aiu-form-behalf"' not in page.text  # alice may not log for others

    resp = await _post(
        client,
        alice,
        "/projects/AUWA/ai-usage/new/",
        _form(issue="AUWA-1", comments="Typed by hand", next="/issue/AUWA-1/#ai-usage"),
        page="/projects/AUWA/ai-usage/new/",
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/issue/AUWA-1/?notice=logged#ai-usage"
    entry = (
        await db_session.execute(select(AiUsageEntry).where(AiUsageEntry.comments == "Typed by hand"))
    ).scalar_one()
    assert (entry.user_id, entry.reporter_user_id, entry.source) == (alice.id, alice.id, "manual")
    assert (entry.project_id, entry.issue_id) == (w.a.id, w.public_issue.id)
    assert entry.api_equivalent == Money("3", "USD")


async def test_log_with_a_known_cost_and_the_automatic_cost_type(
    client: AsyncClient, db_session: AsyncSession, w: World
):
    resp = await _post(
        client,
        w.users["alice"],
        "/projects/AUWA/ai-usage/new/",
        _form(cost_type="", cost_amount="1.25", cost_currency="EUR", comments="known"),
        page="/projects/AUWA/ai-usage/new/",
    )
    assert resp.status_code == 303
    entry = (await db_session.execute(select(AiUsageEntry).where(AiUsageEntry.comments == "known"))).scalar_one()
    assert entry.cost_type == "unknown"  # no subscription covers alice
    assert entry.reported_cost == Money("1.25", "EUR")


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"tokens_input": "", "tokens_output": ""}, "Enter at least one token count."),
        ({"tokens_input": "12.5"}, "Token counts are whole numbers"),
        ({"usage_date": "yesterday"}, "Enter the day of the usage as YYYY-MM-DD"),
        ({"cost_amount": "lots"}, "Enter the cost as a number"),
        ({"issue": "AUWA-2"}, "No issue with that key"),  # carol-like: alice cannot see the private issue
        ({"issue": "AUWB-1"}, "No issue with that key"),
        ({"on_behalf_of": "auw_bob"}, "You cannot log AI usage for this user here."),
        ({"on_behalf_of": "nobody_at_all"}, "You cannot log AI usage for this user here."),
    ],
)
async def test_form_errors_keep_the_input(
    client: AsyncClient, db_session: AsyncSession, w: World, values: dict[str, str], message: str
):
    resp = await _post(
        client,
        w.users["alice"],
        "/projects/AUWA/ai-usage/new/",
        _form(comments="kept comment", **values),
        page="/projects/AUWA/ai-usage/new/",
    )
    assert resp.status_code == 422
    assert message in resp.text and "kept comment" in resp.text
    count = (await db_session.execute(select(AiUsageEntry).where(AiUsageEntry.comments == "kept comment"))).all()
    assert count == []


async def test_form_is_refused_without_permission(client: AsyncClient, w: World):
    assert (await _get(client, w.users["carol"], "/projects/AUWA/ai-usage/new/")).status_code == 403
    resp = await _post(
        client, w.users["carol"], "/projects/AUWA/ai-usage/new/", _form(), page="/projects/AUWA/ai-usage/"
    )
    assert resp.status_code == 422 and "You cannot log AI usage in this project." in resp.text


async def test_log_on_behalf_of_a_member(client: AsyncClient, db_session: AsyncSession, w: World):
    mgr = w.users["mgr"]
    page = await _get(client, mgr, "/projects/AUWA/ai-usage/new/")
    assert 'data-testid="aiu-form-behalf"' in page.text
    resp = await _post(
        client,
        mgr,
        "/projects/AUWA/ai-usage/new/",
        _form(on_behalf_of="auw_bob", comments="for bob"),
        page="/projects/AUWA/ai-usage/new/",
    )
    assert resp.status_code == 303
    entry = (await db_session.execute(select(AiUsageEntry).where(AiUsageEntry.comments == "for bob"))).scalar_one()
    assert (entry.user_id, entry.reporter_user_id) == (w.users["bob"].id, mgr.id)
    resp = await _get(client, mgr, "/projects/AUWA/ai-usage/")
    assert f"logged by {mgr.display_name or mgr.login}" in resp.text


async def test_log_unassigned_and_in_a_project_from_my_form(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    page = await _get(client, alice, "/my/ai-usage/new/")
    assert page.status_code == 200
    assert '<option value="AUWA"' in page.text and '<option value="AUWB"' not in page.text
    resp = await _post(client, alice, "/my/ai-usage/new/", _form(project="", comments="loose"))
    assert resp.status_code == 303 and resp.headers["location"] == "/my/ai-usage/?notice=logged"
    resp = await _post(client, alice, "/my/ai-usage/new/", _form(project="AUWB", comments="not mine"))
    assert resp.status_code == 422
    rows = (await db_session.execute(select(AiUsageEntry).where(AiUsageEntry.user_id == alice.id))).scalars().all()
    assert [(e.comments, e.project_id) for e in rows] == [("loose", None)]


# ---------------------------------------------------------------------------
# My AI usage and bulk reassignment
# ---------------------------------------------------------------------------


async def test_my_page_lists_own_entries_everywhere(client: AsyncClient, db_session: AsyncSession, w: World):
    loose = await _log(db_session, w, "alice", project=None, comments="loose")
    placed = await _log(db_session, w, "alice", comments="placed")
    bobs = await _log(db_session, w, "bob", comments="bobs")
    resp = await _get(client, w.users["alice"], "/my/ai-usage/")
    assert resp.status_code == 200
    assert f'data-entry-id="{loose.id}"' in resp.text and f'data-entry-id="{placed.id}"' in resp.text
    assert f'data-entry-id="{bobs.id}"' not in resp.text
    assert 'data-testid="aiu-reassign-form"' in resp.text
    resp = await _get(client, w.users["alice"], "/my/ai-usage/?project=-")
    assert f'data-entry-id="{loose.id}"' in resp.text and f'data-entry-id="{placed.id}"' not in resp.text


async def test_bulk_reassign(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    one = await _log(db_session, w, "alice", project=None, comments="one")
    two = await _log(db_session, w, "alice", project=None, comments="two")
    resp = await _post(
        client,
        alice,
        "/my/ai-usage/reassign/",
        {"entry_ids": [str(one.id), str(two.id)], "target_issue": "AUWA-1", "next": "/my/ai-usage/?project=-"},
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/my/ai-usage/?project=-&notice=reassigned"
    for entry_id in (one.id, two.id):
        entry = await _entry(db_session, entry_id)
        assert (entry.project_id, entry.issue_id) == (w.a.id, w.public_issue.id)

    # Back to unassigned.
    resp = await _post(client, alice, "/my/ai-usage/reassign/", {"entry_ids": [str(one.id)], "target_project": ""})
    assert resp.headers["location"] == "/my/ai-usage/?notice=reassigned"
    assert (await _entry(db_session, one.id)).project_id is None


async def test_bulk_reassign_is_all_or_nothing(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    mine = await _log(db_session, w, "alice", project=None, comments="mine")
    bobs = await _log(db_session, w, "bob", comments="bobs")

    cases = [
        ({"entry_ids": [], "target_project": "AUWA"}, "nothing_selected"),
        ({"entry_ids": [str(mine.id)], "target_project": "AUWB"}, "project_unknown"),
        ({"entry_ids": [str(mine.id)], "target_issue": "AUWA-2"}, "issue_unknown"),
        ({"entry_ids": [str(mine.id), str(bobs.id)], "target_project": "AUWA"}, "reassign_failed"),
    ]
    for data, code in cases:
        resp = await _post(client, alice, "/my/ai-usage/reassign/", data)
        assert resp.status_code == 303 and resp.headers["location"] == f"/my/ai-usage/?error={code}", data
        assert (await _entry(db_session, mine.id)).project_id is None
    assert (await _entry(db_session, bobs.id)).user_id == w.users["bob"].id

    page = await _get(client, alice, "/my/ai-usage/?error=project_unknown")
    assert "No project with that key, or you cannot reach it." in page.text


# ---------------------------------------------------------------------------
# Edit, exclude, include and delete
# ---------------------------------------------------------------------------


async def test_edit_a_manual_entry(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    entry = await _log(db_session, w, "alice", comments="before")
    page = await _get(client, alice, f"/ai-usage/{entry.id}/edit/")
    assert page.status_code == 200 and 'name="tokens_input"' in page.text and "before" in page.text
    resp = await _post(
        client,
        alice,
        f"/ai-usage/{entry.id}/edit/",
        _form(project="AUWA", tokens_input="2000000", comments="after", next="/projects/AUWA/ai-usage/"),
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/projects/AUWA/ai-usage/?notice=saved"
    saved = await _entry(db_session, entry.id)
    assert (saved.input, saved.comments, saved.api_equivalent) == (2_000_000, "after", Money("5", "USD"))


async def test_reported_entries_show_locked_fields_read_only(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    entry = await _log(db_session, w, "runner", target="alice", reported=True, key="runner", comments="ci")
    page = await _get(client, alice, f"/ai-usage/{entry.id}/edit/")
    assert page.status_code == 200
    assert 'data-testid="aiu-locked"' in page.text and 'name="tokens_input"' not in page.text
    assert "via key auw-runner" in page.text
    resp = await _post(
        client,
        alice,
        f"/ai-usage/{entry.id}/edit/",
        _form(project="AUWA", issue="AUWA-1", tokens_input="5", model="other", comments="attributed"),
    )
    assert resp.status_code == 303
    saved = await _entry(db_session, entry.id)
    assert (saved.issue_id, saved.comments, saved.input, saved.model) == (
        w.public_issue.id,
        "attributed",
        1_000_000,
        "lab-model-1",
    )


async def test_edit_permissions(client: AsyncClient, db_session: AsyncSession, w: World):
    bobs = await _log(db_session, w, "bob", comments="bobs")
    assert (await _get(client, w.users["alice"], f"/ai-usage/{bobs.id}/edit/")).status_code == 404
    assert (await _get(client, w.users["carol"], f"/ai-usage/{bobs.id}/edit/")).status_code == 403
    assert (await _get(client, w.users["mgr"], f"/ai-usage/{bobs.id}/edit/")).status_code == 200
    resp = await _post(client, w.users["carol"], f"/ai-usage/{bobs.id}/exclude/", {"next": "/"})
    assert resp.status_code == 403
    assert (await _entry(db_session, bobs.id)).excluded_at is None


async def test_edit_form_lists_only_the_readers_own_subscriptions(
    client: AsyncClient, db_session: AsyncSession, w: World
):
    bob = w.users["bob"]
    db_session.add(
        AiSubscription(
            user_id=bob.id,
            provider="anthropic",
            plan_key="custom",
            plan_name="Bobs Private Plan",
            amount=Decimal("20"),
            currency_code="USD",
            billing_period="month",
            starts_on=date(2020, 1, 1),
        )
    )
    await db_session.commit()
    bobs = await _log(db_session, w, "bob", comments="bobs")
    own = await _get(client, bob, f"/ai-usage/{bobs.id}/edit/")
    assert own.status_code == 200 and "Bobs Private Plan" in own.text
    managed = await _get(client, w.users["mgr"], f"/ai-usage/{bobs.id}/edit/")
    assert managed.status_code == 200 and "Bobs Private Plan" not in managed.text


async def test_exclude_and_include(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    entry = await _log(db_session, w, "runner", target="alice", reported=True, key="runner", comments="x")
    resp = await _post(client, alice, f"/ai-usage/{entry.id}/exclude/", {"next": "/projects/AUWA/ai-usage/"})
    assert resp.status_code == 303 and resp.headers["location"] == "/projects/AUWA/ai-usage/?notice=excluded"
    assert (await _entry(db_session, entry.id)).excluded_by_id == alice.id
    resp = await _post(client, alice, f"/ai-usage/{entry.id}/include/", {"next": "//evil.example/"})
    assert resp.headers["location"] == "/my/ai-usage/?notice=included"  # an unsafe next falls back
    assert (await _entry(db_session, entry.id)).excluded_at is None


async def test_delete(client: AsyncClient, db_session: AsyncSession, w: World):
    alice = w.users["alice"]
    manual = await _log(db_session, w, "alice", comments="typo")
    reported = await _log(db_session, w, "runner", target="alice", reported=True, key="runner", comments="r")
    page = await _get(client, alice, "/projects/AUWA/ai-usage/")
    assert page.text.count('data-testid="aiu-delete"') == 1  # reported entries are excluded, not deleted
    resp = await _post(client, alice, f"/ai-usage/{reported.id}/delete/", {})
    assert resp.status_code == 403
    resp = await _post(client, alice, f"/ai-usage/{manual.id}/delete/", {"next": "/projects/AUWA/ai-usage/"})
    assert resp.status_code == 303
    gone = await db_session.execute(select(AiUsageEntry.id).where(AiUsageEntry.id == manual.id))
    assert gone.first() is None
    page = await _get(client, w.users["admin"], "/projects/AUWA/ai-usage/")
    assert 'data-testid="aiu-delete"' in page.text


async def test_user_menu_links_my_ai_usage(client: AsyncClient, w: World):
    resp = await _get(client, w.users["alice"], "/my/ai-usage/")
    assert 'data-testid="user-menu-ai-usage"' in resp.text
