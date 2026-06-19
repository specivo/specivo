"""Shared async primitives for Celery sync tasks.

Celery's prefork worker reuses one child process for many tasks, and each task
runs its coroutine in a *new* event loop via :func:`run_async`. A shared cached
engine (the module-level pool in :mod:`specivo.core.database`) keeps its asyncpg
connections bound to the loop they were first opened on, so a later task running
in a different loop fails with ``RuntimeError: Event loop is closed`` or
"got Future attached to a different loop". :func:`task_session` sidesteps this by
creating and disposing a dedicated ``NullPool`` engine inside each task, so no
pooled connection is ever reused across loops.

This module must not import any ``specivo.tasks.*`` module to avoid a circular
import with ``specivo/tasks/__init__.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from specivo.core.config import get_settings


def run_async(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine in a new event loop (for Celery sync tasks)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@asynccontextmanager
async def task_session():  # type: ignore[no-untyped-def]
    """Yield a session on a per-call engine bound to the current event loop.

    Celery's prefork worker reuses one child process for many tasks, each run
    by :func:`run_async` in a *new* loop. The shared cached engine's asyncpg
    connections stay bound to the first loop, so later tasks fail with
    "got Future attached to a different loop". A dedicated NullPool engine
    created and disposed inside the task avoids any cross-loop pool reuse.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()
