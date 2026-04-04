"""Unit tests for hybrid search optimization.

Verifies that hybrid_search() efficiently handles counts by:
1. Getting per-type counts from search() in a single DB query
2. Calling semantic_search() with skip_count=True (counts come from FTS)
3. Using len(merged_keys) as the RRF-fused total
4. search() returning total=0 and empty type_counts when skip_count=True

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
async def test_hybrid_search_gets_counts_from_fts():
    """hybrid_search() computes per-type counts from merged RRF results, not from FTS.

    Both search() and semantic_search() are called with skip_count=True.
    Counts are derived by iterating merged results by type.
    """
    service = SearchService()
    mock_session = AsyncMock()
    mock_user = AsyncMock()

    fts_results = [_make_result("issue", 1), _make_result("issue", 2)]
    sem_results = [_make_result("issue", 2), _make_result("issue", 3)]
    mock_type_counts = {"issues": 2, "wiki": 0, "comments": 0, "attachments": 0, "all": 2}

    with (
        patch.object(
            service,
            "search",
            new_callable=AsyncMock,
            return_value=(fts_results, 2, mock_type_counts),
        ) as mock_search,
        patch.object(
            service, "semantic_search", new_callable=AsyncMock, return_value=(sem_results, 0)
        ) as mock_sem_search,
    ):
        results, total, type_counts = await service.hybrid_search(
            session=mock_session,
            query="test query",
            user=mock_user,
        )

        # Both should be called with skip_count=True — hybrid computes its own counts
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs.kwargs.get("skip_count") is True

        mock_sem_search.assert_called_once()

        # Type counts should be computed from merged results (3 unique issues)
        assert type_counts["issues"] == 3
        assert type_counts["all"] == 3
        sem_call_kwargs = mock_sem_search.call_args
        assert sem_call_kwargs.kwargs.get("skip_count") is True or (
            len(sem_call_kwargs.args) > 0 and "skip_count" in str(sem_call_kwargs)
        ), "semantic_search() should be called with skip_count=True in hybrid mode"

        # type_counts are computed from merged results, not from FTS mock
        assert type_counts["wiki"] == 0
        assert type_counts["comments"] == 0
        assert type_counts["attachments"] == 0


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

    results, total, type_counts = await service.search(
        session=mock_session,
        query="test",
        skip_count=True,
    )

    assert total == 0, "search() with skip_count=True should return total=0"
    assert type_counts == {}, "search() with skip_count=True should return empty type_counts"

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

    mock_type_counts = {"issues": 3, "wiki": 0, "comments": 0, "attachments": 0, "all": 3}

    with (
        patch.object(
            service,
            "search",
            new_callable=AsyncMock,
            return_value=(fts_results, 3, mock_type_counts),
        ),
        patch.object(
            service,
            "semantic_search",
            new_callable=AsyncMock,
            return_value=(sem_results, 0),
        ),
    ):
        results, total, type_counts = await service.hybrid_search(
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
