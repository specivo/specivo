/* ============================================================
   Spend chart entry — bundled on its own into spend-chart.min.js.

   Holds Apache ECharts (tree-shaken, see lib/echarts.js), so only
   the pages with charts load it (the AI spend reports, report.html,
   and My AI usage, my.html; their chart_js block).
   The script is deferred, so the document is parsed when it runs.
   ============================================================ */
import { initSpendCharts, watchHtmx, watchSwitches } from './modules/spend-chart';
import './modules/usage-charts';

watchHtmx();
watchSwitches();
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initSpendCharts());
} else {
    initSpendCharts();
}
