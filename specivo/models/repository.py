"""Repository integration models: a project's connected forge repositories and what they send.

- ``RepositoryIntegration``: one repository connected to one project, with its
  own hook URL (``public_id``) and secret.
- ``RepositoryDelivery``: one webhook delivery received for an integration;
  unique per (integration, forge delivery id), which is what makes a
  redelivery harmless.
- ``RepositoryCommit``: a commit that referenced an issue; unique per
  (integration, sha).
- ``IssueCommitLink``: a commit linked to an issue; its primary key is what
  makes each (commit, issue) journal and command happen once.
- ``PullRequest``: a pull/merge request; unique per (integration, number).
- ``IssuePullRequestLink``: a pull request linked to an issue; its primary
  key makes each (pull request, issue) journal happen once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin

PROVIDERS = ("github", "gitlab", "gitea", "forgejo")

SCOPE_PROJECT = "project"
SCOPE_PROJECT_AND_SUBPROJECTS = "project_and_subprojects"
SCOPES = (SCOPE_PROJECT, SCOPE_PROJECT_AND_SUBPROJECTS)

DELIVERY_ACCEPTED = "accepted"
DELIVERY_DUPLICATE = "duplicate"
DELIVERY_IGNORED = "ignored"
DELIVERY_PROCESSED = "processed"
DELIVERY_FAILED = "failed"
DELIVERY_STATUSES = (DELIVERY_ACCEPTED, DELIVERY_DUPLICATE, DELIVERY_IGNORED, DELIVERY_PROCESSED, DELIVERY_FAILED)

LINK_SOURCES = ("message", "branch")
LINK_KEYWORDS = ("refs", "fixes")

PR_OPEN = "open"
PR_DRAFT = "draft"
PR_MERGED = "merged"
PR_CLOSED = "closed"
PR_STATES = (PR_OPEN, PR_DRAFT, PR_MERGED, PR_CLOSED)
PR_LINK_SOURCES = ("title", "body", "branch")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class RepositoryIntegration(Base, TimestampMixin):
    """A forge repository connected to a project.

    ``secret_encrypted`` is a ``secret_box`` token (purpose
    ``repo-integration-secret``), shown in clear only on create and rotate.
    ``scope`` decides which issues references may resolve to: the project's
    own, or also its subprojects'. ``settings`` holds the commit command
    switches (``commands_enabled``, ``time_logging_enabled``,
    ``fixing_status_id``, ``merge_status_id``, ``merge_automation_enabled``).
    """

    __tablename__ = "repository_integrations"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_repository_integrations_public_id"),
        CheckConstraint(_in("provider", PROVIDERS), name="ck_repository_integrations_provider"),
        CheckConstraint(_in("scope", SCOPES), name="ck_repository_integrations_scope"),
        # The same repository may be connected to several projects, once per project.
        Index(
            "uq_repository_integrations_project_provider_repo",
            "project_id",
            "provider",
            func.lower(text("repo_full_name")),
            unique=True,
        ),
        Index("ix_repository_integrations_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    # The unique expression index leads with project_id, so it also serves as the FK index.
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main", server_default="main")
    scope: Mapped[str] = mapped_column(String(30), nullable=False, default=SCOPE_PROJECT, server_default=SCOPE_PROJECT)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    def __repr__(self) -> str:
        return f"<RepositoryIntegration id={self.id} provider={self.provider} repo={self.repo_full_name!r}>"


class RepositoryDelivery(Base):
    """One webhook delivery received for an integration.

    ``payload`` is kept for processing and the delivery log, without the
    per-commit file lists. Rows older than the retention period are pruned by
    the scheduled cleanup.
    """

    __tablename__ = "repository_deliveries"
    __table_args__ = (
        UniqueConstraint("integration_id", "delivery_id", name="uq_repository_deliveries_integration_delivery"),
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="ck_repository_deliveries_status"),
        # The delivery log, newest first; also serves as the FK index.
        Index("ix_repository_deliveries_integration_received", "integration_id", text("received_at DESC")),
        # The sweeper looks for accepted rows that were never processed.
        Index(
            "ix_repository_deliveries_accepted_received",
            "received_at",
            postgresql_where=text("status = 'accepted'"),
        ),
        Index("ix_repository_deliveries_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    integration_id: Mapped[int] = mapped_column(
        ForeignKey("repository_integrations.id", ondelete="CASCADE"), nullable=False
    )
    delivery_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<RepositoryDelivery id={self.id} event={self.event!r} status={self.status}>"


class RepositoryCommit(Base):
    """A commit seen in a push that referenced at least one issue in scope.

    ``matched_user_id`` is the Specivo user whose *verified* email equals the
    commit author email; the author email itself is not authenticated.
    """

    __tablename__ = "repository_commits"
    __table_args__ = (
        # Leads with integration_id, so it also serves as the FK index.
        UniqueConstraint("integration_id", "sha", name="uq_repository_commits_integration_sha"),
        Index(
            "ix_repository_commits_matched_user_id",
            "matched_user_id",
            postgresql_where=text("matched_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    integration_id: Mapped[int] = mapped_column(
        ForeignKey("repository_integrations.id", ondelete="CASCADE"), nullable=False
    )
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    author_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    author_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    matched_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def first_line(self) -> str:
        return self.message.strip().split("\n", 1)[0].strip() if self.message else ""

    def __repr__(self) -> str:
        return f"<RepositoryCommit id={self.id} sha={self.sha[:12]}>"


class IssueCommitLink(Base):
    """A commit linked to an issue, from its message or its branch name."""

    __tablename__ = "issue_commit_links"
    __table_args__ = (
        CheckConstraint(_in("source", LINK_SOURCES), name="ck_issue_commit_links_source"),
        CheckConstraint(f"keyword IS NULL OR {_in('keyword', LINK_KEYWORDS)}", name="ck_issue_commit_links_keyword"),
        # The primary key leads with commit_id; issue_id needs its own index.
        Index("ix_issue_commit_links_issue_id", "issue_id"),
    )

    commit_id: Mapped[int] = mapped_column(ForeignKey("repository_commits.id", ondelete="CASCADE"), primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    keyword: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<IssueCommitLink commit_id={self.commit_id} issue_id={self.issue_id} source={self.source}>"


class PullRequest(Base, TimestampMixin):
    """A pull request (GitHub, Gitea, Forgejo) or merge request (GitLab) that referenced an issue.

    ``provider_updated_at`` is the forge's update time of the version stored;
    an event carrying an older one is not applied, so events that arrive out
    of order never move the state backwards.
    """

    __tablename__ = "pull_requests"
    __table_args__ = (
        # Leads with integration_id, so it also serves as the FK index.
        UniqueConstraint("integration_id", "number", name="uq_pull_requests_integration_number"),
        CheckConstraint(_in("state", PR_STATES), name="ck_pull_requests_state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    integration_id: Mapped[int] = mapped_column(
        ForeignKey("repository_integrations.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    source_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merged_by_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<PullRequest id={self.id} number={self.number} state={self.state}>"


class IssuePullRequestLink(Base):
    """A pull request linked to an issue, from its title, body or source branch.

    ``closing`` is true when the reference used a fixing keyword. Merge
    automation sets ``merge_handled_at`` when it has acted on the pair, so it
    acts once per issue per merge. A refused move is tried again by later
    deliveries, but ``merge_refused_at`` keeps it to one note on the issue.
    ``merge_qualified_at`` is set on the links that were closing when the
    pull request became merged: automation acts on those only, never on a
    reference or keyword added by editing the text after the merge.
    """

    __tablename__ = "issue_pull_request_links"
    __table_args__ = (
        CheckConstraint(_in("source", PR_LINK_SOURCES), name="ck_issue_pull_request_links_source"),
        # The primary key leads with pull_request_id; issue_id needs its own index.
        Index("ix_issue_pull_request_links_issue_id", "issue_id"),
    )

    pull_request_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    closing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    merge_handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merge_refused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merge_qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<IssuePullRequestLink pull_request_id={self.pull_request_id} issue_id={self.issue_id}>"
