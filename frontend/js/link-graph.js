/* ============================================================
   Link graph entry — bundled on its own into link-graph.min.js.

   Holds Apache ECharts' graph series (tree-shaken, see
   lib/echarts-graph.js), so only the graph views load it: the
   project's Graph page from its chart_js block, the issue's Graph
   tab and the wiki page's Graph block when first opened
   (modules/link-graph-loader.js in app.min.js).
   ============================================================ */
import { initLinkGraphs } from './modules/link-graph';

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initLinkGraphs());
} else {
    initLinkGraphs();
}
