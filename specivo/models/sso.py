"""OpenID Connect single sign-on: providers, external identities, group mappings.

An ``SsoProvider`` is one configured OIDC relying-party registration. A
``UserIdentity`` links a Specivo user to one subject at one issuer; an
``SsoGroupMapping`` turns an IdP group value into membership of a Specivo
``UserGroup``. No IdP tokens and no raw claim dumps are stored here.

The database also runs the ``reject_sso_identity_for_inert_user`` trigger on
``user_identities`` (created by migration, no ORM counterpart): service
accounts and the anonymous user can never hold an external identity.
"""

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from specivo.models.user import User
    from specivo.models.user_group import UserGroup

TOKEN_ENDPOINT_AUTH_METHODS = ("client_secret_basic", "client_secret_post", "none")
LINK_POLICIES = ("manual", "auto_verified_email")
LINKED_VIA_VALUES = ("jit", "user_link", "auto_email", "admin")
SLUG_PATTERN = r"^[a-z0-9-]{2,40}$"

#: ``client_secret_env`` may only name variables in this namespace. Anything
#: else (``SECRET_KEY``, ``DATABASE_URL`` ...) would let whoever controls the
#: provider row send Specivo's own secrets to a token endpoint of their choice.
CLIENT_SECRET_ENV_PREFIX = "SPECIVO_SSO_SECRET_"
CLIENT_SECRET_ENV_PATTERN = r"^SPECIVO_SSO_SECRET_[A-Z0-9][A-Z0-9_]{0,44}$"
_CLIENT_SECRET_ENV_RE = re.compile(CLIENT_SECRET_ENV_PATTERN)


