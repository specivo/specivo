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
# Sanity bound on API keys per user, active and inactive alike. Key lists are
# not paginated and a runaway script should fail rather than add rows forever;
# authentication is an indexed hash lookup, so the count does not slow it down.
API_KEY_MAX_PER_USER = 500

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
# Settled delivery log rows older than this are deleted by the scheduled cleanup,
# at most WEBHOOK_PURGE_BATCH per transaction and WEBHOOK_PURGE_MAX_BATCHES
# transactions per run, so a large backlog is worked off over several runs
# instead of outliving the cleanup lock.
WEBHOOK_DELIVERY_RETENTION_DAYS = 30
WEBHOOK_PURGE_BATCH = 1000
WEBHOOK_PURGE_MAX_BATCHES = 50

# Incoming repository webhooks (see specivo.services.repository_hooks)
# Commit messages are stored up to this many characters.
REPOSITORY_COMMIT_MESSAGE_MAX_CHARS = 10_000
# References taken from one commit (message plus branch name); the rest are ignored,
# so one commit cannot fan out into thousands of journals.
REPOSITORY_MAX_REFS_PER_COMMIT = 20
# Delivery log rows older than this are deleted by the scheduled cleanup.
REPOSITORY_DELIVERY_RETENTION_DAYS = 30
# An accepted delivery not processed after this long is queued again by the sweeper.
REPOSITORY_SWEEP_AGE_SECONDS = 300
# How often the sweeper runs, and how many deliveries it queues per run.
REPOSITORY_SWEEP_INTERVAL_SECONDS = 300
REPOSITORY_SWEEP_BATCH = 200
# Commits handled per database round trip while processing a push.
REPOSITORY_COMMIT_BATCH = 500
# Linked commits listed by specivo_show_issue before "and N more".
REPOSITORY_MCP_COMMIT_LIMIT = 10
# Linked commits and pull requests the issue Development tab and API return.
DEVELOPMENT_COMMIT_LIMIT = 50
DEVELOPMENT_PULL_REQUEST_LIMIT = 100

# Built-in system accounts (``$`` logins) shown with a bundled icon instead of
# initials: login -> icon name, served as /static/img/avatars/<name>.svg.
# The frontend keeps the same map in frontend/js/lib/avatar.js.
SYSTEM_AVATAR_ICONS: dict[str, str] = {"$git": "git"}

# Development activity beyond commits and pull requests (see specivo.services.repository_activity).
# Rows kept per repository integration; past the cap the oldest (by the forge's time) are deleted.
DEVELOPMENT_CI_RUNS_PER_REPOSITORY = 5000
DEVELOPMENT_DEPLOYMENTS_PER_REPOSITORY = 2000
DEVELOPMENT_RELEASES_PER_REPOSITORY = 1000
# Releases an issue is recorded as released in; past the cap the oldest links are deleted.
DEVELOPMENT_RELEASES_PER_ISSUE = 20
# Issues one tag may mark as released by the default-branch rule (the newest first).
DEVELOPMENT_RELEASE_MAX_ISSUES_PER_TAG = 500
# Reviewers kept per pull request; a reviewer past the cap is not recorded.
DEVELOPMENT_REVIEWERS_PER_PULL_REQUEST = 50
# CI runs and deployments of an issue that its summary and lists consider (the newest).
DEVELOPMENT_ACTIVITY_PER_ISSUE = 200
# Timeline paging: default and largest page, and the deepest offset.
DEVELOPMENT_TIMELINE_DEFAULT_LIMIT = 50
DEVELOPMENT_TIMELINE_MAX_LIMIT = 200
DEVELOPMENT_TIMELINE_MAX_OFFSET = 10_000

