"""Shared setup for the entity link graph tests: projects, people and linked content.

Not a test module (no ``test_`` prefix): the link graph service, REST, web and
MCP test modules import it so they exercise the same fixture world.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.wiki import WikiPage
from specivo.schemas.issue import IssueCreate
from specivo.services.entity_link_service import EntityLinkService
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import clear_role_cache
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

_wiki = WikiService()
_issues = IssueService()
_journals = JournalService()
links = EntityLinkService()


@dataclass
class World:
    """Two projects: the reader may see ``home`` (issues and wiki), and nothing of ``hidden``."""

    tracker_id: int
    admin: User
    reader: User
    home: Project
    hidden: Project


async def build_world(db: AsyncSession, prefix: str) -> World:
    """Create lookups, an admin, a reader and the two projects; *prefix* keeps keys and logins unique."""
    status = StatusFactory.build(name=f"{prefix} New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    priority = PriorityFactory.build(name=f"{prefix} Normal", is_default=True, position=2)
    tracker = TrackerFactory.build(name=f"{prefix} Task", default_status_id=status.id)
    admin = AdminUserFactory.build(login=f"{prefix.lower()}_admin", status="active")
    reader = UserFactory.build(login=f"{prefix.lower()}_reader", status="active")
    home = ProjectFactory.build(key=f"{prefix}H", name=f"{prefix} Home", is_public=False)
    hidden = ProjectFactory.build(key=f"{prefix}X", name=f"{prefix} Hidden", is_public=False)
    db.add_all([priority, tracker, admin, reader, home, hidden])
    await db.flush()
    for project in (home, hidden):
        db.add(EnabledModule(project_id=project.id, name="wiki"))
        db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await grant(db, reader, home, ["view_issues", "view_wiki", "add_issue_notes"])
    await db.commit()
    return World(tracker.id, admin, reader, home, hidden)


async def grant(db: AsyncSession, user: User, project: Project, permissions: list[str]) -> None:
    role = Role(
        name=f"role-{project.key}-{user.login}", permissions=permissions, builtin=0, issues_visibility="default"
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()
    clear_role_cache(db)


async def issue(
    db: AsyncSession, world: World, project: Project, subject: str, description: str = "", *, private: bool = False
) -> Issue:
    created = await _issues.create(
        db,
        project,
        IssueCreate(
            project_key=project.key,
            tracker_id=world.tracker_id,
            subject=subject,
            description=description or None,
            is_private=private,
        ),
        world.admin,
    )
    await db.commit()
    await db.refresh(created)
    return created


async def page(db: AsyncSession, world: World, project: Project, title: str, text: str = "") -> WikiPage:
    created, _content = await _wiki.create_page(db, project.id, title, text or title, world.admin)
    await db.commit()
    await db.refresh(created)
    return created


async def comment(db: AsyncSession, world: World, on: Issue, notes: str, *, private: bool = False) -> Journal:
    journal = await _journals.add_comment(db, on, world.admin, notes)
    if private:
        journal.is_private = True
    await db.commit()
    await db.refresh(journal)
    return journal


async def rebuild(db: AsyncSession, *sources: Issue | WikiPage | Journal) -> None:
    """Rebuild the links of *sources* in process, as the queued task would after the commit."""
    keys = []
    for source in sources:
        if isinstance(source, Issue):
            keys.append(("issue", source.id))
        elif isinstance(source, WikiPage):
            keys.append(("wiki_page", source.id))
        else:
            keys.append(("journal", source.id))
    failures = await links.rebuild_sources(db, keys)
    assert failures == []
    await db.commit()


def wiki_ref(project: Project, slug: str) -> str:
    return f"wiki:{project.key}/{slug}"
