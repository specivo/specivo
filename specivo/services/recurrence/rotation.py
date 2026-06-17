"""Assignee rotation strategies for recurring patterns.

Pure functions with no side effects: given a rotation configuration and the
current rotation index, they decide who the next occurrence is assigned to and
return the advanced index. The service layer persists the new index back onto
the pattern.

Only ``round_robin`` is implemented today. Unknown strategies fall back to
round-robin rather than raising, so a misconfigured (or future) strategy never
breaks generation.

Roster contract (enforced by the service layer, NOT here)
---------------------------------------------------------
This module is deliberately DB-free, so it cannot inspect project membership or
roles. It treats ``user_ids`` as an already-vetted, already-ordered roster and
simply cycles through it. The service layer that builds ``assignee_rotation``
MUST, before persisting:

- include only users who are members of the pattern's own project (drop
  non-members / departed users), and
- order the roster managers/admins first by default.

If that list is empty after filtering, rotation is treated as disabled here
(``user_ids`` empty -> ``(None, index)``).
"""

from __future__ import annotations


def next_assignee(rotation_cfg: dict | None, index: int) -> tuple[int | None, int]:
    """Pick the next assignee from a rotation config.

    Args:
        rotation_cfg: e.g. ``{"user_ids": [3, 7, 9], "strategy": "round_robin"}``.
            ``None``, empty, or missing/empty ``user_ids`` disables rotation.
        index: the current rotation index (monotonically increasing counter).

    Returns:
        A ``(user_id, next_index)`` tuple. When rotation is disabled the result
        is ``(None, index)`` — the caller keeps whatever assignee the template
        already specifies and the index is left untouched. Otherwise the chosen
        user is ``user_ids[index % len(user_ids)]`` and the index is advanced
        by one (the counter grows without bound; only its modulo matters).
    """
    if not rotation_cfg:
        return None, index

    user_ids = rotation_cfg.get("user_ids")
    if not user_ids:
        return None, index

    # Only round_robin exists for now; unknown strategies default to it instead
    # of raising, so generation never breaks on a misconfigured strategy.
    chosen = user_ids[index % len(user_ids)]
    return chosen, index + 1
