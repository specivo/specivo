/* Admin > Roles > one role: the editor.
 *
 * The permission checkboxes are rendered by the server from the permission
 * catalogue; this component holds which ones are ticked and keeps them
 * consistent with the catalogue's dependency rules:
 *
 *   - ticking X ticks everything X requires (``requires`` is the transitive
 *     closure, so one pass is enough);
 *   - unticking Y unticks every permission that requires Y.
 *
 * The API normalises and checks the same rules on save, so this is a
 * convenience, not the guard. Full access sends ["*"] and hides the boxes.
 * Save is one PATCH carrying ``lock_version``; a 409 conflict means someone
 * else saved first, and the page offers a reload instead of overwriting.
 */
var FULL_ACCESS = '*';

function errorOf(body) {
    return (body && body.errors && body.errors[0]) || null;
}

export function adminRoleEditor(initial) {
    var i18n = Object.assign({
        saved: 'Saved.',
        saveFailed: 'Failed to save the role.',
        conflict: 'Someone else changed this role; reload to see their changes.',
        deleteFailed: 'Failed to delete the role.',
        confirmDelete: 'Delete the role %s? This cannot be undone.'
    }, initial.i18n || {});

    var role = initial.role || {};
    var catalogue = initial.catalogue || { groups: [], non_member_allowed: [] };

    // Catalogue order of every key, each key's closure, and the keys per group.
    var order = [];
    var requires = {};
    var groupKeys = {};
    catalogue.groups.forEach(function (g) {
        groupKeys[g.key] = [];
        g.permissions.forEach(function (p) {
            order.push(p.key);
            requires[p.key] = p.requires || [];
            groupKeys[g.key].push(p.key);
        });
    });

    // Non member may only hold the allow-listed keys; the rest are disabled.
    var allowed = null;
    if (role.non_member) {
        allowed = {};
        (catalogue.non_member_allowed || []).forEach(function (k) { allowed[k] = true; });
    }

    function grantedMap(permissions) {
        var map = {};
        var full = permissions.indexOf(FULL_ACCESS) !== -1;
        order.forEach(function (k) {
            // Leaving Full access starts from everything ticked, not from nothing.
            map[k] = full ? (allowed === null || !!allowed[k]) : permissions.indexOf(k) !== -1;
        });
        return map;
    }

    var startPermissions = role.permissions || [];

    return {
        roleId: role.id,
        builtin: !!role.builtin,
        nonMember: !!role.non_member,
        name: role.name || '',
        savedName: role.name || '',
        assignable: !!role.assignable,
        fullAccess: !role.non_member && startPermissions.indexOf(FULL_ACCESS) !== -1,
        granted: grantedMap(startPermissions),
        usersVisibility: role.users_visibility,
        timeEntriesVisibility: role.time_entries_visibility,
        aiUsageVisibility: role.ai_usage_visibility,
        lockVersion: role.lock_version,

        saving: false,
        saved: false,
        savedText: i18n.saved,
        error: '',
        nameError: '',
        conflict: false,

        deleting: false,
        deleteError: '',
        deleteProjects: [],

        // -----------------------------------------------------------
        // Permission checkboxes
        // -----------------------------------------------------------

        isOn(key) {
            return !!this.granted[key];
        },

        isDisabled(key) {
            return allowed !== null && !allowed[key];
        },

        toggle(key, on) {
            var granted = this.granted;
            var self = this;
            if (on) {
                if (this.isDisabled(key)) return;
                granted[key] = true;
                requires[key].forEach(function (k) {
                    if (!self.isDisabled(k)) granted[k] = true;
                });
            } else {
                granted[key] = false;
                order.forEach(function (k) {
                    if (requires[k].indexOf(key) !== -1) granted[k] = false;
                });
            }
            this.saved = false;
        },

        enabledKeys(groupKey) {
            var self = this;
            return (groupKeys[groupKey] || []).filter(function (k) { return !self.isDisabled(k); });
        },

        groupEmpty(groupKey) {
            return this.enabledKeys(groupKey).length === 0;
        },

        groupAll(groupKey) {
            var keys = this.enabledKeys(groupKey);
            var self = this;
            return keys.length > 0 && keys.every(function (k) { return self.isOn(k); });
        },

        groupSome(groupKey) {
            var self = this;
            return this.enabledKeys(groupKey).some(function (k) { return self.isOn(k); });
        },

        // "indeterminate" is a DOM property, not an attribute, so x-bind can't set it.
        markGroup(el, groupKey) {
            el.indeterminate = this.groupSome(groupKey) && !this.groupAll(groupKey);
        },

        toggleGroup(groupKey, on) {
            var self = this;
            this.enabledKeys(groupKey).forEach(function (k) { self.toggle(k, on); });
        },

        permissionList() {
            if (this.fullAccess && !this.nonMember) return [FULL_ACCESS];
            var granted = this.granted;
            return order.filter(function (k) { return granted[k]; });
        },

        // -----------------------------------------------------------
        // Save
        // -----------------------------------------------------------

        payload() {
            var body = {
                lock_version: this.lockVersion,
                permissions: this.permissionList(),
                users_visibility: this.usersVisibility,
                time_entries_visibility: this.timeEntriesVisibility,
                ai_usage_visibility: this.aiUsageVisibility
            };
            if (!this.builtin) {
                body.name = this.name.trim();
                body.assignable = this.assignable;
            }
            return body;
        },

        applySaved(out) {
            this.lockVersion = out.lock_version;
            this.name = out.name;
            this.savedName = out.name;
            this.assignable = !!out.assignable;
            var permissions = out.permissions || [];
            this.fullAccess = !this.nonMember && permissions.indexOf(FULL_ACCESS) !== -1;
            this.granted = grantedMap(permissions);
            this.usersVisibility = out.users_visibility;
            this.timeEntriesVisibility = out.time_entries_visibility;
            this.aiUsageVisibility = out.ai_usage_visibility;
        },

        async save() {
            if (this.saving) return;
            this.saving = true;
            this.saved = false;
            this.error = '';
            this.nameError = '';
            this.conflict = false;
            try {
                var res = await spFetch('/api/v1/admin/roles/' + this.roleId + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(this.payload())
                });
                if (res.ok) {
                    this.applySaved(await res.json());
                    this.saved = true;
                } else {
                    this.showSaveError(res.status, errorOf(await res.json().catch(function () { return {}; })));
                }
            } catch (_e) {
                this.error = i18n.saveFailed;
            }
            this.saving = false;
        },

        showSaveError(status, first) {
            if (status === 409 && first && first.code === 'conflict_lock_version') {
                this.conflict = true;
                this.error = i18n.conflict;
                return;
            }
            var message = (first && first.message) || i18n.saveFailed;
            if (first && first.field === 'name') {
                this.nameError = message;
            } else {
                this.error = message;
            }
        },

        reload() {
            window.location.reload();
        },

        // -----------------------------------------------------------
        // Delete
        // -----------------------------------------------------------

        async remove() {
            if (this.deleting) return;
            if (!confirm(i18n.confirmDelete.replace('%s', this.savedName))) return;
            this.deleting = true;
            this.deleteError = '';
            this.deleteProjects = [];
            try {
                var res = await spFetch('/api/v1/admin/roles/' + this.roleId + '/', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) {
                    window.location.href = spUrl('/admin/roles/');
                    return;
                }
                var first = errorOf(await res.json().catch(function () { return {}; }));
                this.deleteError = (first && first.message) || i18n.deleteFailed;
                if (first && first.details && Array.isArray(first.details.projects)) {
                    this.deleteProjects = first.details.projects;
                }
            } catch (_e) {
                this.deleteError = i18n.deleteFailed;
            }
            this.deleting = false;
        }
    };
}
