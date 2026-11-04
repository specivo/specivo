"""Sprint start and completion on each issue's activity tab, journal API and MCP history.

Starting a sprint writes a "Sprint «name» started" line on every issue in it;
completing it writes "Sprint «name» completed" on the issues that stay, and a
normal sprint change, by name, on the issues it moves. Each line is named
only for a reader who may open the sprint's project, links to the sprint for
a signed-in reader, and reaches only those who may read the issue.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError
from specivo.mcp.tools import _complete_sprint, _create_issue, _list_comments, _start_sprint, _update_issue
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.sprint import Sprint
from specivo.services.permission_service import clear_role_cache
from specivo.services.sprint_service import SprintService
from tests.factories.lookups import DoneStatusFactory
from tests.integration.test_issue_visibility_listings import World, _bearer, _cookie, _issue, build_world, go_live

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@dataclass
class Board:
    w: World
    gamma: Sprint
    delta: Sprint
    finished: Issue
    pending: Issue
    hidden: Issue


@pytest_asyncio.fixture
async def board(db_session: AsyncSession) -> Board:
    """Sprint Gamma of project P (planned) with a done issue, an open one and a private one; Delta planned."""
    clear_role_cache(db_session)
    w = await build_world(db_session, "SJR")
    db = db_session
    done = DoneStatusFactory.build(name="SJR Done", position=9, category="done")
    db.add(done)
    await db.flush()
    gamma = Sprint(project_id=w.p.id, name="Sprint Gamma", status="planned")
    delta = Sprint(project_id=w.p.id, name="Sprint Delta", status="planned")
    # The world's own sprint is active; this one is a finished one, so Gamma may start.
    db.add_all([gamma, delta])
    await db.flush()
    w.sprint.status = "completed"
    lookups = await _lookups(db, w.pub)
    finished = await _issue(db, w.p, lookups, w.manager, "Write the release notes", sprint=gamma)
    finished.status_id = done.id
    pending = await _issue(db, w.p, lookups, w.manager, "Fix the login form", sprint=gamma)
    hidden = await _issue(db, w.p, lookups, w.manager, "Rotate the signing keys", sprint=gamma, is_private=True)
    await db.commit()
    yield Board(w=w, gamma=gamma, delta=delta, finished=finished, pending=pending, hidden=hidden)
    clear_role_cache(db_session)


async def _lookups(db: AsyncSession, issue: Issue) -> tuple:
    from specivo.models.lookups import IssuePriority, IssueStatus, Tracker

    return (
        await db.get(Tracker, issue.tracker_id),
        await db.get(IssueStatus, issue.status_id),
        await db.get(IssuePriority, issue.priority_id),
    )


async def _run_sprint(client: AsyncClient, b: Board) -> None:
    headers = await _bearer(client, b.w.manager)
    base = f"/api/v1/projects/{b.w.p.key}/sprints"
    resp = await client.post(f"{base}/{b.gamma.id}/start/", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"{base}/{b.gamma.id}/complete/", headers=headers, json={"move_incomplete_to_sprint_id": b.delta.id}
    )
    assert resp.status_code == 200, resp.text


async def _journals(client: AsyncClient, key: str, headers: dict[str, str]) -> tuple[int, list[dict]]:
    resp = await client.get(f"/api/v1/issues/{key}/", params={"include": "journals"}, headers=headers)
    return resp.status_code, (resp.json().get("journals") or []) if resp.status_code == 200 else []


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Activity tab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("partial", [False, True], ids=["page", "partial"])
async def test_activity_shows_started_and_completed_lines_with_a_sprint_link(
    client: AsyncClient, board: Board, partial: bool
) -> None:
    b = board
    await _run_sprint(client, b)
    key = b.finished.display_key
    url = f"/partials/issues/{key}/activity/" if partial else f"/issue/{key}/"

    resp = await client.get(url, cookies=_cookie(b.w.manager))
    assert resp.status_code == 200, resp.text
    text = _flat(resp.text)

    link = f'«<a href="/projects/{b.w.p.key}/sprints/{b.gamma.id}/" style="color:var(--sp-accent);">Sprint Gamma</a>»'
    assert f'<span class="sp-journal-sprint" data-sprint-event="started"> Sprint {link} started </span>' in text
    assert f'<span class="sp-journal-sprint" data-sprint-event="completed"> Sprint {link} completed </span>' in text


async def test_moved_issue_shows_sprint_names_not_ids(client: AsyncClient, board: Board) -> None:
    b = board
    await _run_sprint(client, b)

    resp = await client.get(f"/issue/{b.pending.display_key}/", cookies=_cookie(b.w.manager))
    assert resp.status_code == 200
    text = _flat(resp.text)
    assert (
        '<strong class="sp-change-label">Sprint</strong> changed from <span class="sp-change-old">Sprint Gamma</span> '
        'to <span class="sp-change-new">Sprint Delta</span>'
    ) in text
    assert 'data-sprint-event="completed"' not in text


async def test_anonymous_sees_the_lines_without_a_link(
    client: AsyncClient, db_session: AsyncSession, board: Board
) -> None:
    b = board
    await _run_sprint(client, b)
    url = f"/issue/{b.finished.display_key}/"
    signed_in = _flat((await client.get(url, cookies=_cookie(b.w.reporter))).text)
    await go_live(db_session, b.w)

    resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    text = _flat(resp.text)
    assert '<span class="sp-journal-sprint" data-sprint-event="started"> Sprint «Sprint Gamma» started </span>' in text
    assert (
        '<span class="sp-journal-sprint" data-sprint-event="completed"> Sprint «Sprint Gamma» completed </span>'
    ) in text
    assert f"/sprints/{b.gamma.id}/" not in text
    # The signed-in reporter is shown the same lines, linked.
    assert f'href="/projects/{b.w.p.key}/sprints/{b.gamma.id}/"' in signed_in


async def test_private_issue_entries_reach_only_its_readers(client: AsyncClient, board: Board) -> None:
    b = board
    await _run_sprint(client, b)
    key = b.hidden.display_key

    status, _ = await _journals(client, key, await _bearer(client, b.w.reporter))
    assert status == 404
    page = await client.get(f"/partials/issues/{key}/activity/", cookies=_cookie(b.w.reporter))
    assert page.status_code in (403, 404)
    assert "Sprint Gamma" not in page.text

    status, journals = await _journals(client, key, await _bearer(client, b.w.manager))
    assert status == 200
    details = [d for j in journals for d in j["details"]]
    assert {(d["property"], d["prop_key"]) for d in details} == {("sprint", "started"), ("attr", "sprint_id")}


async def test_sprint_line_naming_an_unreadable_project_is_left_out(
    client: AsyncClient, db_session: AsyncSession, board: Board
) -> None:
    """A sprint event whose sprint the reader may not open (the issue moved since) is not shown, nor its stored name."""
    b = board
    db = db_session
    elsewhere = Sprint(project_id=b.w.s.id, name="Closed door sprint", status="completed")
    db.add(elsewhere)
    await db.flush()
    journal = Journal(issue_id=b.pending.id, project_id=b.w.p.id, user_id=b.w.manager.id, sequence=1)
    db.add(journal)
    await db.flush()
    db.add(
        JournalDetail(
            journal_id=journal.id,
            property="sprint",
            prop_key="completed",
            old_value=str(elsewhere.id),
            new_value="Closed door sprint",
        )
    )
    await db.commit()

    key = b.pending.display_key
    page = await client.get(f"/issue/{key}/", cookies=_cookie(b.w.reporter))
    assert page.status_code == 200
    assert "Closed door sprint" not in page.text
    assert 'data-sprint-event="completed"' not in page.text
    status, journals = await _journals(client, key, await _bearer(client, b.w.reporter))
    assert status == 200
    assert journals and "Closed door sprint" not in str(journals)
    # The manager may open project S, and is shown it.
    page = await client.get(f"/issue/{key}/", cookies=_cookie(b.w.manager))
    assert "Closed door sprint" in page.text


async def test_journal_api_names_the_sprint(client: AsyncClient, board: Board) -> None:
    b = board
    await _run_sprint(client, b)

    status, journals = await _journals(client, b.finished.display_key, await _bearer(client, b.w.reporter))
    assert status == 200
    sprint_details = [d for j in journals for d in j["details"] if d["property"] == "sprint"]
    assert [(d["prop_key"], d["old_value"], d["old_name"], d["new_name"]) for d in sprint_details] == [
        ("started", str(b.gamma.id), "Sprint Gamma", None),
        ("completed", str(b.gamma.id), "Sprint Gamma", None),
    ]
    assert all(j["user"]["id"] == b.w.manager.id for j in journals)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def test_mcp_issue_tools_refuse_a_completed_sprint(db_session: AsyncSession, board: Board) -> None:
    from specivo.core.exceptions import AppError

    b = board
    with pytest.raises(AppError, match="Cannot assign issues to a completed sprint"):
        await _create_issue(
            db_session, b.w.manager, b.w.p.key, b.w.pub.tracker_id, "Late addition", sprint_id=b.w.sprint.id
        )
    with pytest.raises(AppError, match="Cannot assign issues to a completed sprint"):
        await _update_issue(db_session, b.w.manager, b.w.pub_backlog.display_key, sprint_id=b.w.sprint.id)


async def test_mcp_complete_with_a_bad_target_changes_nothing(db_session: AsyncSession, board: Board) -> None:
    from specivo.core.exceptions import AppError

    b = board
    await _start_sprint(db_session, b.w.manager, b.w.p.key, b.gamma.id)
    await db_session.commit()
    manager, key, gamma_id = b.w.manager, b.w.p.key, b.gamma.id

    for target in (b.w.sprint.id, b.gamma.id, 987654321):
        with pytest.raises(AppError) as exc:
            await _complete_sprint(db_session, manager, key, gamma_id, target)
        assert exc.value.status_code == 422

    await db_session.refresh(b.gamma)
    await db_session.refresh(b.pending)
    assert b.gamma.status == "active"
    assert b.pending.sprint_id == b.gamma.id


async def test_mcp_lifecycle_journals_as_the_caller_and_lists_them(db_session: AsyncSession, board: Board) -> None:
    b = board
    await _start_sprint(db_session, b.w.manager, b.w.p.key, b.gamma.id)
    await _complete_sprint(db_session, b.w.manager, b.w.p.key, b.gamma.id, None)
    await db_session.commit()

    finished = await _list_comments(db_session, b.w.manager, b.finished.display_key, order="asc", include_changes=True)
    assert f"sprint Sprint Gamma (id {b.gamma.id}) started" in finished
    assert f"sprint Sprint Gamma (id {b.gamma.id}) completed" in finished
    assert b.w.manager.display_name in finished
    pending = await _list_comments(db_session, b.w.manager, b.pending.display_key, order="asc", include_changes=True)
    assert f"sprint_id: Sprint Gamma (id {b.gamma.id}) -> (none)" in pending


# ---------------------------------------------------------------------------
# An issue edited while its sprint is being completed
# ---------------------------------------------------------------------------


def _edit_during_completion(monkeypatch: pytest.MonkeyPatch, issue_id: int) -> None:
    """Change *issue_id*'s row after ``complete_sprint`` has loaded it, as a concurrent edit would."""
    load = SprintService._issues_in

    async def racing(self: SprintService, session: AsyncSession, sprint: Sprint) -> list[Issue]:
        issues = await load(self, session, sprint)
        await session.execute(
            text("UPDATE issues SET lock_version = lock_version + 1 WHERE id = :id"), {"id": issue_id}
        )
        return issues

    monkeypatch.setattr(SprintService, "_issues_in", racing)


