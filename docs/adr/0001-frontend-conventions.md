# ADR-0001: Frontend Conventions

**Date:** 2026-04-04
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo uses a zero-build-step frontend: Jinja2 server-side rendering, Alpine.js for reactivity, HTMX for partial updates, Bootstrap 5 for layout utilities. As the UI grew, patterns were needed for JavaScript organization, CSS class naming (to avoid Bootstrap collisions), CSP compliance, and PWA support.

## Decision

### 1. Zero-Build Frontend Stack

No Node.js, no npm, no bundler. All assets are plain files served directly:
- **Jinja2** — server-side HTML rendering with theme support
- **Alpine.js 3.14** — lightweight reactivity (`x-data`, `x-model`, `x-show`)
- **HTMX 2.0** — HTML-over-the-wire partial updates
- **Bootstrap 5.3** — grid, utilities only (NOT Bootstrap JS components)
- **specivo.js** — all custom JavaScript in one file
- **specivo.css** — all custom styles, design tokens as CSS variables

Cache busting via versioned filenames at startup (`specivo.0.1.6.css`), not content hashing.

### 2. Alpine.js Component Pattern

All component logic with API calls or non-trivial state goes into `specivo.js` using `Alpine.data()`. Templates reference components by name only.

**In `specivo.js`:**
```javascript
Alpine.data('issueForm', function (initial) {
    return {
        subject: initial.subject || '',
        async submitForm() { /* fetch() call */ }
    };
});
```

**In template:**
```html
<div x-data='issueForm({ subject: {{ issue.subject | tojson }} })'>
```

**What stays inline in templates:**
- Simple UI toggles: `x-data="{ expanded: false }"`
- Single expressions: `@click="showCreate = true"`

**Naming:** `camelCase` for components (`projectCreateModal`, `wikiForm`), verb-first for methods (`submit`, `loadKeys`, `toggleModule`).

### 3. x-data Attribute Quoting (CRITICAL)

When passing server-rendered data to Alpine components via `tojson`, **always use single-quoted `x-data` attributes**:

```html
<!-- CORRECT: single-quoted attribute, tojson outputs double quotes safely -->
<div x-data='wikiForm({ mode: {{ mode | tojson }}, projectKey: {{ project.key | tojson }} })'>

<!-- WRONG: double-quoted attribute breaks when tojson outputs double quotes -->
<div x-data="wikiForm({ mode: {{ mode | tojson }} })">
```

`tojson` outputs `"value"` with double quotes. If the HTML attribute also uses double quotes, the attribute is truncated and Alpine fails silently.

### 4. CSS Class Naming — `sp-` Prefix

All custom CSS classes use the `sp-` prefix to avoid collisions with Bootstrap:

| Specivo class | Replaces | Why |
|---------------|----------|-----|
| `.sp-btn` | `.btn` | Bootstrap `.btn` resets display/padding |
| `.sp-btn-primary` | `.btn-primary` | Bootstrap sets its own colors |
| `.sp-btn-ghost` | `.btn-ghost` / `.btn-secondary` | Bootstrap secondary style |
| `.sp-btn-danger` | `.btn-danger` | Bootstrap danger style |
| `.sp-btn-sm` | `.btn-sm` | Bootstrap small button |
| `.sp-btn-compare` | `.btn-compare` | Consistency |
| `.sp-row-hover` | inline `onmouseover` | CSP-safe hover |
| `.sp-pagination` | `.pagination` | Bootstrap `.pagination` conflicts |
| `.sp-page-link` | `.page-link` | Bootstrap page link styles |
| `.sp-scrim` | modal backdrop | No Bootstrap modal dependency |
| `.sp-modal` | modal dialog | No Bootstrap modal dependency |

All button variants use the `sp-btn-` prefix consistently:
- `.sp-btn-login` — login page submit button
- `.sp-btn-kill` — kill switch button

Non-button classes that are unique to Specivo keep descriptive names without prefix:
- `.filter-tab`, `.filter-tabs` — tab navigation
- `.project-card`, `.wiki-content`, `.editor-*` — domain components

**Rule:** All new CSS classes that could collide with Bootstrap must use the `sp-` prefix. When in doubt, prefix.

### 5. No Inline JavaScript (CSP Compliance)

The Content Security Policy is `script-src 'self' 'unsafe-eval'` (unsafe-eval required by Alpine.js). Inline scripts and event handlers are blocked.

**Forbidden:**
```html
<!-- NO: inline script tag -->
<script>doSomething();</script>

<!-- NO: inline event handlers -->
<button onclick="save()">
<tr onmouseover="this.style.background='...'">
```

