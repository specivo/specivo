# EasyMDE — vendored

EasyMDE is a Markdown editor based on CodeMirror 5 (a maintained MIT-licensed
fork of SimpleMDE). Used in Specivo for editing wiki pages and issue
descriptions.

- Upstream: https://github.com/Ionaru/easy-markdown-editor
- License: MIT (see `LICENSES/easymde.MIT.txt`)
- Vendored version: **2.18.0**
- Source: https://cdn.jsdelivr.net/npm/easymde@2.18.0/dist/

## Files

Files are flat in `specivo/static/vendor/`, with the version embedded in the
filename (matches the convention used by the other vendored libraries).

| File                     | Size     | SHA-256                                                            |
|--------------------------|----------|--------------------------------------------------------------------|
| `easymde.2.18.0.min.js`  | 326569 B | `42c578c29ae613807f43c292e23365f2f676071450a8f09314668a27720ccee3` |
| `easymde.2.18.0.min.css` |  12923 B | `8a148c947f7e63250d8fb8d97e030b6fef6e02480ea08c0acfacb11618ac11f6` |

## Configuration notes

- **Always pass `spellChecker: false`** when constructing `EasyMDE`. The
  bundled spell-checker fetches dictionary files from `cdn.jsdelivr.net` via
  `XMLHttpRequest`, which Specivo's CSP blocks. Disabling spell-check avoids
  the request entirely.
- **Always pass `previewRender`** to route preview HTML through the server's
  `/api/v1/markdown/preview/` endpoint. Never let EasyMDE's bundled `marked.js`
  render preview client-side — server rendering is the source of truth and the
  only thing that supports `KEY-123` autolinks, mentions, and other Specivo
  extensions.

## Updating

1. Bump the version in the filenames and the table above.
2. `curl -fsSL -o easymde.<v>.min.js https://cdn.jsdelivr.net/npm/easymde@<v>/dist/easymde.min.js`
3. Same for `easymde.<v>.min.css`.
4. Update the SHA-256 hashes above (`shasum -a 256 easymde.<v>.min.*`).
5. Update the `<link>`/`<script>` paths in `base.html` and any wrappers.
