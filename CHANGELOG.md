# Changelog

All notable changes to Specivo are documented here, one line per change.
Each release also has a blog post with the details.

## [0.10.0] - 2026-10-28

### Added
- Added single sign-on with OpenID Connect: people sign in through Keycloak, authentik, Microsoft Entra ID or any OIDC provider, configured under Administration → Single sign-on
- Added several sign-in providers at once, each with a connection self-test and an encrypted client secret (or one read from a `SPECIVO_SSO_SECRET_*` variable)
- Added accounts created on first sign-in, connecting an existing account from Profile → Connected accounts, and auto-linking by verified email; all off by default
- Added mapping of identity provider groups to Specivo user groups and the admin flag, with required groups and an email domain allowlist per provider
- Added sign-out at the identity provider and back-channel logout, so a session ended at the provider also ends in Specivo
- Added login page options: provider buttons, hiding the password form, auto-redirect to one provider and password sign-in for admins only, with `?local=1` and `SSO_FORCE_LOCAL_LOGIN` as the way back in
- Added user deactivation and reactivation for admins; deactivating ends all sessions and turns off the user's API keys
- Added a sessions list under Preferences → Security showing how each session signed in
- Added audit events for sign-in, linking, group changes, logout and provider changes
- Added a single sign-on guide with setup pages for Keycloak, authentik and Microsoft Entra ID

### Changed
- Changed sign-out to a form; opening `/logout/` now asks first, and `POST /api/v1/auth/logout/` returns 200 with `end_session_url`
- Changed password resets by email, by an admin or from the CLI to end every session of the account; people who sign in only through SSO cannot reset a password by email
- Changed admin API keys to be refused for password resets, unlocking or reactivating users, creating people or admins, issuing keys for others and changing SSO settings
- Changed how the client address is read behind a proxy: only proxies in `TRUSTED_PROXIES` count, `TRUSTED_PROXY_HOPS` sets how many (default 1)
- Changed the bundled nginx to a fixed address on a new `proxy` network; set `SPECIVO_PROXY_SUBNET` and `SPECIVO_NGINX_IP` if `docker compose up` reports "Pool overlaps"
- Changed the bundled nginx access log to leave out query strings
- Removed the unused `github_id` and `google_id` user columns

### Fixed
- Fixed sign-out leaving the server-side session and the access token valid until they expired
- Fixed every client behind the bundled nginx sharing one rate-limit bucket

### Security
- Fixed requests that carried only the refresh cookie skipping the CSRF check
- Fixed a token refresh racing a session revocation and keeping the session alive

### Internal
- Added migrations 0041–0043: SSO providers, identities and group mappings, a stable session id on refresh tokens, and deactivation on access loss. Rotating `SECRET_KEY` now also requires re-entering SSO client secrets

## [0.9.0] - 2026-10-26

### Added
- Added `--import-passwords` to bring Redmine password hashes across and upgrade them to bcrypt on first sign-in; off by default, admins and LDAP accounts still reset
- Added password hasher plugins and `PASSWORD_HASHERS`, enabled per algorithm with `LEGACY_PASSWORD_ALGORITHMS`
- Added import source plugins; `python -m specivo.cli.import_data --source NAME` runs any of them
- Added import of Redmine anonymous and non-member access onto per-project public access, without turning on the instance switch
- Added role settings for seeing all users or only project members, and all time entries or only one's own
- Added a tracker option to make new issues private by default, and admin pages for roles and trackers
- Added search by issue key: `ACME-42` finds the issue and its mentions in every search mode, including old keys of moved issues
- Added links to any attachment by filename, such as `[spec](spec.pdf)` and `![chart](chart.png)`, in issues, comments, the preview and the wiki
- Added `python -m specivo.cli.download_model` to fetch the semantic search model at a pinned revision and check its SHA-256

