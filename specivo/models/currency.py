"""Currency model: the ISO 4217 reference table every money column points at."""

from __future__ import annotations

from sqlalchemy import CHAR, Boolean, CheckConstraint, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Currency(Base, TimestampMixin):
    """One ISO 4217 currency, seeded from ``specivo/data/iso4217.csv``.

    ``minor_units`` is the number of decimal places ISO defines, ``NULL`` where
    ISO says "N.A." (metals, units of account, ``XXX``). ``name`` and
    ``symbol`` are the English forms; pages localise them with Babel.

    ``is_active`` only filters pickers: an inactive currency stays valid for
    every amount and setting that already references it. Rows are never
    deleted, because amounts reference them (``ON DELETE RESTRICT``).
    """

    __tablename__ = "currencies"

    __table_args__ = (
        CheckConstraint("code ~ '^[A-Z]{3}$'", name="ck_currencies_code_format"),
        CheckConstraint(
            "kind IN ('national', 'fund', 'metal', 'special', 'test')",
            name="ck_currencies_kind",
        ),
        # The money columns store 8 decimal places, so no currency may need more.
        CheckConstraint(
            "minor_units IS NULL OR minor_units BETWEEN 0 AND 8",
            name="ck_currencies_minor_units",
        ),
    )

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    numeric_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    minor_units: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(8), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<Currency {self.code} active={self.is_active}>"
