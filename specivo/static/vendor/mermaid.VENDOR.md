# Mermaid — vendored

Mermaid draws diagrams (flowcharts, sequence, class, state, Gantt, pie, …)
from a text description. Used in Specivo for ```` ```mermaid ```` fences in
wiki pages, issue descriptions, comments and the editor preview.

- Upstream: https://github.com/mermaid-js/mermaid
- License: MIT (see `LICENSES/mermaid.MIT.txt`)
- Vendored version: **11.17.2** (latest 11.x release)
- Source: https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz, file
  `package/dist/mermaid.min.js` (the self-contained IIFE build that sets
  `window.mermaid`; same file as
  https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.min.js)

## Files

| File                     | Size      | SHA-256                                                            |
|--------------------------|-----------|--------------------------------------------------------------------|
| `mermaid.11.17.2.min.js` | 3572661 B | `581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8` |

## How it is loaded

Not on every page. `base.html` names the file in `<meta name="sp-mermaid">`,
and `frontend/js/modules/mermaid.js` adds the `<script>` only once the page
holds a `pre.sp-mermaid` block (the server's rendering of a mermaid fence),
including blocks added later by htmx or the editor preview. The build makes no
network requests of its own, so it works on air-gapped installs.

## Configuration notes

- **`securityLevel: 'strict'`** and **`htmlLabels: false`**: labels are SVG
  text, click callbacks are off, and the output goes through the library's
  bundled DOMPurify.
- **`secure`** lists the settings a diagram's own `%%{init}%%` directive or
  front matter may not change (security level, HTML labels, flowchart
  settings, the DOMPurify config, theme and theme CSS).
- **`startOnLoad: false`**: the module draws each block itself.
- The build uses no `eval` or `new Function`; the CSP needs no
  `unsafe-eval` for it. It writes a `<style>` element inside each SVG, which
  the existing `style-src 'self' 'unsafe-inline'` allows.

## Updating

1. `curl -fsSL -o mermaid.tgz https://registry.npmjs.org/mermaid/-/mermaid-<v>.tgz && tar xzf mermaid.tgz`
2. Copy `package/dist/mermaid.min.js` to `mermaid.<v>.min.js` and
   `package/LICENSE` to `LICENSES/mermaid.MIT.txt`.
3. Update the version, size and SHA-256 above (`sha256sum mermaid.<v>.min.js`).
4. Update the `<meta name="sp-mermaid">` path in `base.html`.
5. Check the build still sets `window.mermaid`, has no `eval`/`new Function`
   and no CDN URLs (`grep -c 'new Function\|eval(\|jsdelivr'`).
