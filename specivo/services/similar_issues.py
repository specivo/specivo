"""Settings, limits and input handling for similar-issue suggestions.

While someone types the subject of a new issue, the form asks
``SimilarIssueService`` for issues of the same project whose subject
resembles it. This module holds the pieces that need no database: the setting
keys and how their values are read, the query normalisation, and the tuning
constants. The constants are deliberately not settings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Instance switch (``settings`` table). Off only when the stored value is
# exactly "false": a missing row means on, the opposite default to
# ``anonymous_access_enabled``.
SIMILAR_ISSUES_SETTING_KEY = "similar_issues_enabled"

# Per-project switch, a key of ``projects.settings``. Off only for ``False``.
SIMILAR_ISSUES_PROJECT_KEY = "similar_issues"

# ``similarity(subject, q)`` a suggestion must exceed.
SIMILAR_ISSUES_MIN_SCORE = 0.2
# Full-text candidates, best ``ts_rank`` first, that the similarity rerank sees.
CANDIDATE_LIMIT = 50
# Issues, newest first, scored for a title in a script written without spaces.
RECENCY_SCAN_LIMIT = 2000
# Bound on the statement that finds and ranks suggestions. A request that hits
# it answers with no suggestions rather than an error.
STATEMENT_TIMEOUT_MS = 500


def instance_setting_enabled(value: str | None) -> bool:
    """Whether the instance switch's stored *value* leaves the feature on."""
    return value != "false"


def project_setting_enabled(project_settings: Mapping[str, Any] | None) -> bool:
    """Whether a project's settings leave the feature on: off only for an explicit ``False``.

    A missing or unusable value means on, as ``BranchNameSettings.from_project_settings``
    falls back to its defaults.
    """
    return (project_settings or {}).get(SIMILAR_ISSUES_PROJECT_KEY) is not False


def normalize_query(q: str) -> str:
    """Return *q* trimmed, with NUL replaced by a space.

    PostgreSQL rejects a NUL byte in text (SQLSTATE 22021), which would turn a
    stray character in a title into a server error.
    """
    return q.replace("\x00", " ").strip()


# Scripts written without spaces between words. PostgreSQL has no word
# segmentation for them, so a whole title becomes one lexeme and an OR of
# lexemes only matches an identical run of characters; ``similarity()`` still
# scores them. Korean is written with spaces and is not listed.
_UNSEGMENTED_SCRIPT_RE = re.compile(
    "["
    "\u0e00-\u0e7f"  # Thai
    "\u0e80-\u0eff"  # Lao
    "\u1000-\u109f\ua9e0-\ua9ff\uaa60-\uaa7f"  # Myanmar and its extensions
    "\u1780-\u17ff\u19e0-\u19ff"  # Khmer and Khmer symbols
    "\u3040-\u309f"  # Hiragana
    "\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9f"  # Katakana, its extension and halfwidth forms
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # Han: extension A, unified, compatibility
    "\U00020000-\U0003134f"  # Han: supplementary planes
    "]"
)


def uses_recency_scan(q: str) -> bool:
    """Whether *q* holds Han, Hiragana, Katakana, Thai, Lao, Myanmar or Khmer characters.

    Such a title skips full-text search: the newest ``RECENCY_SCAN_LIMIT``
    issues of the project are scored by ``similarity()`` instead.
    """
    return _UNSEGMENTED_SCRIPT_RE.search(q) is not None
