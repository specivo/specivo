"""Notification message templates -- single source of truth for all notification text.

Use str.format() with named placeholders. Keys:
- issue_key: e.g. "ACME-15"
- issue_subject: e.g. "Fix login bug"
- actor_name: e.g. "John Doe"
- comment_text: journal notes (for comment notifications)
"""

from __future__ import annotations

from specivo.core.i18n import gettext_lazy as _l

# --- Assignment ---
ASSIGNMENT_EMAIL_SUBJECT = _l("[{issue_key}] Assigned to you: {issue_subject}")
ASSIGNMENT_IN_APP_TITLE = _l("[{issue_key}] Assigned to you by {actor_name}")

# --- Issue updated (watcher) ---
ISSUE_UPDATED_EMAIL_SUBJECT = _l("[{issue_key}] Updated: {issue_subject}")
ISSUE_UPDATED_IN_APP_TITLE = _l("[{issue_key}] Updated by {actor_name}")

# --- Comment ---
COMMENT_EMAIL_SUBJECT = _l("[{issue_key}] New comment: {issue_subject}")
COMMENT_IN_APP_TITLE = _l("[{issue_key}] {actor_name} commented")
