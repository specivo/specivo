/* Apache ECharts, tree-shaken to what Specivo's charts use.

   Only the parts registered here end up in the bundle: bar and line charts, the grid,
   tooltip, legend, dataset and aria components, and the SVG renderer (vector
   output, crisp at any zoom, and no <canvas> to read back). Import ECharts
   through this module, never from 'echarts' directly, so another chart cannot
   pull in the full library by accident.

   Licences: ECharts is Apache-2.0 (with a NOTICE and a BSD-3-Clause d3 part),
   its renderer zrender BSD-3-Clause, the tslib helpers 0BSD; see
   specivo/static/vendor/LICENSES/. */

import { init, use } from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import {
    AriaComponent,
    DatasetComponent,
    GridComponent,
    LegendComponent,
    TooltipComponent,
} from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';

use([
    BarChart,
    LineChart,
    AriaComponent,
    DatasetComponent,
    GridComponent,
    LegendComponent,
    TooltipComponent,
    SVGRenderer,
]);

// Named exports only: re-exporting the whole 'echarts/core' namespace would
// keep every core module in the bundle.
export { init };
