"""Hybrid search fusion: RRF order, ranks, scope filtering, type counts and pinned issues.

The keyword and semantic sides are stubbed, so these pin how hybrid_search
merges them: duplicates within and across the lists, the display types that
several source types share, and paging around pinned issues.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from specivo.schemas.search import SearchResult
from specivo.services.search_service import SearchService

pytestmark = pytest.mark.unit


def _r(result_type: str, id_: int) -> SearchResult:
    return SearchResult(
        result_type=result_type,
        id=id_,
        title=f"{result_type}-{id_}",
        subtitle=None,
        snippet=f"s{id_}",
        score=0.1,
        project_key="HYB",
    )


# The keyword side repeats issue 1; ``comment``/``journal`` and ``wiki``/``wiki_page`` share a display type.
_FTS = [_r("issue", 1), _r("wiki", 5), _r("comment", 7), _r("issue", 2), _r("attachment", 9), _r("issue", 1)]
_SEM = [_r("issue", 2), _r("journal", 7), _r("wiki_page", 6), _r("issue", 3), _r("attachment", 9)]

_CASES: dict[str, dict[str, Any]] = {
    "all": {},
    "wiki": {"scope": "wiki"},
    "comments": {"scope": "comments"},
    "bogus": {"scope": "bogus"},
    "page": {"offset": 2, "limit": 2},
    "pinned": {"query": "HYB-2 login", "pinned": [_r("issue", 2)], "offset": 0, "limit": 3},
    "pinned_page2": {"query": "HYB-2 login", "pinned": [_r("issue", 2)], "offset": 3, "limit": 3},
    "pinned_wiki": {"query": "HYB-2", "pinned": [_r("issue", 2)], "scope": "wiki"},
}

_EXPECTED: dict[str, Any] = {
    "all": (
        [
            ("issue", 2, 0.032018443, 4, 1, "s2"),
            ("issue", 1, 0.031544958, 6, None, "s1"),
            ("attachment", 9, 0.030769231, 5, 5, "s9"),
            ("wiki", 5, 0.016129032, 2, None, "s5"),
            ("journal", 7, 0.016129032, None, 2, "s7"),
            ("comment", 7, 0.015873016, 3, None, "s7"),
            ("wiki_page", 6, 0.015873016, None, 3, "s6"),
            ("issue", 3, 0.015625, None, 4, "s3"),
        ],
        8,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "wiki": (
        [("wiki", 5, 0.016129032, 2, None, "s5"), ("wiki_page", 6, 0.015873016, None, 3, "s6")],
        2,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "comments": (
        [("journal", 7, 0.016129032, None, 2, "s7"), ("comment", 7, 0.015873016, 3, None, "s7")],
        2,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "bogus": ([], 0, {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8}),
    "page": (
        [("attachment", 9, 0.030769231, 5, 5, "s9"), ("wiki", 5, 0.016129032, 2, None, "s5")],
        8,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "pinned": (
        [
            ("issue", 2, 1.0, None, None, "s2"),
            ("issue", 1, 0.031778058, 5, None, "s1"),
            ("attachment", 9, 0.03125, 4, 4, "s9"),
        ],
        8,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "pinned_page2": (
        [
            ("journal", 7, 0.016393443, None, 1, "s7"),
            ("wiki", 5, 0.016129032, 2, None, "s5"),
            ("wiki_page", 6, 0.016129032, None, 2, "s6"),
        ],
        8,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
    "pinned_wiki": (
        [("wiki", 5, 0.016129032, 2, None, "s5"), ("wiki_page", 6, 0.015873016, None, 3, "s6")],
        2,
        {"issues": 3, "wiki": 2, "comments": 2, "attachments": 1, "all": 8},
    ),
}


async def _run(
    scope: str = "all", offset: int = 0, limit: int = 25, pinned: tuple | list = (), query: str = "login"
) -> tuple[list[tuple], int, dict[str, int]]:
    svc = SearchService()
    with (
        patch.object(svc, "search", new=AsyncMock(return_value=(list(_FTS), 0, {}))),
        patch.object(svc, "semantic_search", new=AsyncMock(return_value=(list(_SEM), 0))),
        patch.object(svc, "_identifier_hits", new=AsyncMock(return_value=list(pinned))),
    ):
        results, total, counts = await svc.hybrid_search(
            AsyncMock(), query, AsyncMock(), scope=scope, offset=offset, limit=limit
        )
    rows = [(x.result_type, x.id, round(x.score, 9), x.fts_rank, x.sem_rank, x.snippet) for x in results]
    return rows, total, counts


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_fusion(case: str) -> None:
    assert await _run(**_CASES[case]) == _EXPECTED[case]


async def test_nothing_found_on_either_side() -> None:
    svc = SearchService()
    with (
        patch.object(svc, "search", new=AsyncMock(return_value=([], 0, {}))),
        patch.object(svc, "semantic_search", new=AsyncMock(return_value=([], 0))),
    ):
        assert await svc.hybrid_search(AsyncMock(), "login", AsyncMock()) == (
            [],
            0,
            {"issues": 0, "wiki": 0, "comments": 0, "attachments": 0, "all": 0},
        )
