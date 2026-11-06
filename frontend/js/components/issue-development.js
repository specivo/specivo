/**
 * The issue page's Development tab: detach a commit or pull request from the
 * issue, or move it to another issue.
 *
 * The tab body is server-rendered (pages/issues/_development*.html); each
 * commit and pull request a person may edit carries `data-dev-kind`,
 * `data-dev-id` and `data-dev-label`, and its menu calls `detach($el)` or
 * `openMove($el)`. Both use the REST endpoints
 * (`DELETE /api/v1/issues/{key}/development/{commits|pull-requests}/{id}/`
 * and `POST .../move/`). On success the body reloads itself through htmx
 * (`sp-dev-refresh` on `x-ref="body"`), which also updates the tab's count.
 * A refused request shows the server's message: above the tab body for a
 * detach, under the issue field for a move.
 *
 * The issue field suggests issues as the relations form does
 * (`/api/v1/issues/autocomplete/`).
 */
export function issueDevelopment(initial) {
    var i18n = Object.assign({
        confirmDetach: 'Detach {item} from this issue?',
        detachFailed: 'Could not detach it.',
        moveFailed: 'Could not move it.',
        connectFailed: 'Unable to connect.',
        enterIssue: 'Enter the key of the issue to move it to.',
        moveLead: 'Move {item} to the issue below.'
    }, initial.i18n || {});
    var PATHS = {commit: 'commits', pull_request: 'pull-requests'};
    var searchTimer = null;

    function itemOf(el) {
        var row = el && el.closest('[data-dev-id]');
        if (!row) return null;
        return {
            kind: row.getAttribute('data-dev-kind'),
            id: row.getAttribute('data-dev-id'),
            label: row.getAttribute('data-dev-label') || ''
        };
    }

    return {
        issueKey: initial.issueKey || '',
        error: '',
        busy: false,
        move: null,
        target: '',
        moveError: '',
        suggestions: [],
        showSuggestions: false,
        highlightIndex: -1,

        init() {
            var self = this;
            this.$watch('target', function (val) {
                self.moveError = '';
                clearTimeout(searchTimer);
                var query = (val || '').trim();
                if (query.length < 2) {
                    self.suggestions = [];
                    self.showSuggestions = false;
                    return;
                }
                searchTimer = setTimeout(function () { self.search(query); }, 250);
            });
        },

        _url(item) {
            return '/api/v1/issues/' + encodeURIComponent(this.issueKey) + '/development/' +
                PATHS[item.kind] + '/' + encodeURIComponent(item.id) + '/';
        },

        async _message(res, fallback) {
            var body = await res.json().catch(function () { return {}; });
            return (body.errors && body.errors[0] && body.errors[0].message) || fallback;
        },

        refresh() {
            if (window.htmx && this.$refs.body) window.htmx.trigger(this.$refs.body, 'sp-dev-refresh');
        },

        async detach(el) {
            var item = itemOf(el);
            if (!item || this.busy) return;
            if (!window.confirm(i18n.confirmDetach.replace('{item}', item.label))) return;
            this.busy = true;
            this.error = '';
            try {
                var res = await spFetch(this._url(item), {method: 'DELETE'});
                if (res.ok) {
                    this.refresh();
                } else {
                    this.error = await this._message(res, i18n.detachFailed);
                    // Gone already (detached or moved elsewhere): show the tab as it is now.
                    if (res.status === 404) this.refresh();
                }
            } catch (_e) {
                this.error = i18n.connectFailed;
            }
            this.busy = false;
        },

        openMove(el) {
            var item = itemOf(el);
            if (!item) return;
            this.error = '';
            this.target = '';
            this.moveError = '';
            this.suggestions = [];
            this.showSuggestions = false;
            item.lead = i18n.moveLead.replace('{item}', item.label);
            this.move = item;
        },

        closeMove() {
            this.move = null;
        },

        focusLater(el) {
            this.$nextTick(function () { el.focus(); });
        },

        async search(query) {
            try {
                var res = await spFetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(query) + '&limit=8');
                if (!res.ok) return;
                var key = this.issueKey.toUpperCase();
                var data = await res.json();
                this.suggestions = data.filter(function (r) { return String(r.key).toUpperCase() !== key; });
                this.showSuggestions = this.suggestions.length > 0 && this.move !== null;
                this.highlightIndex = -1;
            } catch (_e) {
                /* suggestions are a convenience; typing the key still works */
            }
        },

        selectSuggestion(s) {
            this.target = s.key;
            this.showSuggestions = false;
            this.suggestions = [];
        },

        onKeydown(event) {
            if (event.key === 'Enter' && !(this.showSuggestions && this.highlightIndex >= 0)) {
                event.preventDefault();
                this.submitMove();
                return;
            }
            if (!this.showSuggestions) return;
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                this.highlightIndex = Math.min(this.highlightIndex + 1, this.suggestions.length - 1);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                this.highlightIndex = Math.max(this.highlightIndex - 1, 0);
            } else if (event.key === 'Enter') {
                event.preventDefault();
                this.selectSuggestion(this.suggestions[this.highlightIndex]);
            } else if (event.key === 'Escape') {
                event.stopPropagation();
                this.showSuggestions = false;
            }
        },

        closeSuggestions() {
            var self = this;
            setTimeout(function () { self.showSuggestions = false; }, 200);
        },

        async submitMove() {
            if (!this.move || this.busy) return;
            var target = this.target.trim();
            if (!target) {
                this.moveError = i18n.enterIssue;
                return;
            }
            this.busy = true;
            this.moveError = '';
            try {
                var res = await spFetch(this._url(this.move) + 'move/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({target_issue_ref: target})
                });
                if (res.ok) {
                    this.move = null;
                    this.refresh();
                } else {
                    this.moveError = await this._message(res, i18n.moveFailed);
                }
            } catch (_e) {
                this.moveError = i18n.connectFailed;
            }
            this.busy = false;
        }
    };
}
