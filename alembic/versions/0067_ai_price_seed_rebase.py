"""The price immutability trigger lets the bundled seed move a first period's start back.

Create Date: 2026-11-07
Revision ID: 0067
Revises: 0066

- ``ai_model_prices_immutable()`` still refuses any in-place change of the
  prices, currency, identity, ``source`` and ``effective_from``, with one
  exception: a ``seed`` row's ``effective_from`` may move **earlier** while the
  transaction-local setting ``specivo.ai_price_rebase`` is ``on``. The seed
  loader sets it to extend a model's first bundled period back to
  ``FIRST_PERIOD_START``, so usage logged before the price list's ``as_of``
  is priced. Admin rows are never affected.
- No data changes here: the next seed run (worker start, or Admin -> AI model
  prices -> load bundled prices) moves the starts and re-prices the entries.

The downgrade restores the strict trigger function; moved starts stay.
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

_PRICE_COLUMNS = (
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "reasoning",
    "long_input",
    "long_output",
    "long_cache_write_5m",
    "long_cache_write_1h",
    "long_cache_read",
)

_FIXED = (
    "provider",
    "model",
    "currency_code",
    *_PRICE_COLUMNS,
    "long_context_threshold",
    "is_local",
    "baseline_model",
    "source",
)

_FUNCTION = """
CREATE OR REPLACE FUNCTION ai_model_prices_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ({changed}) THEN
        RAISE EXCEPTION 'ai_model_prices rows are never edited in place: close the period and add a new price'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ai_model_prices_immutable';
    END IF;
    RETURN NEW;
END;
$$;
"""

_REBASE_ALLOWED = (
    "(OLD.source = 'seed' AND NEW.effective_from < OLD.effective_from"
    " AND coalesce(current_setting('specivo.ai_price_rebase', true), '') = 'on')"
)


def _function(rebase: bool) -> str:
    fixed = [f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _FIXED]
    start = "NEW.effective_from IS DISTINCT FROM OLD.effective_from"
    if rebase:
        start = f"({start} AND NOT {_REBASE_ALLOWED})"
    return _FUNCTION.format(changed=" OR ".join([*fixed, start]))


def upgrade() -> None:
    op.execute(_function(rebase=True))


def downgrade() -> None:
    op.execute(_function(rebase=False))
