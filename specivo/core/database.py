"""Async database engine and session factory."""

import logging
from collections.abc import AsyncGenerator

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
    """FastAPI dependency: yields a transactional async DB session.

    Commits on clean exit, rolls back on any exception. The session is
    always closed, even if commit/rollback raises.

    For multi-step transactions that need savepoints, use
    ``session.begin_nested()`` inside the endpoint.

    .. note:: **Browser-UI reload race condition**

       Endpoints whose HTML/Alpine.js responses trigger
       ``location.reload()`` should call ``await db.commit()`` explicitly
       before returning.  Otherwise the reload request may arrive before
       this dependency's post-yield commit completes, and the user won't
       see their changes.  The duplicate commit is safe — committing an
       already-committed session is a no-op.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
