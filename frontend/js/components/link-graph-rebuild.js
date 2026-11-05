/**
 * Link graph card on the project settings Search tab (and the admin variant):
 * the graph's figures, a "Rebuild link graph" button, live progress while a
 * rebuild runs, and the last run's result.
 *
 * Talks to /api/v1/projects/{key}/links/rebuild/ (POST, 202 or 409 when one
 * already runs) and .../rebuild/status/ (progress, last_result and stats).
 */
export function linkGraphRebuild(initial) {
    initial = initial || {};
    // Strings are translated in the template and handed in via `i18n`.
    var i18n = Object.assign({
        connectFailed: 'Unable to connect.',
        started: 'Rebuild started.',
        alreadyRunning: 'A rebuild is already running.',
        startFailed: 'Failed to start the rebuild.',
        complete: 'Rebuild complete.',
        failed: 'Rebuild failed.',
        wikiLinks: '%(count)s wiki links',
        mentions: '%(count)s mentions',
        attachmentLinks: '%(count)s attachment links',
        fromWiki: '%(count)s from wiki pages',
        fromIssues: '%(count)s from issue descriptions',
        fromComments: '%(count)s from comments',
        relations: '%(count)s relations',
        parents: '%(count)s parent links',
        attachments: '%(count)s attachments',
        sources: '%(count)s sources',
        edges: '%(count)s links',
        unresolved: '%(count)s unresolved',
        failedSources: '%(count)s failed',
        seconds: '%(count)s s',
        by: 'by %(user)s',
        statusOk: 'OK',
        statusPartial: 'Partly failed',
        statusFailed: 'Failed',
        justNow: 'just now',
        minutesAgo: '%(count)s min ago',
        hoursAgo: '%(count)s h ago',
        daysAgo: '%(count)s d ago'
    }, initial.i18n || {});

    function fmt(n) {
        return String(n || 0).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    function count(template, n) {
        return template.replace('%(count)s', fmt(n));
    }

    return {
        baseUrl: (initial.baseUrl || '').replace(/\/+$/, ''),
        canManage: initial.canManage !== false,
        state: '',
        running: false,
        progress: null,
        lastResult: null,
        stats: null,
        loaded: false,
        starting: false,
        message: '',
        messageError: false,
        _pollTimer: null,

        init: function () {
            this.refresh();
            var self = this;
            window.addEventListener('beforeunload', function () { self._stopPoll(); });
        },

        get busy() {
            return this.running || this.state === 'PENDING' || this.state === 'STARTED' || this.state === 'PROGRESS';
        },

        get empty() {
            return this.loaded && (!this.stats || !this.stats.total);
        },

        get progressText() {
            if (!this.progress || !this.progress.total) return '';
            return fmt(this.progress.processed) + ' / ' + fmt(this.progress.total);
        },

        get kindSummary() {
            var k = (this.stats && this.stats.by_kind) || {};
            return [
                count(i18n.wikiLinks, k.wiki_link),
                count(i18n.mentions, k.mention),
                count(i18n.attachmentLinks, k.attachment_link)
            ].join(' · ');
        },

        get sourceSummary() {
            var s = (this.stats && this.stats.by_source) || {};
            return [
                count(i18n.fromWiki, s.wiki_page),
                count(i18n.fromIssues, s.issue),
                count(i18n.fromComments, s.journal)
            ].join(' · ');
        },

        get structuredSummary() {
            var s = (this.stats && this.stats.structured) || {};
            return [
                count(i18n.relations, s.relation),
                count(i18n.parents, s.parent),
                count(i18n.attachments, s.attachment)
            ].join(' · ');
        },

        get totalText() { return fmt(this.stats && this.stats.total); },
        get unresolvedText() { return fmt(this.stats && this.stats.unresolved); },
        get topUnresolved() { return (this.stats && this.stats.top_unresolved) || []; },
        get lastUpdateText() { return this._timeAgo(this.stats && this.stats.last_update); },

        get lastRebuildText() {
            var r = this.lastResult;
            if (!r) return '';
            var status = r.status === 'success' ? i18n.statusOk
                : (r.status === 'partial' ? i18n.statusPartial : i18n.statusFailed);
            var parts = [status];
            var when = this._timeAgo(r.finished_at);
            if (when) parts.push(when);
            if (r.duration_seconds != null) parts.push(count(i18n.seconds, r.duration_seconds));
            if (r.sources != null) parts.push(count(i18n.sources, r.sources));
            if (r.edges != null) parts.push(count(i18n.edges, r.edges));
            if (r.unresolved != null) parts.push(count(i18n.unresolved, r.unresolved));
            if (r.failed) parts.push(count(i18n.failedSources, r.failed));
            if (r.triggered_by) parts.push(i18n.by.replace('%(user)s', r.triggered_by));
            return parts.join(' · ');
        },

        get lastRebuildFailed() {
            return !!this.lastResult && this.lastResult.status !== 'success';
        },

        refFor: function (item) {
            return item.target_type === 'wiki_page' ? '[[' + item.target_ref + ']]' : item.target_ref;
        },

        _apply: function (data) {
            if (!data) return;
            this.running = !!data.running;
            this.state = data.state || '';
            this.progress = data.progress || null;
            if (Object.prototype.hasOwnProperty.call(data, 'last_result')) this.lastResult = data.last_result || null;
            if (data.stats) this.stats = data.stats;
            this.loaded = true;
        },

        refresh: async function () {
            try {
                var res = await spFetch(this.baseUrl + '/rebuild/status/', { headers: { 'Accept': 'application/json' } });
                if (res.ok) {
                    this._apply(await res.json());
                    if (this.busy) this._startPoll();
                }
            } catch (_e) { /* keep the empty state on a transient error */ }
        },

        rebuild: async function () {
            if (!this.canManage || this.busy || this.starting) return;
            this.starting = true;
            this.message = '';
            try {
                var res = await spFetch(this.baseUrl + '/rebuild/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });
                var data = await res.json().catch(function () { return {}; });
                if (res.status === 202 || res.ok) {
                    this.state = data.state || 'PENDING';
                    this.running = true;
                    this.progress = null;
                    this.message = i18n.started;
                    this.messageError = false;
                    this._startPoll();
                } else if (res.status === 409) {
                    var details = data.errors && data.errors[0] && data.errors[0].details;
                    if (details) this._apply(details);
                    this.message = i18n.alreadyRunning;
                    this.messageError = false;
                    this._startPoll();
                } else {
                    this.message = (data.errors && data.errors[0] && data.errors[0].message) || i18n.startFailed;
                    this.messageError = true;
                }
            } catch (_e) {
                this.message = i18n.connectFailed;
                this.messageError = true;
            }
            this.starting = false;
        },

        _startPoll: function () {
            if (this._pollTimer) return;
            var self = this;
            this._pollTimer = setInterval(function () { self._poll(); }, 2000);
        },

        _stopPoll: function () {
            if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
        },

        _poll: async function () {
            try {
                var res = await spFetch(this.baseUrl + '/rebuild/status/', { headers: { 'Accept': 'application/json' } });
                if (!res.ok) return;
                this._apply(await res.json());
                if (!this.busy) {
                    this._stopPoll();
                    if (this.lastResult && this.lastResult.status === 'success') {
                        this.message = i18n.complete;
                        this.messageError = false;
                    } else if (this.lastResult) {
                        this.message = i18n.failed;
                        this.messageError = true;
                    }
                }
            } catch (_e) { /* keep polling */ }
        },

        _timeAgo: function (iso) {
            if (!iso) return '';
            var then = Date.parse(iso);
            if (isNaN(then)) return '';
            var secs = Math.round((Date.now() - then) / 1000);
            if (secs < 45) return i18n.justNow;
            var mins = Math.round(secs / 60);
            if (mins < 60) return i18n.minutesAgo.replace('%(count)s', mins);
            var hrs = Math.round(mins / 60);
            if (hrs < 24) return i18n.hoursAgo.replace('%(count)s', hrs);
            return i18n.daysAgo.replace('%(count)s', Math.round(hrs / 24));
        }
    };
}
