"""The structured edges one node yields (``EntityLinkService._raw_edges``), in order, per kind and direction."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.relation import IssueRelation
from specivo.services.entity_link_service import (
    KIND_ATTACHMENT,
    KIND_CHILD,
    KIND_COMMIT,
    KIND_PARENT,
    KIND_PULL_REQUEST,
    KIND_RELATION,
)
from tests.integration.link_graph_helpers import build_world, issue, links, page

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

STRUCTURED = frozenset({KIND_RELATION, KIND_PARENT, KIND_CHILD, KIND_ATTACHMENT, KIND_COMMIT, KIND_PULL_REQUEST})


def _edges(edges) -> list[tuple]:
    return [(e.source, e.target, e.kind, e.relation_type, e.identity) for e in edges]


async def test_structured_edges_of_an_issue_and_a_page(db_session: AsyncSession) -> None:
    db = db_session
    world = await build_world(db, "ERE")
    root = await issue(db, world, world.home, "Root")
    parent = await issue(db, world, world.home, "Parent")
    child_a = await issue(db, world, world.home, "Child A")
    child_b = await issue(db, world, world.home, "Child B")
    other = await issue(db, world, world.home, "Other")
    root.parent_id = parent.id
    child_a.parent_id = root.id
    child_b.parent_id = root.id
    out_rel = IssueRelation(issue_from_id=root.id, issue_to_id=other.id, relation_type="blocks")
    in_rel = IssueRelation(issue_from_id=parent.id, issue_to_id=root.id, relation_type="precedes")
    db.add_all([out_rel, in_rel])
    attachments = [
        Attachment(
            container_type=kind,
            container_id=cid,
            filename=f"{name}.txt",
            disk_filename=f"ere-{name}",
            filesize=1,
            author_id=world.admin.id,
        )
        for kind, cid, name in (("Issue", root.id, "issue-file"), ("Issue", other.id, "other-file"))
    ]
    db.add_all(attachments)
    home_page = await page(db, world, world.home, "ERE Home")
    sub_page = await page(db, world, world.home, "ERE Sub")
    sub_page.parent_id = home_page.id
    page_file = Attachment(
        container_type="WikiPage",
        container_id=home_page.id,
        filename="page.txt",
        disk_filename="ere-page",
        filesize=1,
        author_id=world.admin.id,
    )
    db.add(page_file)
    await db.flush()

    node = ("issue", root.id)
    out = _edges(await links._raw_edges(db, node, "out", STRUCTURED))
    assert sorted(out, key=repr) == sorted(
        [
            (node, ("issue", other.id), KIND_RELATION, "blocks", (KIND_RELATION, out_rel.id)),
            (node, ("issue", parent.id), KIND_RELATION, "follows", (KIND_RELATION, in_rel.id)),
            (node, ("issue", parent.id), KIND_PARENT, None, ("hierarchy", "issue", root.id, parent.id)),
            (node, ("issue", child_a.id), KIND_CHILD, None, ("hierarchy", "issue", child_a.id, root.id)),
            (node, ("issue", child_b.id), KIND_CHILD, None, ("hierarchy", "issue", child_b.id, root.id)),
            (node, ("attachment", attachments[0].id), KIND_ATTACHMENT, None, None),
        ],
        key=repr,
    )
    # The kinds come in a fixed order: relations, parent, children, attachments.
    assert [e[2] for e in out] == [KIND_RELATION, KIND_RELATION, KIND_PARENT, KIND_CHILD, KIND_CHILD, KIND_ATTACHMENT]
    both = _edges(await links._raw_edges(db, node, "both", STRUCTURED))
    assert both == out
    only_children = _edges(await links._raw_edges(db, node, "out", frozenset({KIND_CHILD})))
    assert {e[1] for e in only_children} == {("issue", child_a.id), ("issue", child_b.id)}
    assert _edges(await links._raw_edges(db, node, "out", frozenset({KIND_COMMIT, KIND_PULL_REQUEST}))) == []

    wiki_node = ("wiki_page", home_page.id)
    assert _edges(await links._raw_edges(db, wiki_node, "out", STRUCTURED)) == [
        (
            wiki_node,
            ("wiki_page", sub_page.id),
            KIND_CHILD,
            None,
            ("hierarchy", "wiki_page", sub_page.id, home_page.id),
        ),
        (wiki_node, ("attachment", page_file.id), KIND_ATTACHMENT, None, None),
    ]
    sub_node = ("wiki_page", sub_page.id)
    assert _edges(await links._raw_edges(db, sub_node, "out", STRUCTURED)) == [
        (
            sub_node,
            ("wiki_page", home_page.id),
            KIND_PARENT,
            None,
            ("hierarchy", "wiki_page", sub_page.id, home_page.id),
        ),
    ]
    # Other node types have no structured edges of their own.
    assert _edges(await links._raw_edges(db, ("attachment", page_file.id), "out", STRUCTURED)) == []
