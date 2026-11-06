"""AI usage ingest v1 over HTTP: upsert semantics, attribution, authorization, limits and the published examples.

The world: a private project ``INGX`` with issues 3, 7, 12 and a private 20,
a second private project ``INGY`` (issue 1) where the bot may read but not
log, and a repository ``acme/app`` connected to ``INGX`` with one commit
linked to ``INGX-3``.

- ``bot``: a service account logging for itself and for others in ``INGX``,
  a reader in ``INGY``;
- ``bot2``: another reporter like ``bot``;
- ``teammate``: logs own usage in ``INGX``;
- ``reader``: an ``INGX`` member without AI usage permissions;
- ``outsider``: active, member of nothing.

Time is frozen at 2026-11-06 12:00 UTC, the day the examples were written for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.repository import IssueCommitLink, RepositoryCommit
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import ServiceAccountFactory, UserFactory
from tests.fixtures.forge_hooks import make_integration

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/ai-usage/ingest"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
DAY = "2026-11-05"
COMMIT_SHA = "32fe3796ff2245cb43d05a41280745a53a12bd54"
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "ai-usage" / "v1" / "examples"


@dataclass
class World:
    p: Project
    q: Project
    issues: dict[int, Issue]
    q_issue: Issue
    users: dict[str, User]
    keys: dict[str, str]
    key_ids: dict[str, int]

    def auth(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[name]}"}


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("specivo.services.ai_usage_ingest_service.utcnow", lambda: NOW)
    monkeypatch.setattr("specivo.services.ai_usage_service.utcnow", lambda: NOW)


async def _role(db: AsyncSession, name: str, permissions: list[str]) -> Role:
    role = Role(name=name, permissions=permissions, builtin=0, issues_visibility="default", ai_usage_visibility="own")
    db.add(role)
    await db.flush()
    return role


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="Ingx Open")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Ingx Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Ingx Normal")
    p = ProjectFactory.build(key="INGX", identifier="ingx", is_public=False)
    q = ProjectFactory.build(key="INGY", identifier="ingy", is_public=False)
    db.add_all([tracker, priority, p, q])
    await db.flush()

    users: dict[str, User] = {
        "bot": ServiceAccountFactory.build(login="ingx-bot", status="active"),
        "bot2": ServiceAccountFactory.build(login="ingx-bot2", status="active"),
        "teammate": UserFactory.build(login="ingx-teammate", status="active"),
        "reader": UserFactory.build(login="ingx-reader", status="active"),
        "outsider": UserFactory.build(login="ingx-outsider", status="active"),
    }
    db.add_all(users.values())
    await db.flush()

    runner = await _role(db, "Ingx Runner", ["view_issues", "view_ai_usage", "log_ai_usage", "log_ai_usage_for_others"])
    logger_role = await _role(db, "Ingx Logger", ["view_issues", "view_ai_usage", "log_ai_usage"])
    viewer = await _role(db, "Ingx Viewer", ["view_issues"])
    for name in ("bot", "bot2"):
        await _member(db, p, users[name], runner)
        await _member(db, q, users[name], viewer)
    await _member(db, p, users["teammate"], logger_role)
    await _member(db, p, users["reader"], viewer)

    def issue(project: Project, number: int, **kw: Any) -> Issue:
        built: Issue = IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=users["teammate"].id,
            sequence_number=number,
            subject=f"Work item {number}",
            **kw,
        )
        return built

    issues = {n: issue(p, n) for n in (3, 7, 12)}
    issues[20] = issue(p, 20, is_private=True)
    q_issue = issue(q, 1)
    db.add_all([*issues.values(), q_issue])
    await db.flush()

    integration = await make_integration(db, p, repo="acme/app")
    commit = RepositoryCommit(integration_id=integration.id, sha=COMMIT_SHA, message="Add login form")
    db.add(commit)
    await db.flush()
    db.add(IssueCommitLink(commit_id=commit.id, issue_id=issues[3].id, source="message", keyword="refs"))

    keys: dict[str, str] = {}
    key_ids: dict[str, int] = {}
    service = ApiKeyService()
    for name in users:
        key, raw = await service.create_key(db, user_id=users[name].id, name=f"{name}-key", check_scope_access=False)
        keys[name], key_ids[name] = raw, key.id
    limited, raw = await service.create_key(
        db, user_id=users["bot"].id, name="bot-limited", scopes={"projects": [p.key]}, check_scope_access=False
    )
    keys["bot_limited"], key_ids["bot_limited"] = raw, limited.id
    mcp, _raw = await service.create_key(db, user_id=users["bot"].id, name="bot-mcp", check_scope_access=False)
    key_ids["bot_mcp"] = mcp.id
    mcp2, _raw = await service.create_key(db, user_id=users["teammate"].id, name="tm-mcp", check_scope_access=False)
    key_ids["teammate_mcp"] = mcp2.id
    await db.commit()
    clear_role_cache(db)
    return World(p=p, q=q, issues=issues, q_issue=q_issue, users=users, keys=keys, key_ids=key_ids)


def record(key: str, **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "tool": "claude-code",
        "model": "claude-sonnet-4-5",
        "usage_date": DAY,
        "tokens": {"input": 1000, "output": 200},
    }
    values.update(kw)
    return values


def batch(*records: dict[str, Any], **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "specivo.ai-usage.v1",
        "client": {"name": "ingest-tests", "version": "1.2.3"},
        "records": list(records),
    }
    body.update(kw)
    return body


async def post(client: AsyncClient, w: World, who: str, *records: dict[str, Any]) -> list[dict[str, Any]]:
    resp = await client.post(URL, json=batch(*records), headers=w.auth(who))
    assert resp.status_code == 200, resp.text
    results: list[dict[str, Any]] = resp.json()["results"]
    return results


async def entry(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    stmt = select(AiUsageEntry).where(AiUsageEntry.id == entry_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


def codes(result: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in result["warnings"]]


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


async def test_info_lists_versions_limits_tools_and_currencies(client: AsyncClient, w: World) -> None:
    resp = await client.get(URL, headers=w.auth("bot"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["versions"] == ["specivo.ai-usage.v1"]
    assert body["max_records"] == 500
    assert body["max_bytes"] == 1048576
    assert body["max_age_days"] == 400
    assert "claude-code" in body["tools"]
    assert {"USD", "THB"} <= set(body["currencies"])
    assert datetime.fromisoformat(body["server_time"]).tzinfo is not None

    assert (await client.get(URL + "/", headers=w.auth("bot"))).status_code == 200
    assert (await client.get(URL)).status_code == 401


# ---------------------------------------------------------------------------
# Upsert semantics
# ---------------------------------------------------------------------------


async def test_create_unchanged_update_replaces_totals(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    [created] = await post(client, w, "bot", record("k-basic"))
    assert created["status"] == "created"
    assert created["revision"] == 1
    assert created["error"] is None
    assert created["attribution"] == {"method": "none", "project_key": None, "issue_ref": None}

    [same] = await post(client, w, "bot", record("k-basic"))
    assert same["status"] == "unchanged"
    assert same["entry_id"] == created["entry_id"]
    assert same["revision"] == 1

    [updated] = await post(client, w, "bot", record("k-basic", tokens={"input": 1500, "output": 300}))
    assert updated["status"] == "updated"
    assert updated["revision"] == 2

    stored = await entry(db_session, created["entry_id"])
    assert (stored.input, stored.output) == (1500, 300)  # replaced, not added
    assert stored.source == "reporter"
    assert stored.user_id == stored.reporter_user_id == w.users["bot"].id
    assert stored.reporter_api_key_id == w.key_ids["bot"]
    assert (stored.client_name, stored.client_version) == ("ingest-tests", "1.2.3")


async def test_path_with_trailing_slash_works_too(client: AsyncClient, w: World) -> None:
    resp = await client.post(URL + "/", json=batch(record("k-slash")), headers=w.auth("bot"))
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "created"


async def test_summary_counts_each_status(client: AsyncClient, w: World) -> None:
    await post(client, w, "bot", record("k-sum-1"))
    resp = await client.post(
        URL,
        json=batch(record("k-sum-1"), record("k-sum-2"), record("k-sum-3", tokens={"input": -1})),
        headers=w.auth("bot"),
    )
    assert resp.json()["summary"] == {"received": 3, "created": 1, "updated": 0, "unchanged": 1, "rejected": 1}


async def test_key_of_another_reporter_is_key_conflict(client: AsyncClient, w: World) -> None:
    [first] = await post(
        client, w, "bot", record("k-shared", on_behalf_of="ingx-teammate", attribution={"project_key": "INGX"})
    )
    assert first["status"] == "created"
    [second] = await post(
        client, w, "bot2", record("k-shared", on_behalf_of="ingx-teammate", attribution={"project_key": "INGX"})
    )
    assert second["status"] == "rejected"
    assert second["error"]["code"] == "key_conflict"
    assert second["entry_id"] is None

    # The same key for another target user is another bucket.
    [own] = await post(client, w, "bot2", record("k-shared"))
    assert own["status"] == "created"


async def test_duplicate_key_in_one_batch_is_rejected(client: AsyncClient, w: World) -> None:
    results = await post(client, w, "bot", record("k-dup"), record("k-dup", tokens={"input": 5}))
    assert results[0]["status"] == "created"
    assert results[1]["error"]["code"] == "duplicate_key"


async def test_excluded_entry_stays_excluded(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    [created] = await post(client, w, "bot", record("k-excl"))
    stored = await entry(db_session, created["entry_id"])
    await AiUsageService().set_excluded(db_session, w.users["bot"], stored, True)
    await db_session.commit()

    [updated] = await post(client, w, "bot", record("k-excl", tokens={"input": 9999}))
    assert updated["status"] == "updated"
    stored = await entry(db_session, created["entry_id"])
    assert stored.excluded_at is not None
    assert stored.input == 9999


async def test_invalid_record_does_not_block_the_others(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        record("k-ok-1"),
        record("k-bad", started_at="2026-11-06T01:00:00Z"),
        record("k-ok-2"),
    )
    assert [r["status"] for r in results] == ["created", "rejected", "created"]
    assert results[1]["error"] == {
        "code": "invalid_record",
        "message": "started_at must fall on usage_date (a UTC day)",
        "field": "started_at",
    }


async def test_time_rules(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        # 23:30 on the 4th in Bangkok is 16:30 UTC on the 4th: another UTC day.
        record("k-t1", started_at="2026-11-04T23:30:00+07:00"),
        # 06:30 on the 5th in Bangkok is 23:30 UTC on the 4th.
        record("k-t2", started_at="2026-11-05T06:30:00+07:00"),
        record("k-t3", started_at="2026-11-05T07:30:00+07:00", ended_at="2026-11-06T00:00:00Z"),
        record("k-t4", ended_at="2026-11-06T00:00:01Z"),
        record("k-t5", usage_date="2026-11-07"),
        record("k-t6", usage_date="2025-10-01"),
        record("k-t7", usage_date="2025-10-03"),
    )
    assert [r["status"] for r in results] == [
        "rejected",
        "rejected",
        "created",
        "rejected",
        "rejected",
        "rejected",
        "created",
    ]


async def test_priced_at_is_started_at_in_utc(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    [with_start] = await post(client, w, "bot", record("k-p1", started_at="2026-11-05T09:10:00+07:00"))
    [without] = await post(client, w, "bot", record("k-p2"))
    assert (await entry(db_session, with_start["entry_id"])).priced_at == datetime(2026, 11, 5, 2, 10, tzinfo=UTC)
    assert (await entry(db_session, without["entry_id"])).priced_at == datetime(2026, 11, 5, tzinfo=UTC)


async def test_unknown_record_fields_are_ignored_with_a_warning(client: AsyncClient, w: World) -> None:
    [result] = await post(
        client, w, "bot", record("k-unk", future_field=1, tokens={"input": 1, "audio": 5}, attribution={"x": 1})
    )
    assert result["status"] == "created"
    assert codes(result) == ["unknown_fields"]
    assert result["warnings"][0]["message"].endswith("attribution.x, future_field, tokens.audio")


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


async def test_explicit_issue_wins_over_every_hint(client: AsyncClient, w: World) -> None:
    [result] = await post(
        client,
        w,
        "bot",
        record(
            "k-a-explicit",
            attribution={
                "issue_ref": "ingx-12",
                "git_branch": "feat/ingx-7-search",
                "commits": [COMMIT_SHA],
                "repo": "github.com/acme/app",
            },
        ),
    )
    assert result["attribution"] == {"method": "explicit", "project_key": "INGX", "issue_ref": "INGX-12"}


async def test_env_issue_is_recorded_as_env(client: AsyncClient, w: World) -> None:
    [result] = await post(
        client, w, "bot", record("k-a-env", attribution={"issue_ref": "INGX-7", "issue_ref_source": "env"})
    )
    assert result["attribution"]["method"] == "env"
    assert result["attribution"]["issue_ref"] == "INGX-7"


async def _session(
    db: AsyncSession, w: World, key: str, user: str, issue: Issue, start: datetime, end: datetime
) -> None:
    db.add(
        AgentSession(
            api_key_id=w.key_ids[key],
            user_id=w.users[user].id,
            issue_id=issue.id,
            started_at=start,
            last_activity_at=end,
        )
    )
    await db.commit()


async def test_mcp_session_issue_overlapping_the_window(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    await _session(
        db_session,
        w,
        "bot_mcp",
        "bot",
        w.issues[7],
        datetime(2026, 11, 5, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 5, 2, 0, tzinfo=UTC),
    )
    results = await post(
        client,
        w,
        "bot",
        record(
            "k-a-mcp",
            started_at="2026-11-05T01:30:00Z",
            ended_at="2026-11-05T03:00:00Z",
            attribution={"git_branch": "feat/ingx-12-other", "commits": [COMMIT_SHA]},
        ),
        # No overlap: the commit decides.
        record(
            "k-a-mcp-miss",
            started_at="2026-11-05T05:00:00Z",
            ended_at="2026-11-05T06:00:00Z",
            attribution={"commits": [COMMIT_SHA]},
        ),
        # No window at all: the session step is skipped.
        record("k-a-mcp-none"),
    )
    assert results[0]["attribution"] == {"method": "mcp_session", "project_key": "INGX", "issue_ref": "INGX-7"}
    assert results[1]["attribution"]["method"] == "commit"
    assert results[2]["attribution"]["method"] == "none"


async def test_on_behalf_session_prefers_the_reporter_then_the_target(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    start, end = datetime(2026, 11, 5, 1, 0, tzinfo=UTC), datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    await _session(db_session, w, "teammate_mcp", "teammate", w.issues[12], start, end)
    window = {"started_at": "2026-11-05T01:10:00Z", "ended_at": "2026-11-05T01:20:00Z"}

    [target_only] = await post(client, w, "bot", record("k-a-ob1", on_behalf_of="ingx-teammate", **window))
    assert target_only["attribution"]["issue_ref"] == "INGX-12"
    assert target_only["attribution"]["method"] == "mcp_session"

    await _session(db_session, w, "bot_mcp", "bot", w.issues[7], start, end)
    [both] = await post(client, w, "bot", record("k-a-ob2", on_behalf_of="ingx-teammate", **window))
    assert both["attribution"]["issue_ref"] == "INGX-7"


async def test_commit_then_branch_then_project_then_repo(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        record("k-a-c", attribution={"commits": [COMMIT_SHA.upper()], "git_branch": "feat/ingx-7-x"}),
        record("k-a-b", attribution={"git_branch": "INGX-7/search", "project_key": "INGX"}),
        record("k-a-p", attribution={"project_key": "ingx", "repo": "github.com/other/repo"}),
        record("k-a-r", attribution={"repo": "https://github.com/acme/app.git"}),
        record("k-a-n", attribution={"repo": "github.com/nobody/nothing", "git_branch": "main"}),
    )
    assert [(r["attribution"]["method"], r["attribution"]["issue_ref"]) for r in results] == [
        ("commit", "INGX-3"),
        ("branch", "INGX-7"),
        ("explicit", None),
        ("repo", None),
        ("none", None),
    ]
    assert all(r["attribution"]["project_key"] == "INGX" for r in results[:4])


async def test_explicit_targets_that_cannot_be_used_are_rejected(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        record("k-r1", attribution={"issue_ref": "INGY-1"}),  # visible, but no log permission there
        record("k-r2", attribution={"project_key": "INGY"}),
        record("k-r3", attribution={"issue_ref": "INGX-999"}),
        record("k-r4", attribution={"project_key": "NOPE"}),
        record("k-r5", attribution={"issue_ref": "INGX-20"}),  # private: not visible to the bot
        record("k-r6", attribution={"issue_ref": "INGX-7", "project_key": "INGY"}),
    )
    assert [r["status"] for r in results] == ["rejected"] * 6
    assert [r["error"]["code"] for r in results] == [
        "forbidden",
        "forbidden",
        "not_found",
        "not_found",
        "not_found",
        "attribution_invalid",
    ]
    assert results[2]["error"]["field"] == "attribution.issue_ref"
    assert results[3]["error"]["field"] == "attribution.project_key"


async def test_inferred_targets_that_cannot_be_used_are_dropped(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        # INGY-1 is in a project the bot reaches but cannot log to: dropped, then the repository is tried.
        record("k-d1", attribution={"git_branch": "feat/ingy-1-x", "repo": "github.com/acme/app"}),
        # Branch only, and dropped: unassigned.
        record("k-d2", attribution={"git_branch": "feat/ingy-1-x"}),
        # An inferred issue outside the named project is dropped; the project stays.
        record("k-d3", attribution={"git_branch": "feat/ingy-1-x", "project_key": "INGX"}),
    )
    assert results[0]["status"] == "created"
    assert results[0]["attribution"] == {"method": "repo", "project_key": "INGX", "issue_ref": None}
    assert codes(results[0]) == ["attribution_dropped"]
    assert "INGY-1" in results[0]["warnings"][0]["message"]

    assert results[1]["status"] == "created"
    assert results[1]["attribution"]["method"] == "none"
    assert codes(results[1]) == ["attribution_dropped"]

    assert results[2]["attribution"] == {"method": "explicit", "project_key": "INGX", "issue_ref": None}
    assert codes(results[2]) == ["attribution_dropped"]
    assert "not in project INGX" in results[2]["warnings"][0]["message"]


async def test_dropped_issue_the_reporter_cannot_see_is_not_named(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    commit = (await db_session.execute(select(RepositoryCommit))).scalars().first()
    assert commit is not None
    other = RepositoryCommit(integration_id=commit.integration_id, sha="ab" * 20, message="private work")
    db_session.add(other)
    await db_session.flush()
    db_session.add(IssueCommitLink(commit_id=other.id, issue_id=w.issues[20].id, source="message"))
    await db_session.commit()

    [result] = await post(client, w, "bot", record("k-hidden", attribution={"commits": ["ab" * 20]}))
    assert result["status"] == "created"
    assert result["attribution"]["method"] == "none"
    assert codes(result) == ["attribution_dropped"]
    assert "INGX-20" not in result["warnings"][0]["message"]


async def test_replay_keeps_the_stored_attribution(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    [first] = await post(client, w, "bot", record("k-keep", attribution={"project_key": "INGX"}))
    assert first["attribution"]["method"] == "explicit"

    # No attribution: kept, nothing changed.
    [bare] = await post(client, w, "bot", record("k-keep"))
    assert bare["status"] == "unchanged"
    assert bare["attribution"]["project_key"] == "INGX"

    # A different inferred target: not applied, warned.
    [inferred] = await post(client, w, "bot", record("k-keep", attribution={"git_branch": "feat/ingx-7-x"}))
    assert inferred["status"] == "updated"  # the branch is stored as context
    assert inferred["attribution"] == {"method": "explicit", "project_key": "INGX", "issue_ref": None}
    assert codes(inferred) == ["attribution_dropped"]

    # A different explicit target: refused.
    [other_project] = await post(client, w, "bot", record("k-keep", attribution={"project_key": "INGY"}))
    assert other_project["error"]["code"] == "attribution_conflict"
    [other_issue] = await post(client, w, "bot", record("k-keep", attribution={"issue_ref": "INGX-12"}))
    assert other_issue["error"]["code"] == "attribution_conflict"

    # The same explicit target again: fine.
    [same] = await post(
        client, w, "bot", record("k-keep", attribution={"project_key": "INGX", "git_branch": "feat/ingx-7-x"})
    )
    assert same["status"] == "unchanged"
    assert same["error"] is None


async def test_replay_attributes_an_unassigned_entry(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    [first] = await post(client, w, "bot", record("k-late"))
    assert first["attribution"]["method"] == "none"
    [second] = await post(client, w, "bot", record("k-late", attribution={"issue_ref": "INGX-3"}))
    assert second["status"] == "updated"
    assert second["attribution"] == {"method": "explicit", "project_key": "INGX", "issue_ref": "INGX-3"}
    stored = await entry(db_session, first["entry_id"])
    assert stored.issue_id == w.issues[3].id


# ---------------------------------------------------------------------------
# On behalf of another user
# ---------------------------------------------------------------------------


async def test_on_behalf_allowed_and_audited(client: AsyncClient, db_session: AsyncSession, w: World) -> None:
    refused, result = await post(
        client,
        w,
        "bot",
        # Rolled back in its savepoint; the target user it loaded is used again by the next record.
        record("k-ob-refused", on_behalf_of="ingx-teammate", attribution={"issue_ref": "INGY-1"}),
        record("k-ob", on_behalf_of="INGX-TEAMMATE", attribution={"issue_ref": "INGX-3"}),
    )
    assert refused["error"]["code"] == "forbidden"
    assert result["status"] == "created"
    stored = await entry(db_session, result["entry_id"])
    assert stored.user_id == w.users["teammate"].id
    assert stored.reporter_user_id == w.users["bot"].id
    events = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(
                    SecurityAuditLog.event_type == AuditEvent.AI_USAGE_LOGGED_FOR_OTHER.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        event.resource_id == str(result["entry_id"]) or event.details.get("entry_id") == result["entry_id"]
        for event in events
    )


async def test_on_behalf_refused(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot",
        record("k-obr1", on_behalf_of="ingx-outsider", attribution={"project_key": "INGX"}),
        record("k-obr2", on_behalf_of="ingx-teammate"),  # unassigned for someone else: administrators only
        record("k-obr3", on_behalf_of="no-such-login"),  # no project: told apart for administrators only
        record("k-obr6", on_behalf_of="no-such-login", attribution={"project_key": "INGX"}),
    )
    assert [r["error"]["code"] for r in results] == ["target_not_member", "forbidden", "forbidden", "target_unknown"]

    [teammate] = await post(
        client, w, "teammate", record("k-obr4", on_behalf_of="ingx-reader", attribution={"project_key": "INGX"})
    )
    assert teammate["error"]["code"] == "forbidden"

    [reader] = await post(client, w, "reader", record("k-obr5", attribution={"project_key": "INGX"}))
    assert reader["error"]["code"] == "forbidden"


def _rejection(result: dict[str, Any]) -> tuple[str, Any, Any]:
    """What a rejected record tells: status, error and warnings (the key aside)."""
    return result["status"], result["error"], result["warnings"]


async def test_on_behalf_without_rights_hides_which_logins_exist(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """A sender who may not log for others gets one answer for existing and unknown logins alike."""
    inactive = UserFactory.build(login="ingx-gone", status="deactivated")
    db_session.add(inactive)
    await db_session.commit()
    logins = ["ingx-reader", "ingx-outsider", "ingx-gone", "ingx-bot", "no-such-login"]
    for attribution in ({"project_key": "INGX"}, {"issue_ref": "INGX-3"}, None):
        extra = {"attribution": attribution} if attribution else {}
        results = await post(
            client,
            w,
            "teammate",
            *[record(f"k-enum-{i}", on_behalf_of=login, **extra) for i, login in enumerate(logins)],
        )
        answers = {json.dumps(_rejection(r), sort_keys=True) for r in results}
        assert len(answers) == 1, results
        assert results[0]["error"] == {
            "code": "forbidden",
            "message": "You cannot log AI usage for this user here",
            "field": "on_behalf_of",
        }
    # Content errors come first, the same for both.
    bad = {"usage_date": "2020-01-01"}
    known, unknown = await post(
        client,
        w,
        "teammate",
        record("k-enum-bad1", on_behalf_of="ingx-reader", **bad),
        record("k-enum-bad2", on_behalf_of="no-such-login", **bad),
    )
    assert _rejection(known) == _rejection(unknown)
    assert known["error"]["code"] == "invalid_record"


async def test_on_behalf_outside_the_senders_projects_hides_which_logins_exist(
    client: AsyncClient, db_session: AsyncSession, w: World
) -> None:
    """The bot may log for others in INGX only: in INGY and unassigned, targets are not told apart."""
    inactive = UserFactory.build(login="ingx-gone2", status="deactivated")
    db_session.add(inactive)
    await db_session.commit()
    logins = ["ingx-bot2", "ingx-teammate", "ingx-outsider", "ingx-gone2", "no-such-login"]
    for attribution in ({"project_key": "INGY"}, {"issue_ref": "INGY-1"}, None):
        extra = {"attribution": attribution} if attribution else {}
        results = await post(
            client,
            w,
            "bot",
            *[record(f"k-enum2-{i}", on_behalf_of=login, **extra) for i, login in enumerate(logins)],
        )
        answers = {json.dumps(_rejection(r), sort_keys=True) for r in results}
        assert len(answers) == 1, (attribution, results)
        assert results[0]["error"]["code"] == "forbidden"
    # In INGX the bot may: the codes differ there.
    results = await post(
        client,
        w,
        "bot",
        *[
            record(f"k-enum3-{i}", on_behalf_of=login, attribution={"project_key": "INGX"})
            for i, login in enumerate(["ingx-outsider", "ingx-gone2", "no-such-login"])
        ],
    )
    assert [r["error"]["code"] for r in results] == ["target_not_member", "target_invalid", "target_unknown"]


async def test_project_limited_key(client: AsyncClient, w: World) -> None:
    results = await post(
        client,
        w,
        "bot_limited",
        record("k-l1", attribution={"project_key": "INGX"}),
        record("k-l2", attribution={"issue_ref": "INGY-1"}),
        record("k-l3", attribution={"git_branch": "feat/ingy-1-x"}),
        record("k-l4"),
    )
    assert [r["status"] for r in results] == ["created", "rejected", "created", "created"]
    assert results[1]["error"]["code"] == "not_found"
    # Outside the key's limit INGY is not even a known key: no hint, no warning.
    assert results[2]["attribution"]["method"] == "none"
    assert results[2]["warnings"] == []


# ---------------------------------------------------------------------------
# Envelope and limits
# ---------------------------------------------------------------------------


async def test_envelope_errors(client: AsyncClient, w: World) -> None:
    async def send(content: bytes | str) -> Response:
        return await client.post(URL, content=content, headers={**w.auth("bot"), "Content-Type": "application/json"})

    resp = await send("{not json")
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["code"] == "invalid_json"

    resp = await send("[]")
    assert resp.json()["errors"][0]["code"] == "invalid_batch"

    resp = await send(json.dumps(batch(record("k-e"), schema="specivo.ai-usage.v2")))
    assert resp.status_code == 400
    error = resp.json()["errors"][0]
    assert error["code"] == "unsupported_schema"
    assert error["details"] == {"supported": ["specivo.ai-usage.v1"]}

    resp = await send(json.dumps(batch(record("k-e"), client={"version": "1"})))
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["code"] == "invalid_batch"

    resp = await send(json.dumps({"schema": "specivo.ai-usage.v1", "client": {"name": "x"}, "records": "nope"}))
    assert resp.status_code == 400

    resp = await send(json.dumps(batch(record("k-e"), extra_top_level=True)))
    assert resp.status_code == 200
    assert resp.json()["warnings"][0]["code"] == "unknown_fields"


async def test_batch_limits(client: AsyncClient, w: World) -> None:
    too_many = batch(*(record(f"k-many-{i}") for i in range(501)))
    resp = await client.post(URL, json=too_many, headers=w.auth("bot"))
    assert resp.status_code == 413
    assert resp.json()["errors"][0]["code"] == "batch_too_large"

    padded = json.dumps(batch(record("k-big"))) + " " * (1024 * 1024)
    resp = await client.post(URL, content=padded, headers={**w.auth("bot"), "Content-Type": "application/json"})
    assert resp.status_code == 413

    exactly = batch(*(record(f"k-max-{i}") for i in range(500)))
    resp = await client.post(URL, json=exactly, headers=w.auth("bot"))
    assert resp.status_code == 200
    assert resp.json()["summary"]["created"] == 500


async def test_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post(URL, json=batch(record("k-anon")))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# The published examples
# ---------------------------------------------------------------------------


def _example(path: Path) -> str:
    """The example with this module's project key and logins (the examples say ACME and teammate)."""
    text = path.read_text()
    return text.replace("ACME", "INGX").replace("acme-", "ingx-").replace('"teammate"', '"ingx-teammate"')


