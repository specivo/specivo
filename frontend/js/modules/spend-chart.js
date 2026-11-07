/* AI spend report charts (Apache ECharts, SVG renderer).

   The report page holds <div data-sp-spend-chart='{...}'> with the chart data
   (spend and API-equivalent value per row, converted amounts only, plus the
   reporting currency, the period and the reader's locale) and translated
   series names in data-label-* attributes. This module is bundled on its own
   (spend-chart.min.js, see frontend/build.js), and only the report pages load
   it, so ECharts never weighs on other pages. It draws:

   - with a period: spend per period bucket, stacked by group;
   - without: spend and API-equivalent value side by side per group.

   Colours, text and font come from the app's CSS custom properties, read at
   draw time, so the charts follow the theme and redraw when it changes. The
   table under the chart holds the same numbers, so a chart that fails to draw
   is hidden and nothing is missing. No inline script and no eval: data comes
   from attributes, and tooltips are built as DOM nodes with textContent, so
   group names (models, projects, people) are never parsed as HTML. */

import { init } from '../lib/echarts';

/** @type {string} */
export const SELECTOR = '[data-sp-spend-chart]';

/** Series colours used when a theme does not define --sp-chart-N. */
const DEFAULT_PALETTE = ['#c49a3c', '#2563EB', '#16A34A', '#7C3AED', '#EA580C', '#0891B2', '#DB2777', '#6B7280'];

/** Attributes whose change may switch the theme (same list as modules/mermaid.js). */
const THEME_ATTRS = ['class', 'style', 'data-theme', 'data-bs-theme'];

/**
 * @typedef {Object} SpendPoint
 * @property {string} period  Period bucket label ('' without a period).
 * @property {string} label   Group label, already translated and joined.
 * @property {number} spend   Spend in the reporting currency.
 * @property {number} api     API-equivalent value in the reporting currency.
 */

/**
 * @typedef {Object} ChartData
 * @property {string} currency  ISO 4217 code of the reporting currency.
 * @property {string} period    'day' | 'week' | 'month' | ''.
 * @property {SpendPoint[]} points
 * @property {string} [locale]  The reader's locale, e.g. 'en' or 'pt_BR'.
 */

/**
 * @typedef {Object} ChartTheme
 * @property {string} text
 * @property {string} muted
 * @property {string} border
 * @property {string} surface
 * @property {string} font
 * @property {string[]} palette
 */

/**
 * @typedef {Object} Mounted
 * @property {import('echarts/core').ECharts} chart
 * @property {ChartData} data
 * @property {{spend: string, api: string}} labels
 * @property {string} description  The element's aria-label, kept as the chart's aria description.
 * @property {Object<string, boolean>} selected  Legend selection, kept across redraws.
 * @property {string} themeKey
 * @property {number} width
 * @property {ResizeObserver|null} observer
 * @property {Function|null} onWindowResize
 */

/** @type {Map<Element, Mounted>} */
const mounted = new Map();
let themeWatchers = false;

/**
 * Read one CSS custom property, with a fallback for themes that leave it out.
 * @param {CSSStyleDeclaration} style
 * @param {string} name
 * @param {string} fallback
 * @returns {string}
 */
function cssVar(style, name, fallback) {
    const value = style.getPropertyValue(name).trim();
    return value || fallback;
}

/**
 * The chart colours of the theme in effect on *el*.
 * @param {Element} el
 * @returns {ChartTheme}
 */
export function readTheme(el) {
    const style = getComputedStyle(el);
    return {
        text: cssVar(style, '--sp-text-primary', '#1a1a2e'),
        muted: cssVar(style, '--sp-text-secondary', '#4a4a5a'),
        border: cssVar(style, '--sp-border', '#e5e3dc'),
        surface: cssVar(style, '--sp-bg-elevated', '#ffffff'),
        font: cssVar(style, '--sp-font-body', 'system-ui, sans-serif'),
        palette: DEFAULT_PALETTE.map((fallback, i) => cssVar(style, `--sp-chart-${i + 1}`, fallback)),
    };
}

/**
 * Money formatters for the reporting currency: a full one for tooltips and a
 * compact one for axis ticks. Falls back to "<amount> <code>" when the
 * browser does not know the locale or the currency.
 * @param {string|undefined} locale
 * @param {string} currency
 * @returns {{full: (v: number) => string, axis: (v: number) => string}}
 */
