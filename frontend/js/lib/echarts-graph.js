/* Apache ECharts, tree-shaken to what the link graph view uses.

   The graph series (force layout), the tooltip and aria components and the
   SVG renderer. A module of its own, not lib/echarts.js: registering the
   graph chart there would put it in the spend chart bundle too, and the bar
   and line charts would land in this one. Import ECharts for the graph view
   through this module only.

   Licences: as lib/echarts.js (ECharts Apache-2.0, zrender BSD-3-Clause,
   tslib 0BSD; see specivo/static/vendor/LICENSES/). */

import { init, use } from 'echarts/core';
import { GraphChart } from 'echarts/charts';
import { AriaComponent, TooltipComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';

use([GraphChart, AriaComponent, TooltipComponent, SVGRenderer]);

// Named exports only, as in lib/echarts.js.
export { init };
