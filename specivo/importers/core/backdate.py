"""Restore original timestamps on imported rows.

An imported issue from 2019 should say 2019, not the moment the import ran.
Every model carries ``created_at`` and ``updated_at`` with database defaults,
and ``updated_at`` additionally has an ``onupdate``, so the values have to be
put back after the row is created.

A Core UPDATE is used rather than assigning to the ORM object: SQLAlchemy
applies ``onupdate`` only to columns absent from the SET clause, so naming
``updated_at`` explicitly is what stops it being overwritten with "now".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import Table, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def backdate(
    session: AsyncSession,
    model: Any,
    row_id: int,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    **extra: datetime | None,
) -> None:
    """Set *row_id*'s timestamps to the values the source recorded.

    *model* is a mapped class or a :class:`~sqlalchemy.Table`. ``extra`` takes
    any further timestamp columns a caller wants restored in the same statement,
    such as an issue's ``closed_on``. ``None`` values are ignored, so a source
    that never recorded a timestamp leaves the database default alone.

    Does nothing when there is nothing to set, which keeps the call site free of
    conditionals.
    """
    values: dict[str, datetime] = {
        name: value
        for name, value in {"created_at": created_at, "updated_at": updated_at, **extra}.items()
        if value is not None
    }
    if not values:
        return

    table: Table = model if isinstance(model, Table) else model.__table__
    unknown = set(values) - set(table.c.keys())
    if unknown:
        raise ValueError(f"{table.name} has no column(s) {sorted(unknown)}")

    await session.execute(
        update(table).where(table.c.id == row_id).values(**values).execution_options(synchronize_session=False)
    )
