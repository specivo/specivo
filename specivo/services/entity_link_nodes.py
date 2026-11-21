"""Structured nodes of the link graph: tags, metadata values, versions, sprints, assignees and hand-entered commits.

These nodes are read live from the tables they live in; nothing is stored for
them. An issue or wiki page points at them:

========================  ======================  ===================================
Node (``type``)           Ref                     Edge from an issue or page
========================  ======================  ===================================
``tag``                   ``tag:KEY/<name>``      ``tagged`` (issues and pages)
``component``             ``component:KEY/<v>``   ``in_component``
``metadata``              ``metadata:KEY/<k>/<v>``  ``in_metadata``
``version``               ``version:KEY/<name>``  ``targets_version``
``sprint``                ``sprint:KEY/<name>``   ``in_sprint``
``user``                  ``user:<login>``        ``assigned_to`` (the assignee only)
``commit``                ``commit:<sha12>``      ``commit`` (the ``commits`` metadata)
========================  ======================  ===================================

Metadata nodes come from the issue metadata keys a project lists in
``project.settings["graph_metadata_keys"]`` (default ``["component"]``). Each
string value (or each string in a list value) is one node per project and key.
The ``component`` key is the ``component`` node type with its own edge kind;
every other key is a ``metadata`` node whose ref names the key, so values of
different keys never merge. A key holding ``/`` cannot be named in a ref and
is ignored.

A hand-entered commit SHA becomes the commit of a repository connected to the
issue's project when one with that SHA exists, so it merges with the commits
the repository links itself; otherwise it is a node of its own, ref
``commit:<first 12 characters>``.

Hubs
----
Tag, component, metadata, version, sprint and user nodes are hubs: one of them
may join hundreds of items. A hub is expanded (its members listed) only when
it is the root of the read, or when its edge kind is asked for explicitly.
Otherwise it is a leaf that carries ``degree``: how many items the reader may
see point at it, and nothing about the rest.

Visibility
----------
- A tag is visible to a reader who may view the issues or read the wiki of
  its project; a version, sprint or metadata value to one who may view the
  issues of its project.
- A user is visible to any signed-in reader: the edge to them comes from an
  issue the reader may see, which is what the project's member list asks for
  too (``view_issues``). The anonymous user is never shown people.
- Members of a hub are filtered like every other node, in SQL, so a degree
  never counts what the reader may not see.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.member import Member
from specivo.models.project import EnabledModule, Project
from specivo.models.sprint import Sprint
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import WikiPage
from specivo.services.api_key_scope import is_global_admin

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from specivo.services.entity_link_service import LinkNode, NodeKey, _RawEdge

KIND_TAGGED = "tagged"
KIND_IN_COMPONENT = "in_component"
KIND_IN_METADATA = "in_metadata"
KIND_TARGETS_VERSION = "targets_version"
KIND_IN_SPRINT = "in_sprint"
KIND_ASSIGNED_TO = "assigned_to"
HUB_KINDS = frozenset(
    {KIND_TAGGED, KIND_IN_COMPONENT, KIND_IN_METADATA, KIND_TARGETS_VERSION, KIND_IN_SPRINT, KIND_ASSIGNED_TO}
)

NODE_TAG = "tag"
NODE_COMPONENT = "component"
NODE_METADATA = "metadata"
NODE_VERSION = "version"
NODE_SPRINT = "sprint"
NODE_USER = "user"
# Node type -> the kind of the edges that join a member to it.
HUB_KIND_OF = {
    NODE_TAG: KIND_TAGGED,
    NODE_COMPONENT: KIND_IN_COMPONENT,
    NODE_METADATA: KIND_IN_METADATA,
    NODE_VERSION: KIND_TARGETS_VERSION,
    NODE_SPRINT: KIND_IN_SPRINT,
    NODE_USER: KIND_ASSIGNED_TO,
}
HUB_TYPES = frozenset(HUB_KIND_OF)

COMPONENT_KEY = "component"
DEFAULT_GRAPH_METADATA_KEYS: tuple[str, ...] = (COMPONENT_KEY,)
COMMITS_KEY = "commits"

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_PROJECT_REF_SCHEMES = (NODE_TAG, NODE_COMPONENT, NODE_METADATA, NODE_VERSION, NODE_SPRINT)
HUB_REF_SCHEMES = (*_PROJECT_REF_SCHEMES, NODE_USER)


def graph_metadata_keys(settings: dict[str, Any] | None) -> tuple[str, ...]:
    """The issue metadata keys whose values are graph nodes in a project with these *settings*."""
    raw = (settings or {}).get("graph_metadata_keys")
    if not isinstance(raw, list):
        return DEFAULT_GRAPH_METADATA_KEYS
    return tuple(dict.fromkeys(k.strip() for k in raw if isinstance(k, str) and k.strip() and "/" not in k))


def metadata_node(project_id: int, key: str, value: str) -> NodeKey:
    return (NODE_COMPONENT if key == COMPONENT_KEY else NODE_METADATA, (project_id, key, value))


def metadata_values(raw: Any) -> list[str]:
    """The string values of one metadata entry: a string, or the strings of a list."""
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [v for v in raw if isinstance(v, str)]
    else:
        return []
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def _metadata_match(key: str, value: str) -> ColumnElement[bool]:
    return or_(Issue.issue_metadata.contains({key: value}), Issue.issue_metadata.contains({key: [value]}))


def _hub_edge(source: NodeKey, hub: NodeKey, kind: str) -> _RawEdge:
    from specivo.services.entity_link_service import _RawEdge

    return _RawEdge(source, hub, kind, identity=(kind, source, hub))


# ---------------------------------------------------------------------------
# Edges from an issue or page to its hubs
# ---------------------------------------------------------------------------


async def item_hub_edges(
    session: AsyncSession, node: NodeKey, kinds: frozenset[str], linked_commits: Iterable[Any] = ()
) -> list[_RawEdge]:
    """The tags of an issue or page and, for an issue, its version, sprint, assignee, metadata values and commits.

    *linked_commits* are the commit nodes the issue already has from a
    connected repository; a hand-entered SHA naming one of them is not
    listed twice.
    """
    edges: list[_RawEdge] = []
    if KIND_TAGGED in kinds:
        column = TagLink.issue_id if node[0] == "issue" else TagLink.wiki_page_id
        tag_ids = (await session.execute(select(TagLink.tag_id).where(column == node[1]))).scalars()
        edges.extend(_hub_edge(node, (NODE_TAG, tag_id), KIND_TAGGED) for tag_id in tag_ids)
    if node[0] == "issue" and kinds & (HUB_KINDS | {"commit"}):
        edges.extend(await _issue_field_edges(session, node, kinds, set(linked_commits)))
    return edges


async def _issue_field_edges(
    session: AsyncSession, node: NodeKey, kinds: frozenset[str], linked_commits: set[Any]
) -> list[_RawEdge]:
    row = (
        await session.execute(
            select(Issue.project_id, Issue.fixed_version_id, Issue.sprint_id, Issue.assigned_to_id)
            .add_columns(Issue.issue_metadata, Project.settings)
            .join(Project, Project.id == Issue.project_id)
            .where(Issue.id == node[1])
        )
    ).one_or_none()
    if row is None:
        return []
    project_id, version_id, sprint_id, assignee_id, metadata, settings = row
    metadata = metadata or {}
    edges: list[_RawEdge] = []
    for kind, node_type, target_id in (
        (KIND_TARGETS_VERSION, NODE_VERSION, version_id),
        (KIND_IN_SPRINT, NODE_SPRINT, sprint_id),
        (KIND_ASSIGNED_TO, NODE_USER, assignee_id),
    ):
        if kind in kinds and target_id is not None:
            edges.append(_hub_edge(node, (node_type, target_id), kind))
    for key in graph_metadata_keys(settings):
        hub_kind = KIND_IN_COMPONENT if key == COMPONENT_KEY else KIND_IN_METADATA
        if hub_kind in kinds:
            edges.extend(
                _hub_edge(node, metadata_node(project_id, key, v), hub_kind) for v in metadata_values(metadata.get(key))
            )
    if "commit" in kinds:
        edges.extend(await _metadata_commit_edges(session, node, project_id, metadata.get(COMMITS_KEY), linked_commits))
    return edges


async def _metadata_commit_edges(
    session: AsyncSession, node: NodeKey, project_id: int, raw: Any, linked: set[Any]
) -> list[_RawEdge]:
    """``commit`` edges for the SHAs in the issue's ``commits`` metadata, merged with known repository commits."""
    from specivo.models.repository import RepositoryCommit, RepositoryIntegration
    from specivo.services.entity_link_service import _RawEdge

    shas = [sha for sha in (v.lower() for v in metadata_values(raw)) if _SHA.match(sha)]
    if not shas:
        return []
    known: list[tuple[int, str]] = [
        (commit_id, sha)
        for commit_id, sha in await session.execute(
            select(RepositoryCommit.id, RepositoryCommit.sha)
            .join(RepositoryIntegration, RepositoryIntegration.id == RepositoryCommit.integration_id)
            .where(
                RepositoryIntegration.project_id == project_id,
                or_(*(RepositoryCommit.sha.startswith(sha) for sha in shas)),
            )
            .order_by(RepositoryCommit.id)
        )
    ]
    edges: list[_RawEdge] = []
    seen = set(linked)
    for sha in shas:
        commit_id = next((cid for cid, full in known if full.lower().startswith(sha)), None)
        target: NodeKey = ("commit", commit_id) if commit_id is not None else ("commit", sha[:12])
        if target in seen:
            continue
        seen.add(target)
        # A repository commit is shown through any visible issue naming it, as through one it links itself.
        edges.append(_RawEdge(node, target, "commit", vouched=commit_id is not None))
    return edges