### Changed
- Changed time tracking permissions to apply everywhere; with default roles, Reporter and Non member no longer see time entries, and Agent gets *View time entries*
- Changed content to be indexed on save, so keyword search finds new and edited content at once; embeddings follow from the worker
- Changed embedding jobs to run in a new `celery-embeddings` service that loads the model once; custom compose files need it, or `EMBEDDING_QUEUE=celery`
- Removed support for embedding models other than the bundled local one, and the endpoint for registering them
- Changed change password, sign out and session revocation to require a CSRF token when called with a session cookie
- Changed MCP to report unexpected tool errors as "Internal error while executing tool", cap request bodies at 4 MiB (HTTP 413) and report the Specivo version
- Changed Markdown fields to a 262,144-character limit; text too complex to render quickly is shown as plain text
- Changed relative times to stay relative for 24 hours instead of switching to "Yesterday" at midnight
- Removed `specivo.cli.import_redmine`; use `specivo.cli.import_data --source redmine` instead
- Changed the `make` targets for model download, backfill, reindex and import to work from the release bundle

### Fixed
- Fixed new comments not being found by search and edits not being re-indexed
- Fixed semantic search missing visible matches ranked behind hidden ones and scanning every embedding; it now uses a vector index
- Fixed deleted wiki pages still matching keyword search
- Fixed the search backfill reporting success without a model and skipping some attachments
- Fixed the release bundle's `configure.py` writing outside the bundle and the model download pointing to a missing release asset
- Fixed a misconfigured embedding model breaking indexing and search; semantic search now turns off instead
- Fixed remaining untranslated interface text, and dates not following the interface language
- Fixed keyword search returning short pages after the first one
- Fixed comments saved in the same moment appearing out of order
- Fixed large wiki pages showing as plain text on a busy server; render limits now use CPU time, tuned by `MARKDOWN_RENDER_DEADLINE_SECONDS`, `MARKDOWN_RENDER_REQUEST_BUDGET_SECONDS` and `MARKDOWN_RENDER_SPEED_FACTOR`

### Security
- Fixed attachments of issues a reader cannot open being downloadable by id and shown on wiki pages
- Fixed attachments of deleted wiki pages staying searchable
- Fixed crafted Markdown keeping a worker busy for minutes per view, and deeply nested lists causing errors
- Fixed text with many unclosed `<!--` taking minutes to index and show in search, and long search queries failing
- Fixed time entries being readable, loggable and editable without the matching permissions, and timers starting in any project
- Fixed shared versions of private projects not being filtered by access
- Fixed a very deep search offset making the database rank millions of rows
- Fixed indexing errors writing content to the logs
- Fixed app containers running as root; they now run as UID 10001, and custom compose files need the new one-shot `init-data` service
- Fixed MCP sessions being resumable with a different or revoked API key, and MCP tool errors exposing internal exception text

### Internal
- Added migrations 0033–0040: role and tracker visibility, issue-key search terms, comment search chunks and an HNSW vector index. Run a search reindex after upgrading (Administration → Settings)
- Upgraded all dependencies, including the MCP SDK 2.x, Pillow 13 and PyJWT 2.15, and added a Trivy image scan that blocks releases with fixable HIGH/CRITICAL vulnerabilities

## [0.8.0] - 2026-09-29

### Added
- Added user groups that hold project roles, managed under Admin → Groups; members get the group's roles and lose them when removed
- Added read-only access to public projects for visitors without an account, off by default and enabled per project plus an instance-wide switch
- Added visitor access to the project list, overview, issues, wiki and search in the browser and JSON API; MCP stays API-key only
- Added changing your own password from preferences; other sessions are signed out and wrong attempts are rate limited without locking the account
- Added an admin option to require a password change on next sign-in; API keys keep working and service accounts are exempt
- Added import of Redmine groups as Specivo groups holding their project roles, instead of per-person grants; name clashes get a suffix

