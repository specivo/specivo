"""Notification message templates -- single source of truth for all notification text.

Use str.format() with named placeholders. Keys:
- issue_key: e.g. "ACME-15"
- issue_subject: e.g. "Fix login bug"
- actor_name: e.g. "John Doe"
- comment_text: journal notes (for comment notifications)
"""

from __future__ import annotations

from specivo.core.i18n import gettext_lazy as _l

# Issue email subjects are always "[{issue_key}] {issue_subject}" (see
# specivo.core.mail_headers), so a mail client threads every mail about one
# issue together; the *_EMAIL_LEAD line says what happened, first in the body.

# --- Assignment ---
ASSIGNMENT_EMAIL_LEAD = _l("Assigned to you by {actor_name}")
ASSIGNMENT_IN_APP_TITLE = _l("[{issue_key}] Assigned to you by {actor_name}")

# --- Issue updated (watcher) ---
ISSUE_UPDATED_EMAIL_LEAD = _l("Updated by {actor_name}")
ISSUE_UPDATED_IN_APP_TITLE = _l("[{issue_key}] Updated by {actor_name}")

# --- Comment ---
COMMENT_EMAIL_LEAD = _l("{actor_name} commented")
COMMENT_IN_APP_TITLE = _l("[{issue_key}] {actor_name} commented")

# --- Password reset ---
PASSWORD_RESET_EMAIL_SUBJECT = _l("Password reset request")
