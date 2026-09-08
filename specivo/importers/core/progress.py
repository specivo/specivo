"""Progress reporting for long-running imports.

Importing a large instance takes a long time and skips rows the operator needs
to hear about, so progress and warnings go through a protocol rather than bare
``print`` calls. The CLI implementation logs; a future admin UI can push the
same events over a WebSocket without touching the pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# How often a phase reports progress while it runs. Emitting a line per row
# would bury the warnings that matter in an import of any real size.
_PROGRESS_INTERVAL_SECONDS = 5.0


@runtime_checkable
class ProgressReporter(Protocol):
    """Receives phase lifecycle events, per-item ticks, and warnings."""

    def phase_started(self, phase: str, total: int | None = None) -> None: ...

    def item_done(self, phase: str, count: int = 1) -> None: ...

    def phase_done(self, phase: str, summary: dict[str, Any] | None = None) -> None: ...

    def warning(self, message: str, context: dict[str, Any] | None = None) -> None: ...


class NullProgressReporter:
    """Discards every event. Default for tests and library callers."""

    def phase_started(self, phase: str, total: int | None = None) -> None:
        """Ignore the event."""

    def item_done(self, phase: str, count: int = 1) -> None:
        """Ignore the event."""

    def phase_done(self, phase: str, summary: dict[str, Any] | None = None) -> None:
        """Ignore the event."""

    def warning(self, message: str, context: dict[str, Any] | None = None) -> None:
        """Ignore the event."""


class CliProgressReporter:
    """Logs progress, throttled to one line per phase every few seconds."""

    def __init__(self, interval_seconds: float = _PROGRESS_INTERVAL_SECONDS) -> None:
        self._interval = interval_seconds
        self._counts: dict[str, int] = {}
        self._last_report: dict[str, float] = {}
        self._started: dict[str, float] = {}

    def phase_started(self, phase: str, total: int | None = None) -> None:
        """Record the start time and log the phase heading."""
        now = time.monotonic()
        self._counts[phase] = 0
        self._started[phase] = now
        self._last_report[phase] = now
        if total is None:
            logger.info("[%s] started", phase)
        else:
            logger.info("[%s] started (%d items)", phase, total)

    def item_done(self, phase: str, count: int = 1) -> None:
        """Count *count* processed items, logging at most once per interval."""
        self._counts[phase] = self._counts.get(phase, 0) + count
        now = time.monotonic()
        if now - self._last_report.get(phase, now) >= self._interval:
            self._last_report[phase] = now
            logger.info("[%s] %d processed", phase, self._counts[phase])

    def phase_done(self, phase: str, summary: dict[str, Any] | None = None) -> None:
        """Log the final count and elapsed time for *phase*."""
        elapsed = time.monotonic() - self._started.get(phase, time.monotonic())
        count = self._counts.get(phase, 0)
        if summary:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
            logger.info("[%s] done: %d in %.1fs (%s)", phase, count, elapsed, detail)
        else:
            logger.info("[%s] done: %d in %.1fs", phase, count, elapsed)

    def warning(self, message: str, context: dict[str, Any] | None = None) -> None:
        """Log a warning, appending sorted context key/value pairs."""
        if context:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(context.items()))
            logger.warning("%s (%s)", message, detail)
        else:
            logger.warning("%s", message)