async def test_edit_during_completion_is_a_conflict_and_retry_works(
    client: AsyncClient, db_session: AsyncSession, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = board
    gamma_id, pending_id = b.gamma.id, b.pending.id
    headers = await _bearer(client, b.w.manager)
    base = f"/api/v1/projects/{b.w.p.key}/sprints/{gamma_id}"
    assert (await client.post(f"{base}/start/", headers=headers)).status_code == 200

    with monkeypatch.context() as patch:
        _edit_during_completion(patch, pending_id)
        resp = await client.post(f"{base}/complete/", headers=headers, json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["errors"][0]["code"] == "conflict_lock_version"

    db_session.expire_all()
    assert (await db_session.get(Sprint, gamma_id)).status == "active"
    assert (await db_session.get(Issue, pending_id)).sprint_id == gamma_id

    assert (await client.post(f"{base}/complete/", headers=headers, json={})).status_code == 200


async def test_edit_during_completion_is_a_conflict_over_mcp(
    db_session: AsyncSession, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = board
    await _start_sprint(db_session, b.w.manager, b.w.p.key, b.gamma.id)
    _edit_during_completion(monkeypatch, b.pending.id)

    with pytest.raises(ConflictError):
        await _complete_sprint(db_session, b.w.manager, b.w.p.key, b.gamma.id, None)
    await db_session.rollback()
