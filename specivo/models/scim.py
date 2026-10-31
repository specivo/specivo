"""SCIM 2.0 provisioning: which Specivo users and groups an identity provider manages.

A ``ScimResource`` row says that one SSO provider's SCIM client created (or
adopted) a Specivo user or user group, and gives it the opaque SCIM ``id``
the IdP addresses it by. A user or a group belongs to at most one provider's
SCIM (the unique ``user_id`` / ``user_group_id``).

A user resource deleted through SCIM keeps its row with ``deleted_at`` set:
the account stays deactivated (issues and history keep their author) and
stays bound to the provider, so no other provider can claim it and the
provider's OIDC login never links it again until the IdP creates it anew. A
group resource goes away with its user group.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin

#: SCIM resource types Specivo serves (RFC 7643 section 4 and 4.2).
SCIM_RESOURCE_TYPES = ("User", "Group")


class ScimResource(Base, TimestampMixin):
    """One user or user group provisioned by one provider's SCIM client.

    Indexes:
    - uq_scim_resources_scim_id: the id in SCIM URLs
    - uq_scim_resources_user_id / uq_scim_resources_user_group_id: one provider per
      user or group (and the FK indexes)
    - uq_scim_resources_provider_user_name: a live ``userName`` is unique per
      provider, case-insensitively
    - ix_scim_resources_provider_type: listing and filtering
    - ix_scim_resources_provider_external_id: ``externalId eq`` filters
    """

    __tablename__ = "scim_resources"

    __table_args__ = (
        UniqueConstraint("scim_id", name="uq_scim_resources_scim_id"),
        UniqueConstraint("user_id", name="uq_scim_resources_user_id"),
        UniqueConstraint("user_group_id", name="uq_scim_resources_user_group_id"),
        CheckConstraint("resource_type IN ('User', 'Group')", name="ck_scim_resources_resource_type"),
        CheckConstraint(
            "(resource_type = 'User' AND user_id IS NOT NULL AND user_group_id IS NULL)"
            " OR (resource_type = 'Group' AND user_group_id IS NOT NULL AND user_id IS NULL"
            " AND deleted_at IS NULL)",
            name="ck_scim_resources_one_target",
        ),
        Index(
            "uq_scim_resources_provider_user_name",
            "provider_id",
            func.lower(text("user_name")),
            unique=True,
            postgresql_where=text("resource_type = 'User' AND deleted_at IS NULL"),
        ),
        Index("ix_scim_resources_provider_type", "provider_id", "resource_type"),
        Index(
            "ix_scim_resources_provider_external_id",
            "provider_id",
            "external_id",
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    provider_id: Mapped[int] = mapped_column(ForeignKey("sso_providers.id", ondelete="CASCADE"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(8), nullable=False)
    # Opaque, stable, never the database id: a random UUID in canonical form.
    scim_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    user_group_id: Mapped[int | None] = mapped_column(ForeignKey("user_groups.id", ondelete="CASCADE"), nullable=True)
    # The IdP's own identifier, echoed back; not assumed to equal the OIDC ``sub``.
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The ``userName`` exactly as the IdP sent it (the login is a normalised
    # form of it). Users only.
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set by a SCIM DELETE of a user; the resource then answers 404.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ScimResource id={self.id} provider_id={self.provider_id} type={self.resource_type} "
            f"scim_id={self.scim_id!r}>"
        )
