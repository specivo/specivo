"""Helpers for repository webhook tests: integrations, push payloads and signed requests."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.models.project import Project
from specivo.models.repository import RepositoryIntegration

FORGE_FIXTURES = Path(__file__).resolve().parent / "forge"
SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
REPO = "example-org/widgets"


def fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((FORGE_FIXTURES / name).read_bytes())


async def make_integration(
    db: AsyncSession,
    project: Project,
    *,
    provider: str = "github",
    repo: str = REPO,
    secret: str = SECRET,
    scope: str = "project",
    is_active: bool = True,
    created_by_id: int | None = None,
) -> RepositoryIntegration:
    integration = RepositoryIntegration(
        project_id=project.id,
        provider=provider,
        repo_full_name=repo,
        repo_url=f"https://forge.example.com/{repo}",
        secret_encrypted=secret_box.encrypt(secret, "repo-integration-secret"),
        scope=scope,
        is_active=is_active,
        created_by_id=created_by_id,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


def commit(sha_seed: str, message: str, *, name: str = "Alex Example", email: str = "alex@example.com") -> dict:
    sha = hashlib.sha1(sha_seed.encode()).hexdigest()
    return {
        "id": sha,
        "message": message,
        "timestamp": "2026-10-30T10:00:00+07:00",
        "url": f"https://forge.example.com/{REPO}/commit/{sha}",
        "author": {"name": name, "email": email, "username": "alex-example"},
        "added": ["a.txt"],
        "removed": [],
        "modified": ["b.txt"],
    }


def push_payload(
    provider: str,
    commits: list[dict],
    *,
    branch: str = "main",
    repo: str = REPO,
    ref: str | None = None,
) -> dict[str, Any]:
    """A push payload in the provider's shape, built on its fixture."""
    name = {"github": "github_push.json", "gitlab": "gitlab_push.json"}.get(provider, f"{provider}_push.json")
    payload = copy.deepcopy(fixture_payload(name))
    payload["ref"] = ref or f"refs/heads/{branch}"
    payload["commits"] = commits
    if provider == "gitlab":
        payload["project"]["path_with_namespace"] = repo
    else:
        payload["repository"]["full_name"] = repo
        payload["head_commit"] = commits[-1] if commits else None
    return payload


def signed_headers(
    provider: str, body: bytes, *, secret: str = SECRET, event: str = "push", delivery: str | None = None
) -> dict[str, str]:
    """The headers the forge sends with *body*."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json"}
    if provider == "github":
        headers |= {"X-GitHub-Event": event, "X-Hub-Signature-256": "sha256=" + digest}
        if delivery:
            headers["X-GitHub-Delivery"] = delivery
    elif provider == "gitlab":
        gitlab_event = {"push": "Push Hook", "ping": "Push Hook"}.get(event, event)
        headers |= {"X-Gitlab-Event": gitlab_event, "X-Gitlab-Token": secret}
        if delivery:
            headers["X-Gitlab-Event-UUID"] = delivery
    elif provider == "gitea":
        headers |= {"X-Gitea-Event": event, "X-Gitea-Signature": digest, "X-GitHub-Event": event}
        if delivery:
            headers["X-Gitea-Delivery"] = delivery
    else:
        headers |= {
            "X-Forgejo-Event": event,
            "X-Gitea-Event": event,
            "X-Forgejo-Signature": digest,
            "X-Gitea-Signature": digest,
        }
        if delivery:
            headers["X-Forgejo-Delivery"] = delivery
            headers["X-Gitea-Delivery"] = delivery
    return headers


def hook_path(integration: RepositoryIntegration) -> str:
    return f"/hooks/{integration.provider}/{integration.public_id}/"