### Changed
- Changed access to be decided by roles alone, with a new read-only built-in Non member role for signed-in non-members of public projects
- Changed custom roles without *View issues* to lose access to issues everywhere, including search, autocomplete and MCP
- Changed signed-in non-members of public projects to pass *View issues* checks for sprints, versions, recurring patterns, reactions and MCP issue tools
- Changed search to hide a public project's wiki pages from signed-in non-members, and private notes from everyone but administrators
- Changed opening a project's wiki to create its Home page only for readers who can manage the wiki
- Changed adding or removing issue relations to require *Manage issue relations* in REST and the web; Reporter, Non member and custom roles without it lose it
- Changed MCP `specivo_remove_relation` to remove only relations attached to the issue passed as `issue_ref`
- Changed project cards and the overview to count people with direct or group access; the admin projects table shows People and Groups

### Fixed
- Fixed messages, toasts, placeholders and relative times in 27 interactive components always showing in English
- Fixed untranslated groups administration, members tab, tags, issue moves, search indexing, password changes and computed fields in every locale

### Security
- Fixed issue autocomplete revealing keys and subjects of issues the user cannot open, including private issues
- Fixed search returning issue snippets and wiki pages to custom roles without the needed permissions
- Fixed private subprojects being listed on their parent project's overview
- Fixed the REST API letting anyone who could read two issues add or remove a relation without *Manage issue relations*

### Internal
- Added migrations 0028–0032: user groups, group memberships, `users.must_change_password`, a reserved `$anonymous` user, `projects.anonymous_permissions` and the Non member role. The upgrade stops if a login `$anonymous` exists
- Changed frontend bundles to be built in the Docker image or by `make frontend-build` instead of being committed

## [0.7.0] - 2026-09-08

### Added
- Added `import_redmine` to migrate projects, users, memberships, issues with full history, relations, watchers, custom fields, wiki revisions, attachments and time from Redmine
- Added reading Redmine's MySQL or PostgreSQL database and attachment files directly; a read-only database user and files mount are enough
- Added `--dry-run`, which runs the whole import and rolls it back, reporting derived project keys, inferred status categories and unsupported data
- Added `--project-key-map` and `--status-category-map` to correct derived project keys and status categories before the real run
- Added `--resume` to continue an interrupted import; a second run of a finished import creates nothing
- Added reporting of what cannot be imported: passwords, with logins listed for reset, groups, which become per-member role grants, and unsupported modules, fields and permissions

### Internal
- Added migration 0027: an `import_id_map` table recording which source record became which Specivo row
- Added a disposable Redmine 7.0.1 fixture (`docker-compose.redmine.yml`) with end-to-end import tests marked `redmine`

## [0.6.0] - 2026-08-27

### Added
- Added issue tags to MCP `show_issue` and `list_issues` output, fetched in one batched query, so agents no longer need a tag call per issue

## [0.5.0] - 2026-08-03

### Fixed
- Fixed project computed metadata being write-only; `GET /api/v1/projects/{key}/` now returns `computed_metadata` to users who may manage the project

### Added
- Added `computed_metadata` to `POST /api/v1/projects/` so a project can be created with computed metadata in one call
- Added a Computed Fields editor on the project settings Metadata tab for users who may manage the project

## [0.4.2] - 2026-07-26

### Fixed
- Fixed MCP clients hanging on connect after a server restart while another client held a long-lived event stream; present since 0.1.9

## [0.4.1] - 2026-07-25

### Fixed
- Fixed filtering issues by status name returning the unfiltered list; names now match case-insensitively and unknown values return an error

### Security
- Fixed outstanding dependency advisories by updating Pillow to 12.3.0, the MCP SDK to 1.28.1, pydantic-settings to 2.14.2 and esbuild to 0.25

### Internal
- Fixed test runs aborting on database deadlocks between workers and leaking registered features between tests

## [0.4.0] - 2026-06-24

### Added
- Added moving issues between projects from the sidebar, REST API and MCP; old keys still resolve and sub-task hierarchies must be detached first
- Added project-derived computed metadata, configured per project, filled in read-only on every issue and recomputed when an issue moves
- Added a `metadata` map to MCP `create_issue`

### Changed
- Changed `KEY-123` references to auto-link only when the issue exists; look-alike tokens stay plain text

### Fixed
- Fixed wiki pages whose title has no Latin letters or digits, such as Thai titles, getting an empty slug

