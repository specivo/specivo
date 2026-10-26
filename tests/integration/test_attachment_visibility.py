"""Attachments are only reachable by readers who may see their container.

Two paths are pinned here:

- the filename → URL map a wiki page resolves bare image references against
  (``AttachmentService.build_project_attachment_map``) holds only the
  attachments the reader may open: wiki-page files when the reader can view
  the wiki, issue files only for issues the reader can see;
- the attachment endpoints (metadata, download, update, delete) answer a
  hidden attachment exactly like a missing one, with 404.

Issue visibility is the same predicate as the project issue list, so private
issues and roles limited to own issues are covered by both paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services import attachment_service as attachment_module
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.attachment_service import AttachmentService
from specivo.services.permission_service import clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = AttachmentService()
_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class World:
    project: Project
    admin: User
    author: User
    member: User
    own_member: User
    outsider: User
    private_issue: Issue
    shared_issue: Issue
    authors_open_issue: Issue
    wiki_slug: str
    secret: Attachment
    roadmap: Attachment
    diagram: Attachment
    tie_visible: Attachment
    tie_hidden: Attachment
    private_note_file: Attachment


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


def _attachment(container_type: str, container_id: int, filename: str, author: User, minutes: int) -> Attachment:
    return Attachment(
        container_type=container_type,
        container_id=container_id,
        filename=filename,
        disk_filename=f"amv-{container_type.lower()}-{container_id}-{minutes}-{filename}",
        content_type="image/png",
        filesize=4,
        author_id=author.id,
        created_at=_BASE_TIME + timedelta(minutes=minutes),
    )


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="AMV New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="AMV Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="AMV Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()

    project = ProjectFactory.build(key="AMV", identifier="attachment-map-visibility", is_public=False)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))

    admin = AdminUserFactory.build(login="amv_admin", status="active")
    author = UserFactory.build(login="amv_author", status="active")
    member = UserFactory.build(login="amv_member", status="active")
    own_member = UserFactory.build(login="amv_own", status="active")
    outsider = UserFactory.build(login="amv_outsider", status="active")
    db.add_all([admin, author, member, own_member, outsider])
    await db.flush()

    reader_perms = ["view_issues", "add_issues", "view_wiki"]
    developer = Role(name="AMV Developer", permissions=reader_perms, builtin=0, issues_visibility="default")
    own_only = Role(name="AMV Own only", permissions=reader_perms, builtin=0, issues_visibility="own")
    db.add_all([developer, own_only])
    await db.flush()
    await _member(db, project, author, developer)
    await _member(db, project, member, developer)
    await _member(db, project, own_member, own_only)

    def issue(seq: int, issue_author: User, *, private: bool, subject: str) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=seq,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=issue_author.id,
            subject=subject,
            is_private=private,
        )

    private_issue = issue(1, author, private=True, subject="Private budget")
    shared_issue = issue(2, member, private=False, subject="Shared charts")
    authors_open_issue = issue(3, author, private=False, subject="Open roadmap")
    db.add_all([private_issue, shared_issue, authors_open_issue])
    await db.flush()

    page, _content = await WikiService().create_page(
        db,
        project.id,
        "Charts",
        "![secret](secret.png) ![roadmap](roadmap.png) ![diagram](diagram.png) ![tie](tie.png)",
        admin,
        skip_search_index=True,
    )

    private_note = Journal(
        issue_id=shared_issue.id,
        project_id=project.id,
        user_id=author.id,
        sequence=1,
        notes="Internal note",
        is_private=True,
    )
    db.add(private_note)
    await db.flush()

    secret = _attachment("Issue", private_issue.id, "secret.png", author, 1)
    roadmap = _attachment("Issue", authors_open_issue.id, "roadmap.png", author, 2)
    diagram = _attachment("WikiPage", page.id, "diagram.png", admin, 3)
    # Same filename twice: the older copy is visible to every member, the newer
    # one sits on the private issue.
    tie_visible = _attachment("Issue", shared_issue.id, "tie.png", member, 4)
    tie_hidden = _attachment("Issue", private_issue.id, "tie.png", author, 5)
    private_note_file = _attachment("Journal", private_note.id, "note.png", author, 6)
    db.add_all([secret, roadmap, diagram, tie_visible, tie_hidden, private_note_file])
    await db.commit()
    clear_role_cache(db)

    return World(
        project=project,
        admin=admin,
        author=author,
        member=member,
        own_member=own_member,
        outsider=outsider,
        private_issue=private_issue,
        shared_issue=shared_issue,
        authors_open_issue=authors_open_issue,
        wiki_slug=page.slug,
        secret=secret,
        roadmap=roadmap,
        diagram=diagram,
        tie_visible=tie_visible,
        tie_hidden=tie_hidden,
        private_note_file=private_note_file,
    )


@pytest_asyncio.fixture
async def files_on_disk(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write every attachment of *world* to a temporary upload directory.

    With the bytes present, a 404 from a download can only come from the
    visibility check, not from a missing file.
    """
    monkeypatch.setattr(attachment_module, "_upload_dir", tmp_path)
    for att in (
        world.secret,
        world.roadmap,
        world.diagram,
        world.tie_visible,
        world.tie_hidden,
        world.private_note_file,
    ):
        (tmp_path / att.disk_filename).write_bytes(b"\x89PNG")


