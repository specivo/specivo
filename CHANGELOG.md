# Changelog

All notable changes to Specivo are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.9] - 2026-04-19

### Added
- **Dashboard v2** — project stats, recent releases, wiki activity, paginated issues with sprints and agent activity feed
- **Sprint detail page** — sprint metrics, rich issue list layout
- **Autocomplete selects** — version and sprint sidebar selects with search, closed versions and completed sprints remain selectable
- **MCP session persistence** — sessions stored in Redis with sliding TTL so clients survive API restarts
- **MCP tools** — `specivo_list_comments`, comment count on `show_issue`, `get` op for `specivo_metadata`, search defaults to hybrid mode with configurable `mode` param
- **Admin MCP config** — Codex CLI option added to MCP configuration card
- **Project key length** — raised from 12 to 128 characters

### Fixed
- Activity log shows sprint and version names instead of raw IDs
- Metadata changes render as per-key diffs in activity log
- CSRF HMAC secret derived from `settings.secret_key` instead of separate value
- Silent JWT refresh on API endpoints (was only on page routes)
- Styled 404 page for routes unmatched by the router
- Codex MCP config uses native streamable HTTP

### Security
- CSRF secret derivation hardened — tied to application secret key

## [0.1.8] - 2026-04-12

### Added
- **Sprint management** — backlog page, sprint board (kanban), sprint edit with lifecycle controls (start/complete), velocity chart and burndown, sprint analytics page, sprint history table
- **Custom metadata schemas** — define structured fields per project or issue type, preset library with builtin/custom presets, dynamic metadata on issue forms and detail page, admin interface for schema lifecycle
- **Kanban board view** — issues list page with board/list toggle, segmented status progress bar, per-column card limits, drag-friendly layout
- **Version management UI** — version detail page with linked issues and progress, version selector in issue sidebar, full CRUD in project settings
- **Wiki soft-delete** — trash with restore, auto-purge, search index cleanup on delete
- **Issue relations** — add/list/remove relations (9 types), autocomplete for related issue input, structured display on detail page
- **MCP tool expansion** — 38 tools total: wiki section ops (read/replace/append), wiki delete, wiki metadata, issue relations, sprint CRUD, version management, metadata ops, attachment upload, `whoami` for agent self-identification
- **MCP self-documentation** — `specivo_setup_guide()` returns full agent configuration, enriched tool descriptions
- **Short issue URLs** — `/issue/KEY-123/` redirects instead of `/projects/KEY/issues/KEY-123/`
- **Wiki enhancements** — paste-to-embed images, drag-drop file upload, `[[wikilinks]]` in issue descriptions and comments, auto-linked issue references (KEY-123)
- **Sprint sidebar link** — "Current Sprint" appears when an active sprint exists
- **Project parent selector** — set parent project in project settings General tab
- **Member role management** — change member roles from project settings page
- **Separate `manage_sprints` permission** — independent from `manage_versions`

### Changed
- Issue detail SQL queries reduced from 39 to ~15
- Project access control: membership-based visibility with public/private project support
- All API endpoints enforce project access checks
- Project key max length increased from 10 to 12 characters
- Timestamps display in user's timezone instead of UTC
- Sidebar icons differentiated (were duplicate 4-square grid)
- Sidebar is now `position: fixed` — stays viewport-height on long pages
- Wide tables in wiki and issues scroll horizontally instead of being clipped
- FTS underscore tokenization fixed — `wiki_page` and `wiki page` both searchable
- Status categories (backlog/active/done/closed) replace boolean `is_closed` for roadmap progress
- Search results use enum-based result types instead of hardcoded strings

### Fixed
- Sidebar spacer stretched to ~10000px on wiki pages with long content
- Wide code blocks in descriptions pushed sidebar off-screen
- Kanban columns stretched full page width and pushed page horizontally
- Large inline images broke page layout — overflow container width
- Wiki inline images not rendered (bare filename src not resolved to attachment URL)
- Project list showed wrong open/closed counts (missing "done" category)
- Project list avatar initials hard to read on dark backgrounds
- Member autocomplete popup style broken in project settings
- Description diff missing — no initial version stored on issue creation
- Race condition: project not visible after creation page reload
- MCP `update_issue` silently ignored `done_ratio` parameter
- Search result wiki links missing trailing slash (caused proxy redirect)
- Markdown tables had no cell padding in descriptions and comments
- SQL wildcard injection in autocomplete endpoint (% and _ not escaped)
- API error pages returned raw JSON instead of styled error templates

