# Specivo frontend build

Source for Specivo's custom CSS and JavaScript. esbuild bundles, minifies, and
content-hashes the sources into committed artifacts under
`../specivo/static/dist/`, which the FastAPI app serves at runtime. The runtime
never needs Node — only this build step does.

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

Outputs (committed): `specivo/static/dist/{js,css}/*.min.<hash>.{js,css}` plus a
`manifest.json` per directory mapping logical name -> hashed name. The app reads
those manifests at startup.

## Workflow

```
cd frontend
npm install        # once
npm run build      # production: minified + hashed
npm run watch      # rebuild on change (dev: unminified + sourcemaps)
```

After editing anything under `frontend/`, run `npm run build` and **commit the
regenerated `specivo/static/dist/` artifacts** along with your source changes. CI
rebuilds and fails if the committed artifacts are stale.

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