def is_allowed_client_secret_env(name: str | None) -> bool:
    """True when ``name`` is a variable an SSO provider may read its client secret from."""
    return bool(name) and _CLIENT_SECRET_ENV_RE.fullmatch(name or "") is not None


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SsoProvider(Base, TimestampMixin):
    """An OIDC identity provider registration.

    ``client_secret_encrypted`` is a ``specivo.core.secret_box`` token
    (purpose ``sso-client-secret``); ``client_secret_env`` names an
    environment variable holding the secret instead. At most one is set, and
    neither for a public client.

    Indexes:
    - uq_sso_providers_slug: the slug is used in URLs
    """

    __tablename__ = "sso_providers"

    __table_args__ = (
        UniqueConstraint("slug", name="uq_sso_providers_slug"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="ck_sso_providers_slug"),
        CheckConstraint(
            "client_secret_encrypted IS NULL OR client_secret_env IS NULL",
            name="ck_sso_providers_single_secret_source",
        ),
        CheckConstraint(
            _in("token_endpoint_auth_method", TOKEN_ENDPOINT_AUTH_METHODS),
            name="ck_sso_providers_token_endpoint_auth_method",
        ),
        CheckConstraint(_in("link_policy", LINK_POLICIES), name="ck_sso_providers_link_policy"),
        CheckConstraint("session_max_age_hours > 0", name="ck_sso_providers_session_max_age_hours"),
        CheckConstraint(
            "jsonb_typeof(allowed_email_domains) = 'array'", name="ck_sso_providers_allowed_email_domains_array"
        ),
        CheckConstraint("jsonb_typeof(required_groups) = 'array'", name="ck_sso_providers_required_groups_array"),
        CheckConstraint("jsonb_typeof(admin_groups) = 'array'", name="ck_sso_providers_admin_groups_array"),
        CheckConstraint("jsonb_typeof(claim_config) = 'object'", name="ck_sso_providers_claim_config_object"),
        CheckConstraint("jsonb_typeof(extra_auth_params) = 'object'", name="ck_sso_providers_extra_auth_params_object"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Presentation ---
    slug: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Created disabled: an admin enables a provider once its configuration works.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Login buttons are sorted by position.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # --- OIDC client registration ---
    # Must exactly match the discovery ``issuer`` and the ID token ``iss``.
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    # NULL means ``issuer + /.well-known/openid-configuration``.
    discovery_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="client_secret_basic", server_default="client_secret_basic"
    )
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_env: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Space-separated; ``openid`` is enforced by the service layer.
    scopes: Mapped[str] = mapped_column(
        String(512), nullable=False, default="openid profile email", server_default="openid profile email"
    )
    # username_claim, email_claim, name_claim, groups_claim (dotted path),
    # locale_claim, use_userinfo.
    claim_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # --- Account policy ---
    allow_jit_provisioning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    link_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    # The admin asserts the IdP really verifies emails; required for auto_verified_email.
    trust_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    allowed_email_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Empty: any IdP user may sign in.
    required_groups: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # IdP group values that grant is_admin.
    admin_groups: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    sync_admin_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sync_groups: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # A sign-in refused by the access policy also deactivates the linked account.
    deactivate_on_access_loss: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- Sessions and logout ---
    rp_logout_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    backchannel_logout_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Absolute session cap.
    session_max_age_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24, server_default="24")
    # Allowlisted keys only (prompt, domain_hint, login_hint, acr_values, max_age).
    extra_auth_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # --- Discovery cache (last good document) ---
    discovery_cache: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    discovery_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # RESTRICT in the database: a provider with linked identities is disabled,
    # not deleted. passive_deletes="all" keeps the ORM from nulling the FK of
    # loaded identities so the database refuses the delete.
    identities: Mapped[list["UserIdentity"]] = relationship(
        "UserIdentity", back_populates="provider", lazy="raise", passive_deletes="all"
    )
    group_mappings: Mapped[list["SsoGroupMapping"]] = relationship(
        "SsoGroupMapping",
        back_populates="provider",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SsoProvider id={self.id} slug={self.slug!r} enabled={self.enabled}>"


class UserIdentity(Base, TimestampMixin):
    """Links a Specivo user to one subject at one OIDC issuer.

    Indexes:
    - uq_user_identities_issuer_subject: an external subject maps to one user
    - uq_user_identities_provider_user: one identity per provider per user
    - ix_user_identities_user_id, ix_user_identities_provider_id: FK lookups
    """

    __tablename__ = "user_identities"

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        UniqueConstraint("provider_id", "user_id", name="uq_user_identities_provider_user"),
        CheckConstraint(_in("linked_via", LINKED_VIA_VALUES), name="ck_user_identities_linked_via"),
        Index("ix_user_identities_user_id", "user_id"),
        Index("ix_user_identities_provider_id", "provider_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[int] = mapped_column(ForeignKey("sso_providers.id", ondelete="RESTRICT"), nullable=False)

    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_last_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    linked_via: Mapped[str] = mapped_column(String(20), nullable=False)

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], lazy="raise")
    provider: Mapped["SsoProvider"] = relationship(
        "SsoProvider", back_populates="identities", foreign_keys=[provider_id], lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<UserIdentity id={self.id} user_id={self.user_id} provider_id={self.provider_id}>"


class SsoGroupMapping(Base, TimestampMixin):
    """Maps an IdP group value (exact, case-sensitive) to a Specivo user group.

    Indexes:
    - uq_sso_group_mappings_provider_group_user_group
    - ix_sso_group_mappings_provider_id, ix_sso_group_mappings_user_group_id: FK lookups
    """

    __tablename__ = "sso_group_mappings"

    __table_args__ = (
        UniqueConstraint(
            "provider_id", "idp_group", "user_group_id", name="uq_sso_group_mappings_provider_group_user_group"
        ),
        Index("ix_sso_group_mappings_provider_id", "provider_id"),
        Index("ix_sso_group_mappings_user_group_id", "user_group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    provider_id: Mapped[int] = mapped_column(ForeignKey("sso_providers.id", ondelete="CASCADE"), nullable=False)
    idp_group: Mapped[str] = mapped_column(String(512), nullable=False)
    user_group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id", ondelete="CASCADE"), nullable=False)

    provider: Mapped["SsoProvider"] = relationship(
        "SsoProvider", back_populates="group_mappings", foreign_keys=[provider_id], lazy="raise"
    )
    user_group: Mapped["UserGroup"] = relationship("UserGroup", foreign_keys=[user_group_id], lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<SsoGroupMapping id={self.id} provider_id={self.provider_id} "
            f"idp_group={self.idp_group!r} user_group_id={self.user_group_id}>"
        )
