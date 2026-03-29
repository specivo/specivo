"""Tests for embedding prefix registry and intent-based prefix application.

Covers:
- prefix_registry.resolve_prefix() for known model families
- prefix_registry.get_effective_prefix() with DB override semantics
- EmbeddingService._apply_prefix() with intent routing
- EmbeddingService.generate_embedding() end-to-end prefix behavior
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from specivo.services.prefix_registry import get_effective_prefix, resolve_prefix

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_prefix() — model name pattern matching
# ---------------------------------------------------------------------------


class TestResolvePrefix:
    def test_e5_small_gets_query_passage_prefixes(self):
        """multilingual-e5-small -> ("passage: ", "query: ")."""
        assert resolve_prefix("multilingual-e5-small") == ("passage: ", "query: ")

    def test_e5_large_gets_query_passage_prefixes(self):
        """multilingual-e5-large -> ("passage: ", "query: ")."""
        assert resolve_prefix("multilingual-e5-large") == ("passage: ", "query: ")

    def test_e5_base_v2(self):
        """e5-base-v2 -> ("passage: ", "query: ")."""
        assert resolve_prefix("e5-base-v2") == ("passage: ", "query: ")

    def test_e5_instruct_gets_instruction_prefix(self):
        """e5-mistral-7b-instruct -> passage prefix and instruction-based query prefix."""
        passage, query = resolve_prefix("e5-mistral-7b-instruct")
        assert passage == "passage: "
        assert "Instruct:" in query
        assert "Query:" in query

    def test_openai_gets_no_prefix(self):
        """text-embedding-3-small -> ("", "")."""
        assert resolve_prefix("text-embedding-3-small") == ("", "")

    def test_cohere_gets_no_prefix(self):
        """embed-multilingual-v3.0 -> ("", "") (Cohere uses API params, not text prefix)."""
        assert resolve_prefix("embed-multilingual-v3.0") == ("", "")

    def test_voyage_gets_no_prefix(self):
        """voyage-3-large -> ("", "")."""
        assert resolve_prefix("voyage-3-large") == ("", "")

    def test_unknown_model_gets_no_prefix(self):
        """my-custom-model -> ("", "")."""
        assert resolve_prefix("my-custom-model") == ("", "")

    def test_bge_gets_instruction_prefix(self):
        """bge-large-en-v1.5 -> has non-empty query and passage prefixes."""
        passage, query = resolve_prefix("bge-large-en-v1.5")
        assert passage != ""
        assert query != ""
        assert "Represent" in passage
        assert "Represent" in query

    def test_nomic_gets_search_prefixes(self):
        """nomic-embed-text-v1.5 -> ("search_document: ", "search_query: ")."""
        assert resolve_prefix("nomic-embed-text-v1.5") == ("search_document: ", "search_query: ")

    def test_gte_gets_no_prefix(self):
        """gte-large -> ("", "")."""
        assert resolve_prefix("gte-large") == ("", "")

    def test_mock_model_gets_no_prefix(self):
        """mock-384 matches no registry pattern -> ("", "")."""
        assert resolve_prefix("mock-384") == ("", "")

    def test_mock_1536_gets_no_prefix(self):
        """mock-1536 (used in integration tests) matches no pattern -> ("", "")."""
        assert resolve_prefix("mock-1536") == ("", "")


# ---------------------------------------------------------------------------
# get_effective_prefix() — DB override semantics
# ---------------------------------------------------------------------------


class TestGetEffectivePrefix:
    def test_null_uses_auto_detect(self):
        """NULL stored values -> use registry auto-detection."""
        assert get_effective_prefix("multilingual-e5-small", None, None) == (
            "passage: ",
            "query: ",
        )

    def test_empty_string_overrides_auto(self):
        """Empty string stored values -> explicitly no prefix, even for e5."""
        assert get_effective_prefix("multilingual-e5-small", "", "") == ("", "")

    def test_custom_override(self):
        """Custom stored values override registry defaults."""
        assert get_effective_prefix("multilingual-e5-small", "doc: ", "search: ") == (
            "doc: ",
            "search: ",
        )

    def test_partial_override_query_only(self):
        """Override query prefix only, auto-detect passage prefix."""
        assert get_effective_prefix("multilingual-e5-small", None, "find: ") == (
            "passage: ",
            "find: ",
        )

    def test_partial_override_passage_only(self):
        """Override passage prefix only, auto-detect query prefix."""
        assert get_effective_prefix("multilingual-e5-small", "doc: ", None) == (
            "doc: ",
            "query: ",
        )

    def test_null_for_unknown_model(self):
        """NULL for unknown model -> ("", "")."""
        assert get_effective_prefix("my-custom-model", None, None) == ("", "")

    def test_empty_string_for_unknown_model(self):
        """Empty string for unknown model -> ("", "")."""
        assert get_effective_prefix("my-custom-model", "", "") == ("", "")


# ---------------------------------------------------------------------------
# EmbeddingService._apply_prefix() — intent-based prefix application
# ---------------------------------------------------------------------------


def _make_model_stub(
    model_name: str = "mock-1536",
    passage_prefix: str | None = None,
    query_prefix: str | None = None,
    provider: str = "mock",
    dimensions: int = 1536,
) -> MagicMock:
    """Create a mock EmbeddingModel with the specified attributes."""
    model = MagicMock()
    model.model_name = model_name
    model.passage_prefix = passage_prefix
    model.query_prefix = query_prefix
    model.provider = provider
    model.dimensions = dimensions
    return model


class TestApplyPrefix:
    def test_passage_intent_uses_passage_prefix_for_e5(self):
        """E5 model with passage intent -> "passage: " prepended."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="multilingual-e5-small")
        svc = EmbeddingService()
        assert svc._apply_prefix("hello world", model, "passage") == "passage: hello world"

    def test_query_intent_uses_query_prefix_for_e5(self):
        """E5 model with query intent -> "query: " prepended."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="multilingual-e5-small")
        svc = EmbeddingService()
        assert svc._apply_prefix("hello world", model, "query") == "query: hello world"

    def test_no_prefix_for_openai(self):
        """OpenAI model -> no prefix applied regardless of intent."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="text-embedding-3-small")
        svc = EmbeddingService()
        assert svc._apply_prefix("hello world", model, "passage") == "hello world"
        assert svc._apply_prefix("hello world", model, "query") == "hello world"

    def test_no_prefix_for_mock(self):
        """Mock model -> no prefix applied (no registry match)."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="mock-1536")
        svc = EmbeddingService()
        assert svc._apply_prefix("hello world", model, "passage") == "hello world"
        assert svc._apply_prefix("hello world", model, "query") == "hello world"

    def test_db_prefix_overrides_registry(self):
        """DB-stored prefix overrides registry auto-detection."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(
            model_name="multilingual-e5-small",
            query_prefix="custom_q: ",
            passage_prefix="custom_p: ",
        )
        svc = EmbeddingService()
        assert svc._apply_prefix("hello", model, "query") == "custom_q: hello"
        assert svc._apply_prefix("hello", model, "passage") == "custom_p: hello"

    def test_db_empty_string_means_no_prefix(self):
        """DB empty string ("") -> no prefix, even for e5 model."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(
            model_name="multilingual-e5-small",
            query_prefix="",
            passage_prefix="",
        )
        svc = EmbeddingService()
        assert svc._apply_prefix("hello", model, "query") == "hello"
        assert svc._apply_prefix("hello", model, "passage") == "hello"

    def test_db_null_means_auto_detect(self):
        """DB NULL (None) -> auto-detect from registry."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(
            model_name="multilingual-e5-small",
            query_prefix=None,
            passage_prefix=None,
        )
        svc = EmbeddingService()
        assert svc._apply_prefix("hello", model, "query") == "query: hello"
        assert svc._apply_prefix("hello", model, "passage") == "passage: hello"


