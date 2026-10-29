"""Async database engine and session factory."""

import logging
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from specivo.core.config import get_settings

logger = logging.getLogger(__name__)


async def _register_ltree_codec(conn):
    """Register ltree type with asyncpg connection so it handles ltree as text."""
    await conn.set_type_codec(
        "ltree",
        encoder=str,
        decoder=str,
        schema="public",
        format="text",
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # WARNING: When debug=True, SQLAlchemy echo=True logs ALL SQL statements
        # including those containing sensitive data (passwords, tokens, user info).
        # This MUST be disabled in production. The warning below alerts operators
        # at startup; CI should fail if DEBUG=true reaches staging/production.
        if settings.debug:
            logger.warning(
                "Database engine created with debug=True — SQL statements will be logged. Do not use in production."
            )
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )

        # Register ltree codec for every new asyncpg connection
        @event.listens_for(_engine.sync_engine, "connect")
        def _on_connect(dbapi_connection, connection_record):
            dbapi_connection.run_async(_register_ltree_codec)

        # Per-request SQL profiling (only in debug mode)
        if settings.debug:
            from specivo.core.middleware import install_sql_debug_hooks

            install_sql_debug_hooks(_engine)

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


def set_engine(engine: AsyncEngine) -> None:
    """Replace the global engine (used by tests to inject a test engine)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = None  # Force recreation with new engine


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a transactional async DB session; commit on clean exit.

    Commits when the handler returns normally, rolls back on any exception,
    and always closes the session, even if commit/rollback raises.

    **Never depend on this with a bare** ``Depends(get_db)``. Use
    :data:`DbSession` (or :data:`DB_SESSION` where a default value is needed).
    Both declare ``scope="function"``, so FastAPI runs the code after
    ``yield`` -- the commit -- once the endpoint has returned and its response
    has been serialized, but *before* the response is sent. With the default
    request scope the commit ran after the response had gone out, so a client
    that wrote and then read straight away (a browser reload, an API client,
    an MCP agent) could read stale data. ``tests/integration/
    test_commit_before_response.py`` checks every route for this.

    Consequences of the function scope:

    - the session is closed by the time a ``StreamingResponse`` body or a
      background task runs, so neither may use it; open a session of their
      own from :func:`get_session_factory` instead;
    - a yield dependency that takes the session (``get_reader``,
      ``get_web_reader``) must itself be declared with ``scope="function"``
      -- FastAPI refuses to start otherwise.

    For multi-step transactions that need savepoints, use
    ``session.begin_nested()`` inside the endpoint.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DB_SESSION: Any = Depends(get_db, scope="function")
"""The session dependency as a default value: ``db: AsyncSession = DB_SESSION``.

Used where ``db`` follows parameters that have defaults, so the
:data:`DbSession` annotation (a parameter without a default) cannot be used.
"""

DbSession = Annotated[AsyncSession, DB_SESSION]
"""The request's DB session, committed before the response is sent."""
