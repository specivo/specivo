"""Embedding prefix registry -- maps model names to prefix conventions.

Known model families and their required text prefixes for asymmetric retrieval.
Returns (passage_prefix, query_prefix) tuples.

Priority logic for DB-stored vs auto-detected prefixes:
- NULL (None) in DB -> auto-detect from model name using this registry
- "" (empty string) in DB -> explicitly no prefix
- "some: " in DB -> use that exact prefix
"""

from __future__ import annotations

import re

# (compiled_pattern, passage_prefix, query_prefix)
_REGISTRY: list[tuple[re.Pattern[str], str, str]] = [
    # E5-instruct variants (must precede generic E5 pattern)
    (
        re.compile(r"e5[_-].*instruct", re.IGNORECASE),
        "passage: ",
        "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
    ),
    # E5 family (v1, v2, multilingual) -- NOT e5-instruct
    (
        re.compile(r"e5[_-](?!.*instruct)", re.IGNORECASE),
        "passage: ",
        "query: ",
    ),
    # BGE family
    (
        re.compile(r"\bbge[_-]", re.IGNORECASE),
        "Represent this sentence: ",
        "Represent this sentence for retrieving relevant passages: ",
    ),
    # Nomic family
    (
        re.compile(r"\bnomic[_-]embed", re.IGNORECASE),
        "search_document: ",
        "search_query: ",
    ),
    # GTE family -- no prefix
    (re.compile(r"\bgte[_-]", re.IGNORECASE), "", ""),
    # OpenAI models -- no prefix (handled via API input_type)
    (re.compile(r"text-embedding-", re.IGNORECASE), "", ""),
    # Cohere embed -- no prefix (handled via API input_type)
    (re.compile(r"embed-(?:english|multilingual|v)", re.IGNORECASE), "", ""),
    # Voyage -- no prefix (handled via API input_type)
    (re.compile(r"voyage-", re.IGNORECASE), "", ""),
]


def resolve_prefix(model_name: str) -> tuple[str, str]:
    """Return (passage_prefix, query_prefix) for a model name.

    Scans the registry for the first pattern match. Returns ("", "") if
    no pattern matches (safe default: no prefix).

    Args:
        model_name: The model identifier (e.g. "multilingual-e5-small",
            "text-embedding-3-small", "bge-large-en-v1.5").

    Returns:
        Tuple of (passage_prefix, query_prefix).
    """
    for pattern, passage, query in _REGISTRY:
        if pattern.search(model_name):
            return passage, query
    return "", ""


def get_effective_prefix(
    model_name: str,
    stored_passage: str | None,
    stored_query: str | None,
) -> tuple[str, str]:
    """Return the effective (passage_prefix, query_prefix) for a model.

    Priority:
    1. If stored value is a string (including empty ""), use it as-is.
    2. If stored value is None, auto-detect from model_name.

    Args:
        model_name: Model identifier for auto-detection fallback.
        stored_passage: DB-stored passage prefix (None = auto-detect).
        stored_query: DB-stored query prefix (None = auto-detect).

    Returns:
        Tuple of (effective_passage_prefix, effective_query_prefix).
    """
    auto_passage, auto_query = resolve_prefix(model_name)

    effective_passage = auto_passage if stored_passage is None else stored_passage
    effective_query = auto_query if stored_query is None else stored_query

    return effective_passage, effective_query
