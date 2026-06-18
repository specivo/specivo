"""Pure, DB-free recurrence engine for recurring patterns.

This package is intentionally decoupled from SQLAlchemy and the ORM: it never
imports ``specivo.models`` or the database. It operates purely on plain Python
values (see :class:`RecurrenceSpec`), which makes it exhaustively unit-testable
without any database or async machinery.

The service layer (a later phase) is responsible for adapting an ORM
``RecurringPattern`` into a :class:`RecurrenceSpec` and calling
:func:`expand_occurrences`.
"""

from __future__ import annotations

from specivo.services.recurrence.engine import (
    RecurrenceSpec,
    expand_occurrences,
    spec_from_mapping,
)
from specivo.services.recurrence.macros import expand_macros
from specivo.services.recurrence.rotation import next_assignee

__all__ = [
    "RecurrenceSpec",
    "expand_macros",
    "expand_occurrences",
    "next_assignee",
    "spec_from_mapping",
]
