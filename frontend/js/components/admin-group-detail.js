/* Admin > Groups > one group.
 *
 * Two lists that only make sense next to each other: the users in the group,
 * and the projects the group holds membership on. Adding a user to the list
 * on the left grants them everything on the right, so the page shows both
 * rather than making an administrator hold one of them in their head.
 *
 * Only the user list mutates here; the project list is rendered with the page
 * because membership is granted from the project's own settings, not here.
 */
export function adminGroupDetail(initial) {
    var i18n = Object.assign({
        addFailed: 'Failed to add user.',
        removeFailed: 'Failed to remove user.',
        confirmRemove: 'Remove %s from this group?'
    }, initial.i18n || {});

    return {
        groupId: initial.groupId,
        groupName: initial.groupName || '',
        users: initial.users || [],
        projectCount: initial.projectCount || 0,

        // ---------------------------------------------------------------
        // Add a user
        // ---------------------------------------------------------------

        userQuery: '',
        suggestions: [],
        showSuggestions: false,
        selectedUser: null,
        adding: false,
        addError: '',

        async searchUsers() {
            this.addError = '';
            var query = this.userQuery.trim();
            if (query.length < 1) {
                this.suggestions = [];
                this.showSuggestions = false;
                return;
            }
            var res = await spFetch('/api/v1/users/autocomplete/?q=' + encodeURIComponent(query));
            if (res.ok) {
                var present = this.users.map(function (u) { return u.user_id; });
                var data = await res.json();
                this.suggestions = data.filter(function (u) { return present.indexOf(u.id) === -1; });
                this.showSuggestions = true;
            }
        },

        selectUser(u) {
            this.selectedUser = u;
            this.userQuery = u.display_name + ' (' + u.login + ')';
            this.showSuggestions = false;
            this.suggestions = [];
        },

        async addUser() {
            if (!this.selectedUser) return;
            this.adding = true;
            this.addError = '';
            var res = await spFetch('/api/v1/admin/groups/' + this.groupId + '/users/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ user_id: this.selectedUser.id })
            });
            if (res.ok) {
                var added = await res.json();
                var already = this.users.some(function (u) { return u.user_id === added.user_id; });
                if (!already) {
                    this.users.push(added);
                    this.users.sort(function (a, b) { return a.login < b.login ? -1 : 1; });
                }
                this.userQuery = '';
                this.selectedUser = null;
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.addError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.addFailed;
            }
            this.adding = false;
        },

        // ---------------------------------------------------------------
        // Remove a user
        // ---------------------------------------------------------------

        removeError: '',

        async removeUser(u) {
            if (!confirm(i18n.confirmRemove.replace('%s', u.display_name))) return;
            this.removeError = '';
            var res = await spFetch('/api/v1/admin/groups/' + this.groupId + '/users/' + u.user_id + '/', {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'}
            });
            if (res.ok || res.status === 204) {
                this.users = this.users.filter(function (row) { return row.user_id !== u.user_id; });
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.removeError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.removeFailed;
            }
        }
    };
}