### Security
- Fixed text with very many issue-reference tokens turning a page view into an oversized database lookup

## [0.3.0] - 2026-06-20

### Added
- Added per-project tags with optional colors for issues and wiki pages, managed in Project Settings → Tags
- Added tag filters in search with multi-select AND matching and autocomplete, plus MCP and API support for tags
- Added a per-project full-text search language setting in Project Settings, with a one-click reindex

### Fixed
- Fixed MCP sessions losing their initialization across an API restart
- Fixed Celery task database sessions leaking connections

## [0.2.1] - 2026-06-18

### Fixed
- Fixed recurring task generation and token cleanup erroring when a Redis lock was contended or lost

## [0.2.0] - 2026-06-18

### Added
- Added recurring tasks with RRULE patterns, a Celery beat generator, REST API, MCP tools and a create/edit page
- Added date macros such as `{{year}}`, `{{month}}` and `{{weekday}}` in recurring issue subjects and descriptions, localized to the workspace language
- Added a "Created from recurring pattern" activity entry linking each generated issue to its pattern
- Added filtering issues by metadata: array values link to a metadata-filtered search across all accessible projects
- Added translations for the remaining project, sprint and wiki interface text

### Changed
- Changed the workspace default language and timezone to save together from one button
- Changed metadata preset identifiers to be normalized and unique regardless of case

### Fixed
- Fixed the "Create preset" action failing silently; name and identifier now show inline errors
- Fixed recurring pattern form saving, the enable toggle, the Skip action, the detail layout and the list Name column

### Security
- Fixed outstanding dependency advisories by updating cryptography, idna, Mako, Pillow, Pygments, PyJWT, pytest, python-multipart, starlette and urllib3

## [0.1.10] - 2026-06-14

### Added
- Added Russian, Chinese, French, Spanish and Thai interface languages, with a workspace default and a per-user override
- Added a Markdown editor (EasyMDE) for issue descriptions and wiki pages, and a Markdown preview endpoint
- Added editing an issue's title and description together from one button
- Added MCP tools for metadata schemas, and filtering `list_issues` by metadata key=value

### Fixed
- Fixed the sidebar not filling long pages, and clipping in the project settings modal and add-member autocomplete
- Fixed workflow admin transition checkboxes not saving
- Fixed an intermittent 404 right after creating an issue
- Fixed metadata schema endpoints not being wired up and secured
- Fixed `specivo_metadata` accepting lossy numeric inputs
- Fixed Markdown editor refresh and toolbar issues
- Fixed stale browser caches by content-hashing CSS and JS filenames
- Fixed the issue activity feed not opening on the latest page
- Fixed Celery task registration with monthly partitioning

## [0.1.9] - 2026-04-19

### Added
- Added a dashboard with project stats, recent releases, wiki activity, paginated issues with sprints and an agent activity feed
- Added a sprint detail page with metrics and an issue list
- Added searchable version and sprint selects in the sidebar that still offer closed versions and completed sprints
- Added MCP session persistence in Redis with a sliding TTL, so clients survive API restarts
- Added MCP `specivo_list_comments`, comment counts in `show_issue`, a `get` op for `specivo_metadata` and a search `mode` defaulting to hybrid
- Added a Codex CLI option to the admin MCP configuration card
- Added support for project keys up to 128 characters, up from 12

### Fixed
- Fixed the activity log showing raw sprint and version IDs, and whole metadata instead of per-key diffs
- Fixed JWT refresh not happening silently on API endpoints
- Fixed unmatched routes not showing the styled 404 page
- Fixed the Codex MCP config not using native streamable HTTP

### Security
- Fixed the CSRF HMAC secret being a separate value; it is now derived from `settings.secret_key`

## [0.1.8] - 2026-04-12

