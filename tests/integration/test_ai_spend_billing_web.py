"""Who paid for AI usage in the spend reports: REST, CSV, the report pages, entry lists and MCP.

The world of ``tests/services/test_ai_spend_billing_reports.py`` with the
prefix ``rs``: projects ``RSA`` (company work) and ``RSB`` (personal), a
company gateway and personal accounts of alice (``Alice Max``, ``Alice key``)
and of the manager (``Mgr key``). Reimbursable in March: 49.50 USD.
"""

from __future__ import annotations

import csv
import html as html_lib
import io
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import AppError
from specivo.mcp import server
from specivo.mcp.ai_usage_tools import _ai_usage, _ai_usage_report
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.services.test_ai_spend_billing_reports import Billing, build_billing

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MARCH = {"from_date": "2026-03-01", "to_date": "2026-03-31"}
API_PROJECT = "/api/v1/projects/RSA/ai-usage/report/"
API_INSTANCE = "/api/v1/ai-usage/report/"
PROJECT = "/projects/RSA/ai-usage/report/"
ADMIN = "/admin/ai-spend/"


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    billing = await build_billing(db_session, "rs")
    clear_role_cache(db_session)
    return billing


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


async def _json(client: AsyncClient, user: User, url: str, **params: Any) -> dict[str, Any]:
    resp = await client.get(url, params={**MARCH, "currency": "USD", **params}, headers=_bearer(user))
    assert resp.status_code == 200, resp.text[:500]
    return resp.json()


async def _page(client: AsyncClient, user: User, url: str, **params: str) -> str:
    cookies = {"access_token": _make_access_token(user, get_settings())}
    resp = await client.get(url, params={**MARCH, "currency": "USD", **params}, cookies=cookies)
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


def _links(html: str, testid: str) -> list[dict[str, str]]:
    found = re.findall(rf'<a [^>]*href="([^"]+)"[^>]*data-testid="{testid}"', html)
    found += re.findall(rf'<a [^>]*data-testid="{testid}"[^>]*href="([^"]+)"', html)
    return [{k: v[0] for k, v in parse_qs(urlsplit(html_lib.unescape(u)).query).items()} for u in found]


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_rest_report_by_payer_with_reimbursable(client: AsyncClient, b: Billing) -> None:
    data = await _json(client, b.w.admin, API_INSTANCE, group_by="payer")
    assert data["reimbursable_shown"] is True
    assert data["totals"]["reimbursable"]["total"]["amount"] == "49.50"
    payers = {p["key"]["payer"]: p for p in data["payer_totals"]}
    assert list(payers) == ["employer", "employee", "unknown"]
    assert payers["employer"]["total_spend"]["total"]["amount"] == "20.00"
    assert payers["employee"]["leverage"] == "0.40"
    assert {r["labels"]["payer"] for r in data["rows"]} >= {"Employer", "Employee", "Unknown"}


async def test_rest_filters(client: AsyncClient, b: Billing) -> None:
    w = b.w
    employer = await _json(client, w.admin, API_INSTANCE, payer="employer", group_by="")
    assert employer["totals"]["entries"] == 1 and employer["unallocated_shown"] is False
    org = await _json(client, w.admin, API_INSTANCE, scope="organization", group_by="project")
    assert org["totals"]["entries"] == 4
    by_slug = await _json(client, w.mgr, API_PROJECT, billing_account="rs-gateway", group_by="")
    assert by_slug["totals"]["entries"] == 1
    # Someone else's personal account matches nothing, by id or slug.
    hidden = await _json(client, w.mgr, API_PROJECT, billing_account=str(b.alice_key.id), group_by="")
    assert hidden["totals"]["entries"] == 0
    assert (await _json(client, w.mgr, API_PROJECT, billing_account="rs-alice-key"))["totals"]["entries"] == 0
    routed = await _json(client, w.mgr, API_PROJECT, route="corp-gw", group_by="route")
    assert [r["key"]["route"] for r in routed["rows"]] == ["corp-gw"]
    resp = await client.get(API_INSTANCE, params={**MARCH, "payer": "company"}, headers=_bearer(w.admin))
    assert resp.status_code == 422
    resp = await client.get(API_INSTANCE, params={**MARCH, "scope": "mine"}, headers=_bearer(w.admin))
    assert resp.status_code == 422


async def test_rest_names_only_accounts_the_reader_may_see(client: AsyncClient, b: Billing) -> None:
    data = await _json(client, b.w.mgr, API_PROJECT, group_by="billing_account")
    assert data["hidden_billing_account_key"] == 0
    labels = {r["key"]["billing_account"]: r["labels"]["billing_account"] for r in data["rows"]}
    assert labels[0] == "Personal account"
    assert "Alice Max" not in str(data) and "Alice key" not in str(data)
    assert data["reimbursable_shown"] is False and data["totals"]["reimbursable"] is None
    own = await _json(client, b.w.mgr, API_PROJECT, user_id=b.w.mgr.id, group_by="")
    assert own["reimbursable_shown"] is True and own["totals"]["reimbursable"]["total"]["amount"] == "2.00"


async def test_rest_csv(client: AsyncClient, b: Billing) -> None:
    resp = await client.get(
        API_INSTANCE,
        params={**MARCH, "currency": "USD", "group_by": "payer", "format": "csv"},
        headers=_bearer(b.w.admin),
    )
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    total = next(r for r in rows if r["period"] == "total")
    assert total["reimbursable"] == "49.50"
    assert [r["period"] for r in rows if r["period"].startswith("total:")] == [
        "total:employer",
        "total:employee",
        "total:unknown",
    ]


