/* AI usage charts (My AI usage and the spend reports): chart shapes for the shared chart setup of spend-chart.js.

   The page (pages/ai_usage/my.html) holds one <div data-sp-spend-chart='{...}'>
   per chart, whose data has a "kind" and every label already translated
   (period labels and tooltip headings, series names, the "N entries without a
   price" notes). Mounting, sizing, theme, tooltips and htmx handling are those
   of modules/spend-chart.js; this module only turns the data into ECharts
   options:

   - usage-time:   API-equivalent value per period, stacked by tool or by model
                   (on the spend reports also by user or project; the chart's
                   mode, set by a switch), with spend as a line when there is
                   any, and seat cost as a dashed line where the reader may see it. Periods with unpriced entries get a marked axis
                   label and a tooltip note, so they never just look cheap.
   - usage-tokens: token counts per period, one stacked series per class.
   - usage-share:  API-equivalent value (and spend) per group, horizontal bars.

   Tooltips are DOM nodes filled with textContent, never HTML strings. */

import { axisStyle, groupOption, registerShape, tooltipBox, tooltipRow } from './spend-chart';

/**
 * @typedef {Object} Period
 * @property {string} label  Axis label.
 * @property {string} title  Tooltip heading.
 * @property {string} note   Warning line for the tooltip ('' when none), e.g. "2 entries without a price".
 */

/**
 * Number formatters for token counts: a full one for tooltips and a compact one for axis ticks.
 * @param {string|undefined} locale
 * @returns {{full: (v: number) => string, axis: (v: number) => string}}
 */
export function countFormatters(locale) {
    const tag = locale ? String(locale).replace(/_/g, '-') : undefined;
    const make = (options) => {
        try {
            const fmt = new Intl.NumberFormat(tag, options);
            return (v) => fmt.format(v);
        } catch (e) {
            return (v) => String(Math.round(v));
        }
    };
    return { full: make({}), axis: make({ notation: 'compact', maximumFractionDigits: 1 }) };
}

/**
 * A tooltip warning line (unpriced or unconverted entries in the period).
 * @param {string} text
 * @returns {HTMLElement}
 */
function tooltipNote(text) {
    const note = document.createElement('div');
    note.className = 'sp-chart-tooltip-note';
    note.textContent = text;
    return note;
}

/** @param {{periods?: Period[]}} data */
function hasPeriods(data) {
    return Array.isArray(data.periods) && data.periods.length > 0;
}

/**
 * The category axis of a time chart: labels turned when crowded, marked (with
 * an asterisk in the warning colour) on periods that carry a note.
 * @param {Period[]} periods
 * @param {import('./spend-chart').ChartTheme} theme
 * @param {number} width
 */
function periodAxis(periods, theme, width) {
    const axis = axisStyle(theme);
    const longest = Math.max(...periods.map((p) => p.label.length + (p.note ? 2 : 0)));
    const crowded = periods.length * (longest * 7 + 16) > width - 80;
    return {
        type: 'category',
        data: periods.map((p) => p.label),
        ...axis,
        axisLabel: {
            ...axis.axisLabel,
            hideOverlap: true,
            rotate: crowded ? 45 : 0,
            formatter: (value, index) => (periods[index] && periods[index].note ? `{warn|${value} *}` : value),
            rich: { warn: { color: theme.warning, fontWeight: 'bold', fontFamily: theme.font } },
        },
        splitLine: { show: false },
    };
}

/**
 * The tooltip of a time chart: the period heading, one line per non-zero
 * series, an optional total, and the period's note.
 * @param {Period[]} periods
 * @param {(v: number) => string} format
 * @param {{label: string, skip?: Set<number>}|null} total  Sum of the series (those in *skip* left out), when set.
 */
