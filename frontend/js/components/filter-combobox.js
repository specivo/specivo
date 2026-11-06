import { avatarIconClass, avatarInitials } from '../lib/avatar';

/* One searchable filter field of the issue list filter bar (assignee, tag,
   release). An ARIA 1.2 combobox: a text input that owns a listbox of
   suggestions fetched page by page from a search endpoint while the user
   types. Picking a suggestion writes its value into the field's hidden input
   and submits the filter form, so the choice lives in the URL; the chosen
   value is then rendered by the server as a chip with a plain clear link.

   Config (from the template):
     endpoint   search URL; receives q, offset and limit, answers a JSON list
     kind       'user' | 'tag' | 'version' — how a row is shaped and drawn
     listId     id of the listbox element (option ids derive from it)
     specials   fixed options listed first, e.g. Unassigned / Me / No release:
                [{value, label, user?}]
     projectKey the current project, to mark versions shared from elsewhere
     pageSize   rows per request (default 20) */
export function filterCombobox(config) {
    var cfg = config || {};
    return {
        endpoint: cfg.endpoint || '',
        kind: cfg.kind || 'tag',
        listId: cfg.listId || 'sp-filter-combo-list',
        specials: cfg.specials || [],
        projectKey: cfg.projectKey || '',
        pageSize: cfg.pageSize || 20,

        query: '',
        items: [],
        open: false,
        loading: false,
        hasMore: false,
        offset: 0,
        activeIndex: -1,
        debounceTimer: null,
        requestSeq: 0,
        _lastQuery: null,

        init() {
            var self = this;
            this.$watch('query', function () {
                self.open = true;
                self.search();
            });
            var reposition = function () { if (self.open) self._place(); };
            this.$watch('open', function (isOpen) { if (isOpen) self.$nextTick(reposition); });
            this.$watch('items', function () { self.$nextTick(reposition); });
            window.addEventListener('scroll', reposition, true);
            window.addEventListener('resize', reposition);
        },

        /* Anchor the menu under the field (spAnchorMenu), then keep it inside
           the viewport: on a phone the menu is wider than a field near the
           right edge. */
        _place() {
            var menu = this.$el.querySelector('.sp-ac-menu');
            if (!menu) return;
            spAnchorMenu(this.$el, menu);
            var margin = 8;
            var rect = menu.getBoundingClientRect();
            var maxRight = document.documentElement.clientWidth - margin;
            if (rect.right > maxRight) {
                menu.style.left = Math.max(margin, maxRight - rect.width) + 'px';
            }
        },

        /* ---- ARIA helpers ---- */

        optionId(idx) {
            return this.listId + '-opt-' + idx;
        },

        activeId() {
            return this.open && this.activeIndex >= 0 && this.activeIndex < this.items.length
                ? this.optionId(this.activeIndex)
                : '';
        },

        expanded() {
            return this.open ? 'true' : 'false';
        },

        isActive(idx) {
            return idx === this.activeIndex ? 'true' : 'false';
        },

        /* ---- Rows ---- */

        _userRow(u, value, label) {
            var name = label || u.display_name || u.login;
            var color = u.avatar_color || '';
            return {
                key: 'v-' + value,
                value: String(value),
                label: name,
                secondary: label ? '' : (u.display_name && u.display_name !== u.login ? u.login : ''),
                isUser: true,
                avatarIcon: avatarIconClass(u.login),
                avatarUrl: avatarIconClass(u.login) ? '' : (u.avatar_url || ''),
                initials: avatarInitials(u.display_name || u.login),
                avatarStyle: color ? 'background:' + color + ';color:white' : '',
                color: '',
                special: false
            };
        },

        _row(item) {
            if (this.kind === 'user') return this._userRow(item, item.id, '');
            var row = {
                key: 'v-' + item.id,
                value: String(item.id),
                label: item.name,
                secondary: '',
                isUser: false,
                color: this.kind === 'tag' && item.color ? 'background:' + item.color : '',
                special: false
            };
            if (this.kind === 'version') {
                var notes = [];
                if (item.project_key && item.project_key !== this.projectKey) notes.push(item.project_key);
                if (item.status && item.status !== 'open') notes.push(item.status);
                row.secondary = notes.join(' · ');
            }
            return row;
        },

        _specialRows(q) {
            var lower = (q || '').toLowerCase();
            var rows = [];
            var self = this;
            this.specials.forEach(function (s) {
                if (lower && String(s.label).toLowerCase().indexOf(lower) === -1) return;
                var row;
                if (s.user) {
                    row = self._userRow(s.user, s.value, s.label);
                } else {
                    row = { value: s.value, label: s.label, secondary: '', isUser: false, color: '' };
                }
                row.key = 's-' + s.value;
                row.special = true;
                rows.push(row);
            });
            return rows;
        },

        /* ---- Fetching ---- */

        _url(q, offset) {
            var sep = this.endpoint.indexOf('?') === -1 ? '?' : '&';
            var url = this.endpoint + sep + 'limit=' + this.pageSize + '&offset=' + offset;
            if (q) url += '&q=' + encodeURIComponent(q);
            return url;
        },

        async fetchPage(append) {
            var q = (this.query || '').trim();
            var offset = append ? this.offset : 0;
            var seq = ++this.requestSeq;
            this.loading = true;
            try {
                var res = await spFetch(this._url(q, offset));
                if (seq !== this.requestSeq) return;
                if (res.ok) {
                    var data = await res.json();
                    if (seq !== this.requestSeq) return;
                    var list = Array.isArray(data) ? data : (data.items || []);
                    var rows = list.map(this._row.bind(this));
                    if (append) {
                        this.items = this.items.concat(rows);
                    } else {
                        this.items = this._specialRows(q).concat(rows);
                        this.activeIndex = this.items.length ? 0 : -1;
                    }
                    this.offset = offset + list.length;
                    this.hasMore = list.length >= this.pageSize;
                } else if (!append) {
                    this.items = this._specialRows(q);
                    this.hasMore = false;
                }
            } catch (_e) {
                if (!append) this.items = this._specialRows(q);
                this.hasMore = false;
            }
            if (seq === this.requestSeq) this.loading = false;
        },

        search() {
            var q = (this.query || '').trim();
            if (q === this._lastQuery) return;
            this._lastQuery = q;
            clearTimeout(this.debounceTimer);
            var self = this;
            this.debounceTimer = setTimeout(function () { self.fetchPage(false); }, q ? 200 : 0);
        },

        loadMore() {
            if (this.loading || !this.hasMore) return;
            this.fetchPage(true);
        },

        onScroll(event) {
            var el = event.target;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) this.loadMore();
        },

        /* ---- Interaction ---- */

        onFocus() {
            this.open = true;
            if (this._lastQuery === null) this.search();
        },

        onBlur() {
            var self = this;
            // Delay so a click on a row still lands.
            setTimeout(function () { self.close(); }, 150);
        },

        close() {
            this.open = false;
            this.activeIndex = -1;
        },

        _scrollActiveIntoView() {
            var self = this;
            this.$nextTick(function () {
                var el = document.getElementById(self.optionId(self.activeIndex));
                if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
            });
        },

        moveDown() {
            if (!this.open) {
                this.open = true;
                if (this._lastQuery === null) this.search();
                else if (this.items.length) this.activeIndex = 0;
                return;
            }
            if (this.activeIndex < this.items.length - 1) {
                this.activeIndex++;
                this._scrollActiveIntoView();
            }
            if (this.activeIndex >= this.items.length - 2) this.loadMore();
        },

        moveUp() {
            if (this.activeIndex > 0) {
                this.activeIndex--;
                this._scrollActiveIntoView();
            }
        },

        onEnter(event) {
            // With the list open Enter picks; otherwise it submits the form as usual.
            if (!this.open) return;
            event.preventDefault();
            if (this.activeIndex >= 0 && this.activeIndex < this.items.length) {
                this.choose(this.items[this.activeIndex]);
            }
        },

        onEscape() {
            if (this.open) {
                this.close();
            } else {
                this.query = '';
            }
        },

        choose(item) {
            var input = this.$refs.value;
            if (!input) return;
            input.value = item.value;
            input.disabled = false;
            this.close();
            var form = this.$el.closest('form');
            if (!form) return;
            // The form carries no offset, so the filtered list starts at page one.
            if (form.requestSubmit) form.requestSubmit(); else form.submit();
        }
    };
}