**Instead:**
```html
<!-- Use data attributes + JS in specivo.js -->
<button data-font="font-md">A</button>

<!-- Use CSS :hover via .sp-row-hover class -->
<tr class="sp-row-hover">

<!-- Alpine.js directives are OK (processed by Alpine, not eval'd by browser) -->
<button @click="save()">
```

All custom JS lives in `specivo.js`. No `<script>` tags with code in templates.

### 6. PWA Support

Specivo is installable as a Progressive Web App:

- **`/manifest.json`** — dynamic FastAPI route, uses `brand_name` from DB settings
- **`/static/sw.js`** — service worker (cache-first for vendor assets, network-first for pages)
- **Meta tags** in `base.html`: `<link rel="manifest">`, `theme-color`, `apple-mobile-web-app-capable`
- **Service worker registration** in `specivo.js` (not inline script)

The manifest reflects the current `brand_name` setting without rebuilding.

### 7. Markdown Rendering

User-authored content (wiki pages, issue descriptions, comments) is stored as raw Markdown and rendered server-side:

- **Library:** Python `markdown` with extensions: `fenced_code`, `tables`, `toc`
- **Jinja2 filter:** `{{ content.text | markdown }}` — returns `Markup` (safe HTML)
- **CSS:** `.wiki-content`, `.editor-preview`, `.prose` classes style rendered HTML
- **Code blocks:** `pre code` resets inline `code` styling (background, color) to prevent clash with fenced code blocks

### 8. Pagination

The `pagination` Jinja2 macro in `components/macros.html` renders a three-part pagination bar:

```
1–25 of 43          « 1 [2] »          Show [25 ▾]
(info)              (page links)       (page size)
```

- **`.sp-pagination`** — flexbox container with `space-between`
- **`.sp-pagination-info`** — "1–25 of 43" summary
- **`.sp-pagination-pages`** — page number links with `«` / `»` arrows
- **`.sp-pagination-size`** — `<select>` with 10/25/50 options, JS-driven via `data-pagination-limit`
- Page size select uses `data-` attribute + JS event listener (no inline `onchange`)

### 9. Static Asset Organization

```
specivo/static/
  css/specivo.css              # All custom styles (design tokens + components)
  js/specivo.js                # All custom JS (Alpine components + utilities)
  sw.js                        # Service worker (PWA)
  vendor/                      # Third-party (bootstrap, alpine, htmx) — immutable
  fonts/                       # Self-hosted WOFF2 — immutable
  img/                         # SVG favicon
```

No build step. Versioned copies (`specivo.0.1.6.css`) created at startup for cache busting. Vendor files use version in filename (`bootstrap.5.3.6.min.css`).

### 9. Template Resolution Order

1. Custom theme from `data/themes/{name}/` (user-provided, optional)
2. Built-in theme from `specivo/templates/themes/{name}/` (if not "default")
3. Default theme `specivo/templates/themes/default/` (always present)
4. Custom error pages from `data/errors/` (optional: 403.html, 404.html, 500.html)
5. Shared templates `specivo/templates/_shared/` (error pages, email templates)

Default templates are baked into the Docker image. Custom themes/errors are in the external data mount. Missing directories are silently skipped.

### 10. Brand Customization

The `brand_name` DB setting (default: "Specivo") replaces hardcoded product name in:
- Sidebar brand text
- Login page heading
- All page `<title>` tags (via `{{ brand_name }}` template variable)
- PWA manifest (`/manifest.json`)

Cached in memory at startup, updated instantly when admin changes it. "Powered by Specivo" footer keeps the product name (not the instance brand).

## Consequences

**Positive:**
- Zero build complexity — no Node.js, no npm audit, no bundler config
- All JS in one searchable file — easy to debug and test
- No Bootstrap class collisions — `sp-` prefix is unambiguous
- CSP-compliant — no inline scripts, safe deployment
- PWA installable with dynamic branding

**Negative:**
- `specivo.js` will grow. When it exceeds ~1000 lines, split into modules and concatenate with esbuild (deferred — no build step until needed)
- `unsafe-eval` in CSP required by Alpine.js (Alpine CSP build would fix this but forces all inline expressions into JS)
- Single CSS file — no component-level scoping (mitigated by `sp-` prefix convention)

## Not Chosen

- **React/Vue/Svelte SPA** — too heavy for server-rendered admin UI
- **CSS Modules / Tailwind** — adds build step, Tailwind requires Node.js
- **Bootstrap JS components** — modals, dropdowns replaced with Alpine.js for lighter weight
- **Syntax highlighting (Prism/Highlight.js)** — deferred, plain `<pre><code>` styling sufficient for now
