# Font Awesome 4 — vendored

Font Awesome 4 is required by EasyMDE: every toolbar button is rendered with
class names like `fa fa-bold`, `fa fa-italic`, `fa fa-eye`, etc. Without an
external Font Awesome stylesheet the toolbar buttons render with no glyphs
(the `::before` content is empty). EasyMDE explicitly does not bundle the
icons — it expects the host page to provide them.

We vendor a minimal Font Awesome 4.7.0 subset (CSS + the WOFF2 font file
only) so the editor toolbar works without any CDN call. CSP `font-src 'self'`
is preserved.

- Upstream: https://github.com/FortAwesome/Font-Awesome (4.x archive)
- Vendored version: **4.7.0** (final 4.x release; matches the class names
  hard-coded in EasyMDE 2.18.0)
- License (font): SIL OFL 1.1 — `LICENSES/font-awesome.OFL-MIT.txt`
- License (CSS):  MIT          — `LICENSES/font-awesome.OFL-MIT.txt`

## Files

Files live under a versioned subdirectory because the CSS references the
font with a relative path (`../fonts/...`). Keeping the original layout
avoids editing absolute URLs inside the minified CSS.

| File                                              | Size     | SHA-256                                                            |
|---------------------------------------------------|----------|--------------------------------------------------------------------|
| `font-awesome/4.7.0/css/font-awesome.min.css`     |  30653 B | `cfca43d9c70d8cb1d16e27814cb886d2b423cba17fb0e70b4ec5fe10e5f40202` |
| `font-awesome/4.7.0/fonts/fontawesome-webfont.woff2` |  77160 B | `2adefcbc041e7d18fcf2d417879dc5a09997aa64d675b7a3c4b6ce33da13f3fe` |

## Modifications from upstream

The shipped `font-awesome.min.css` has its `@font-face` block rewritten to
reference only the WOFF2 file. Upstream lists EOT/WOFF/TTF/SVG fallbacks too,
but every browser supported by Specivo speaks WOFF2, so the other formats
would only generate 404s for fonts we do not vendor.

Original block:
```
src:url('../fonts/fontawesome-webfont.eot?v=4.7.0');
src:url('../fonts/fontawesome-webfont.eot?#iefix&v=4.7.0') format('embedded-opentype'),
    url('../fonts/fontawesome-webfont.woff2?v=4.7.0') format('woff2'),
    url('../fonts/fontawesome-webfont.woff?v=4.7.0') format('woff'),
    url('../fonts/fontawesome-webfont.ttf?v=4.7.0') format('truetype'),
    url('../fonts/fontawesome-webfont.svg?v=4.7.0#fontawesomeregular') format('svg');
```

Replacement block (only change):
```
src:url('../fonts/fontawesome-webfont.woff2?v=4.7.0') format('woff2');
```

No other content was changed; class definitions and Unicode private-use code
points are preserved verbatim.

## Updating

1. Bump the version number in this file's table and in the directory name.
2. `curl -fsSL -o /tmp/fa.tgz https://github.com/FortAwesome/Font-Awesome/archive/refs/tags/v<v>.tar.gz`
3. Extract `css/font-awesome.min.css` and `fonts/fontawesome-webfont.woff2`
   into `font-awesome/<v>/css/` and `font-awesome/<v>/fonts/`.
4. Rewrite the `@font-face` block to reference only the WOFF2 file (see above).
5. Update the SHA-256 hashes (`shasum -a 256 ...`).
6. Update the `<link>` href in `specivo/templates/themes/default/base.html`.

## Why FA 4 specifically (not FA 5/6)

EasyMDE 2.18.0 hard-codes class names of the form `fa fa-<name>` (e.g.
`fa fa-bold`, `fa fa-eye`, `fa fa-arrows-alt`). Those are Font Awesome 4
class names. Font Awesome 5 and 6 use a different scheme (`fas fa-bold`,
`fa-solid`, etc.) and renamed several glyphs. Shipping FA 4 keeps the
toolbar working without patching EasyMDE.
