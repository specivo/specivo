"""MetadataTarget registry — pluggable targets for ``specivo_metadata``.

The ``specivo_metadata`` MCP tool applies per-key operations to the
``metadata`` JSON blob on a variety of content entities.  Rather than
hard-coding which entities are supported, the core exposes a
``MetadataTarget`` Protocol and a singleton ``MetadataTargetRegistry``.

Each target implements a small, uniform interface:

* ``scheme`` — string prefix used in ``target_ref`` (e.g. ``"issue"``)
* ``content_type`` — value used on ``metadata_schemas.content_type`` when
  validating updates
* ``permission`` — permission required to mutate metadata
* ``resolve`` — parse the ref and return the entity
* ``get_metadata`` / ``set_metadata`` — read/write the blob
* ``project_id_of`` — used for permission checks

Core registers ``IssueMetadataTarget`` at import time.  Plugins can
register additional targets in their ``on_register`` / ``on_startup``
hooks without requiring any changes to core code.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.user import User


@runtime_checkable
class MetadataTarget(Protocol):
    """Protocol implemented by every metadata target."""

    scheme: str
    content_type: str
    permission: str

    async def resolve(
        self,
        session: AsyncSession,
        ref: str,
        user: User,
    ) -> Any:
        """Resolve *ref* to an ORM entity.

        ``ref`` is the portion of ``target_ref`` following the scheme
        prefix — e.g. for ``"issue:ACME-12"`` the target receives
        ``"ACME-12"``.  Implementations may raise ``NotFoundError``.
        """
        ...

    def get_metadata(self, entity: Any) -> dict:
        """Return a shallow copy of the entity's metadata blob."""
        ...

    async def set_metadata(
        self,
        session: AsyncSession,
        entity: Any,
        metadata: dict,
        user: User,
        api_key_id: int | None = None,
    ) -> Any:
        """Persist *metadata* onto *entity*.

        Implementations are responsible for optimistic-locking,
        journaling (where applicable), and any schema validation.
        """
        ...

    def project_id_of(self, entity: Any) -> int:
        """Return the project_id used for permission checks."""
        ...

    def display_ref(self, entity: Any) -> str:
        """Return a human-readable identifier for log / response strings."""
        ...


class MetadataTargetRegistry:
    """Singleton registry mapping scheme -> target implementation."""

    def __init__(self) -> None:
        self._targets: dict[str, MetadataTarget] = {}

    def register(self, target: MetadataTarget) -> None:
        """Register *target* under its ``scheme``.

        Registering the same scheme twice overwrites the previous
        entry — useful for tests and plugin hot-reloads.
        """
        self._targets[target.scheme] = target

    def unregister(self, scheme: str) -> None:
        self._targets.pop(scheme, None)

    def get(self, scheme: str) -> MetadataTarget | None:
        return self._targets.get(scheme)

    def schemes(self) -> list[str]:
        return sorted(self._targets.keys())

    def parse_ref(self, target_ref: str) -> tuple[str, str]:
        """Split *target_ref* into ``(scheme, ref)``.

        Accepts ``"scheme:ref"``.  When no scheme is present, defaults
        to ``"issue"`` for backward compatibility with bare issue refs
        like ``"ACME-12"``.
        """
        if ":" in target_ref:
            scheme, _, ref = target_ref.partition(":")
            return scheme, ref
        return "issue", target_ref


_registry: MetadataTargetRegistry | None = None


def get_metadata_target_registry() -> MetadataTargetRegistry:
    """Return the process-wide singleton, creating it on first use."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = MetadataTargetRegistry()
        _registry.register(IssueMetadataTarget())
    return _registry


# ---------------------------------------------------------------------------
# Core target: issues
# ---------------------------------------------------------------------------


class IssueMetadataTarget:
    """Metadata target for :class:`specivo.models.issue.Issue`."""

    scheme = "issue"
    content_type = "issue"
    permission = "edit_issues"

    async def resolve(
        self,
        session: AsyncSession,
        ref: str,
        user: User,
    ) -> Any:
        from specivo.services.issue_service import IssueService

        svc = IssueService()
        return await svc.get_by_display_key(session, ref, user=user)

    def get_metadata(self, entity: Any) -> dict:
        import copy

        # Deep copy so callers can mutate freely without touching the
        # ORM-managed attribute — essential for SQLAlchemy to detect the
        # eventual reassignment as a dirty change on the JSONB column.
        return copy.deepcopy(entity.issue_metadata or {})

    async def set_metadata(
        self,
        session: AsyncSession,
        entity: Any,
        metadata: dict,
        user: User,
        api_key_id: int | None = None,
    ) -> Any:
        from specivo.schemas.issue import IssueUpdate
        from specivo.services.issue_service import IssueService

        svc = IssueService()
        data = IssueUpdate(
            metadata=metadata,
            lock_version=entity.lock_version,
        )
        return await svc.update(session, entity, data, user, api_key_id=api_key_id)

    def project_id_of(self, entity: Any) -> int:
        return int(entity.project_id)

    def display_ref(self, entity: Any) -> str:
        return str(entity.display_key)
