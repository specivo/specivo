"""Incoming GitHub push webhook handler.

Validates the X-Hub-Signature-256 header using HMAC-SHA256 with the stored
github_webhook_secret setting. Parses commit messages for issue references
and creates journal entries linking commits to issues.
"""

from __future__ import annotations

import hashlib
import hmac
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


async def _validate_github_signature(request: Request, body: bytes, db: AsyncSession) -> None:
    """Validate X-Hub-Signature-256 header using HMAC-SHA256."""
    signature_header = request.headers.get("X-Hub-Signature-256")
    if not signature_header:
        raise AppError(
            code="webhook_unauthorized",
            message="Missing X-Hub-Signature-256 header",
            status_code=401,
        )

    result = await db.execute(select(Setting).where(Setting.key == "github_webhook_secret"))
    setting = result.scalar_one_or_none()
    if setting is None or not setting.value:
        raise AppError(
            code="webhook_unauthorized",
            message="GitHub webhook secret not configured",
            status_code=401,
        )

    expected = "sha256=" + hmac.new(setting.value.encode(), body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature_header, expected):
        raise AppError(
            code="webhook_unauthorized",
            message="Invalid GitHub webhook signature",
            status_code=401,
        )


@router.post("/github/push")
async def github_push(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive GitHub push webhook.

    Validates X-Hub-Signature-256 HMAC header.
    Parses commit messages for 'refs #PROJECT-NNN' patterns.
    Creates journal entries linking commits to referenced issues.
    """
    body = await request.body()
    await _validate_github_signature(request, body, db)

    import json

    payload = json.loads(body)
    commits = payload.get("commits", [])

    linked_count = 0
    for commit in commits:
        commit_id = commit.get("id", "")
        message = commit.get("message", "")
        url = commit.get("url", "")
        author = commit.get("author", {})
        author_name = author.get("name", author.get("username", "Unknown"))

        linked = await link_commit_to_issues(
            session=db,
            commit_id=commit_id,
            commit_message=message,
            commit_url=url,
            author_name=author_name,
        )
        linked_count += len(linked)

    logger.info("GitHub push: processed %d commits, linked %d issues", len(commits), linked_count)
    return {"status": "ok", "linked": linked_count}
