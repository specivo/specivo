/* Admin > Groups list.
 *
 * A user group is an access grant: putting someone in one hands them every
 * role the group holds on every project it is a member of. Deleting a group
 * therefore revokes access in bulk, which is why the delete flow here shows
 * the two counts already on the row before the button is pressed rather than
 * reporting them afterwards.
 */
export function adminGroups(initial) {
    var i18n = Object.assign({
        createFailed: 'Failed to create group.',
        saveFailed: 'Failed to save group.',
        deleteFailed: 'Failed to delete group.'
    }, initial.i18n || {});

    return {
        groups: initial.groups || [],
        filter: '',

        get visibleGroups() {
            var needle = this.filter.trim().toLowerCase();
            if (!needle) return this.groups;
            return this.groups.filter(function (g) {
                return g.name.toLowerCase().indexOf(needle) !== -1
                    || (g.description || '').toLowerCase().indexOf(needle) !== -1;
            });
        },

        groupUrl(g) {
            return '/admin/groups/' + g.id + '/';
        },

        _readError(err, fallback) {
            return (err.errors && err.errors[0] && err.errors[0].message) || err.detail || fallback;
        },

        // ---------------------------------------------------------------
        // Create
        // ---------------------------------------------------------------

        showCreate: false,
        creating: false,
        createError: '',
        newGroup: { name: '', description: '' },

        openCreate() {
            this.newGroup = { name: '', description: '' };
            this.createError = '';
            this.showCreate = true;
        },

        async createGroup() {
            if (!this.newGroup.name.trim()) return;
            this.creating = true;
            this.createError = '';
            var res = await spFetch('/api/v1/admin/groups/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    name: this.newGroup.name.trim(),
                    description: this.newGroup.description.trim() || null
                })
            });
            if (res.ok) {
                var created = await res.json();
                this.groups.push({
                    id: created.id,
                    name: created.name,
                    description: created.description || '',
                    user_count: 0,
                    project_count: 0
                });
                this.showCreate = false;
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.createError = this._readError(err, i18n.createFailed);
            }
            this.creating = false;
        },

        // ---------------------------------------------------------------
        // Rename / edit description
        // ---------------------------------------------------------------

        showEdit: false,
        editing: false,
        editError: '',
        editGroup: null,
        editDraft: { name: '', description: '' },

        openEdit(g) {
            this.editGroup = g;
            this.editDraft = { name: g.name, description: g.description || '' };
            this.editError = '';
            this.showEdit = true;
        },

        async saveGroup() {
            if (!this.editGroup || !this.editDraft.name.trim()) return;
            this.editing = true;
            this.editError = '';
            var res = await spFetch('/api/v1/admin/groups/' + this.editGroup.id + '/', {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    name: this.editDraft.name.trim(),
                    description: this.editDraft.description.trim() || null
                })
            });
            if (res.ok) {
                var updated = await res.json();
                this.editGroup.name = updated.name;
                this.editGroup.description = updated.description || '';
                this.showEdit = false;
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.editError = this._readError(err, i18n.saveFailed);
            }
            this.editing = false;
        },

        // ---------------------------------------------------------------
        // Delete
        //
        // The modal is opened with the row still in hand, so the counts it
        // shows are the ones the administrator was already looking at. No
        // request is made to work out the blast radius — it is on the row.
        // ---------------------------------------------------------------

        showDelete: false,
        deleting: false,
        deleteError: '',
        deleteGroup: null,

        openDelete(g) {
            this.deleteGroup = g;
            this.deleteError = '';
            this.showDelete = true;
        },

        /** True when deleting this group takes real access away with it. */
        get deleteHasConsequences() {
            if (!this.deleteGroup) return false;
            return this.deleteGroup.user_count > 0 && this.deleteGroup.project_count > 0;
        },

        async confirmDelete() {
            if (!this.deleteGroup) return;
            this.deleting = true;
            this.deleteError = '';
            var id = this.deleteGroup.id;
            var res = await spFetch('/api/v1/admin/groups/' + id + '/', {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'}
            });
            if (res.ok || res.status === 204) {
                this.groups = this.groups.filter(function (g) { return g.id !== id; });
                this.showDelete = false;
                this.deleteGroup = null;
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.deleteError = this._readError(err, i18n.deleteFailed);
            }
            this.deleting = false;
        }
    };
}
