"""Unit tests for the Redis sliding window rate limiter.

Redis calls are mocked via ``unittest.mock`` so these tests run without a
live Redis instance.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specivo.core.rate_limit import RateLimiter, _get_client_ip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_results(count_before: int, *, with_entry=True):
    """Return fake pipeline results: [zremrangebyscore, zcard, zadd, expire]."""
    return [0, count_before, 1 if with_entry else 0, True]


def _make_redis_mock(count_before: int, oldest_score_us: int | None = None):
    """Return a (redis_mock, pipeline_mock) pair with preset behaviour."""
    pipeline_mock = MagicMock()
    pipeline_mock.zremrangebyscore = MagicMock()
    pipeline_mock.zcard = MagicMock()
    pipeline_mock.zadd = MagicMock()
    pipeline_mock.expire = MagicMock()
    pipeline_mock.execute = AsyncMock(return_value=_make_pipeline_results(count_before))

    redis_mock = AsyncMock()
    redis_mock.pipeline = MagicMock(return_value=pipeline_mock)

    if oldest_score_us is not None:
        # Return the oldest entry for retry_after calculation
        redis_mock.zrange = AsyncMock(return_value=[("ts", float(oldest_score_us))])
    else:
        redis_mock.zrange = AsyncMock(return_value=[])

    redis_mock.zrem = AsyncMock(return_value=1)

    return redis_mock, pipeline_mock


# ---------------------------------------------------------------------------
# RateLimiter.check — allowed cases
# ---------------------------------------------------------------------------


class TestRateLimiterAllowed:
    @pytest.mark.asyncio
    async def test_first_request_is_allowed(self):
        redis_mock, _ = _make_redis_mock(count_before=0)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.rate_limit.RateLimiter.check", wraps=limiter.check):
            with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
                allowed, remaining, retry_after = await limiter.check("user:1")

        assert allowed is True
        assert remaining == 9  # 10 - 0 - 1
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_request_just_under_limit_is_allowed(self):
        # 9 requests already in window, limit is 10 → 10th request allowed
        redis_mock, _ = _make_redis_mock(count_before=9)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            allowed, remaining, retry_after = await limiter.check("user:1")

        assert allowed is True
        assert remaining == 0
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_remaining_decrements_correctly(self):
        for already_used in range(10):
            redis_mock, _ = _make_redis_mock(count_before=already_used)
            limiter = RateLimiter("test", max_requests=10, window_seconds=60)

            with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
                allowed, remaining, _ = await limiter.check("user:1")

            assert allowed is True
            assert remaining == 10 - already_used - 1


# ---------------------------------------------------------------------------
# RateLimiter.check — denied cases
# ---------------------------------------------------------------------------


class TestRateLimiterDenied:
    @pytest.mark.asyncio
    async def test_request_at_limit_is_denied(self):
        now_us = int(time.time() * 1_000_000)
        # oldest entry is 30s ago → retry_after ≈ 30s
        oldest_us = now_us - 30 * 1_000_000
        redis_mock, _ = _make_redis_mock(count_before=10, oldest_score_us=oldest_us)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            allowed, remaining, retry_after = await limiter.check("user:1")

        assert allowed is False
        assert remaining == 0
        assert retry_after > 0

    @pytest.mark.asyncio
    async def test_retry_after_reflects_oldest_entry_age(self):
        now_us = int(time.time() * 1_000_000)
        # oldest entry is 10s ago in a 60s window → retry_after ≈ 50s
        oldest_us = now_us - 10 * 1_000_000
        redis_mock, _ = _make_redis_mock(count_before=10, oldest_score_us=oldest_us)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            allowed, remaining, retry_after = await limiter.check("user:1")

        assert allowed is False
        # Should be approximately 50s (60 - 10), allow ±2s for clock drift
        assert 48 <= retry_after <= 52

    @pytest.mark.asyncio
    async def test_denied_request_entry_is_removed_from_zset(self):
        now_us = int(time.time() * 1_000_000)
        oldest_us = now_us - 5 * 1_000_000
        redis_mock, _ = _make_redis_mock(count_before=10, oldest_score_us=oldest_us)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            allowed, _, _ = await limiter.check("user:1")

        assert allowed is False
        # The zrem call should have been made to remove the rejected entry
        redis_mock.zrem.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_after_minimum_is_one(self):
        now_us = int(time.time() * 1_000_000)
        # oldest entry is almost exactly window_seconds ago → retry_after would be ~0
        oldest_us = now_us - 59 * 1_000_000
        redis_mock, _ = _make_redis_mock(count_before=10, oldest_score_us=oldest_us)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            _, _, retry_after = await limiter.check("user:1")

        assert retry_after >= 1


# ---------------------------------------------------------------------------
# Graceful degradation — Redis unavailable
# ---------------------------------------------------------------------------


class TestRateLimiterGracefulDegradation:
    @pytest.mark.asyncio
    async def test_allows_all_when_redis_unavailable(self):
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(side_effect=ConnectionError("Redis down"))):
            allowed, remaining, retry_after = await limiter.check("user:1")

        assert allowed is True
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_allows_when_pipeline_raises(self):
        redis_mock = AsyncMock()
        pipeline_mock = MagicMock()
        pipeline_mock.zremrangebyscore = MagicMock()
        pipeline_mock.zcard = MagicMock()
        pipeline_mock.zadd = MagicMock()
        pipeline_mock.expire = MagicMock()
        pipeline_mock.execute = AsyncMock(side_effect=Exception("Pipeline exploded"))
        redis_mock.pipeline = MagicMock(return_value=pipeline_mock)

        limiter = RateLimiter("test", max_requests=5, window_seconds=30)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            allowed, _, _ = await limiter.check("user:99")

        assert allowed is True


# ---------------------------------------------------------------------------
# Sliding window: old entries are pruned
# ---------------------------------------------------------------------------


class TestSlidingWindowBehaviour:
    @pytest.mark.asyncio
    async def test_window_slide_clears_old_entries(self):
        """The pipeline must call zremrangebyscore to prune old entries."""
        redis_mock, pipeline_mock = _make_redis_mock(count_before=5)
        limiter = RateLimiter("test", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            await limiter.check("user:1")

        # zremrangebyscore must have been called on the pipeline
        pipeline_mock.zremrangebyscore.assert_called_once()
        args = pipeline_mock.zremrangebyscore.call_args[0]
        assert args[1] == "-inf"  # from -inf
        # The cutoff score is approximately now - window; spot-check it's large
        assert args[2] > 0

    @pytest.mark.asyncio
    async def test_key_prefixed_correctly(self):
        redis_mock, pipeline_mock = _make_redis_mock(count_before=0)
        limiter = RateLimiter("auth_login", max_requests=10, window_seconds=60)

        with patch("specivo.core.redis.get_redis", AsyncMock(return_value=redis_mock)):
            await limiter.check("192.168.1.1")

        args = pipeline_mock.zremrangebyscore.call_args[0]
        assert args[0] == "rl:auth_login:192.168.1.1"


# ---------------------------------------------------------------------------
# Identifier extraction
# ---------------------------------------------------------------------------


class TestGetClientIp:
    def _make_request(self, forwarded_for: str | None = None, client_host: str | None = None):
        req = MagicMock()
        headers = {}
        if forwarded_for:
            headers["X-Forwarded-For"] = forwarded_for
        req.headers = headers
        if client_host:
            req.client = MagicMock()
            req.client.host = client_host
        else:
            req.client = None
        return req

    def test_ignores_xff_when_client_not_in_chain(self):
        """XFF is not trusted when client.host does not appear in the XFF chain."""
        req = self._make_request(forwarded_for="203.0.113.10", client_host="10.0.0.1")
        assert _get_client_ip(req) == "10.0.0.1"

    def test_does_not_trust_private_ip_without_trusted_proxies(self):
        """XFF is NOT trusted when client.host is a private IP but not in trusted_proxies."""
        req = self._make_request(forwarded_for="203.0.113.10, 10.0.0.1", client_host="10.0.0.1")
        assert _get_client_ip(req) == "10.0.0.1"

    def test_falls_back_to_client_host(self):
        req = self._make_request(client_host="192.168.1.5")
        assert _get_client_ip(req) == "192.168.1.5"

    def test_returns_unknown_when_no_info(self):
        req = self._make_request()
        assert _get_client_ip(req) == "unknown"
