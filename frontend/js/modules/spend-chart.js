/* AI spend report charts (Observable Plot).

   The report page holds <div data-sp-spend-chart='{...}'> with the chart data
   (spend and API-equivalent value per row, converted amounts only) and the
   paths of the vendored D3 and Plot builds. This module loads them only on
   such a page, D3 first (Plot's UMD build reads the global d3), and draws:

   - with a period: spend per period bucket, stacked by group;
   - without: spend and API-equivalent value side by side per group.

   The table under the chart holds the same numbers, so a chart that fails to
   load leaves nothing missing. No inline script: everything comes from data
   attributes. */

var SELECTOR = '[data-sp-spend-chart]';
var loaded = {};

function loadScript(src) {
    if (loaded[src]) return loaded[src];
    loaded[src] = new Promise(function (resolve, reject) {
        var script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.onload = function () { resolve(); };
        script.onerror = function () { reject(new Error('could not load ' + src)); };
        document.head.appendChild(script);
    });
    return loaded[src];
}

function loadPlot(el) {
    if (window.Plot && window.d3) return Promise.resolve(window.Plot);
    return loadScript(el.getAttribute('data-d3-src'))
        .then(function () { return loadScript(el.getAttribute('data-plot-src')); })
        .then(function () {
            if (!window.Plot) throw new Error('Plot did not load');
            return window.Plot;
        });
}

function moneyFormat(locale, currency) {
    try {
        var fmt = new Intl.NumberFormat(locale || undefined, {
            style: 'currency', currency: currency, maximumFractionDigits: 0
        });
        return function (v) { return fmt.format(v); };
    } catch (e) {
        return function (v) { return Math.round(v) + ' ' + currency; };
    }
}

function draw(Plot, el) {
    var data;
    try {
        data = JSON.parse(el.getAttribute('data-sp-spend-chart') || '{}');
    } catch (e) {
        return;
    }
    var points = data.points || [];
    if (!points.length) return;
    var spendLabel = el.getAttribute('data-label-spend') || 'Spend';
    var apiLabel = el.getAttribute('data-label-api') || 'API-equivalent value';
    var fmt = moneyFormat(data.locale, data.currency);
    var width = Math.max(320, el.clientWidth || 640);
    var figure;
    if (data.period) {
        figure = Plot.plot({
            width: width,
            height: 280,
            marginLeft: 64,
            x: { type: 'band', label: null, tickRotate: points.length > 12 ? -45 : 0 },
            y: { grid: true, label: spendLabel + ' (' + data.currency + ')', tickFormat: fmt },
            color: { legend: true },
            marks: [
                Plot.barY(points, {
                    x: 'period', y: 'spend', fill: 'label',
                    title: function (d) { return d.period + ' · ' + d.label + ': ' + fmt(d.spend); }
                }),
                Plot.ruleY([0])
            ]
        });
    } else {
        var rows = [];
        points.forEach(function (d) {
            rows.push({ label: d.label, measure: spendLabel, value: d.spend });
            rows.push({ label: d.label, measure: apiLabel, value: d.api });
        });
        var labels = points.map(function (d) { return d.label; });
        figure = Plot.plot({
            width: width,
            height: Math.max(120, labels.length * 44 + 40),
            marginLeft: Math.min(220, 12 + 7 * Math.max.apply(null, labels.map(function (l) { return l.length; }))),
            x: { grid: true, label: data.currency, tickFormat: fmt },
            y: { axis: null, domain: [spendLabel, apiLabel] },
            fy: { domain: labels, label: null, axis: 'left' },
            color: { legend: true, domain: [spendLabel, apiLabel] },
            marks: [
                Plot.barX(rows, {
                    fy: 'label', y: 'measure', x: 'value', fill: 'measure',
                    title: function (d) { return d.label + ' · ' + d.measure + ': ' + fmt(d.value); }
                }),
                Plot.ruleX([0])
            ]
        });
    }
    el.replaceChildren(figure);
    el.setAttribute('data-sp-drawn', '');
}

export function initSpendChart() {
    var charts = document.querySelectorAll(SELECTOR);
    if (!charts.length) return;
    loadPlot(charts[0]).then(function (Plot) {
        charts.forEach(function (el) {
            try {
                draw(Plot, el);
            } catch (e) {
                el.hidden = true;
            }
        });
    }, function () {
        charts.forEach(function (el) { el.hidden = true; });
    });
}