async def test_valid_examples_replay_through_the_endpoint(client: AsyncClient, w: World) -> None:
    expected = json.loads((EXAMPLES / "expected.json").read_text())["valid"]
    assert sorted(expected) == sorted(p.name for p in (EXAMPLES / "valid").glob("*.json"))
    for name, outcomes in expected.items():
        content = _example(EXAMPLES / "valid" / name)
        resp = await client.post(URL, content=content, headers={**w.auth("bot"), "Content-Type": "application/json"})
        assert resp.status_code == 200, (name, resp.text)
        results = resp.json()["results"]
        got = [
            {
                "status": r["status"],
                "method": r["attribution"]["method"] if r["attribution"] else None,
                "issue_ref": r["attribution"]["issue_ref"] if r["attribution"] else None,
                "project_key": r["attribution"]["project_key"] if r["attribution"] else None,
            }
            for r in results
        ]
        want = [
            {
                **o,
                "issue_ref": o["issue_ref"] and o["issue_ref"].replace("ACME", "INGX"),
                "project_key": o["project_key"] and o["project_key"].replace("ACME", "INGX"),
            }
            for o in outcomes
        ]
        assert got == want, (name, results)

        replay = await client.post(URL, content=content, headers={**w.auth("bot"), "Content-Type": "application/json"})
        assert [r["status"] for r in replay.json()["results"]] == ["unchanged"] * len(outcomes), name


async def test_invalid_examples_get_the_documented_answers(client: AsyncClient, w: World) -> None:
    expected = json.loads((EXAMPLES / "expected.json").read_text())["invalid"]
    assert sorted(expected) == sorted(p.name for p in (EXAMPLES / "invalid").glob("*.json"))
    for name, outcome in expected.items():
        content = _example(EXAMPLES / "invalid" / name)
        resp = await client.post(URL, content=content, headers={**w.auth("bot"), "Content-Type": "application/json"})
        assert resp.status_code == outcome["http_status"], (name, resp.text)
        if outcome["http_status"] != 200:
            assert resp.json()["errors"][0]["code"] == outcome["code"], name
            continue
        results = resp.json()["results"]
        assert [r["status"] for r in results] == ["rejected"] * len(outcome["records"]), name
        assert [(r["error"]["code"], r["error"]["field"]) for r in results] == [
            (o["code"], o["field"]) for o in outcome["records"]
        ], name
