// The running timer in the page header: the elapsed time ticks in the
// browser, "Stop & log" asks for the activity (the default one preselected)
// and an optional comment, "Discard" drops the timer without logging time.
// Configured through data attributes (data-elapsed, data-i18n) and driven by
// plain property and method names only, so it works with the Alpine CSP build.

export function formatElapsed(seconds) {
    var total = Math.max(0, Math.floor(seconds));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

export function readI18n(el, defaults) {
    var parsed = {};
    try {
        parsed = JSON.parse(el.dataset.i18n || '{}');
    } catch (_e) {
        parsed = {};
    }
    return Object.assign({}, defaults, parsed);
}

async function errorMessage(res, fallback) {
    try {
        var data = await res.json();
        return (data.errors && data.errors[0] && data.errors[0].message) || fallback;
    } catch (_e) {
        return fallback;
    }
}

export function headerTimer() {
    return {
        i18n: {},
        elapsed: 0,
        mode: 'idle',
        activities: [],
        activityId: '',
        comments: '',
        busy: false,
        error: '',
        _base: 0,
        _loadedAt: 0,
        _interval: null,

        init() {
            this.i18n = readI18n(this.$el, {
                loadFailed: 'Could not load the activities.',
                stopFailed: 'Could not stop the timer.',
                discardFailed: 'Could not discard the timer.',
                connectFailed: 'Unable to connect.'
            });
            this._base = parseInt(this.$el.dataset.elapsed, 10) || 0;
            this._loadedAt = Date.now();
            this.elapsed = this._base;
            var self = this;
            this._interval = setInterval(function () { self.tick(); }, 1000);
        },

        destroy() {
            clearInterval(this._interval);
        },

        tick() {
            this.elapsed = this._base + Math.floor((Date.now() - this._loadedAt) / 1000);
        },

        get elapsedLabel() { return formatElapsed(this.elapsed); },
        get isIdle() { return this.mode === 'idle'; },
        get isStopping() { return this.mode === 'stop'; },
        get isDiscarding() { return this.mode === 'discard'; },
        get hasError() { return this.error !== ''; },
        get cannotStop() { return this.busy || !this.activityId; },

        async openStop() {
            this.mode = 'stop';
            this.error = '';
            if (!this.activities.length) {
                await this.loadActivities();
            }
        },

        async loadActivities() {
            try {
                var res = await spFetch('/api/v1/time-entries/activities/');
                if (!res.ok) {
                    this.error = this.i18n.loadFailed;
                    return;
                }
                var activities = await res.json();
                var preferred = activities.find(function (a) { return a.is_default; }) || activities[0];
                this.activities = activities;
                if (preferred) {
                    var self = this;
                    // Select once the <option>s exist, or the select keeps its blank value.
                    this.$nextTick(function () { self.activityId = String(preferred.id); });
                }
            } catch (_e) {
                this.error = this.i18n.connectFailed;
            }
        },

        askDiscard() {
            this.mode = 'discard';
            this.error = '';
        },

        cancel() {
            this.mode = 'idle';
            this.error = '';
        },

        async stop() {
            if (this.cannotStop) {
                return;
            }
            this.busy = true;
            this.error = '';
            try {
                var res = await spFetch('/api/v1/timer/stop/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        activity_id: parseInt(this.activityId, 10),
                        comments: this.comments.trim() || null
                    })
                });
                if (res.ok) {
                    window.location.reload();
                    return;
                }
                this.error = await errorMessage(res, this.i18n.stopFailed);
            } catch (_e) {
                this.error = this.i18n.connectFailed;
            }
            this.busy = false;
        },

        async discard() {
            this.busy = true;
            this.error = '';
            try {
                var res = await spFetch('/api/v1/timer/discard/', {method: 'POST'});
                // 404: the timer is already gone (stopped in another tab); reload either way.
                if (res.ok || res.status === 404) {
                    window.location.reload();
                    return;
                }
                this.error = await errorMessage(res, this.i18n.discardFailed);
            } catch (_e) {
                this.error = this.i18n.connectFailed;
            }
            this.busy = false;
        }
    };
}
