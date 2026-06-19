export function tagField(initial) {
        return {
            endpoint: initial.endpoint || '',
            saveUrl: initial.saveUrl || '',
            tags: initial.tags || [],
            canEdit: initial.canEdit !== false,

            query: '',
            results: [],
            open: false,
            loading: false,
            saving: false,
            activeIndex: -1,
            debounceTimer: null,
            _lastQuery: null,

            _names() {
                return this.tags.map(function (t) { return t.name; });
            },

            // Link a tag chip to the tag-filtered search (issues + wiki).
            tagSearchUrl(name) {
                return '/search/?scope=all&tag=' + encodeURIComponent(name);
            },

            _hasTag(name) {
                var lower = name.toLowerCase();
                for (var i = 0; i < this.tags.length; i++) {
                    if (this.tags[i].name.toLowerCase() === lower) return true;
                }
                return false;
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
                } else {
                    this.commitTyped();
                }
            },

            cancel() {
                this.open = false;
                this.activeIndex = -1;
                this.query = '';
            },

            selectResult(item) {
                if (!this._hasTag(item.name)) {
                    this.tags.push({id: item.id, name: item.name, color: item.color || null});
                    this.persist();
                }
                this.query = '';
                this._lastQuery = null;
                this.results = [];
                this.open = false;
                this.activeIndex = -1;
            },

            commitTyped() {
                var name = (this.query || '').trim();
                if (!name) return;
                if (!this._hasTag(name)) {
                    this.tags.push({id: null, name: name, color: null});
                    this.persist();
                }
                this.query = '';
                this._lastQuery = null;
                this.results = [];
                this.open = false;
                this.activeIndex = -1;
            },

            removeTag(index) {
                this.tags.splice(index, 1);
                this.persist();
            },

            removeLast() {
                if (this.query) return;
                if (this.tags.length) {
                    this.tags.pop();
                    this.persist();
                }
            },

            async persist() {
                if (!this.saveUrl) return;  // pending mode — host reads this.tags
                this.saving = true;
                try {
                    var res = await spFetch(this.saveUrl, {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({names: this._names()})
                    });
                    if (res.ok) {
                        this.tags = await res.json();
                    }
                } catch (_e) {}
                this.saving = false;
            }
        };
    }
