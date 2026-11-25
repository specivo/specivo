/* The link graph view (Apache ECharts graph series, force layout, SVG renderer).

   A graph is a <div data-sp-link-graph> rendered by pages/_link_graph.html:
   the link kind checkboxes, the "Show suggestions" switch, a status line, the
   drawing area and a list of the same items for keyboard and screen reader
   users. Its data is the link graph JSON of data-endpoint (an issue, a wiki
   page or a project); hub nodes (tags, components, versions, sprints,
   assignees, concepts) are expanded through data-expand-endpoint with the hub as root.

   - Every request names the checked kinds; changing a checkbox reloads.
     "Show suggestions" adds include_suggestions=true, which a server without
     suggested links ignores. An edge with ``suggested: true`` is dashed.
   - Clicking an issue, page, commit or pull request opens it; clicking a hub
     shows its items. The list offers the same with links and buttons.
   - The node count is capped (MAX_NODES) across expansions; a truncated
     response and the cap are both said in the status line.

   Node titles, refs and link texts are stored text: they reach the page only
   as text (textContent, ECharts plain-text labels without rich styles, DOM
   tooltips), never as HTML, and only http(s) or same-site URLs are followed.
   Colours come from CSS custom properties (--sp-graph-*, see
   css/link-graph.css), read at draw time, so the graph follows the theme. */

import { init } from '../lib/echarts-graph';
import { safeNextPath } from '../lib/safe-url';

const SELECTOR = '[data-sp-link-graph]';
/** Nodes at most, over the first read and every hub expansion. */
export const MAX_NODES = 300;
/** Edges drawn at most across expansions: a dense graph beyond this would stall the force layout. */
export const MAX_EDGES = 1500;
/** Above these the layout is computed without animating every step. */
const STATIC_LAYOUT_NODES = 150;
const STATIC_LAYOUT_EDGES = 600;
/** The zoom buttons' step, and the zoom range they keep to. */
const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.2;
const ZOOM_MAX = 4;
/** Neighbours asked of the server: an item reads two hops, a project one. */
const LIMITS = { item: 100, project: 200 };
const HUB_LIMIT = 50;
const HUB_TYPES = new Set(['tag', 'component', 'metadata', 'version', 'sprint', 'user', 'concept']);
const THEME_ATTRS = ['class', 'style', 'data-theme', 'data-bs-theme'];
/** Edge kinds drawn without an arrow: they say "these two belong together", not "this points at that". */
const UNDIRECTED = new Set(['similar']);

/** @type {Map<Element, Object>} */
const mounted = new Map();
let themeWatchers = false;

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

/** The category of a node: its colour and legend entry. */
export function nodeCategory(type) {
    if (type === 'issue' || type === 'wiki_page' || type === 'attachment' || type === 'broken') return type;
    if (type === 'commit' || type === 'pull_request') return 'development';
    if (HUB_TYPES.has(type)) return 'hub';
    return 'development';
}

export function isHub(node) {
    return Boolean(node) && HUB_TYPES.has(node.type);
}

/** A URL the graph may send the reader to: a path on this site (prefixed) or an http(s) URL; else ''. */
export function safeHref(url) {
    if (typeof url !== 'string' || !url) return '';
    if (url.charAt(0) === '/') {
        const path = safeNextPath(url);
        return path ? spUrl(path) : '';
    }
    return /^https?:\/\/[^\s]+$/i.test(url) ? url : '';
}

function shorten(text, size) {
    const value = String(text || '');
    return value.length > size ? `${value.slice(0, size - 1)}…` : value;
}

