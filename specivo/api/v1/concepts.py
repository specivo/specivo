"""A project's AI provider and its concept extraction: status, provider (administrators) and running it now."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_provider_service import AiProviderService, ProviderInput
from specivo.services.concept_extraction import dispatch_extract, extraction_enabled, project_status
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

router = APIRouter(prefix="/projects", tags=["concepts"])
_providers = AiProviderService()
_projects = ProjectService()
_audit = SecurityAuditService()


class AiProviderIn(BaseModel):
    """An OpenAI-compatible chat completions endpoint for the project."""

    base_url: str = Field(
        max_length=500,
        description=(
            "Base URL of an OpenAI-compatible API (the server calls {base_url}/chat/completions), e.g. "
            "https://api.openai.com/v1 or a local server such as http://ollama:11434/v1"
        ),
    )
    model: str = Field(max_length=200, description="Model name as the endpoint expects it")
    provider: str = Field(
        default="openai", max_length=50, description="Provider name used to price the usage, e.g. openai, ollama"
    )
    api_key: str | None = Field(
        default=None,
        max_length=1000,
        description="API key. Omit to keep the stored one, empty string to remove it. Never returned.",
    )


class AiProviderOut(BaseModel):
    provider: str
    base_url: str
    model: str
    has_api_key: bool
    updated_at: datetime | None = None


class ConceptExtractionOut(BaseModel):
    """The project's concept extraction: switch, settings, provider (without its key) and what was extracted."""

    enabled: bool
    confidence_threshold: float
    run_limit: int
    provider: AiProviderOut | None
    concepts: int
    sources_extracted: int
    sources_rejected: int
    last_extracted_at: datetime | None = None


class ConceptRunOut(BaseModel):
    queued: bool


async def _project(db: AsyncSession, key: str, user: User) -> Project:
    try:
        return await _projects.get_readable_by_key(db, key.upper(), user)
    except NotFoundError:
        raise NotFoundError(f"Project '{key.upper()}' not found") from None


async def _require_manage(db: AsyncSession, project: Project, user: User) -> None:
    if user.is_admin or await check_permission(user, project.id, Permission.MANAGE_PROJECT, db):
        return
    raise PermissionDeniedError(f"Permission '{Permission.MANAGE_PROJECT}' denied for this project")


@router.get("/{key}/concept-extraction/", response_model=ConceptExtractionOut)
async def get_concept_extraction(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ConceptExtractionOut:
    """Concept extraction of a project: whether it is on, its settings, the provider and the counts.

    Needs ``manage_project``. Switch it on or off and change the settings with
    ``PATCH /projects/{key}/`` (``concept_extraction``, ``concept_confidence_threshold``,
    ``concept_extraction_run_limit``).
    """
    project = await _project(db, key, current_user)
    await _require_manage(db, project, current_user)
    return ConceptExtractionOut.model_validate(await project_status(db, project))


@router.post("/{key}/concept-extraction/run/", response_model=ConceptRunOut, status_code=status.HTTP_202_ACCEPTED)
async def run_concept_extraction(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> ConceptRunOut:
    """Queue an extraction run now (changed issues and pages only, up to the run limit). Needs ``manage_project``."""
    project = await _project(db, key, current_user)
    await _require_manage(db, project, current_user)
    if not extraction_enabled(project.settings):
        raise ValidationError("Concept extraction is off for this project", field="concept_extraction")
    if await _providers.get(db, project.id) is None:
        raise ValidationError("This project has no AI provider", field="provider")
    return ConceptRunOut(queued=dispatch_extract(project.id))


@router.put("/{key}/ai-provider/", response_model=AiProviderOut)
async def set_ai_provider(
    key: str,
    data: AiProviderIn,
    request: Request,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = DB_SESSION,
) -> AiProviderOut:
    """Set the AI provider the project's content is sent to (instance administrators only). Audited."""
    project = await _project(db, key, current_user)
    row = await _providers.set(
        db,
        project,
        ProviderInput(base_url=data.base_url, model=data.model, provider=data.provider, api_key=data.api_key),
        current_user,
    )
    api_key = "kept" if data.api_key is None else ("set" if data.api_key.strip() else "removed")
    await _audit.log_event(
        db,
        AuditEvent.PROJECT_AI_PROVIDER_CHANGED,
        user_id=current_user.id,
        project_id=project.id,
        resource_type="project",
        resource_id=project.id,
        details={
            "op": "set",
            "project_key": project.key,
            "provider": row.provider,
            "model": row.model,
            "host": urlsplit(row.base_url).hostname,
            "api_key": api_key,
        },
        request=request,
    )
    await db.flush()
    await db.refresh(row)
    described = AiProviderService.describe(row)
    return AiProviderOut.model_validate(described)


@router.delete("/{key}/ai-provider/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_provider(
    key: str,
    request: Request,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = DB_SESSION,
) -> None:
    """Remove the project's AI provider (instance administrators only); concept extraction is switched off.

    Concepts already extracted stay, hidden while the extraction is off.
    """
    project = await _project(db, key, current_user)
    if not await _providers.delete(db, project):
        raise NotFoundError("This project has no AI provider")
    await _audit.log_event(
        db,
        AuditEvent.PROJECT_AI_PROVIDER_CHANGED,
        user_id=current_user.id,
        project_id=project.id,
        resource_type="project",
        resource_id=project.id,
        details={"op": "removed", "project_key": project.key},
        request=request,
    )
