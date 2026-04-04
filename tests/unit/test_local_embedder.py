"""Unit tests for specivo.services.local_embedder (TDD red phase).

Tests the expected interface of the LocalEmbedder class and get_local_embedder
factory, which provides ONNX-based local embedding inference using the
multilingual-e5-small model stored in data/models/.

These tests WILL FAIL until the module is implemented.
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# is_available() — filesystem detection
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_is_available_false_when_model_missing(self, tmp_path: Path):
        """is_available() returns False when neither .onnx nor tokenizer.json exist."""
        from specivo.services.local_embedder import LocalEmbedder

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")
        assert embedder.is_available() is False

    def test_is_available_false_when_only_onnx_present(self, tmp_path: Path):
        """is_available() returns False when .onnx exists but tokenizer.json is missing."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"fake onnx data")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")
        assert embedder.is_available() is False

    def test_is_available_false_when_only_tokenizer_present(self, tmp_path: Path):
        """is_available() returns False when tokenizer.json exists but .onnx is missing."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")
        assert embedder.is_available() is False

    def test_is_available_true_when_model_present(self, tmp_path: Path):
        """is_available() returns True when both .onnx and tokenizer.json exist."""
        from specivo.services.local_embedder import LocalEmbedder

        # is_available() checks model_dir/{model_name}.onnx and model_dir/tokenizer.json
        (tmp_path / "multilingual-e5-small.onnx").write_bytes(b"fake onnx data")
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")
        assert embedder.is_available() is True


# ---------------------------------------------------------------------------
# encode() — inference and output shape
# ---------------------------------------------------------------------------


def _make_mock_session(dims: int = 384) -> MagicMock:
    """Build a minimal onnxruntime.InferenceSession mock."""
    mock_session = MagicMock()
    # Run returns [token_embeddings: shape (1, seq_len, dims)]
    mock_session.run.return_value = [np.random.randn(1, 10, dims).astype(np.float32)]
    return mock_session


def _make_mock_tokenizer() -> MagicMock:
    """Build a minimal tokenizers.Tokenizer mock."""
    mock_tokenizer = MagicMock()
    mock_encoded = MagicMock()
    mock_encoded.ids = [101, 2023, 102]
    mock_encoded.attention_mask = [1, 1, 1]
    mock_tokenizer.encode.return_value = mock_encoded
    return mock_tokenizer


class TestEncode:
    def test_encode_returns_list_of_floats(self, tmp_path: Path):
        """encode() returns a list[float] with length equal to model dimensions (384)."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"fake onnx data")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")

        with (
            patch("onnxruntime.InferenceSession", return_value=_make_mock_session(384)),
            patch("tokenizers.Tokenizer.from_file", return_value=_make_mock_tokenizer()),
        ):
            result = embedder.encode("Hello world")

        assert isinstance(result, list)
        assert len(result) == 384
        assert all(isinstance(v, float) for v in result)

    def test_encode_returns_normalized_vector(self, tmp_path: Path):
        """encode() returns a unit-normalized vector (L2 norm ≈ 1.0)."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"fake onnx data")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")

        with (
            patch("onnxruntime.InferenceSession", return_value=_make_mock_session(384)),
            patch("tokenizers.Tokenizer.from_file", return_value=_make_mock_tokenizer()),
        ):
            result = embedder.encode("Hello world")

        norm = math.sqrt(sum(v * v for v in result))
        assert abs(norm - 1.0) < 1e-5, f"Expected unit vector, got L2 norm={norm}"

    def test_encode_with_prefix(self, tmp_path: Path):
        """encode() passes text to the tokenizer with the given prefix applied."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"fake onnx data")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")
        mock_tokenizer = _make_mock_tokenizer()

        with (
            patch("onnxruntime.InferenceSession", return_value=_make_mock_session(384)),
            patch("tokenizers.Tokenizer.from_file", return_value=mock_tokenizer),
        ):
            embedder.encode("passage: Hello world")

        # The tokenizer must have been called with the prefixed text
        mock_tokenizer.encode.assert_called_once()
        call_arg = mock_tokenizer.encode.call_args[0][0]
        assert call_arg == "passage: Hello world"

    def test_encode_dimensions_match_model(self, tmp_path: Path):
        """encode() output length matches the dimension parameter passed to the session."""
        from specivo.services.local_embedder import LocalEmbedder

        model_dir = tmp_path / "multilingual-e5-small"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"fake onnx data")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

        embedder = LocalEmbedder(model_dir=tmp_path, model_name="multilingual-e5-small")

        # Simulate a model that outputs 768-dim vectors
        with (
            patch("onnxruntime.InferenceSession", return_value=_make_mock_session(768)),
            patch("tokenizers.Tokenizer.from_file", return_value=_make_mock_tokenizer()),
        ):
            result = embedder.encode("Test text")

        assert len(result) == 768


# ---------------------------------------------------------------------------
# get_local_embedder() — module-level cache
# ---------------------------------------------------------------------------


class TestGetLocalEmbedder:
    def test_get_local_embedder_caches_instance(self):
        """Calling get_local_embedder() twice with the same name returns the same instance."""
        from specivo.services.local_embedder import get_local_embedder

        # Clear any cached state between tests
        try:
            from specivo.services import local_embedder as _mod

            _mod._embedder_cache.clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass  # Cache may be structured differently — the equality check will verify

        inst1 = get_local_embedder("multilingual-e5-small")
        inst2 = get_local_embedder("multilingual-e5-small")

        assert inst1 is inst2

    def test_get_local_embedder_different_names_different_instances(self):
        """Calling get_local_embedder() with different model names returns different instances."""
        from specivo.services.local_embedder import get_local_embedder

        try:
            from specivo.services import local_embedder as _mod

            _mod._embedder_cache.clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass

        inst_a = get_local_embedder("multilingual-e5-small")
        inst_b = get_local_embedder("multilingual-e5-large")

        assert inst_a is not inst_b

    def test_get_local_embedder_returns_local_embedder_instance(self):
        """get_local_embedder() returns a LocalEmbedder instance."""
        from specivo.services.local_embedder import LocalEmbedder, get_local_embedder

        instance = get_local_embedder("multilingual-e5-small")
        assert isinstance(instance, LocalEmbedder)