# ---------------------------------------------------------------------------
# EmbeddingService.generate_embedding() — end-to-end with mock provider
# ---------------------------------------------------------------------------


class TestGenerateEmbeddingWithPrefix:
    @pytest.mark.asyncio
    async def test_e5_model_passage_and_query_produce_different_vectors(self):
        """E5 model: same text with passage vs query intent produces different mock vectors."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="multilingual-e5-small", dimensions=384)
        svc = EmbeddingService()

        vec_passage = await svc.generate_embedding("hello world", model, intent="passage")
        vec_query = await svc.generate_embedding("hello world", model, intent="query")
        vec_raw = svc._mock_embedding("hello world", 384)

        # Passage and query vectors differ (different prefixes)
        assert vec_passage != vec_query
        # Both differ from raw text (prefix was applied)
        assert vec_passage != vec_raw
        assert vec_query != vec_raw

    @pytest.mark.asyncio
    async def test_mock_model_same_vectors_as_before(self):
        """Mock model name (mock-1536) -> no prefix, so vectors are identical to raw."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="mock-1536", dimensions=1536)
        svc = EmbeddingService()

        vec_default = await svc.generate_embedding("test text", model)
        vec_passage = await svc.generate_embedding("test text", model, intent="passage")
        vec_query = await svc.generate_embedding("test text", model, intent="query")
        vec_raw = svc._mock_embedding("test text", 1536)

        # All identical because mock-1536 matches no prefix pattern
        assert vec_default == vec_raw
        assert vec_passage == vec_raw
        assert vec_query == vec_raw

    @pytest.mark.asyncio
    async def test_intent_defaults_to_passage(self):
        """Default intent is 'passage' for backward compatibility."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="multilingual-e5-small", dimensions=384)
        svc = EmbeddingService()

        vec_default = await svc.generate_embedding("hello", model)
        vec_passage = await svc.generate_embedding("hello", model, intent="passage")

        assert vec_default == vec_passage

    @pytest.mark.asyncio
    async def test_openai_model_no_prefix_applied(self):
        """OpenAI model -> no prefix, vectors match raw hash."""
        from specivo.services.embedding_service import EmbeddingService

        model = _make_model_stub(model_name="text-embedding-3-small", dimensions=1536)
        svc = EmbeddingService()

        vec = await svc.generate_embedding("hello", model, intent="query")
        vec_raw = svc._mock_embedding("hello", 1536)

        assert vec == vec_raw
