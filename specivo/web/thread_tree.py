"""Build a 2-level thread tree from a flat list of journals."""

from __future__ import annotations

from typing import Any


def build_thread_tree(journals: list) -> list[dict[str, Any]]:
    """Group journals into a 2-level thread tree.

    Top-level journals (reply_to_id is None) are roots.
    Replies are nested under their parent. Reply-to-reply
    is flattened to the same level as first-level replies
    (2-level max). Orphan replies (parent not in list)
    become roots.

    Returns list of {"journal": Journal, "replies": [{"journal": ...}]}
    """
    by_id: dict[int, Any] = {j.id: j for j in journals}
    roots: list[dict] = []
    replies_by_parent: dict[int, list[dict]] = {}

    for j in journals:
        if j.reply_to_id is None:
            roots.append({"journal": j, "replies": []})
        else:
            parent_id = j.reply_to_id
            # If parent is itself a reply, flatten to grandparent
            parent = by_id.get(parent_id)
            if parent and parent.reply_to_id is not None:
                parent_id = parent.reply_to_id
            # If parent not in list, treat as root
            if parent_id not in by_id or by_id[parent_id].reply_to_id is not None:
                roots.append({"journal": j, "replies": []})
            else:
                replies_by_parent.setdefault(parent_id, []).append({"journal": j})

    for root in roots:
        root["replies"] = replies_by_parent.get(root["journal"].id, [])

    return roots