# ---------------------------------------------------------------------------
# Hub members, visibility, degrees and descriptions
# ---------------------------------------------------------------------------


class HubReader:
    """What one reader may see of the hub nodes, and their visible members; the hub half of ``_Reader``."""

    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        self._visible: dict[NodeKey, bool] = {}
        self._hub_project: dict[NodeKey, int] = {}
        self._projects: dict[int, Project] = {}
        self._issue_projects: dict[int, bool] = {}
        self._wiki_projects: dict[int, bool] = {}
        self._clause_loaded = False
        self._clause: ColumnElement[bool] | None = None

    # -- project permissions -------------------------------------------------

    async def issue_clause(self) -> ColumnElement[bool] | None:
        """The reader's issue visibility predicate (None: every issue), computed once."""
        if not self._clause_loaded:
            from specivo.services.issue_service import IssueService

            self._clause = await IssueService().visible_issues_clause(self.session, self.user)
            self._clause_loaded = True
        return self._clause

    async def _load_projects(self, ids: set[int]) -> None:
        wanted = ids - self._issue_projects.keys()
        if not wanted:
            return
        from specivo.services.permission_service import Permission, projects_granting

        projects = list((await self.session.execute(select(Project).where(Project.id.in_(wanted)))).scalars())
        self._projects.update({p.id: p for p in projects})
        issues = await projects_granting(self.session, self.user, projects, Permission.VIEW_ISSUES)
        enabled = set(
            (
                await self.session.execute(
                    select(EnabledModule.project_id).where(
                        EnabledModule.project_id.in_(wanted), EnabledModule.name == "wiki"
                    )
                )
            ).scalars()
        )
        wiki = enabled & await projects_granting(self.session, self.user, projects, Permission.VIEW_WIKI)
        for project_id in wanted:
            self._issue_projects[project_id] = project_id in issues
            self._wiki_projects[project_id] = project_id in wiki

    async def issues_readable(self, project_id: int) -> bool:
        await self._load_projects({project_id})
        return self._issue_projects[project_id]

    async def wiki_readable(self, project_id: int) -> bool:
        await self._load_projects({project_id})
        return self._wiki_projects[project_id]

    # -- visibility ------------------------------------------------------------

    async def load(self, nodes: Iterable[NodeKey]) -> None:
        """Decide which of the hub *nodes* the reader may see."""
        wanted = {n for n in nodes if n[0] in HUB_TYPES and n not in self._visible}
        if not wanted:
            return
        await self._load_hub_projects(wanted)
        await self._load_projects(set(self._hub_project.values()))
        for node in wanted:
            if node[0] == NODE_USER:
                self._visible[node] = not self.user.is_anonymous
                continue
            project_id = self._hub_project.get(node)
            if project_id is None:
                self._visible[node] = False
            elif node[0] == NODE_TAG:
                self._visible[node] = self._issue_projects[project_id] or self._wiki_projects[project_id]
            else:
                self._visible[node] = self._issue_projects[project_id]

    async def _load_hub_projects(self, nodes: set[NodeKey]) -> None:
        by_type: dict[str, set[Any]] = defaultdict(set)
        for node_type, node_id in nodes:
            by_type[node_type].add(node_id)
        for node_type, model in ((NODE_TAG, Tag), (NODE_VERSION, Version), (NODE_SPRINT, Sprint)):
            if by_type[node_type]:
                for hub_id, project_id in await self.session.execute(
                    select(model.id, model.project_id).where(model.id.in_(by_type[node_type]))
                ):
                    self._hub_project[(node_type, hub_id)] = project_id
        for node_type in (NODE_COMPONENT, NODE_METADATA):
            for node_id in by_type[node_type]:
                self._hub_project[(node_type, node_id)] = node_id[0]

    def visible(self, node: NodeKey) -> bool:
        return self._visible.get(node, False)

    # -- members -----------------------------------------------------------------

    async def member_edges(self, hub: NodeKey, kinds: frozenset[str], cap: int) -> list[_RawEdge]:
        """The visible issues (and, for a tag, pages) that point at *hub*, newest first, at most *cap* of each."""
        kind = HUB_KIND_OF[hub[0]]
        if kind not in kinds:
            return []
        await self.load([hub])
        if not self.visible(hub):
            return []
        stmt = select(Issue.id).where(self._member_condition(hub)).order_by(Issue.id.desc()).limit(cap)
        clause = await self.issue_clause()
        if clause is not None:
            stmt = stmt.where(clause)
        members: list[NodeKey] = [("issue", issue_id) for issue_id in (await self.session.execute(stmt)).scalars()]
        if hub[0] == NODE_TAG and await self.wiki_readable(self._hub_project[hub]):
            pages = select(WikiPage.id).join(TagLink, TagLink.wiki_page_id == WikiPage.id)
            pages = pages.where(TagLink.tag_id == hub[1], WikiPage.deleted_at.is_(None))
            members += [
                ("wiki_page", page_id)
                for page_id in (await self.session.execute(pages.order_by(WikiPage.id.desc()).limit(cap))).scalars()
            ]
        return [_hub_edge(member, hub, kind) for member in members]

    @staticmethod
    def _member_condition(hub: NodeKey) -> ColumnElement[bool]:
        node_type, node_id = hub
        if node_type == NODE_TAG:
            return Issue.id.in_(select(TagLink.issue_id).where(TagLink.tag_id == int(node_id)))
        if node_type == NODE_VERSION:
            return Issue.fixed_version_id == int(node_id)
        if node_type == NODE_SPRINT:
            return Issue.sprint_id == int(node_id)
        if node_type == NODE_USER:
            return Issue.assigned_to_id == int(node_id)
        project_id, key, value = node_id
        return and_(Issue.project_id == project_id, _metadata_match(key, value))

    async def degrees(self, hubs: Iterable[NodeKey]) -> dict[NodeKey, int]:
        """How many items the reader may see point at each of the visible *hubs*."""
        wanted = [h for h in hubs if self.visible(h)]
        counts: dict[NodeKey, int] = dict.fromkeys(wanted, 0)
        clause = await self.issue_clause()
        by_type: dict[str, list[Any]] = defaultdict(list)
        for node_type, node_id in wanted:
            by_type[node_type].append(node_id)
        for node_type, column in (
            (NODE_VERSION, Issue.fixed_version_id),
            (NODE_SPRINT, Issue.sprint_id),
            (NODE_USER, Issue.assigned_to_id),
        ):
            if by_type[node_type]:
                grouped = select(column, func.count()).where(column.in_(by_type[node_type])).group_by(column)
                for hub_id, count in await self.session.execute(grouped if clause is None else grouped.where(clause)):
                    counts[(node_type, hub_id)] = count
        if by_type[NODE_TAG]:
            await self._tag_degrees(by_type[NODE_TAG], clause, counts)
        for node_type in (NODE_COMPONENT, NODE_METADATA):
            for node_id in by_type[node_type]:
                total = select(func.count()).select_from(Issue).where(self._member_condition((node_type, node_id)))
                counts[(node_type, node_id)] = (
                    await self.session.scalar(total if clause is None else total.where(clause))
                ) or 0
        return counts

    async def _tag_degrees(
        self, tag_ids: list[int], clause: ColumnElement[bool] | None, counts: dict[NodeKey, int]
    ) -> None:
        stmt = (
            select(TagLink.tag_id, func.count())
            .join(Issue, Issue.id == TagLink.issue_id)
            .where(TagLink.tag_id.in_(tag_ids))
            .group_by(TagLink.tag_id)
        )
        for tag_id, count in await self.session.execute(stmt if clause is None else stmt.where(clause)):
            counts[(NODE_TAG, tag_id)] += count
        readable = [t for t in tag_ids if self._wiki_projects.get(self._hub_project[(NODE_TAG, t)], False)]
        if readable:
            for tag_id, count in await self.session.execute(
                select(TagLink.tag_id, func.count())
                .join(WikiPage, WikiPage.id == TagLink.wiki_page_id)
                .where(TagLink.tag_id.in_(readable), WikiPage.deleted_at.is_(None))
                .group_by(TagLink.tag_id)
            ):
                counts[(NODE_TAG, tag_id)] += count

    async def describe(self, nodes: Iterable[NodeKey]) -> dict[NodeKey, LinkNode]:
        """Describe the visible hub *nodes* and the hand-entered commits (``("commit", "<sha12>")``)."""
        from specivo.services.entity_link_service import LinkNode

        out: dict[NodeKey, LinkNode] = {}
        by_type: dict[str, list[Any]] = defaultdict(list)
        for node in nodes:
            if node[0] == "commit" and isinstance(node[1], str):
                out[node] = LinkNode(f"commit:{node[1]}", "commit", "", None, None)
            elif node[0] in HUB_TYPES and self.visible(node):
                by_type[node[0]].append(node[1])
        keys = await self._project_keys()
        if by_type[NODE_TAG]:
            for tag_id, name, project_id in await self.session.execute(
                select(Tag.id, Tag.name, Tag.project_id).where(Tag.id.in_(by_type[NODE_TAG]))
            ):
                key = keys[project_id]
                out[(NODE_TAG, tag_id)] = LinkNode(f"tag:{key}/{name}", NODE_TAG, name, None, None, key)
        for node_type, model, path in ((NODE_VERSION, Version, "versions"), (NODE_SPRINT, Sprint, "sprints")):
            if by_type[node_type]:
                for hub_id, name, status, project_id in await self.session.execute(
                    select(model.id, model.name, model.status, model.project_id).where(model.id.in_(by_type[node_type]))
                ):
                    key = keys[project_id]
                    out[(node_type, hub_id)] = LinkNode(
                        f"{node_type}:{key}/{name}", node_type, name, status, f"/projects/{key}/{path}/{hub_id}/", key
                    )
        if by_type[NODE_USER]:
            for user_id, login, display_name in await self.session.execute(
                select(User.id, User.login, User.display_name).where(User.id.in_(by_type[NODE_USER]))
            ):
                out[(NODE_USER, user_id)] = LinkNode(f"user:{login}", NODE_USER, display_name or login, None, None)
        for node_type in (NODE_COMPONENT, NODE_METADATA):
            for node_id in by_type[node_type]:
                out[(node_type, node_id)] = _metadata_link_node(node_type, node_id, keys[node_id[0]])
        return out

    async def _project_keys(self) -> dict[int, str]:
        ids = set(self._hub_project.values()) - self._projects.keys()
        if ids:
            await self._load_projects(ids)
        return {pid: project.key for pid, project in self._projects.items()}


