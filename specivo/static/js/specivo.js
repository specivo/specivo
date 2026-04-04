/* ============================================================
   SPECIVO — Global JavaScript (Alpine.js stores + utilities)
   ============================================================ */

/* Service Worker registration (PWA) */
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js');
}

/* Wiki history: compare two versions */
(function () {
    var btn = document.getElementById('btn-compare');
    if (!btn) return;
    var checks = document.querySelectorAll('.version-check');
    function update() {
        var selected = document.querySelectorAll('.version-check:checked');
        if (selected.length === 2) {
            btn.classList.add('enabled');
        } else {
            btn.classList.remove('enabled');
        }
        // Limit to max 2 selections
        if (selected.length >= 2) {
            checks.forEach(function (c) {
                if (!c.checked) c.disabled = true;
            });
        } else {
            checks.forEach(function (c) { c.disabled = false; });
        }
    }
    checks.forEach(function (c) { c.addEventListener('change', update); });
    btn.addEventListener('click', function () {
        var selected = document.querySelectorAll('.version-check:checked');
        if (selected.length !== 2) return;
        var v1 = selected[0].value;
        var v2 = selected[1].value;
        // Sort so older version is first
        var from = Math.min(v1, v2);
        var to = Math.max(v1, v2);
        var path = window.location.pathname.replace('/history/', '/diff/');
        window.location.href = path + '?from_version=' + from + '&to_version=' + to;
    });
})();

/* Pagination page-size select */
(function () {
    var sel = document.querySelector('[data-pagination-limit]');
    if (!sel) return;
    sel.addEventListener('change', function () {
        var base = sel.getAttribute('data-base-url') || window.location.pathname;
        window.location.href = base + '?offset=0&limit=' + sel.value;
    });
})();

/* Font size toggle — bound via data-font attribute, no inline onclick */
(function () {
    var container = document.querySelector('.font-toggle');
    if (!container) return;
    container.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-font]');
        if (!btn) return;
        var cls = btn.getAttribute('data-font');
        document.documentElement.classList.remove('font-md', 'font-lg');
        if (cls) document.documentElement.classList.add(cls);
        container.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
    });
})();

/* Mobile sidebar toggle */
(function () {
    var hamburger = document.querySelector('.hamburger');
    var overlay = document.querySelector('.sidebar-overlay');
    if (hamburger) {
        hamburger.addEventListener('click', function () {
            document.body.classList.add('sidebar-open');
        });
    }
    if (overlay) {
        overlay.addEventListener('click', function () {
            document.body.classList.remove('sidebar-open');
        });
    }
})();

/* Command palette trigger (Cmd+K / Ctrl+K) */
document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        var searchInput = document.getElementById('global-search');
        if (searchInput) {
            searchInput.focus();
            searchInput.select();
        }
    }
});

/* Color picker: toggle selected class on radio change */
(function () {
    var radios = document.querySelectorAll('.sp-color-radio');
    if (!radios.length) return;
    radios.forEach(function (r) {
        r.addEventListener('change', function () {
            document.querySelectorAll('.sp-color-swatch').forEach(function (s) { s.classList.remove('selected'); });
            if (r.checked && r.nextElementSibling) r.nextElementSibling.classList.add('selected');
        });
    });
})();

/* Avatar upload: auto-submit on file select */
(function () {
    var input = document.querySelector('[data-avatar-upload]');
    if (input) input.addEventListener('change', function () { input.closest('form').submit(); });
})();

