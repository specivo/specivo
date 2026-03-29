"""API key service: creation, authentication, listing, deactivation, deletion.

Key design decisions:
- Raw keys are generated with a "spv_" prefix for brand recognition.
- Only SHA-256 hashes are stored; raw keys shown once at creation.
- key_prefix (first 12 chars of raw key) is stored for identification in UIs.
- last_used_at is debounced: updated at most once per 60 seconds per key.
- IP allowlist validation uses the ipaddress stdlib module.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import API_KEY_ENTROPY_BYTES, API_KEY_PREFIX
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.features import get_feature_registry
from specivo.core.i18n import gettext as _
from specivo.core.utils import utcnow
from specivo.models.auth import ApiKey
from specivo.models.user import User

# Debounce interval for last_used_at updates
_DEBOUNCE_SECONDS = 60


def _hash_key(raw: str) -> str:
    """Return the SHA-256 hex digest of a raw API key string."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _generate_raw_key() -> str:
    """Generate a cryptographically random API key with 'spv_' prefix."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_ENTROPY_BYTES)


def _check_ip_allowlist(ip_allowlist: list | None, client_ip: str | None) -> None:
    """Raise AppError if client_ip is not in the allowlist.

    When ip_allowlist is None or empty, any IP is allowed.
    When client_ip is None, we cannot verify — deny for safety only if list is set.
    """
    if not ip_allowlist:
        return
    if client_ip is None:
        raise AppError(
            code="api_key_ip_forbidden",
            message=_("Client IP not allowed by key allowlist"),
            status_code=403,
        )
    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        raise AppError(
            code="api_key_ip_forbidden",
            message=_("Client IP not allowed by key allowlist"),
            status_code=403,
        )
    for cidr in ip_allowlist:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if client_addr in network:
                return
        except ValueError:
            continue
    raise AppError(
        code="api_key_ip_forbidden",
        message="Client IP not allowed by key allowlist",
        status_code=403,
    )


class ApiKeyService:
    """Stateless service class — all state lives in the DB session."""

    _MAX_KEYS_FREE = 20

    async def _check_key_limit_exceeded(self, session: AsyncSession, user_id: int) -> bool:
        """Return True if user has reached the core-tier key limit (20 keys)."""
        from sqlalchemy import func

        count_result = await session.execute(select(func.count()).where(ApiKey.user_id == user_id))
        current_count = count_result.scalar_one()
        return isinstance(current_count, int) and current_count >= self._MAX_KEYS_FREE

    async def create_key(
        self,
        session: AsyncSession,
        user_id: int,
        name: str,
        scopes: dict | None = None,
        expires_at: datetime | None = None,
        ip_allowlist: list[str] | None = None,
    ) -> tuple[ApiKey, str]:
        """Create a new API key for *user_id*.

        Returns *(model, raw_key)*. The raw key is shown once and never
        retrievable again — callers must return it to the user immediately.

        Feature gates:
        - Without ``unlimited_api_keys``: max 20 keys per user.
        - Without ``api_key_scopes``: scopes are silently ignored.
        """
        registry = get_feature_registry()

        # Gate: enforce 5-key limit unless unlimited_api_keys feature is available
        if not registry.has_feature("unlimited_api_keys"):
            if await self._check_key_limit_exceeded(session, user_id):
                raise AppError(
                    code="api_key_limit",
                    message=_("API key limit reached ({limit}). Upgrade to Specivo Pro for unlimited keys.").format(
                        limit=self._MAX_KEYS_FREE
                    ),
                    status_code=422,
                )

        # Gate: ignore scopes unless api_key_scopes feature is available
        if not registry.has_feature("api_key_scopes"):
            scopes = None

        raw_key = _generate_raw_key()
        key_hash = _hash_key(raw_key)
        # Store first 12 characters for display identification
        key_prefix = raw_key[:12]

        record = ApiKey(
            user_id=user_id,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes,
            expires_at=expires_at,
            ip_allowlist=ip_allowlist,
            is_active=True,
        )
        session.add(record)
        await session.flush()
        return record, raw_key

    async def authenticate(
        self,
        session: AsyncSession,
        raw_key: str,
        client_ip: str | None = None,
    ) -> tuple[User, ApiKey]:
        """Authenticate by raw API key. Returns (User, ApiKey).

        Checks: is_active, expires_at, ip_allowlist, user status.
        Updates last_used_at with a 60-second debounce.
        Raises AppError(401) on any auth failure.
        """
        key_hash = _hash_key(raw_key)

        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash)
        result = await session.execute(stmt)
        key = result.scalar_one_or_none()

        if key is None:
            raise AppError(
                code="api_key_invalid",
                message=_("Invalid API key"),
                status_code=401,
            )

        if not key.is_active:
            raise AppError(
                code="api_key_inactive",
                message=_("API key is inactive"),
                status_code=401,
            )

        if key.expires_at is not None and key.expires_at < utcnow():
            raise AppError(
                code="api_key_expired",
                message=_("API key has expired"),
                status_code=401,
            )

        _check_ip_allowlist(key.ip_allowlist, client_ip)

        # Load the associated user
        user_result = await session.execute(select(User).where(User.id == key.user_id))
        user = user_result.scalar_one_or_none()

        # Per spec: locked accounts can still use API keys.
        # Locking is brute-force protection targeting password login.
        # Only deactivated accounts (admin action) block all auth methods.
        if user is None or user.status == "deactivated":
            raise AppError(
                code="api_key_invalid",
                message=_("Invalid API key"),
                status_code=401,
            )

        # Debounced last_used_at update: skip if updated within the last 60s
        now = utcnow()
        if key.last_used_at is None or (now - key.last_used_at).total_seconds() >= _DEBOUNCE_SECONDS:
            key.last_used_at = now
            await session.flush()

        return user, key

    async def list_keys(self, session: AsyncSession, user_id: int) -> list[ApiKey]:
        """Return all API keys for *user_id*, ordered newest first.

        Never returns raw key or hash — callers use ApiKeyOut schema.
        """
        stmt = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update_key(
        self,
        session: AsyncSession,
        user_id: int,
        key_id: int,
        is_active: bool,
    ) -> ApiKey:
        """Update is_active on an API key owned by *user_id*.

        Raises NotFoundError if the key does not exist or belongs to another user.
        """
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        result = await session.execute(stmt)
        key = result.scalar_one_or_none()
        if key is None:
            raise NotFoundError(message=_("API key not found"))
        key.is_active = is_active
        await session.flush()
        return key

    async def deactivate(self, session: AsyncSession, user_id: int, key_id: int) -> None:
        """Soft-deactivate an API key (sets is_active=False)."""
        await self.update_key(session, user_id, key_id, is_active=False)

    async def delete(self, session: AsyncSession, user_id: int, key_id: int) -> None:
        """Hard-delete an API key owned by *user_id*.

        Raises NotFoundError if the key does not exist or belongs to another user.
        """
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        result = await session.execute(stmt)
        key = result.scalar_one_or_none()
        if key is None:
            raise NotFoundError(message=_("API key not found"))
        stmt_del = delete(ApiKey).where(ApiKey.id == key_id)
        await session.execute(stmt_del)
        await session.flush()
