"""Schemas for the entity link graph (``/issues/{ref}/links/``, ``/projects/{key}/wiki/{slug}/links/``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LinkNodeOut(BaseModel):
    """An issue, wiki page, attachment, commit or pull request, or a tag, component, metadata value, version,
    sprint or user, in a link graph."""

    model_config = {"from_attributes": True}

    ref: str = Field(
        description=(
            "ACME-12, wiki:ACME/slug, attachment:ID, commit:SHA, pr:ID, tag:ACME/name, component:ACME/value, "
            "metadata:ACME/key/value, version:ACME/name, sprint:ACME/name, concept:ACME/name or user:login"
        )
    )
    type: str = Field(
        description=(
            "issue, wiki_page, attachment, commit, pull_request, tag, component, metadata, version, sprint, user "
            "or concept"
        )
    )
    title: str
    status: str | None = Field(
        default=None,
        description=(
            "Issue status name, pull request state, version or sprint status, or a concept's kind (feature, "
            "external_system, decision, term)"
        ),
    )
    url: str | None = None
    project: str | None = Field(default=None, description="Project key of an issue, wiki page or project-level node")
    degree: int | None = Field(
        default=None,
        description=(
            "For a tag, component, metadata value, version, sprint, user or concept: the items you may see that "
            "point at it (for a concept, its mentions at or above the confidence threshold)"
        ),
    )


class LinkEdgeOut(BaseModel):
    """A link between two nodes; ``source`` and ``target`` are node refs."""

    model_config = {"from_attributes": True}

    source: str
    target: str
    kind: str = Field(
        description=(
            "Text links: wiki_link, mention, attachment_link. Typed links (an accepted suggestion): typed_link. "
            "Structured: relation, parent, child, attachment, commit, pull_request. "
            "Structured nodes: tagged, in_component, in_metadata, targets_version, in_sprint, assigned_to. "
            "Concepts (extracted by the project's AI provider): mentions. "
            "Suggested links: similar (only when asked for)."
        )
    )
    direction: str | None = Field(
        default=None, description="out or in, relative to the root; null for edges between two neighbours"
    )
    label: str | None = Field(
        default=None,
        description=(
            "The link text as written, when it differs from the target; for typed_link its type (documents, "
            "implements, decided_in, supersedes, see_also); for similar the cosine score, e.g. 0.874; for mentions "
            "the model's confidence, e.g. 0.85"
        ),
    )
    relation_type: str | None = Field(default=None, description="For relations, labelled from the source's side")
    via: str | None = Field(default=None, description="The comment the link is written in, e.g. ACME-12#3")
    broken: bool = Field(default=False, description="The target does not exist; target is the ref as written")
    suggested: bool = Field(
        default=False,
        description=(
            "A suggestion, not a link anyone made: kind similar (similar content), or kind mentions below the "
            "project's confidence threshold"
        ),
    )
    passage: str | None = Field(
        default=None, description="For mentions: the passage of the issue or page that mentions the concept"
    )


class LinkGraphOut(BaseModel):
    """The neighbours of one issue or wiki page that the reader may see."""

    root: LinkNodeOut | None
    nodes: list[LinkNodeOut]
    edges: list[LinkEdgeOut]
    truncated: bool = Field(description="True when the limit left neighbours out")


class LinkSuggestionDecisionIn(BaseModel):
    """Accept or dismiss the suggested link between two items."""

    ref: str = Field(
        min_length=1,
        max_length=300,
        description="The issue (ACME-12) or wiki page (wiki:ACME/slug) the link goes from; permission is checked here",
    )
    other_ref: str = Field(min_length=1, max_length=300, description="The other end: an issue or wiki page ref")
    link_type: str | None = Field(
        default=None,
        max_length=20,
        description=(
            "When accepting. Two issues: a relation type, read 'ref <type> other_ref' (default relates). An issue "
            "and a page, or two pages: documents, implements, decided_in, supersedes or see_also (default see_also)"
        ),
    )


class LinkSuggestionDecisionOut(BaseModel):
    """What accepting or dismissing a suggested link did."""

    op: str = Field(description="accept or dismiss")
    status: str = Field(description="accepted or dismissed")
    ref: str
    other_ref: str
    link_kind: str | None = Field(default=None, description="relation or typed_link, when accepted")
    link_type: str | None = Field(default=None, description="The relation type or typed link type, when accepted")
    relation_id: int | None = Field(default=None, description="The relation created between two issues")
    message: str