def _metadata_link_node(node_type: str, node_id: tuple[int, str, str], project_key: str) -> LinkNode:
    from specivo.services.entity_link_service import LinkNode

    _project_id, key, value = node_id
    if node_type == NODE_COMPONENT:
        return LinkNode(f"component:{project_key}/{value}", NODE_COMPONENT, value, None, None, project_key)
    return LinkNode(f"metadata:{project_key}/{key}/{value}", NODE_METADATA, f"{key}: {value}", None, None, project_key)


async def attachment_counts(session: AsyncSession, user: User, nodes: Iterable[NodeKey]) -> dict[NodeKey, int]:
    """How many files are attached to each issue or wiki page in *nodes* (all visible to the reader already).

    A file attached to an issue or page is visible with it, as in the
    ``attachment`` edges; the anonymous user is shown no files.
    """
    items = [n for n in nodes if n[0] in ("issue", "wiki_page")]
    counts: dict[NodeKey, int] = dict.fromkeys(items, 0)
    if not items or user.is_anonymous:
        return counts
    containers = {"issue": "Issue", "wiki_page": "WikiPage"}
    node_types = {v: k for k, v in containers.items()}
    conditions = [
        and_(Attachment.container_type == container, Attachment.container_id.in_(ids))
        for container, ids in ((containers[t], [n[1] for n in items if n[0] == t]) for t in ("issue", "wiki_page"))
        if ids
    ]
    for container, container_id, count in await session.execute(
        select(Attachment.container_type, Attachment.container_id, func.count())
        .where(or_(*conditions))
        .group_by(Attachment.container_type, Attachment.container_id)
    ):
        counts[(node_types[container], container_id)] = count
    return counts


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------


