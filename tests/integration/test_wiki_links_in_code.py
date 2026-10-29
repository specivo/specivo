"""Issue descriptions, comments and wiki pages leave ``[[Page]]`` in code as written.

The renderer links ``[[Page]]`` on what Python-Markdown parsed, so the pages
that show saved text link it in ordinary text and show it verbatim in fenced,
indented and inline code and in raw HTML blocks.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import EnabledModule, Project
from specivo.services.journal_service import JournalService
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

TEXT = (
    "Read [[Setup Guide]] first.\n\n"
    "```\nfenced [[Fenced Page]]\n```\n\n"
    "    indented [[Indented Page]]\n\n"
    "Inline `[[Inline Page]]` code.\n\n"
    '<div class="raw">\nraw [[Raw Page]]\n</div>\n'
)


def assert_rendered(html: str, project_key: str) -> None:
    text = " ".join(html.split())
    assert f'href="/projects/{project_key}/wiki/setup-guide/"' in text
    for slug in ("fenced-page", "indented-page", "inline-page", "raw-page"):
        assert f"/wiki/{slug}/" not in text, slug
    for literal in ("[[Fenced Page]]", "[[Indented Page]]", "<code>[[Inline Page]]</code>", "raw [[Raw Page]]"):
        assert literal in text, literal


async def _project(db: AsyncSession, key: str, *modules: str) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), name=f"Project {key}")
    db.add(project)
    await db.flush()
    db.add_all(EnabledModule(project_id=project.id, name=module) for module in modules)
    await db.flush()
    return project


async def test_issue_description_and_comment(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = admin_client.state.user
    project = await _project(db_session, "WLCI", "issue_tracking")
    status = StatusFactory.build(name="WLC New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="WLC Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="WLC Normal", position=1)
    db_session.add_all([tracker, priority])
    await db_session.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=admin.id,
        subject="Wiki links in code",
        description=TEXT,
    )
    db_session.add(issue)
    await db_session.flush()
    await JournalService().add_comment(db_session, issue, admin, TEXT.replace("Setup Guide", "Comment Guide"))
    await db_session.commit()

    resp = await admin_client.get(f"/issue/{issue.display_key}/")
    assert resp.status_code == 200, resp.text
    assert_rendered(resp.text, project.key)
    assert 'href="/projects/WLCI/wiki/comment-guide/"' in resp.text


async def test_wiki_page(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = admin_client.state.user
    project = await _project(db_session, "WLCW", "wiki")
    page, _content = await WikiService().create_page(db_session, project.id, "Code Samples", TEXT, admin)
    await db_session.commit()

    resp = await admin_client.get(f"/projects/{project.key}/wiki/{page.slug}/")
    assert resp.status_code == 200, resp.text
    assert_rendered(resp.text, project.key)


async def test_preview_endpoint(admin_client: AsyncClient) -> None:
    resp = await admin_client.post("/api/v1/markdown/preview/", json={"text": TEXT})
    assert resp.status_code == 200, resp.text
    assert_rendered(resp.json()["html"], "")
