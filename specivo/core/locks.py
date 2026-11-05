"""Small locking helpers: a token-owned Redis lock and a session-level PostgreSQL advisory lock.

``TokenLock`` is ``SET key token NX EX ttl`` with a release and a TTL refresh
that only act while the key still holds *our* token (checked and applied in one
Lua script), so a holder whose lock expired can never delete or extend the lock
somebody else took since. It guards "one run at a time" for work dispatched
from a request: the request takes it before queueing, the worker refreshes it
as it goes and releases it at the end, and a crashed worker's lock simply
expires.

``try_advisory_lock`` takes a session-level ``pg_try_advisory_lock`` on a
connection the caller keeps open for the duration of the work. Unlike the
Redis lock it cannot outlive its holder: PostgreSQL drops it when the
connection ends, however the worker died.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)

# Delete / extend the key only while it still holds the caller's token.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
# Take KEYS[1] for ARGV[1] (TTL ARGV[2]) unless one of the other KEYS exists: -1 blocked, 1 taken, 0 held.
_ACQUIRE_UNLESS_SCRIPT = """
for i = 2, #KEYS do
    if redis.call('exists', KEYS[i]) == 1 then
        return -1
    end
end
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
    return 1
end
return 0
"""
_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class LockUnavailableError(RuntimeError):
    """Redis could not be asked; callers must treat the lock as not acquired (fail closed)."""


class LockLostError(RuntimeError):
    """A run lock is now held by somebody else; the holder must stop."""


class TokenLock:
    """A Redis lock owned by a token (for example the id of the task doing the work)."""

    def __init__(self, redis: Redis, key: str) -> None:
        self.redis = redis
        self.key = key

    async def acquire(self, token: str, ttl: int) -> bool:
        """Take the lock for *token* unless someone holds it. Raises ``LockUnavailableError`` if Redis fails."""
        try:
            return bool(await self.redis.set(self.key, token, nx=True, ex=ttl))
        except RedisError as exc:
            raise LockUnavailableError(f"Could not take lock {self.key}: {type(exc).__name__}") from exc

    async def acquire_unless(self, token: str, ttl: int, blockers: list[str]) -> int:
        """Take the lock unless one of the *blockers* keys exists, in one atomic step.

        Returns 1 when taken, 0 when the lock is held, -1 when a blocker
        exists (and nothing was taken). Raises ``LockUnavailableError`` if
        Redis fails.
        """
        try:
            result = await self.redis.eval(  # type: ignore[misc]
                _ACQUIRE_UNLESS_SCRIPT, 1 + len(blockers), self.key, *blockers, token, str(ttl)
            )
        except RedisError as exc:
            raise LockUnavailableError(f"Could not take lock {self.key}: {type(exc).__name__}") from exc
        return int(result)

    async def holder(self) -> str | None:
        """The token holding the lock, or None. Raises ``LockUnavailableError`` if Redis fails."""
        try:
            value = await self.redis.get(self.key)
        except RedisError as exc:
            raise LockUnavailableError(f"Could not read lock {self.key}: {type(exc).__name__}") from exc
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def refresh(self, token: str, ttl: int) -> bool:
        """Extend the lock to *ttl* seconds if *token* still holds it; never raises."""
        try:
            return bool(await self.redis.eval(_REFRESH_SCRIPT, 1, self.key, token, str(ttl)))  # type: ignore[misc]
        except RedisError as exc:
            logger.warning("Could not refresh lock %s: %s", self.key, type(exc).__name__)
            return False

    async def hold(self, token: str, ttl: int) -> bool:
        """Keep the lock for *token*: extend it, or take it again if it expired; False if another token holds it.

        For long-running holders (a heartbeat). A lock lost to expiry or a Redis
        restart is re-taken at once; one somebody else now holds is not, and
        the caller must stop rather than carry on unprotected. Raises
        ``LockUnavailableError`` when Redis cannot be asked.
        """
        if await self.refresh(token, ttl):
            return True
        return await self.acquire(token, ttl)

    async def release(self, token: str) -> bool:
        """Delete the lock if *token* still holds it; never raises."""
        try:
            return bool(await self.redis.eval(_RELEASE_SCRIPT, 1, self.key, token))  # type: ignore[misc]
        except RedisError as exc:
            logger.warning("Could not release lock %s: %s", self.key, type(exc).__name__)
            return False


#: Celery task states after which a run lock still naming the task is stale.
FINISHED_TASK_STATES = frozenset({"SUCCESS", "FAILURE", "REVOKED"})


async def clear_if_finished(lock: TokenLock, task_state: Callable[[str], str]) -> None:
    """Release *lock* if the task holding it has finished (a stale lock); compare-and-delete, so only that holder's."""
    holder = await lock.holder()
    if holder and task_state(holder) in FINISHED_TASK_STATES:
        await lock.release(holder)


async def acquire_run_lock(lock: TokenLock, token: str, ttl: int, task_state: Callable[[str], str]) -> bool:
    """Take a run lock for the task *token* is the id of; True if acquired.

    When the lock is held by a task that has finished without releasing it
    (``task_state(holder)`` is a finished state, for example its worker died
    between the end of the work and the release, or it ended as a no-op), the
    stale lock is cleared (compare-and-delete, so only that holder's) and taken.
    Raises ``LockUnavailableError`` when Redis cannot be asked: fail closed.
    """
    if await lock.acquire(token, ttl):
        return True
    holder = await lock.holder()
    if holder and task_state(holder) in FINISHED_TASK_STATES:
        await lock.release(holder)
        return await lock.acquire(token, ttl)
    return False


AdvisoryKey = tuple[int, int, bool]
"""``(namespace, key, shared)`` of one session-level advisory lock."""


@asynccontextmanager
async def try_advisory_locks(engine: AsyncEngine, keys: list[AdvisoryKey]) -> AsyncIterator[bool]:
    """Hold all of *keys* (``pg_try_advisory_lock`` / ``..._shared``) on one dedicated connection, or none.

    Yields whether every lock was acquired; when one was not, those already
    taken are released at once and the caller should do nothing. Shared locks
    let several holders of a scope run side by side while excluding an
    exclusive holder of the same key (an instance-wide job). The connection is
    kept open (and the locks held) until the block ends; if the process dies
    the connection closes and PostgreSQL frees them.
    """
    async with engine.connect() as connection:
        held: list[AdvisoryKey] = []
        for namespace, key, shared in keys:
            take = func.pg_try_advisory_lock_shared if shared else func.pg_try_advisory_lock
            if not await connection.scalar(select(take(namespace, key))):
                break
            held.append((namespace, key, shared))
        acquired = len(held) == len(keys)
        if not acquired:
            await _unlock(connection, held)
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await _unlock(connection, held)
                await connection.commit()


async def _unlock(connection: AsyncConnection, held: list[AdvisoryKey]) -> None:
    for namespace, key, shared in reversed(held):
        release = func.pg_advisory_unlock_shared if shared else func.pg_advisory_unlock
        await connection.scalar(select(release(namespace, key)))


@asynccontextmanager
async def try_advisory_lock(engine: AsyncEngine, namespace: int, key: int) -> AsyncIterator[bool]:
    """Hold ``pg_try_advisory_lock(namespace, key)`` on a dedicated connection for the block.

    Yields whether the lock was acquired; when it was not, the caller should do
    nothing. See :func:`try_advisory_locks`.
    """
    async with try_advisory_locks(engine, [(namespace, key, False)]) as acquired:
        yield acquired