export function moneyFormatters(locale, currency) {
    const tag = locale ? String(locale).replace(/_/g, '-') : undefined;
    const make = (options, digits) => {
        try {
            const fmt = new Intl.NumberFormat(tag, { style: 'currency', currency, ...options });
            return (v) => fmt.format(v);
        } catch (e) {
            return (v) => `${Number(v).toFixed(digits)} ${currency}`;
        }
    };
    return {
        full: make({}, 2),
        axis: make({ notation: 'compact', maximumFractionDigits: 1 }, 0),
    };
}

/**
 * Values in first-seen order, without repeats.
 * @param {string[]} values
 * @returns {string[]}
 */
function unique(values) {
    return Array.from(new Set(values));
}

/**
 * One tooltip line: colour marker, series name, amount.
 * @param {string} color
 * @param {string} name
 * @param {string} amount
 * @returns {HTMLElement}
 */
function tooltipRow(color, name, amount) {
    const row = document.createElement('div');
    row.className = 'sp-chart-tooltip-row';
    const marker = document.createElement('span');
    marker.className = 'sp-chart-tooltip-marker';
    marker.style.backgroundColor = color;
    const label = document.createElement('span');
    label.className = 'sp-chart-tooltip-name';
    label.textContent = name;
    const value = document.createElement('span');
    value.className = 'sp-chart-tooltip-value';
    value.textContent = amount;
    row.append(marker, label, value);
    return row;
}

/**
 * A tooltip: a heading (period or group) over one line per series.
 * @param {string} heading
 * @param {HTMLElement[]} rows
 * @returns {HTMLElement}
 */
function tooltipBox(heading, rows) {
    const box = document.createElement('div');
    box.className = 'sp-chart-tooltip';
    const head = document.createElement('div');
    head.className = 'sp-chart-tooltip-heading';
    head.textContent = heading;
    box.append(head, ...rows);
    return box;
}

/**
 * Axis settings both chart shapes share.
 * @param {ChartTheme} theme
 */
function axisStyle(theme) {
    return {
        axisLine: { lineStyle: { color: theme.border } },
        axisTick: { lineStyle: { color: theme.border } },
        axisLabel: { color: theme.muted, fontFamily: theme.font },
        splitLine: { lineStyle: { color: theme.border } },
    };
}

/**
 * Spend per period bucket, one stacked bar series per group.
 * @param {ChartData} data
 * @param {ChartTheme} theme
 * @param {number} width
 * @param {ReturnType<typeof moneyFormatters>} fmt
 */
function periodOption(data, theme, width, fmt) {
    const periods = unique(data.points.map((p) => p.period));
    const groups = unique(data.points.map((p) => p.label));
    const spend = new Map(data.points.map((p) => [`${p.period}\u0000${p.label}`, p.spend]));
    // Dataset rows: [period, spend of group 0, spend of group 1, ...]. The
    // dimension names are positional, so a group called "period" cannot clash.
    const source = periods.map((period) => [period, ...groups.map((g) => spend.get(`${period}\u0000${g}`) ?? null)]);
    const axis = axisStyle(theme);
    // Turn the period labels when they would not fit side by side (about 7 px
    // per character plus a gap, against the plot width beside the value axis).
    const longest = Math.max(...periods.map((p) => p.length));
    const crowded = periods.length * (longest * 7 + 16) > width - 80;
    return {
        height: width < 480 ? 280 : 320,
        option: {
            dataset: { dimensions: ['period', ...groups.map((_, i) => `g${i}`)], source },
            xAxis: {
                type: 'category',
                ...axis,
                axisLabel: { ...axis.axisLabel, hideOverlap: true, rotate: crowded ? 45 : 0 },
                splitLine: { show: false },
            },
            yAxis: {
                type: 'value',
                ...axis,
                axisLabel: { ...axis.axisLabel, formatter: fmt.axis },
            },
            series: groups.map((name, i) => ({
                type: 'bar',
                name,
                stack: 'spend',
                encode: { x: 0, y: i + 1 },
                barMaxWidth: 48,
            })),
            tooltip: {
                trigger: 'item',
                formatter: (params) => {
                    if (params.componentType !== 'series') return tooltipBox(params.name, []);
                    const amount = fmt.full(params.value[params.seriesIndex + 1] || 0);
                    return tooltipBox(params.name, [tooltipRow(params.color, params.seriesName, amount)]);
                },
            },
        },
    };
}

