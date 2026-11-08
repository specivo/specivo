/* The "Limit to projects" picker of the API key create forms.
 *
 * Two pages create keys: Your API keys (/my/api-keys/, apiKeyManager) and an
 * account's page in Admin > Users (adminUserDetail). Both render the picker
 * from pages/auth/_api_key_project_picker.html and mix this object into their
 * Alpine component with Object.assign, so the list, the filter and the labels
 * of the key list behave the same on both.
 *
 * The projects come from GET /api/v1/projects/, which lists the projects the
 * signed-in user can see. A self-service key may be limited only to those; an
 * administrator sees every project, and the admin route only checks that the
 * projects exist. Nothing ticked sends no `scopes`, so the key is not limited.
 * The ticked projects are sent by id, which is also how the server stores them.
 *
 * The host component sets `limitI18n` before the picker is shown:
 *   allHint   - the line shown while nothing is ticked
 *   all       - the key list label of a key that is not limited
 *   limitedTo - "Limited to: %(projects)s"
 *   none      - the key list label of a limited key whose projects are all gone
 */

// GET /api/v1/projects/ answers at most 200 projects per page.
var PAGE_SIZE = 200;
// Above this many projects the list gets a filter box.
var FILTER_FROM = 15;

export function apiKeyProjectPicker() {
    return {
        limitI18n: {},
        limitProjects: [],
        limitSelected: [],
        limitFilter: '',
        limitLoading: false,
        limitLoaded: false,
        limitLoadFailed: false,

        /** Load every project the viewer can see, page by page; once per page view. */
        async loadLimitProjects() {
            if (this.limitLoaded || this.limitLoading) return;
            this.limitLoading = true;
            this.limitLoadFailed = false;
            var all = [];
            try {
                for (var offset = 0; ; offset += PAGE_SIZE) {
                    var res = await spFetch('/api/v1/projects/?limit=' + PAGE_SIZE + '&offset=' + offset);
                    if (!res.ok) throw new Error('projects ' + res.status);
                    var page = await res.json();
                    var items = page.items || [];
                    items.forEach(function (p) { all.push({ id: p.id, key: p.key, name: p.name }); });
                    if (items.length < PAGE_SIZE || all.length >= page.total_count) break;
                }
                this.limitProjects = all;
                this.limitLoaded = true;
            } catch (_e) {
                this.limitLoadFailed = true;
            }
            this.limitLoading = false;
        },

        limitShowFilter() {
            return this.limitProjects.length > FILTER_FROM;
        },

        /** The projects matching the filter, by key or name. Ticked ones stay ticked while hidden. */
        limitVisibleProjects() {
            var query = this.limitFilter.trim().toLowerCase();
            if (!query) return this.limitProjects;
            return this.limitProjects.filter(function (p) {
                return p.key.toLowerCase().includes(query) || p.name.toLowerCase().includes(query);
            });
        },

        limitNoMatch() {
            return this.limitProjects.length > 0 && this.limitVisibleProjects().length === 0;
        },

        /** The keys of the ticked projects, in list order. */
        limitSelectedKeys() {
            var selected = this.limitSelected;
            return this.limitProjects
                .filter(function (p) { return selected.indexOf(p.id) !== -1; })
                .map(function (p) { return p.key; });
        },

        /** The line under the list: what the key will reach. */
        limitSummary() {
            var keys = this.limitSelectedKeys();
            if (!keys.length) return this.limitI18n.allHint || '';
            return (this.limitI18n.limitedTo || 'Limited to: %(projects)s').replace('%(projects)s', keys.join(', '));
        },

        /** The `scopes` value for the create request, or null for a key that is not limited. */
        limitScopes() {
            return this.limitSelected.length ? { projects: this.limitSelected.slice() } : null;
        },

        resetLimit() {
            this.limitSelected = [];
            this.limitFilter = '';
        },

        /** The key list label of *key*'s limit, from its `project_keys`. */
        limitLabel(key) {
            var keys = key.project_keys;
            if (keys === null || keys === undefined) return this.limitI18n.all || 'All projects';
            if (!keys.length) return this.limitI18n.none || '';
            return (this.limitI18n.limitedTo || 'Limited to: %(projects)s').replace('%(projects)s', keys.join(', '));
        }
    };
}
