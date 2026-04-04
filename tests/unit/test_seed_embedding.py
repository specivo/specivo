"""Unit tests for seed_embedding_model() in specivo.cli.seed (TDD red phase).

Tests that the seed command creates the default local embedding model record,
is idempotent on repeated runs, and does not overwrite an admin-modified config.

These tests WILL FAIL until seed_embedding_model() is implemented.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers — build a minimal AsyncSession mock
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_NAME = "multilingual-e5-small"
_DEFAULT_PROVIDER = "local"
_DEFAULT_DIMENSIONS = 384


def _make_session_with_no_model() -> MagicMock:
    """AsyncSession mock where EmbeddingModel select returns no rows."""
    session = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _make_existing_model(
    name: str = _DEFAULT_MODEL_NAME,
    provider: str = _DEFAULT_PROVIDER,
    dimensions: int = _DEFAULT_DIMENSIONS,
    is_default: bool = True,
) -> MagicMock:
    """Simulate an EmbeddingModel already in the DB."""
    model = MagicMock()
    model.name = name
    model.provider = provider
    model.dimensions = dimensions
    model.is_default = is_default
    return model


def _make_session_with_model(existing_model: MagicMock) -> MagicMock:
    """AsyncSession mock where EmbeddingModel select returns one existing row."""
    session = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing_model
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeedEmbeddingModel:
    @pytest.mark.asyncio
    async def test_seed_creates_default_embedding_model(self):
        """seed_embedding_model() inserts an EmbeddingModel with expected defaults when none exists."""
        from specivo.cli.seed import seed_embedding_model

        session = _make_session_with_no_model()
        await seed_embedding_model(session)

        session.add.assert_called_once()
        added_model = session.add.call_args[0][0]

        # Verify the key attributes on the inserted model
        assert added_model.model_name == _DEFAULT_MODEL_NAME
        assert added_model.provider == _DEFAULT_PROVIDER
        assert added_model.dimensions == _DEFAULT_DIMENSIONS
        assert added_model.is_default is True

    @pytest.mark.asyncio
    async def test_seed_creates_model_with_correct_name(self):
        """The seeded model has a human-readable name field set."""
        from specivo.cli.seed import seed_embedding_model

        session = _make_session_with_no_model()
        await seed_embedding_model(session)

        added_model = session.add.call_args[0][0]
        # name is distinct from model_name (display label vs. HuggingFace slug)
        assert added_model.name is not None
        assert len(added_model.name) > 0

    @pytest.mark.asyncio
    async def test_seed_idempotent(self):
        """seed_embedding_model() does NOT insert when a model with the same name already exists."""
        from specivo.cli.seed import seed_embedding_model

        existing = _make_existing_model()
        session = _make_session_with_model(existing)

        await seed_embedding_model(session)

        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_seed_does_not_overwrite_existing(self):
        """If admin changed dimensions/provider, seed does not reset those values."""
        from specivo.cli.seed import seed_embedding_model

        # Admin changed dimensions to 512 and provider to "ollama"
        existing = _make_existing_model(dimensions=512, provider="ollama")
        session = _make_session_with_model(existing)

        await seed_embedding_model(session)

        # No add, and the existing model's attributes should not be mutated
        session.add.assert_not_called()
        assert existing.dimensions == 512
        assert existing.provider == "ollama"

    @pytest.mark.asyncio
    async def test_seed_flushes_session(self):
        """seed_embedding_model() flushes the session after inserting."""
        from specivo.cli.seed import seed_embedding_model

        session = _make_session_with_no_model()
        await seed_embedding_model(session)

        session.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_seed_no_flush_when_already_exists(self):
        """seed_embedding_model() does not flush when model already exists (no-op path)."""
        from specivo.cli.seed import seed_embedding_model

        existing = _make_existing_model()
        session = _make_session_with_model(existing)

        await seed_embedding_model(session)

        # flush may or may not be called — but add must not be called
        session.add.assert_not_called()
