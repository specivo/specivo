"""RepositoryIntegrationService — connect forge repositories to a project and read their delivery log.

Each integration gets its own hook URL (``/hooks/{provider}/{public_id}/``)
and secret. The secret is generated here (32 random bytes, hex), stored as a
``secret_box`` token (purpose ``repo-integration-secret``) and returned in
clear only by the call that set it: create and rotate. Rotation replaces it at
once; there is no grace period for the old one.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, NotFoundError, ValidationError
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.repository import (
    IssueCommitLink,
    IssuePullRequestLink,
    PullRequest,
    RepositoryCommit,
    RepositoryDelivery,
    RepositoryIntegration,
)
from specivo.models.role import Role, RoleBuiltin
from specivo.models.user import User
from specivo.schemas.repository import (
    DevelopmentBranchOut,
    DevelopmentCommitOut,
    DevelopmentPullRequestOut,
    IssueDevelopmentOut,
    RepositoryIntegrationCreate,
    RepositoryIntegrationOut,
    RepositoryIntegrationSecretOut,
    RepositorySettings,
)

logger = logging.getLogger(__name__)

REPO_INTEGRATION_SECRET_PURPOSE = "repo-integration-secret"
#: The seeded role ``$git`` gets for merge automation unless a manager picks another.
AUTOMATION_ROLE_NAME = "Automation"


def generate_secret() -> str:
    """A new integration secret: 32 random bytes, hex encoded."""
    return secrets.token_hex(32)


def hook_path(integration: RepositoryIntegration) -> str:
    """The hook path, stealth prefix included."""
    prefix = get_settings().stealth_prefix.rstrip("/")
    return f"{prefix}/hooks/{integration.provider}/{integration.public_id}/"


def hook_url(integration: RepositoryIntegration) -> str:
    """The absolute URL to paste into the forge, built on ``APP_URL``."""
    return get_settings().app_url.rstrip("/") + hook_path(integration)


def integration_out(integration: RepositoryIntegration, git_role_id: int | None = None) -> RepositoryIntegrationOut:
    """The API shape; *git_role_id* is the project role ``$git`` holds, shown while merge automation is on."""
    settings = RepositorySettings.model_validate(integration.settings or {}, strict=False)
    return RepositoryIntegrationOut(
        id=integration.id,
        public_id=integration.public_id,
        project_id=integration.project_id,
        provider=integration.provider,
        repo_full_name=integration.repo_full_name,
        repo_url=integration.repo_url,
        default_branch=integration.default_branch,
        scope=integration.scope,
        settings=settings,
        is_active=integration.is_active,
        last_delivery_at=integration.last_delivery_at,
        hook_url=hook_url(integration),
        automation_role_id=git_role_id if settings.merge_automation_enabled else None,
        created_at=integration.created_at,
        updated_at=integration.updated_at,
    )


def integration_secret_out(
    integration: RepositoryIntegration, secret: str, git_role_id: int | None = None
) -> RepositoryIntegrationSecretOut:
    return RepositoryIntegrationSecretOut(**integration_out(integration, git_role_id).model_dump(), secret=secret)


class RepositoryAlreadyConnectedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="repository_already_connected",
            message="This repository is already connected to the project.",
            status_code=409,
            field="repo_full_name",
        )


async def automation_role(session: AsyncSession) -> Role | None:
    """The seeded Automation role (created by the migration), if it still exists."""
    return (
        await session.execute(select(Role).where(Role.name == AUTOMATION_ROLE_NAME, Role.builtin == RoleBuiltin.CUSTOM))
    ).scalar_one_or_none()


async def git_role_id(session: AsyncSession, project_id: int) -> int | None:
    """The (first) project role ``$git`` holds in the project, or None without a membership."""
    from specivo.services.repository_hooks import GIT_LOGIN

    return (
        await session.execute(
            select(MemberRole.role_id)
            .join(Member, Member.id == MemberRole.member_id)
            .join(User, User.id == Member.user_id)
            .where(User.login == GIT_LOGIN, Member.project_id == project_id)
            .order_by(MemberRole.id)
            .limit(1)
        )
    ).scalar_one_or_none()


class RepositoryIntegrationService:
    """Stateless service for repository integrations.

    Merge automation runs as ``$git`` with a real project role. Turning it on
    gives ``$git`` a membership in the project holding exactly that role (the
    Automation role unless the caller picks one); turning it off on the
    project's last integration that had it removes the membership.
    """

    async def _validate_settings(self, session: AsyncSession, settings: RepositorySettings) -> None:
        for name in ("fixing_status_id", "merge_status_id"):
            status_id = getattr(settings, name)
            if status_id is not None and await session.get(IssueStatus, status_id) is None:
                raise ValidationError(f"Status {status_id} does not exist", field=f"settings.{name}")

    async def _git_role(self, session: AsyncSession, project_id: int, role_id: int | None) -> Role:
        """The role ``$git`` should hold: *role_id*, else its current one, else Automation."""
        if role_id is None:
            role_id = await git_role_id(session, project_id)
        if role_id is None:
            role = await automation_role(session)
            if role is None:
                raise ValidationError("Pick a project role for merge automation", field="automation_role_id")
            return role
        role = await session.get(Role, role_id)
        if role is None:
            raise NotFoundError(f"Role {role_id} not found")
        if role.builtin != RoleBuiltin.CUSTOM:
            raise ValidationError("Builtin roles cannot be given to a membership", field="automation_role_id")
        return role

    async def _sync_git_membership(self, session: AsyncSession, project_id: int, role_id: int | None) -> None:
        """Give ``$git`` its role while an integration of the project has merge automation on; else no membership."""
        from specivo.services.repository_hooks import get_git_user

        git = await get_git_user(session)
        integrations = await self.list_for_project(session, project_id)
        wanted = any(
            RepositorySettings.model_validate(i.settings or {}, strict=False).merge_automation_enabled
            for i in integrations
        )
        member = (
            await session.execute(select(Member).where(Member.user_id == git.id, Member.project_id == project_id))
        ).scalar_one_or_none()
        if not wanted:
            if member is not None:
                await session.delete(member)
                await session.flush()
                logger.info("Merge automation off in project %d: $git membership removed", project_id)
            return
        role = await self._git_role(session, project_id, role_id)
        if member is None:
            member = Member(user_id=git.id, project_id=project_id)
            session.add(member)
            await session.flush()
        current = list(
            (await session.execute(select(MemberRole).where(MemberRole.member_id == member.id))).scalars().all()
        )
        if [mr.role_id for mr in current] == [role.id]:
            return
        for member_role in current:
            await session.delete(member_role)
        await session.flush()
        session.add(MemberRole(member_id=member.id, role_id=role.id))
        await session.flush()
        logger.info("Merge automation in project %d: $git holds role %d", project_id, role.id)

    async def git_role_id(self, session: AsyncSession, project_id: int) -> int | None:
        return await git_role_id(session, project_id)

    async def _flush_unique(self, session: AsyncSession) -> None:
        """Flush in a savepoint so a duplicate repository is a 409, not a broken transaction."""
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            if "uq_repository_integrations_project_provider_repo" in str(exc.orig):
                raise RepositoryAlreadyConnectedError() from None
            raise

    async def create(
        self,
        session: AsyncSession,
        project_id: int,
        data: RepositoryIntegrationCreate,
        actor: User,
    ) -> tuple[RepositoryIntegration, str]:
        """Connect a repository; returns the integration and its secret in clear."""
        await self._validate_settings(session, data.settings)
        raw_secret = generate_secret()
        integration = RepositoryIntegration(
            project_id=project_id,
            provider=data.provider,
            repo_full_name=data.repo_full_name,
            repo_url=data.repo_url,
            secret_encrypted=secret_box.encrypt(raw_secret, REPO_INTEGRATION_SECRET_PURPOSE),
            scope=data.scope,
            settings=data.settings.model_dump(),
            is_active=True,
            created_by_id=actor.id,
        )
        session.add(integration)
        await self._flush_unique(session)
        if data.settings.merge_automation_enabled:
            await self._sync_git_membership(session, project_id, data.automation_role_id)
        await session.refresh(integration)
        logger.info("Repository integration %d created for project %d (%s)", integration.id, project_id, data.provider)
        return integration, raw_secret

    async def list_for_project(self, session: AsyncSession, project_id: int) -> list[RepositoryIntegration]:
        stmt = (
            select(RepositoryIntegration)
            .where(RepositoryIntegration.project_id == project_id)
            .order_by(RepositoryIntegration.created_at.desc(), RepositoryIntegration.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def get(self, session: AsyncSession, integration_id: int, project_id: int) -> RepositoryIntegration:
        """One integration of the project; NotFoundError for another project's."""
        stmt = select(RepositoryIntegration).where(
            RepositoryIntegration.id == integration_id, RepositoryIntegration.project_id == project_id
        )
        integration = (await session.execute(stmt)).scalar_one_or_none()
        if integration is None:
            raise NotFoundError("Repository integration not found")
        return integration

    async def update(
        self,
        session: AsyncSession,
        integration_id: int,
        project_id: int,
        data: dict[str, Any],
        *,
        actor: User,
    ) -> RepositoryIntegration:
        """Apply a partial update (``RepositoryIntegrationUpdate`` fields that were set).

        The actor becomes the integration's ``created_by``: whoever last
        configured it is the one whose rights bound its subproject scope, so
        editing an administrator's integration never borrows their rights.
        """
        integration = await self.get(session, integration_id, project_id)
        integration.created_by_id = actor.id
        for field in ("repo_full_name", "repo_url", "scope", "is_active"):
            if data.get(field) is not None:
                setattr(integration, field, data[field])
        if data.get("settings") is not None:
            settings = RepositorySettings.model_validate(data["settings"])
            await self._validate_settings(session, settings)
            integration.settings = settings.model_dump()
        await self._flush_unique(session)
        if data.get("settings") is not None or data.get("automation_role_id") is not None:
            await self._sync_git_membership(session, project_id, data.get("automation_role_id"))
        await session.refresh(integration)
        return integration

    async def rotate_secret(
        self, session: AsyncSession, integration_id: int, project_id: int, *, actor: User
    ) -> tuple[RepositoryIntegration, str]:
        """Replace the secret; the old one stops working at once.

        The actor, who now holds the secret, becomes ``created_by`` (see ``update``).
        """
        integration = await self.get(session, integration_id, project_id)
        integration.created_by_id = actor.id
        raw_secret = generate_secret()
        integration.secret_encrypted = secret_box.encrypt(raw_secret, REPO_INTEGRATION_SECRET_PURPOSE)
        await session.flush()
        await session.refresh(integration)
        logger.info("Repository integration %d secret rotated", integration.id)
        return integration, raw_secret

    async def delete(self, session: AsyncSession, integration_id: int, project_id: int) -> None:
        """Disconnect: the integration, its deliveries and its commits go; journals stay as history."""
        integration = await self.get(session, integration_id, project_id)
        await session.delete(integration)
        await session.flush()
        await self._sync_git_membership(session, project_id, None)

    async def list_deliveries(
        self,
        session: AsyncSession,
        integration: RepositoryIntegration,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[RepositoryDelivery], int]:
        """The integration's deliveries, newest first, and their total count."""
        base = select(RepositoryDelivery).where(RepositoryDelivery.integration_id == integration.id)
        total = await session.scalar(select(func.count()).select_from(base.subquery()))
        rows = await session.execute(
            base.order_by(RepositoryDelivery.received_at.desc(), RepositoryDelivery.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars().all()), int(total or 0)


async def commits_for_issue(session: AsyncSession, issue_id: int, limit: int) -> tuple[list[RepositoryCommit], int]:
    """The commits linked to an issue, newest first, at most *limit*, and how many there are.

    The caller has already checked that the user may see the issue.
    """
    base = (
        select(RepositoryCommit)
        .join(IssueCommitLink, IssueCommitLink.commit_id == RepositoryCommit.id)
        .where(IssueCommitLink.issue_id == issue_id)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.execute(
        base.order_by(
            RepositoryCommit.committed_at.desc().nulls_last(),
            RepositoryCommit.created_at.desc(),
            RepositoryCommit.id.desc(),
        ).limit(limit)
    )
    return list(rows.scalars().all()), int(total or 0)


def branch_url(provider: str, repo_url: str, branch: str) -> str | None:
    """The forge's web page for *branch*, or None when the repository URL is not a web URL."""
    if not repo_url.startswith(("https://", "http://")):
        return None
    path = {"github": "tree", "gitlab": "-/tree", "gitea": "src/branch", "forgejo": "src/branch"}.get(provider)
    if path is None:
        return None
    return f"{repo_url.rstrip('/')}/{path}/{quote(branch, safe='/')}"


async def development_for_issue(session: AsyncSession, issue_id: int) -> IssueDevelopmentOut:
    """Commits, pull requests and branches linked to an issue, in three queries.

    Commits newest first (at most ``DEVELOPMENT_COMMIT_LIMIT``, with the
    total), pull requests open first then newest, and the branches of both
    except each repository's default branch. The caller has already checked
    that the user may see the issue.
    """
    from specivo.core.constants import DEVELOPMENT_COMMIT_LIMIT, DEVELOPMENT_PULL_REQUEST_LIMIT

    commits, total = await commits_for_issue(session, issue_id, DEVELOPMENT_COMMIT_LIMIT)
    integration_ids = {c.integration_id for c in commits}
    pr_rows = (
        await session.execute(
            select(PullRequest, IssuePullRequestLink.closing)
            .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
            .where(IssuePullRequestLink.issue_id == issue_id)
            .order_by(
                PullRequest.state.in_(("open", "draft")).desc(),
                PullRequest.provider_updated_at.desc().nulls_last(),
                PullRequest.id.desc(),
            )
            .limit(DEVELOPMENT_PULL_REQUEST_LIMIT)
        )
    ).all()
    integration_ids |= {pr.integration_id for pr, _closing in pr_rows}
    integrations = (
        {
            i.id: i
            for i in (
                await session.execute(
                    select(RepositoryIntegration).where(RepositoryIntegration.id.in_(integration_ids))
                )
            ).scalars()
        }
        if integration_ids
        else {}
    )

    branches: dict[tuple[int, str], DevelopmentBranchOut] = {}

    def _branch(integration: RepositoryIntegration, name: str | None) -> None:
        if not name or name == integration.default_branch or (integration.id, name) in branches:
            return
        branches[(integration.id, name)] = DevelopmentBranchOut(
            name=name,
            url=branch_url(integration.provider, integration.repo_url, name),
            repository=integration.repo_full_name,
            provider=integration.provider,
        )

    pull_requests = []
    for pr, closing in pr_rows:
        integration = integrations[pr.integration_id]
        _branch(integration, pr.source_branch)
        pull_requests.append(
            DevelopmentPullRequestOut(
                number=pr.number,
                title=pr.title,
                url=pr.url,
                state=pr.state,
                source_branch=pr.source_branch,
                target_branch=pr.target_branch,
                author_login=pr.author_login,
                merged_at=pr.merged_at,
                closing=closing,
                repository=integration.repo_full_name,
                provider=integration.provider,
            )
        )
    commits_out = []
    for c in commits:
        integration = integrations[c.integration_id]
        _branch(integration, c.branch)
        commits_out.append(
            DevelopmentCommitOut(
                sha=c.sha,
                short_sha=c.short_sha,
                first_line=c.first_line,
                url=c.url,
                branch=c.branch,
                author_name=c.author_name,
                committed_at=c.committed_at,
                repository=integration.repo_full_name,
                provider=integration.provider,
            )
        )
    return IssueDevelopmentOut(
        commit_count=total, commits=commits_out, pull_requests=pull_requests, branches=list(branches.values())
    )
