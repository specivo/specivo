"""Column helpers for money: an amount column plus a sibling currency column.

Every amount is stored as an exact ``NUMERIC`` next to a ``CHAR(3)`` column
that references ``currencies.code``. A model declares the pair with these
helpers, so the types, the foreign key and its index are the same everywhere::

    class Fee(Base):
        __tablename__ = "fees"
        __table_args__ = (*money_pair_constraints("fees", "price", nullable=True),)

        price_amount: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
        price_currency: Mapped[str | None] = currency_column(nullable=True)

        @property
        def price(self) -> Money | None:
            return money_or_none(self.price_amount, self.price_currency)

Scales:

- ``amount`` -- computed amounts, ``NUMERIC(20, 8)``. Eight places hold any
  ISO currency (four at most) plus sub-cent per-token costs. Never rounded
  when stored.
- ``unit_price`` -- prices per unit (per 1M tokens), ``NUMERIC(18, 6)``.
- ``rate`` -- exchange rates, ``NUMERIC(24, 12)``.

PostgreSQL cannot vary the scale per row, so the scale is fixed per column
kind; precision per currency is enforced on input
(:func:`specivo.core.money.check_minor_units`), not by the column.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Index, Numeric
from sqlalchemy.orm import MappedColumn, mapped_column

from specivo.core.money import Money

MoneyColumnKind = Literal["amount", "unit_price", "rate"]

#: (precision, scale) of each kind of money column.
MONEY_NUMERIC: dict[str, tuple[int, int]] = {
    "amount": (20, 8),
    "unit_price": (18, 6),
    "rate": (24, 12),
}


def money_numeric(kind: MoneyColumnKind = "amount") -> Numeric:
    """The ``NUMERIC`` type of a money column of *kind* (``asdecimal``: values are ``Decimal``)."""
    precision, scale = MONEY_NUMERIC[kind]
    return Numeric(precision, scale, asdecimal=True)


def amount_column(kind: MoneyColumnKind = "amount", *, nullable: bool = False, **kwargs: Any) -> MappedColumn[Any]:
    """An amount column of *kind* (see the module docstring for the scales)."""
    return mapped_column(money_numeric(kind), nullable=nullable, **kwargs)


def currency_column(*, nullable: bool = False, **kwargs: Any) -> MappedColumn[Any]:
    """A ``CHAR(3)`` column referencing ``currencies.code``.

    ``ON DELETE RESTRICT``: currencies are never deleted, only deactivated.
    Add its index with :func:`money_pair_constraints` (PostgreSQL does not
    index foreign keys on its own).
    """
    return mapped_column(
        CHAR(3),
        ForeignKey("currencies.code", ondelete="RESTRICT"),
        nullable=nullable,
        **kwargs,
    )


def money_pair_constraints(table: str, name: str, *, nullable: bool) -> tuple[Any, ...]:
    """``__table_args__`` entries for the ``<name>_amount`` / ``<name>_currency`` pair.

    Always the index on the currency column; for a nullable pair also a check
    that the two are set together (both NULL or neither).
    """
    items: list[Any] = [Index(f"ix_{table}_{name}_currency", f"{name}_currency")]
    if nullable:
        items.append(
            CheckConstraint(
                f"({name}_amount IS NULL) = ({name}_currency IS NULL)",
                name=f"ck_{table}_{name}_money_pair",
            )
        )
    return tuple(items)


def money_or_none(amount: Decimal | None, currency: str | None) -> Money | None:
    """Combine a stored pair into :class:`Money`, or ``None`` when either half is NULL."""
    if amount is None or currency is None:
        return None
    return Money(amount, currency)
