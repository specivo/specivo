# ADR-0001: Frontend Conventions

**Date:** 2026-04-04
**Revised:** 2026-06-20 — adopted an esbuild asset pipeline (previously zero-build); the
JavaScript and CSS monoliths were split into modular sources.
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo uses server-rendered Jinja2 templates with Alpine.js for reactivity, HTMX for partial
updates, and Bootstrap 5 for layout utilities. As the UI grew, patterns were needed for JavaScript
organization, CSS class naming (to avoid Bootstrap collisions), CSP compliance, and PWA support.

The project originally shipped a deliberate **zero-build** frontend: a single hand-written
`specivo.css` and a single `specivo.js`, hashed at app startup. Those files grew past 3,000 and
5,000 lines respectively and became write-only — duplicated rules, 50+ Alpine components in one
file, mixed concerns. As anticipated in the original ADR ("split with esbuild when it exceeds
~1000 lines"), we adopted a small build step: **esbuild** bundles modular sources into committed,
content-hashed artifacts. The runtime stays Node-free.

## Decision

### 1. esbuild Asset Pipeline

Custom CSS/JS live as modular **sources** under `frontend/` and are bundled by **esbuild** into
committed, content-hashed artifacts under `specivo/static/dist/`. The stack:

- **Jinja2** — server-side HTML rendering with theme support
- **Alpine.js 3.14** — lightweight reactivity (`x-data`, `x-model`, `x-show`)
- **HTMX 2.0** — HTML-over-the-wire partial updates
- **Bootstrap 5.3** — grid, utilities only (NOT Bootstrap JS components)
- **esbuild** — the only build dependency (single Go binary, dev/CI only)

Three bundles are produced:

| Source entry | Bundle | Role |
|--------------|--------|------|
| `frontend/css/specivo.css` | `dist/css/specivo.min.css` | all custom styles |
| `frontend/js/alpine-init.js` | `dist/js/alpine-init.min.js` | registers every Alpine component + store |
| `frontend/js/app.js` | `dist/js/app.min.js` | vanilla (non-Alpine) modules |

- **Content hashing + manifest:** esbuild writes `<name>.<hash>.<ext>` plus a `manifest.json`
  per output dir. At startup the app loads the manifests (`_load_asset_manifests()` in `main.py`)
  into the `versioned` Jinja global; templates resolve the served filename via
  `{{ versioned['specivo.min.css'] }}`. This replaced the old Python startup SHA hashing.
- **Committed artifacts → Node-free runtime:** `dist/` is committed, so the Docker image and
  end users never need Node. Only the dev/CI build needs it.
- **CI guard:** CI runs `npm ci && npm run build` then `git diff --exit-code specivo/static/dist/`,
  failing if committed bundles are stale relative to source.

**Dev workflow:** edit under `frontend/`, run `npm run build` (or `npm run watch`), and commit the
regenerated `dist/` with the source change. See `frontend/README.md`.

### 2. Alpine.js Component Pattern

Each Alpine component is an exported factory in its own file under `frontend/js/components/`, and
is registered in `frontend/js/alpine-init.js`. Templates reference components by name only.

**`frontend/js/components/issue-form.js`:**
```javascript
export function issueForm(initial) {
    return {
        subject: initial.subject || '',
        async submitForm() { /* spFetch() call */ }
    };
}
```

**`frontend/js/alpine-init.js`:**
```javascript
import { issueForm } from './components/issue-form';
document.addEventListener('alpine:init', function () {
    Alpine.data('issueForm', issueForm);
});
```

**`frontend/js/app.js`** wires non-Alpine concerns: each vanilla module under
`frontend/js/modules/` exports an `initX()` called on `DOMContentLoaded`.

Shared helpers (`spFetch`, `_getCsrfToken`, autocomplete anchoring) live in
`frontend/js/lib/globals.js` and are attached to `window` so component bodies — and inline Alpine
components in templates — can call them by bare name.

The registration name string passed to `Alpine.data(...)` must match the `x-data="name(...)"` in
templates. Bundle load order is load-bearing: `alpine-init.min.js` is deferred **before**
`alpine.3.14.min.js` so components register before `alpine:init` fires; EasyMDE loads before
`alpine-init` so `markdownEditor` sees `window.EasyMDE`.

**What stays inline in templates:**
- Simple data objects: `x-data="{ expanded: false }"`
- Simple assignments: `@click="showCreate = true"`
- Property access and comparisons: `x-show="tab === 'general'"`
- Object class bindings: `:class="{ active: tab === 'general' }"`

**What must be in registered components** (so it survives a future switch to the Alpine CSP build —
see §5):
- String methods: `.trim()`, `.toUpperCase()`, `.substring()`, `.includes()`
- Global functions: `Object.keys()`, `JSON.stringify()`, `Math.max()`
- `async`/`await` and `fetch()` calls
- Arrow functions, destructuring, template literals, spread operator

**Naming:** `camelCase` for components (`projectCreateModal`, `wikiForm`), kebab-case file names
(`project-create-modal.js`), verb-first for methods (`submit`, `loadKeys`, `toggleModule`).

### 3. x-data Attribute Quoting (CRITICAL)

When passing server-rendered data to Alpine components via `tojson`, **always use single-quoted
`x-data` attributes**:

```html
<!-- CORRECT: single-quoted attribute, tojson outputs double quotes safely -->
<div x-data='wikiForm({ mode: {{ mode | tojson }}, projectKey: {{ project.key | tojson }} })'>

<!-- WRONG: double-quoted attribute breaks when tojson outputs double quotes -->
<div x-data="wikiForm({ mode: {{ mode | tojson }} })">
```

`tojson` outputs `"value"` with double quotes. If the HTML attribute also uses double quotes, the
attribute is truncated and Alpine fails silently.

### 4. CSS Organization & Naming

CSS is split into cascade-ordered partials under `frontend/css/`, imported by a single entry
`frontend/css/specivo.css` whose `@import` order matches the original monolith textually so the
bundled cascade is unchanged. esbuild inlines the imports into one `specivo.min.css`.

```
frontend/css/
  specivo.css         # entry: @import list in documented cascade order
  tokens.css base.css layout.css            # foundations (+ @font-face, design tokens)
  sidebar.css header.css chrome.css         # chrome
  cards.css buttons.css badges.css tables.css   # components
  kanban.css activity.css dashboard.css sprints.css versions.css metadata.css ...  # features
  pages/<name>.css                          # page-specific
  animations.css responsive.css ...         # trailing utilities (order matters)
```

**Cascade order matters** — some later rules intentionally layer on earlier ones (e.g. the
animation block re-declares `.project-card { animation }` after the base `.project-card`). Do not
reorder/merge partials without checking the cascade; the entry file documents this.

**`sp-` prefix:** all custom classes that could collide with Bootstrap use the `sp-` prefix
(`.sp-btn`, `.sp-btn-primary`, `.sp-pagination`, `.sp-modal`, `.sp-scrim`, …). When in doubt,
prefix. Design tokens are `--sp-*` CSS custom properties in `tokens.css`.

**Deferred cleanup** (tracked here, not yet done): many page/feature classes predate the prefix
rule and keep unprefixed names (`.project-card`, `.filter-tab`, `.velocity-chart`); renaming them
touches many templates and is deferred. The `.sp-modal-*` child rules (header/body/footer/close)
still layer across `base.css` and `metadata.css`; only the `.sp-modal`/`.sp-scrim` containers have
been consolidated into `base.css`.

### 5. No Inline JavaScript (CSP-oriented conventions)

Custom JS lives only in the bundles — no `<script>` code blocks or inline event handlers in
templates:

**Forbidden:**
```html
<script>doSomething();</script>          <!-- NO inline script -->
<button onclick="save()">                <!-- NO inline handler -->
<tr onmouseover="this.style.background='...'">
```

**Instead:**
```html
<button data-font="font-md">A</button>   <!-- data attribute + module in app.js -->
<tr class="sp-row-hover">                 <!-- CSS :hover, not inline -->
<button @click="save()">                  <!-- Alpine directive, processed by Alpine -->
```

**Current CSP (accurate):** the served header is
`script-src 'self' 'unsafe-eval' 'unsafe-inline'`, and templates load the standard Alpine build
(`alpine.3.14.min.js`). The stricter `script-src 'self'` with the Alpine **CSP** build
(`alpine.csp.3.14.min.js`, already vendored) is the **target**, not the current state — tightening
it is out of scope for the esbuild migration. The conventions above (no inline handlers,
data-attributes, component-registered logic) keep that switch a drop-in change later. esbuild's
output introduces no new `eval`/`new Function`.

### 6. PWA Support

Specivo is installable as a Progressive Web App:

- **`/manifest.json`** — dynamic FastAPI route, uses `brand_name` from DB settings
- **`/static/sw.js`** — service worker (cache-first for vendor assets, network-first for pages)
- **Meta tags** in `base.html`: `<link rel="manifest">`, `theme-color`, `mobile-web-app-capable`
- **Service worker registration** in `frontend/js/modules/service-worker.js` (run from `app.js`)

The manifest reflects the current `brand_name` setting without rebuilding.

### 7. Markdown Rendering

User-authored content (wiki pages, issue descriptions, comments) is stored as raw Markdown and
rendered server-side:

- **Library:** Python `markdown` with extensions: `fenced_code`, `tables`, `toc`
- **Jinja2 filter:** `{{ content.text | markdown }}` — returns `Markup` (safe HTML)
- **CSS:** `.wiki-content`, `.editor-preview`, `.prose` classes style rendered HTML
- **Editing:** the `markdownEditor` component (`frontend/js/components/markdown-editor.js`) wraps
  EasyMDE; EasyMDE must load before `alpine-init.min.js`

### 8. Pagination

The `pagination` Jinja2 macro in `components/macros.html` renders a three-part bar:

```
1–25 of 43          « 1 [2] »          Show [25 ▾]
(info)              (page links)       (page size)
```

- **`.sp-pagination`** / **`.sp-pagination-info`** / **`.sp-pagination-pages`** /
  **`.sp-pagination-size`** — the bar's parts
- The page-size `<select>` is driven by `data-pagination-limit` + a listener in
  `frontend/js/modules/page-size.js` (no inline `onchange`)

### 9. Static Asset Organization

```
frontend/                          # esbuild SOURCE (dev/CI only, Node)
  build.js package.json            # esbuild driver + scripts (esbuild devDep only)
  css/ js/                         # modular partials and ES modules (see §2, §4)
specivo/static/
  dist/                            # COMMITTED build output, served at runtime
    css/specivo.min.<hash>.css  +  manifest.json
    js/{alpine-init,app}.min.<hash>.js  +  manifest.json
  sw.js                            # service worker (PWA)
  vendor/                          # third-party (bootstrap, alpine, htmx, easymde) — immutable
  fonts/                           # self-hosted WOFF2 — immutable
  img/                             # SVG favicon
```

`frontend/node_modules/` and `dist/**/*.map` are gitignored; the hashed bundles + manifests are
committed.

### 10. Template Resolution Order

1. Custom theme from `data/themes/{name}/` (user-provided, optional)
2. Built-in theme from `specivo/templates/themes/{name}/` (if not "default")
3. Default theme `specivo/templates/themes/default/` (always present)
4. Custom error pages from `data/errors/` (optional: 403.html, 404.html, 500.html)
5. Shared templates `specivo/templates/_shared/` (error pages, email templates)

Default templates are baked into the Docker image. Custom themes/errors are in the external data
mount. Missing directories are silently skipped.

### 11. Brand Customization

The `brand_name` DB setting (default: "Specivo") replaces the hardcoded product name in:
- Sidebar brand text
- Login page heading
- All page `<title>` tags (via `{{ brand_name }}` template variable)
- PWA manifest (`/manifest.json`)

Cached in memory at startup, updated instantly when admin changes it. "Powered by Specivo" footer
keeps the product name (not the instance brand).

## Consequences

**Positive:**
- Modular, navigable source — one Alpine component per file, CSS partitioned by cascade layer
- Minified, content-hashed bundles; manifests drive cache busting (no manual versioning)
- Runtime stays Node-free (artifacts committed); only one build dependency (esbuild)
- CI fails on stale bundles, so committed `dist/` can't drift from source
- No Bootstrap class collisions — `sp-` prefix is unambiguous
- PWA installable with dynamic branding

**Negative:**
- A build step now exists: editing `frontend/` requires `npm run build` and committing `dist/`
- Bundles are committed artifacts in the repo (diff noise on rebuilds)
- Alpine logic that uses string/global methods must stay in registered components to remain
  compatible with a future CSP-build switch
- Some CSS still uses unprefixed class names and layered `.sp-modal-*` children (see §4 deferred)

## Not Chosen

- **React/Vue/Svelte SPA** — too heavy for a server-rendered admin UI
- **Webpack/Vite** — esbuild is a single binary with one dependency; no need for a larger toolchain
- **CSS Modules / Tailwind** — Tailwind adds a heavier Node build; the `sp-` prefix + partials
  give enough isolation
- **Bootstrap JS components** — modals, dropdowns replaced with Alpine.js for lighter weight
- **Building inside Docker** — would add a Node toolchain to the runtime image; committing
  pre-built artifacts keeps the image slim and Node-free
- **Syntax highlighting (Prism/Highlight.js)** — deferred, plain `<pre><code>` styling sufficient
