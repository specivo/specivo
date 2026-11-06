"""Minimum similarity a semantic search hit needs, per embedding model.

Semantic search ranks chunks by cosine similarity (``1 - (a <=> b)``) and
would otherwise return the nearest neighbours of any query, however far away.
A cut-off drops the hits that are only "least far" rather than related.

Cosine similarity has no model-independent scale. Models trained with a
contrastive loss at a low temperature, like the E5 family, place every pair of
texts in a narrow, high band (0.7 to 0.9), while other models spread unrelated
pairs around 0.1 to 0.3. So the default is chosen per model family, from scores
measured on real content, and the ``SEARCH_SEMANTIC_MIN_SIMILARITY`` setting
overrides it for whichever model is in use.

Only cosine distance (pgvector's ``<=>``) is used for search, so these values
are cosine similarities in ``[-1, 1]``.
"""

from __future__ import annotations

import re

from specivo.core.config import get_settings

# (model name pattern, minimum cosine similarity)
#
# multilingual-e5-small (the bundled model), measured on tracker-style issues
# and wiki pages with the "query: " / "passage: " prefixes: the document a
# query paraphrases scored 0.81 to 0.89 (median 0.87), while queries unrelated
# to the content (recipes, sports, gibberish, a lone generic word such as
# "trash") reached at most 0.81 against any document, with a median of 0.74.
# 0.82 sits just above that noise ceiling and keeps all but the weakest
# paraphrases; keyword hits are unaffected in hybrid search.
_REGISTRY: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"e5[_-]", re.IGNORECASE), 0.82),
]


def default_min_similarity(model_name: str) -> float | None:
    """The built-in cut-off for *model_name*, or None when none is known.

    A model without a measured default (including the ``mock`` provider used
    by the tests) gets no cut-off: a guessed value would either let noise
    through or hide real matches.
    """
    for pattern, threshold in _REGISTRY:
        if pattern.search(model_name):
            return threshold
    return None


def min_similarity_for(model_name: str) -> float | None:
    """The cut-off semantic search applies for *model_name*.

    ``SEARCH_SEMANTIC_MIN_SIMILARITY`` wins when set; otherwise the model
    family's default. None means every nearest neighbour is kept.
    """
    configured = get_settings().search_semantic_min_similarity
    if configured is not None:
        return configured
    return default_min_similarity(model_name)
