"""User model for authentication and account management."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """Platform user account.

    Covers human users, service accounts (agents), and OAuth-linked accounts.
    Password hashing is handled by app/services/auth_utils.py (bcrypt).

    Indexes:
    - uq_users_login_ci: case-insensitive unique on LOWER(login)
    - uq_users_email_ci: case-insensitive unique on LOWER(email)
    - ix_users_status: for admin user listing
    - ix_users_github_id: partial, for OAuth lookup (WHERE github_id IS NOT NULL)
    - ix_users_google_id: partial, for OAuth lookup (WHERE google_id IS NOT NULL)
    """

    __tablename__ = "users"

    __table_args__ = (
        # Case-insensitive unique indexes — plain unique=True on the column is
        # NOT used. These expression-based indexes enforce uniqueness and prevent
        # concurrent inserts of "Alice" and "alice".
        # text() columns are used so autogenerate emits the correct DDL:
        #   CREATE UNIQUE INDEX uq_users_login_ci ON users (LOWER(login))
        Index("uq_users_login_ci", func.lower(text("login")), unique=True),
        Index("uq_users_email_ci", func.lower(text("email")), unique=True),
        # Index for admin user listing filtered by status
        Index("ix_users_status", "status"),
        # Partial indexes for OAuth foreign ID lookups
        Index(
            "ix_users_github_id",
            "github_id",
            postgresql_where="github_id IS NOT NULL",
        ),
        Index(
            "ix_users_google_id",
            "google_id",
            postgresql_where="google_id IS NOT NULL",
        ),
        # Status must be one of the defined account states
        CheckConstraint(
            "status IN ('active', 'locked', 'pending_verification', 'deactivated')",
            name="ck_users_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Identity ---
    login: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Display ---
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # avatar_url: populated from Gravatar or uploaded file
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Localisation ---
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en", server_default="en")
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC", server_default="UTC")

    # --- Account state ---
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default="active")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_service_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # --- Brute-force protection ---
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Verification / lifecycle timestamps ---
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- OAuth preparation ---
    github_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    google_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- User preferences (global defaults, per-project prefs on members table) ---
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<User id={self.id} login={self.login!r} status={self.status!r}>"