### Added
- Added sprints: backlog page, sprint board, start and complete controls, velocity and burndown charts, sprint analytics and history
- Added custom metadata schemas per project or issue type, with a preset library, metadata on issue forms and an admin interface
- Added a kanban board view of the issue list with a status progress bar and per-column card limits
- Added version management: a version detail page with linked issues and progress, a sidebar version selector and CRUD in project settings
- Added wiki soft-delete with trash, restore and auto-purge
- Added issue relations of 9 types, with autocomplete and display on the issue page
- Added MCP tools for wiki sections, wiki delete, relations, sprints, versions, metadata, attachment upload and `whoami`, 38 tools in total
- Added `specivo_setup_guide()` returning the full agent configuration, and richer MCP tool descriptions
- Added short issue URLs: `/issue/KEY-123/` redirects to the full issue address
- Added wiki image paste, drag-and-drop upload, `[[wikilinks]]` in issues and comments, and auto-linked `KEY-123` references
- Added a "Current Sprint" sidebar link when a sprint is active
- Added a parent project selector and member role management to project settings
- Added a separate `manage_sprints` permission, independent of `manage_versions`

### Changed
- Changed project visibility to be membership-based with public and private projects, enforced by all API endpoints
- Changed the project key limit from 10 to 12 characters
- Changed timestamps to display in the user's timezone instead of UTC
- Changed roadmap progress to use status categories (backlog, active, done, closed) instead of `is_closed`
- Changed full-text search to tokenize underscores, so `wiki_page` and `wiki page` both match
- Changed the sidebar to fixed positioning with distinct icons, and wide tables to scroll horizontally
- Changed the issue page to run about 15 SQL queries instead of 39

### Fixed
- Fixed the sidebar and kanban layout breaking on long pages, wide code blocks and large images
- Fixed wiki inline images with bare filenames not rendering
- Fixed wrong open/closed counts and hard-to-read avatar initials in the project list
- Fixed the member autocomplete popup style in project settings
- Fixed missing description diffs by storing the initial version on issue creation
- Fixed a new project not being visible after reloading the creation page
- Fixed MCP `update_issue` ignoring `done_ratio`
- Fixed wiki search result links missing a trailing slash
- Fixed Markdown tables having no cell padding
- Fixed SQL wildcards `%` and `_` not being escaped in autocomplete
- Fixed API errors showing raw JSON instead of styled error pages
- Fixed wiki chunking splitting fenced code blocks

### Security
- Fixed forms, fetch and htmx requests lacking CSRF protection; a double-submit cookie now covers them
- Fixed XSS in search snippets
- Fixed attachment download Content-Disposition headers to follow RFC 5987
- Fixed the sprint edit page missing an authorization check
- Fixed wiki restore cascading with soft-delete `deleted_by_id` tracking
- Fixed notification text being rendered with `text | safe` instead of the escaped `title`
- Fixed password resets not being audit logged
- Fixed attachments having no SHA-256 content hash or project-wide unique filenames; collisions are now auto-renamed

### Performance
- Added composite indexes on query-heavy tables and per-request caching of permission checks
- Changed project lookup with alias fallback to a single LEFT JOIN

## [0.1.7] - 2026-04-05

### Added
- Added Streamable HTTP and SSE transports for MCP in place of stdio, for remote agents
- Added 7 MCP tools: create_wiki, list_lookups, list_members, log_time and list/create/update versions, 18 in total
- Added project-scoped permission checks on every MCP tool call
- Added MCP audit events per tool, tagged `source: "mcp"`
- Added an admin user detail page with API key management and service accounts
- Added `PASSWORD_MIN_LENGTH` (default 8, minimum 6)

### Changed
- Changed MCP transport auth to check key format only, with real auth per tool call so key revocation takes effect at once
- Changed API key auth, permission checks and project alias lookup to use fewer queries
- Changed the debug footer to show the git commit hash

### Fixed
- Fixed an IDOR in MCP version updates; version ownership is now checked against the project
- Fixed service account creation failing with 422
- Fixed form errors missing field names and multiline messages
- Fixed user creation accepting `is_admin`; admins can only be promoted via the CLI
- Fixed search failing with a TypeError when the embedding model is missing

### Security
- Fixed the per-request role cache not being cleared between requests, which could leak permissions
- Fixed duplicate role assignments with a unique constraint on `member_roles(member_id, role_id)`

