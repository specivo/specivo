"""SSO provider configuration: CRUD, the write-only client secret, group mappings, self-test.

Providers live in the database and are edited at runtime by administrators.
Rules enforced here (field-level rules are in :mod:`specivo.schemas.sso`):

* The client secret is write-only. It is stored encrypted with
  :mod:`specivo.core.secret_box` (purpose :data:`CLIENT_SECRET_PURPOSE`), or
  referenced by the name of an environment variable (``client_secret_env``),
  never both. A public client (``token_endpoint_auth_method = none``) has
  neither. A confidential client can be saved without a secret while it is
  disabled, but not enabled without one.
* ``http://`` issuer and discovery URLs need ``SSO_ALLOW_INSECURE_HTTP``.
* ``link_policy = auto_verified_email`` needs ``trust_email_verified``.
* Every change drops the provider's cached discovery document and JWKS; a
  change of issuer or discovery URL also drops the "last known good" copy on
  the row.
* Disabling a provider ends the SSO sessions that came from it (the count is
  in :class:`ProviderUpdateResult`); refresh refuses them anyway.
* A provider with linked identities is not deleted (409): disable it instead.
  ``force`` ends the SSO sessions that came from it and unlinks the
  identities. The user-group memberships it managed are removed on delete.

Every create / update / delete / secret change goes through :meth:`ProviderService._emit`,
the single place where these events are recorded: a ``specivo.sso`` log line
and an ``SSO_PROVIDER_*`` security audit event with the provider and the
*names* of the changed fields (never values), by
:func:`specivo.services.sso.events.provider_changed`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic import SecretStr
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import secret_box
from specivo.core.config import Settings, get_settings
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.sso import SsoGroupMapping, SsoProvider, UserIdentity, is_allowed_client_secret_env
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.schemas.sso import (
    SsoGroupMappingIn,
    SsoGroupMappingOut,
    SsoProviderCreate,
    SsoProviderOut,
    SsoProviderUpdate,
    SsoSelfTestResult,
)
from specivo.services.security_audit_service import AuditEvent
from specivo.services.sso import events
from specivo.services.sso.auth_settings import replace_auto_redirect_provider
from specivo.services.sso.discovery import (
    ProviderMetadata,
    default_discovery_url,
    fetch_discovery_document,
    invalidate_provider_cache,
    parse_discovery,
)
from specivo.services.sso.errors import SsoError
from specivo.services.sso.jwks import ALLOWED_ALGORITHMS, fetch_jwks_uncached
from specivo.services.sso.log import logger, sanitize_idp_text
from specivo.services.sso.tokens import CLIENT_SECRET_PURPOSE

if TYPE_CHECKING:
    from specivo.models.user import User

__all__ = [
    "CLIENT_SECRET_PURPOSE",
    "ProviderDeleteResult",
    "ProviderService",
    "ProviderUpdateResult",
    "SecretStatus",
    "backchannel_logout_uri",
    "redirect_uri",
]

_MAX_SECRET_LENGTH = 1024

# Columns copied 1:1 from the request schemas. The secret is handled apart.
_PLAIN_FIELDS: tuple[str, ...] = (
    "slug",
    "display_name",
    "enabled",
    "position",
    "issuer",
    "discovery_url",
    "client_id",
    "token_endpoint_auth_method",
    "scopes",
    "claim_config",
    "allow_jit_provisioning",
    "link_policy",
    "trust_email_verified",
    "allowed_email_domains",
    "required_groups",
    "admin_groups",
    "sync_admin_flag",
    "sync_groups",
    "deactivate_on_access_loss",
    "rp_logout_enabled",
    "backchannel_logout_enabled",
    "session_max_age_hours",
    "extra_auth_params",
)
# Fields whose change makes a stored discovery document belong to another endpoint.
_ENDPOINT_FIELDS = frozenset({"issuer", "discovery_url"})


class SecretStatus(StrEnum):
    """What the admin UI says about a provider's client secret."""

    CONFIGURED = "configured"  # stored encrypted and readable
    UNREADABLE = "unreadable"  # stored, but SECRET_KEY changed: re-enter it
    ENV = "env"  # referenced env var is set
    ENV_MISSING = "env_missing"  # referenced env var is not set in this process
    # Name outside SPECIVO_SSO_SECRET_*: never read, and whether it exists is not revealed.
    ENV_NOT_ALLOWED = "env_not_allowed"
    MISSING = "missing"  # confidential client without a secret
    NOT_REQUIRED = "not_required"  # public client (auth method ``none``)