# Entity link graph (links written in issue descriptions, comments and wiki pages)
# Links stored per source text; anything beyond is dropped with a warning.
ENTITY_LINKS_MAX_PER_SOURCE = 500
# Neighbours a link graph read returns by default, and at most.
ENTITY_LINKS_DEFAULT_LIMIT = 50
ENTITY_LINKS_MAX_LIMIT = 200
# Edges a link graph read returns at most (more are cut, and the read says it was truncated),
# so a densely linked project cannot send the browser thousands of edges to lay out.
ENTITY_LINKS_MAX_EDGES = 1000
# Neighbours of the first hop expanded when depth=2.
ENTITY_LINKS_MAX_EXPANDED = 50
# Linked items listed inline by specivo_show_issue / specivo_read_wiki per direction.
ENTITY_LINKS_INLINE_LIMIT = 20
# How deep a read from a hub root (tag, component, version, sprint, user) may go; items stop at 2.
ENTITY_LINKS_HUB_MAX_DEPTH = 3
# Path finding (specivo_get_links to=): hops at most, and nodes discovered before giving up.
ENTITY_LINKS_PATH_MAX_HOPS = 4
ENTITY_LINKS_PATH_NODE_BUDGET = 500
# Hubs (tags, versions, ...) whose members one hop of path finding reads at most.
ENTITY_LINKS_PATH_MAX_HUBS = 20
# Search with expand=1: the top hits (issues and wiki pages) expanded, and neighbours listed per hit.
SEARCH_EXPAND_HITS = 5
SEARCH_EXPAND_NEIGHBOURS = 10
# Project settings graph_metadata_keys: how many keys at most, and how long each may be.
GRAPH_METADATA_KEYS_MAX = 10
GRAPH_METADATA_KEY_MAX_LENGTH = 64
# Suggested links (``similar`` edges) from document vectors. Neighbours are ranked by raw cosine
# similarity, but a pair is suggested only when its *centred* cosine (both vectors minus the mean
# document vector of the project) reaches the project's threshold: two unrelated items of one project
# score about 0.88 raw (every document vector shares the model's and the project's common direction)
# but about 0 centred, while pairs people link score about 0.34 centred. The threshold is
# settings["similar_links_threshold"] within the bounds below, a centred cosine. A project with fewer
# than SIMILAR_LINKS_MIN_ITEMS vectors gets no suggestions: its mean is mostly its own items, which
# makes centred scores meaningless. Also: the nearest issues, and as many nearest pages, kept per
# item; the extra candidates read so that linked and dismissed pairs do not leave an item short; how
# many are listed inline by show_issue / read_wiki; how many of each type ("Issues", "Wiki pages")
# the web pages list; and the length of the plain-text preview each web row shows.
SIMILAR_LINKS_TOP_K = 10
SIMILAR_LINKS_DEFAULT_THRESHOLD = 0.30
SIMILAR_LINKS_MIN_THRESHOLD = 0.0
SIMILAR_LINKS_MAX_THRESHOLD = 0.95
SIMILAR_LINKS_MIN_ITEMS = 10
SIMILAR_LINKS_CANDIDATE_MARGIN = 20
SIMILAR_LINKS_INLINE_LIMIT = 5
SIMILAR_LINKS_WEB_LIMIT_PER_TYPE = 10
SIMILAR_LINKS_PREVIEW_CHARS = 220
# Concept extraction (``concept`` nodes, ``mentions`` edges) with the project's own AI provider:
# the confidence a mention needs to be shown by default (below it, it is a suggestion; per project:
# settings["concept_confidence_threshold"] within the bounds below), how many changed sources one run
# sends to the model (settings["concept_extraction_run_limit"]), how much of a source is sent, the
# answer size, how many concepts are kept per source and per project, and the request timeout.
CONCEPT_DEFAULT_CONFIDENCE_THRESHOLD = 0.6
CONCEPT_MIN_CONFIDENCE_THRESHOLD = 0.1
CONCEPT_MAX_CONFIDENCE_THRESHOLD = 0.99
CONCEPT_EXTRACTION_DEFAULT_RUN_LIMIT = 50
CONCEPT_EXTRACTION_MAX_RUN_LIMIT = 500
CONCEPT_EXTRACTION_MAX_INPUT_CHARS = 12_000
CONCEPT_EXTRACTION_MAX_OUTPUT_TOKENS = 1_500
CONCEPT_EXTRACTION_MAX_RESPONSE_BYTES = 256 * 1024
CONCEPT_EXTRACTION_MAX_PER_SOURCE = 20
CONCEPT_EXTRACTION_MAX_PER_PROJECT = 5_000
CONCEPT_EXTRACTION_TIMEOUT_SECONDS = 120
# Consecutive failed provider calls after which a run stops (the provider is down or refusing).
CONCEPT_EXTRACTION_MAX_CONSECUTIVE_FAILURES = 3
# Failed calls for the same content after which a source waits until its content changes.
CONCEPT_EXTRACTION_MAX_SOURCE_FAILURES = 3
# Wall-clock limit of one run (its lock lasts longer), and the pause between two manual runs of a project.
CONCEPT_EXTRACTION_MAX_RUN_SECONDS = 5 * 3600
CONCEPT_EXTRACTION_RUN_COOLDOWN_SECONDS = 600

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
