"""Unit tests for EmbeddingService local provider routing (TDD red phase).

Tests that EmbeddingService correctly routes to the local ONNX provider
when model.provider == "local", handles unavailable model files gracefully,
and applies correct E5 prefixes based on indexing vs. search intent.

These tests WILL FAIL until the local provider integration is implemented.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_local_model(
    model_name: str = "multilingual-e5-small",
    dimensions: int = 384,
    provider: str = "local",
    passage_prefix: str | None = None,
    query_prefix: str | None = None,
) -> MagicMock:
    """Build a mock EmbeddingModel configured for the local provider."""
    model = MagicMock()
    model.model_name = model_name
    model.provider = provider
    model.dimensions = dimensions
    model.passage_prefix = passage_prefix
    model.query_prefix = query_prefix
    return model


def _make_available_embedder(dims: int = 384) -> MagicMock:
    """Build a mock LocalEmbedder that is_available() and returns a plausible vector."""
    embedder = MagicMock()
    embedder.is_available.return_value = True
    embedder.encode.return_value = [0.1] * dims
    return embedder


def _make_unavailable_embedder() -> MagicMock:
    """Build a mock LocalEmbedder whose is_available() returns False."""
    embedder = MagicMock()
    embedder.is_available.return_value = False
    return embedder


# ---------------------------------------------------------------------------
# Local provider routing
# ---------------------------------------------------------------------------


class TestLocalProviderRouting:
    @pytest.mark.asyncio
    async def test_generate_embedding_local_provider(self):
        """When model.provider='local', EmbeddingService calls local_embedder.encode()."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model()
        mock_embedder = _make_available_embedder(384)

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            result = await svc.generate_embedding("Hello world", model)

        mock_embedder.encode.assert_called_once()
        assert isinstance(result, list)
        assert len(result) == 384

    @pytest.mark.asyncio
    async def test_generate_embedding_local_provider_passes_text(self):
        """EmbeddingService passes the (possibly prefixed) text to local_embedder.encode()."""
        from specivo.services.embedding_service import EmbeddingService

        # multilingual-e5-small gets "passage: " prefix for passage intent
        model = _make_local_model(model_name="multilingual-e5-small")
        mock_embedder = _make_available_embedder(384)

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            await svc.generate_embedding("Hello world", model, intent="passage")

        call_arg = mock_embedder.encode.call_args[0][0]
        assert call_arg == "passage: Hello world"


# ---------------------------------------------------------------------------
# Graceful degradation — model files missing
# ---------------------------------------------------------------------------


class TestLocalProviderGracefulDegradation:
    @pytest.mark.asyncio
    async def test_generate_embedding_graceful_when_model_unavailable(self, caplog):
        """generate_embedding() returns None (not raises) when is_available() is False."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model()
        mock_embedder = _make_unavailable_embedder()

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            with caplog.at_level(logging.WARNING):
                result = await svc.generate_embedding("Hello world", model)

        assert result is None

    @pytest.mark.asyncio
    async def test_generate_embedding_logs_warning_when_unavailable(self, caplog):
        """A WARNING is emitted when the local embedder is not available."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model()
        mock_embedder = _make_unavailable_embedder()

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            with caplog.at_level(logging.WARNING, logger="specivo.services.embedding_service"):
                await svc.generate_embedding("Hello world", model)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) >= 1


# ---------------------------------------------------------------------------
# Intent-based prefix routing for local provider
# ---------------------------------------------------------------------------


class TestLocalProviderPrefixRouting:
    @pytest.mark.asyncio
    async def test_generate_embedding_with_passage_prefix(self):
        """For indexing (intent='passage'), text is prefixed with 'passage: ' for E5 models."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model(model_name="multilingual-e5-small")
        mock_embedder = _make_available_embedder(384)

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            await svc.generate_embedding("Document text", model, intent="passage")

        encoded_text = mock_embedder.encode.call_args[0][0]
        assert encoded_text.startswith("passage: ")

    @pytest.mark.asyncio
    async def test_generate_embedding_with_query_prefix(self):
        """For searching (intent='query'), text is prefixed with 'query: ' for E5 models."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model(model_name="multilingual-e5-small")
        mock_embedder = _make_available_embedder(384)

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=mock_embedder,
        ):
            svc = EmbeddingService()
            await svc.generate_embedding("search query text", model, intent="query")

        encoded_text = mock_embedder.encode.call_args[0][0]
        assert encoded_text.startswith("query: ")

    @pytest.mark.asyncio
    async def test_generate_embedding_passage_and_query_produce_different_calls(self):
        """Passage and query intent result in different prefixed texts being encoded."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_local_model(model_name="multilingual-e5-small")
        embedder_passage = _make_available_embedder(384)
        embedder_query = _make_available_embedder(384)

        base_text = "information retrieval"

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=embedder_passage,
        ):
            svc = EmbeddingService()
            await svc.generate_embedding(base_text, model, intent="passage")

        with patch(
            "specivo.services.embedding_service.get_local_embedder",
            return_value=embedder_query,
        ):
            await svc.generate_embedding(base_text, model, intent="query")

        passage_arg = embedder_passage.encode.call_args[0][0]
        query_arg = embedder_query.encode.call_args[0][0]

        assert passage_arg != query_arg
        assert "passage" in passage_arg
        assert "query" in query_arg