/* Alpine.js stores & components — initialised once Alpine is ready */
document.addEventListener('alpine:init', function () {
    /* -------------------------------------------------------
       STORES
       ------------------------------------------------------- */

    /* Notification polling store */
    Alpine.store('notifications', {
        unreadCount: 0,

        async refresh() {
            try {
                var res = await fetch('/api/v1/notifications/unread-count/');
                if (res.ok) {
                    var data = await res.json();
                    this.unreadCount = data.count;
                }
            } catch (_e) {
                /* Silently ignore — notifications are non-critical */
            }
        }
    });

    /* Sidebar collapse store */
    Alpine.store('sidebar', {
        collapsed: false,

        toggle() {
            this.collapsed = !this.collapsed;
        }
    });

    /* -------------------------------------------------------
       COMPONENTS
       ------------------------------------------------------- */

    /**
     * Login form — handles authentication via API.
     *
     * i18n messages are passed via data-msg-* attributes on the root element:
     *   data-msg-invalid   — "Invalid credentials"
     *   data-msg-error     — "Unable to connect. Please try again."
     *   data-msg-loading   — "Signing in..."
     *   data-msg-submit    — "Sign in"
     */
    Alpine.data('loginForm', function () {
        return {
            login: '',
            password: '',
            remember: false,
            error: '',
            loading: false,
            msgInvalid: '',
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.msgInvalid = this.$el.dataset.msgInvalid || 'Invalid credentials';
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Signing in...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Sign in';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.loading = true;
                this.error = '';
                try {
                    var res = await fetch('/api/v1/auth/login/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({login: this.login, password: this.password, remember: this.remember})
                    });
                    if (res.ok) {
                        window.location.href = '/';
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || this.msgInvalid;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    });

    /**
     * Forgot password form — sends reset email via API.
     *
     * i18n messages via data-msg-* attributes:
     *   data-msg-error   — "Unable to connect. Please try again."
     *   data-msg-loading — "Sending..."
     *   data-msg-submit  — "Send reset link"
     */
    Alpine.data('forgotPasswordForm', function () {
        return {
            email: '',
            error: '',
            loading: false,
            sent: false,
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Sending...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Send reset link';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.loading = true;
                this.error = '';
                try {
                    var res = await fetch('/api/v1/auth/forgot-password/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({email: this.email})
                    });
                    if (res.ok || res.status === 202) {
                        this.sent = true;
                    } else if (res.status === 429) {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Too many requests. Please wait.';
                    } else {
                        /* Always show success to prevent enumeration */
                        this.sent = true;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    });

    /**
     * Reset password form — sets new password via API.
     *
     * i18n messages via data-msg-* attributes:
     *   data-token        — the reset token from the URL
     *   data-msg-mismatch — "Passwords do not match"
     *   data-msg-short    — "Password must be at least 8 characters"
     *   data-msg-error    — "Unable to connect. Please try again."
     *   data-msg-loading  — "Resetting..."
     *   data-msg-submit   — "Reset Password"
     */
    Alpine.data('resetPasswordForm', function () {
        return {
            password: '',
            confirm: '',
            error: '',
            loading: false,
            token: '',
            msgMismatch: '',
            msgShort: '',
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.token = this.$el.dataset.token || '';
                this.msgMismatch = this.$el.dataset.msgMismatch || 'Passwords do not match';
                this.msgShort = this.$el.dataset.msgShort || 'Password must be at least 8 characters';
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Resetting...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Reset Password';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.error = '';
                if (this.password.length < 8) {
                    this.error = this.msgShort;
                    return;
                }
                if (this.password !== this.confirm) {
                    this.error = this.msgMismatch;
                    return;
                }
                this.loading = true;
                try {
                    var res = await fetch('/api/v1/auth/reset-password/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({token: this.token, new_password: this.password})
                    });
                    if (res.ok) {
                        window.location.href = '/login/?reset=ok';
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || this.msgError;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    });

    /**
     * API key management — list, create, toggle, delete API keys.
     */
    Alpine.data('apiKeyManager', function () {
        return {
            keys: [],
            showCreate: false,
            newKeyName: '',
            newKey: null,
            loading: false,
            error: '',
            copied: false,

            init() {
                this.loadKeys();
            },

            async loadKeys() {
                var res = await fetch('/api/v1/my/api-keys/');
                if (res.ok) {
                    this.keys = await res.json();
                }
            },

            async createKey() {
                if (!this.newKeyName.trim()) return;
                this.loading = true;
                this.error = '';
                try {
                    var res = await fetch('/api/v1/my/api-keys/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.newKeyName.trim()})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this.newKey = data.raw_key;
                        this.newKeyName = '';
                        await this.loadKeys();
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message) || 'Failed to create key';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect. Please try again.';
                }
                this.loading = false;
            },

            async toggleKey(id, active) {
                var res = await fetch('/api/v1/my/api-keys/' + id + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_active: !active})
                });
                if (res.ok) await this.loadKeys();
            },

            async deleteKey(id) {
                if (!confirm('Are you sure you want to permanently delete this API key?')) return;
                var res = await fetch('/api/v1/my/api-keys/' + id + '/', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) await this.loadKeys();
            },

            copyKey() {
                if (this.newKey) {
                    navigator.clipboard.writeText(this.newKey);
                    this.copied = true;
                    var self = this;
                    setTimeout(function () { self.copied = false; }, 2000);
                }
            },

            dismissNewKey() {
                this.newKey = null;
                this.showCreate = false;
            },

            formatDate(iso) {
                if (!iso) return '-';
                var d = new Date(iso);
                return d.toLocaleDateString('en-US', {year: 'numeric', month: 'short', day: 'numeric'});
            }
        };
    });

    /**
     * Project general settings tab — edit name and description.
     *
     * Expects initial data via argument:
     *   x-data="projectGeneralSettings({ name, description, projectKey })"
     */
    Alpine.data('projectGeneralSettings', function (initial) {
        return {
            name: initial.name || '',
            description: initial.description || '',
            projectKey: initial.projectKey || '',
            saving: false,
            message: '',

            async save() {
                this.saving = true;
                this.message = '';
                try {
                    var res = await fetch('/api/v1/projects/' + this.projectKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.name, description: this.description})
                    });
                    if (res.ok) {
                        this.message = 'Saved successfully.';
                    } else {
                        var data = await res.json();
                        this.message = 'Error: ' + ((data.errors && data.errors[0] && data.errors[0].message) || 'Failed to save');
                    }
                } catch (_e) {
                    this.message = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    });

    /**
     * Project members tab — list and remove members.
     *
     * Expects initial data via argument:
     *   x-data="projectMembers({ members, projectKey })"
     */
    Alpine.data('projectMembers', function (initial) {
        return {
            members: initial.members || [],
            projectKey: initial.projectKey || '',
            roles: initial.roles || [],

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
                var res = await fetch('/api/v1/users/autocomplete/?q=' + encodeURIComponent(this.userQuery));
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
                var res = await fetch('/api/v1/projects/' + this.projectKey + '/members/', {
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
                var res = await fetch('/api/v1/projects/' + this.projectKey + '/members/' + userId + '/', {
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
                // Map current role names to IDs
                var roleMap = {};
                this.roles.forEach(function (r) { roleMap[r.name] = r.id; });
                this.editRoleIds = member.roles.map(function (name) { return roleMap[name]; }).filter(Boolean);
                this.editError = '';
                this.editModal = true;
            },

            async saveRoles() {
                if (!this.editMember || this.editRoleIds.length === 0) return;
                this.editSaving = true;
                this.editError = '';
                var res = await fetch('/api/v1/projects/' + this.projectKey + '/members/' + this.editMember.user_id + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ role_ids: this.editRoleIds })
                });
                if (res.ok) {
                    var updated = await res.json();
                    var m = this.members.find(function (m) { return m.user_id === updated.user_id; });
                    if (m) m.roles = updated.roles;
                    this.editModal = false;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.editError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to update roles.';
                }
                this.editSaving = false;
            }
        };
    });

    /**
     * Project modules tab — toggle project modules on/off.
     *
     * Expects initial data via argument:
     *   x-data="projectModules({ modules, projectKey })"
     */
    Alpine.data('projectModules', function (initial) {
        return {
            modules: initial.modules || {},
            projectKey: initial.projectKey || '',
            saving: false,
            message: '',

            async toggleModule(name) {
                this.saving = true;
                this.message = '';
                var payload = {};
                payload[name] = !this.modules[name];
                try {
                    var res = await fetch('/api/v1/projects/' + this.projectKey + '/modules/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({modules: payload})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this.modules = data.modules;
                        this.message = 'Module updated.';
                    }
                } catch (_e) {
                    this.message = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    });

    /**
     * Admin users — list, create, reset password.
     *
     * Expects initial data via argument:
     *   x-data="adminUsers({ users, roles })"
     */
    Alpine.data('adminUsers', function (initial) {
        return {
            users: initial.users || [],
            roles: initial.roles || [],

            // Create user
            showCreate: false,
            creating: false,
            createError: '',
            newUser: { login: '', email: '', display_name: '', password: '', is_admin: false, is_service_account: false },

            async createUser() {
                this.creating = true;
                this.createError = '';
                var res = await fetch('/api/v1/admin/users/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(this.newUser)
                });
                if (res.ok) {
                    // Reload page to get fresh data
                    window.location.reload();
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.createError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to create user.';
                }
                this.creating = false;
            },

            // Reset password
            showReset: false,
            resetUser: null,
            resetPassword: '',
            resetting: false,
            resetError: '',
            resetSuccess: '',

            openResetPassword(u) {
                this.resetUser = u;
                this.resetPassword = '';
                this.resetError = '';
                this.resetSuccess = '';
                this.showReset = true;
            },

            async doResetPassword() {
                if (!this.resetUser || this.resetPassword.length < 10) {
                    this.resetError = 'Password must be at least 10 characters.';
                    return;
                }
                this.resetting = true;
                this.resetError = '';
                this.resetSuccess = '';
                var res = await fetch('/api/v1/admin/users/' + this.resetUser.id + '/reset-password/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ password: this.resetPassword })
                });
                if (res.ok) {
                    this.resetSuccess = 'Password reset successfully.';
                    this.resetPassword = '';
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.resetError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to reset password.';
                }
                this.resetting = false;
            },

            async toggleLock(u) {
                var action = u.status === 'locked' ? 'unlock' : 'lock';
                if (!confirm(action.charAt(0).toUpperCase() + action.slice(1) + ' user ' + u.login + '?')) return;
                var res = await fetch('/api/v1/admin/users/' + u.id + '/' + action + '/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok) {
                    var updated = await res.json();
                    u.status = updated.status;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    alert((err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to ' + action + ' user.');
                }
            },

            timeAgo(iso) {
                if (!iso) return 'Never';
                var d = new Date(iso);
                var now = new Date();
                var diff = Math.floor((now - d) / 1000);
                if (diff < 60) return 'Just now';
                if (diff < 3600) return Math.floor(diff / 60) + ' min ago';
                if (diff < 86400) return Math.floor(diff / 3600) + ' hours ago';
                if (diff < 172800) return 'Yesterday';
                if (diff < 604800) return Math.floor(diff / 86400) + ' days ago';
                return d.toLocaleDateString();
            }
        };
    });

    /**
     * Admin settings — edit global key/value settings.
     *
     * Expects initial data via argument:
     *   x-data="adminSettings({ key: value, ... })"
     */
    Alpine.data('adminSettings', function (initial) {
        return {
            items: initial || {},
            editingKey: null,
            editValue: '',
            saving: false,
            message: '',
            messageError: false,

            startEdit(key) {
                this.editingKey = key;
                this.editValue = this.items[key] || '';
                this.message = '';
            },

            cancelEdit() {
                this.editingKey = null;
                this.editValue = '';
            },

            async save(key) {
                this.saving = true;
                this.message = '';
                var payload = {};
                payload[key] = this.editValue;
                var res = await fetch('/api/v1/admin/settings/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    var data = await res.json();
                    this.items = data;
                    this.editingKey = null;
                    this.message = 'Setting updated.';
                    this.messageError = false;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.message = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to save.';
                    this.messageError = true;
                }
                this.saving = false;
            }
        };
    });

    /**
     * Issue autocomplete — search issues by key or subject.
     *
     * Usage:
     *   x-data="issueAutocomplete({ value: '' })"
     */
    Alpine.data('issueAutocomplete', function (initial) {
        return {
            query: initial.value || '',
            results: [],
            showResults: false,
            selectedKey: initial.value || '',
            loading: false,
            debounceTimer: null,

            search() {
                clearTimeout(this.debounceTimer);
                this.selectedKey = '';
                if (this.query.length < 1) {
                    this.results = [];
                    this.showResults = false;
                    return;
                }
                var self = this;
                this.debounceTimer = setTimeout(async function () {
                    self.loading = true;
                    try {
                        var res = await fetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(self.query) + '&limit=8');
                        if (res.ok) {
                            self.results = await res.json();
                            self.showResults = self.results.length > 0;
                        }
                    } catch (_e) {}
                    self.loading = false;
                }, 250);
            },

            select(item) {
                this.query = item.key;
                this.selectedKey = item.key;
                this.showResults = false;
                this.$dispatch('issue-selected', {key: item.key, subject: item.subject});
            }
        };
    });

    /**
     * Issue create/edit form.
     *
     * Expects initial data via argument:
     *   x-data="issueForm({ subject, tracker_id, ..., mode, projectKey, displayKey, lockVersion })"
     */
    Alpine.data('issueForm', function (initial) {
        return {
            submitting: false,
            subject: initial.subject || '',
            tracker_id: initial.tracker_id || 0,
            description: initial.description || '',
            status_id: initial.status_id || 0,
            priority_id: initial.priority_id || 0,
            assigned_to_id: initial.assigned_to_id || null,
            start_date: initial.start_date || '',
            due_date: initial.due_date || '',
            est_hours: initial.estimated_hours ? Math.floor(parseFloat(initial.estimated_hours)) : '',
            est_minutes: initial.estimated_hours ? Math.round((parseFloat(initial.estimated_hours) % 1) * 60) : '',
            done_ratio: initial.done_ratio || 0,
            is_private: initial.is_private || false,
            version_id: initial.version_id || '',
            mode: initial.mode || 'create',
            projectKey: initial.projectKey || '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,
            files: [],
            dragover: false,
            pendingRelations: [],
            showRelationForm: false,
            newRelationType: 'relates',
            newRelationKey: '',

            init() {
                var self = this;
                this.$el.addEventListener('relation-issue-selected', function (e) {
                    self.newRelationKey = e.detail.key;
                });
            },

            addFiles(fileList) {
                for (var i = 0; i < fileList.length; i++) {
                    this.files.push(fileList[i]);
                }
            },

            handleDrop(event) {
                this.dragover = false;
                if (event.dataTransfer && event.dataTransfer.files) {
                    this.addFiles(event.dataTransfer.files);
                }
            },

            addPendingRelation() {
                if (!this.newRelationKey) return;
                var typeLabels = {
                    relates: 'Relates to',
                    blocks: 'Blocks',
                    blocked: 'Blocked by',
                    duplicates: 'Duplicates',
                    duplicated: 'Duplicated by',
                    precedes: 'Precedes',
                    follows: 'Follows'
                };
                this.pendingRelations.push({
                    issue_key: this.newRelationKey,
                    relation_type: this.newRelationType,
                    type_label: typeLabels[this.newRelationType] || this.newRelationType
                });
                this.newRelationKey = '';
                this.newRelationType = 'relates';
                this.showRelationForm = false;
            },

            async submitForm(continueCreating) {
                this.submitting = true;
                var payload = {
                    project_key: this.projectKey,
                    tracker_id: this.tracker_id,
                    subject: this.subject,
                    description: this.description || null,
                    status_id: this.status_id || null,
                    priority_id: this.priority_id || null,
                    assigned_to_id: this.assigned_to_id ? parseInt(this.assigned_to_id) : null,
                    start_date: this.start_date || null,
                    due_date: this.due_date || null,
                    estimated_hours: (this.est_hours || this.est_minutes) ? Math.round(((parseInt(this.est_hours) || 0) + (parseInt(this.est_minutes) || 0) / 60) * 100) / 100 : null,
                    done_ratio: this.done_ratio,
                    is_private: this.is_private,
                    version_id: this.version_id ? parseInt(this.version_id) : null
                };

                try {
                    if (this.mode === 'edit') {
                        payload.lock_version = this.lockVersion;
                        var res = await fetch('/api/v1/issues/' + this.displayKey + '/', {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        if (res.ok) {
                            window.location.href = '/projects/' + this.projectKey + '/issues/' + this.displayKey + '/';
                        }
                    } else {
                        var res = await fetch('/api/v1/projects/' + this.projectKey + '/issues/', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        var data = await res.json();
                        if (!data.key) {
                            this.submitting = false;
                            return;
                        }

                        /* Upload attachments */
                        for (var i = 0; i < this.files.length; i++) {
                            var formData = new FormData();
                            formData.append('file', this.files[i]);
                            formData.append('container_type', 'Issue');
                            formData.append('container_id', data.id);
                            try {
                                await fetch('/api/v1/attachments/', {
                                    method: 'POST',
                                    body: formData
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        /* Create pending relations */
                        for (var j = 0; j < this.pendingRelations.length; j++) {
                            var rel = this.pendingRelations[j];
                            try {
                                await fetch('/api/v1/issues/' + data.key + '/relations/', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({
                                        issue_to_key: rel.issue_key,
                                        relation_type: rel.relation_type
                                    })
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        if (continueCreating) {
                            this.subject = '';
                            this.description = '';
                            this.files = [];
                            this.pendingRelations = [];
                            this.newRelationKey = '';
                            this.showRelationForm = false;
                            this.submitting = false;
                            return;
                        }

                        window.location.href = '/projects/' + this.projectKey + '/issues/' + data.key + '/';
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.submitting = false;
            }
        };
    });

    /**
     * Issue comment form.
     *
     * Expects initial data via argument:
     *   x-data="commentForm({ displayKey })"
     */
    Alpine.data('commentForm', function (initial) {
        return {
            notes: '',
            submitting: false,
            displayKey: initial.displayKey || '',

            async submit() {
                this.submitting = true;
                try {
                    var res = await fetch('/api/v1/issues/' + this.displayKey + '/journals/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({notes: this.notes})
                    });
                    if (res.ok) {
                        this.notes = '';
                        window.location.reload();
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.submitting = false;
            }
        };
    });

    Alpine.data('replyForm', function (initial) {
        return {
            showReply: false,
            replyText: '',
            submitting: false,
            displayKey: initial.displayKey || '',
            journalId: initial.journalId || 0,

            async submitReply() {
                if (!this.replyText.trim()) return;
                this.submitting = true;
                try {
                    var res = await fetch('/api/v1/issues/' + this.displayKey + '/journals/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({notes: this.replyText, reply_to_id: this.journalId})
                    });
                    if (res.ok) {
                        this.replyText = '';
                        this.showReply = false;
                        window.location.reload();
                    }
                } catch (_e) {}
                this.submitting = false;
            }
        };
    });

    Alpine.data('descriptionEditor', function (initial) {
        return {
            description: initial.description || '',
            draft: '',
            editing: false,
            saving: false,
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            startEdit() {
                this.draft = this.description;
                this.editing = true;
            },

            cancelEdit() {
                this.editing = false;
            },

            async save() {
                this.saving = true;
                try {
                    var res = await fetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({description: this.draft, lock_version: this.lockVersion})
                    });
                    if (res.ok) {
                        window.location.reload();
                    }
                } catch (_e) {}
                this.saving = false;
            }
        };
    });

    /**
     * Issue sidebar — inline field updates (status, priority, tracker, assignee).
     *
     * Expects initial data via argument:
     *   x-data="issueSidebar({ displayKey, lockVersion })"
     */
    Alpine.data('issueSidebar', function (initial) {
        return {
            updating: false,
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            saveEstimate() {
                var h = parseInt(this.$refs.estH.value) || 0;
                var m = Math.min(parseInt(this.$refs.estM.value) || 0, 59);
                var total = (h || m) ? Math.round((h + m / 60) * 100) / 100 : null;
                this.updateField('estimated_hours', total);
            },

            async updateField(fieldName, value) {
                this.updating = true;
                var payload = {lock_version: this.lockVersion};
                payload[fieldName] = value;
                try {
                    var res = await fetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        window.location.reload();
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.updating = false;
            }
        };
    });

    /**
     * Project creation modal.
     *
     * Auto-generates identifier (slug) and key (uppercase) from project name.
     * On success, redirects to the new project page.
     *
     * Usage:
     *   x-data='projectCreateModal({ colors: [...], allProjects: [...] })'
     */
    Alpine.data('projectCreateModal', function (initial) {
        return {
            name: '',
            identifier: '',
            key: '',
            description: '',
            parentKey: (initial && initial.parentKey) || '',
            color: (initial && initial.colors && initial.colors[0]) || '#c49a3c',
            colors: (initial && initial.colors) || [],
            allProjects: (initial && initial.allProjects) || [],
            moduleWiki: true,
            moduleTime: true,
            saving: false,
            error: '',

            slugify: function (v) {
                return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 50);
            },

            keyify: function (v) {
                return v.toUpperCase().replace(/[^A-Z0-9]/g, '').substring(0, 8);
            },

            onNameInput: function () {
                this.identifier = this.slugify(this.name);
                this.key = this.keyify(this.name);
            },

            async submit() {
                this.saving = true;
                this.error = '';
                var modules = ['issue_tracking'];
                if (this.moduleWiki) modules.push('wiki');
                if (this.moduleTime) modules.push('time_tracking');
                try {
                    var res = await fetch('/api/v1/projects/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            name: this.name,
                            identifier: this.identifier,
                            key: this.key,
                            description: this.description || null,
                            parent_key: this.parentKey || null,
                            color: this.color || null,
                            modules: modules
                        })
                    });
                    if (res.ok) {
                        var data = await res.json();
                        var url = '/projects/' + data.key + '/';
                        for (var i = 0; i < 5; i++) {
                            var check = await fetch(url, {method: 'HEAD'});
                            if (check.ok) break;
                            await new Promise(function (r) { setTimeout(r, 300); });
                        }
                        window.location.href = url;
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message)
                            || (errData.detail && errData.detail[0] && errData.detail[0].msg)
                            || errData.detail
                            || 'Failed to create project';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    });

    /**
     * Admin projects management — list, create, edit, archive/unarchive.
     *
     * Usage:
     *   x-data='adminProjects({ projects: [...], colors: [...], allProjects: [...] })'
     */
    Alpine.data('adminProjects', function (initial) {
        return {
            projects: (initial && initial.projects) || [],
            colors: (initial && initial.colors) || [],
            allProjects: (initial && initial.allProjects) || [],
            showModal: false,
            editMode: false,
            editHasIssues: false,
            saving: false,
            formError: '',
            form: { name: '', identifier: '', key: '', description: '', color: '', parentKey: '', moduleWiki: true, moduleTime: true },

            formatDate: function (iso) {
                if (!iso) return '-';
                var d = new Date(iso);
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                return months[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
            },

            get parentOptions() {
                var self = this;
                return this.allProjects.filter(function (p) {
                    return !self.editMode || p.key !== self.form.key;
                });
            },

            slugify: function (v) {
                return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 50);
            },

            keyify: function (v) {
                return v.toUpperCase().replace(/[^A-Z0-9]/g, '').substring(0, 8);
            },

            onNameInput: function () {
                if (!this.editMode) {
                    this.form.identifier = this.slugify(this.form.name);
                    this.form.key = this.keyify(this.form.name);
                }
            },

            openCreate: function () {
                this.editMode = false;
                this.editHasIssues = false;
                this.formError = '';
                this.form = { name: '', identifier: '', key: '', description: '', color: this.colors[0] || '#c49a3c', parentKey: '', moduleWiki: true, moduleTime: true };
                this.showModal = true;
            },

            openEdit: function (p) {
                this.editMode = true;
                this.editHasIssues = p.has_issues;
                this.formError = '';
                this.form = { name: p.name, identifier: p.identifier, key: p.key, description: p.description || '', color: p.color || '#c49a3c', parentKey: '', moduleWiki: true, moduleTime: true };
                this.showModal = true;
            },

            closeModal: function () {
                this.showModal = false;
            },

            async submitForm() {
                this.saving = true;
                this.formError = '';
                try {
                    if (this.editMode) {
                        var res = await fetch('/api/v1/projects/' + this.form.key + '/', {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                name: this.form.name,
                                description: this.form.description || null,
                                color: this.form.color || null
                            })
                        });
                    } else {
                        var modules = ['issue_tracking'];
                        if (this.form.moduleWiki) modules.push('wiki');
                        if (this.form.moduleTime) modules.push('time_tracking');
                        var res = await fetch('/api/v1/projects/', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                name: this.form.name,
                                identifier: this.form.identifier,
                                key: this.form.key,
                                description: this.form.description || null,
                                parent_key: this.form.parentKey || null,
                                color: this.form.color || null,
                                modules: modules
                            })
                        });
                    }
                    if (res.ok) {
                        location.reload();
                    } else {
                        var data = await res.json();
                        this.formError = (data.errors && data.errors[0] && data.errors[0].message)
                            || (data.detail && data.detail[0] && data.detail[0].msg)
                            || data.detail
                            || 'Failed to save';
                    }
                } catch (_e) {
                    this.formError = 'Unable to connect.';
                }
                this.saving = false;
            },

            async archiveProject(p) {
                if (!confirm('Archive ' + p.name + '?')) return;
                await fetch('/api/v1/admin/projects/' + p.key + '/archive/', { method: 'POST' });
                location.reload();
            },

            async unarchiveProject(p) {
                await fetch('/api/v1/admin/projects/' + p.key + '/unarchive/', { method: 'POST' });
                location.reload();
            }
        };
    });

    /**
     * Wiki page create/edit form.
     *
     * Submits via fetch() with JSON body instead of native form post
     * (the API expects application/json, not form-encoded data).
     *
     * Expects initial data via argument:
     *   x-data="wikiForm({ mode, projectKey, slug, lockVersion })"
     */
    Alpine.data('wikiForm', function (initial) {
        return {
            title: initial.title || '',
            text: initial.text || '',
            comments: '',
            parentSlug: initial.parentSlug || '',
            parentTitle: initial.parentTitle || '',
            submitting: false,
            error: '',
            mode: initial.mode || 'create',
            projectKey: initial.projectKey || '',
            slug: initial.slug || '',
            lockVersion: initial.lockVersion || 0,
            preview: false,

            async submitForm() {
                this.submitting = true;
                this.error = '';
                try {
                    var url, method, payload;
                    if (this.mode === 'edit') {
                        url = '/api/v1/projects/' + this.projectKey + '/wiki/' + this.slug + '/';
                        method = 'PATCH';
                        payload = {
                            title: this.title,
                            text: this.text,
                            lock_version: this.lockVersion,
                            comments: this.comments || null,
                            parent_slug: this.parentSlug || null
                        };
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/wiki/';
                        method = 'POST';
                        payload = {
                            title: this.title,
                            text: this.text,
                            parent_slug: this.parentSlug || null,
                            comments: this.comments || null
                        };
                    }
                    var res = await fetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        var data = await res.json();
                        var slug = data.slug || this.slug;
                        window.location.href = '/projects/' + this.projectKey + '/wiki/' + slug + '/';
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message) || 'Failed to save';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect. Please try again.';
                }
                this.submitting = false;
            }
        };
    });

    Alpine.data('resolveThread', function (initial) {
        return {
            showModal: false,
            summary: '',
            issueKey: initial.issueKey || '',
            journalId: initial.journalId || 0,

            async resolve() {
                var res = await fetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/resolve/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({summary: this.summary})
                });
                if (res.ok) {
                    this.showModal = false;
                    window.location.reload();
                }
            }
        };
    });

    Alpine.data('watcherToggle', function (initial) {
        return {
            watching: initial.watching || false,
            issueKey: initial.issueKey || '',
            async toggle() {
                var method = this.watching ? 'DELETE' : 'POST';
                var res = await fetch('/api/v1/issues/' + this.issueKey + '/watchers/', {
                    method: method,
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) {
                    this.watching = !this.watching;
                    window.location.reload();
                }
            }
        };
    });

    /**
     * Inline time log form — log time directly from the issue time tab.
     *
     * Expects initial data via argument:
     *   x-data="timeLogForm({ projectKey, issueId })"
     */
    Alpine.data('timeLogForm', function (initial) {
        return {
            showForm: false,
            hours: '',
            minutes: '',
            activityId: '',
            spentOn: new Date().toISOString().split('T')[0],
            comments: '',
            submitting: false,
            error: '',
            projectKey: initial.projectKey || '',
            issueId: initial.issueId || 0,

            get totalHours() {
                var h = parseInt(this.hours) || 0;
                var m = parseInt(this.minutes) || 0;
                return h + m / 60;
            },

            async submit() {
                this.submitting = true;
                this.error = '';
                try {
                    var total = this.totalHours;
                    if (total <= 0) {
                        this.error = 'Enter at least 1 minute';
                        this.submitting = false;
                        return;
                    }
                    var res = await fetch('/api/v1/projects/' + this.projectKey + '/time-entries/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            issue_id: this.issueId,
                            activity_id: parseInt(this.activityId),
                            hours: Math.round(total * 100) / 100,
                            spent_on: this.spentOn,
                            comments: this.comments || null
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Failed to log time';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    });

    /**
     * Inline add relation form.
     *
     * Expects initial data via argument:
     *   x-data="relationForm({ issueKey })"
     */
    Alpine.data('relationForm', function (initial) {
        return {
            showForm: false,
            issueToKey: '',
            relationType: 'relates',
            submitting: false,
            error: '',
            issueKey: initial.issueKey || '',

            async submit() {
                this.submitting = true;
                this.error = '';
                try {
                    var res = await fetch('/api/v1/issues/' + this.issueKey + '/relations/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            issue_to_key: this.issueToKey,
                            relation_type: this.relationType
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Failed to add relation';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    });

    /**
     * Inline attachment upload form.
     *
     * Expects initial data via argument:
     *   x-data="attachmentForm({ issueId })"
     */
    Alpine.data('attachmentForm', function (initial) {
        return {
            showForm: false,
            file: null,
            description: '',
            submitting: false,
            error: '',
            issueId: initial.issueId || 0,

            selectFile(event) {
                this.file = event.target.files[0] || null;
            },

            async submit() {
                if (!this.file) return;
                this.submitting = true;
                this.error = '';
                try {
                    var formData = new FormData();
                    formData.append('file', this.file);
                    formData.append('container_type', 'Issue');
                    formData.append('container_id', this.issueId);
                    if (this.description) {
                        formData.append('description', this.description);
                    }
                    var res = await fetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Upload failed';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    });

    Alpine.data('journalReactions', function (initial) {
        return {
            reactions: initial.reactions || [],
            showPicker: false,
            issueKey: initial.issueKey || '',
            journalId: initial.journalId || 0,
            emojiMap: {thumbs_up: '👍', thumbs_down: '👎', heart: '❤️', rocket: '🚀', eyes: '👀', tada: '🎉'},

            emojiChar(key) {
                return this.emojiMap[key] || key;
            },

            async toggle(emoji) {
                var res = await fetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/reactions/' + emoji + '/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok) {
                    var data = await res.json();
                    this.updateLocal(emoji, data.added);
                }
            },

            updateLocal(emoji, added) {
                var found = false;
                for (var i = 0; i < this.reactions.length; i++) {
                    if (this.reactions[i].emoji === emoji) {
                        found = true;
                        if (added) {
                            this.reactions[i].count++;
                            this.reactions[i].reacted_by_me = true;
                        } else {
                            this.reactions[i].count--;
                            this.reactions[i].reacted_by_me = false;
                            if (this.reactions[i].count <= 0) {
                                this.reactions.splice(i, 1);
                            }
                        }
                        break;
                    }
                }
                if (!found && added) {
                    this.reactions.push({emoji: emoji, count: 1, reacted_by_me: true});
                }
            }
        };
    });

    Alpine.data('wikiToc', function () {
        return {
            headings: [],
            activeId: '',

            init() {
                var content = document.querySelector('.wiki-content');
                if (!content) return;
                var headers = content.querySelectorAll('h2, h3, h4');
                var items = [];
                for (var i = 0; i < headers.length; i++) {
                    var h = headers[i];
                    if (!h.id) {
                        h.id = 'heading-' + i;
                    }
                    items.push({
                        id: h.id,
                        text: h.textContent.trim(),
                        level: parseInt(h.tagName.charAt(1))
                    });
                }
                this.headings = items;

                if (items.length === 0) return;

                var self = this;
                var observer = new IntersectionObserver(function (entries) {
                    for (var j = 0; j < entries.length; j++) {
                        if (entries[j].isIntersecting) {
                            self.activeId = entries[j].target.id;
                        }
                    }
                }, { rootMargin: '-80px 0px -60% 0px', threshold: 0 });

                for (var k = 0; k < headers.length; k++) {
                    observer.observe(headers[k]);
                }
            }
        };
    });
});
