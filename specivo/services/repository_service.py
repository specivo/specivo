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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.repository import IssueCommitLink, RepositoryCommit, RepositoryDelivery, RepositoryIntegration
from specivo.models.user import User
from specivo.schemas.repository import (
    RepositoryIntegrationCreate,
    RepositoryIntegrationOut,
    RepositoryIntegrationSecretOut,
    RepositorySettings,
)

logger = logging.getLogger(__name__)

REPO_INTEGRATION_SECRET_PURPOSE = "repo-integration-secret"


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


def integration_out(integration: RepositoryIntegration) -> RepositoryIntegrationOut:
    return RepositoryIntegrationOut(
        id=integration.id,
        public_id=integration.public_id,
        project_id=integration.project_id,
        provider=integration.provider,
        repo_full_name=integration.repo_full_name,
        repo_url=integration.repo_url,
        default_branch=integration.default_branch,
        scope=integration.scope,
        settings=RepositorySettings.model_validate(integration.settings or {}, strict=False),
        is_active=integration.is_active,
        last_delivery_at=integration.last_delivery_at,
        hook_url=hook_url(integration),
        created_at=integration.created_at,
        updated_at=integration.updated_at,
    )


def integration_secret_out(integration: RepositoryIntegration, secret: str) -> RepositoryIntegrationSecretOut:
    return RepositoryIntegrationSecretOut(**integration_out(integration).model_dump(), secret=secret)


class RepositoryAlreadyConnectedError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="repository_already_connected",
            message="This repository is already connected to the project.",
            status_code=409,
            field="repo_full_name",
        )


class RepositoryIntegrationService:
    """Stateless service for repository integrations."""

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
    ) -> RepositoryIntegration:
        """Apply a partial update (``RepositoryIntegrationUpdate`` fields that were set)."""
        integration = await self.get(session, integration_id, project_id)
        for field in ("repo_full_name", "repo_url", "scope", "is_active"):
            if data.get(field) is not None:
                setattr(integration, field, data[field])
        if data.get("settings") is not None:
            integration.settings = RepositorySettings.model_validate(data["settings"]).model_dump()
        await self._flush_unique(session)
        await session.refresh(integration)
        return integration

    async def rotate_secret(
        self, session: AsyncSession, integration_id: int, project_id: int
    ) -> tuple[RepositoryIntegration, str]:
        """Replace the secret; the old one stops working at once."""
        integration = await self.get(session, integration_id, project_id)
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
