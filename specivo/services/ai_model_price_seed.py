"""Write the bundled AI model price seed into ``ai_model_prices`` (synchronous SQLAlchemy Core).

:func:`apply_seed` reads the stored ``seed`` rows, asks
:func:`specivo.core.ai_pricing_seed.plan_seed` what changed, and writes it:
closes first, then inserts, then links each closed row to its successor
(``superseded_by_id``). ``admin`` rows are never read or written.

It takes a synchronous ``Connection`` so the same code serves the migration
that first seeds the table (``op.get_bind()``) and the service
(``AsyncSession.run_sync``). A transaction-scoped advisory lock serialises
concurrent runs (several workers starting at once), so the exclusion
constraint never sees two runs insert the same period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Table, func, insert, select, text, update

from specivo.core.ai_pricing_seed import (
    PRICE_COLUMNS,
    HistoryPeriod,
    SeedPlan,
    Snapshot,
    StoredSeedRow,
    load_history,
    load_snapshot,
    plan_seed,
)

#: Key of the advisory lock held while a seed run writes.
SEED_LOCK_KEY = "ai_model_prices_seed"


@dataclass
class SeedOutcome:
    """What one seed run did."""

    as_of: datetime
    inserted: int = 0
    closed: int = 0
    history_inserted: int = 0
    aliases_updated: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.closed or self.aliases_updated)

    def details(self) -> dict[str, Any]:
        """Audit details (counts and the snapshot instant; the skipped messages are capped)."""
        return {
            "as_of": self.as_of.isoformat(),
            "inserted": self.inserted,
            "closed": self.closed,
            "history_inserted": self.history_inserted,
            "aliases_updated": self.aliases_updated,
            "unchanged": self.unchanged,
            "skipped": len(self.skipped),
        }


def _default_table() -> Table:
    from specivo.models.ai_model_price import AiModelPrice

    return AiModelPrice.__table__  # type: ignore[return-value]


def read_stored_seed_rows(conn: Connection, table: Table) -> list[StoredSeedRow]:
    c = table.c
    columns = [
        c.id,
        c.provider,
        c.model,
        c.currency_code,
        c.long_context_threshold,
        c.aliases,
        c.effective_from,
        c.effective_to,
        c.is_active,
        *(c[name] for name in PRICE_COLUMNS),
    ]
    rows = conn.execute(select(*columns).where(c.source == "seed")).mappings().all()
    return [
        StoredSeedRow(
            id=row["id"],
            provider=row["provider"],
            model=row["model"],
            currency=row["currency_code"].strip(),
            prices={name: row[name] for name in PRICE_COLUMNS},
            long_context_threshold=row["long_context_threshold"],
            aliases=tuple(row["aliases"] or ()),
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            is_active=row["is_active"],
        )
        for row in rows
    ]


def write_plan(conn: Connection, table: Table, plan: SeedPlan) -> SeedOutcome:
    """Execute *plan*; returns the counts."""
    c = table.c
    outcome = SeedOutcome(as_of=plan.as_of, unchanged=plan.unchanged, skipped=list(plan.skipped))
    for row_id, instant in plan.closes.items():
        conn.execute(update(table).where(c.id == row_id).values(effective_to=instant, updated_at=func.now()))
        outcome.closed += 1
    if plan.inserts:
        stmt = insert(table).returning(c.id, sort_by_parameter_order=True)
        new_ids = conn.execute(stmt, [p.values() for p in plan.inserts]).scalars().all()
        for planned, new_id in zip(plan.inserts, new_ids, strict=True):
            if planned.supersedes_id is not None:
                conn.execute(update(table).where(c.id == planned.supersedes_id).values(superseded_by_id=new_id))
            if planned.from_history:
                outcome.history_inserted += 1
            else:
                outcome.inserted += 1
    for row_id, aliases in plan.alias_updates.items():
        conn.execute(update(table).where(c.id == row_id).values(aliases=list(aliases), updated_at=func.now()))
        outcome.aliases_updated += 1
    return outcome


def apply_seed(
    conn: Connection,
    table: Table | None = None,
    snapshot: Snapshot | None = None,
    history: tuple[HistoryPeriod, ...] | None = None,
) -> SeedOutcome:
    """Load the bundled snapshot (and history file) into *table*; idempotent.

    Defaults: the ``ai_model_prices`` table and the committed files in
    ``specivo/data``. Runs in the caller's transaction.
    """
    table = table if table is not None else _default_table()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    history = history if history is not None else load_history()
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": SEED_LOCK_KEY})
    plan = plan_seed(read_stored_seed_rows(conn, table), snapshot, history)
    return write_plan(conn, table, plan)
