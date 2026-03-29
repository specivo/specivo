"""SQLAlchemy base model with common mixins."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TimestampMixin:
    """Adds created_at and updated_at (timezone-aware) to any model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LockVersionMixin:
    """Optimistic locking via SQLAlchemy version_id_col.

    ``__mapper_args__`` must reference the actual column *object*, not a string.
    Subclasses inherit this mixin AFTER the model body defines the table, so
    SQLAlchemy can resolve the column at mapper configuration time.

    Usage::

        class Issue(Base, TimestampMixin, LockVersionMixin):
            __tablename__ = "issues"
            id: Mapped[int] = mapped_column(primary_key=True)

    Raises ``sqlalchemy.orm.exc.StaleDataError`` on concurrent update collision.
    """

    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __mapper_args__: dict = {"version_id_col": lock_version}