### Security
- **CSRF protection** — double-submit cookie pattern across all forms, fetch, and htmx requests
- XSS fix in search snippets — HTML-escaped content with preserved `<mark>` highlight tags
- Content-Disposition header fix for attachment downloads (RFC 5987 compliance)
- Authorization check added to sprint edit page
- Wiki restore cascade fix for soft-delete `deleted_by_id` tracking
- Notification text rendering uses escaped `title` instead of `text | safe`
- Audit logging for password reset operations
- SHA-256 content hash stored for attachments
- Project-wide attachment filenames with auto-rename on collision

### Performance
- Composite indexes on query-heavy tables
- Permission checks cached per-request
- Project lookup with alias fallback uses single LEFT JOIN
- Wiki chunking no longer splits inside fenced code blocks

### Tests
- 1449 integration tests + 266 E2E tests passing
- E2E test infrastructure: ConsoleErrorTracker, page objects, page-centric organization
- Visual regression snapshots across 4 viewports (mobile, tablet, narrow, desktop)

## [0.1.7] - 2026-04-05

### Added
- **MCP HTTP transport** — Streamable HTTP + SSE endpoints replace stdio, enabling remote AI agent connectivity
- **MCP tool expansion** — 7 new tools: create_wiki, list_lookups, list_members, log_time, list/create/update versions (18 tools total)
- **MCP permission enforcement** — every tool call checks project-scoped permissions via `check_permission`
- **MCP audit logging** — granular event types per tool (ISSUE_CREATED, WIKI_UPDATED, etc.) with `source: "mcp"` tag
- **Admin user detail page** — API key management (create/list/revoke), service account support
- **Configurable password policy** — `PASSWORD_MIN_LENGTH` setting (default 8, minimum 6)

### Changed
- MCP transport-level auth is now format-only (no DB hit); real auth happens per-tool call for instant key revocation
- API key authentication uses single JOIN query instead of two sequential queries
- Permission checks cached per-request to avoid repeated 3-table JOINs
- Project lookup with alias fallback uses single LEFT JOIN query instead of two queries
- Git commit hash shown in debug footer

### Fixed
- IDOR in MCP `_update_version` — version ownership now verified against claimed project
- Service account creation sends no password field (was sending empty string → 422)
- Form error display shows field names and supports multiline errors
- `is_admin=False` enforced on user creation (admin promotion only via CLI)
- Search service handles missing embedding model gracefully (was TypeError on None vector)
- 23 test failures from seed data collisions resolved (select-or-insert pattern)

### Security
- Per-request role cache cleared at request boundaries to prevent cross-request permission leakage
- Unique constraint on `member_roles(member_id, role_id)` prevents duplicate role assignments

### Performance
- Composite B-tree indexes on `security_audit_logs(event_type, created_at)` and `(user_id, created_at)`
- Index on `refresh_tokens.expires_at` for background token cleanup
- Eliminated double DB round-trip on every MCP tool call (transport + tool auth)
- 1153 tests passing (up from ~1100)

## [0.1.6] - 2026-04-05

