"""Unit tests for RRF (Reciprocal Rank Fusion) logic.

Covers:
- Merging two ranked lists via RRF
- k=60 dampening parameter correctness
- Graceful fallback when one result set is empty
"""

from __future__ import annotations

import pytest

from specivo.services.search_service import rrf_fuse

pytestmark = pytest.mark.unit


class TestRRFMerge:
    def test_rrf_merges_two_lists(self):
        """RRF merges FTS ranks [A, B, C] and semantic ranks [C, A, D] correctly."""
        # FTS results: A rank 1, B rank 2, C rank 3
        fts_ids = [10, 20, 30]
        # Semantic results: C rank 1, A rank 2, D rank 3
        sem_ids = [30, 10, 40]

        merged = rrf_fuse(fts_ids, sem_ids, k=60)

        # A (id=10): 1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
        # C (id=30): 1/(60+3) + 1/(60+1) = 0.01587 + 0.01639 = 0.03226
        # B (id=20): 1/(60+2) + 0        = 0.01613
        # D (id=40): 0        + 1/(60+3) = 0.01587
        assert merged[0] == 10  # A: highest combined score
        assert merged[1] == 30  # C: second highest
        assert merged[2] == 20  # B: third
        assert merged[3] == 40  # D: fourth

    def test_rrf_k60(self):
        """Verify RRF scores with k=60 dampening match expected math."""
        fts_ids = [1, 2]
        sem_ids = [2, 3]

        merged = rrf_fuse(fts_ids, sem_ids, k=60)

        # id=1: fts rank 1 -> 1/61, sem rank 0 -> 0. Score = 1/61
        # id=2: fts rank 2 -> 1/62, sem rank 1 -> 1/61. Score = 1/62 + 1/61
        # id=3: fts rank 0 -> 0, sem rank 2 -> 1/62. Score = 1/62
        # id=2 > id=1 > id=3
        assert merged[0] == 2
        assert merged[1] == 1
        assert merged[2] == 3

    def test_rrf_empty_semantic(self):
        """When semantic results are empty, RRF returns FTS order."""
        fts_ids = [5, 10, 15]
        sem_ids: list[int] = []

        merged = rrf_fuse(fts_ids, sem_ids, k=60)

        assert merged == [5, 10, 15]

    def test_rrf_empty_fts(self):
        """When FTS results are empty, RRF returns semantic order."""
        fts_ids: list[int] = []
        sem_ids = [100, 200, 300]

        merged = rrf_fuse(fts_ids, sem_ids, k=60)

        assert merged == [100, 200, 300]

    def test_rrf_both_empty(self):
        """When both are empty, RRF returns empty list."""
        assert rrf_fuse([], [], k=60) == []

    def test_rrf_identical_lists(self):
        """When both lists have the same items, order is preserved (rank 1 in both stays first)."""
        ids = [1, 2, 3]
        merged = rrf_fuse(ids, ids, k=60)
        assert merged == [1, 2, 3]