/** The short text drawn next to a node. */
export function nodeLabel(node) {
    switch (node.type) {
    case 'issue':
        return node.ref;
    case 'pull_request':
        return shorten(node.title.split(' ')[0] || node.ref, 12);
    case 'commit':
        return node.ref.replace(/^commit:/, '').slice(0, 7);
    case 'broken':
        // wiki:ACME/missing-page -> missing-page; an issue key stays as it is.
        return shorten(node.ref.replace(/^wiki:[^/]+\//, ''), 24);
    default:
        if (isHub(node) && typeof node.degree === 'number') return `${shorten(node.title, 22)} (${node.degree})`;
        return shorten(node.title || node.ref, 24);
    }
}

function edgeKey(edge) {
    return [edge.source, edge.target, edge.kind, edge.via || '', edge.relation_type || ''].join('\u0000');
}

/** Fill a message's %(name)s placeholders. */
export function fill(message, values) {
    return String(message || '').replace(/%\((\w+)\)s/g, (match, name) => (name in values ? String(values[name]) : match));
}

/**
 * Add one link graph response to *state*: nodes (first sighting wins, up to
 * MAX_NODES) and the edges between nodes it holds. A link to a missing
 * target gets a "broken" node named by the ref as written. The root of the
 * *main* response (the issue or page the view is about) is drawn as the root;
 * an expanded hub's root is just one more node.
 * @returns {boolean} whether the cap left something out
 */
export function mergeGraph(state, data, main = false) {
    let capped = false;
    const add = (node) => {
        if (!node || typeof node.ref !== 'string') return false;
        const known = state.nodes.get(node.ref);
        if (known) {
            if (typeof node.degree === 'number') known.degree = node.degree;
            return true;
        }
        if (state.nodes.size >= MAX_NODES) {
            capped = true;
            return false;
        }
        state.nodes.set(node.ref, {
            ref: node.ref,
            type: String(node.type || ''),
            title: String(node.title || ''),
            status: node.status ? String(node.status) : '',
            url: node.url ? String(node.url) : '',
            project: node.project ? String(node.project) : '',
            degree: typeof node.degree === 'number' ? node.degree : null,
        });
        return true;
    };
    if (data.root) {
        add(data.root);
        if (main) state.rootRef = data.root.ref;
    }
    (data.nodes || []).forEach(add);
    (data.edges || []).forEach((edge) => {
        if (!edge || typeof edge.source !== 'string' || typeof edge.target !== 'string') return;
        if (edge.broken && !state.nodes.has(edge.target)) {
            add({ ref: edge.target, type: 'broken', title: edge.target });
        }
        if (!state.nodes.has(edge.source) || !state.nodes.has(edge.target)) return;
        const key = edgeKey(edge);
        if (state.edges.has(key)) return;
        if (state.edges.size >= MAX_EDGES) {
            capped = true;
            return;
        }
        state.edges.set(key, {
            source: edge.source,
            target: edge.target,
            kind: String(edge.kind || ''),
            label: edge.label ? String(edge.label) : '',
            relation_type: edge.relation_type ? String(edge.relation_type) : '',
            via: edge.via ? String(edge.via) : '',
            broken: Boolean(edge.broken),
            suggested: edge.suggested === true,
        });
    });
    if (data.truncated) state.truncated = true;
    if (capped) state.capped = true;
    return capped;
}

/** The kinds the checkboxes ask for. */
function checkedKinds(el) {
    const kinds = [];
    el.querySelectorAll('[data-graph-kind]').forEach((box) => {
        if (box.checked) box.value.split(',').forEach((kind) => kinds.push(kind));
    });
    return kinds;
}

function suggestionsOn(el) {
    const box = el.querySelector('[data-graph-suggestions]');
    return Boolean(box && box.checked);
}

/** The query of a request for the current controls. */
export function requestParams(state, extra) {
    const params = new URLSearchParams();
    params.set('kinds', checkedKinds(state.el).join(','));
    if (suggestionsOn(state.el)) params.set('include_suggestions', 'true');
    Object.keys(extra).forEach((name) => params.set(name, String(extra[name])));
    return params;
}

async function fetchGraph(url) {
    const response = await spFetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
}

function withQuery(path, params) {
    return `${path}${path.indexOf('?') === -1 ? '?' : '&'}${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

function cssVar(style, name, fallback) {
    const value = style.getPropertyValue(name).trim();
    return value || fallback;
}

/** The colours of the theme in effect on *el*. */
export function readTheme(el) {
    const style = getComputedStyle(el);
    return {
        text: cssVar(style, '--sp-text-primary', '#1a1a2e'),
        muted: cssVar(style, '--sp-text-muted', '#8a8a9a'),
        surface: cssVar(style, '--sp-bg-elevated', '#ffffff'),
        border: cssVar(style, '--sp-border', '#e5e3dc'),
        font: cssVar(style, '--sp-font-body', 'system-ui, sans-serif'),
        edge: cssVar(style, '--sp-graph-edge', '#b8b5ad'),
        suggested: cssVar(style, '--sp-graph-suggested', '#EA580C'),
        colors: {
            issue: cssVar(style, '--sp-graph-issue', '#2563EB'),
            wiki_page: cssVar(style, '--sp-graph-wiki', '#16A34A'),
            development: cssVar(style, '--sp-graph-development', '#7C3AED'),
            attachment: cssVar(style, '--sp-graph-attachment', '#0891B2'),
            hub: cssVar(style, '--sp-graph-hub', '#c49a3c'),
            broken: cssVar(style, '--sp-graph-broken', '#DC2626'),
        },
    };
}

function messages(el) {
    const msg = (name) => el.getAttribute(`data-msg-${name}`) || '';
    let types = {};
    let kinds = {};
    try {
        types = JSON.parse(el.getAttribute('data-type-labels') || '{}');
        kinds = JSON.parse(el.getAttribute('data-kind-labels') || '{}');
    } catch (_e) {
        // Labels fall back to the raw type and kind names.
    }
    return { msg, types, kinds };
}

function tooltipLine(className, text) {
    const line = document.createElement('div');
    line.className = className;
    line.textContent = text;
    return line;
}

function nodeTooltip(state, node) {
    const { msg, types } = state.text;
    const box = document.createElement('div');
    box.className = 'sp-graph-tip';
    box.append(tooltipLine('sp-graph-tip-type', types[node.type] || node.type));
    if (node.type === 'issue' || node.type === 'commit' || node.type === 'broken') {
        box.append(tooltipLine('sp-graph-tip-ref', node.ref));
    }
    if (node.title && node.type !== 'broken') box.append(tooltipLine('sp-graph-tip-title', node.title));
    if (node.status) box.append(tooltipLine('sp-graph-tip-meta', node.status));
    if (isHub(node) && typeof node.degree === 'number') {
        box.append(tooltipLine('sp-graph-tip-meta', fill(msg('degree'), { count: node.degree })));
    }
    if (isHub(node) && !state.expanded.has(node.ref)) {
        box.append(tooltipLine('sp-graph-tip-hint', msg('hint-hub')));
    } else if (safeHref(node.url)) {
        box.append(tooltipLine('sp-graph-tip-hint', msg('hint-open')));
    }
    return box;
}

function edgeTooltip(state, edge) {
    const { msg, kinds } = state.text;
    const box = document.createElement('div');
    box.className = 'sp-graph-tip';
    const kind = kinds[edge.kind] || edge.kind;
    box.append(tooltipLine('sp-graph-tip-type', edge.relation_type ? `${kind}: ${edge.relation_type.replace(/_/g, ' ')}` : kind));
    box.append(tooltipLine('sp-graph-tip-ref', `${edge.source} → ${edge.target}`));
    if (edge.label) box.append(tooltipLine('sp-graph-tip-title', edge.label));
    if (edge.via) box.append(tooltipLine('sp-graph-tip-meta', edge.via));
    if (edge.suggested) box.append(tooltipLine('sp-graph-tip-hint', msg('suggested')));
    return box;
}

/** The ECharts option for the nodes and edges of *state*. */
export function buildOption(state, theme, width) {
    const connections = new Map();
    state.edges.forEach((edge) => {
        connections.set(edge.source, (connections.get(edge.source) || 0) + 1);
        connections.set(edge.target, (connections.get(edge.target) || 0) + 1);
    });
    const many = state.nodes.size > 60;
    const narrow = width < 480;
    const nodes = [];
    state.nodes.forEach((node) => {
        const category = nodeCategory(node.type);
        const root = node.ref === state.rootRef;
        const hub = category === 'hub';
        let size = 12 + Math.min(10, (connections.get(node.ref) || 0) * 1.5);
        if (hub) size = 8;
        if (root) size = 26;
        nodes.push({
            id: node.ref,
            name: node.ref,
            symbol: hub ? 'diamond' : 'circle',
            symbolSize: narrow ? Math.max(6, size * 0.8) : size,
            itemStyle: {
                color: theme.colors[category] || theme.colors.development,
                borderColor: root ? theme.text : theme.surface,
                borderWidth: root ? 2 : 1,
            },
            label: {
                // On a crowded graph only the root and the hubs keep a label; the rest show theirs on hover.
                show: !many || root || hub,
                formatter: () => nodeLabel(node),
                fontWeight: root ? 600 : 400,
            },
            cursor: isHub(node) || safeHref(node.url) ? 'pointer' : 'default',
        });
    });
    const links = [];
    state.edges.forEach((edge, key) => {
        links.push({
            id: key,
            source: edge.source,
            target: edge.target,
            symbol: UNDIRECTED.has(edge.kind) ? ['none', 'none'] : ['none', 'arrow'],
            symbolSize: [0, 6],
            lineStyle: {
                color: edge.suggested ? theme.suggested : theme.edge,
                type: edge.suggested ? 'dashed' : 'solid',
                width: edge.suggested ? 1.5 : 1,
                opacity: 0.9,
            },
        });
    });
    const big = nodes.length >= STATIC_LAYOUT_NODES || links.length >= STATIC_LAYOUT_EDGES;
    return {
        animation: !big,
        textStyle: { color: theme.text, fontFamily: theme.font },
        aria: { enabled: true, label: { description: state.description } },
        tooltip: {
            confine: true,
            backgroundColor: theme.surface,
            borderColor: theme.border,
            textStyle: { color: theme.text, fontFamily: theme.font },
            formatter: (params) => {
                if (params.dataType === 'edge') {
                    const edge = state.edges.get(params.data && params.data.id);
                    return edge ? edgeTooltip(state, edge) : '';
                }
                const node = state.nodes.get(params.data && params.data.id);
                return node ? nodeTooltip(state, node) : '';
            },
        },
        series: [{
            type: 'graph',
            layout: 'force',
            roam: true,
            // Drag and zoom anywhere on the canvas, not only over the drawn graph: zoomed out, the
            // graph covers a small part of it and the rest would not move.
            roamTrigger: 'global',
            draggable: true,
            autoCurveness: true,
            data: nodes,
            links,
            // A little zoomed out, and long labels cut, so labels at the edge of the layout stay in view.
            zoom: initialZoom(narrow),
            scaleLimit: { min: ZOOM_MIN, max: ZOOM_MAX },
            label: {
                position: 'right',
                color: theme.text,
                fontFamily: theme.font,
                fontSize: narrow ? 10 : 11,
                width: narrow ? 90 : 140,
                overflow: 'truncate',
            },
            labelLayout: { hideOverlap: true },
            emphasis: { focus: 'adjacency', label: { show: true }, lineStyle: { width: 2 } },
            lineStyle: { color: theme.edge, curveness: 0 },
            // Spread scales with the width, so a phone keeps the graph on screen and a desktop does not huddle.
            force: {
                // A big graph is laid out at once rather than animated step by step.
                layoutAnimation: !big,
                repulsion: narrow ? 120 : Math.min(420, Math.max(220, width / 4)),
                edgeLength: narrow ? [35, 90] : [90, 170],
                gravity: narrow ? 0.12 : 0.06,
                friction: 0.15,
            },
        }],
    };
}

function initialZoom(narrow) {
    return narrow ? 0.8 : 0.9;
}

/** The chart's zoom now (it changes when the reader scrolls or pinches), or the initial one. */
function currentZoom(state) {
    const series = state.chart && state.chart.getOption().series;
    const zoom = series && series[0] && series[0].zoom;
    return typeof zoom === 'number' ? zoom : initialZoom(state.width < 640);
}

/** The zoom buttons: in and out by a step around the view's centre, or back to the first view. */
export function zoomGraph(state, how) {
    if (!state.chart) return;
    if (how === 'reset') {
        state.chart.setOption({ series: [{ zoom: initialZoom(state.width < 640), center: null }] });
        state.chart.dispatchAction({ type: 'restore' });
        return;
    }
    const factor = how === 'in' ? ZOOM_STEP : 1 / ZOOM_STEP;
    const zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, currentZoom(state) * factor));
    state.chart.setOption({ series: [{ zoom }] });
}

function draw(state, animate) {
    const canvas = state.canvas;
    if (!state.nodes.size || !state.edges.size || !canvas.clientWidth) return;
    const theme = readTheme(state.el);
    state.themeKey = JSON.stringify(theme);
    state.width = canvas.clientWidth;
    try {
        if (!state.chart) {
            state.chart = init(canvas, null, { renderer: 'svg' });
            state.chart.on('click', (params) => onChartClick(state, params));
        }
        const option = buildOption(state, theme, state.width);
        if (!animate) option.animation = false;
        state.chart.resize();
        state.chart.setOption(option, { notMerge: true });
    } catch (_e) {
        setStatus(state, state.text.msg('error'), true);
    }
}

// ---------------------------------------------------------------------------
// Status, list and controls
// ---------------------------------------------------------------------------

function setStatus(state, text, isError) {
    state.status.textContent = text;
    state.status.classList.toggle('sp-graph-status--error', Boolean(isError));
}

function summary(state) {
    const { msg } = state.text;
    const parts = [fill(msg('summary'), { nodes: state.nodes.size, edges: state.edges.size })];
    if (state.truncated) parts.push(fill(msg('truncated'), { count: state.nodes.size }));
    if (state.capped) parts.push(msg('cap'));
    return parts.join(' ');
}

function typeOrder(node, rootRef) {
    if (node.ref === rootRef) return 0;
    return { issue: 1, wiki_page: 2 }[node.type] || (isHub(node) ? 4 : 3);
}

/** The accessible list: every node, as a link (open) or a button (show a hub's items). */
function renderList(state) {
    const { msg, types } = state.text;
    const items = Array.from(state.nodes.values()).sort((a, b) => (
        typeOrder(a, state.rootRef) - typeOrder(b, state.rootRef) || a.ref.localeCompare(b.ref)
    ));
    const rows = items.map((node) => {
        const li = document.createElement('li');
        li.className = 'sp-graph-item';
        const type = document.createElement('span');
        type.className = `sp-graph-item-type sp-graph-item-type--${nodeCategory(node.type)}`;
        type.textContent = types[node.type] || node.type;
        li.append(type);
        const href = safeHref(node.url);
        const name = node.type === 'issue' ? `${node.ref} ${node.title}` : (node.title || node.ref);
        if (href) {
            const link = document.createElement('a');
            link.className = 'sp-graph-item-name';
            link.href = href;
            if (!href.startsWith('/')) link.rel = 'noopener noreferrer';
            link.textContent = name;
            li.append(link);
        } else {
            const text = document.createElement('span');
            text.className = 'sp-graph-item-name';
            text.textContent = name;
            li.append(text);
        }
        if (isHub(node)) {
            if (typeof node.degree === 'number') {
                const degree = document.createElement('span');
                degree.className = 'sp-graph-item-meta';
                degree.textContent = fill(msg('degree'), { count: node.degree });
                li.append(degree);
            }
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'sp-btn sp-btn-ghost sp-btn-sm sp-graph-item-expand';
            const done = state.expanded.has(node.ref);
            button.disabled = done || state.capped;
            button.textContent = done ? msg('expanded') : msg('expand');
            button.addEventListener('click', () => expandHub(state, node.ref));
            li.append(button);
        } else if (node.status) {
            const status = document.createElement('span');
            status.className = 'sp-graph-item-meta';
            status.textContent = node.status;
            li.append(status);
        }
        return li;
    });
    state.items.replaceChildren(...rows);
    if (state.count) state.count.textContent = `(${state.nodes.size})`;
}

function showResult(state) {
    const empty = !state.edges.size;
    state.el.classList.toggle('sp-graph--empty', empty);
    if (empty) {
        setStatus(state, state.text.msg('empty'), false);
        if (state.chart) state.chart.clear();
    } else {
        setStatus(state, summary(state), false);
    }
    renderList(state);
    if (!empty) draw(state, true);
}

function reset(state) {
    state.nodes = new Map();
    state.edges = new Map();
    state.rootRef = '';
    state.truncated = false;
    state.capped = false;
}

/** Read the graph for the current controls, with the hubs expanded so far. */
async function load(state) {
    const ticket = ++state.ticket;
    const kinds = checkedKinds(state.el);
    setStatus(state, state.text.msg('loading'), false);
    state.el.setAttribute('aria-busy', 'true');
    try {
        if (!kinds.length) {
            reset(state);
        } else {
            const main = requestParams(state, state.scope === 'project'
                ? { limit: LIMITS.project }
                : { limit: LIMITS.item, depth: 2, direction: 'both' });
            const reads = [fetchGraph(withQuery(state.endpoint, main))];
            state.expanded.forEach((ref) => {
                reads.push(fetchGraph(withQuery(state.expandEndpoint, requestParams(state, { ref, depth: 1, limit: HUB_LIMIT })))
                    .catch(() => null));
            });
            const [first, ...hubs] = await Promise.all(reads);
            if (ticket !== state.ticket) return;
            reset(state);
            mergeGraph(state, first, true);
            hubs.forEach((data) => {
                if (data) mergeGraph(state, data);
            });
        }
        if (ticket !== state.ticket) return;
        showResult(state);
    } catch (_e) {
        if (ticket !== state.ticket) return;
        setStatus(state, state.text.msg('error'), true);
    } finally {
        if (ticket === state.ticket) state.el.removeAttribute('aria-busy');
    }
}

/** Add the items of a hub node (a tag, component, version, sprint or assignee) to the graph. */
async function expandHub(state, ref) {
    const node = state.nodes.get(ref);
    if (!isHub(node) || state.expanded.has(ref) || state.capped) return;
    state.expanded.add(ref);
    const ticket = state.ticket;
    try {
        const data = await fetchGraph(withQuery(state.expandEndpoint, requestParams(state, { ref, depth: 1, limit: HUB_LIMIT })));
        if (ticket !== state.ticket) return;
        mergeGraph(state, data);
        showResult(state);
    } catch (_e) {
        state.expanded.delete(ref);
        setStatus(state, state.text.msg('error'), true);
    }
}

function onChartClick(state, params) {
    if (params.dataType !== 'node' || !params.data) return;
    const node = state.nodes.get(params.data.id);
    if (!node) return;
    if (isHub(node) && !state.expanded.has(node.ref)) {
        expandHub(state, node.ref);
        return;
    }
    const href = safeHref(node.url);
    if (!href) return;
    if (href.startsWith('/')) {
        window.location.assign(href);
    } else {
        window.open(href, '_blank', 'noopener');
    }
}

// ---------------------------------------------------------------------------
// Mounting
// ---------------------------------------------------------------------------

function watchSize(state) {
    let frame = 0;
    const onResize = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
            frame = 0;
            if (!mounted.has(state.el)) return;
            const width = state.canvas.clientWidth;
            if (!width || width === state.width) return;
            if (state.chart) state.chart.resize();
            // A graph mounted while hidden, or whose width crosses the phone size, is drawn again.
            if (!state.chart || (width < 480) !== (state.width < 480)) draw(state, false);
            else state.width = width;
        });
    };
    if (typeof ResizeObserver === 'function') {
        state.observer = new ResizeObserver(onResize);
        state.observer.observe(state.canvas);
    } else {
        window.addEventListener('resize', onResize);
    }
}

function redrawOnThemeChange() {
    mounted.forEach((state) => {
        if (state.chart && JSON.stringify(readTheme(state.el)) !== state.themeKey) draw(state, false);
    });
}

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

/** Draw the graph of one element. Safe to call twice. */
export function mountLinkGraph(el) {
    if (mounted.has(el)) return;
    const canvas = el.querySelector('[data-graph-canvas]');
    const status = el.querySelector('[data-graph-status]');
    const items = el.querySelector('[data-graph-items]');
    const endpoint = safeNextPath(el.getAttribute('data-endpoint') || '');
    const expandEndpoint = safeNextPath(el.getAttribute('data-expand-endpoint') || '');
    if (!canvas || !status || !items || !endpoint || !expandEndpoint) return;
    const state = {
        el,
        canvas,
        status,
        items,
        count: el.querySelector('[data-graph-count]'),
        endpoint,
        expandEndpoint,
        scope: el.getAttribute('data-scope') === 'project' ? 'project' : 'item',
        description: canvas.getAttribute('aria-label') || '',
        text: messages(el),
        expanded: new Set(),
        ticket: 0,
        chart: null,
        themeKey: '',
        width: 0,
        observer: null,
        timer: 0,
    };
    reset(state);
    mounted.set(el, state);
    el.addEventListener('change', (event) => {
        const target = event.target;
        if (!(target instanceof HTMLInputElement)) return;
        if (!target.hasAttribute('data-graph-kind') && !target.hasAttribute('data-graph-suggestions')) return;
        clearTimeout(state.timer);
        state.timer = setTimeout(() => load(state), 150);
    });
    el.querySelectorAll('[data-graph-zoom]').forEach((button) => {
        button.addEventListener('click', () => zoomGraph(state, button.getAttribute('data-graph-zoom')));
    });
    watchSize(state);
    watchTheme();
    el.setAttribute('data-sp-graph-mounted', '');
    load(state);
}

/** Draw every graph marked open, now and when one is opened later. */
export function initLinkGraphs() {
    window.spLinkGraph = { mount: mountLinkGraph };
    document.querySelectorAll(`${SELECTOR}[data-sp-graph-open]`).forEach(mountLinkGraph);
    document.addEventListener('sp-link-graph-open', (event) => {
        const el = event.target;
        if (el instanceof Element && el.matches(SELECTOR)) mountLinkGraph(el);
    });
}
