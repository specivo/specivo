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
# Deepest result offset search accepts. Each keyword-search part ranks every
# row up to the end of the requested page, so an unbounded offset would let a
# client make the database rank and sort arbitrarily many rows per request.
MAX_SEARCH_OFFSET = 1000
# Results the MCP search tool returns at most.
MCP_SEARCH_MAX_LIMIT = 50
SEARCH_FTS_HEADLINE_OPTIONS = "MaxWords=35, MinWords=15, StartSel=<mark>, StopSel=</mark>"
# Delay before a queued embedding task runs, so a burst of edits to one source
# is embedded once, from its latest text.
SEARCH_EMBED_DEBOUNCE_SECONDS = 5
# Vector size covered by the HNSW index on chunk_embeddings (migration 0038):
# the local multilingual-e5-small model. Embeddings of another size are
# searched exactly, without the index.
SEMANTIC_INDEX_DIMENSIONS = 384
# Semantic search reads candidates from the index in widening rounds (times
# this factor) until enough of them are visible to the user. Past this many
# candidates it ranks every embedding exactly instead, so a user who can see
# only a sliver of the instance never loses a match that ranks behind hidden ones.
SEMANTIC_CANDIDATE_GROWTH = 4
SEMANTIC_MAX_INDEX_CANDIDATES = 10_000
# pgvector release that added iterative index scans (hnsw.iterative_scan), which
# let an index scan return more rows than hnsw.ef_search. Older releases are
# searched exactly.
PGVECTOR_ITERATIVE_SCAN_VERSION = (0, 8)

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

# Outgoing webhooks
# Response body kept on a delivery for the log (an excerpt, in characters).
WEBHOOK_RESPONSE_MAX_BYTES = 4096
# Attempts per delivery before it is marked failed.
WEBHOOK_MAX_ATTEMPTS = 8
# Whole send-and-read budget for one attempt, on top of httpx's per-phase timeout,
# so a receiver trickling bytes cannot hold a worker and the row lock.
WEBHOOK_ATTEMPT_DEADLINE_SECONDS = 60
# Retry n waits BASE * 2**(n-1) seconds, capped at MAX, plus up to 25% jitter.
WEBHOOK_BACKOFF_BASE_SECONDS = 30
WEBHOOK_BACKOFF_MAX_SECONDS = 3600
# A queued delivery is left to its task for this long before the sweeper queues it again.
WEBHOOK_LEASE_SECONDS = 300
# How often the sweeper looks for due pending deliveries, and how many it queues per run.
WEBHOOK_SWEEP_INTERVAL_SECONDS = 60
WEBHOOK_SWEEP_BATCH = 200
# How long a project's active-webhook list is cached in Redis.
WEBHOOK_SUBSCRIPTION_CACHE_SECONDS = 60

# Celery tasks
CELERY_MAX_RETRIES = 3
CELERY_RETRY_DELAY_EMAIL = 60
CELERY_RETRY_DELAY_EMBEDDING = 30
CELERY_RETRY_DELAY_LINK_GRAPH = 15
CELERY_RETRY_DELAY_RECURRING = 120

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
