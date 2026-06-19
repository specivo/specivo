export function tagFilter(initial) {
        return {
            endpoint: initial.endpoint || '/api/v1/tags/search/',
            tags: initial.tags || [],
            baseParams: initial.baseParams || {},

            query: '',
            results: [],
            open: false,
            loading: false,
            activeIndex: -1,
            debounceTimer: null,
            _lastQuery: null,

            init() {
                var self = this;
                this.$watch('query', function () {
                    self.open = true;
                    self.search();
                });
                spBindAnchoredMenu(this);
                this.$watch('results', function () {
                    if (self.open) self.$nextTick(function () { spAnchorMenu(self.$el, self.$el.querySelector('.sp-ac-menu')); });
                });
            },

            _hasTag(name) {
                var lower = name.toLowerCase();
                for (var i = 0; i < this.tags.length; i++) {
                    if (this.tags[i].name.toLowerCase() === lower) return true;
                }
                return false;
            },

            onFocus() {
                this.open = true;
                if (!this.query) this.fetchDefault();
            },

            onBlur() {
                var self = this;
                setTimeout(function () {
                    self.open = false;
                    self.activeIndex = -1;
                }, 150);
            },

            async fetchDefault() {
                this.loading = true;
                try {
                    var res = await spFetch(this.endpoint + '?limit=20');
                    if (res.ok) {
                        this.results = await res.json();
                        this.activeIndex = -1;
                    }
                } catch (_e) {}
                this.loading = false;
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

            moveDown() {
                if (!this.open) { this.open = true; this.fetchDefault(); return; }
                if (this.activeIndex < this.results.length - 1) this.activeIndex++;
            },

            moveUp() {
                if (this.activeIndex > 0) this.activeIndex--;
            },

            confirm() {
                if (this.activeIndex >= 0 && this.activeIndex < this.results.length) {
                    this.selectResult(this.results[this.activeIndex]);
                }
            },

            selectResult(item) {
                if (!this._hasTag(item.name)) {
                    this.tags.push({name: item.name});
                    this.navigate();
                    return;
                }
                this.query = '';
                this._lastQuery = null;
                this.results = [];
                this.open = false;
                this.activeIndex = -1;
            },

            removeTag(index) {
                this.tags.splice(index, 1);
                this.navigate();
            },

            removeLast() {
                if (this.query) return;
                if (this.tags.length) {
                    this.tags.pop();
                    this.navigate();
                }
            },

            navigate() {
                var b = this.baseParams || {};
                var p = new URLSearchParams();
                if (b.q) p.set('q', b.q);
                p.set('mode', b.mode || 'hybrid');
                // Tags cover issues + wiki; non-taggable scopes fall back to "all".
                var scope = (b.scope === 'issues' || b.scope === 'wiki') ? b.scope : 'all';
                p.set('scope', scope);
                if (b.project_key) p.set('project_key', b.project_key);
                p.set('limit', b.limit ? String(b.limit) : '25');
                this.tags.forEach(function (t) { p.append('tag', t.name); });
                window.location.assign('/search/?' + p.toString());
            }
        };
    }
