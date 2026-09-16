# Specivo frontend build

Source for Specivo's custom CSS and JavaScript. esbuild bundles, minifies, and
content-hashes the sources into `../specivo/static/dist/`, which the FastAPI app
serves at runtime. The runtime never needs Node — only this build step does.

`specivo/static/dist/` is **generated and gitignored**. It is built:

- in the Docker image, by the Dockerfile's `frontend` stage;
- in CI, before the backend and E2E test runs;
- in development, by the `frontend` watcher service that `make dev-up` starts.

## Layout

```
frontend/
  build.js              esbuild driver (3 entries -> 3 bundles)
  css/specivo.css       CSS entry: @import list in cascade order
  css/*.css             partials by layer (tokens, base, components, features)
  css/pages/*.css       page-specific partials
  js/alpine-init.js     registers every Alpine.data component + store
  js/app.js             wires the vanilla (non-Alpine) modules on DOMContentLoaded
  js/stores.js          Alpine store factories (notifications, sidebar)
  js/lib/*.js           shared helpers (csrf, anchored-menu)
  js/components/*.js     one Alpine factory per file
  js/features/*/        complex features (markdown editor)
  js/modules/*.js        vanilla initX() modules
```

Outputs (never committed): `specivo/static/dist/{js,css}/*.min.<hash>.{js,css}`
plus a `manifest.json` per directory mapping logical name -> hashed name. The app
reads those manifests at startup and logs a warning when they are missing.

## Workflow

With Docker only (no Node on the host):

```
make dev-up            # starts the watcher container; bundles rebuild on save
make frontend-build    # one-off build (uses a Node container if npm is absent)
```

With Node on the host:

```
cd frontend
npm ci                 # once, or after package-lock.json changes
npm run build          # production: minified + hashed
npm run watch          # rebuild on change (dev: unminified + sourcemaps)
```

Commit only the sources under `frontend/`. Run a build before `make test-e2e`
(the target does it for you) or when you want the bundle-serving integration
tests to run instead of skip.

Watch mode rebuilds the bundles in place under their logical names and writes
manifests that map each name to itself: the app reads manifests only at
startup, so a content hash would go stale on the first rebuild.

## Conventions

- **Adding a CSS partial:** create the file, then `@import` it at the correct
  cascade position in `css/specivo.css`. Cascade order matters — see the header
  comment in that file.
- **Adding an Alpine component:** create `js/components/<name>.js` exporting a
  factory `export function name() { return { ... } }`, then import and register
  it in `js/alpine-init.js`. The registration name string must match the
  `x-data="name(...)"` used in templates.
- **Adding a vanilla module:** create `js/modules/<name>.js` exporting
  `export function initName() { ... }`, then call it in `js/app.js`.
