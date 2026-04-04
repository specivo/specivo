"""Admin project operations — rename, archive/unarchive (superadmin only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.project import ProjectOut, ProjectRenameOut, ProjectRenameRequest
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

router = APIRouter(tags=["admin-projects"])
_service = ProjectService()
_audit = SecurityAuditService()


@router.post("/admin/projects/{key}/rename/", response_model=ProjectRenameOut)
async def rename_project(
    key: str,
    data: ProjectRenameRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> ProjectRenameOut:
    """Rename a project key and/or identifier. Superadmin only.

    Re-keys all existing issues atomically. Old key is stored as an alias
    so API lookups using the retired key still resolve.
    """
    project = await _service.get_by_key(db, key.upper())
    old_key = project.key
    old_identifier = project.identifier

    project, issues_rekeyed = await _service.rename(
        session=db,
        project=project,
        new_key=data.new_key,
        new_identifier=data.new_identifier,
        admin_user=admin,
    )

    # Audit log
    try:
        details: dict = {"issues_rekeyed": issues_rekeyed}
        if data.new_key and data.new_key != old_key:
            details["old_key"] = old_key
            details["new_key"] = data.new_key
        if data.new_identifier and data.new_identifier != old_identifier:
            details["old_identifier"] = old_identifier
            details["new_identifier"] = data.new_identifier
        if data.reason:
            details["reason"] = data.reason
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.PROJECT_KEY_RENAMED,
            user_id=admin.id,
            project_id=project.id,
            details=details,
            request=request,
        )
    except Exception:
        pass

    parent_key = await _service.get_parent_key(db, project)
    out = ProjectRenameOut.model_validate(project)
    out.parent_key = parent_key
    out.old_key = old_key if old_key != project.key else None
    out.old_identifier = old_identifier if old_identifier != project.identifier else None
    out.issues_rekeyed = issues_rekeyed
    return out


@router.post("/admin/projects/{key}/archive/", response_model=ProjectOut)
async def archive_project(
    key: str,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """Archive a project. Superadmin only."""
    project = await _service.get_by_key(db, key.upper())
    project.status = 9  # archived
    await db.commit()
    await db.refresh(project)
    return ProjectOut.model_validate(project)


@router.post("/admin/projects/{key}/unarchive/", response_model=ProjectOut)
async def unarchive_project(
    key: str,
    admin: Annotated[User, Depends(require_admin_api)],
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """Unarchive a project (restore to active). Superadmin only."""
    project = await _service.get_by_key(db, key.upper())
    project.status = 1  # active
    await db.commit()
    await db.refresh(project)
    return ProjectOut.model_validate(project)
