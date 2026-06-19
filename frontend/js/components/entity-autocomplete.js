export function entityAutocomplete(initial) {
        return {
            endpoint: initial.endpoint || '',
            field: initial.field || '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,
            kind: initial.kind || 'version',

            selectedId: initial.currentId || null,
            selectedLabel: initial.currentLabel || '',
            selectedStatus: initial.currentStatus || '',
            query: initial.currentLabel || '',

            results: [],
            defaultResults: [],
            open: false,
            loading: false,
            activeIndex: -1,
            debounceTimer: null,
            _lastQuery: null,

            _formatLabel(item) {
                var label = item.name;
                var closedLike = ['closed', 'locked', 'completed'];
                if (item.status && closedLike.indexOf(item.status) !== -1) {
                    label += ' (' + item.status + ')';
                }
                return label;
            },

            _ensureCurrentPinned(list) {
                if (!this.selectedId) return list;
                for (var i = 0; i < list.length; i++) {
                    if (list[i].id === this.selectedId) return list;
                }
                // Inject a synthetic entry for the current selection so it stays visible
                var pinned = {
                    id: this.selectedId,
                    name: this.selectedLabel,
                    status: this.selectedStatus || ''
                };
                return [pinned].concat(list);
            },

            async fetchDefault() {
                if (this.defaultResults.length) {
                    this.results = this._ensureCurrentPinned(this.defaultResults.slice());
                    return;
                }
                this.loading = true;
                try {
                    var res = await spFetch(this.endpoint);
                    if (res.ok) {
                        var data = await res.json();
                        this.defaultResults = data;
                        this.results = this._ensureCurrentPinned(data.slice());
                    }
                } catch (_e) {}
                this.loading = false;
            },

            onFocus() {
                this.open = true;
                if (!this.query || this.query === this.selectedLabel) {
                    this.fetchDefault();
                }
            },

            onBlur() {
                var self = this;
                // Delay so click on a row still fires select()
                setTimeout(function () {
                    self.open = false;
                    self.activeIndex = -1;
                    // If user typed but didn't select, restore previous label
                    if (self.query !== self.selectedLabel) {
                        self.query = self.selectedLabel;
                    }
                }, 150);
            },

            search() {
                var q = (this.query || '').trim();
                if (q === this._lastQuery) return;
                this._lastQuery = q;
                clearTimeout(this.debounceTimer);
                var self = this;
                if (!q) {
                    this.fetchDefault();
                    return;
                }
                this.debounceTimer = setTimeout(async function () {
                    self.loading = true;
                    try {
                        var res = await spFetch(self.endpoint + '?q=' + encodeURIComponent(q) + '&limit=20');
                        if (res.ok) {
                            self.results = await res.json();
                            self.activeIndex = self.results.length ? 0 : -1;
                        }
                    } catch (_e) {}
                    self.loading = false;
                }, 200);
            },

            init() {
                var self = this;
                this.$watch('query', function () {
                    self.open = true;
                    self.search();
                });
                spBindAnchoredMenu(this);
                // Re-anchor when the row set changes the menu height.
                this.$watch('results', function () {
                    if (self.open) self.$nextTick(function () { spAnchorMenu(self.$el, self.$el.querySelector('.sp-ac-menu')); });
                });
            },

            moveDown() {
                if (!this.open) { this.open = true; this.fetchDefault(); return; }
                if (this.activeIndex < this.results.length - 1) this.activeIndex++;
            },

            moveUp() {
                if (this.activeIndex > 0) this.activeIndex--;
            },

            confirm() {
                if (this.activeIndex >= 0 && this.activeIndex < this.results.length) {
                    this.select(this.results[this.activeIndex]);
                }
            },

            cancel() {
                this.open = false;
                this.activeIndex = -1;
                this.query = this.selectedLabel;
            },

            async clearSelection() {
                this.selectedId = null;
                this.selectedLabel = '';
                this.selectedStatus = '';
                this.query = '';
                this.open = false;
                await this._commit(null);
            },

            async select(item) {
                this.selectedId = item.id;
                this.selectedLabel = item.name;
                this.selectedStatus = item.status || '';
                this.query = item.name;
                this.open = false;
                this.activeIndex = -1;
                await this._commit(item.id);
            },

            async _commit(value) {
                var payload = {lock_version: this.lockVersion};
                payload[this.field] = value;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        window.location.reload();
                    }
                } catch (_e) {}
            }
        };
    }
