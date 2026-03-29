"""Unit tests for hybrid search optimization.

Verifies that hybrid_search() avoids redundant count queries by:
1. Calling search() and semantic_search() with skip_count=True
2. Using len(merged_keys) as the total instead of sub-search counts
3. search() returning total=0 when skip_count=True (no COUNT query)

These tests mock the search internals to verify the optimization
without hitting the database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from specivo.schemas.search import SearchResult
from specivo.services.search_service import SearchService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(result_type: str, id: int, score: float = 0.5) -> SearchResult:
    """Build a minimal SearchResult for testing."""
    return SearchResult(
        result_type=result_type,
        id=id,
        title=f"{result_type}-{id}",
        subtitle=f"Test {result_type} {id}",
        snippet=None,
        score=score,
        project_key="TEST",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_search_does_not_run_count_queries():
    """hybrid_search() should call search() and semantic_search() with
    skip_count=True so they skip their COUNT(*) queries.

    This is a performance optimization: hybrid search computes its own
    total from the RRF merged results, so sub-search counts are wasted work.
    """
    service = SearchService()
    mock_session = AsyncMock()
    mock_user = AsyncMock()

    fts_results = [_make_result("issue", 1), _make_result("issue", 2)]
    sem_results = [_make_result("issue", 2), _make_result("issue", 3)]

    with (
        patch.object(service, "search", new_callable=AsyncMock, return_value=(fts_results, 0)) as mock_search,
        patch.object(
            service, "semantic_search", new_callable=AsyncMock, return_value=(sem_results, 0)
        ) as mock_sem_search,
    ):
        results, total = await service.hybrid_search(
            session=mock_session,
            query="test query",
            user=mock_user,
        )

        # Verify search() was called with skip_count=True
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs.kwargs.get("skip_count") is True or (
            len(call_kwargs.args) > 0 and "skip_count" in str(call_kwargs)
        ), "search() should be called with skip_count=True in hybrid mode"

        # Verify semantic_search() was called with skip_count=True
        mock_sem_search.assert_called_once()
        sem_call_kwargs = mock_sem_search.call_args
        assert sem_call_kwargs.kwargs.get("skip_count") is True or (
            len(sem_call_kwargs.args) > 0 and "skip_count" in str(sem_call_kwargs)
        ), "semantic_search() should be called with skip_count=True in hybrid mode"


@pytest.mark.asyncio
async def test_search_with_skip_count_returns_zero_total():
    """When skip_count=True, search() should return total=0 without
    executing any COUNT queries.

    This tests the search() method directly to verify it respects the
    skip_count parameter.
    """
    service = SearchService()
    mock_session = AsyncMock()

    # Mock the session.execute to track calls
    # We need to verify that no COUNT query is executed
    execute_calls = []
    _original_execute = mock_session.execute  # noqa: F841

    async def tracking_execute(stmt, params=None):
        sql_text = str(stmt) if hasattr(stmt, "text") else str(stmt)
        execute_calls.append(sql_text)
        # Return empty result set
        mock_result = AsyncMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        return mock_result

    mock_session.execute = AsyncMock(side_effect=tracking_execute)

    results, total = await service.search(
        session=mock_session,
        query="test",
        skip_count=True,
    )

    assert total == 0, "search() with skip_count=True should return total=0"

    # Verify no COUNT query was executed
    count_queries = [c for c in execute_calls if "COUNT" in c.upper()]
    assert len(count_queries) == 0, (
        f"search() with skip_count=True should not execute COUNT queries, but found {len(count_queries)} count queries"
    )


@pytest.mark.asyncio
async def test_hybrid_total_uses_rrf_count():
    """Hybrid search total should be len(merged_keys) from the RRF fusion,
    not from the sub-search count queries.

    Given:
    - FTS returns issues [1, 2, 3]
    - Semantic returns issues [2, 3, 4, 5]
    - Merged unique keys = [1, 2, 3, 4, 5] (5 items)

    The hybrid total should be 5 (the RRF merged count).
    """
    service = SearchService()
    mock_session = AsyncMock()
    mock_user = AsyncMock()

    fts_results = [
        _make_result("issue", 1, score=0.9),
        _make_result("issue", 2, score=0.7),
        _make_result("issue", 3, score=0.5),
    ]
    sem_results = [
        _make_result("issue", 2, score=0.8),
        _make_result("issue", 3, score=0.6),
        _make_result("issue", 4, score=0.4),
        _make_result("issue", 5, score=0.3),
    ]

    with (
        patch.object(
            service,
            "search",
            new_callable=AsyncMock,
            # Sub-search returns 0 total (because skip_count=True)
            return_value=(fts_results, 0),
        ),
        patch.object(
            service,
            "semantic_search",
            new_callable=AsyncMock,
            return_value=(sem_results, 0),
        ),
    ):
        results, total = await service.hybrid_search(
            session=mock_session,
            query="test query",
            user=mock_user,
            limit=25,
        )

        # Total should be the number of unique merged keys (5),
        # not 0 (from skip_count) and not sum of sub-totals
        assert total == 5, (
            f"Expected hybrid total=5 (RRF merged count), got {total}. "
            "hybrid_search() should use len(merged_keys) as total."
        )

        # All 5 unique results should be present
        result_ids = {(r.result_type, r.id) for r in results}
        expected_ids = {("issue", i) for i in range(1, 6)}
        assert result_ids == expected_ids
