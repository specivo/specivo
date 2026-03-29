"""Setting model — global application configuration key/value store."""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Setting(Base, TimestampMixin):
    """Global application settings stored as key/value pairs.

    Values are stored as Text (serialized JSON, plain strings, etc.).
    The API layer is responsible for parsing/serializing values.
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Setting id={self.id} key={self.key!r}>"
