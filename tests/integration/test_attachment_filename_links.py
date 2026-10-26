"""Bare attachment filenames resolve to download links where the reader may open the file.

``![chart](chart.png)`` and ``[the spec](spec.pdf)`` are rewritten to the
attachment's download URL by the shared Markdown renderer. What changes per
page is the filename map it is given:

- an issue's description, its comments and its description history resolve
  against that issue's own attachments (``build_issue_attachment_map``), so a
  file on another issue never resolves there;
- a wiki page resolves against the project-wide map of attachments the reader
  may open (``build_project_attachment_map``);
- the editor preview resolves like the page the text is stored on, given the
  container in the request, and resolves nothing without one.

Every map holds only files the reader may open: a file on a private note is
for admins, a file on a hidden issue for those who can see that issue, and an
anonymous visitor gets no attachment links at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.attachment_service import AttachmentService
from specivo.services.permission_service import Permission, clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = AttachmentService()
_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_PREVIEW_URL = "/api/v1/markdown/preview/"

# Every kind of target the renderer must leave alone, plus one unknown name.
_UNTOUCHED = (
    "[missing](missing.pdf) "
    "[external](https://example.com/spec.pdf) "
    "[path](docs/spec.pdf) "
    "[anchor](#spec.pdf) "
    "[mail](mailto:team@example.com)"
)
_UNTOUCHED_HREFS = (
    'href="missing.pdf"',
    'href="https://example.com/spec.pdf"',
    'href="docs/spec.pdf"',
    'href="#spec.pdf"',
    'href="mailto:team@example.com"',
)

DESCRIPTION = (
    "Read [the spec](spec.pdf) and see ![chart](chart.png).\n\n"
    "Spaces: [notes](<release notes.pdf>), encoded: [notes again](release%20notes.pdf).\n\n"
    "Elsewhere: [other](other.pdf), private note: [note](note.pdf).\n\n" + _UNTOUCHED
)
COMMENT = "The log is [here](crash.log) and the screen ![shot](shot.png)."
WIKI_TEXT = "Wiki: [the spec](spec.pdf) ![chart](chart.png) [budget](budget.pdf) [diagram](diagram.pdf)."


@dataclass
class World:
    project: Project
    admin: User
    author: User
    member: User
    issue: Issue
    other_issue: Issue
    private_issue: Issue
    wiki_slug: str
    description_journal: Journal
    files: dict[str, Attachment]


def _url(att: Attachment) -> str:
    return f"/api/v1/attachments/{att.id}/download/"


async def _member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    db = db_session
    status = StatusFactory.build(name="AFL New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="AFL Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="AFL Normal", position=1)
    db.add_all([tracker, priority])
    await db.flush()

    # Public, so the anonymous tests can opt it in.
    project = ProjectFactory.build(key="AFL", identifier="attachment-filename-links", is_public=True)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))

    admin = AdminUserFactory.build(login="afl_admin", status="active")
    author = UserFactory.build(login="afl_author", status="active")
    member = UserFactory.build(login="afl_member", status="active")
    db.add_all([admin, author, member])
    await db.flush()

    role = Role(
        name="AFL Developer",
        permissions=["view_issues", "add_issues", "edit_issues", "add_notes", "view_wiki"],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    await _member(db, project, author, role)
    await _member(db, project, member, role)

    def issue(seq: int, *, private: bool, description: str) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            sequence_number=seq,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=author.id,
            subject=f"AFL issue {seq}",
            description=description,
            is_private=private,
        )

    main_issue = issue(1, private=False, description=DESCRIPTION)
    other_issue = issue(2, private=False, description="Has its own file.")
    private_issue = issue(3, private=True, description="Budget lives here.")
    db.add_all([main_issue, other_issue, private_issue])
    await db.flush()

    page, _content = await WikiService().create_page(db, project.id, "Links", WIKI_TEXT, admin, skip_search_index=True)

    comment = Journal(issue_id=main_issue.id, project_id=project.id, user_id=author.id, sequence=1, notes=COMMENT)
    private_note = Journal(
        issue_id=main_issue.id,
        project_id=project.id,
        user_id=author.id,
        sequence=2,
        notes="Internal note.",
        is_private=True,
    )
    description_journal = Journal(issue_id=main_issue.id, project_id=project.id, user_id=author.id, sequence=3)
    db.add_all([comment, private_note, description_journal])
    await db.flush()
    db.add(
        JournalDetail(
            journal_id=description_journal.id,
            property="attr",
            prop_key="description",
            old_value="Earlier text.",
            new_value=DESCRIPTION,
        )
    )

    specs = [
        ("Issue", main_issue.id, "spec.pdf", "application/pdf"),
        ("Issue", main_issue.id, "chart.png", "image/png"),
        ("Issue", main_issue.id, "release notes.pdf", "application/pdf"),
        ("Issue", main_issue.id, "shot.png", "image/png"),
        ("Journal", comment.id, "crash.log", "text/plain"),
        ("Journal", private_note.id, "note.pdf", "application/pdf"),
        ("Issue", other_issue.id, "other.pdf", "application/pdf"),
        ("Issue", private_issue.id, "budget.pdf", "application/pdf"),
        ("WikiPage", page.id, "diagram.pdf", "application/pdf"),
    ]
    files: dict[str, Attachment] = {}
    for minutes, (container_type, container_id, filename, content_type) in enumerate(specs):
        att = Attachment(
            container_type=container_type,
            container_id=container_id,
            filename=filename,
            disk_filename=f"afl-{minutes}-{filename}",
            content_type=content_type,
            filesize=4,
            author_id=author.id,
            created_at=_BASE_TIME + timedelta(minutes=minutes),
        )
        db.add(att)
        files[filename] = att
    await db.commit()
    clear_role_cache(db)

    return World(
        project=project,
        admin=admin,
        author=author,
        member=member,
        issue=main_issue,
        other_issue=other_issue,
        private_issue=private_issue,
        wiki_slug=page.slug,
        description_journal=description_journal,
        files=files,
    )


async def _token(client: AsyncClient, user: User) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _page(client: AsyncClient, path: str, user: User | None) -> str:
    cookies = {"access_token": await _token(client, user)} if user else None
    resp = await client.get(path, cookies=cookies)
    assert resp.status_code == 200, resp.text
    return resp.text


def _issue_path(world: World) -> str:
    return f"/issue/{world.project.key}-{world.issue.sequence_number}/"


def _assert_untouched(html: str) -> None:
    for href in _UNTOUCHED_HREFS:
        assert href in html, href


# ---------------------------------------------------------------------------
# The per-issue map
# ---------------------------------------------------------------------------


async def test_issue_map_holds_only_that_issues_files(db_session: AsyncSession, world: World) -> None:
    att_map = await _svc.build_issue_attachment_map(db_session, world.issue.id, world.member)
    assert set(att_map) == {"spec.pdf", "chart.png", "release notes.pdf", "shot.png", "crash.log"}
    assert att_map["spec.pdf"] == _url(world.files["spec.pdf"])


async def test_private_note_file_is_mapped_for_admins_only(db_session: AsyncSession, world: World) -> None:
    for reader in (world.member, world.author):
        att_map = await _svc.build_issue_attachment_map(db_session, world.issue.id, reader)
        assert "note.pdf" not in att_map, reader.login
    admin_map = await _svc.build_issue_attachment_map(db_session, world.issue.id, world.admin)
    assert admin_map["note.pdf"] == _url(world.files["note.pdf"])


async def test_hidden_issue_maps_nothing(db_session: AsyncSession, world: World) -> None:
    assert await _svc.build_issue_attachment_map(db_session, world.private_issue.id, world.member) == {}
    author_map = await _svc.build_issue_attachment_map(db_session, world.private_issue.id, world.author)
    assert author_map == {"budget.pdf": _url(world.files["budget.pdf"])}


async def test_anonymous_reader_gets_an_empty_issue_map(db_session: AsyncSession, world: World) -> None:
    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    assert await _svc.build_issue_attachment_map(db_session, world.issue.id, anonymous) == {}


# ---------------------------------------------------------------------------
# Issue pages
# ---------------------------------------------------------------------------


async def test_issue_description_resolves_links_and_images(client: AsyncClient, world: World) -> None:
    html = await _page(client, _issue_path(world), world.member)
    f = world.files

    assert f'href="{_url(f["spec.pdf"])}"' in html
    assert f'src="{_url(f["chart.png"])}"' in html
    # Both the angle-bracket and the URL-encoded spelling of a name with a space.
    assert html.count(f'href="{_url(f["release notes.pdf"])}"') >= 2
    _assert_untouched(html)


async def test_issue_description_does_not_resolve_another_issues_file(client: AsyncClient, world: World) -> None:
    html = await _page(client, _issue_path(world), world.member)
    assert _url(world.files["other.pdf"]) not in html
    assert 'href="other.pdf"' in html


async def test_issue_comment_resolves_links_and_images(client: AsyncClient, world: World) -> None:
    html = await _page(client, _issue_path(world), world.member)
    assert f'href="{_url(world.files["crash.log"])}"' in html
    assert f'src="{_url(world.files["shot.png"])}"' in html


async def test_activity_partial_resolves_comment_links(client: AsyncClient, world: World) -> None:
    token = await _token(client, world.member)
    resp = await client.get(
        f"/partials/issues/{world.project.key}-{world.issue.sequence_number}/activity/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, resp.text
    assert f'href="{_url(world.files["crash.log"])}"' in resp.text


async def test_private_note_file_links_for_admins_only(client: AsyncClient, world: World) -> None:
    note_url = _url(world.files["note.pdf"])
    for reader in (world.member, world.author):
        html = await _page(client, _issue_path(world), reader)
        assert note_url not in html, reader.login
        assert 'href="note.pdf"' in html

    assert f'href="{note_url}"' in await _page(client, _issue_path(world), world.admin)


async def test_description_history_resolves_links(client: AsyncClient, world: World) -> None:
    path = f"{_issue_path(world)}versions/{world.description_journal.id}/"
    html = await _page(client, path, world.member)
    assert f'href="{_url(world.files["spec.pdf"])}"' in html
    assert _url(world.files["other.pdf"]) not in html


# ---------------------------------------------------------------------------
# Wiki pages
# ---------------------------------------------------------------------------


async def test_wiki_page_resolves_links_and_images_project_wide(client: AsyncClient, world: World) -> None:
    html = await _page(client, f"/projects/{world.project.key}/wiki/{world.wiki_slug}/", world.member)
    f = world.files
    assert f'href="{_url(f["spec.pdf"])}"' in html
    assert f'src="{_url(f["chart.png"])}"' in html
    assert f'href="{_url(f["diagram.pdf"])}"' in html


async def test_wiki_page_does_not_link_a_file_the_reader_cannot_open(client: AsyncClient, world: World) -> None:
    path = f"/projects/{world.project.key}/wiki/{world.wiki_slug}/"
    budget_url = _url(world.files["budget.pdf"])

    member_html = await _page(client, path, world.member)
    assert budget_url not in member_html
    assert 'href="budget.pdf"' in member_html

    assert f'href="{budget_url}"' in await _page(client, path, world.author)


# ---------------------------------------------------------------------------
# Anonymous visitors
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def anonymous_live(db_session: AsyncSession, world: World) -> World:
    """*world* opted in to anonymous reading of issues and wiki."""
    await set_anonymous_permissions(
        db_session, world.project, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], world.admin
    )
    await set_anonymous_access_enabled(db_session, True, world.admin, confirmed_projects=[world.project.key])
    await db_session.commit()
    clear_role_cache(db_session)
    return world


async def test_anonymous_visitor_gets_no_attachment_links(client: AsyncClient, anonymous_live: World) -> None:
    world = anonymous_live
    for path in (_issue_path(world), f"/projects/{world.project.key}/wiki/{world.wiki_slug}/"):
        html = await _page(client, path, None)
        assert "/api/v1/attachments/" not in html, path
        assert 'href="spec.pdf"' in html, path


# ---------------------------------------------------------------------------
# Editor preview
# ---------------------------------------------------------------------------


async def _preview(client: AsyncClient, user: User, **body: object) -> str:
    token = await _token(client, user)
    resp = await client.post(_PREVIEW_URL, json=body, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["html"]


async def test_preview_without_context_resolves_nothing(client: AsyncClient, world: World) -> None:
    for context in ("issue", "wiki"):
        html = await _preview(client, world.member, text=DESCRIPTION, context=context)
        assert "/api/v1/attachments/" not in html
        assert 'href="spec.pdf"' in html


async def test_preview_with_issue_context_matches_the_issue_page(client: AsyncClient, world: World) -> None:
    html = await _preview(client, world.member, text=DESCRIPTION, context="issue", issue_id=world.issue.id)
    f = world.files
    assert f'href="{_url(f["spec.pdf"])}"' in html
    assert f'src="{_url(f["chart.png"])}"' in html
    assert _url(f["other.pdf"]) not in html
    assert _url(f["note.pdf"]) not in html
    _assert_untouched(html)


async def test_preview_for_a_hidden_issue_resolves_nothing(client: AsyncClient, world: World) -> None:
    text = "[budget](budget.pdf)"
    member_html = await _preview(client, world.member, text=text, context="issue", issue_id=world.private_issue.id)
    assert "/api/v1/attachments/" not in member_html

    author_html = await _preview(client, world.author, text=text, context="issue", issue_id=world.private_issue.id)
    assert f'href="{_url(world.files["budget.pdf"])}"' in author_html


async def test_preview_with_wiki_context_matches_the_wiki_page(client: AsyncClient, world: World) -> None:
    html = await _preview(client, world.member, text=WIKI_TEXT, context="wiki", project_key=world.project.key)
    assert f'href="{_url(world.files["spec.pdf"])}"' in html
    assert f'href="{_url(world.files["diagram.pdf"])}"' in html
    assert _url(world.files["budget.pdf"]) not in html


async def test_preview_with_an_unknown_project_resolves_nothing(client: AsyncClient, world: World) -> None:
    html = await _preview(client, world.member, text=WIKI_TEXT, context="wiki", project_key="NOSUCH")
    assert "/api/v1/attachments/" not in html