/**
 * Spend and API-equivalent value side by side per group (horizontal bars).
 * @param {ChartData} data
 * @param {Mounted['labels']} labels
 * @param {ChartTheme} theme
 * @param {number} width
 * @param {ReturnType<typeof moneyFormatters>} fmt
 */
function groupOption(data, labels, theme, width, fmt) {
    const points = data.points;
    const axis = axisStyle(theme);
    return {
        height: Math.max(160, points.length * 52 + 72),
        option: {
            dataset: {
                dimensions: ['label', 'spend', 'api'],
                source: points.map((p) => [p.label, p.spend, p.api]),
            },
            xAxis: {
                type: 'value',
                ...axis,
                axisLabel: { ...axis.axisLabel, formatter: fmt.axis, hideOverlap: true },
            },
            yAxis: {
                type: 'category',
                inverse: true,
                ...axis,
                axisTick: { show: false },
                // Long group names are cut with an ellipsis; the tooltip and
                // the table show them in full.
                axisLabel: { ...axis.axisLabel, width: Math.min(220, Math.round(width * 0.35)), overflow: 'truncate' },
                splitLine: { show: false },
            },
            series: [
                { type: 'bar', name: labels.spend, encode: { y: 'label', x: 'spend' }, barMaxWidth: 18 },
                { type: 'bar', name: labels.api, encode: { y: 'label', x: 'api' }, barMaxWidth: 18 },
            ],
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'shadow' },
                formatter: (list) => {
                    const items = Array.isArray(list) ? list : [list];
                    if (!items.length) return '';
                    if (items[0].componentType !== 'series') return tooltipBox(items[0].name, []);
                    const point = points[items[0].dataIndex];
                    return tooltipBox(point.label, items.map((item) => tooltipRow(
                        item.color,
                        item.seriesName,
                        fmt.full(item.seriesIndex === 0 ? point.spend : point.api),
                    )));
                },
            },
        },
    };
}

/**
 * The full ECharts option and the chart height for one mounted chart.
 * @param {Mounted} state
 * @param {ChartTheme} theme
 * @param {number} width
 * @param {boolean} animate
 * @returns {{height: number, option: Object}}
 */
export function buildOption(state, theme, width, animate) {
    const { data, labels } = state;
    const fmt = moneyFormatters(data.locale, data.currency);
    const shape = data.period ? periodOption(data, theme, width, fmt) : groupOption(data, labels, theme, width, fmt);
    const option = {
        ...shape.option,
        animation: animate,
        animationDuration: 300,
        color: theme.palette,
        textStyle: { color: theme.text, fontFamily: theme.font },
        aria: { enabled: true, label: { description: state.description } },
        legend: {
            type: 'scroll',
            top: 0,
            left: 0,
            right: 0,
            selected: state.selected,
            // Long group names are cut with an ellipsis, and the legend
            // tooltip shows them in full; items past the width scroll.
            textStyle: {
                color: theme.text,
                fontFamily: theme.font,
                width: Math.max(80, Math.min(240, Math.round(width * 0.45))),
                overflow: 'truncate',
            },
            tooltip: { show: true, formatter: (params) => tooltipBox(params.name, []) },
            inactiveColor: theme.border,
            pageIconColor: theme.muted,
            pageIconInactiveColor: theme.border,
            pageTextStyle: { color: theme.muted },
        },
        // Axis labels are kept inside the grid box, so none is ever clipped
        // at the chart's edge.
        grid: {
            left: 4,
            right: 16,
            top: 40,
            bottom: 4,
            outerBoundsMode: 'same',
            outerBoundsContain: 'axisLabel',
        },
        tooltip: {
            ...shape.option.tooltip,
            confine: true,
            backgroundColor: theme.surface,
            borderColor: theme.border,
            textStyle: { color: theme.text, fontFamily: theme.font },
        },
    };
    return { height: shape.height, option };
}

/**
 * Draw (or redraw) one mounted chart at its current width and theme.
 * @param {Element} el
 * @param {Mounted} state
 * @param {boolean} animate
 */
function render(el, state, animate) {
    const theme = readTheme(el);
    const width = el.clientWidth || 640;
    const { height, option } = buildOption(state, theme, width, animate);
    state.themeKey = JSON.stringify(theme);
    state.width = width;
    if (el.style.height !== `${height}px`) el.style.height = `${height}px`;
    state.chart.resize({ width, height });
    state.chart.setOption(option, { notMerge: true });
}

