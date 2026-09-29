/* Project settings > Members tab.
 *
 * A project membership is held by a user or by a user group, and this list
 * shows both kinds side by side. Every row therefore carries a
 * `principal_type` and is addressed as `{principal_type}/{principal_id}`;
 * nothing here may assume a `user_id` exists, because a group row has none.
 *
 * Group rows also carry the users the group covers (`users`), which is what
 * makes access arriving through a group visible on this screen rather than
 * implied. The members API does not return that list, so a group added
 * during the session reloads the page to pick it up.
 */
export function projectMembers(initial) {
    // User-facing strings are translated in the template and handed in here,
    // because these are composed with runtime values that Jinja cannot see.
    // The defaults are a last resort for a caller that forgets to pass them.
    var i18n = Object.assign({
        onePerson: '1 person',
        nPeople: '%d people',
        added: 'added.',
        addFailed: 'Failed to add member.',
        saveFailed: 'Failed to update roles.',
        confirmRemoveUser: 'Remove %s from this project?',
        confirmRemoveGroup: 'Remove the group %s? %d people lose the access it grants.',
        kindGroup: 'Group',
        kindUser: 'User'
    }, initial.i18n || {});

    return {
        members: initial.members || [],
        projectKey: initial.projectKey || '',
        roles: initial.roles || [],
        allGroups: initial.allGroups || [],

        // Exposed so the template can render them without splicing a
        // translated string into a JS literal inside an HTML attribute — an
        // apostrophe in the translation would truncate the attribute.
        kindGroup: i18n.kindGroup,
        kindUser: i18n.kindUser,

        // ---------------------------------------------------------------
        // Row identity and shape
        // ---------------------------------------------------------------

        isGroup(m) {
            return m.principal_type === 'group';
        },

        /** The id of whoever holds the row, whichever kind it is. */
        principalId(m) {
            return m.principal_type === 'group' ? m.group_id : m.user_id;
        },

        /**
         * Stable x-for key. `user_id` alone collapses every group row onto
         * `undefined`, so the kind is part of the key.
         */
        principalKey(m) {
            return m.principal_type + ':' + this.principalId(m);
        },

        /** What to call the holder: a group's name, or a user's display name. */
        principalName(m) {
            return m.principal_type === 'group' ? m.name : m.display_name;
        },

        /** The secondary line: a user's login, or how many people a group covers. */
        principalSubtitle(m) {
            if (m.principal_type === 'group') {
                return m.user_count === 1 ? i18n.onePerson : i18n.nPeople.replace('%d', m.user_count);
            }
            return '(' + m.login + ')';
        },

        memberUrl(m) {
            return '/api/v1/projects/' + this.projectKey + '/members/' + m.principal_type + '/' + this.principalId(m) + '/';
        },

        joinRoles(m) {
            return m.roles.join(', ');
        },

        // ---------------------------------------------------------------
        // Counts
        //
        // Two different numbers that a single "members" label would hide:
        // the table lists grants (rows), and a group grant reaches many
        // people. Both are computed from the loaded rows so they stay
        // correct as rows are added and removed.
        // ---------------------------------------------------------------

        get directCount() {
            return this.members.filter(function (m) { return m.principal_type === 'user'; }).length;
        },

        get groupCount() {
            return this.members.filter(function (m) { return m.principal_type === 'group'; }).length;
        },

        /** Distinct humans the rows reach — a user in two groups counts once. */
        get peopleCount() {
            var seen = {};
            this.members.forEach(function (m) {
                if (m.principal_type === 'user') {
                    seen[m.user_id] = true;
                } else if (Array.isArray(m.users)) {
                    m.users.forEach(function (u) { seen[u.user_id] = true; });
                }
            });
            return Object.keys(seen).length;
        },

        // ---------------------------------------------------------------
        // Group expansion — who a group row actually covers
        // ---------------------------------------------------------------

        expanded: [],

        isExpanded(m) {
            return this.expanded.indexOf(this.principalKey(m)) !== -1;
        },

        toggleExpanded(m) {
            var key = this.principalKey(m);
            var at = this.expanded.indexOf(key);
            if (at === -1) {
                this.expanded.push(key);
            } else {
                this.expanded.splice(at, 1);
            }
        },

        /** True when this person holds no direct row — they are here only via a group. */
        onlyViaGroup(userId) {
            return !this.members.some(function (m) {
                return m.principal_type === 'user' && m.user_id === userId;
            });
        },

        // ---------------------------------------------------------------
        // Add-member picker — one field, users and groups together
        // ---------------------------------------------------------------

        userQuery: '',
        suggestions: [],
        showSuggestions: false,
        selected: null,
        selectedRoleId: '',
        adding: false,
        addError: '',
        addSuccess: '',

        /** Ids already holding a row, so the picker cannot offer a duplicate. */
        _takenIds(kind) {
            return this.members
                .filter(function (m) { return m.principal_type === kind; })
                .map(function (m) { return kind === 'group' ? m.group_id : m.user_id; });
        },

        _matchingGroups(query) {
            var taken = this._takenIds('group');
            var needle = query.toLowerCase();
            return this.allGroups
                .filter(function (g) {
                    return taken.indexOf(g.id) === -1 && g.name.toLowerCase().indexOf(needle) !== -1;
                })
                .map(function (g) {
                    return {
                        kind: 'group',
                        id: g.id,
                        name: g.name,
                        subtitle: g.user_count === 1 ? i18n.onePerson : i18n.nPeople.replace('%d', g.user_count)
                    };
                });
        },

        async searchPrincipals() {
            this.addError = '';
            this.addSuccess = '';
            var query = this.userQuery.trim();
            if (query.length < 1) {
                this.suggestions = [];
                this.showSuggestions = false;
                return;
            }

            // Groups come from the page (they were rendered with it); users
            // are searched server-side. Groups lead, because there are few of
            // them and an administrator granting access to a team should see
            // the team before scrolling past a list of individuals.
            var groups = this._matchingGroups(query);

            var users = [];
            var res = await spFetch('/api/v1/users/autocomplete/?q=' + encodeURIComponent(query));
            if (res.ok) {
                var taken = this._takenIds('user');
                var data = await res.json();
                users = data
                    .filter(function (u) { return taken.indexOf(u.id) === -1; })
                    .map(function (u) {
                        return { kind: 'user', id: u.id, name: u.display_name, subtitle: '(' + u.login + ')' };
                    });
            }

            this.suggestions = groups.concat(users);
            this.showSuggestions = true;
        },

        suggestionKey(s) {
            return s.kind + ':' + s.id;
        },

        selectPrincipal(s) {
            this.selected = s;
            this.userQuery = s.name;
            this.showSuggestions = false;
            this.suggestions = [];
        },

        clearSelection() {
            this.selected = null;
        },

        async addMember() {
            if (!this.selected || !this.selectedRoleId) return;
            this.adding = true;
            this.addError = '';
            this.addSuccess = '';

            var body = { role_ids: [parseInt(this.selectedRoleId)] };
            if (this.selected.kind === 'group') {
                body.group_id = this.selected.id;
            } else {
                body.user_id = this.selected.id;
            }

            var res = await spFetch('/api/v1/projects/' + this.projectKey + '/members/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });

            if (res.ok) {
                var member = await res.json();
                // A group row needs the list of users it covers to show who
                // the grant reaches, and the members API does not carry it.
                // Re-render the page rather than display a row that cannot
                // answer the question the group column exists to answer.
                if (member.principal_type === 'group') {
                    window.location.reload();
                    return;
                }
                var self = this;
                var existing = this.members.find(function (m) {
                    return m.principal_type === member.principal_type
                        && self.principalId(m) === self.principalId(member);
                });
                if (existing) {
                    existing.roles = member.roles;
                    existing.role_ids = member.role_ids || [];
                } else {
                    this.members.push(member);
                }
                this.addSuccess = this.principalName(member) + ' — ' + i18n.added;
                this.userQuery = '';
                this.selected = null;
                this.selectedRoleId = '';
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.addError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.addFailed;
            }
            this.adding = false;
        },

        // ---------------------------------------------------------------
        // Remove
        // ---------------------------------------------------------------

        async removeMember(m) {
            var warning = m.principal_type === 'group'
                ? i18n.confirmRemoveGroup.replace('%s', this.principalName(m)).replace('%d', m.user_count)
                : i18n.confirmRemoveUser.replace('%s', this.principalName(m));
            if (!confirm(warning)) return;

            var res = await spFetch(this.memberUrl(m), {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'}
            });
            if (res.ok || res.status === 204) {
                var key = this.principalKey(m);
                var self = this;
                this.members = this.members.filter(function (row) { return self.principalKey(row) !== key; });
            }
        },

        // ---------------------------------------------------------------
        // Edit roles
        // ---------------------------------------------------------------

        editModal: false,
        editMember: null,
        editRoleIds: [],
        editSaving: false,
        editError: '',

        openEditRoles(member) {
            this.editMember = member;
            // Prefer role_ids from the server; fall back to mapping names.
            if (Array.isArray(member.role_ids) && member.role_ids.length > 0) {
                this.editRoleIds = member.role_ids.slice();
            } else {
                var roleMap = {};
                this.roles.forEach(function (r) { roleMap[r.name] = r.id; });
                this.editRoleIds = member.roles.map(function (name) { return roleMap[name]; }).filter(Boolean);
            }
            this.editError = '';
            this.editModal = true;
        },

        editTitle() {
            return this.editMember ? this.principalName(this.editMember) : '';
        },

        async saveRoles() {
            if (!this.editMember || this.editRoleIds.length === 0) return;
            this.editSaving = true;
            this.editError = '';
            var res = await spFetch(this.memberUrl(this.editMember), {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ role_ids: this.editRoleIds })
            });
            if (res.ok) {
                var updated = await res.json();
                var self = this;
                var m = this.members.find(function (row) {
                    return row.principal_type === updated.principal_type
                        && self.principalId(row) === self.principalId(updated);
                });
                if (m) {
                    m.roles = updated.roles;
                    m.role_ids = updated.role_ids || [];
                }
                this.editModal = false;
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.editError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.saveFailed;
            }
            this.editSaving = false;
        }
    };
}