# ---------------------------------------------------------------------------
# The report pages and the entry lists
# ---------------------------------------------------------------------------


async def test_admin_page_shows_who_paid_and_drills_down(client: AsyncClient, b: Billing) -> None:
    html = await _page(client, b.w.admin, ADMIN, group="payer")
    assert 'data-testid="spend-payers"' in html
    assert 'data-testid="spend-reimbursable"' in html and "49.50" in html
    assert 'data-testid="spend-payer"' in html and 'data-testid="spend-scope"' in html
    opened = {q["payer"]: q for q in _links(html, "spend-open")}
    assert set(opened) == {"employer", "employee", "unknown"}
    assert opened["employee"]["group"] == "billing_account"

    html = await _page(client, b.w.admin, ADMIN, payer="employee", group="billing_account")
    assert "Alice Max" in html and "Mgr key" in html
    accounts = {q["billing_account"] for q in _links(html, "spend-open")}
    assert str(b.alice_key.id) in accounts and "-" in accounts

    html = await _page(client, b.w.admin, ADMIN, billing_account=str(b.alice_key.id), group="user")
    assert 'aria-current="page">Alice key<' in html

    # The entry list behind a level takes the same filters.
    html = await _page(
        client, b.w.admin, "/admin/ai-spend/entries/", payer="employee", billing_account=str(b.alice_key.id)
    )
    assert "Alice key" in html
    assert html.count('data-testid="aiu-payer"') == 2


async def test_project_page_hides_personal_accounts(client: AsyncClient, b: Billing) -> None:
    html = await _page(client, b.w.mgr, PROJECT, group="billing_account")
    assert "Personal account" in html and "Alice Max" not in html and "Alice key" not in html
    assert 'data-testid="spend-reimbursable"' not in html
    # The hidden row does not open.
    opened = {q.get("billing_account") for q in _links(html, "spend-open")}
    assert "0" not in opened and str(b.gateway.id) in opened

    html = await _page(client, b.w.mgr, PROJECT, scope="organization", group="user")
    assert 'value="organization" selected' in html

    # The project's entry list: a hidden account matches nothing and is named by its number.
    entries = await _page(client, b.w.mgr, "/projects/RSA/ai-usage/", billing_account=str(b.alice_key.id))
    assert 'data-testid="aiu-account-filter"' in entries and "Alice key" not in entries
    assert 'data-testid="aiu-payer"' not in entries
    routed = await _page(client, b.w.mgr, "/projects/RSA/ai-usage/", route="corp-gw", payer="employer")
    assert 'data-testid="aiu-route-filter"' in routed and 'data-testid="aiu-payer-filter"' in routed


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def _report(db: AsyncSession, user: User, **kw: Any) -> str:
    args: dict[str, Any] = {
        "project_key": None,
        "date_from": "2026-03-01",
        "date_to": "2026-03-31",
        "preset": None,
        "group_by": None,
        "period": None,
        "currency": "USD",
        "rate_policy": None,
        "include_seat_cost": True,
        "user_login": None,
        "tool": None,
        "model": None,
        "cost_type": None,
        "sort": "key",
    }
    args.update(kw)
    clear_role_cache(db)
    return await _ai_usage_report(db, user, **args)


async def test_mcp_report_by_payer(db_session: AsyncSession, b: Billing) -> None:
    text = await _report(db_session, b.w.admin, group_by="payer")
    assert "payer=Employee:" in text and "reimbursable=49.50 USD" in text
    tail = text.split("By payer:\n", 1)[1].splitlines()
    assert [line.split(":", 1)[0].strip() for line in tail] == ["employer", "employee", "unknown"]

    org = await _report(db_session, b.w.admin, scope="organization", group_by="project")
    assert "(organization only)" in org.splitlines()[0]
    assert next(line for line in org.splitlines() if line.startswith("Total:")).startswith("Total: entries=4")

    mgr = await _report(db_session, b.w.mgr, project_key="RSA", group_by="billing_account")
    assert "billing_account=Personal account" in mgr and "Alice" not in mgr and "reimbursable" not in mgr
    assert "billing_account=(no billing account)" in mgr
    none = await _report(db_session, b.w.mgr, project_key="RSA", billing_account="none", group_by="")
    assert "Total: entries=1" in none
    with pytest.raises(AppError) as exc:
        await _report(db_session, b.w.admin, payer="company")
    assert exc.value.field == "payer"


async def test_mcp_tools_document_the_new_arguments() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    report = tools["specivo_ai_usage_report"]
    assert "scope='organization'" in (server.mcp.tool_help("specivo_ai_usage_report") or "")
    assert {"payer", "billing_account", "route", "scope"} <= set(report.input_schema["properties"])
    assert {"payer", "billing_account"} <= set(tools["specivo_ai_usage"].input_schema["properties"])


async def test_mcp_entry_list_filters_by_payer_and_account(db_session: AsyncSession, b: Billing) -> None:
    clear_role_cache(db_session)
    listed = await _ai_usage(db_session, b.w.mgr, "list", project_key="RSA", payer="employer")
    assert "(1 total" in listed and "payer=employer" in listed
    hidden = await _ai_usage(db_session, b.w.mgr, "list", project_key="RSA", billing_account="rs-alice-key")
    assert "(0 total" in hidden
    gateway = await _ai_usage(db_session, b.w.mgr, "list", project_key="RSA", billing_account="rs-gateway")
    assert "(1 total" in gateway
