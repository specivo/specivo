"""Local ONNX-based embedding inference.

Provides the ``LocalEmbedder`` class for generating embeddings using
ONNX Runtime and HuggingFace tokenizers, with the multilingual-e5-small
model stored in ``data/models/``.

The ``get_local_embedder()`` factory caches instances per model name so
that tokenizer and ONNX session are loaded only once.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_DIR = Path("data/models")

# Module-level cache: model_name -> LocalEmbedder
_embedder_cache: dict[str, LocalEmbedder] = {}


class LocalEmbedder:
    """ONNX-based local embedding model.

    Args:
        model_dir: Root directory containing model subdirectories.
        model_name: Name of the model (subdirectory under model_dir).
    """

    def __init__(self, model_dir: Path = _DEFAULT_MODEL_DIR, model_name: str = "multilingual-e5-small") -> None:
        self._model_dir = model_dir
        self._model_name = model_name

    def is_available(self) -> bool:
        """Check whether both the ONNX model and tokenizer files exist."""
        onnx_path = self._model_dir / f"{self._model_name}.onnx"
        tokenizer_path = self._model_dir / "tokenizer.json"
        return onnx_path.exists() and tokenizer_path.exists()

    def encode(self, text: str) -> list[float]:
        """Encode text into an embedding vector.

        Loads the ONNX session and tokenizer on first call, tokenizes the
        input, runs inference, mean-pools the token embeddings, and
        L2-normalizes the result.

        Args:
            text: Input text (prefix should already be applied by caller).

        Returns:
            A unit-normalized list of floats.
        """
        import numpy as np
        import onnxruntime
        import tokenizers

        # Load tokenizer and session
        tokenizer_path = str(self._model_dir / "tokenizer.json")
        onnx_path = str(self._model_dir / f"{self._model_name}.onnx")

        tokenizer = tokenizers.Tokenizer.from_file(tokenizer_path)
        session = onnxruntime.InferenceSession(onnx_path)

        # Tokenize
        encoded = tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

        # token_type_ids — zeros for single-sequence input
        token_type_ids = np.zeros_like(input_ids)

        # Run inference
        outputs = session.run(
            None,
            {"input_ids": input_ids, "attention_mask": attention_mask, "token_type_ids": token_type_ids},
        )

        # outputs[0] shape: (1, seq_len, dims)
        token_embeddings = outputs[0]

        # Mean pooling over the sequence dimension
        mean_pooled = np.mean(token_embeddings, axis=1)  # shape: (1, dims)

        # L2 normalize
        vector = mean_pooled[0]
        magnitude = math.sqrt(float(np.sum(vector * vector)))
        if magnitude > 0:
            vector = vector / magnitude

        return vector.tolist()


def get_local_embedder(model_name: str = "multilingual-e5-small") -> LocalEmbedder:
    """Get or create a cached LocalEmbedder instance.

    Args:
        model_name: The model name (subdirectory under data/models/).

    Returns:
        A cached LocalEmbedder instance.
    """
    if model_name not in _embedder_cache:
        _embedder_cache[model_name] = LocalEmbedder(model_name=model_name)
    return _embedder_cache[model_name]
