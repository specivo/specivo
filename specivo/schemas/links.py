"""Schemas for the entity link graph (``/issues/{ref}/links/``, ``/projects/{key}/wiki/{slug}/links/``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LinkNodeOut(BaseModel):
    """An issue, wiki page, attachment, commit or pull request in a link graph."""

    model_config = {"from_attributes": True}

    ref: str = Field(description="ACME-12, wiki:ACME/slug, attachment:ID, commit:SHA or pr:ID")
    type: str = Field(description="issue, wiki_page, attachment, commit or pull_request")
    title: str
    status: str | None = Field(default=None, description="Issue status name, or pull request state")
    url: str | None = None
    project: str | None = Field(default=None, description="Project key of an issue or wiki page")


class LinkEdgeOut(BaseModel):
    """A link between two nodes; ``source`` and ``target`` are node refs."""

    model_config = {"from_attributes": True}

    source: str
    target: str
    kind: str = Field(
        description=(
            "Text links: wiki_link, mention, attachment_link. "
            "Structured: relation, parent, child, attachment, commit, pull_request."
        )
    )
    direction: str | None = Field(
        default=None, description="out or in, relative to the root; null for edges between two neighbours"
    )
    label: str | None = Field(default=None, description="The link text as written, when it differs from the target")
    relation_type: str | None = Field(default=None, description="For relations, labelled from the source's side")
    via: str | None = Field(default=None, description="The comment the link is written in, e.g. ACME-12#3")
    broken: bool = Field(default=False, description="The target does not exist; target is the ref as written")


class LinkGraphOut(BaseModel):
    """The neighbours of one issue or wiki page that the reader may see."""

    root: LinkNodeOut | None
    nodes: list[LinkNodeOut]
    edges: list[LinkEdgeOut]
    truncated: bool = Field(description="True when the limit left neighbours out")
