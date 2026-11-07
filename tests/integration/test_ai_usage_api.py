"""The AI usage entries REST API: log, list, read, change, exclude and delete.

The world (``build_world``, shared with the MCP tests): two private projects,
``AUA`` with a public and a private issue, and ``AUB``; prices for an
invented provider ``testlab`` so the costs are exact (1M input and 100k
output tokens of ``lab-model-1`` cost 3 USD).

- ``mgr``: ``*`` with ``ai_usage_visibility = all`` in AUA and AUB;
- ``alice``, ``bob``: may view their own usage and log it in AUA;
- ``runner``: a service account that logs for others in AUA;
- ``carol``: may view everyone's usage in AUA, not log it, not see the private issue;
- ``dave``: AUA member without AI usage permissions;
- ``eve``: a deactivated AUA member; ``outsider``: member of nothing; ``admin``.

Every account has an API key; ``alice_limited`` and ``mgr_limited`` are limited to AUA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DAY = "2026-03-10"
REFUSED = {
    "code": "permission_denied",
    "message": "You cannot log AI usage for this user here",
    "field": "on_behalf_of",
}


@dataclass
class World:
    a: Project
    b: Project
    public_issue: Issue
    private_issue: Issue
    users: dict[str, User]
    keys: dict[str, str] = field(default_factory=dict)
    key_ids: dict[str, int] = field(default_factory=dict)

    def auth(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[name]}"}


async def _role(db: AsyncSession, name: str, permissions: list[str], visibility: str = "own") -> Role:
    role = Role(
        name=name, permissions=permissions, builtin=0, issues_visibility="default", ai_usage_visibility=visibility
    )
    db.add(role)
    await db.flush()
    return role


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


async def build_world(db: AsyncSession, prefix: str) -> World:
    """The world of the module docstring; *prefix* keeps logins and keys unique to the calling module."""
    up = prefix.upper()
    status = StatusFactory.build(name=f"{up} Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name=f"{up} Task", default_status_id=status.id)
    priority = PriorityFactory.build(name=f"{up} Normal")
    a = ProjectFactory.build(key=f"{up}A", identifier=f"{prefix}a", is_public=False)
    b = ProjectFactory.build(key=f"{up}B", identifier=f"{prefix}b", is_public=False)
    db.add_all([tracker, priority, a, b])
    await db.flush()

    users: dict[str, User] = {}
    for name in ("mgr", "alice", "bob", "carol", "dave", "outsider"):
        users[name] = UserFactory.build(login=f"{prefix}_{name}", status="active")
    users["eve"] = UserFactory.build(login=f"{prefix}_eve", status="deactivated")
    users["runner"] = ServiceAccountFactory.build(login=f"{prefix}_runner", status="active")
    users["admin"] = AdminUserFactory.build(login=f"{prefix}_admin", status="active")
    db.add_all(users.values())
    await db.flush()

    mgr = await _role(db, f"{up} Manager", ["*"], "all")
    dev = await _role(db, f"{up} Developer", ["view_issues", "view_ai_usage", "log_ai_usage"])
    runner = await _role(
        db, f"{up} Runner", ["view_issues", "view_ai_usage", "log_ai_usage", "log_ai_usage_for_others"]
    )
    viewer = await _role(db, f"{up} Viewer", ["view_issues", "view_ai_usage"], "all")
    nothing = await _role(db, f"{up} Nothing", ["view_issues"])
    await _member(db, a, users["mgr"], mgr)
    await _member(db, b, users["mgr"], mgr)
    for name in ("alice", "bob", "eve"):
        await _member(db, a, users[name], dev)
    await _member(db, a, users["runner"], runner)
    await _member(db, a, users["carol"], viewer)
    await _member(db, a, users["dave"], nothing)

    def issue(number: int, **kw: Any) -> Issue:
        built: Issue = IssueFactory.build(
            project_id=a.id,
            project_key=a.key,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=users["mgr"].id,
            sequence_number=number,
            subject=f"Work item {number}",
            **kw,
        )
        return built

    public_issue, private_issue = issue(1), issue(2, is_private=True)
    db.add_all([public_issue, private_issue])
    await db.flush()

    await AiModelPriceService().create(
        db,
        PriceInput(
            provider="testlab",
            model="lab-model-1",
            currency="USD",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            prices={"input": "2", "output": "10", "cache_read": "0.2"},
        ),
        actor=users["admin"],
    )

    world = World(a=a, b=b, public_issue=public_issue, private_issue=private_issue, users=users)
    keys = ApiKeyService()
    for name, user in users.items():
        if name == "eve":
            continue
        key, raw = await keys.create_key(db, user_id=user.id, name=f"{prefix}-{name}", check_scope_access=False)
        world.keys[name], world.key_ids[name] = raw, key.id
    for name in ("alice", "mgr"):
        key, raw = await keys.create_key(
            db,
            user_id=users[name].id,
            name=f"{prefix}-{name}-limited",
            scopes={"projects": [a.key]},
            check_scope_access=False,
        )
        world.keys[f"{name}_limited"], world.key_ids[f"{name}_limited"] = raw, key.id
    await db.commit()
    clear_role_cache(db)
    return world


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session, "aua")


def body(**kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tool": "claude-code",
        "provider": "testlab",
        "model": "lab-model-1",
        "usage_date": DAY,
        "tokens": {"input": 1_000_000, "output": 100_000},
        "cost_type": "api",
    }
    values.update(kw)
    return values


async def log(client: AsyncClient, w: World, who: str, project: str | None = "AUAA", **kw: Any) -> dict[str, Any]:
    url = f"/api/v1/projects/{project}/ai-usage/" if project else "/api/v1/my/ai-usage/"
    resp = await client.post(url, json=body(**kw), headers=w.auth(who))
    assert resp.status_code == 201, resp.text
    result: dict[str, Any] = resp.json()
    return result


def error(resp: Response) -> dict[str, Any]:
    first: dict[str, Any] = resp.json()["errors"][0]
    return {k: first[k] for k in ("code", "message", "field")}


async def _events(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value))
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


async def test_log_own_usage_with_an_api_key(client: AsyncClient, w: World) -> None:
    entry = await log(client, w, "alice", issue_id=w.public_issue.id, comments="Login form")
    assert entry["source"] == "api"
    assert entry["user"]["login"] == entry["reporter"]["login"] == "aua_alice"
    assert entry["reporter_api_key_id"] == w.key_ids["alice"]
    assert (entry["project_key"], entry["issue_key"], entry["attribution_method"]) == ("AUAA", "AUAA-1", "explicit")
    assert entry["tokens"] == {
        "input": 1_000_000,
        "output": 100_000,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 0,
        "reasoning": 0,
    }
    assert entry["api_equivalent_cost"]["currency"] == "USD"
    assert float(entry["api_equivalent_cost"]["amount"]) == 3
    assert float(entry["marginal_cost"]["amount"]) == 3
    assert entry["cost_basis"] == "calculated"
    assert entry["locked"] is False and entry["excluded"] is False
    assert entry["revision"] == 1


async def test_entry_reports_both_buckets_and_their_total(client: AsyncClient, w: World) -> None:
    created = await log(client, w, "alice", long_context_tokens={"input": 300_000, "output": 20_000})
    assert created["tokens"]["input"] == 1_000_000 and created["long_context_tokens"]["input"] == 300_000
    assert created["total_tokens"] == 1_420_000
    plain = await log(client, w, "alice")
    assert plain["long_context_tokens"] is None and plain["total_tokens"] == 1_100_000


async def test_log_by_a_person_with_a_known_cost(client: AsyncClient, w: World) -> None:
    resp = await client.post("/api/v1/auth/login/", json={"login": "aua_alice", "password": TEST_PASSWORD})
    token = resp.json()["access_token"]
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/",
        json=body(cost={"amount": "1.50", "currency": "USD"}),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert entry["source"] == "manual"
    assert entry["reporter_api_key_id"] is None
    assert entry["reported_cost"]["currency"] == "USD"
    assert float(entry["reported_cost"]["amount"]) == 1.5
    assert float(entry["marginal_cost"]["amount"]) == 1.5
    assert entry["cost_basis"] == "manual"


async def test_log_refusals(client: AsyncClient, w: World) -> None:
    resp = await client.post("/api/v1/projects/AUAA/ai-usage/", json=body(), headers=w.auth("dave"))
    assert resp.status_code == 403
    resp = await client.post("/api/v1/projects/AUAA/ai-usage/", json=body(), headers=w.auth("outsider"))
    assert resp.status_code == 404
    resp = await client.post("/api/v1/projects/AUAA/ai-usage/", json=body(), headers=w.auth("carol"))
    assert resp.status_code == 403
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/", json=body(tokens={"input": -1}), headers=w.auth("alice")
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/",
        json=body(cost={"amount": "1", "currency": "ZZZ"}),
        headers=w.auth("alice"),
    )
    assert resp.status_code == 422
    assert error(resp)["field"].startswith("cost")
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/", json=body(usage_date="2019-12-31"), headers=w.auth("alice")
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/", json=body(issue_id=w.private_issue.id), headers=w.auth("alice")
    )
    assert resp.status_code == 404
    resp = await client.post("/api/v1/projects/AUAA/ai-usage/", json=body(source="reporter"), headers=w.auth("alice"))
    assert resp.status_code == 422  # unknown property


async def test_own_unassigned_usage_needs_no_permission(client: AsyncClient, w: World) -> None:
    entry = await log(client, w, "dave", project=None)
    assert entry["project_id"] is None and entry["attribution_method"] == "none"
    resp = await client.post("/api/v1/my/ai-usage/", json=body(issue_id=w.public_issue.id), headers=w.auth("dave"))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# On behalf
# ---------------------------------------------------------------------------


async def test_on_behalf_allowed_and_audited(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await log(client, w, "runner", on_behalf_of="AUA_ALICE")
    assert entry["user"]["login"] == "aua_alice"
    assert entry["reporter"]["login"] == "aua_runner"
    events = await _events(db_session, AuditEvent.AI_USAGE_LOGGED_FOR_OTHER)
    assert [e.details["entry_id"] for e in events] == [entry["id"]]
    # Logging in one's own login is not on behalf.
    own = await log(client, w, "alice", on_behalf_of="aua_alice")
    assert own["user"]["login"] == "aua_alice"


async def test_on_behalf_codes_for_an_entitled_sender(client: AsyncClient, w: World) -> None:
    for login, code in (
        ("aua_outsider", "ai_usage_target_not_member"),
        ("aua_eve", "ai_usage_target_invalid"),
        ("nobody-here", "ai_usage_target_unknown"),
    ):
        resp = await client.post(
            "/api/v1/projects/AUAA/ai-usage/", json=body(on_behalf_of=login), headers=w.auth("runner")
        )
        assert resp.status_code == 422, resp.text
        assert error(resp)["code"] == code
    # Unassigned usage for someone else: administrators only.
    resp = await client.post("/api/v1/my/ai-usage/", json=body(on_behalf_of="aua_alice"), headers=w.auth("runner"))
    assert resp.status_code == 403
    assert error(resp) == REFUSED
    entry = await log(client, w, "admin", project=None, on_behalf_of="aua_outsider")
    assert entry["user"]["login"] == "aua_outsider"


async def test_on_behalf_hides_which_logins_exist(client: AsyncClient, w: World) -> None:
    """Without the right to log for others, every login gets the same answer, byte for byte."""
    logins = ["aua_bob", "aua_outsider", "aua_eve", "aua_admin", "nobody-here"]
    for who, url, extra, status in (
        ("alice", "/api/v1/projects/AUAA/ai-usage/", {}, 403),
        ("alice", "/api/v1/projects/AUAA/ai-usage/", {"issue_id": 999_999}, 403),
        ("alice", "/api/v1/projects/AUAA/ai-usage/", {"usage_date": "2019-12-31"}, 403),
        ("alice", "/api/v1/my/ai-usage/", {}, 403),
        ("runner", "/api/v1/my/ai-usage/", {}, 403),
        # A body the schema refuses never reaches the login.
        ("alice", "/api/v1/projects/AUAA/ai-usage/", {"cost": {"amount": "1", "currency": "ZZZ"}}, 422),
    ):
        answers = set()
        for login in logins:
            resp = await client.post(url, json=body(on_behalf_of=login, **extra), headers=w.auth(who))
            answers.add((resp.status_code, json.dumps(resp.json(), sort_keys=True)))
        assert len(answers) == 1, (who, url, extra, answers)
        got_status, text = answers.pop()
        assert got_status == status
        if status == 403:
            assert error_from(text) == REFUSED


def error_from(text: str) -> dict[str, Any]:
    first: dict[str, Any] = json.loads(text)["errors"][0]
    return {k: first[k] for k in ("code", "message", "field")}


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def entries(client: AsyncClient, db_session: AsyncSession, w: World) -> dict[str, dict[str, Any]]:
    found = {
        "alice": await log(client, w, "alice", issue_id=w.public_issue.id),
        "alice_unassigned": await log(client, w, "alice", project=None, tool="codex"),
        "alice_unassigned_limited": await log(client, w, "alice_limited", project=None),
        "bob": await log(client, w, "bob", cost_type="subscription"),
        "bob_private": await log(client, w, "mgr", on_behalf_of="aua_bob", issue_id=w.private_issue.id),
        "mgr_b": await log(client, w, "mgr", project="AUAB"),
    }
    return found


def ids(resp: Response) -> set[int]:
    assert resp.status_code == 200, resp.text
    return {item["id"] for item in resp.json()["items"]}


async def test_my_usage_includes_unassigned_entries(
    client: AsyncClient, w: World, entries: dict[str, dict[str, Any]]
) -> None:
    mine = {entries[n]["id"] for n in ("alice", "alice_unassigned", "alice_unassigned_limited")}
    assert ids(await client.get("/api/v1/my/ai-usage/", headers=w.auth("alice"))) == mine
    unassigned = await client.get("/api/v1/my/ai-usage/", params={"unassigned": True}, headers=w.auth("alice"))
    assert ids(unassigned) == mine - {entries["alice"]["id"]}
    in_project = await client.get("/api/v1/my/ai-usage/", params={"project_key": "auaa"}, headers=w.auth("alice"))
    assert ids(in_project) == {entries["alice"]["id"]}
    by_tool = await client.get("/api/v1/my/ai-usage/", params={"tool": "codex"}, headers=w.auth("alice"))
    assert ids(by_tool) == {entries["alice_unassigned"]["id"]}
    # Bob sees his entry on the private issue, logged by the manager, without the issue's key.
    bob = (await client.get("/api/v1/my/ai-usage/", headers=w.auth("bob"))).json()["items"]
    private = next(item for item in bob if item["id"] == entries["bob_private"]["id"])
    assert private["issue_id"] is None and private["issue_key"] is None
    assert private["reporter"]["login"] == "aua_mgr"


async def test_project_listing_follows_visibility(
    client: AsyncClient, w: World, entries: dict[str, dict[str, Any]]
) -> None:
    url = "/api/v1/projects/AUAA/ai-usage/"
    assert ids(await client.get(url, headers=w.auth("alice"))) == {entries["alice"]["id"]}
    # Carol sees everyone's, but not the entry on the issue she cannot see.
    assert ids(await client.get(url, headers=w.auth("carol"))) == {entries["alice"]["id"], entries["bob"]["id"]}
    assert ids(await client.get(url, headers=w.auth("dave"))) == set()
    everything = {entries[n]["id"] for n in ("alice", "bob", "bob_private")}
    assert ids(await client.get(url, headers=w.auth("mgr"))) == everything
    assert (await client.get(url, headers=w.auth("outsider"))).status_code == 404

    filtered = await client.get(url, params={"cost_type": "subscription"}, headers=w.auth("mgr"))
    assert ids(filtered) == {entries["bob"]["id"]}
    by_user = await client.get(url, params={"user_id": w.users["bob"].id}, headers=w.auth("mgr"))
    assert ids(by_user) == {entries["bob"]["id"], entries["bob_private"]["id"]}
    by_issue = await client.get(url, params={"issue_id": w.public_issue.id}, headers=w.auth("mgr"))
    assert ids(by_issue) == {entries["alice"]["id"]}
    by_model = await client.get(url, params={"model": "lab-model-1", "limit": 1}, headers=w.auth("mgr"))
    page = by_model.json()
    assert (page["total_count"], page["limit"], len(page["items"])) == (3, 1, 1)
    dated = await client.get(url, params={"from_date": "2026-03-11"}, headers=w.auth("mgr"))
    assert ids(dated) == set()


async def test_project_limited_key(client: AsyncClient, w: World, entries: dict[str, dict[str, Any]]) -> None:
    # Unassigned entries only where this key reported them.
    limited = await client.get("/api/v1/my/ai-usage/", headers=w.auth("alice_limited"))
    assert ids(limited) == {entries["alice"]["id"], entries["alice_unassigned_limited"]["id"]}
    assert (await client.get("/api/v1/projects/AUAB/ai-usage/", headers=w.auth("mgr_limited"))).status_code == 404
    entry = f"/api/v1/ai-usage/{entries['mgr_b']['id']}/"
    assert (await client.get(entry, headers=w.auth("mgr_limited"))).status_code == 404
    assert (await client.get(entry, headers=w.auth("mgr"))).status_code == 200
    resp = await client.post("/api/v1/projects/AUAB/ai-usage/", json=body(), headers=w.auth("mgr_limited"))
    assert resp.status_code == 404
    # Moving an entry out of the limit is refused like any other project outside it.
    resp = await client.patch(
        f"/api/v1/ai-usage/{entries['alice']['id']}/", json={"project_key": "AUAB"}, headers=w.auth("mgr_limited")
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


async def test_get_follows_visibility(client: AsyncClient, w: World, entries: dict[str, dict[str, Any]]) -> None:
    url = f"/api/v1/ai-usage/{entries['alice']['id']}/"
    assert (await client.get(url, headers=w.auth("alice"))).json()["id"] == entries["alice"]["id"]
    assert (await client.get(url, headers=w.auth("carol"))).status_code == 200
    assert (await client.get(url, headers=w.auth("bob"))).status_code == 404
    assert (await client.get(url, headers=w.auth("outsider"))).status_code == 404
    assert (await client.get("/api/v1/ai-usage/999999999/", headers=w.auth("admin"))).status_code == 404


async def test_update_reprices_and_audits(
    client: AsyncClient, db_session: AsyncSession, w: World, entries: dict[str, dict[str, Any]]
) -> None:
    url = f"/api/v1/ai-usage/{entries['alice']['id']}/"
    resp = await client.patch(url, json={"tokens": {"input": 2_000_000}, "comments": "Redone"}, headers=w.auth("alice"))
    assert resp.status_code == 200, resp.text
    entry = resp.json()
    assert entry["tokens"]["input"] == 2_000_000 and entry["tokens"]["output"] == 100_000  # only what was sent
    assert float(entry["api_equivalent_cost"]["amount"]) == 5
    assert entry["comments"] == "Redone" and entry["revision"] == 2
    assert await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER) == []

    # Carol may read it, not change it; the manager may, and is audited.
    assert (await client.patch(url, json={"comments": "x"}, headers=w.auth("carol"))).status_code == 403
    resp = await client.patch(url, json={"cost_type": "free", "issue_id": None}, headers=w.auth("mgr"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["marginal_cost"]["amount"] in ("0", "0.00000000")
    assert resp.json()["issue_id"] is None
    events = await _events(db_session, AuditEvent.AI_USAGE_UPDATED_BY_OTHER)
    assert len(events) == 1 and "cost_type" in events[0].details["changes"]

    resp = await client.patch(url, json={"model": None}, headers=w.auth("alice"))
    assert resp.status_code == 422
    resp = await client.patch(url, json={"source": "mcp"}, headers=w.auth("alice"))
    assert resp.status_code == 422


async def test_move_between_projects(client: AsyncClient, w: World, entries: dict[str, dict[str, Any]]) -> None:
    unassigned = f"/api/v1/ai-usage/{entries['alice_unassigned']['id']}/"
    resp = await client.patch(
        unassigned, json={"project_key": "AUAA", "issue_id": w.public_issue.id}, headers=w.auth("alice")
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["project_key"], resp.json()["issue_key"]) == ("AUAA", "AUAA-1")
    # Alice cannot reach AUAB.
    resp = await client.patch(unassigned, json={"project_key": "AUAB"}, headers=w.auth("alice"))
    assert resp.status_code == 404
    resp = await client.patch(unassigned, json={"project_key": None}, headers=w.auth("alice"))
    assert resp.status_code == 200
    # A person moved it: marked manual, so a reporter's replay never changes it again.
    assert (resp.json()["project_id"], resp.json()["issue_id"], resp.json()["attribution_method"]) == (
        None,
        None,
        "manual",
    )
    # Someone else's entry into a project where the manager may log for others, but bob is not a member.
    resp = await client.patch(
        f"/api/v1/ai-usage/{entries['bob']['id']}/", json={"project_key": "AUAB"}, headers=w.auth("mgr")
    )
    assert resp.status_code == 422
    assert error(resp)["code"] == "ai_usage_target_not_member"


async def test_delete(
    client: AsyncClient, db_session: AsyncSession, w: World, entries: dict[str, dict[str, Any]]
) -> None:
    url = f"/api/v1/ai-usage/{entries['bob']['id']}/"
    assert (await client.delete(url, headers=w.auth("carol"))).status_code == 403
    assert (await client.delete(url, headers=w.auth("alice"))).status_code == 404
    assert (await client.delete(url, headers=w.auth("bob"))).status_code == 204
    assert (await client.get(url, headers=w.auth("bob"))).status_code == 404
    assert len(await _events(db_session, AuditEvent.AI_USAGE_DELETED)) == 1


# ---------------------------------------------------------------------------
# Reporter entries: locked fields and the exclude flow
# ---------------------------------------------------------------------------


async def _ingest(client: AsyncClient, w: World, who: str, **kw: Any) -> dict[str, Any]:
    record = {
        "idempotency_key": "aua-session-1",
        "tool": "claude-code",
        "provider": "testlab",
        "model": "lab-model-1",
        "usage_date": DAY,
        "tokens": {"input": 1_000_000, "output": 100_000},
        "attribution": {"project_key": "AUAA"},
    }
    record.update(kw)
    resp = await client.post(
        "/api/v1/ai-usage/ingest/",
        json={"schema": "specivo.ai-usage.v1", "client": {"name": "aua-tests"}, "records": [record]},
        headers=w.auth(who),
    )
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["results"][0]
    assert result["status"] in ("created", "updated", "unchanged"), result
    return result


async def test_reporter_entries_keep_their_usage(client: AsyncClient, w: World) -> None:
    created = await _ingest(client, w, "alice")
    url = f"/api/v1/ai-usage/{created['entry_id']}/"
    entry = (await client.get(url, headers=w.auth("alice"))).json()
    assert (entry["source"], entry["locked"], entry["client_name"]) == ("reporter", True, "aua-tests")

    for change in ({"tokens": {"input": 1}}, {"model": "other"}, {"cost": {"amount": "1", "currency": "USD"}}):
        resp = await client.patch(url, json=change, headers=w.auth("alice"))
        assert resp.status_code == 422, change
        assert error(resp)["code"] == "ai_usage_field_locked"
    resp = await client.patch(
        url, json={"issue_id": w.public_issue.id, "comments": "Login form"}, headers=w.auth("alice")
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["issue_key"], resp.json()["comments"]) == ("AUAA-1", "Login form")

    # People exclude reported entries; only administrators delete them.
    assert (await client.delete(url, headers=w.auth("alice"))).status_code == 403
    assert (await client.delete(url, headers=w.auth("mgr"))).status_code == 403
    assert (await client.delete(url, headers=w.auth("admin"))).status_code == 204


async def test_exclude_flow(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    created = await _ingest(client, w, "alice")
    url = f"/api/v1/ai-usage/{created['entry_id']}/"
    assert (await client.post(url + "exclude/", headers=w.auth("carol"))).status_code == 403
    resp = await client.post(url + "exclude/", headers=w.auth("alice"))
    assert resp.status_code == 200
    excluded = resp.json()
    assert excluded["excluded"] is True and excluded["excluded_by_id"] == w.users["alice"].id

    # A later re-send of the bucket keeps the exclusion.
    updated = await _ingest(client, w, "alice", tokens={"input": 3_000_000, "output": 100_000})
    assert updated["status"] == "updated"
    again = (await client.get(url, headers=w.auth("alice"))).json()
    assert again["excluded"] is True and again["tokens"]["input"] == 3_000_000

    listing = "/api/v1/my/ai-usage/"
    assert ids(await client.get(listing, params={"excluded": True}, headers=w.auth("alice"))) == {excluded["id"]}
    assert ids(await client.get(listing, params={"excluded": False}, headers=w.auth("alice"))) == set()

    resp = await client.post(url + "include/", headers=w.auth("mgr"))
    assert resp.status_code == 200 and resp.json()["excluded"] is False
    assert len(await _events(db_session, AuditEvent.AI_USAGE_EXCLUDED)) == 1
    assert len(await _events(db_session, AuditEvent.AI_USAGE_INCLUDED)) == 1


async def test_ingest_path_is_not_an_entry_id(client: AsyncClient, w: World) -> None:
    resp = await client.get("/api/v1/ai-usage/ingest/", headers=w.auth("alice"))
    assert resp.status_code == 200
    assert "versions" in resp.json()


async def test_entries_in_the_database(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    entry = await log(client, w, "runner", on_behalf_of="aua_bob", request_count=3, usage_date="2026-03-11")
    stored = await db_session.get(AiUsageEntry, entry["id"])
    assert stored is not None
    await db_session.refresh(stored)
    assert (stored.user_id, stored.reporter_user_id, stored.source) == (
        w.users["bob"].id,
        w.users["runner"].id,
        "api",
    )
    assert stored.usage_date == date(2026, 3, 11) and stored.request_count == 3


async def test_on_behalf_of_a_system_account_is_an_unknown_login(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    db_session.add(UserFactory.build(login="$aua_git", status="active", display_name="Git", password_hash=None))
    await db_session.commit()
    resp = await client.post(
        "/api/v1/projects/AUAA/ai-usage/", json=body(on_behalf_of="$AUA_GIT"), headers=w.auth("runner")
    )
    assert resp.status_code == 422, resp.text
    assert error(resp)["code"] == "ai_usage_target_unknown"
