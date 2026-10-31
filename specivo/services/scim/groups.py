"""SCIM ``/Groups``: Specivo user groups managed by one SSO provider's SCIM client.

A SCIM group is a :class:`~specivo.models.user_group.UserGroup` whose
memberships carry the provider in ``managed_by_provider_id``, the provenance
SSO group sync already uses. Members can only be users provisioned by the
same provider (any other id is 400 ``invalidValue``).

* A ``displayName`` already used by any Specivo user group (case-insensitive)
  is 409 ``uniqueness``: an existing group, and the project roles it holds,
  is never taken over by an identity provider.
* PUT replaces the name and ``externalId``; it replaces the members only when
  the body has ``members`` (some clients rename with a PUT that leaves them
  out). PATCH adds and removes members (the way Entra ID and Okta send member
  changes).
* Only memberships this provider manages are added or removed. A membership
  an administrator added by hand stays, and no second row is added next to it.
* DELETE deletes the user group: its memberships go with it, and its project
  memberships follow the usual group deletion
  (:meth:`specivo.services.user_group_service.UserGroupService.delete`).

The role cache is cleared after every membership change, so project roles
held through the group apply at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.scim import ScimResource
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.permission_service import clear_role_cache
from specivo.services.scim import events
from specivo.services.scim.config_service import scim_base_url
from specivo.services.scim.errors import ScimError
from specivo.services.scim.filters import Comparison
from specivo.services.scim.mapping import GroupAttributes, group_attributes
from specivo.services.scim.patch import apply_patch
from specivo.services.scim.resources import GROUP_SCHEMA, meta
from specivo.services.security_audit_service import AuditEvent
from specivo.services.user_group_service import UserGroupService

__all__ = ["ScimGroupService"]


class ScimGroupService:
    """Stateless; every method takes the provider whose token authenticated the request."""

    # -----------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------

    @staticmethod
    def _scope(provider: SsoProvider) -> list[ColumnElement[bool]]:
        return [ScimResource.provider_id == provider.id, ScimResource.resource_type == "Group"]

    @staticmethod
    def _condition(comparison: Comparison) -> ColumnElement[bool]:
        if comparison.attribute == "displayName":
            return func.lower(UserGroup.name) == comparison.value.lower()
        if comparison.attribute == "externalId":
            return ScimResource.external_id == comparison.value
        if comparison.attribute == "id":
            return ScimResource.scim_id == comparison.value
        raise ScimError.invalid_filter("Filtering on this attribute is not supported.")  # pragma: no cover

    async def search(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        *,
        comparisons: Sequence[Comparison],
        start_index: int,
        count: int,
    ) -> tuple[int, list[tuple[ScimResource, UserGroup]]]:
        conditions = [*self._scope(provider), *(self._condition(c) for c in comparisons)]
        joined = select(ScimResource, UserGroup).join(UserGroup, UserGroup.id == ScimResource.user_group_id)
        total = int(
            await db.scalar(
                select(func.count())
                .select_from(ScimResource)
                .join(UserGroup, UserGroup.id == ScimResource.user_group_id)
                .where(*conditions)
            )
            or 0
        )
        if count == 0:
            return total, []
        rows = await db.execute(
            joined.where(*conditions).order_by(ScimResource.id).offset(start_index - 1).limit(count)
        )
        return total, [(res, group) for res, group in rows.all()]

    async def get(self, db: AsyncSession, provider: SsoProvider, scim_id: str) -> tuple[ScimResource, UserGroup]:
        row = (
            await db.execute(
                select(ScimResource, UserGroup)
                .join(UserGroup, UserGroup.id == ScimResource.user_group_id)
                .where(*self._scope(provider), ScimResource.scim_id == scim_id)
            )
        ).first()
        if row is None:
            raise ScimError.not_found("Group not found")
        return row[0], row[1]

    async def members(
        self, db: AsyncSession, provider: SsoProvider, group_ids: Sequence[int]
    ) -> dict[int, list[tuple[str, str]]]:
        """For each group, its members provisioned by this provider: ``(scim id, display name)``."""
        if not group_ids:
            return {}
        rows = await db.execute(
            select(UserGroupMember.group_id, ScimResource.scim_id, User.display_name)
            .join(ScimResource, ScimResource.user_id == UserGroupMember.user_id)
            .join(User, User.id == UserGroupMember.user_id)
            .where(
                UserGroupMember.group_id.in_(group_ids),
                ScimResource.provider_id == provider.id,
                ScimResource.resource_type == "User",
                ScimResource.deleted_at.is_(None),
            )
            .order_by(UserGroupMember.group_id, ScimResource.id)
        )
        result: dict[int, list[tuple[str, str]]] = {gid: [] for gid in group_ids}
        for group_id, scim_id, display_name in rows.all():
            result[group_id].append((scim_id, display_name))
        return result

    @staticmethod
    def to_resource(
        provider: SsoProvider,
        res: ScimResource,
        group: UserGroup,
        members: list[tuple[str, str]] | None,
    ) -> dict[str, Any]:
        """The SCIM Group; ``members`` ``None`` leaves them out (``excludedAttributes=members``)."""
        base = scim_base_url(provider.slug)
        body: dict[str, Any] = {"schemas": [GROUP_SCHEMA], "id": res.scim_id, "displayName": group.name}
        if res.external_id is not None:
            body["externalId"] = res.external_id
        if members is not None:
            body["members"] = [
                {"value": scim_id, "display": name, "type": "User", "$ref": f"{base}/Users/{scim_id}"}
                for scim_id, name in members
            ]
        modified = max((t for t in (res.updated_at, group.updated_at) if t is not None), default=None)
        body["meta"] = meta("Group", res.created_at, modified, f"{base}/Groups/{res.scim_id}")
        return body

    # -----------------------------------------------------------------------
    # Writing
    # -----------------------------------------------------------------------

    @staticmethod
    def _attributes(body: Any) -> GroupAttributes:
        if not isinstance(body, dict):
            raise ScimError.invalid_syntax("The request body must be a JSON object.")
        return group_attributes(body)

    @staticmethod
    async def _require_name_free(db: AsyncSession, name: str, *, except_group_id: int | None = None) -> None:
        stmt = select(UserGroup.id).where(func.lower(UserGroup.name) == name.lower())
        if except_group_id is not None:
            stmt = stmt.where(UserGroup.id != except_group_id)
        if (await db.execute(stmt.limit(1))).first() is not None:
            raise ScimError.uniqueness("A Specivo group with this name already exists.")

    @staticmethod
    async def _member_user_ids(db: AsyncSession, provider: SsoProvider, scim_ids: Sequence[str]) -> set[int]:
        """The user ids behind ``scim_ids``; 400 when one is not a live user of this provider."""
        if not scim_ids:
            return set()
        rows = await db.execute(
            select(ScimResource.scim_id, ScimResource.user_id).where(
                ScimResource.provider_id == provider.id,
                ScimResource.resource_type == "User",
                ScimResource.deleted_at.is_(None),
                ScimResource.scim_id.in_(list(scim_ids)),
            )
        )
        found = {scim_id: user_id for scim_id, user_id in rows.all()}
        missing = [s for s in scim_ids if s not in found]
        if missing:
            raise ScimError.invalid_value(
                "Members must be users provisioned by this identity provider; unknown member id: " + missing[0][:64]
            )
        return {uid for uid in found.values() if uid is not None}

    async def _set_members(
        self, db: AsyncSession, provider: SsoProvider, group: UserGroup, wanted: set[int]
    ) -> tuple[list[int], list[int]]:
        """Make this provider's members of ``group`` exactly ``wanted``; returns (added, removed) user ids."""
        rows = list(
            (await db.execute(select(UserGroupMember).where(UserGroupMember.group_id == group.id))).scalars().all()
        )
        present = {row.user_id for row in rows}
        added: list[int] = []
        for user_id in sorted(wanted - present):
            inserted = await db.execute(
                insert(UserGroupMember)
                .values(group_id=group.id, user_id=user_id, managed_by_provider_id=provider.id)
                .on_conflict_do_nothing(constraint="uq_user_group_members_group_user")
                .returning(UserGroupMember.id)
            )
            if inserted.first() is not None:
                added.append(user_id)
        removed: list[int] = []
        for row in rows:
            if row.managed_by_provider_id == provider.id and row.user_id not in wanted:
                await db.delete(row)
                removed.append(row.user_id)
        if added or removed:
            await db.flush()
            clear_role_cache(db)
        return added, sorted(removed)

    async def create(self, db: AsyncSession, provider: SsoProvider, body: Any) -> tuple[ScimResource, UserGroup]:
        attrs = self._attributes(body)
        await self._require_name_free(db, attrs.display_name)
        wanted = await self._member_user_ids(db, provider, attrs.member_ids)
        savepoint = await db.begin_nested()
        try:
            group = UserGroup(name=attrs.display_name)
            db.add(group)
            await db.flush()
            res = ScimResource(
                provider_id=provider.id,
                resource_type="Group",
                scim_id=str(uuid.uuid4()),
                user_group_id=group.id,
                external_id=attrs.external_id,
            )
            db.add(res)
            await db.flush()
        except IntegrityError:
            await savepoint.rollback()
            raise ScimError.uniqueness("A Specivo group with this name already exists.") from None
        if savepoint.is_active:
            await savepoint.commit()
        added, _removed = await self._set_members(db, provider, group, wanted)
        await db.refresh(res)
        await db.refresh(group)
        events.group_changed(
            AuditEvent.SCIM_GROUP_CREATED,
            provider_slug=provider.slug,
            scim_id=res.scim_id,
            group_id=group.id,
            action="created",
            members_added=added,
        )
        return res, group

    async def _update(
        self,
        db: AsyncSession,
        provider: SsoProvider,
        res: ScimResource,
        group: UserGroup,
        attrs: GroupAttributes,
        *,
        replace_members: bool,
    ) -> tuple[ScimResource, UserGroup]:
        fields: list[str] = []
        if attrs.display_name != group.name:
            if attrs.display_name.lower() != group.name.lower():
                await self._require_name_free(db, attrs.display_name, except_group_id=group.id)
            group.name = attrs.display_name
            fields.append("displayName")
        if attrs.external_id != res.external_id:
            res.external_id = attrs.external_id
            fields.append("externalId")
        added: list[int] = []
        removed: list[int] = []
        if replace_members:
            wanted = await self._member_user_ids(db, provider, attrs.member_ids)
            added, removed = await self._set_members(db, provider, group, wanted)
        if not fields and not added and not removed:
            return res, group
        try:
            await db.flush()
        except IntegrityError:
            raise ScimError.uniqueness("A Specivo group with this name already exists.") from None
        await db.refresh(res)
        await db.refresh(group)
        events.group_changed(
            AuditEvent.SCIM_GROUP_UPDATED,
            provider_slug=provider.slug,
            scim_id=res.scim_id,
            group_id=group.id,
            action="updated",
            fields=fields,
            members_added=added,
            members_removed=removed,
        )
        return res, group

    async def replace(
        self, db: AsyncSession, provider: SsoProvider, scim_id: str, body: Any
    ) -> tuple[ScimResource, UserGroup]:
        res, group = await self.get(db, provider, scim_id)
        attrs = self._attributes(body)
        return await self._update(db, provider, res, group, attrs, replace_members=attrs.members_present)

    async def patch(
        self, db: AsyncSession, provider: SsoProvider, scim_id: str, request: Any
    ) -> tuple[ScimResource, UserGroup]:
        res, group = await self.get(db, provider, scim_id)
        members = (await self.members(db, provider, [group.id]))[group.id]
        current = self.to_resource(provider, res, group, members)
        patched = apply_patch(current, request)
        patched.setdefault("members", [])
        attrs = self._attributes(patched)
        unchanged = attrs.member_ids == [scim for scim, _name in members]
        return await self._update(db, provider, res, group, attrs, replace_members=not unchanged)

    async def delete(self, db: AsyncSession, provider: SsoProvider, scim_id: str) -> None:
        res, group = await self.get(db, provider, scim_id)
        scim, group_id = res.scim_id, group.id
        deletion = await UserGroupService().delete(db, group_id)
        clear_role_cache(db)
        events.group_changed(
            AuditEvent.SCIM_GROUP_DELETED,
            provider_slug=provider.slug,
            scim_id=scim,
            group_id=group_id,
            action="deleted",
            users_removed=deletion.users_removed,
            project_memberships_removed=deletion.project_memberships_removed,
        )
