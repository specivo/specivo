"""Every access path gives the same answer to the same question.

Role resolution is the single definition of who can see what. This module
builds one world — six projects covering every combination of public,
per-project anonymous opt-in and archived — and asks each viewer, with the
instance switch on and off, what they can see through every surface that
decides it:

- ``check_permission`` for ``view_issues`` / ``view_wiki``;
- ``ProjectService.require_project_access``;
- ``IssueService._check_visible`` and key lookup;
- ``IssueService.list_issues``;
- ``IssueService.visible_issues_clause`` (issue autocomplete);
- keyword search results and its per-type counts, for issues, wiki pages and
  comments;
- the visible children and relations of an issue.

Each surface is compared with an oracle written from the specification rather
than from the code, so a surface that drifts fails on its own. A second test
checks monotonicity from the observed answers alone: a signed-in non-member
never sees less than an anonymous visitor.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AnonymousAccessDeniedError, NotFoundError
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.relation import IssueRelation
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.chunking_service import ChunkingService
from specivo.services.embedding_service import EmbeddingService
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.permission_service import Permission, check_permission, clear_role_cache
from specivo.services.project_service import ProjectService
from specivo.services.relation_service import RelationService
from specivo.services.search_service import SearchService
from specivo.services.wiki_service import WikiService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

TERM = "armillary"

_issues = IssueService()
_projects = ProjectService()
_relations = RelationService()
_search = SearchService()

VI = Permission.VIEW_ISSUES
VW = Permission.VIEW_WIKI


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectSpec:
    key: str
    is_public: bool
    anonymous_permissions: tuple[str, ...]
    status: int = 1


PROJECTS = (
    ProjectSpec("ARMPRIV", is_public=False, anonymous_permissions=()),
    ProjectSpec("ARMPUB", is_public=True, anonymous_permissions=()),
    ProjectSpec("ARMPVI", is_public=True, anonymous_permissions=(VI,)),
    ProjectSpec("ARMPVW", is_public=True, anonymous_permissions=(VW,)),
    ProjectSpec("ARMPBO", is_public=True, anonymous_permissions=(VI, VW)),
    ProjectSpec("ARMARC", is_public=True, anonymous_permissions=(VI, VW), status=9),
)

# Viewers who hold a membership on every project: (permissions, issues_visibility).
MEMBER_ROLES: dict[str, tuple[tuple[str, ...], str]] = {
    "member_all": ((VI, VW), "all"),
    "member_default": ((VI, VW), "default"),
    "member_own": ((VI, VW), "own"),
    "member_no_view": ((VW, Permission.ADD_ISSUES), "default"),
    "group_member": ((VI, VW), "default"),
}

VIEWERS = ("admin", *MEMBER_ROLES, "non_member", "anonymous")

OK, NOT_FOUND, ANONYMOUS_DENIED = "ok", "not_found", "anonymous_denied"


@dataclass(frozen=True)
class Grant:
    """What a viewer holds in one project: issue visibility level, wiki access, project access."""

    level: str | None  # "admin", "all", "default", "own" or None
    wiki: bool
    access: str


def expected_grant(viewer: str, spec: ProjectSpec, switch: bool) -> Grant:
    if viewer == "admin":
        return Grant("admin", True, OK)
    if viewer in MEMBER_ROLES:
        permissions, visibility = MEMBER_ROLES[viewer]
        return Grant(visibility if VI in permissions else None, VW in permissions, OK)

    # Anonymous permissions apply only with the switch on, to an active public project.
    anonymous = set(spec.anonymous_permissions) if switch and spec.is_public and spec.status == 1 else set()

    if viewer == "non_member":
        if not spec.is_public:
            return Grant(None, False, NOT_FOUND)
        # The seeded Non member role grants view_issues with "default" visibility.
        return Grant("default", VW in anonymous, OK)

    assert viewer == "anonymous"
    return Grant("default" if VI in anonymous else None, VW in anonymous, OK if anonymous else ANONYMOUS_DENIED)


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class ProjectContent:
    project: Project
    open: Issue  # non-private, authored by the admin
    kid: Issue  # non-private child of ``open``
    secret: Issue  # private, unassigned child of ``open``
    mine: Issue | None  # private, assigned to the viewer (signed-in non-admin viewers only)
    wiki_page_id: int
    public_comment: Journal
    private_comment: Journal

    @property
    def issues(self) -> list[Issue]:
        return [i for i in (self.open, self.kid, self.secret, self.mine) if i is not None]


@dataclass
class World:
    users: dict[str, User]
    content: dict[str, ProjectContent] = field(default_factory=dict)


_seq = itertools.count(1)


async def _issue(
    db: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    author: User,
    subject: str,
    *,
    is_private: bool = False,
    assignee: User | None = None,
    parent: Issue | None = None,
) -> Issue:
    tracker, status, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=next(_seq),
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=assignee.id if assignee else None,
        parent_id=parent.id if parent else None,
        subject=f"Armillary {subject}",
        description=None,
        is_private=is_private,
    )
    db.add(issue)
    await db.flush()
    return issue


async def _indexed_comment(
    db: AsyncSession, issue: Issue, author: User, notes: str, model: EmbeddingModel, *, private: bool
) -> Journal:
    journal = await JournalService().add_comment(db, issue, author, notes)
    journal.is_private = private
    await db.flush()
    chunks = ChunkingService().chunk_journal(journal.notes)
    assert chunks, "comment is too short to be indexed"
    await EmbeddingService().embed_source(
        db, source_type="journal", entity_id=journal.id, project_id=issue.project_id, chunks=chunks, model_id=model.id
    )
    return journal


async def build_world(db: AsyncSession, viewer: str, switch: bool) -> World:
    admin = AdminUserFactory.build(login="arm_admin", status="active")
    users: dict[str, User] = {"admin": admin}
    for name in (*MEMBER_ROLES, "non_member"):
        users[name] = UserFactory.build(login=f"arm_{name}", status="active")
    db.add_all(list(users.values()))
    anonymous = await get_anonymous_user(db)
    assert anonymous is not None
    users["anonymous"] = anonymous

    status = StatusFactory.build(name="Arm New", position=1, category="backlog")
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Arm Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Arm Normal", is_default=False, position=2)
    model = EmbeddingModel(name="arm-mock", provider="mock", model_name="mock-1536", dimensions=1536, is_default=True)
    db.add_all([tracker, priority, model])
    await db.flush()
    lookups = (tracker, status, priority)

    roles: dict[str, Role] = {}
    for name, (permissions, visibility) in MEMBER_ROLES.items():
        roles[name] = Role(name=f"Arm {name}", permissions=list(permissions), issues_visibility=visibility)
    db.add_all(list(roles.values()))
    group = UserGroup(name="arm-group")
    db.add(group)
    await db.flush()
    db.add(UserGroupMember(group_id=group.id, user_id=users["group_member"].id))

    world = World(users=users)
    mine_owner = users[viewer] if viewer in (*MEMBER_ROLES, "non_member") else None

    for spec in PROJECTS:
        project = ProjectFactory.build(
            key=spec.key, identifier=spec.key.lower(), is_public=spec.is_public, status=spec.status
        )
        db.add(project)
        await db.flush()
        for module in ("issue_tracking", "wiki"):
            db.add(EnabledModule(project_id=project.id, name=module))
        for name, role in roles.items():
            if name == "group_member":
                member = Member(project_id=project.id, group_id=group.id)
            else:
                member = Member(project_id=project.id, user_id=users[name].id)
            db.add(member)
            await db.flush()
            db.add(MemberRole(member_id=member.id, role_id=role.id))
        if spec.anonymous_permissions:
            await set_anonymous_permissions(db, project, list(spec.anonymous_permissions), admin)

        open_issue = await _issue(db, project, lookups, admin, f"open {spec.key}")
        kid = await _issue(db, project, lookups, admin, f"kid {spec.key}", parent=open_issue)
        secret = await _issue(db, project, lookups, admin, f"secret {spec.key}", is_private=True, parent=open_issue)
        mine = None
        if mine_owner is not None:
            mine = await _issue(db, project, lookups, admin, f"mine {spec.key}", is_private=True, assignee=mine_owner)
        for target in (kid, secret):
            db.add(IssueRelation(issue_from_id=open_issue.id, issue_to_id=target.id, relation_type="relates"))

        page, _content = await WikiService().create_page(
            db,
            project.id,
            f"Armillary Handbook {spec.key}",
            f"Armillary wiki body for {spec.key}",
            admin,
            skip_search_index=True,
            skip_link_rebuild=True,
        )
        public_comment = await _indexed_comment(
            db, open_issue, admin, f"Armillary comment about {spec.key} open handling", model, private=False
        )
        private_comment = await _indexed_comment(
            db, open_issue, admin, f"Armillary private note about {spec.key} secret handling", model, private=True
        )
        world.content[spec.key] = ProjectContent(
            project, open_issue, kid, secret, mine, page.id, public_comment, private_comment
        )

    confirmed = [s.key for s in PROJECTS if s.anonymous_permissions]
    await set_anonymous_access_enabled(db, switch, admin, confirmed_projects=confirmed if switch else None)
    await db.commit()
    clear_role_cache(db)
    return world


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answers:
    """Everything one viewer is told about one project, by each surface."""

    view_issues: bool
    view_wiki: bool
    access: str
    checked: frozenset[int]
    by_key: frozenset[int]
    listed: frozenset[int]
    autocomplete: frozenset[int]
    searched_issues: frozenset[int]
    searched_wiki: frozenset[int]
    searched_comments: frozenset[int]
    children: frozenset[int]
    relations: frozenset[str]


def expected_answers(viewer: str, content: ProjectContent, grant: Grant) -> Answers:
    def visible(issue: Issue) -> bool:
        if grant.level == "admin":
            return True
        if viewer == "anonymous":
            own = False
        else:
            own = issue.assigned_to_id is not None and issue is content.mine
        if grant.level in ("all", "default"):
            return not issue.is_private or own
        if grant.level == "own":
            return own
        return False

    visible_ids = frozenset(i.id for i in content.issues if visible(i))
    comments = set()
    if content.open.id in visible_ids:
        comments.add(content.public_comment.id)
        if grant.level == "admin":
            comments.add(content.private_comment.id)
    return Answers(
        view_issues=grant.level is not None,
        view_wiki=grant.wiki,
        access=grant.access,
        checked=visible_ids,
        by_key=visible_ids,
        listed=visible_ids,
        autocomplete=visible_ids,
        searched_issues=visible_ids,
        searched_wiki=frozenset({content.wiki_page_id}) if grant.wiki else frozenset(),
        searched_comments=frozenset(comments),
        children=frozenset(i.id for i in (content.kid, content.secret) if i.id in visible_ids),
        relations=frozenset(i.display_key for i in (content.kid, content.secret) if i.id in visible_ids),
    )


async def observe(db: AsyncSession, world: World, viewer: str) -> tuple[dict[str, Answers], dict[str, int]]:
    user = world.users[viewer]
    results, _total, type_counts = await _search.search(db, TERM, user, scope="all", limit=1000)
    by_type: dict[tuple[str, str], set[int]] = {}
    for result in results:
        by_type.setdefault((str(result.result_type), result.project_key), set()).add(result.id)

    clause = await _issues.visible_issues_clause(db, user)
    answers: dict[str, Answers] = {}
    for key, content in world.content.items():
        project = content.project
        try:
            await _projects.require_project_access(db, project, user)
            access = OK
        except NotFoundError:
            access = NOT_FOUND
        except AnonymousAccessDeniedError:
            access = ANONYMOUS_DENIED

        checked = {i.id for i in content.issues if await _issues._check_visible(db, i, user)}
        by_key = set()
        for issue in content.issues:
            try:
                by_key.add((await _issues.get_by_display_key(db, issue.display_key, user)).id)
            except NotFoundError:
                pass
        listed, _count = await _issues.list_issues(
            db, project_id=project.id, filters={"status": "all"}, sort="id:asc", offset=0, limit=200, user=user
        )
        stmt = select(Issue.id).where(Issue.project_id == project.id)
        if clause is not None:
            stmt = stmt.where(clause)
        autocomplete = set((await db.execute(stmt)).scalars())
        children = await _issues.list_visible_children(db, content.open, user)
        relations = await _relations.list_for_issue(db, content.open, user)

        answers[key] = Answers(
            view_issues=await check_permission(user, project.id, VI, db),
            view_wiki=await check_permission(user, project.id, VW, db),
            access=access,
            checked=frozenset(checked),
            by_key=frozenset(by_key),
            listed=frozenset(i.id for i in listed),
            autocomplete=frozenset(autocomplete),
            searched_issues=frozenset(by_type.get(("issue", key), set())),
            searched_wiki=frozenset(by_type.get(("wiki", key), set())),
            searched_comments=frozenset(by_type.get(("comment", key), set())),
            children=frozenset(c.id for c in children),
            relations=frozenset(r["issue_to_key"] for r in relations),
        )
    return answers, type_counts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("switch", [False, True], ids=["switch_off", "switch_on"])
@pytest.mark.parametrize("viewer", VIEWERS)
async def test_every_surface_agrees_with_the_specification(db_session: AsyncSession, viewer: str, switch: bool) -> None:
    world = await build_world(db_session, viewer, switch)

    observed, type_counts = await observe(db_session, world, viewer)

    mismatches = []
    for spec in PROJECTS:
        grant = expected_grant(viewer, spec, switch)
        expected = expected_answers(viewer, world.content[spec.key], grant)
        actual = observed[spec.key]
        for name in Answers.__dataclass_fields__:
            if getattr(actual, name) != getattr(expected, name):
                mismatches.append(f"{spec.key}.{name}: expected {getattr(expected, name)}, got {getattr(actual, name)}")
    assert not mismatches, "\n".join(mismatches)

    expected_counts = {
        "issues": sum(len(observed[s.key].searched_issues) for s in PROJECTS),
        "wiki": sum(len(observed[s.key].searched_wiki) for s in PROJECTS),
        "comments": sum(len(observed[s.key].searched_comments) for s in PROJECTS),
        "attachments": 0,
    }
    assert {k: type_counts[k] for k in expected_counts} == expected_counts


@pytest.mark.parametrize("switch", [False, True], ids=["switch_off", "switch_on"])
async def test_signed_in_non_member_never_sees_less_than_anonymous(db_session: AsyncSession, switch: bool) -> None:
    """Checked from observed answers only, independently of the oracle."""
    world = await build_world(db_session, "non_member", switch)

    non_member, nm_counts = await observe(db_session, world, "non_member")
    anonymous, anon_counts = await observe(db_session, world, "anonymous")

    for key in world.content:
        nm, anon = non_member[key], anonymous[key]
        for name, value in vars(anon).items():
            if isinstance(value, frozenset):
                assert value <= getattr(nm, name), f"{key}.{name}"
            elif isinstance(value, bool):
                assert not value or getattr(nm, name), f"{key}.{name}"
        if anon.access == OK:
            assert nm.access == OK, key
    for type_name in ("issues", "wiki", "comments"):
        assert anon_counts[type_name] <= nm_counts[type_name], type_name