def is_hub_ref(ref: str) -> bool:
    scheme, sep, _rest = ref.partition(":")
    return bool(sep) and scheme.strip().lower() in HUB_REF_SCHEMES


async def resolve_hub_root(session: AsyncSession, user: User, ref: str) -> NodeKey:
    """Resolve ``tag:``, ``component:``, ``metadata:``, ``version:``, ``sprint:`` or ``user:`` refs *user* may see.

    A project the user cannot reach, a permission they lack and a name that
    does not exist all answer "not found", as for issues.
    """
    from specivo.core.exceptions import NotFoundError

    scheme, _sep, rest = ref.partition(":")
    scheme = scheme.strip().lower()
    rest = rest.strip()
    if scheme == NODE_USER:
        return await _resolve_user(session, user, rest)
    project_key, sep, name = rest.partition("/")
    if not sep or not name.strip() or not project_key.strip():
        shape = "metadata:PROJECT_KEY/<key>/<value>" if scheme == NODE_METADATA else f"{scheme}:PROJECT_KEY/<name>"
        raise ValueError(f"A {scheme} ref is '{shape}'")
    project = await _reachable_project(session, user, project_key.strip())
    hubs = HubReader(session, user)
    readable = await hubs.issues_readable(project.id)
    if scheme == NODE_TAG:
        readable = readable or await hubs.wiki_readable(project.id)
    if not readable:
        raise NotFoundError(f"{scheme.capitalize()} {rest!r} not found")
    return await _resolve_in_project(session, project, scheme, name.strip(), rest)