async def _token(client: AsyncClient, user: User) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _url(att: Attachment) -> str:
    return f"/api/v1/attachments/{att.id}/download/"


# ---------------------------------------------------------------------------
# The filename map
# ---------------------------------------------------------------------------


async def test_private_issue_file_is_mapped_for_its_author_and_admins_only(
    db_session: AsyncSession, world: World
) -> None:
    for reader in (world.author, world.admin):
        att_map = await _svc.build_project_attachment_map(db_session, world.project, reader)
        assert att_map["secret.png"] == _url(world.secret), reader.login

    att_map = await _svc.build_project_attachment_map(db_session, world.project, world.member)
    assert "secret.png" not in att_map
    assert _url(world.secret) not in att_map.values()


async def test_role_limited_to_own_issues_maps_only_own_files(db_session: AsyncSession, world: World) -> None:
    member_map = await _svc.build_project_attachment_map(db_session, world.project, world.member)
    assert member_map["roadmap.png"] == _url(world.roadmap)

    own_map = await _svc.build_project_attachment_map(db_session, world.project, world.own_member)
    assert "roadmap.png" not in own_map
    assert "tie.png" not in own_map
    # The wiki itself stays readable for that role.
    assert own_map["diagram.png"] == _url(world.diagram)


async def test_same_filename_resolves_to_the_newest_visible_file(db_session: AsyncSession, world: World) -> None:
    member_map = await _svc.build_project_attachment_map(db_session, world.project, world.member)
    assert member_map["tie.png"] == _url(world.tie_visible)

    for reader in (world.author, world.admin):
        att_map = await _svc.build_project_attachment_map(db_session, world.project, reader)
        assert att_map["tie.png"] == _url(world.tie_hidden), reader.login


async def test_wiki_page_file_resolves_for_every_wiki_reader(db_session: AsyncSession, world: World) -> None:
    for reader in (world.admin, world.author, world.member, world.own_member):
        att_map = await _svc.build_project_attachment_map(db_session, world.project, reader)
        assert att_map["diagram.png"] == _url(world.diagram), reader.login


async def test_reader_without_wiki_or_issue_access_gets_an_empty_map(db_session: AsyncSession, world: World) -> None:
    assert await _svc.build_project_attachment_map(db_session, world.project, world.outsider) == {}


async def test_anonymous_reader_gets_an_empty_map(db_session: AsyncSession, world: World) -> None:
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    assert await _svc.build_project_attachment_map(db_session, world.project, anonymous) == {}


async def test_wiki_page_renders_only_visible_images(client: AsyncClient, world: World) -> None:
    page_url = f"/projects/{world.project.key}/wiki/{world.wiki_slug}/"

    member_token = await _token(client, world.member)
    resp = await client.get(page_url, cookies={"access_token": member_token})
    assert resp.status_code == 200, resp.text
    assert _url(world.diagram) in resp.text
    assert _url(world.tie_visible) in resp.text
    assert _url(world.secret) not in resp.text
    assert _url(world.tie_hidden) not in resp.text

    author_token = await _token(client, world.author)
    resp = await client.get(page_url, cookies={"access_token": author_token})
    assert resp.status_code == 200, resp.text
    assert _url(world.secret) in resp.text
    assert _url(world.tie_hidden) in resp.text


# ---------------------------------------------------------------------------
# The attachment endpoints
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on_disk")
async def test_hidden_attachment_answers_like_a_missing_one(client: AsyncClient, world: World) -> None:
    token = await _token(client, world.member)
    headers = {"Authorization": f"Bearer {token}"}

    missing = await client.get("/api/v1/attachments/999999999/download/", headers=headers)
    assert missing.status_code == 404

    for att in (world.secret, world.tie_hidden, world.private_note_file):
        for resp in (
            await client.get(_url(att), headers=headers),
            await client.get(f"/api/v1/attachments/{att.id}/", headers=headers),
            await client.patch(f"/api/v1/attachments/{att.id}/", json={"description": "x"}, headers=headers),
            await client.delete(f"/api/v1/attachments/{att.id}/", headers=headers),
        ):
            assert resp.status_code == 404, (att.filename, resp.request.method, resp.text)
            assert resp.json()["errors"][0]["code"] == missing.json()["errors"][0]["code"]


@pytest.mark.usefixtures("files_on_disk")
async def test_role_limited_to_own_issues_cannot_download_others_files(client: AsyncClient, world: World) -> None:
    token = await _token(client, world.own_member)
    resp = await client.get(_url(world.roadmap), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


@pytest.mark.usefixtures("files_on_disk")
async def test_visible_attachment_still_downloads(client: AsyncClient, world: World) -> None:
    author_token = await _token(client, world.author)
    resp = await client.get(_url(world.secret), headers={"Authorization": f"Bearer {author_token}"})
    assert resp.status_code == 200, resp.text
    assert resp.content == b"\x89PNG"

    member_token = await _token(client, world.member)
    resp = await client.get(_url(world.diagram), headers={"Authorization": f"Bearer {member_token}"})
    assert resp.status_code == 200, resp.text


async def test_upload_to_a_hidden_issue_is_refused(client: AsyncClient, world: World) -> None:
    token = await _token(client, world.member)
    resp = await client.post(
        "/api/v1/attachments/",
        files={"file": ("probe.txt", b"probe", "text/plain")},
        data={"container_type": "Issue", "container_id": str(world.private_issue.id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text
