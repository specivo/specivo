export function projectMembers(initial) {
        return {
            members: initial.members || [],
            projectKey: initial.projectKey || '',
            roles: initial.roles || [],

            joinRoles(m) {
                return m.roles.join(', ');
            },

            // Add member form state
            userQuery: '',
            suggestions: [],
            showSuggestions: false,
            selectedUserId: null,
            selectedRoleId: '',
            adding: false,
            addError: '',
            addSuccess: '',

            async searchUsers() {
                this.addError = '';
                this.addSuccess = '';
                if (this.userQuery.length < 1) {
                    this.suggestions = [];
                    this.showSuggestions = false;
                    return;
                }
                var res = await spFetch('/api/v1/users/autocomplete/?q=' + encodeURIComponent(this.userQuery));
                if (res.ok) {
                    var data = await res.json();
                    // Exclude users who are already members
                    var memberIds = this.members.map(function (m) { return m.user_id; });
                    this.suggestions = data.filter(function (u) { return memberIds.indexOf(u.id) === -1; });
                    this.showSuggestions = true;
                }
            },

            selectUser(u) {
                this.selectedUserId = u.id;
                this.userQuery = u.display_name + ' (' + u.login + ')';
                this.showSuggestions = false;
                this.suggestions = [];
            },

            async addMember() {
                if (!this.selectedUserId || !this.selectedRoleId) return;
                this.adding = true;
                this.addError = '';
                this.addSuccess = '';
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/members/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ user_id: this.selectedUserId, role_ids: [parseInt(this.selectedRoleId)] })
                });
                if (res.ok) {
                    var member = await res.json();
                    // Update or add in the local list
                    var existing = this.members.find(function (m) { return m.user_id === member.user_id; });
                    if (existing) {
                        existing.roles = member.roles;
                        existing.role_ids = member.role_ids || [];
                    } else {
                        this.members.push(member);
                    }
                    this.addSuccess = member.display_name + ' added as member.';
                    this.userQuery = '';
                    this.selectedUserId = null;
                    this.selectedRoleId = '';
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.addError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to add member.';
                }
                this.adding = false;
            },

            async removeMember(userId) {
                if (!confirm('Remove this member?')) return;
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/members/' + userId + '/', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) {
                    this.members = this.members.filter(function (m) { return m.user_id !== userId; });
                }
            },

            // Edit roles modal state
            editModal: false,
            editMember: null,
            editRoleIds: [],
            editSaving: false,
            editError: '',

            openEditRoles(member) {
                this.editMember = member;
                // Prefer role_ids from the server; fall back to mapping names for older payloads.
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

            async saveRoles() {
                if (!this.editMember || this.editRoleIds.length === 0) return;
                this.editSaving = true;
                this.editError = '';
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/members/' + this.editMember.user_id + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ role_ids: this.editRoleIds })
                });
                if (res.ok) {
                    var updated = await res.json();
                    var m = this.members.find(function (m) { return m.user_id === updated.user_id; });
                    if (m) {
                        m.roles = updated.roles;
                        m.role_ids = updated.role_ids || [];
                    }
                    this.editModal = false;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.editError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to update roles.';
                }
                this.editSaving = false;
            }
        };
    }