async def _resolve_in_project(session: AsyncSession, project: Project, scheme: str, name: str, rest: str) -> NodeKey:
    from specivo.core.exceptions import NotFoundError

    if scheme in (NODE_COMPONENT, NODE_METADATA):
        key, value = (COMPONENT_KEY, name) if scheme == NODE_COMPONENT else _split_metadata(name)
        if key not in graph_metadata_keys(project.settings):
            raise NotFoundError(f"Metadata key {key!r} is not a graph node in {project.key}")
        return metadata_node(project.id, key, value)
    models: dict[str, type[Tag] | type[Version] | type[Sprint]] = {
        NODE_TAG: Tag,
        NODE_VERSION: Version,
        NODE_SPRINT: Sprint,
    }
    model = models[scheme]
    match = func.lower(model.name) == name.lower() if scheme == NODE_TAG else model.name == name
    hub_id = await session.scalar(
        select(model.id).where(model.project_id == project.id, match).order_by(model.id).limit(1)
    )
    if hub_id is None:
        raise NotFoundError(f"{scheme.capitalize()} {rest!r} not found")
    return (scheme, hub_id)


def _split_metadata(name: str) -> tuple[str, str]:
    key, sep, value = name.partition("/")
    if not sep or not key.strip() or not value.strip():
        raise ValueError("A metadata ref is 'metadata:PROJECT_KEY/<key>/<value>'")
    return key.strip(), value.strip()


