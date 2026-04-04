"""Incoming GitLab push webhook handler.

Validates the X-Gitlab-Token header against the stored gitlab_webhook_token
setting. Parses commit messages for issue references and creates journal
entries linking commits to issues.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError
from specivo.hooks.refs import link_commit_to_issues
from specivo.models.setting import Setting

logger = logging.getLogger(__name__)

router = APIRouter()


async def _validate_gitlab_token(request: Request, db: AsyncSession) -> None:
    """Validate X-Gitlab-Token header against stored setting."""
    token = request.headers.get("X-Gitlab-Token")
    if not token:
        raise AppError(
            code="webhook_unauthorized",
            message="Missing X-Gitlab-Token header",
            status_code=401,
        )

    result = await db.execute(select(Setting).where(Setting.key == "gitlab_webhook_token"))
    setting = result.scalar_one_or_none()
    import hmac

    if setting is None or not setting.value or not hmac.compare_digest(setting.value, token):
        raise AppError(
            code="webhook_unauthorized",
            message="Invalid GitLab webhook token",
            status_code=401,
        )


@router.post("/gitlab/push/")
async def gitlab_push(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive GitLab push webhook.

    Validates X-Gitlab-Token header.
    Parses commit messages for 'refs #PROJECT-NNN' patterns.
    Creates journal entries linking commits to referenced issues.
    """
    await _validate_gitlab_token(request, db)

    payload = await request.json()
    commits = payload.get("commits", [])

    linked_count = 0
    for commit in commits:
        commit_id = commit.get("id", "")
        message = commit.get("message", "")
        url = commit.get("url", "")
        author = commit.get("author", {})
        author_name = author.get("name", "Unknown")

        linked = await link_commit_to_issues(
            session=db,
            commit_id=commit_id,
            commit_message=message,
            commit_url=url,
            author_name=author_name,
        )
        linked_count += len(linked)

    logger.info("GitLab push: processed %d commits, linked %d issues", len(commits), linked_count)
    return {"status": "ok", "linked": linked_count}
