export function ftsReindex(initial) {
        initial = initial || {};
        // User-facing strings are translated in the template and handed in via
        // `i18n`; the English defaults only cover a caller that omits them.
        // Counts stay out of plural agreement (the number is only known here),
        // so translations phrase them in a count-neutral form.
        var i18n = Object.assign({
            inherit: 'Inherit (%(language)s)',
            languageSaved: 'Language saved.',
            saveLanguageFailed: 'Failed to save language.',
            connectFailed: 'Unable to connect.',
            reindexStarted: 'Reindex started.',
            startFailed: 'Failed to start reindex.',
            reindexComplete: 'Reindex complete.',
            reindexFailed: 'Reindex failed.',
            countIssues: '%(count)s issues',
            countWiki: '%(count)s wiki',
            countChunks: '%(count)s chunks',
            justNow: 'just now',
            minutesAgo: '%(count)s min ago',
            hoursAgo: '%(count)s h ago',
            daysAgo: '%(count)s d ago'
        }, initial.i18n || {});

        return {
            baseUrl: (initial.baseUrl || '').replace(/\/+$/, ''),
            isProject: !!initial.isProject,
            canManage: initial.canManage !== false,
            allowed: initial.allowed || [],
            instanceDefault: initial.instanceDefault || 'english',
            // For project scope an empty string means "inherit"; the select
            // is bound to selected and we map "" <-> inherit on save.
            selected: initial.language == null ? '' : initial.language,
            // The persisted language at load/last-save; used to detect an
            // unsaved change so we can prompt "save, then reindex".
            savedLanguage: initial.language == null ? '' : initial.language,
            effective: initial.effective || initial.language || '',
            reindexNeeded: !!initial.reindexNeeded,
            lastResult: initial.lastResult || null,
            state: '',
            running: !!initial.running,
            progress: null,
            saving: false,
            starting: false,
            message: '',
            messageError: false,
            _pollTimer: null,

            init: function () {
                // Pull fresh server state on mount (seeded values may be stale).
                this.refresh();
                var self = this;
                window.addEventListener('beforeunload', function () { self._stopPoll(); });
            },

            get busy() {
                return this.running ||
                    this.state === 'PENDING' || this.state === 'STARTED' || this.state === 'PROGRESS';
            },

            // True when the language select differs from the persisted value
            // (an unsaved change) — prompts the user to save before reindexing.
            get languageDirty() {
                return String(this.selected) !== String(this.savedLanguage);
            },

            get languageOptions() {
                // Project scope prepends an "Inherit" pseudo-option (value "").
                var opts = this.allowed.map(function (l) { return {value: l, label: l}; });
                if (this.isProject) {
                    opts.unshift({value: '', label: i18n.inherit.replace('%(language)s', this.instanceDefault)});
                }
                return opts;
            },

            get progressDone() {
                if (!this.progress) return 0;
                return (this.progress.issues || 0) +
                    (this.progress.wiki_contents || 0) +
                    (this.progress.search_chunks || 0);
            },

            _applyState: function (data) {
                if (!data) return;
                if (Object.prototype.hasOwnProperty.call(data, 'language')) {
                    this.selected = data.language == null ? '' : data.language;
                    this.savedLanguage = this.selected;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'effective')) {
                    this.effective = data.effective;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'instance_default')) {
                    this.instanceDefault = data.instance_default;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'allowed')) {
                    this.allowed = data.allowed;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'running')) {
                    this.running = !!data.running;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'state')) {
                    this.state = data.state || '';
                }
                if (Object.prototype.hasOwnProperty.call(data, 'progress')) {
                    this.progress = data.progress || null;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'last_result')) {
                    this.lastResult = data.last_result || null;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'reindex_needed')) {
                    this.reindexNeeded = !!data.reindex_needed;
                }
            },

            refresh: async function () {
                try {
                    var res = await spFetch(this.baseUrl + '/fts/', {
                        headers: {'Accept': 'application/json'}
                    });
                    if (res.ok) {
                        this._applyState(await res.json());
                        if (this.busy) this._startPoll();
                    }
                } catch (_e) { /* leave seeded state on transient error */ }
            },

            save: async function () {
                if (!this.canManage || this.saving) return;
                this.saving = true;
                this.message = '';
                try {
                    // Inherit (project scope) sends null; instance scope sends the value.
                    var lang = this.selected === '' ? null : this.selected;
                    var res = await spFetch(this.baseUrl + '/fts/language/', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({language: lang})
                    });
                    if (res.ok) {
                        this._applyState(await res.json());
                        this.message = i18n.languageSaved;
                        this.messageError = false;
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.message = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.saveLanguageFailed;
                        this.messageError = true;
                    }
                } catch (_e) {
                    this.message = i18n.connectFailed;
                    this.messageError = true;
                }
                this.saving = false;
            },

            reindex: async function () {
                if (!this.canManage || this.busy || this.starting) return;
                this.starting = true;
                this.message = '';
                try {
                    var res = await spFetch(this.baseUrl + '/reindex/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'}
                    });
                    if (res.ok || res.status === 202) {
                        var data = await res.json().catch(function () { return {}; });
                        this.state = data.state || 'PENDING';
                        this.running = true;
                        this.progress = null;
                        this.message = i18n.reindexStarted;
                        this.messageError = false;
                        this._startPoll();
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.message = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.startFailed;
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
                    var res = await spFetch(this.baseUrl + '/reindex/status/', {
                        headers: {'Accept': 'application/json'}
                    });
                    if (!res.ok) return;
                    var data = await res.json();
                    this._applyState(data);
                    if (!this.busy) {
                        this._stopPoll();
                        this.progress = null;
                        if (this.lastResult && this.lastResult.status === 'success') {
                            this.message = i18n.reindexComplete;
                            this.messageError = false;
                        } else if (this.lastResult && this.lastResult.status === 'failed') {
                            this.message = i18n.reindexFailed;
                            this.messageError = true;
                        }
                    }
                } catch (_e) { /* keep polling */ }
            },

            resultSummary: function () {
                var r = this.lastResult;
                if (!r || !r.counts) return '';
                var c = r.counts;
                var parts = [];
                if (c.issues != null) parts.push(i18n.countIssues.replace('%(count)s', this._fmt(c.issues)));
                if (c.wiki_contents != null) parts.push(i18n.countWiki.replace('%(count)s', this._fmt(c.wiki_contents)));
                if (c.search_chunks != null) parts.push(i18n.countChunks.replace('%(count)s', this._fmt(c.search_chunks)));
                var summary = parts.join(' · ');
                var when = this._timeAgo(r.finished_at);
                return when ? summary + ' — ' + when : summary;
            },

            _fmt: function (n) {
                return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
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
                var days = Math.round(hrs / 24);
                return i18n.daysAgo.replace('%(count)s', days);
            }
        };
    }