async def _reachable_project(session: AsyncSession, user: User, key: str) -> Project:
    from specivo.core.exceptions import NotFoundError
    from specivo.services.project_service import ProjectService

    service = ProjectService()
    try:
        project = await service.get_by_key(session, key.upper())
        await service.require_project_access(session, project, user)
    except NotFoundError:
        raise NotFoundError(f"Project '{key}' not found") from None
    return project


async def _resolve_user(session: AsyncSession, user: User, login: str) -> NodeKey:
    """A person the reader may see in a member list: themselves, or a member of a project whose issues they view."""
    from specivo.core.exceptions import NotFoundError
    from specivo.services.permission_service import Permission, projects_granting

    not_found = NotFoundError(f"User {login!r} not found")
    if user.is_anonymous or not login:
        raise not_found
    target_id = await session.scalar(
        select(User.id).where(func.lower(User.login) == login.lower(), User.is_anonymous.is_(False))
    )
    if target_id is None:
        raise not_found
    if target_id == user.id or is_global_admin(user):
        return (NODE_USER, target_id)
    projects = list(
        (
            await session.execute(
                select(Project).where(Project.id.in_(select(Member.project_id).where(Member.user_id == target_id)))
            )
        ).scalars()
    )
    if not await projects_granting(session, user, projects, Permission.VIEW_ISSUES):
        raise not_found
    return (NODE_USER, target_id)
