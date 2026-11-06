# Observable Plot and D3 — vendored

Observable Plot draws the charts of the AI spend reports (project "Spend
report" and the admin "AI spend" page). Plot's UMD build needs D3 as the
global `d3`, so D3 is vendored next to it.

- Upstream: https://github.com/observablehq/plot and https://github.com/d3/d3
- Licenses: ISC (see `LICENSES/observable-plot.ISC.txt` and `LICENSES/d3.ISC.txt`)
- Vendored versions: Plot **0.6.16**, D3 **7.9.0** (the version Plot's UMD
  header names)
- Sources: https://cdn.jsdelivr.net/npm/@observablehq/plot@0.6.16/dist/plot.umd.min.js
  and https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js (`LICENSE` from the
  same package)

## Files

| File                         | Size     | SHA-256                                                            |
|------------------------------|----------|--------------------------------------------------------------------|
| `observable-plot.0.6.min.js` | 207408 B | `ee8e99124680704d3ea4878bdb786362c70a412b7b9eb988ae852827fd6e0711` |
| `d3.7.9.0.min.js`            | 279706 B | `f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539` |

## How they are loaded

Not on every page. `frontend/js/modules/spend-chart.js` looks for a
`[data-sp-spend-chart]` element; only then does it add the D3 and then the Plot
`<script>` (paths from the element's `data-d3-src` / `data-plot-src`), and
draws from the JSON in `data-sp-spend-chart`. No inline script; the table under
the chart holds the same numbers. Neither build makes network requests, so the
charts work on air-gapped installs.

## Notes

- D3's `d3-dsv` builds a row converter with `new Function` when it parses CSV
  with an inferred column list. The charts never parse CSV, so it never runs;
  Plot itself has no `eval` or `new Function`.
- Plot writes a `<style>` element into each figure, which the existing
  `style-src 'self' 'unsafe-inline'` allows.

## Updating

1. Download the two files above for the new versions and their `LICENSE` files.
2. Keep D3 at the version Plot's UMD header names (`require("d3@<v>/...")`).
3. Update the versions, sizes and SHA-256 here and the paths in
   `pages/ai_usage/report.html`.