function periodTooltip(periods, format, total) {
    return {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (list) => {
            const items = Array.isArray(list) ? list : [list];
            if (!items.length) return '';
            const period = periods[items[0].dataIndex] || { title: items[0].name, note: '' };
            const rows = [];
            let sum = 0;
            let parts = 0;
            items.forEach((item) => {
                const value = Number(item.value) || 0;
                if (!value) return;
                rows.push(tooltipRow(item.color, item.seriesName, format(value)));
                if (total && !(total.skip && total.skip.has(item.seriesIndex))) {
                    sum += value;
                    parts += 1;
                }
            });
            if (total && parts > 1) rows.push(tooltipRow('transparent', total.label, format(sum)));
            const box = tooltipBox(period.title, rows);
            if (period.note) box.append(tooltipNote(period.note));
            return box;
        },
    };
}

/** API-equivalent value over time, stacked by tool or model, spend as a line. */
registerShape('usage-time', {
    valid: (data) => hasPeriods(data) && !!data.stacks,
    build(state, theme, width, fmt) {
        const data = state.data;
        const mode = data.stacks[state.mode] ? state.mode : Object.keys(data.stacks)[0];
        const stack = data.stacks[mode] || { names: [], values: [] };
        const axis = axisStyle(theme);
        // Stable colours (palette slots) when the data names them: the report tables and
        // person cards on the page use the same slots, so a group keeps its colour.
        const colors = Array.isArray(stack.colors) ? stack.colors : null;
        const series = stack.names.map((name, i) => {
            const item = {
                type: 'bar',
                name,
                stack: 'api',
                data: stack.values[i].map((v) => v || null),
                barMaxWidth: 48,
            };
            if (colors && Number.isInteger(colors[i])) {
                item.itemStyle = { color: theme.palette[colors[i] % theme.palette.length] };
            }
            return item;
        });
        const skip = new Set();
        if (Array.isArray(data.spend)) {
            skip.add(series.length);
            series.push({
                type: 'line',
                name: data.spend_label,
                data: data.spend,
                symbol: 'circle',
                symbolSize: 5,
                z: 5,
                lineStyle: { width: 2, color: theme.text },
                itemStyle: { color: theme.text },
            });
        }
        if (Array.isArray(data.seat)) {
            // Spend reports: the seat cost alone, dashed, under the spend line that includes it.
            skip.add(series.length);
            series.push({
                type: 'line',
                name: data.seat_label,
                data: data.seat,
                symbol: 'none',
                z: 4,
                lineStyle: { width: 2, type: 'dashed', color: theme.muted },
                itemStyle: { color: theme.muted },
            });
        }
        return {
            height: width < 480 ? 280 : 320,
            option: {
                xAxis: periodAxis(data.periods, theme, width),
                yAxis: { type: 'value', ...axis, axisLabel: { ...axis.axisLabel, formatter: fmt.axis } },
                series,
                tooltip: periodTooltip(data.periods, fmt.full, { label: data.total_label, skip }),
            },
        };
    },
});

/** Token counts over time, one stacked series per class. */
registerShape('usage-tokens', {
    valid: (data) => hasPeriods(data) && Array.isArray(data.series) && data.series.length > 0,
    build(state, theme, width) {
        const data = state.data;
        const counts = countFormatters(data.locale);
        const axis = axisStyle(theme);
        return {
            height: width < 480 ? 240 : 280,
            option: {
                xAxis: periodAxis(data.periods, theme, width),
                yAxis: { type: 'value', ...axis, axisLabel: { ...axis.axisLabel, formatter: counts.axis } },
                series: data.series.map((s) => ({
                    type: 'bar',
                    name: s.name,
                    stack: 'tokens',
                    data: s.values.map((v) => v || null),
                    barMaxWidth: 48,
                })),
                tooltip: periodTooltip(data.periods, counts.full, data.total_label ? { label: data.total_label } : null),
            },
        };
    },
});

/** API-equivalent value (and spend) per group: the spend report's group chart. */
registerShape('usage-share', {
    valid: (data) => Array.isArray(data.points) && data.points.length > 0,
    build(state, theme, width, fmt) {
        return groupOption(state.data, state.labels, theme, width, fmt);
    },
});
