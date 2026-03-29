/* ============================================================
   SPECIVO — Global JavaScript (Alpine.js stores + utilities)
   ============================================================ */

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
                var res = await fetch('/api/v1/notifications/unread-count');
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
                    var res = await fetch('/api/v1/auth/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({login: this.login, password: this.password})
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
                var res = await fetch('/api/v1/my/api-keys');
                if (res.ok) {
                    this.keys = await res.json();
                }
            },

            async createKey() {
                if (!this.newKeyName.trim()) return;
                this.loading = true;
                this.error = '';
                try {
                    var res = await fetch('/api/v1/my/api-keys', {
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
                var res = await fetch('/api/v1/my/api-keys/' + id, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_active: !active})
                });
                if (res.ok) await this.loadKeys();
            },

            async deleteKey(id) {
                if (!confirm('Are you sure you want to permanently delete this API key?')) return;
                var res = await fetch('/api/v1/my/api-keys/' + id, {
                    method: 'DELETE'
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
                    var res = await fetch('/api/v1/projects/' + this.projectKey, {
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

            async removeMember(userId) {
                if (!confirm('Remove this member?')) return;
                var res = await fetch('/api/v1/projects/' + this.projectKey + '/members/' + userId, {
                    method: 'DELETE'
                });
                if (res.ok || res.status === 204) {
                    this.members = this.members.filter(function (m) { return m.user_id !== userId; });
                }
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
                    var res = await fetch('/api/v1/projects/' + this.projectKey + '/modules', {
                        method: 'PUT',
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
            estimated_hours: initial.estimated_hours || '',
            done_ratio: initial.done_ratio || 0,
            is_private: initial.is_private || false,
            mode: initial.mode || 'create',
            projectKey: initial.projectKey || '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            async submitForm() {
                this.submitting = true;
                var payload = {
                    project_key: this.projectKey,
                    tracker_id: this.tracker_id,
                    subject: this.subject,
                    description: this.description || null,
                    priority_id: this.priority_id || null,
                    assigned_to_id: this.assigned_to_id ? parseInt(this.assigned_to_id) : null,
                    start_date: this.start_date || null,
                    due_date: this.due_date || null,
                    estimated_hours: this.estimated_hours ? parseFloat(this.estimated_hours) : null,
                    done_ratio: this.done_ratio,
                    is_private: this.is_private
                };

                try {
                    if (this.mode === 'edit') {
                        payload.lock_version = this.lockVersion;
                        payload.status_id = this.status_id || null;
                        var res = await fetch('/api/v1/issues/' + this.displayKey, {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        if (res.ok) {
                            window.location.href = '/projects/' + this.projectKey + '/issues/' + this.displayKey;
                        }
                    } else {
                        var res = await fetch('/api/v1/issues', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        var data = await res.json();
                        if (data.key) {
                            window.location.href = '/projects/' + this.projectKey + '/issues/' + data.key;
                        } else {
                            window.location.href = '/projects/' + this.projectKey + '/issues';
                        }
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
                    var res = await fetch('/api/v1/issues/' + this.displayKey + '/journals', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({notes: this.notes})
                    });
                    if (res.ok) {
                        this.notes = '';
                        htmx.trigger('#issue-activity', 'htmx:trigger');
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.submitting = false;
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

            async updateField(fieldName, value) {
                this.updating = true;
                var payload = {lock_version: this.lockVersion};
                payload[fieldName] = value;
                try {
                    await fetch('/api/v1/issues/' + this.displayKey, {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                } catch (_e) {
                    /* request failed */
                }
                this.updating = false;
            }
        };
    });
});
