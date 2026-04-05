"""Named constants used across the application.

Single source of truth -- import from here, never duplicate values.
"""

# Authentication
API_KEY_PREFIX = "spv_"
API_KEY_MIN_LENGTH = 20  # fast reject for obviously invalid keys (prefix + some entropy)
MCP_PENDING_CLIENT_ID = "pending-auth"  # placeholder until tool-level auth resolves real user
JWT_ALGORITHM = "HS256"
API_KEY_ENTROPY_BYTES = 32
REFRESH_TOKEN_ENTROPY_BYTES = 32
CREDENTIAL_TOKEN_ENTROPY_BYTES = 48

# Pagination
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 200

# Search
RRF_K = 60
SEARCH_SNIPPET_MAX_CHARS = 200
SEARCH_HYBRID_PREFETCH_LIMIT = 100
SEARCH_FTS_HEADLINE_OPTIONS = "MaxWords=35, MinWords=15, StartSel=<mark>, StopSel=</mark>"

# Hierarchy
MAX_HIERARCHY_DEPTH = 10  # Issue nesting (parent/child issues)
MAX_PROJECT_DEPTH = 5  # Project nesting (subprojects)

# Avatar
DEFAULT_AVATAR_PALETTE = [
    "#c49a3c",
    "#5B8C5A",
    "#7B68AE",
    "#E07B6C",
    "#4A90B8",
    "#D4915A",
    "#8B7FC7",
    "#3D9B8F",
    "#B85C4A",
    "#6B7B8D",
]

# Project colors — default palette for project card borders
DEFAULT_PROJECT_COLORS = [
    "#c49a3c",  # gold
    "#7C3AED",  # violet
    "#D97706",  # amber
    "#2563EB",  # blue
    "#E11D48",  # rose
    "#0D9488",  # teal
    "#4F46E5",  # indigo
    "#059669",  # emerald
    "#64748B",  # slate
    "#EA580C",  # orange
]

# Webhooks
WEBHOOK_RESPONSE_MAX_BYTES = 4096

# Celery tasks
CELERY_MAX_RETRIES = 3
CELERY_RETRY_DELAY_EMAIL = 60
CELERY_RETRY_DELAY_WEBHOOK = 30
CELERY_RETRY_DELAY_EMBEDDING = 30
CELERY_RETRY_DELAY_LINK_GRAPH = 15

# Activity feed
ACTIVITY_DEFAULT_PER_PAGE = 25
ACTIVITY_PER_PAGE_OPTIONS = [25, 50, 100]

# Emoji reactions
REACTION_EMOJI = {
    "thumbs_up": "\U0001f44d",
    "thumbs_down": "\U0001f44e",
    "heart": "\u2764\ufe0f",
    "rocket": "\U0001f680",
    "eyes": "\U0001f440",
    "tada": "\U0001f389",
}
