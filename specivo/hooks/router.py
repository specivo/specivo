"""Incoming webhooks router — GitLab and GitHub push events."""

from fastapi import APIRouter

from specivo.hooks.github import router as github_router
from specivo.hooks.gitlab import router as gitlab_router

hooks_router = APIRouter(prefix="/hooks", tags=["hooks"])
hooks_router.include_router(gitlab_router)
hooks_router.include_router(github_router)
