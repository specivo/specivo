"""Auth-related models: refresh tokens, API keys."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin

AUTH_METHODS = ("password", "sso")


class RefreshToken(Base, TimestampMixin):
    """Stored refresh token for session management.

    Token values are never stored raw. Only a SHA-256 hash is persisted.
    The raw token is issued once in the HTTP response and never retrievable.

    The refresh token is the session. Rotation replaces the row but copies
    ``session_id`` (and every other session column) onto the new one, so a
    login keeps one id for its whole life. Access tokens carry it as the
    ``sid`` claim; ``SessionService.revoke`` uses it to cut them off.

    Cleanup: expired tokens should be pruned by a background task (Celery beat).
    """

    __tablename__ = "refresh_tokens"

    __table_args__ = (
        # Index for fast per-user session listing and bulk revocation
        Index("ix_refresh_tokens_user_id", "user_id"),
        # Index for background cleanup of expired tokens
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        # Revocation by session id
        Index("ix_refresh_tokens_session_id", "session_id"),
        # Back-channel logout and unlink; also covers the user_identity_id FK
        Index(
            "ix_refresh_tokens_user_identity_idp_sid",
            "user_identity_id",
            "idp_sid",
            postgresql_where=text("user_identity_id IS NOT NULL"),
        ),
        CheckConstraint(f"auth_method IN {AUTH_METHODS!r}", name="ck_refresh_tokens_auth_method"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 hex digest of the raw token (64 hex chars)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # User-Agent string or a human label set by the client
    device_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # IP address at token creation time (stored as string for portability)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Stable across rotation; the ``sid`` claim of this session's access tokens.
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # How the session signed in: "password" or "sso".
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False, default="password", server_default="password")
    # The external identity an SSO session signed in with.
    user_identity_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_identities.id", ondelete="SET NULL"), nullable=True
    )
    # The IdP's own session id ("sid" claim), for back-channel logout.
    idp_sid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The ID token, sealed, sent back to the IdP as ``id_token_hint`` on logout.
    id_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The IdP's refresh token, sealed for the issuer, token endpoint and client
    # that issued it (``tokens.idp_refresh_token_purpose``), never sent elsewhere. The
    # session is renewed only after the IdP accepted it (when the provider has
    # ``verify_session_at_idp`` on); replaced when the IdP rotates it. NULL: the
    # IdP issued none, or refused it at sign-in, and the session is not checked.
    idp_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the IdP last accepted that token: the check runs at most once per
    # ``SSO_IDP_RECHECK_MINUTES``.
    idp_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hard cap: the session cannot be refreshed past this point, whatever
    # ``expires_at`` says. NULL means no cap (password sessions).
    absolute_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<RefreshToken id={self.id} user_id={self.user_id}>"


class PasswordResetToken(Base, TimestampMixin):
    """One-time token for self-service password reset.

    Token values are never stored raw. Only a SHA-256 hash is persisted.
    The raw token is sent via email and never retrievable from the DB.

    Tokens are single-use: ``used_at`` is set when the password is changed.
    """

    __tablename__ = "password_reset_tokens"

    __table_args__ = (Index("ix_password_reset_tokens_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<PasswordResetToken id={self.id} user_id={self.user_id}>"


class ApiKey(Base, TimestampMixin):
    """Machine-generated API key for service accounts and CI agents.

    Raw keys are never stored. Only the SHA-256 hash is persisted.
    The raw key is issued once at creation and never retrievable.

    key_prefix stores the first 12 characters of the raw key for UI
    identification without exposing enough to reconstruct the key.

    Scopes example: {"projects": ["ACME"], "permissions": ["issues:read"]}
    ip_allowlist: list of CIDR strings, null means any IP is allowed.
    """

    __tablename__ = "api_keys"

    __table_args__ = (Index("ix_api_keys_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Human label, e.g. "build-agent" or "ci-deploy"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # First 12 characters of the raw key for identification (e.g. "spv_acme_a1b")
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # SHA-256 hex digest of the raw key (64 hex chars) — never store raw
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Optional scope restrictions: {"projects": [...], "permissions": [...]}
    scopes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Null means no expiry
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Debounced: updated at most once per minute on use
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft disable without deleting
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # List of CIDR strings; null = any IP allowed
    ip_allowlist: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} user_id={self.user_id} name={self.name!r}>"