/**
 * Parse the chart attributes of *el*; null when there is nothing to draw.
 * @param {Element} el
 * @returns {ChartData|null}
 */
function readData(el) {
    let data;
    try {
        data = JSON.parse(el.getAttribute('data-sp-spend-chart') || '{}');
    } catch (e) {
        return null;
    }
    if (!data || !Array.isArray(data.points) || !data.points.length || !data.currency) return null;
    return data;
}

/**
 * Draw the chart of one element. Safe to call twice.
 * @param {Element} el
 */
export function mountSpendChart(el) {
    if (mounted.has(el)) return;
    const data = readData(el);
    if (!data) return;
    try {
        const state = {
            chart: init(el, null, { renderer: 'svg' }),
            data,
            labels: {
                spend: el.getAttribute('data-label-spend') || 'Spend',
                api: el.getAttribute('data-label-api') || 'API-equivalent value',
            },
            description: el.getAttribute('aria-label') || '',
            selected: {},
            themeKey: '',
            width: 0,
            observer: null,
            onWindowResize: null,
        };
        state.chart.on('legendselectchanged', (event) => {
            state.selected = { ...event.selected };
        });
        mounted.set(el, state);
        render(el, state, true);
        watchSize(el, state);
        watchTheme();
        el.setAttribute('data-sp-drawn', '');
    } catch (e) {
        unmountSpendChart(el);
        el.hidden = true;
    }
}

/**
 * Dispose of the chart of one element and stop watching it.
 * @param {Element} el
 */
export function unmountSpendChart(el) {
    const state = mounted.get(el);
    mounted.delete(el);
    if (!state) return;
    if (state.observer) state.observer.disconnect();
    if (state.onWindowResize) window.removeEventListener('resize', state.onWindowResize);
    state.chart.dispose();
    el.removeAttribute('data-sp-drawn');
}

/**
 * Redraw a chart when its width changes; the height follows from the width.
 * @param {Element} el
 * @param {Mounted} state
 */
function watchSize(el, state) {
    let frame = 0;
    const onResize = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
            frame = 0;
            if (!mounted.has(el)) return;
            if (el.clientWidth && el.clientWidth !== state.width) render(el, state, false);
        });
    };
    if (typeof ResizeObserver === 'function') {
        state.observer = new ResizeObserver(onResize);
        state.observer.observe(el);
    } else {
        state.onWindowResize = onResize;
        window.addEventListener('resize', onResize);
    }
}

/** Redraw every chart whose theme colours changed. */
function redrawOnThemeChange() {
    mounted.forEach((state, el) => {
        if (JSON.stringify(readTheme(el)) !== state.themeKey) render(el, state, false);
    });
}

/** Watch the theme switches once per page: root attributes and the OS colour scheme. */
function watchTheme() {
    if (themeWatchers) return;
    themeWatchers = true;
    const observer = new MutationObserver(redrawOnThemeChange);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: THEME_ATTRS });
    if (document.body) observer.observe(document.body, { attributes: true, attributeFilter: THEME_ATTRS });
    if (window.matchMedia) {
        const query = window.matchMedia('(prefers-color-scheme: dark)');
        if (query.addEventListener) query.addEventListener('change', redrawOnThemeChange);
    }
}

/** Dispose of charts whose element left the document (an htmx swap replaced it). */
function sweep() {
    mounted.forEach((_, el) => {
        if (!el.isConnected) unmountSpendChart(el);
    });
}

/**
 * Draw every chart under *root*.
 * @param {ParentNode} [root=document]
 */
export function initSpendCharts(root = document) {
    if (root instanceof Element && root.matches(SELECTOR)) mountSpendChart(root);
    root.querySelectorAll(SELECTOR).forEach(mountSpendChart);
}

/**
 * Hook the charts into htmx, so content swapped in later is drawn and charts
 * swapped out are disposed of. Harmless on pages without htmx.
 */
export function watchHtmx() {
    document.addEventListener('htmx:load', (event) => {
        sweep();
        const elt = event.detail && event.detail.elt;
        if (elt && elt.querySelectorAll) initSpendCharts(elt);
    });
    document.addEventListener('htmx:beforeCleanupElement', (event) => {
        const elt = event.target;
        if (!(elt instanceof Element)) return;
        if (mounted.has(elt)) unmountSpendChart(elt);
        mounted.forEach((_, el) => {
            if (elt.contains(el)) unmountSpendChart(el);
        });
    });
    document.addEventListener('htmx:afterSettle', sweep);
}