@dataclass(frozen=True)
class ProviderUpdateResult:
    provider: SsoProvider
    #: Refresh rows deleted because this update disabled the provider.
    sessions_revoked: int = 0


@dataclass(frozen=True)
class ProviderDeleteResult:
    provider_id: int
    slug: str
    identities_unlinked: int
    sessions_revoked: int
    memberships_removed: int


def _app_url(settings: Settings | None) -> str:
    return (settings or get_settings()).app_url.rstrip("/")


def redirect_uri(slug: str, settings: Settings | None = None) -> str:
    """The OIDC redirect URI for a provider: ``{APP_URL}/auth/oidc/{slug}/callback/``."""
    return f"{_app_url(settings)}/auth/oidc/{slug}/callback/"


def backchannel_logout_uri(slug: str, settings: Settings | None = None) -> str:
    """The back-channel logout URI: ``{APP_URL}/auth/oidc/{slug}/backchannel-logout/``."""
    return f"{_app_url(settings)}/auth/oidc/{slug}/backchannel-logout/"


def _invalid(code: str, message: str, field: str | None = None) -> AppError:
    return AppError(code=code, message=message, status_code=422, field=field)


class ProviderService:
    """CRUD and checks for :class:`SsoProvider` rows. Stateless; see the module docstring."""

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def list_providers(
        self, session: AsyncSession, *, offset: int = 0, limit: int = 200
    ) -> tuple[list[SsoProvider], int]:
        total = (await session.execute(select(func.count()).select_from(SsoProvider))).scalar_one()
        rows = await session.execute(
            select(SsoProvider)
            .order_by(SsoProvider.position, SsoProvider.display_name, SsoProvider.id)
            .offset(offset)
            .limit(limit)
        )
        return list(rows.scalars().all()), total

    async def get(self, session: AsyncSession, provider_id: int) -> SsoProvider:
        provider = await session.get(SsoProvider, provider_id)
        if provider is None:
            raise NotFoundError(f"SSO provider {provider_id} not found")
        return provider

    async def identity_counts(self, session: AsyncSession, provider_ids: list[int]) -> dict[int, int]:
        if not provider_ids:
            return {}
        rows = await session.execute(
            select(UserIdentity.provider_id, func.count())
            .where(UserIdentity.provider_id.in_(provider_ids))
            .group_by(UserIdentity.provider_id)
        )
        return {pid: count for pid, count in rows.all()}

    def secret_status(self, provider: SsoProvider) -> SecretStatus:
        if provider.token_endpoint_auth_method == "none":
            return SecretStatus.NOT_REQUIRED
        if provider.client_secret_encrypted:
            try:
                secret_box.decrypt(provider.client_secret_encrypted, CLIENT_SECRET_PURPOSE)
            except secret_box.SecretUnreadableError:
                return SecretStatus.UNREADABLE
            return SecretStatus.CONFIGURED
        if provider.client_secret_env:
            if not is_allowed_client_secret_env(provider.client_secret_env):
                return SecretStatus.ENV_NOT_ALLOWED
            return SecretStatus.ENV if os.environ.get(provider.client_secret_env) else SecretStatus.ENV_MISSING
        return SecretStatus.MISSING

    def get_client_secret(self, provider: SsoProvider) -> str | None:
        """The plaintext client secret, or ``None`` (public client, unset env var, no secret).

        Raises :class:`~specivo.core.secret_box.SecretUnreadableError` when the
        stored secret cannot be decrypted (``SECRET_KEY`` rotated): the admin
        has to enter it again.
        """
        if provider.token_endpoint_auth_method == "none":
            return None
        if provider.client_secret_encrypted:
            return secret_box.decrypt(provider.client_secret_encrypted, CLIENT_SECRET_PURPOSE)
        if provider.client_secret_env:
            if not is_allowed_client_secret_env(provider.client_secret_env):
                return None
            return os.environ.get(provider.client_secret_env) or None
        return None

    async def managed_membership_counts(self, session: AsyncSession, provider_ids: list[int]) -> dict[int, int]:
        if not provider_ids:
            return {}
        rows = await session.execute(
            select(UserGroupMember.managed_by_provider_id, func.count())
            .where(UserGroupMember.managed_by_provider_id.in_(provider_ids))
            .group_by(UserGroupMember.managed_by_provider_id)
        )
        return {pid: count for pid, count in rows.all() if pid is not None}

    def to_out(
        self,
        provider: SsoProvider,
        *,
        identity_count: int = 0,
        managed_membership_count: int = 0,
        settings: Settings | None = None,
    ) -> SsoProviderOut:
        """The API view of a provider: no secret, plus the URLs to register at the IdP."""
        return SsoProviderOut(
            id=provider.id,
            slug=provider.slug,
            display_name=provider.display_name,
            enabled=provider.enabled,
            position=provider.position,
            issuer=provider.issuer,
            discovery_url=provider.discovery_url,
            client_id=provider.client_id,
            token_endpoint_auth_method=provider.token_endpoint_auth_method,
            client_secret_configured=bool(provider.client_secret_encrypted or provider.client_secret_env),
            client_secret_status=self.secret_status(provider).value,
            client_secret_env=provider.client_secret_env,
            scopes=provider.scopes,
            claim_config=dict(provider.claim_config or {}),
            allow_jit_provisioning=provider.allow_jit_provisioning,
            link_policy=provider.link_policy,
            trust_email_verified=provider.trust_email_verified,
            allowed_email_domains=list(provider.allowed_email_domains or []),
            required_groups=list(provider.required_groups or []),
            admin_groups=list(provider.admin_groups or []),
            sync_admin_flag=provider.sync_admin_flag,
            sync_groups=provider.sync_groups,
            deactivate_on_access_loss=provider.deactivate_on_access_loss,
            rp_logout_enabled=provider.rp_logout_enabled,
            backchannel_logout_enabled=provider.backchannel_logout_enabled,
            session_max_age_hours=provider.session_max_age_hours,
            extra_auth_params={k: str(v) for k, v in (provider.extra_auth_params or {}).items()},
            discovery_fetched_at=provider.discovery_fetched_at,
            identity_count=identity_count,
            managed_membership_count=managed_membership_count,
            redirect_uri=redirect_uri(provider.slug, settings),
            backchannel_logout_uri=backchannel_logout_uri(provider.slug, settings),
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    async def out(self, session: AsyncSession, provider: SsoProvider) -> SsoProviderOut:
        counts = await self.identity_counts(session, [provider.id])
        managed = await self.managed_membership_counts(session, [provider.id])
        return self.to_out(
            provider,
            identity_count=counts.get(provider.id, 0),
            managed_membership_count=managed.get(provider.id, 0),
        )

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    async def create(self, session: AsyncSession, data: SsoProviderCreate, *, actor: User | None) -> SsoProvider:
        settings = get_settings()
        values = {field: self._column_value(field, getattr(data, field)) for field in _PLAIN_FIELDS}
        secret = self._read_secret(data.client_secret)
        if secret is not None and data.client_secret_env is not None:
            raise self._secret_conflict()
        source = "stored" if secret is not None else ("env" if data.client_secret_env is not None else None)

        self._check_state(values, source, settings)
        await self._check_slug_free(session, values["slug"])

        provider = SsoProvider(**values)
        provider.client_secret_encrypted = (
            secret_box.encrypt(secret, CLIENT_SECRET_PURPOSE) if secret is not None else None
        )
        provider.client_secret_env = data.client_secret_env
        session.add(provider)
        await session.flush()

        self._emit(AuditEvent.SSO_PROVIDER_CREATED, provider, actor)
        if source is not None:
            self._emit(
                AuditEvent.SSO_PROVIDER_SECRET_CHANGED,
                provider,
                actor,
                ["client_secret" if source == "stored" else "client_secret_env"],
            )
        # A new id has nothing cached, unless an earlier row had the same id.
        await invalidate_provider_cache(provider.id)
        return provider

    async def update(
        self, session: AsyncSession, provider_id: int, data: SsoProviderUpdate, *, actor: User | None
    ) -> ProviderUpdateResult:
        """Apply the fields present in ``data``.

        Disabling the provider (``enabled`` true to false) also ends every SSO
        session that came from it, as a force delete does: the provider's
        policy can no longer be checked at the next sign-in, so nothing it
        let in keeps running. Password sessions and other providers' sessions
        stay. Enabling it again brings none of them back.
        """
        settings = get_settings()
        provider = await self.get(session, provider_id)
        fields_set = data.model_fields_set

        current = {field: getattr(provider, field) for field in _PLAIN_FIELDS}
        values = dict(current)
        for field in _PLAIN_FIELDS:
            if field not in fields_set:
                continue
            value = getattr(data, field)
            # Only discovery_url may be cleared with null; null elsewhere means "unchanged".
            if value is None and field != "discovery_url":
                continue
            values[field] = self._column_value(field, value)

        # --- secret source after this update ---
        secret = self._read_secret(data.client_secret) if "client_secret" in fields_set else None
        new_env = data.client_secret_env if "client_secret_env" in fields_set else None
        clear_env = "client_secret_env" in fields_set and data.client_secret_env is None
        if secret is not None and new_env is not None:
            raise self._secret_conflict()
        if data.clear_client_secret and (secret is not None or new_env is not None):
            raise self._secret_conflict()

        stored, env = provider.client_secret_encrypted, provider.client_secret_env
        secret_fields: list[str] = []
        if data.clear_client_secret:
            if stored or env:
                secret_fields = ["client_secret"] if stored else ["client_secret_env"]
            stored, env = None, None
        elif secret is not None:
            stored, env = secret_box.encrypt(secret, CLIENT_SECRET_PURPOSE), None
            secret_fields = ["client_secret"]
        elif new_env is not None:
            if new_env != env or stored:
                secret_fields = ["client_secret_env"]
            stored, env = None, new_env
        elif clear_env and env:
            env = None
            secret_fields = ["client_secret_env"]
        source = "stored" if stored else ("env" if env else None)

        self._check_state(values, source, settings)
        if values["slug"] != current["slug"]:
            await self._check_slug_free(session, values["slug"])

        changed = [field for field in _PLAIN_FIELDS if values[field] != current[field]]
        for field in changed:
            setattr(provider, field, values[field])
        provider.client_secret_encrypted = stored
        provider.client_secret_env = env
        if _ENDPOINT_FIELDS.intersection(changed):
            provider.discovery_cache = None
            provider.discovery_fetched_at = None
        await session.flush()
        sessions_revoked = 0
        if current["enabled"] and not values["enabled"]:
            sessions_revoked = await self._revoke_sessions(
                session,
                await self._identity_ids(session, provider.id),
                reason="sso_provider_disabled",
                actor_id=actor.id if actor is not None else None,
            )
        # updated_at is set by the database on UPDATE; load it now (no lazy IO later).
        await session.refresh(provider, attribute_names=["updated_at"])

        if "slug" in changed:
            await replace_auto_redirect_provider(session, current["slug"], values["slug"])
        if changed:
            counts = {"sessions_revoked": sessions_revoked} if "enabled" in changed and not provider.enabled else None
            self._emit(AuditEvent.SSO_PROVIDER_UPDATED, provider, actor, changed, counts=counts)
        if secret_fields:
            self._emit(AuditEvent.SSO_PROVIDER_SECRET_CHANGED, provider, actor, secret_fields)
        if changed or secret_fields:
            await invalidate_provider_cache(provider.id)
        return ProviderUpdateResult(provider=provider, sessions_revoked=sessions_revoked)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(
        self, session: AsyncSession, provider_id: int, *, force: bool = False, actor: User | None
    ) -> ProviderDeleteResult:
        """Delete a provider. With linked identities this is refused (409) unless ``force``.

        ``force`` ends the SSO sessions that came from the provider's
        identities (password sessions and other providers' sessions stay),
        then unlinks the identities. Either way, the user-group memberships
        the provider managed are removed: their authority came from the IdP.
        Group mappings go with the provider.
        """
        provider = await self.get(session, provider_id)
        identity_ids = await self._identity_ids(session, provider.id)
        if identity_ids and not force:
            raise AppError(
                code="sso_provider_has_identities",
                message=(
                    f"{len(identity_ids)} user(s) sign in through this provider. Disable it instead, "
                    "or delete with force to unlink them and end their sessions."
                ),
                status_code=409,
                details={"identity_count": len(identity_ids)},
            )

        # Sessions first: the identity FK on refresh_tokens is SET NULL, so
        # once the identities are gone their sessions can no longer be found.
        sessions_revoked = await self._revoke_sessions(
            session,
            identity_ids,
            reason="sso_provider_deleted",
            actor_id=actor.id if actor is not None else None,
        )
        if identity_ids:
            await session.execute(delete(UserIdentity).where(UserIdentity.provider_id == provider.id))
        removed = cast(
            CursorResult,
            await session.execute(delete(UserGroupMember).where(UserGroupMember.managed_by_provider_id == provider.id)),
        )
        memberships_removed = removed.rowcount or 0
        if memberships_removed:
            from specivo.services.permission_service import clear_role_cache

            clear_role_cache(session)

        slug = provider.slug
        await replace_auto_redirect_provider(session, slug, None)
        self._emit(
            AuditEvent.SSO_PROVIDER_DELETED,
            provider,
            actor,
            counts={
                "identities_unlinked": len(identity_ids),
                "sessions_revoked": sessions_revoked,
                "memberships_removed": memberships_removed,
            },
        )
        await session.execute(delete(SsoProvider).where(SsoProvider.id == provider.id))
        session.expunge(provider)
        await session.flush()
        await invalidate_provider_cache(provider_id)
        return ProviderDeleteResult(
            provider_id=provider_id,
            slug=slug,
            identities_unlinked=len(identity_ids),
            sessions_revoked=sessions_revoked,
            memberships_removed=memberships_removed,
        )

    @staticmethod
    async def _identity_ids(session: AsyncSession, provider_id: int) -> list[int]:
        result = await session.execute(select(UserIdentity.id).where(UserIdentity.provider_id == provider_id))
        return list(result.scalars())

    async def _revoke_sessions(
        self, session: AsyncSession, identity_ids: list[int], *, reason: str, actor_id: int | None
    ) -> int:
        """End the SSO sessions signed in through these identities; returns refresh rows deleted."""
        from specivo.services.session_service import SessionService

        sessions = SessionService()
        total = 0
        for identity_id in sorted(set(identity_ids)):
            total += await sessions.revoke_by_identity(session, identity_id, reason=reason, actor_id=actor_id)
        return total

    # ------------------------------------------------------------------
    # Group mappings
    # ------------------------------------------------------------------

    async def list_group_mappings(self, session: AsyncSession, provider_id: int) -> list[SsoGroupMappingOut]:
        await self.get(session, provider_id)
        rows = await session.execute(
            select(SsoGroupMapping, UserGroup.name)
            .join(UserGroup, UserGroup.id == SsoGroupMapping.user_group_id)
            .where(SsoGroupMapping.provider_id == provider_id)
            .order_by(SsoGroupMapping.idp_group.collate("C"), UserGroup.name, SsoGroupMapping.id)
        )
        return [
            SsoGroupMappingOut(
                id=mapping.id,
                idp_group=mapping.idp_group,
                user_group_id=mapping.user_group_id,
                user_group_name=name,
            )
            for mapping, name in rows.all()
        ]

    async def replace_group_mappings(
        self, session: AsyncSession, provider_id: int, items: list[SsoGroupMappingIn], *, actor: User | None
    ) -> list[SsoGroupMappingOut]:
        """Make the provider's mappings exactly ``items`` (duplicates collapse)."""
        provider = await self.get(session, provider_id)
        pairs = list(dict.fromkeys((item.idp_group, item.user_group_id) for item in items))
        await self._check_user_groups(session, {group_id for _, group_id in pairs})

        await session.execute(delete(SsoGroupMapping).where(SsoGroupMapping.provider_id == provider_id))
        session.add_all(
            SsoGroupMapping(provider_id=provider_id, idp_group=idp_group, user_group_id=group_id)
            for idp_group, group_id in pairs
        )
        await session.flush()
        self._emit(AuditEvent.SSO_PROVIDER_UPDATED, provider, actor, ["group_mappings"])
        return await self.list_group_mappings(session, provider_id)

    async def add_group_mapping(
        self, session: AsyncSession, provider_id: int, item: SsoGroupMappingIn, *, actor: User | None
    ) -> SsoGroupMappingOut:
        provider = await self.get(session, provider_id)
        await self._check_user_groups(session, {item.user_group_id})
        existing = await session.execute(
            select(SsoGroupMapping.id).where(
                SsoGroupMapping.provider_id == provider_id,
                SsoGroupMapping.idp_group == item.idp_group,
                SsoGroupMapping.user_group_id == item.user_group_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise AppError(
                code="sso_group_mapping_exists",
                message="This IdP group is already mapped to this user group",
                status_code=409,
            )
        mapping = SsoGroupMapping(provider_id=provider_id, idp_group=item.idp_group, user_group_id=item.user_group_id)
        session.add(mapping)
        await session.flush()
        self._emit(AuditEvent.SSO_PROVIDER_UPDATED, provider, actor, ["group_mappings"])
        name = (await session.execute(select(UserGroup.name).where(UserGroup.id == item.user_group_id))).scalar_one()
        return SsoGroupMappingOut(
            id=mapping.id, idp_group=mapping.idp_group, user_group_id=mapping.user_group_id, user_group_name=name
        )

    async def delete_group_mapping(
        self, session: AsyncSession, provider_id: int, mapping_id: int, *, actor: User | None
    ) -> None:
        provider = await self.get(session, provider_id)
        result = await session.execute(
            select(SsoGroupMapping).where(SsoGroupMapping.id == mapping_id, SsoGroupMapping.provider_id == provider_id)
        )
        mapping = result.scalar_one_or_none()
        if mapping is None:
            raise NotFoundError(f"Group mapping {mapping_id} not found")
        await session.delete(mapping)
        await session.flush()
        self._emit(AuditEvent.SSO_PROVIDER_UPDATED, provider, actor, ["group_mappings"])

    async def _check_user_groups(self, session: AsyncSession, group_ids: set[int]) -> None:
        if not group_ids:
            return
        found = set((await session.execute(select(UserGroup.id).where(UserGroup.id.in_(group_ids)))).scalars())
        missing = sorted(group_ids - found)
        if missing:
            raise AppError(
                code="sso_user_group_not_found",
                message=f"User group(s) not found: {', '.join(str(m) for m in missing)}",
                status_code=422,
                field="user_group_id",
                details={"missing": missing},
            )

    # ------------------------------------------------------------------
    # Discovery self-test
    # ------------------------------------------------------------------

    async def self_test(self, provider: SsoProvider, *, settings: Settings | None = None) -> SsoSelfTestResult:
        """Fetch discovery and the JWKS now and report what was found. Nothing is stored.

        Unlike :func:`~specivo.services.sso.discovery.get_provider_metadata`
        there is no cache and no "last known good" fallback: the admin needs
        to see what the IdP serves right now.
        """
        settings = settings or get_settings()
        url = provider.discovery_url or default_discovery_url(provider.issuer)
        found: dict[str, Any] = {"discovery_url": url, "expected_issuer": provider.issuer}

        try:
            document = await fetch_discovery_document(url, settings=settings)
        except SsoError as exc:
            return self._failed("discovery", exc, found, provider)

        reported = document.get("issuer")
        found["reported_issuer"] = sanitize_idp_text(reported, 512) if reported is not None else None
        found["issuer_match"] = reported == provider.issuer
        try:
            metadata = parse_discovery(
                document, expected_issuer=provider.issuer, allow_insecure_http=settings.sso_allow_insecure_http
            )
        except SsoError as exc:
            return self._failed("discovery", exc, found, provider)

        found.update(self._describe_metadata(metadata))
        warnings = self._warnings(provider, metadata)
        try:
            keys = await fetch_jwks_uncached(metadata.jwks_uri, settings=settings)
        except SsoError as exc:
            return self._failed("jwks", exc, found, provider, warnings)

        found["jwks_key_count"] = len(keys)
        found["jwks_key_ids"] = sorted(sanitize_idp_text(k["kid"], 128) for k in keys if k.get("kid"))
        logger.info("sso.provider.self_test provider=%s ok=true", provider.slug)
        return SsoSelfTestResult(ok=True, warnings=warnings, **found)

    def _failed(
        self,
        step: str,
        exc: SsoError,
        found: dict[str, Any],
        provider: SsoProvider,
        warnings: list[str] | None = None,
    ) -> SsoSelfTestResult:
        logger.info("sso.provider.self_test provider=%s ok=false step=%s reason=%s", provider.slug, step, exc.reason)
        return SsoSelfTestResult(
            ok=False,
            failed_step=step,  # type: ignore[arg-type]
            error_code=exc.code,
            error_reason=exc.reason,
            warnings=warnings or [],
            **found,
        )

    @staticmethod
    def _describe_metadata(metadata: ProviderMetadata) -> dict[str, Any]:
        algorithms = list(metadata.id_token_signing_alg_values_supported)
        return {
            "authorization_endpoint": metadata.authorization_endpoint,
            "token_endpoint": metadata.token_endpoint,
            "userinfo_endpoint": metadata.userinfo_endpoint,
            "end_session_endpoint": metadata.end_session_endpoint,
            "jwks_uri": metadata.jwks_uri,
            "signing_algorithms": algorithms,
            "usable_signing_algorithms": [alg for alg in algorithms if alg in ALLOWED_ALGORITHMS],
            "token_endpoint_auth_methods": list(metadata.token_endpoint_auth_methods_supported),
            "pkce_s256": ("S256" in metadata.code_challenge_methods_supported)
            if metadata.code_challenge_methods_supported
            else None,
            "iss_parameter_supported": metadata.authorization_response_iss_parameter_supported,
            "backchannel_logout_supported": metadata.backchannel_logout_supported,
            "backchannel_logout_session_supported": metadata.backchannel_logout_session_supported,
        }

    def _warnings(self, provider: SsoProvider, metadata: ProviderMetadata) -> list[str]:
        warnings: list[str] = []
        # Discovery 1.0: an omitted list means client_secret_basic only.
        methods = metadata.token_endpoint_auth_methods_supported or ("client_secret_basic",)
        if provider.token_endpoint_auth_method not in methods:
            warnings.append("auth_method_not_advertised")
        if not metadata.code_challenge_methods_supported:
            warnings.append("pkce_not_advertised")
        if provider.rp_logout_enabled and not metadata.end_session_endpoint:
            warnings.append("end_session_missing")
        if provider.backchannel_logout_enabled and not metadata.backchannel_logout_supported:
            warnings.append("backchannel_logout_unsupported")
        if (provider.claim_config or {}).get("use_userinfo") and not metadata.userinfo_endpoint:
            warnings.append("userinfo_missing")
        status = self.secret_status(provider)
        if status is SecretStatus.UNREADABLE:
            warnings.append("client_secret_unreadable")
        elif status is SecretStatus.ENV_MISSING:
            warnings.append("client_secret_env_unset")
        elif status is SecretStatus.ENV_NOT_ALLOWED:
            warnings.append("client_secret_env_not_allowed")
        elif status is SecretStatus.MISSING:
            warnings.append("client_secret_missing")
        return warnings

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _column_value(field: str, value: Any) -> Any:
        if field == "claim_config":
            return value.model_dump(exclude_none=True, exclude_defaults=True) if value is not None else {}
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return dict(value)
        return value

    @staticmethod
    def _read_secret(value: SecretStr | None) -> str | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        if not secret.strip() or len(secret) > _MAX_SECRET_LENGTH:
            raise _invalid(
                "sso_client_secret_invalid",
                f"The client secret must be 1-{_MAX_SECRET_LENGTH} characters",
                "client_secret",
            )
        return secret

    @staticmethod
    def _secret_conflict() -> AppError:
        return _invalid(
            "sso_client_secret_conflict",
            "Use either a client secret or an environment variable name, not both (or clear it)",
            "client_secret",
        )

    @staticmethod
    def _check_state(values: dict[str, Any], secret_source: str | None, settings: Settings) -> None:
        """Cross-field rules on the provider as it will be saved."""
        for field in ("issuer", "discovery_url"):
            url = values.get(field)
            if url and httpx.URL(url).scheme == "http" and not settings.sso_allow_insecure_http:
                raise _invalid(
                    "sso_insecure_url",
                    "Identity provider URLs must use https (SSO_ALLOW_INSECURE_HTTP is off)",
                    field,
                )
        public = values["token_endpoint_auth_method"] == "none"
        if public and secret_source is not None:
            raise _invalid(
                "sso_client_secret_not_allowed",
                "A public client (token endpoint auth method 'none') has no client secret; clear it",
                "client_secret",
            )
        if not public and secret_source is None and values["enabled"]:
            raise _invalid(
                "sso_client_secret_required",
                "A confidential client needs a client secret before it can be enabled",
                "client_secret",
            )
        if values["link_policy"] == "auto_verified_email" and not values["trust_email_verified"]:
            raise _invalid(
                "sso_trust_email_verified_required",
                "Automatic linking by email requires trusting the provider's email_verified claim",
                "link_policy",
            )

    @staticmethod
    async def _check_slug_free(session: AsyncSession, slug: str) -> None:
        taken = await session.execute(select(SsoProvider.id).where(SsoProvider.slug == slug))
        if taken.scalar_one_or_none() is not None:
            raise AppError(
                code="sso_provider_slug_taken",
                message=f"An SSO provider with the slug '{slug}' already exists",
                status_code=409,
                field="slug",
            )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @staticmethod
    def _emit(
        event: AuditEvent,
        provider: SsoProvider,
        actor: User | None,
        fields: list[str] | None = None,
        *,
        counts: dict[str, int] | None = None,
    ) -> None:
        """Record a configuration event: the log line and the ``SSO_PROVIDER_*`` audit event.

        ``fields`` are field *names* only. Values, and the secret above all,
        never reach this function.
        """
        events.provider_changed(
            event,
            provider_id=provider.id,
            provider_slug=provider.slug,
            issuer=provider.issuer,
            actor_id=actor.id if actor is not None else None,
            fields=fields or [],
            counts=counts,
        )