### Performance
- Added indexes on `security_audit_logs` and `refresh_tokens.expires_at`
- Removed a duplicate database round-trip on every MCP tool call

## [0.1.6] - 2026-04-05

### Added
- Added a redesigned issue page with threaded comments, reactions, resolve, description versions with diffs, estimated hours, watchers and relations
- Added wiki Home pages, a table of contents, `[[Page Name]]` wikilinks, page hierarchy with a tree picker and inline rename
- Added hybrid search as the default, filter tabs with counts, highlighted snippets, comment and attachment search, and a header search box
- Added the local multilingual-e5-small embedding model via ONNX, `make download-model` and a backfill CLI for embeddings
- Added an admin panel with stats, user and project management, settings, email configuration with test delivery and a workflow viewer
- Added profile settings for display name, avatar color or photo, language and timezone
- Added silent JWT refresh, Remember Me and password reset by email
- Added a project creation modal with generated identifier and key, color, parent project and modules
- Added notifications with email delivery through Celery and WebSocket support
- Added an installable PWA with branding from settings, and a mobile layout with a hamburger menu
- Added audit logging for logins, logouts, search queries and project renames
- Added admin-only project key renames that re-key all issues and keep old keys resolving
- Added a debug-mode SQL profiler in the footer, and persistent file logging with sensitive data masking
- Added `scripts/update.sh` for updating instances

### Changed
- Changed PostgreSQL and Redis containers to start by default without profiles
- Changed the dev overlay port to be set by `SPECIVO_DEV_PORT` (default 8000)
- Changed all route paths to end with a trailing slash
- Changed the activity feed to show 25 entries per page by default, with a configurable page size

### Fixed
- Fixed quick start by adding compose defaults for `SECRET_KEY`, `DATABASE_URL` and `REDIS_URL`
- Fixed the `CORS_ORIGINS` format to a JSON array
- Fixed CSP violations by moving inline scripts into specivo.js and Alpine directives
- Fixed missing CSRF protection on wiki restore, HTML sanitization and wiki link escaping
- Fixed concurrent insert races across services
- Fixed private issues showing in autocomplete and search
- Fixed hybrid search type counts and the comment search source type
- Fixed nginx losing upstreams after container restarts

### Security
- Fixed OpenAPI docs and ReDoc being public in production; they are now admin-only
- Fixed missing rate limits on auth endpoints and emoji reactions
- Fixed missing permission checks on relation delete, resolve/unresolve and description restore
- Fixed non-admins being able to reply to private notes

## [0.1.5] - 2026-03-29

### Fixed
- Fixed zlib CVEs by upgrading the Alpine base image, and CVE-2025-8869 and CVE-2026-1703 by removing pip from it
- Fixed CVE-2026-34073 by upgrading cryptography to 46.0.6
- Fixed static assets using absolute `http://` URLs behind reverse proxies, causing mixed-content blocking on HTTPS

### Changed
- Changed the container to an Alpine base image for a smaller size
- Changed CI to accept a `workflow_call` trigger for reuse

## [0.1.0] - 2026-03-29

### Added
- Added Specivo Community Edition, the first public release
- Added project tracking with issues, nested hierarchy, a workflow engine and time tracking
- Added a wiki with versioning, page hierarchy and a link graph
- Added hybrid search combining full-text and pgvector semantic search with RRF fusion, including searchable attachments
- Added an MCP server with 11 tools for AI agents
- Added the bundled multilingual-e5-small embedding model (MIT, 100 languages), with automatic prefixes for e5, OpenAI and Cohere models
- Added internationalization with Babel and gettext
- Added a frontend plugin architecture for feature-gated pro and enterprise UI
- Added the Navy and Gold theme with self-hosted fonts and vendored Bootstrap, Alpine.js and htmx
- Added versioned static filenames for cache busting
- Added Docker deployment with configurable instance names and an offline bundle

### Security
- Fixed 12 security audit findings covering SQL injection, SSRF, XSS and access control