### Added
- **Issue detail redesign** — threaded comments with inline replies, emoji reactions, resolve/unresolve, description versioning with diff view, editable estimated hours, sidebar with watchers and relations
- **Wiki improvements** — auto-created Home page, TOC sidebar with scrollspy, `[[Page Name]]` wikilinks, page hierarchy with tree picker, inline rename
- **Search overhaul** — hybrid search (FTS + vector) as default mode, per-type filter tabs with counts, highlighted snippets, comments and attachments search, global search input in header
- **Local embedding model** — multilingual-e5-small support via ONNX runtime, interactive download script (`make download-model`), backfill CLI for issues, wiki, comments, and attachments
- **Admin panel** — dashboard with stats, user management (create, password reset, lock/unlock), project management (create, edit, archive/unarchive), settings editor, email configuration with test delivery, workflow viewer
- **Profile and preferences** — editable display name, avatar color picker and photo upload, language and timezone selects with full IANA timezone list
- **Authentication** — silent JWT refresh for web pages, Remember Me, password reset via email token, login/logout audit logging
- **Project creation** — modal with auto-generated identifier and key, color picker, parent project selector, module checkboxes
- **Notifications** — extensible channel architecture with email delivery via Celery, WebSocket support
- **PWA support** — installable progressive web app with dynamic branding from DB settings
- **Mobile responsive** layout with hamburger menu
- **Audit logging** — login success/failure, search queries, project rename events
- **Project key rename** — atomic re-keying of all issues with alias table for old key resolution (admin only)
- **E2E test suite** — Playwright infrastructure with tests for issue detail, relations, comments, search
- **SQL debug profiler** with query count and timing in footer (debug mode)
- **Persistent file logging** with sensitive data masking
- Update script (`scripts/update.sh`) for instance deployments

### Changed
- PostgreSQL and Redis containers start by default (removed profiles requirement)
- Dev overlay port configurable via `SPECIVO_DEV_PORT` env var (default 8000)
- Trailing slashes on all route paths for consistency
- `require_user` and `require_admin` FastAPI dependencies replace duplicated auth boilerplate
- Service layer extractions — project stats, wiki page tree, issue detail tabs moved from routes to services
- Admin API uses shared `require_admin_api` dependency across all endpoints
- Activity feed default 25 per page with configurable page size
- CSS classes use `sp-` prefix consistently to avoid Bootstrap collisions

### Fixed
- Zero-config quick start — added defaults for `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL` in compose
- `CORS_ORIGINS` env var format (JSON array)
- CSP compliance — all inline scripts moved to specivo.js, Alpine.js directives replace onclick handlers
- CSRF protection on wiki restore, HTML sanitization, wiki link escaping
- Concurrent insert race conditions across services
- Private issue filtering in autocomplete and search
- Hybrid search computes per-type counts from merged RRF results
- Comment search uses correct source type
- Nginx dynamic upstream resolver for container restarts

### Security
- Restrict OpenAPI/docs/redoc to admin-only in production
- Rate limiting on auth endpoints and emoji reactions
- Permission checks on relation delete, resolve/unresolve, description restore
- Private note reply guard — non-admin replies to private notes are rejected

## [0.1.5] - 2026-03-29

### Fixed
- Alpine base image upgraded for zlib CVEs
- Removed pip from Alpine image (CVE-2025-8869, CVE-2026-1703)
- Upgraded cryptography 46.0.5 → 46.0.6 (CVE-2026-34073)
- Static assets used absolute `http://` URLs behind reverse proxies, causing mixed-content blocking on HTTPS

### Changed
- Switched to Alpine base image for smaller container size
- Added `workflow_call` trigger to CI for reusable workflows

## [0.1.0] - 2026-03-29

First public release of Specivo Community Edition.

### Added
- Project tracking with issues, nested-set hierarchy, workflow engine, and time tracking
- Wiki with versioning, page hierarchy, and link graph visualization
- Hybrid search: full-text (tsvector) + semantic (pgvector) + RRF fusion
- Searchable file attachments with multi-chunk indexing
- MCP server (11 tools) for AI agent integration
- Bundled multilingual-e5-small embedding model (MIT, 100 languages)
- Universal embedding prefix system — auto-detect for e5/OpenAI/Cohere
- Internationalization with Babel + gettext + Jinja2 i18n
- Frontend plugin architecture with feature-gated pro/enterprise UI
- Navy+Gold theme with self-hosted fonts (DM Sans, Source Serif, JetBrains Mono)
- Vendored assets — Bootstrap 5.3.6, Alpine.js, htmx
- Versioned static filenames for cache busting (no build step)
- Configurable Docker instance names for multi-instance deployments
- Docker deployment with offline bundle support
- CI/CD via GitHub Actions
- 722 tests (unit + integration)

### Security
- Fixed 12 findings from security audit — SQL injection, SSRF, XSS, access control
