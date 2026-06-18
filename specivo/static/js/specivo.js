/* ============================================================
   SPECIVO — Global JavaScript (Alpine.js stores + utilities)
   ============================================================ */

/* -------------------------------------------------------
   CSRF — read token from cookie, attach to mutating requests
   ------------------------------------------------------- */
function _getCsrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return match ? match[1] : '';
}

/**
 * Drop-in fetch() wrapper that auto-attaches the X-CSRF-Token header
 * on mutating requests (POST/PATCH/PUT/DELETE).
 */
function spFetch(url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
        opts.headers = opts.headers || {};
        if (opts.headers instanceof Headers) {
            opts.headers.set('X-CSRF-Token', _getCsrfToken());
        } else {
            opts.headers['X-CSRF-Token'] = _getCsrfToken();
        }
    }
    return fetch(url, opts);
}

/* HTMX — inject CSRF header on every mutating request */
document.addEventListener('htmx:configRequest', function (e) {
    var method = (e.detail.verb || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
        e.detail.headers['X-CSRF-Token'] = _getCsrfToken();
    }
});

/* HTMX — CSP-safe after-request handlers via data attributes.
   Instead of inline hx-on::after-request="...", elements use:
     data-hx-reload    — reload page on success
     data-hx-redirect  — redirect to URL on success */
document.addEventListener('htmx:afterRequest', function (e) {
    if (!e.detail.successful) return;
    var el = e.detail.elt;
    if (el.hasAttribute('data-hx-reload')) {
        window.location.reload();
    } else if (el.hasAttribute('data-hx-redirect')) {
        window.location.href = el.getAttribute('data-hx-redirect');
    }
});

/* HTML forms — inject csrf_token hidden field before submission */
document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    var method = (form.method || 'GET').toUpperCase();
    if (method === 'GET' || method === 'HEAD') return;
    if (!form.querySelector('input[name="csrf_token"]')) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = _getCsrfToken();
        form.appendChild(input);
    }
});

/* Service Worker registration (PWA) — skip if URL contains credentials (Basic Auth proxy) */
if ('serviceWorker' in navigator && !location.href.match(/:\/\/[^@]+@/)) {
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

/* Dashboard My Issues page-size select — uses htmx to swap the container */
(function () {
    document.addEventListener('change', function (e) {
        var sel = e.target.closest('[data-my-issues-limit]');
        if (!sel) return;
        var limit = sel.value;
        var container = document.getElementById('my-issues-container');
        if (!container || typeof htmx === 'undefined') return;
        htmx.ajax('GET', '/partials/dashboard/my-issues/?offset=0&limit=' + limit, {target: container, swap: 'innerHTML'});
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

/* Activity per-page selector — saves preference then reloads */
(function () {
    var sel = document.querySelector('[data-activity-per-page]');
    if (!sel) return;
    sel.addEventListener('change', function () {
        spFetch('/api/v1/users/me/preferences/activity-per-page/?per_page=' + sel.value, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'}
        }).then(function () { window.location.search = ''; });
    });
})();

/* Board per-column selector — updates URL query param */
(function () {
    document.querySelectorAll('[data-board-per-col]').forEach(function (sel) {
        sel.addEventListener('change', function () {
            var href = window.location.href.replace(/board_per_col=\d+/, '').replace(/[?&]$/, '');
            window.location.href = href + (window.location.search ? '&' : '?') + 'board_per_col=' + sel.value;
        });
    });
})();

/* Search page size selector */
(function () {
    var sel = document.querySelector('[data-search-limit]');
    if (!sel) return;
    sel.addEventListener('change', function () {
        var base = sel.getAttribute('data-search-limit');
        window.location = base + '&offset=0&limit=' + sel.value;
    });
})();

/* Confirm dialog via data-confirm attribute (CSP-safe, replaces inline onclick="return confirm(...)") */
document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-confirm]');
    if (!btn) return;
    var msg = btn.getAttribute('data-confirm');
    if (!confirm(msg)) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
});

/* Generic modal open/close via data attributes (CSP-safe) */
(function () {
    document.addEventListener('click', function (e) {
        var opener = e.target.closest('[data-open-modal]');
        if (opener) {
            var id = opener.getAttribute('data-open-modal');
            var el = document.getElementById(id);
            if (el) el.style.display = 'flex';
            return;
        }
        var closer = e.target.closest('[data-close-modal]');
        if (closer) {
            var cid = closer.getAttribute('data-close-modal');
            var cel = document.getElementById(cid);
            if (cel) cel.style.display = 'none';
            return;
        }
        /* Backdrop click to close */
        if (e.target.hasAttribute && e.target.hasAttribute('data-modal-backdrop')) {
            e.target.style.display = 'none';
        }
    });
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
                var res = await spFetch('/api/v1/notifications/unread-count/');
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

            get errorClass() {
                return this.error ? 'show' : '';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.loading = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/auth/login/', {
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
                    var res = await spFetch('/api/v1/auth/forgot-password/', {
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
                    var res = await spFetch('/api/v1/auth/reset-password/', {
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
                var res = await spFetch('/api/v1/my/api-keys/');
                if (res.ok) {
                    this.keys = await res.json();
                }
            },

            async createKey() {
                if (!this.newKeyName.trim()) return;
                this.loading = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/my/api-keys/', {
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
                var res = await spFetch('/api/v1/my/api-keys/' + id + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_active: !active})
                });
                if (res.ok) await this.loadKeys();
            },

            async deleteKey(id) {
                if (!confirm('Are you sure you want to permanently delete this API key?')) return;
                var res = await spFetch('/api/v1/my/api-keys/' + id + '/', {
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
     * Project general settings tab — edit name, description, and parent project.
     *
     * Expects initial data via argument:
     *   x-data="projectGeneralSettings({ name, description, projectKey, parentId, availableParents })"
     */
    Alpine.data('projectGeneralSettings', function (initial) {
        return {
            name: initial.name || '',
            description: initial.description || '',
            projectKey: initial.projectKey || '',
            parentId: initial.parentId !== undefined ? initial.parentId : null,
            availableParents: initial.availableParents || [],
            saving: false,
            message: '',

            async save() {
                this.saving = true;
                this.message = '';
                try {
                    var payload = {
                        name: this.name,
                        description: this.description,
                        parent_id: this.parentId
                    };
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
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
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/modules/', {
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
     * Project versions tab — list, create, edit, delete versions.
     *
     * Expects initial data via argument:
     *   x-data="projectVersions({ versions, projectKey })"
     */
    Alpine.data('projectVersions', function (initial) {
        return {
            versions: initial.versions || [],
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            showModal: false,
            showDeleteModal: false,
            editingVersion: null,
            deletingVersion: null,
            saving: false,
            deleting: false,
            message: '',
            messageType: 'success',
            form: {name: '', description: '', status: 'open', due_date: ''},

            get canSaveVersion() {
                return !this.saving && this.form.name.trim() !== '';
            },

            openCreate: function () {
                if (!this.canManage) return;
                this.editingVersion = null;
                this.form = {name: '', description: '', status: 'open', due_date: ''};
                this.showModal = true;
            },

            openEdit: function (v) {
                if (!this.canManage) return;
                this.editingVersion = v;
                this.form = {
                    name: v.name,
                    description: v.description || '',
                    status: v.status,
                    due_date: v.due_date || ''
                };
                this.showModal = true;
            },

            saveVersion: async function () {
                if (!this.form.name.trim()) return;
                this.saving = true;
                try {
                    var payload = {
                        name: this.form.name.trim(),
                        description: this.form.description || null,
                        status: this.form.status,
                        effective_date: this.form.due_date || null
                    };
                    var url, method;
                    if (this.editingVersion) {
                        url = '/api/v1/projects/' + this.projectKey + '/versions/' + this.editingVersion.id + '/';
                        method = 'PATCH';
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/versions/';
                        method = 'POST';
                    }
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        // Reload versions with roadmap data to get fresh counts
                        await this._reloadVersions();
                        var action = this.editingVersion ? 'updated' : 'created';
                        this.flash('Version "' + this.form.name + '" ' + action + '.', 'success');
                        this.showModal = false;
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.flash((err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to save version.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.saving = false;
            },

            confirmDelete: function (v) {
                if (!this.canManage) return;
                this.deletingVersion = v;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deletingVersion) return;
                this.deleting = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/versions/' + this.deletingVersion.id + '/', {
                        method: 'DELETE',
                        headers: {'Content-Type': 'application/json'}
                    });
                    if (res.ok || res.status === 204) {
                        var name = this.deletingVersion.name;
                        this.versions = this.versions.filter(function (v) { return v.id !== this.deletingVersion.id; }.bind(this));
                        this.showDeleteModal = false;
                        this.deletingVersion = null;
                        this.flash('Version "' + name + '" deleted.', 'success');
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.flash((err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to delete version.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.deleting = false;
            },

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type;
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            _reloadVersions: async function () {
                try {
                    var roadmapRes = await spFetch('/api/v1/projects/' + this.projectKey + '/roadmap/');
                    if (roadmapRes.ok) {
                        var entries = await roadmapRes.json();
                        var today = new Date().toISOString().slice(0, 10);
                        this.versions = entries.map(function (e) {
                            var dueDate = e.version.effective_date || '';
                            return {
                                id: e.version.id,
                                name: e.version.name,
                                description: e.version.description,
                                status: e.version.status,
                                due_date: dueDate,
                                progress: e.progress_percent,
                                open_count: e.open_count,
                                closed_count: e.closed_count,
                                overdue: dueDate !== '' && dueDate < today && e.version.status !== 'closed'
                            };
                        });
                    }
                } catch (_e) {
                    // Silently fail — page already has stale data
                }
            }
        };
    });

    /* ----------------------------------------------------------------
       Recurring tasks — shared form helpers + list/detail components.
       The RRULE builder lives in `_recurring_form_modal.html`; these
       mixins back its reactive state, payload assembly, and live preview.
       ---------------------------------------------------------------- */

    /** Build an empty form object for a new pattern. */
    function _blankRecurringForm() {
        return {
            name: '',
            enabled: true,
            freq: 'weekly',
            rrule_interval: 1,
            byday: [],
            bymonthday: '',
            bysetpos: '',
            anchor_mode: 'fixed',
            base_date_strategy: 'scheduled',
            timezone: 'UTC',
            creation_lead_time_days: 30,
            dtstart: '',
            template_tracker_id: null,
            template_status_id: null,
            template_priority_id: null,
            template_assigned_to_id: null,
            template_subject: '',
            template_description: '',
            carry_over: {description: true, assignee: true, metadata: true, estimated_hours: true},
            reset_checklist: true,
            rotation_user_ids: [],
            lock_version: 0
        };
    }

    /** Map a server pattern summary onto an editable form object. */
    function _patternToForm(p) {
        var f = _blankRecurringForm();
        f.name = p.name || '';
        f.enabled = p.enabled !== false;
        f.freq = p.freq || 'weekly';
        f.rrule_interval = p.rrule_interval || 1;
        f.byday = (p.byday || []).slice();
        f.bymonthday = (p.bymonthday || []).join(', ');
        f.bysetpos = (p.bysetpos || []).join(', ');
        f.anchor_mode = p.anchor_mode || 'fixed';
        f.base_date_strategy = p.base_date_strategy || 'scheduled';
        f.timezone = p.timezone || 'UTC';
        f.creation_lead_time_days = p.creation_lead_time_days || 30;
        f.dtstart = p.dtstart ? p.dtstart.slice(0, 16) : '';
        f.template_tracker_id = p.template_tracker_id || null;
        f.template_status_id = p.template_status_id || null;
        f.template_priority_id = p.template_priority_id || null;
        f.template_assigned_to_id = p.template_assigned_to_id || null;
        f.template_subject = p.template_subject || '';
        f.template_description = p.template_description || '';
        f.carry_over = Object.assign(f.carry_over, p.carry_over || {});
        f.reset_checklist = p.reset_checklist !== false;
        f.rotation_user_ids = (p.assignee_rotation && p.assignee_rotation.user_ids) ? p.assignee_rotation.user_ids.slice() : [];
        f.lock_version = (typeof p.lock_version === 'number') ? p.lock_version : 0;
        return f;
    }

    /** Parse a comma-separated int list (drops blanks / non-numbers). */
    function _parseIntList(raw) {
        if (!raw) return null;
        var parts = String(raw).split(',');
        var out = [];
        for (var i = 0; i < parts.length; i++) {
            var n = parseInt(parts[i].trim(), 10);
            if (!isNaN(n)) out.push(n);
        }
        return out.length > 0 ? out : null;
    }

    /**
     * Assemble the REST payload from a form.
     *
     * When ``includeLock`` is true (the PATCH/edit path) the optimistic-locking
     * ``lock_version`` is included so the server can reject a stale update with
     * a 409 Conflict; the create (POST) path omits it.
     */
    function _formToPayload(form, includeLock) {
        var dtstart = form.dtstart;
        if (dtstart && dtstart.length === 16) {
            dtstart = dtstart + ':00';
        }
        var rotation = null;
        if (form.rotation_user_ids && form.rotation_user_ids.length > 0) {
            rotation = {user_ids: form.rotation_user_ids, strategy: 'round_robin'};
        }
        var payload = {
            name: form.name.trim(),
            enabled: form.enabled,
            freq: form.freq,
            rrule_interval: form.rrule_interval || 1,
            // byday applies to weekly schedules and to monthly/yearly
            // nth-weekday rules (combined with bysetpos, e.g. "2nd Tuesday").
            byday: (form.freq !== 'daily' && form.byday.length > 0) ? form.byday : null,
            bymonthday: form.freq === 'monthly' ? _parseIntList(form.bymonthday) : null,
            bysetpos: (form.freq === 'monthly' || form.freq === 'yearly') ? _parseIntList(form.bysetpos) : null,
            anchor_mode: form.anchor_mode,
            base_date_strategy: form.base_date_strategy,
            timezone: form.timezone || 'UTC',
            creation_lead_time_days: form.creation_lead_time_days || 30,
            dtstart: dtstart,
            template_tracker_id: form.template_tracker_id,
            template_status_id: form.template_status_id || null,
            template_priority_id: form.template_priority_id || null,
            template_assigned_to_id: form.template_assigned_to_id || null,
            template_subject: form.template_subject.trim(),
            template_description: form.template_description || null,
            carry_over: form.carry_over,
            reset_checklist: form.reset_checklist,
            assignee_rotation: rotation
        };
        if (includeLock) {
            payload.lock_version = form.lock_version;
        }
        return payload;
    }

    /** Human-readable schedule label for the list table ("Weekly", "Every 2 months"). */
    function _scheduleLabel(p) {
        var n = p.rrule_interval || 1;
        var plural = {daily: 'days', weekly: 'weeks', monthly: 'months', yearly: 'years'};
        var single = {daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', yearly: 'Yearly'};
        if (n > 1) return 'Every ' + n + ' ' + (plural[p.freq] || p.freq);
        return single[p.freq] || p.freq;
    }

    /** Format an ISO occurrence string for display ("YYYY-MM-DD HH:MM"). */
    function _fmtOccurrence(iso) {
        if (!iso) return '';
        return iso.replace('T', ' ').slice(0, 16);
    }

    /**
     * Recurring pattern create/edit — dedicated full-page form (RRULE builder).
     *
     * Replaces the old modal. Drives create (POST) and edit (PATCH) of a
     * pattern through the REST API, then navigates to the pattern's detail page
     * on success. Translated, user-facing strings are passed in via ``labels``
     * from the template (CSP-safe; no hardcoded English in JS).
     *
     *   x-data="recurringPatternForm({ mode, pattern, projectKey, canManage,
     *     members, trackers, statuses, priorities, labels })"
     *
     * Race safety:
     *   - ``saving`` double-submit guard: the submit button is :disabled while a
     *     request is in flight and the handler returns early if already saving,
     *     so a duplicate POST can never create a duplicate pattern.
     *   - On edit, ``form.lock_version`` rides along in the PATCH body; a stale
     *     version is rejected server-side with 409 and surfaced here as a clear
     *     "changed by someone else" message.
     */
    Alpine.data('recurringPatternForm', function (initial) {
        var labels = initial.labels || {};
        var isEdit = initial.mode === 'edit';
        var form;
        if (isEdit && initial.pattern) {
            form = _patternToForm(initial.pattern);
        } else {
            form = _blankRecurringForm();
            if (initial.trackers && initial.trackers.length > 0) {
                form.template_tracker_id = initial.trackers[0].id;
            }
            // New patterns default to the timezone resolved from the user's
            // profile / instance settings (server-provided), fallback UTC.
            form.timezone = initial.defaultTimezone || 'UTC';
        }
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            members: initial.members || [],
            trackers: initial.trackers || [],
            statuses: initial.statuses || [],
            priorities: initial.priorities || [],
            timezones: initial.timezones || [],
            labels: labels,
            editing: isEdit,
            patternId: (isEdit && initial.pattern) ? initial.pattern.id : null,
            saving: false,
            form: form,
            formError: '',
            previewForm: [],
            previewLoading: false,
            previewError: false,

            // Timezone combobox state.
            tzOpen: false,
            tzQuery: '',
            tzActiveIndex: 0,

            init: function () {
                if (this.editing) this.refreshFormPreview();
            },

            /**
             * Case-insensitive filter over the IANA timezone list. Matches on the
             * zone name (e.g. "Europe/London"); the full name already carries the
             * region/city offset hint, so a plain substring match is enough.
             */
            /** Display label for a timezone: IANA id with underscores shown as
             *  spaces (e.g. "America/El_Aaiun" -> "America/El Aaiun"). The stored
             *  value (form.timezone) always keeps the canonical underscore form. */
            tzLabel: function (tz) {
                return (tz || '').replace(/_/g, ' ');
            },

            tzFiltered: function () {
                // Match underscore-insensitively so typing "El Aaiun" finds
                // "America/El_Aaiun".
                var q = this.tzQuery.trim().toLowerCase().replace(/_/g, ' ');
                if (!q) return this.timezones;
                var out = [];
                for (var i = 0; i < this.timezones.length; i++) {
                    var hay = this.timezones[i].toLowerCase().replace(/_/g, ' ');
                    if (hay.indexOf(q) !== -1) {
                        out.push(this.timezones[i]);
                    }
                }
                return out;
            },

            tzToggle: function () {
                if (this.tzOpen) {
                    this.tzClose();
                } else {
                    this.tzOpenPanel();
                }
            },

            tzOpenPanel: function () {
                this.tzOpen = true;
                this.tzQuery = '';
                // Position the active highlight on the current selection.
                var list = this.tzFiltered();
                var sel = list.indexOf(this.form.timezone);
                this.tzActiveIndex = sel === -1 ? 0 : sel;
                var self = this;
                this.$nextTick(function () {
                    if (self.$refs.tzSearch) self.$refs.tzSearch.focus();
                });
            },

            tzClose: function () {
                if (!this.tzOpen) return;
                this.tzOpen = false;
                // Return focus to the trigger for keyboard users.
                var trigger = this.$el.querySelector('.sp-tz-trigger');
                if (trigger) trigger.focus();
            },

            tzSelect: function (tz) {
                this.form.timezone = tz;
                this.refreshFormPreview();
                this.tzClose();
            },

            /** Keyboard on the trigger button: open on arrow/enter/space. */
            tzTriggerKeydown: function (e) {
                if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.tzOpenPanel();
                }
            },

            /** Keyboard inside the search input: navigate and choose options. */
            tzSearchKeydown: function (e) {
                var list = this.tzFiltered();
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    if (list.length) this.tzActiveIndex = (this.tzActiveIndex + 1) % list.length;
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    if (list.length) this.tzActiveIndex = (this.tzActiveIndex - 1 + list.length) % list.length;
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (list.length) this.tzSelect(list[this.tzActiveIndex]);
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    this.tzClose();
                }
                this.$nextTick(this._tzScrollActive.bind(this));
            },

            /** Keep the active option in view while arrow-navigating. */
            _tzScrollActive: function () {
                var opt = this.$el.querySelector('#sp-tz-opt-' + this.tzActiveIndex);
                if (opt && opt.scrollIntoView) opt.scrollIntoView({ block: 'nearest' });
            },

            get canSaveForm() {
                return !this.saving
                    && this.form.name.trim() !== ''
                    && this.form.template_subject.trim() !== ''
                    && !!this.form.template_tracker_id
                    && !!this.form.dtstart;
            },

            /** Full weekday name for the weekday-button aria-label. */
            weekdayName: function (code) {
                var names = this.labels.weekdays || {};
                return names[code] || code;
            },

            toggleByday: function (day) {
                var idx = this.form.byday.indexOf(day);
                if (idx === -1) {
                    this.form.byday.push(day);
                } else {
                    this.form.byday.splice(idx, 1);
                }
                this.refreshFormPreview();
            },

            /**
             * Live preview: the occurrences endpoint requires a persisted
             * pattern, so we query it only when editing. For a brand-new pattern
             * the live count appears after the first save.
             */
            refreshFormPreview: async function () {
                if (!this.editing || !this.patternId) {
                    this.previewForm = [];
                    this.previewError = false;
                    return;
                }
                this.previewLoading = true;
                this.previewError = false;
                try {
                    var days = this.form.creation_lead_time_days || 30;
                    var url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.patternId + '/occurrences/?days=' + days;
                    var res = await spFetch(url);
                    if (res.ok) {
                        var data = await res.json();
                        this.previewForm = (data.occurrences || []).slice(0, 5).map(_fmtOccurrence);
                    } else {
                        this.previewError = true;
                    }
                } catch (_e) {
                    this.previewError = true;
                }
                this.previewLoading = false;
            },

            saveForm: async function () {
                // Double-submit guard: bail if a request is already in flight.
                if (this.saving || !this.canSaveForm) return;
                this.saving = true;
                this.formError = '';
                try {
                    var url, method;
                    if (this.editing) {
                        url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.patternId + '/';
                        method = 'PATCH';
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/';
                        method = 'POST';
                    }
                    var payload = _formToPayload(this.form, this.editing);
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        // Navigate to the saved pattern's detail page. Stay in the
                        // saving state so the button cannot be re-submitted while
                        // the browser navigates away.
                        var saved = await res.json();
                        var id = saved.id || this.patternId;
                        window.location = '/projects/' + this.projectKey + '/recurring-patterns/' + id + '/';
                        return;
                    }
                    if (res.status === 409) {
                        this.formError = this.labels.conflict || 'This pattern was changed by someone else. Reload and try again.';
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.formError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || this.labels.saveFailed || 'Failed to save pattern.';
                    }
                } catch (_e) {
                    this.formError = this.labels.connectFailed || 'Unable to connect.';
                }
                // Re-enable only on error (on success we navigate away).
                this.saving = false;
            },

            scheduleLabel: function (p) { return _scheduleLabel(p); }
        };
    });

    /**
     * Recurring tasks — management list (enable-toggle / delete).
     * Create/edit now happen on dedicated form pages (see recurringPatternForm),
     * so this component no longer carries any modal/form state.
     *   x-data="recurringPatterns({ patterns, projectKey, canManage })"
     */
    Alpine.data('recurringPatterns', function (initial) {
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            patterns: initial.patterns || [],
            message: '',
            messageType: 'success',
            showDeleteModal: false,
            deleting: null,
            deletingBusy: false,

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type || 'success';
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            scheduleLabel: function (p) { return _scheduleLabel(p); },

            toggleEnabled: async function (p) {
                if (!this.canManage) return;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + p.id + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({enabled: !p.enabled, lock_version: p.lock_version})
                    });
                    if (res.ok) {
                        // Refresh enabled + lock_version from the response so a
                        // subsequent toggle does not hit a stale-version 409.
                        var updated = await res.json();
                        p.enabled = updated.enabled;
                        p.lock_version = updated.lock_version;
                        this.flash('Pattern ' + (p.enabled ? 'enabled' : 'disabled') + '.', 'success');
                    } else {
                        this.flash('Failed to update pattern.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
            },

            confirmDelete: function (p) {
                if (!this.canManage) return;
                this.deleting = p;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deleting) return;
                this.deletingBusy = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.deleting.id + '/', {
                        method: 'DELETE',
                        headers: {'Content-Type': 'application/json'}
                    });
                    if (res.ok || res.status === 204) {
                        var id = this.deleting.id;
                        this.patterns = this.patterns.filter(function (x) { return x.id !== id; });
                        this.showDeleteModal = false;
                        this.deleting = null;
                        this.flash('Pattern deleted.', 'success');
                    } else {
                        this.flash('Failed to delete pattern.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.deletingBusy = false;
            }
        };
    });

    /**
     * Recurring task detail — skip occurrences and live preview. Editing now
     * happens on a dedicated form page (see recurringPatternForm), so this
     * component carries no form/modal state.
     *   x-data="recurringPatternDetail({ pattern, projectKey, canManage })"
     */
    Alpine.data('recurringPatternDetail', function (initial) {
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            pattern: initial.pattern || {},
            preview: [],
            previewLoading: false,
            previewError: false,
            message: '',
            messageType: 'success',

            init: function () {
                this.loadPreview();
            },

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type || 'success';
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            loadPreview: async function () {
                this.previewLoading = true;
                this.previewError = false;
                try {
                    var days = this.pattern.creation_lead_time_days || 30;
                    var url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.pattern.id + '/occurrences/?days=' + days;
                    var res = await spFetch(url);
                    if (res.ok) {
                        var data = await res.json();
                        this.preview = (data.occurrences || []).slice(0, 5).map(_fmtOccurrence);
                    } else {
                        this.previewError = true;
                    }
                } catch (_e) {
                    this.previewError = true;
                }
                this.previewLoading = false;
            },

            skipOccurrence: async function (occurrenceAt) {
                if (!this.canManage) return;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.pattern.id + '/skip/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({occurrence_at: occurrenceAt})
                    });
                    if (res.ok) {
                        this.flash('Occurrence skipped.', 'success');
                        window.location.reload();
                    } else {
                        this.flash('Failed to skip occurrence.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
            }
        };
    });

    /**
     * Project settings — Metadata tab.
     *
     * Manages preset enabling/disabling and custom schema CRUD.
     * Expects initial data via argument:
     *   x-data="projectMetadataSettings({ presets, enabledSlugs, schemas, trackers, projectKey })"
     */
    Alpine.data('projectMetadataSettings', function (initial) {
        var _knownIcons = ['code', 'bug', 'megaphone', 'sprint', 'book'];
        return {
            presets: initial.presets || [],
            enabledSlugs: initial.enabledSlugs || [],
            schemas: initial.schemas || [],
            trackers: initial.trackers || [],
            projectKey: initial.projectKey,
            enableScope: {},

            isKnownIcon(icon) {
                return _knownIcons.indexOf(icon) !== -1;
            },

            schemaFields(obj) {
                if (!obj || !obj.properties) return [];
                return Object.keys(obj.properties);
            },

            trackerName(trackerId) {
                for (var i = 0; i < this.trackers.length; i++) {
                    if (this.trackers[i].id === trackerId) return this.trackers[i].name;
                }
                return 'Tracker #' + trackerId;
            },

            showSchemaModal: false,
            showDeleteModal: false,
            showDisableWarning: false,
            editingSchema: null,
            deleteTarget: null,
            schemaError: '',
            disableWarningMsg: '',

            schemaForm: {name: '', tracker_id: '', schema_definition_raw: ''},

            isEnabled: function (slug) {
                return this.enabledSlugs.includes(slug);
            },

            getEnabledScope: function (slug) {
                var schema = this.schemas.find(function (s) { return s.preset_slug === slug; });
                if (!schema) return '';
                if (schema.tracker_id) {
                    var tracker = this.trackers.find(function (t) { return t.id === schema.tracker_id; });
                    return '(' + (tracker ? tracker.name : 'Tracker #' + schema.tracker_id) + ' only)';
                }
                return '(all trackers)';
            },

            enablePreset: async function (slug) {
                var trackerId = this.enableScope[slug] || null;
                var body = trackerId ? {tracker_id: parseInt(trackerId)} : {};
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-presets/' + slug + '/enable/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (resp.ok) {
                    var schema = await resp.json();
                    this.enabledSlugs.push(slug);
                    schema.usage_count = 0;
                    this.schemas.push(schema);
                }
            },

            disablePreset: async function (slug) {
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-presets/' + slug + '/disable/', {
                    method: 'DELETE'
                });
                if (resp.ok) {
                    this.enabledSlugs = this.enabledSlugs.filter(function (s) { return s !== slug; });
                    this.schemas = this.schemas.filter(function (s) { return s.preset_slug !== slug; });
                } else if (resp.status === 409) {
                    var data = await resp.json();
                    this.disableWarningMsg = (data.errors && data.errors[0] && data.errors[0].message) || 'Cannot disable: issues have data from this preset.';
                    this.showDisableWarning = true;
                }
            },

            openCreateSchema: function () {
                this.editingSchema = null;
                this.schemaError = '';
                this.schemaForm = {
                    name: '',
                    tracker_id: '',
                    schema_definition_raw: JSON.stringify({type: 'object', properties: {field_name: {type: 'string'}}}, null, 2)
                };
                this.showSchemaModal = true;
            },

            openEditSchema: function (schema) {
                this.editingSchema = schema;
                this.schemaError = '';
                this.schemaForm = {
                    name: schema.name,
                    tracker_id: schema.tracker_id || '',
                    schema_definition_raw: JSON.stringify(schema.schema_definition, null, 2)
                };
                this.showSchemaModal = true;
            },

            validateSchema: function () {
                if (!this.schemaForm.schema_definition_raw.trim()) {
                    this.schemaError = 'Schema definition is required.';
                    return false;
                }
                try {
                    var parsed = JSON.parse(this.schemaForm.schema_definition_raw);
                    if (parsed.type !== 'object') {
                        this.schemaError = 'Root type must be "object".';
                        return false;
                    }
                    this.schemaError = '';
                    return true;
                } catch (e) {
                    this.schemaError = 'Invalid JSON: ' + e.message;
                    return false;
                }
            },

            saveSchema: async function () {
                if (!this.validateSchema()) return;
                var parsed = JSON.parse(this.schemaForm.schema_definition_raw);
                var trackerId = this.schemaForm.tracker_id ? parseInt(this.schemaForm.tracker_id) : null;

                if (this.editingSchema) {
                    var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + this.editingSchema.id + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.schemaForm.name, tracker_id: trackerId, schema_definition: parsed})
                    });
                    if (resp.ok) {
                        var updated = await resp.json();
                        updated.usage_count = this.editingSchema.usage_count;
                        var idx = this.schemas.findIndex(function (s) { return s.id === this.editingSchema.id; }.bind(this));
                        if (idx !== -1) this.schemas[idx] = updated;
                    }
                } else {
                    var resp2 = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.schemaForm.name, tracker_id: trackerId, schema_definition: parsed})
                    });
                    if (resp2.ok) {
                        var created = await resp2.json();
                        created.usage_count = 0;
                        this.schemas.push(created);
                    }
                }
                this.showSchemaModal = false;
            },

            confirmDeleteSchema: async function (schema) {
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + schema.id + '/usage/');
                if (resp.ok) {
                    var data = await resp.json();
                    if (data.usage_count > 0) {
                        this.disableWarningMsg = 'Cannot delete: ' + data.usage_count + ' issue(s) use this schema.';
                        this.showDisableWarning = true;
                        return;
                    }
                }
                this.deleteTarget = schema;
                this.showDeleteModal = true;
            },

            doDeleteSchema: async function () {
                if (!this.deleteTarget) return;
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + this.deleteTarget.id + '/', {
                    method: 'DELETE'
                });
                if (resp.ok) {
                    this.schemas = this.schemas.filter(function (s) { return s.id !== this.deleteTarget.id; }.bind(this));
                }
                this.showDeleteModal = false;
                this.deleteTarget = null;
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

            initials(name) {
                return name.split(' ').map(function (w) { return w[0]; }).join('').substring(0, 2).toUpperCase();
            },

            capitalize(s) {
                return s.charAt(0).toUpperCase() + s.slice(1);
            },

            generatePassword: function () {
                var chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%&*';
                var arr = new Uint8Array(16);
                crypto.getRandomValues(arr);
                return Array.from(arr, function (b) { return chars[b % chars.length]; }).join('');
            },

            // Create user
            showCreate: false,
            creating: false,
            createError: '',
            newUser: { login: '', email: '', display_name: '', password: '', is_admin: false, is_service_account: false },

            async createUser() {
                this.creating = true;
                this.createError = '';
                var payload = Object.assign({}, this.newUser);
                // Service accounts don't need a password
                if (payload.is_service_account && !payload.password) {
                    delete payload.password;
                }
                if (!payload.password && !payload.is_service_account) {
                    this.createError = 'Password: required for regular users.';
                    this.creating = false;
                    return;
                }
                var res = await spFetch('/api/v1/admin/users/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    window.location.reload();
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    if (err.errors && err.errors.length > 0) {
                        this.createError = err.errors.map(function (e) {
                            var field = e.field ? e.field + ': ' : '';
                            return field + e.message;
                        }).join('\n');
                    } else {
                        this.createError = err.detail || 'Failed to create user.';
                    }
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
                var res = await spFetch('/api/v1/admin/users/' + this.resetUser.id + '/reset-password/', {
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
                var res = await spFetch('/api/v1/admin/users/' + u.id + '/' + action + '/', {
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
     * Admin user detail — view user info and manage API keys for any user.
     *
     * Expects initial data via argument:
     *   x-data="adminUserDetail({ targetUser: {...}, apiKeys: [...] })"
     */
    Alpine.data('adminUserDetail', function (initial) {
        return {
            targetUser: initial.targetUser || {},
            apiKeys: initial.apiKeys || [],

            initials(name) {
                return name.split(' ').map(function (w) { return w[0]; }).join('').substring(0, 2).toUpperCase();
            },

            capitalize(s) {
                return s.charAt(0).toUpperCase() + s.slice(1);
            },

            // Create key state
            newKeyName: '',
            newKey: null,
            creating: false,
            createError: '',
            copied: false,

            async loadKeys() {
                var res = await spFetch('/api/v1/admin/users/' + this.targetUser.id + '/api-keys/');
                if (res.ok) {
                    this.apiKeys = await res.json();
                }
            },

            async createKey() {
                if (!this.newKeyName.trim()) return;
                this.creating = true;
                this.createError = '';
                try {
                    var res = await spFetch('/api/v1/admin/users/' + this.targetUser.id + '/api-keys/', {
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
                        var errData = await res.json().catch(function () { return {}; });
                        this.createError = (errData.errors && errData.errors[0] && errData.errors[0].message) || errData.detail || 'Failed to create key';
                    }
                } catch (_e) {
                    this.createError = 'Unable to connect. Please try again.';
                }
                this.creating = false;
            },

            async revokeKey(id) {
                if (!confirm('Revoke this API key? This cannot be undone.')) return;
                var res = await spFetch('/api/v1/admin/users/' + this.targetUser.id + '/api-keys/' + id + '/', {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) {
                    await this.loadKeys();
                }
            },

            copyKey() {
                if (this.newKey) {
                    navigator.clipboard.writeText(this.newKey);
                    this.copied = true;
                    var self = this;
                    setTimeout(function () { self.copied = false; }, 2000);
                }
            },

            // MCP config snippet state — supports multiple client formats (JSON for
            // Claude/Cursor/Windsurf/Cline, TOML for Codex CLI via mcp-remote bridge).
            mcpClient: 'claude',
            mcpCopied: false,
            copyMcpConfig() {
                var refName = this.mcpClient === 'codex' ? 'mcpConfigCodex' : 'mcpConfigClaude';
                var el = this.$refs && this.$refs[refName];
                if (!el) return;
                navigator.clipboard.writeText(el.textContent);
                this.mcpCopied = true;
                var self = this;
                setTimeout(function () { self.mcpCopied = false; }, 2000);
            },

            formatDate(iso) {
                if (!iso) return '-';
                var d = new Date(iso);
                return d.toLocaleDateString('en-US', {year: 'numeric', month: 'short', day: 'numeric'});
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

            get itemKeys() {
                return Object.keys(this.items);
            },

            get hasItems() {
                return Object.keys(this.items).length > 0;
            },

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
                var res = await spFetch('/api/v1/admin/settings/', {
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
                        var res = await spFetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(self.query) + '&limit=8');
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

            get canSubmit() {
                return !this.submitting && this.subject.trim() !== '';
            },

            fileSizeKB(f) {
                return (f.size / 1024).toFixed(1) + ' KB';
            },

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

            _getMetadataFromRenderer() {
                // Collect metadata from nested metadataFieldRenderer component if present
                var el = this.$el.querySelector('[x-data*="metadataFieldRenderer"]');
                if (el && el._x_dataStack) {
                    for (var i = 0; i < el._x_dataStack.length; i++) {
                        var data = el._x_dataStack[i];
                        if (typeof data.getMetadataForSubmit === 'function') {
                            return data.getMetadataForSubmit();
                        }
                    }
                }
                return null;
            },

            async submitForm(continueCreating) {
                this.submitting = true;
                var metadataVal = this._getMetadataFromRenderer();
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
                if (metadataVal !== null) {
                    payload.metadata = metadataVal;
                }

                try {
                    if (this.mode === 'edit') {
                        payload.lock_version = this.lockVersion;
                        var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        if (res.ok) {
                            window.location.href = '/projects/' + this.projectKey + '/issues/' + this.displayKey + '/';
                        }
                    } else {
                        var res = await spFetch('/api/v1/projects/' + this.projectKey + '/issues/', {
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
                                await spFetch('/api/v1/attachments/', {
                                    method: 'POST',
                                    body: formData
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        /* Create pending relations */
                        for (var j = 0; j < this.pendingRelations.length; j++) {
                            var rel = this.pendingRelations[j];
                            try {
                                await spFetch('/api/v1/issues/' + data.key + '/relations/', {
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
     * Metadata field renderer for the issue create/edit form.
     *
     * Dynamically renders metadata fields based on project schemas and
     * selected tracker.  Exposes ``getMetadataForSubmit()`` so the parent
     * ``issueForm`` can include metadata in the API payload.
     */
    Alpine.data('metadataFieldRenderer', function (initial) {
        return {
            schemas: initial.schemas || [],
            trackerId: initial.trackerId || null,
            existingValues: initial.existingValues || {},
            currentFields: [],
            currentSchemaNames: [],
            metadata: { ...initial.existingValues },

            resolveFields() {
                var tid = parseInt(this.trackerId) || null;
                var matching = this.schemas.filter(function (s) {
                    return s.tracker_id === null || s.tracker_id === tid;
                });

                var fields = [];
                var names = [];
                for (var si = 0; si < matching.length; si++) {
                    var schema = matching[si];
                    names.push(schema.name);
                    var props = (schema.schema_definition && schema.schema_definition.properties) || {};
                    var keys = Object.keys(props);
                    for (var ki = 0; ki < keys.length; ki++) {
                        var key = keys[ki];
                        var prop = props[key];
                        var inputType = 'text', typeLabel = 'string', options = null, placeholder = '', min = null, max = null;
                        if (prop.enum) {
                            inputType = 'enum'; typeLabel = 'enum'; options = prop.enum;
                        } else if (prop.type === 'integer' || prop.type === 'number') {
                            inputType = 'number'; typeLabel = prop.type; min = prop.minimum; max = prop.maximum;
                        } else if (prop.type === 'boolean') {
                            inputType = 'boolean'; typeLabel = 'boolean';
                        } else if (prop.type === 'string' && prop.format === 'date') {
                            inputType = 'date'; typeLabel = 'date';
                        } else if (prop.type === 'array' && prop.items && prop.items.enum) {
                            inputType = 'multiselect'; typeLabel = 'multi'; options = prop.items.enum;
                        } else if (prop.type === 'array') {
                            inputType = 'tags'; typeLabel = 'array'; placeholder = 'Type and press Enter...';
                        } else {
                            placeholder = prop.description || '';
                        }

                        var label = key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
                        fields.push({ key: key, label: label, inputType: inputType, typeLabel: typeLabel, options: options, placeholder: placeholder, min: min, max: max });

                        // Initialize metadata value if not set
                        if (!(key in this.metadata)) {
                            if (inputType === 'tags' || inputType === 'multiselect') this.metadata[key] = [];
                            else if (inputType === 'boolean') this.metadata[key] = false;
                            else this.metadata[key] = '';
                        }
                    }
                }
                this.currentFields = fields;
                // Deduplicate schema names
                var unique = [];
                for (var i = 0; i < names.length; i++) {
                    if (unique.indexOf(names[i]) === -1) unique.push(names[i]);
                }
                this.currentSchemaNames = unique;
            },

            addTag(key, event) {
                var val = event.target.value.trim();
                if (!val) return;
                if (!this.metadata[key]) this.metadata[key] = [];
                if (this.metadata[key].indexOf(val) === -1) this.metadata[key].push(val);
                event.target.value = '';
            },

            removeLastTag(key, event) {
                if (event.target.value === '' && this.metadata[key] && this.metadata[key].length > 0) {
                    this.metadata[key].pop();
                }
            },

            hasMultiValue(key, opt) {
                return (this.metadata[key] || []).indexOf(opt) !== -1;
            },

            toggleMulti(key, opt) {
                if (!this.metadata[key]) this.metadata[key] = [];
                var idx = this.metadata[key].indexOf(opt);
                if (idx >= 0) this.metadata[key].splice(idx, 1);
                else this.metadata[key].push(opt);
            },

            schemaNameLabel() {
                return this.currentSchemaNames.join(' + ');
            },

            getMetadataForSubmit() {
                var result = {};
                for (var i = 0; i < this.currentFields.length; i++) {
                    var f = this.currentFields[i];
                    var v = this.metadata[f.key];
                    if (v === '' || v === null || v === undefined) continue;
                    if (Array.isArray(v) && v.length === 0) continue;
                    result[f.key] = v;
                }
                return result;
            }
        };
    });

    /**
     * Issue detail metadata panel — collapsible, editable.
     *
     * Shows metadata fields from applicable schemas with inline editing
     * and save/discard functionality.
     */
    Alpine.data('issueMetadataPanel', function (initial) {
        function resolveFieldsFromSchemas(schemas, metadataObj) {
            var fields = [];
            var names = [];
            for (var si = 0; si < schemas.length; si++) {
                var schema = schemas[si];
                names.push(schema.name);
                var props = (schema.schema_definition && schema.schema_definition.properties) || {};
                var keys = Object.keys(props);
                for (var ki = 0; ki < keys.length; ki++) {
                    var key = keys[ki];
                    var prop = props[key];
                    var inputType = 'text', typeLabel = 'string', options = null, placeholder = '', min = null, max = null;
                    if (prop.enum) {
                        inputType = 'enum'; typeLabel = 'enum'; options = prop.enum;
                    } else if (prop.type === 'integer' || prop.type === 'number') {
                        inputType = 'number'; typeLabel = prop.type; min = prop.minimum; max = prop.maximum;
                    } else if (prop.type === 'boolean') {
                        inputType = 'boolean'; typeLabel = 'boolean';
                    } else if (prop.type === 'string' && prop.format === 'date') {
                        inputType = 'date'; typeLabel = 'date';
                    } else if (prop.type === 'array' && prop.items && prop.items.enum) {
                        inputType = 'multiselect'; typeLabel = 'multi'; options = prop.items.enum;
                    } else if (prop.type === 'array') {
                        inputType = 'tags'; typeLabel = 'array'; placeholder = 'Type and press Enter...';
                    } else {
                        placeholder = prop.description || '';
                    }
                    var label = key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
                    fields.push({ key: key, label: label, inputType: inputType, typeLabel: typeLabel, options: options, placeholder: placeholder, min: min, max: max });

                    // Initialize metadata value if not set
                    if (!(key in metadataObj)) {
                        if (inputType === 'tags' || inputType === 'multiselect') metadataObj[key] = [];
                        else if (inputType === 'boolean') metadataObj[key] = false;
                        else metadataObj[key] = '';
                    }
                }
            }
            // Deduplicate
            var unique = [];
            for (var i = 0; i < names.length; i++) {
                if (unique.indexOf(names[i]) === -1) unique.push(names[i]);
            }
            return { fields: fields, schemaNames: unique };
        }

        var metadataObj = JSON.parse(JSON.stringify(initial.metadata || {}));
        var resolved = resolveFieldsFromSchemas(initial.schemas || [], metadataObj);

        return {
            expanded: false,
            dirty: false,
            saving: false,
            fields: resolved.fields,
            schemaNames: resolved.schemaNames,
            metadata: metadataObj,
            savedMetadata: JSON.parse(JSON.stringify(metadataObj)),
            issueRef: initial.issueRef || '',
            projectKey: initial.projectKey || '',
            lockVersion: initial.lockVersion || 0,

            get totalCount() { return this.fields.length; },
            get filledCount() {
                var count = 0;
                for (var i = 0; i < this.fields.length; i++) {
                    var v = this.metadata[this.fields[i].key];
                    if (Array.isArray(v)) { if (v.length > 0) count++; }
                    else if (typeof v === 'boolean') { count++; }
                    else if (v !== '' && v !== null && v !== undefined) { count++; }
                }
                return count;
            },

            metadataSearchUrl(slug, value, isArray) {
                return '/search/?scope=issues&mf=' + encodeURIComponent(slug) +
                    '&mv=' + encodeURIComponent(value) + '&ma=' + (isArray ? '1' : '0');
            },

            get summaryChips() {
                var chips = [];
                for (var i = 0; i < this.fields.length; i++) {
                    var f = this.fields[i];
                    var v = this.metadata[f.key];
                    if (v === '' || v === null || v === undefined) continue;
                    if (Array.isArray(v) && v.length === 0) continue;
                    var cls = '';
                    var values = [];
                    if (Array.isArray(v)) {
                        // Each array element renders as its own clickable tag-link.
                        cls = 'sp-ms-tag';
                        for (var j = 0; j < v.length; j++) {
                            values.push({ text: String(v[j]), href: this.metadataSearchUrl(f.key, v[j], true) });
                        }
                    } else if (typeof v === 'boolean') {
                        values.push({ text: v ? 'Yes' : 'No', href: null });
                    } else if (f.inputType === 'enum') {
                        cls = 'sp-ms-enum';
                        values.push({ text: String(v), href: this.metadataSearchUrl(f.key, v, false) });
                    } else {
                        values.push({ text: String(v), href: this.metadataSearchUrl(f.key, v, false) });
                    }
                    chips.push({ key: f.key, label: f.label, values: values, chipClass: cls });
                }
                return chips;
            },

            addTag(key, event) {
                var val = event.target.value.trim();
                if (!val) return;
                if (!this.metadata[key]) this.metadata[key] = [];
                if (this.metadata[key].indexOf(val) === -1) this.metadata[key].push(val);
                event.target.value = '';
                this.dirty = true;
            },

            removeLastTag(key, event) {
                if (event.target.value === '' && this.metadata[key] && this.metadata[key].length > 0) {
                    this.metadata[key].pop();
                    this.dirty = true;
                }
            },

            hasMultiValue(key, opt) {
                return (this.metadata[key] || []).indexOf(opt) !== -1;
            },

            toggleMulti(key, opt) {
                if (!this.metadata[key]) this.metadata[key] = [];
                var idx = this.metadata[key].indexOf(opt);
                if (idx >= 0) this.metadata[key].splice(idx, 1);
                else this.metadata[key].push(opt);
                this.dirty = true;
            },

            revert() {
                this.metadata = JSON.parse(JSON.stringify(this.savedMetadata));
                this.dirty = false;
            },

            async save() {
                this.saving = true;
                try {
                    // Build clean metadata (only filled values)
                    var clean = {};
                    for (var i = 0; i < this.fields.length; i++) {
                        var f = this.fields[i];
                        var v = this.metadata[f.key];
                        if (v === '' || v === null || v === undefined) continue;
                        if (Array.isArray(v) && v.length === 0) continue;
                        clean[f.key] = v;
                    }
                    var res = await spFetch('/api/v1/issues/' + this.issueRef + '/', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ metadata: clean, lock_version: this.lockVersion })
                    });
                    if (res.ok) {
                        var data = await res.json();
                        if (data.lock_version) this.lockVersion = data.lock_version;
                        this.savedMetadata = JSON.parse(JSON.stringify(this.metadata));
                        this.dirty = false;
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.saving = false;
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

            get canSubmit() {
                return !this.submitting && this.notes.trim() !== '';
            },

            async submit() {
                this.submitting = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/journals/', {
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

            get canSubmitReply() {
                return !this.submitting && this.replyText.trim() !== '';
            },

            async submitReply() {
                if (!this.replyText.trim()) return;
                this.submitting = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/journals/', {
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

    /**
     * Markdown editor wrapper around EasyMDE (vendored, CodeMirror 5 based).
     *
     * Usage in templates:
     *   <div x-data='markdownEditor({
     *       initial: "...",
     *       previewUrl: "/api/v1/markdown/preview/",
     *       context: "wiki" | "issue",
     *       onPasteDrop: "componentMethodName"   // optional
     *   })'>
     *     <textarea x-ref="textarea"></textarea>
     *   </div>
     *
     * Two-way binding: read/write the current Markdown via `value`. The
     * underlying EasyMDE instance is stored on `editor`. The component is
     * intentionally torn down on destroy to avoid leaking CodeMirror
     * instances when toggling edit mode.
     *
     * Preview is rendered server-side via POST to previewUrl (debounced),
     * never via EasyMDE's bundled marked.js — server rendering is the source
     * of truth and supports KEY-123 autolinks and mentions. The server
     * sanitises markdown output (see specivo/services/markdown_service.py),
     * so injecting the returned HTML into the preview element is safe.
     *
     * If `onPasteDrop` is provided, the named method on the *parent* Alpine
     * component is invoked with (event, kind, mdEditor) where kind is
     * 'paste' or 'drop'. The parent is responsible for handling file
     * extraction, upload, and inserting the resulting markdown via
     * `mdEditor.insertText(...)`.
     */
    Alpine.data('markdownEditor', function (initial) {
        initial = initial || {};
        return {
            value: initial.initial || '',
            editor: null,
            previewUrl: initial.previewUrl || '/api/v1/markdown/preview/',
            context: initial.context || 'wiki',
            onPasteDrop: initial.onPasteDrop || '',
            _previewTimer: null,
            _lastPreviewText: null,
            _lastPreviewHtml: '',

            init() {
                var self = this;
                if (typeof window.EasyMDE === 'undefined') {
                    /* EasyMDE asset not loaded — keep textarea functional. */
                    return;
                }
                var ta = this.$refs.textarea;
                if (!ta) return;
                ta.value = this.value;

                this.editor = new window.EasyMDE({
                    element: ta,
                    autoDownloadFontAwesome: false,
                    spellChecker: false,
                    /* Disable status bar items that imply spell-check or remote fetch. */
                    status: ['lines', 'words'],
                    tabSize: 2,
                    indentWithTabs: false,
                    forceSync: true,
                    minHeight: '200px',
                    placeholder: 'Write your content here (Markdown supported)...',
                    /* Use built-in toolbar shorthand strings — EasyMDE wires up the
                     * matching prototype methods (togglePreview, toggleSideBySide,
                     * toggleFullScreen) internally. Passing static method refs as
                     * `action` was redundant and risked breaking when the bundle's
                     * internal toolbar map was rebuilt against unbound prototype
                     * methods on click. */
                    toolbar: [
                        'bold', 'italic', 'code', '|',
                        'heading-1', 'heading-2', 'heading-3', '|',
                        'unordered-list', 'ordered-list', 'quote', 'horizontal-rule', '|',
                        'link', '|',
                        'preview'
                        /* 'side-by-side' and 'fullscreen' deliberately omitted —
                         * EasyMDE pins them with position:fixed and assumes the
                         * editor owns the viewport, which clobbers the page chrome
                         * (sidebar nav, header, metadata panel). Preview-only is
                         * the right affordance for an editor embedded in a card. */
                    ],
                    previewRender: function (plainText, previewElement) {
                        return self._renderPreview(plainText, previewElement);
                    }
                });

                /* Refresh CodeMirror on the first user interaction.
                 *
                 * After EasyMDE's gfm-overlay mode finishes async tokenization,
                 * the doc replaces the Line object for each tokenized line, but
                 * the cached display.view items still reference the previous
                 * Line objects. A subsequent prepareSelection -> Bn(viewItem,
                 * line, n) checks `viewItem.line === line`, the identity check
                 * fails, the function falls through to a non-rest branch and
                 * returns undefined, and the caller throws "Cannot read
                 * properties of undefined (reading 'map')" — the symptom
                 * users see as silent keystroke drops on every click into
                 * the editor body.
                 *
                 * Calling cm.refresh() rebuilds display.view from the current
                 * Doc, restoring view[i].line === doc line i. The catch is
                 * timing: a refresh scheduled from init (rAF, double-rAF,
                 * setTimeout) all fire BEFORE the gfm tokenizer has finished
                 * its own deferred work, so the view gets re-corrupted right
                 * after we rebuild it. Refreshing on the first mousedown /
                 * focus runs after every async setup is done, when the user
                 * actually wants to interact. A flag ensures we only do this
                 * once per editor lifetime; subsequent interactions go through
                 * a clean view. */
                var cm = this.editor.codemirror;
                var firstInteractionDone = false;
                var refreshOnFirstUse = function () {
                    if (firstInteractionDone) return;
                    firstInteractionDone = true;
                    if (!self.editor || self.editor.codemirror !== cm) return;
                    cm.refresh();
                };
                cm.on('mousedown', refreshOnFirstUse);
                cm.on('focus', refreshOnFirstUse);

                /* Keep Alpine `value` in sync with the editor. */
                cm.on('change', function () {
                    self.value = self.editor.value();
                });

                /* Seed the editor with the current bound value, then keep it in
                 * sync when the parent updates `value` (via x-modelable / x-model).
                 * This matters when the wrapper is rendered inside x-show: Alpine
                 * inits the component on page load when the parent's bound value
                 * may still be empty, so EasyMDE's initial snapshot is empty.
                 * Once the parent populates the value (e.g. issue Edit click sets
                 * draft = description), we push it into the editor view. Also
                 * covers Cancel reverting drafts back to the saved value.
                 *
                 * After every value swap we call codemirror.refresh() on the next
                 * tick. CodeMirror caches scroller/sizer dimensions at init time;
                 * if the wrapper was display:none at init (x-show="editing"), the
                 * cached metrics are wrong and content renders pushed to the
                 * bottom of the pane. refresh() recomputes them once the wrapper
                 * is visible and the value has settled. */
                var refreshCm = function () {
                    if (!self.editor || !self.editor.codemirror) return;
                    self.$nextTick(function () {
                        self.editor.codemirror.refresh();
                    });
                };
                if (this.value && this.editor.value() !== this.value) {
                    this.editor.value(this.value);
                    refreshCm();
                }
                this.$watch('value', function (newValue) {
                    if (!self.editor) return;
                    if (self.editor.value() === (newValue || '')) return;
                    self.editor.value(newValue || '');
                    refreshCm();
                });

                /* Expose paste/drop hooks for the parent component.
                 * Walk parent elements asking Alpine for their data scope until
                 * we find one that exposes the named method. Uses the public
                 * Alpine.$data(el) API which returns the merged data proxy. */
                if (this.onPasteDrop) {
                    var dispatch = function (event, kind) {
                        var node = self.$el.parentElement;
                        while (node) {
                            try {
                                var data = window.Alpine && window.Alpine.$data
                                    ? window.Alpine.$data(node)
                                    : null;
                                if (data && typeof data[self.onPasteDrop] === 'function') {
                                    data[self.onPasteDrop](event, kind, self);
                                    return;
                                }
                            } catch (_e) { /* node has no data scope */ }
                            node = node.parentElement;
                        }
                    };
                    this.editor.codemirror.on('paste', function (cm, event) {
                        dispatch(event, 'paste');
                    });
                    this.editor.codemirror.on('drop', function (cm, event) {
                        dispatch(event, 'drop');
                    });
                }

                /* Add a stable hook class so CSS overrides can target the wrapper. */
                var wrapper = ta.nextElementSibling;
                if (wrapper && wrapper.classList && wrapper.classList.contains('EasyMDEContainer')) {
                    wrapper.classList.add('sp-md-editor');
                }

                /* When the editor lives inside a hidden ancestor (x-show="editing"),
                 * CodeMirror caches zero-size metrics at init and renders content
                 * pushed to the bottom of the pane once the wrapper is shown. An
                 * IntersectionObserver covers every reveal — including the first
                 * Edit click and any subsequent show/hide cycle — by refreshing
                 * the editor once the container intersects the viewport.
                 *
                 * The observer is attached directly to the DOM element (not the
                 * Alpine reactive state) because Alpine's proxy strips non-plain
                 * objects on assignment. destroy() reads it back from the DOM. */
                if (wrapper && typeof window.IntersectionObserver === 'function') {
                    var io = new IntersectionObserver(function (entries) {
                        entries.forEach(function (entry) {
                            if (entry.isIntersecting && self.editor && self.editor.codemirror) {
                                self.editor.codemirror.refresh();
                            }
                        });
                    });
                    io.observe(wrapper);
                    wrapper._spVisibilityObserver = io;
                }
            },

            destroy() {
                if (this._previewTimer) {
                    clearTimeout(this._previewTimer);
                    this._previewTimer = null;
                }
                /* Disconnect the visibility observer attached to the wrapper. */
                var ta = this.$refs && this.$refs.textarea;
                var wrapper = ta && ta.nextElementSibling;
                if (wrapper && wrapper._spVisibilityObserver) {
                    try { wrapper._spVisibilityObserver.disconnect(); } catch (_e) { /* already gone */ }
                    wrapper._spVisibilityObserver = null;
                }
                if (this.editor) {
                    try {
                        this.editor.toTextArea();
                    } catch (_e) { /* already torn down */ }
                    this.editor = null;
                }
            },

            /* Replace the editor contents (used by parent when resetting drafts). */
            setValue(text) {
                this.value = text || '';
                if (this.editor) {
                    this.editor.value(this.value);
                }
            },

            /* Insert text at the current cursor position (used by paste/drop upload). */
            insertText(text) {
                if (!this.editor) return;
                var cm = this.editor.codemirror;
                cm.replaceSelection(text);
                cm.focus();
            },

            /* Server-side preview with debounce. */
            _renderPreview(plainText, previewElement) {
                var self = this;
                if (this._lastPreviewText === plainText) {
                    return this._lastPreviewHtml;
                }
                if (this._previewTimer) {
                    clearTimeout(this._previewTimer);
                }
                /* Loading placeholder — plain text only, no HTML injection. */
                previewElement.textContent = 'Rendering...';
                this._previewTimer = setTimeout(function () {
                    self._fetchPreview(plainText, previewElement);
                }, 250);
                return '';
            },

            async _fetchPreview(plainText, previewElement) {
                try {
                    var res = await spFetch(this.previewUrl, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({text: plainText, context: this.context})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this._lastPreviewText = plainText;
                        this._lastPreviewHtml = data.html || '';
                        /* Server returns sanitised HTML (markdown_service.preview).
                         * Same path as the saved-content rendering, so this matches
                         * what the user will see after Save. */
                        previewElement.innerHTML = this._lastPreviewHtml;  /* noqa: XSS */
                    } else {
                        previewElement.textContent = 'Preview unavailable.';
                    }
                } catch (_e) {
                    previewElement.textContent = 'Preview unavailable.';
                }
            }
        };
    });

    Alpine.data('descriptionEditor', function (initial) {
        return {
            subject: initial.subject || '',
            description: initial.description || '',
            subjectDraft: '',
            draft: '',
            editing: false,
            saving: false,
            error: '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            startEdit() {
                this.subjectDraft = this.subject;
                this.draft = this.description;
                this.error = '';
                this.editing = true;
                /* Focus the title input on next tick so Alpine has rendered it */
                this.$nextTick(function () {
                    if (this.$refs.subjectInput) {
                        this.$refs.subjectInput.focus();
                    }
                }.bind(this));
            },

            cancelEdit() {
                this.subjectDraft = '';
                this.draft = '';
                this.error = '';
                this.editing = false;
            },

            async save() {
                /* Client-side validation: title must contain non-whitespace */
                var trimmed = (this.subjectDraft || '').trim();
                if (!trimmed) {
                    this.error = 'Title cannot be empty.';
                    return;
                }
                this.error = '';
                this.saving = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            subject: trimmed,
                            description: this.draft,
                            lock_version: this.lockVersion
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var msg = '';
                        try {
                            var body = await res.json();
                            if (body && body.errors && body.errors.length) {
                                msg = body.errors[0].message || '';
                            }
                        } catch (_e) { /* ignore parse errors */ }
                        this.error = msg || 'Could not save changes.';
                    }
                } catch (_e) {
                    this.error = 'Network error. Please retry.';
                }
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

            /* CSP-safe event handlers for select/input changes */
            onSelectInt(fieldName, event) {
                this.updateField(fieldName, parseInt(event.target.value) || null);
            },
            onSelectIntRequired(fieldName, event) {
                this.updateField(fieldName, parseInt(event.target.value));
            },
            onInputChange(fieldName, event) {
                this.updateField(fieldName, event.target.value || null);
            },

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
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
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
     * Generic autocomplete for picking a single entity (version or sprint)
     * and PATCHing an issue field with the selected id.
     *
     * initial: {
     *   endpoint,       // e.g. /api/v1/projects/FOO/versions/search/
     *   field,          // e.g. fixed_version_id
     *   displayKey,     // e.g. FOO-42
     *   lockVersion,    // issue lock_version
     *   currentId,      // currently-assigned id or null
     *   currentLabel,   // currently-assigned label
     *   currentStatus,  // status string (e.g. 'open','closed','completed')
     *   kind            // 'version' or 'sprint'
     * }
     */
    Alpine.data('entityAutocomplete', function (initial) {
        return {
            endpoint: initial.endpoint || '',
            field: initial.field || '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,
            kind: initial.kind || 'version',

            selectedId: initial.currentId || null,
            selectedLabel: initial.currentLabel || '',
            selectedStatus: initial.currentStatus || '',
            query: initial.currentLabel || '',

            results: [],
            defaultResults: [],
            open: false,
            loading: false,
            activeIndex: -1,
            debounceTimer: null,
            _lastQuery: null,

            _formatLabel(item) {
                var label = item.name;
                var closedLike = ['closed', 'locked', 'completed'];
                if (item.status && closedLike.indexOf(item.status) !== -1) {
                    label += ' (' + item.status + ')';
                }
                return label;
            },

            _ensureCurrentPinned(list) {
                if (!this.selectedId) return list;
                for (var i = 0; i < list.length; i++) {
                    if (list[i].id === this.selectedId) return list;
                }
                // Inject a synthetic entry for the current selection so it stays visible
                var pinned = {
                    id: this.selectedId,
                    name: this.selectedLabel,
                    status: this.selectedStatus || ''
                };
                return [pinned].concat(list);
            },

            async fetchDefault() {
                if (this.defaultResults.length) {
                    this.results = this._ensureCurrentPinned(this.defaultResults.slice());
                    return;
                }
                this.loading = true;
                try {
                    var res = await spFetch(this.endpoint);
                    if (res.ok) {
                        var data = await res.json();
                        this.defaultResults = data;
                        this.results = this._ensureCurrentPinned(data.slice());
                    }
                } catch (_e) {}
                this.loading = false;
            },

            onFocus() {
                this.open = true;
                if (!this.query || this.query === this.selectedLabel) {
                    this.fetchDefault();
                }
            },

            onBlur() {
                var self = this;
                // Delay so click on a row still fires select()
                setTimeout(function () {
                    self.open = false;
                    self.activeIndex = -1;
                    // If user typed but didn't select, restore previous label
                    if (self.query !== self.selectedLabel) {
                        self.query = self.selectedLabel;
                    }
                }, 150);
            },

            search() {
                var q = (this.query || '').trim();
                if (q === this._lastQuery) return;
                this._lastQuery = q;
                clearTimeout(this.debounceTimer);
                var self = this;
                if (!q) {
                    this.fetchDefault();
                    return;
                }
                this.debounceTimer = setTimeout(async function () {
                    self.loading = true;
                    try {
                        var res = await spFetch(self.endpoint + '?q=' + encodeURIComponent(q) + '&limit=20');
                        if (res.ok) {
                            self.results = await res.json();
                            self.activeIndex = self.results.length ? 0 : -1;
                        }
                    } catch (_e) {}
                    self.loading = false;
                }, 200);
            },

            init() {
                var self = this;
                this.$watch('query', function () {
                    self.open = true;
                    self.search();
                });
            },

            moveDown() {
                if (!this.open) { this.open = true; this.fetchDefault(); return; }
                if (this.activeIndex < this.results.length - 1) this.activeIndex++;
            },

            moveUp() {
                if (this.activeIndex > 0) this.activeIndex--;
            },

            confirm() {
                if (this.activeIndex >= 0 && this.activeIndex < this.results.length) {
                    this.select(this.results[this.activeIndex]);
                }
            },

            cancel() {
                this.open = false;
                this.activeIndex = -1;
                this.query = this.selectedLabel;
            },

            async clearSelection() {
                this.selectedId = null;
                this.selectedLabel = '';
                this.selectedStatus = '';
                this.query = '';
                this.open = false;
                await this._commit(null);
            },

            async select(item) {
                this.selectedId = item.id;
                this.selectedLabel = item.name;
                this.selectedStatus = item.status || '';
                this.query = item.name;
                this.open = false;
                this.activeIndex = -1;
                await this._commit(item.id);
            },

            async _commit(value) {
                var payload = {lock_version: this.lockVersion};
                payload[this.field] = value;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        window.location.reload();
                    }
                } catch (_e) {}
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
                    var res = await spFetch('/api/v1/projects/', {
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
                            var check = await spFetch(url, {method: 'HEAD'});
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
                        var res = await spFetch('/api/v1/projects/' + this.form.key + '/', {
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
                        var res = await spFetch('/api/v1/projects/', {
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
                await spFetch('/api/v1/admin/projects/' + p.key + '/archive/', { method: 'POST' });
                location.reload();
            },

            async unarchiveProject(p) {
                await spFetch('/api/v1/admin/projects/' + p.key + '/unarchive/', { method: 'POST' });
                location.reload();
            }
        };
    });

    /**
     * Admin versions management — cross-project version list with bulk actions.
     *
     * Usage:
     *   x-data='adminVersions({ versions: [...], projects: [...] })'
     */
    Alpine.data('versionIssues', function (initial) {
        return {
            filter: 'all',
            issues: (initial && initial.issues) || [],

            assigneeInitials(issue) {
                if (!issue.assignee) return '';
                return issue.assignee.substring(0, 2).toUpperCase();
            },

            get filtered() {
                if (this.filter === 'open') return this.issues.filter(function (i) { return i.is_open; });
                if (this.filter === 'closed') return this.issues.filter(function (i) { return !i.is_open; });
                return this.issues;
            }
        };
    });

    Alpine.data('adminVersions', function (initial) {
        return {
            allVersions: (initial && initial.versions) || [],
            projects: (initial && initial.projects) || [],
            filterProject: '',
            filterStatus: '',
            selected: [],

            countByStatus(status) {
                var count = 0;
                for (var i = 0; i < this.allVersions.length; i++) {
                    if (this.allVersions[i].status === status) count++;
                }
                return count;
            },

            get filtered() {
                var self = this;
                return this.allVersions.filter(function (v) {
                    if (self.filterProject && v.project_key !== self.filterProject) return false;
                    if (self.filterStatus && v.status !== self.filterStatus) return false;
                    return true;
                });
            },

            toggleAll: function (e) {
                if (e.target.checked) {
                    this.selected = this.filtered.map(function (v) { return v.id; });
                } else {
                    this.selected = [];
                }
            },

            async bulkAction(newStatus) {
                var self = this;
                var ids = this.selected.slice();
                var promises = [];
                for (var i = 0; i < ids.length; i++) {
                    var ver = this.allVersions.find(function (v) { return v.id === ids[i]; });
                    if (!ver) continue;
                    promises.push(
                        spFetch('/api/v1/projects/' + ver.project_key + '/versions/' + ver.id + '/', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ status: newStatus })
                        })
                    );
                }
                await Promise.all(promises);
                // Update local state
                for (var j = 0; j < ids.length; j++) {
                    var v = this.allVersions.find(function (item) { return item.id === ids[j]; });
                    if (v) v.status = newStatus;
                }
                this.selected = [];
            },

            async deleteVersion(v) {
                if (!confirm('Delete version "' + v.name + '"?')) return;
                var res = await spFetch('/api/v1/projects/' + v.project_key + '/versions/' + v.id + '/', {
                    method: 'DELETE'
                });
                if (res.ok) {
                    this.allVersions = this.allVersions.filter(function (item) { return item.id !== v.id; });
                    this.selected = this.selected.filter(function (id) { return id !== v.id; });
                }
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
            pageId: initial.pageId || 0,
            lockVersion: initial.lockVersion || 0,
            preview: false,
            uploading: false,

            /**
             * Paste/drop handler invoked by the markdownEditor wrapper.
             * On image paste or drop, upload the file as a wiki attachment
             * and insert markdown referring to it at the cursor.
             *
             * Works only in edit mode when a pageId is available; for new
             * pages there is no container yet, so we let the default
             * editor behaviour (raw paste of text) run.
             */
            async handleEditorPasteDrop(event, kind, mdEditor) {
                if (!this.pageId) return; /* no container yet (create mode) */
                var files = [];
                if (kind === 'paste') {
                    var items = event.clipboardData && event.clipboardData.items;
                    if (!items) return;
                    for (var i = 0; i < items.length; i++) {
                        if (items[i].kind === 'file') {
                            var f = items[i].getAsFile();
                            if (f) files.push(f);
                        }
                    }
                } else if (kind === 'drop') {
                    var dt = event.dataTransfer;
                    if (!dt || !dt.files || !dt.files.length) return;
                    for (var j = 0; j < dt.files.length; j++) {
                        files.push(dt.files[j]);
                    }
                }
                if (!files.length) return;
                event.preventDefault();
                this.uploading = true;
                for (var k = 0; k < files.length; k++) {
                    await this._uploadAndInsert(files[k], mdEditor);
                }
                this.uploading = false;
            },

            async _uploadAndInsert(file, mdEditor) {
                try {
                    var formData = new FormData();
                    formData.append('file', file);
                    formData.append('container_type', 'WikiPage');
                    formData.append('container_id', this.pageId);
                    var res = await spFetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });
                    if (!res.ok) return;
                    var data = await res.json();
                    var url = '/api/v1/attachments/' + data.id + '/download/';
                    var isImage = (file.type || '').indexOf('image/') === 0;
                    var snippet = isImage
                        ? '![' + (data.filename || file.name) + '](' + url + ')'
                        : '[' + (data.filename || file.name) + '](' + url + ')';
                    mdEditor.insertText(snippet + '\n');
                } catch (_e) { /* upload failed silently — user can retry */ }
            },

            get canSubmit() {
                if (this.submitting) return false;
                if (this.mode === 'create' && !this.title.trim()) return false;
                return true;
            },

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
                    var res = await spFetch(url, {
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
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/resolve/', {
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
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/watchers/', {
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
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/time-entries/', {
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
        var _searchTimer = null;
        return {
            showForm: false,
            issueToKey: '',
            relationType: 'relates',
            submitting: false,
            error: '',
            issueKey: initial.issueKey || '',

            // Autocomplete state
            suggestions: [],
            showSuggestions: false,
            highlightIndex: -1,
            searching: false,

            init() {
                this.$watch('issueToKey', function (val) {
                    clearTimeout(_searchTimer);
                    var query = (val || '').trim();
                    if (query.length < 2) {
                        this.suggestions = [];
                        this.showSuggestions = false;
                        return;
                    }
                    var self = this;
                    _searchTimer = setTimeout(function () { self.searchIssues(query); }, 300);
                }.bind(this));
            },

            get canSubmit() {
                return !this.submitting && this.issueToKey.trim() !== '';
            },

            async searchIssues(query) {
                this.searching = true;
                try {
                    var res = await spFetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(query) + '&limit=8');
                    if (res.ok) {
                        var data = await res.json();
                        this.suggestions = data
                            .filter(function (r) { return r.key !== initial.issueKey; });
                        this.showSuggestions = this.suggestions.length > 0;
                        this.highlightIndex = -1;
                    }
                } catch (_e) {
                    /* search failed silently */
                }
                this.searching = false;
            },

            selectSuggestion(s) {
                this.issueToKey = s.key;
                this.showSuggestions = false;
                this.suggestions = [];
            },

            onKeydown(event) {
                if (!this.showSuggestions) return;
                if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    this.highlightIndex = Math.min(this.highlightIndex + 1, this.suggestions.length - 1);
                } else if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    this.highlightIndex = Math.max(this.highlightIndex - 1, 0);
                } else if (event.key === 'Enter' && this.highlightIndex >= 0) {
                    event.preventDefault();
                    this.selectSuggestion(this.suggestions[this.highlightIndex]);
                } else if (event.key === 'Escape') {
                    this.showSuggestions = false;
                }
            },

            closeSuggestions() {
                // Delay to allow click on suggestion to fire first
                var self = this;
                setTimeout(function () { self.showSuggestions = false; }, 200);
            },

            async submit() {
                this.submitting = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/issues/' + this.issueKey + '/relations/', {
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
                    var res = await spFetch('/api/v1/attachments/', {
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
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/reactions/' + emoji + '/', {
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

    /**
     * Wiki page attachments — gallery, file list, upload, delete.
     * Expects initial data via argument:
     *   x-data="wikiAttachments({ pageId, attachments })"
     */
    Alpine.data('wikiAttachments', function (initial) {
        if (initial.dataElementId && !initial.attachments) {
            var el = document.getElementById(initial.dataElementId);
            initial.attachments = el ? JSON.parse(el.textContent) : [];
        }
        return spAttachmentsComponent(initial, 'WikiPage', 'pageId');
    });

    /**
     * Generic attachments component — used by both wiki pages and issues.
     * Expects initial data with containerId, containerType, and attachments array.
     *   x-data="issueAttachments({ containerId, attachments })"
     */
    Alpine.data('issueAttachments', function (initial) {
        /* CSP-safe: parse attachments from a <script type=application/json> element */
        if (initial.dataElementId && !initial.attachments) {
            var el = document.getElementById(initial.dataElementId);
            initial.attachments = el ? JSON.parse(el.textContent) : [];
        }
        return spAttachmentsComponent(initial, 'Issue', 'containerId');
    });

    function spAttachmentsComponent(initial, defaultContainerType, idField) {
        var IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'];
        var EXT_MAP = {
            pdf: 'pdf', doc: 'doc', docx: 'doc',
            xls: 'xls', xlsx: 'xls', csv: 'xls',
            zip: 'zip', gz: 'zip', tar: 'zip', '7z': 'zip', rar: 'zip',
            txt: 'txt', md: 'txt', json: 'txt', yml: 'txt', yaml: 'txt',
            png: 'img', jpg: 'img', jpeg: 'img', gif: 'img', webp: 'img', svg: 'img',
            mp4: 'vid', mov: 'vid', avi: 'vid', mkv: 'vid',
            mp3: 'aud', wav: 'aud', ogg: 'aud', flac: 'aud'
        };

        function getExt(filename) {
            var parts = (filename || '').split('.');
            return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
        }

        var containerId = initial.containerId || initial.pageId || 0;
        var containerType = initial.containerType || defaultContainerType;

        return {
            containerId: containerId,
            containerType: containerType,
            attachments: initial.attachments || [],
            showUpload: false,
            isDragging: false,
            uploads: [],
            uploadError: '',
            lightbox: null,
            deleteTarget: null,
            deleting: false,

            get images() {
                return this.attachments.filter(function (a) {
                    return IMAGE_TYPES.indexOf(a.content_type) !== -1;
                });
            },

            get files() {
                return this.attachments.filter(function (a) {
                    return IMAGE_TYPES.indexOf(a.content_type) === -1;
                });
            },

            fileIconClass(att) {
                var ext = getExt(att.filename);
                return EXT_MAP[ext] || 'generic';
            },

            fileIconLabel(att) {
                var ext = getExt(att.filename);
                return ext ? ext.toUpperCase().substring(0, 4) : 'FILE';
            },

            formatSize(bytes) {
                if (bytes < 1024) return bytes + ' B';
                if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
                return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
            },

            formatDate(isoStr) {
                if (!isoStr) return '';
                var d = new Date(isoStr);
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                return months[d.getMonth()] + ' ' + d.getDate();
            },

            openLightbox(att) {
                this.lightbox = {
                    url: '/api/v1/attachments/' + att.id + '/download/',
                    name: att.filename,
                    size: this.formatSize(att.filesize)
                };
            },

            copyLink(att) {
                var isImage = IMAGE_TYPES.indexOf(att.content_type) !== -1;
                var url = '/api/v1/attachments/' + att.id + '/download/';
                var md = isImage
                    ? '![' + att.filename + '](' + url + ')'
                    : '[' + att.filename + '](' + url + ')';
                navigator.clipboard.writeText(md);
            },

            downloadFile(att) {
                window.open('/api/v1/attachments/' + att.id + '/download/', '_blank');
            },

            confirmDelete(att) {
                this.deleteTarget = att;
                this.deleting = false;
            },

            async doDelete() {
                if (!this.deleteTarget) return;
                this.deleting = true;
                try {
                    var res = await spFetch('/api/v1/attachments/' + this.deleteTarget.id + '/', {
                        method: 'DELETE'
                    });
                    if (res.ok || res.status === 204) {
                        var targetId = this.deleteTarget.id;
                        this.attachments = this.attachments.filter(function (a) {
                            return a.id !== targetId;
                        });
                        this.deleteTarget = null;
                    } else {
                        var data = {};
                        try { data = await res.json(); } catch (_e) {}
                        this.uploadError = (data.errors && data.errors[0] && data.errors[0].message) || 'Delete failed';
                        this.deleteTarget = null;
                    }
                } catch (_e) {
                    this.uploadError = 'Unable to connect.';
                    this.deleteTarget = null;
                }
                this.deleting = false;
            },

            handleDrop(event) {
                this.isDragging = false;
                var files = event.dataTransfer && event.dataTransfer.files;
                if (files && files.length) {
                    this.handleFiles(files);
                }
            },

            async handleFiles(fileList) {
                if (!fileList || !fileList.length) return;
                this.uploadError = '';
                for (var i = 0; i < fileList.length; i++) {
                    await this._uploadOne(fileList[i]);
                }
                // Reset file input so the same file can be re-selected
                if (this.$refs.fileInput) {
                    this.$refs.fileInput.value = '';
                }
            },

            async _uploadOne(file) {
                var entry = { name: file.name, progress: 0 };
                this.uploads.push(entry);
                try {
                    var formData = new FormData();
                    formData.append('file', file);
                    formData.append('container_type', this.containerType);
                    formData.append('container_id', this.containerId);

                    var res = await spFetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });

                    entry.progress = 100;
                    if (res.ok) {
                        var data = await res.json();
                        this.attachments.push(data);
                    } else {
                        var errData = {};
                        try { errData = await res.json(); } catch (_e) {}
                        this.uploadError = (errData.errors && errData.errors[0] && errData.errors[0].message)
                            || errData.message || 'Upload failed';
                    }
                } catch (_e) {
                    this.uploadError = 'Unable to connect.';
                }
                // Remove progress entry after a short delay
                var self = this;
                setTimeout(function () {
                    var idx = self.uploads.indexOf(entry);
                    if (idx !== -1) self.uploads.splice(idx, 1);
                }, 1500);
            }
        };
    }

    /**
     * Kanban board horizontal scroll with arrow navigation.
     * Wraps .kanban-board with left/right scroll buttons and fade hints.
     */
    Alpine.data('testEmailForm', function () {
        return {
            to: '',
            subject: 'Specivo test email',
            body: 'This is a test email from Specivo to verify SMTP configuration.\n\nIf you received this message, email delivery is working correctly.',
            sending: false,
            result: null,
            resultOk: false,
            async sendTest() {
                if (!this.to) return;
                this.sending = true;
                this.result = null;
                try {
                    var resp = await spFetch('/api/v1/admin/test-email/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({to: this.to, subject: this.subject, body: this.body})
                    });
                    var data = await resp.json();
                    if (data.ok) {
                        this.result = 'Test email sent to ' + this.to;
                        this.resultOk = true;
                    } else {
                        this.result = data.error || 'Unknown error';
                        this.resultOk = false;
                    }
                } catch (e) {
                    this.result = 'Request failed: ' + e.message;
                    this.resultOk = false;
                } finally {
                    this.sending = false;
                }
            }
        };
    });

    Alpine.data('sprintEdit', function (initial) {
        return {
            startDate: initial.startDate || '',
            endDate: initial.endDate || '',
            saving: false,
            saved: false,
            error: '',

            get duration() {
                if (!this.startDate || !this.endDate) return '';
                var s = new Date(this.startDate);
                var e = new Date(this.endDate);
                var days = Math.round((e - s) / (1000 * 60 * 60 * 24));
                if (days < 0) return 'Invalid range';
                var weeks = Math.floor(days / 7);
                if (weeks > 0 && days % 7 === 0) return days + ' days (' + weeks + ' week' + (weeks > 1 ? 's' : '') + ')';
                if (weeks > 0) return days + ' days (~' + weeks + ' week' + (weeks > 1 ? 's' : '') + ')';
                return days + ' day' + (days !== 1 ? 's' : '');
            },

            onSaveResponse(event) {
                var self = this;
                this.saving = false;
                if (event.detail.successful) {
                    this.saved = true;
                    setTimeout(function () { self.saved = false; }, 3000);
                } else {
                    this.error = 'Failed to save changes.';
                }
            }
        };
    });

    Alpine.data('assigneePicker', function () {
        return {
            open: false,
            search: '',

            matchesSearch(login) {
                if (!this.search) return true;
                return login.toLowerCase().indexOf(this.search.toLowerCase()) !== -1;
            }
        };
    });

    Alpine.data('backlogPage', function (initial) {
        return {
            showCreateModal: false,
            name: '',
            goal: '',
            start_date: '',
            end_date: '',
            projectKey: initial.projectKey || '',

            async createSprint() {
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/sprints/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: this.name,
                        goal: this.goal || null,
                        start_date: this.start_date || null,
                        end_date: this.end_date || null
                    })
                });
                if (res.ok) window.location.reload();
            }
        };
    });

    Alpine.data('sprintComplete', function () {
        return {
            moveToSprint: '',
            getHxVals() {
                return this.moveToSprint
                    ? JSON.stringify({move_incomplete_to_sprint_id: parseInt(this.moveToSprint)})
                    : '{}';
            }
        };
    });

    Alpine.data('kanbanScroll', function () {
        return {
            canScrollLeft: false,
            canScrollRight: false,

            init() {
                var board = this.$refs.board;
                if (!board) return;
                var self = this;
                var update = function () {
                    self.canScrollLeft = board.scrollLeft > 10;
                    self.canScrollRight = board.scrollLeft < board.scrollWidth - board.clientWidth - 10;
                    var wrap = board.closest('.kanban-wrap');
                    if (wrap) {
                        wrap.classList.toggle('has-overflow-right', self.canScrollRight);
                    }
                };
                board.addEventListener('scroll', update);
                window.addEventListener('resize', update);
                // Initial check after render
                this.$nextTick(function () { update(); });
            },

            scrollLeft() {
                var board = this.$refs.board;
                if (board) board.scrollBy({ left: -280, behavior: 'smooth' });
            },

            scrollRight() {
                var board = this.$refs.board;
                if (board) board.scrollBy({ left: 280, behavior: 'smooth' });
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

    Alpine.data('adminMetadataPresets', function (initialPresets, initialLabels) {
        var _knownIcons = ['code', 'bug', 'megaphone', 'sprint', 'book'];
        return {
            presets: initialPresets || [],
            labels: initialLabels || {},

            isKnownIcon(icon) {
                return _knownIcons.indexOf(icon) !== -1;
            },

            schemaFields(obj) {
                if (!obj || !obj.properties) return [];
                return Object.keys(obj.properties);
            },

            fieldCountLabel(obj) {
                var count = this.schemaFields(obj).length;
                return count + ' fields';
            },

            get builtinPresets() {
                return this.presets.filter(function (p) { return p.is_builtin; });
            },
            get customPresets() {
                return this.presets.filter(function (p) { return !p.is_builtin; });
            },

            showModal: false,
            showDeleteModal: false,
            editingPreset: null,
            deleteTarget: null,
            saving: false,
            schemaError: '',
            nameError: '',
            slugError: '',
            form: {
                slug: '',
                name: '',
                description: '',
                icon: 'default',
                schema_definition_raw: ''
            },

            openCreate: function () {
                this.editingPreset = null;
                this.schemaError = '';
                this.nameError = '';
                this.slugError = '';
                this.form = {
                    slug: '',
                    name: '',
                    description: '',
                    icon: 'default',
                    schema_definition_raw: JSON.stringify({ type: 'object', properties: { field_name: { type: 'string' } } }, null, 2)
                };
                this.showModal = true;
            },

            openEdit: function (preset) {
                this.editingPreset = preset;
                this.schemaError = '';
                this.nameError = '';
                this.slugError = '';
                this.form = {
                    slug: preset.slug,
                    name: preset.name,
                    description: preset.description || '',
                    icon: preset.icon,
                    schema_definition_raw: JSON.stringify(preset.schema_definition, null, 2)
                };
                this.showModal = true;
            },

            validateSchema: function () {
                if (!this.form.schema_definition_raw.trim()) {
                    this.schemaError = 'Schema definition is required.';
                    return false;
                }
                try {
                    var parsed = JSON.parse(this.form.schema_definition_raw);
                    if (parsed.type !== 'object') {
                        this.schemaError = 'Root schema type must be "object".';
                        return false;
                    }
                    this.schemaError = '';
                    return true;
                } catch (e) {
                    this.schemaError = 'Invalid JSON: ' + e.message;
                    return false;
                }
            },

            normalizeSlug: function (raw) {
                return (raw || '').trim().toLowerCase().replace(/\s+/g, '-');
            },

            validateForm: function () {
                var ok = true;
                this.nameError = '';
                this.slugError = '';
                if (!this.form.name || !this.form.name.trim()) {
                    this.nameError = this.labels.nameRequired || 'Name is required.';
                    ok = false;
                }
                // Slug is read-only for built-in presets, so only validate when editable.
                if (!(this.editingPreset && this.editingPreset.is_builtin)) {
                    var slug = this.normalizeSlug(this.form.slug);
                    if (!slug) {
                        this.slugError = this.labels.slugRequired || 'Identifier is required.';
                        ok = false;
                    } else if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
                        this.slugError = this.labels.slugInvalid || 'Use lowercase letters, numbers and dashes only.';
                        ok = false;
                    }
                }
                if (!this.validateSchema()) ok = false;
                return ok;
            },

            save: async function () {
                if (!this.validateForm()) return;
                this.saving = true;
                try {
                    var parsed = JSON.parse(this.form.schema_definition_raw);
                    var payload = {
                        name: this.form.name.trim(),
                        description: this.form.description || null,
                        icon: this.form.icon,
                        schema_definition: parsed
                    };
                    var resp;
                    if (this.editingPreset) {
                        if (!this.editingPreset.is_builtin) {
                            payload.slug = this.normalizeSlug(this.form.slug);
                        }
                        resp = await spFetch('/api/v1/admin/metadata-presets/' + this.editingPreset.slug + '/', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                    } else {
                        payload.slug = this.normalizeSlug(this.form.slug);
                        resp = await spFetch('/api/v1/admin/metadata-presets/', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                    }
                    if (!resp.ok) {
                        var err = await resp.json();
                        var msg = (err.errors && err.errors[0] && err.errors[0].message) || 'Save failed';
                        window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: msg } }));
                        return;
                    }
                    var updated = await resp.json();
                    if (this.editingPreset) {
                        var idx = this.presets.findIndex(function (p) { return p.slug === this.editingPreset.slug; }.bind(this));
                        if (idx !== -1) {
                            this.presets[idx] = updated;
                        }
                    } else {
                        this.presets.push(updated);
                    }
                    this.showModal = false;
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'success', message: this.editingPreset ? 'Preset updated' : 'Preset created' } }));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: 'Network error: ' + e.message } }));
                } finally {
                    this.saving = false;
                }
            },

            confirmDelete: function (preset) {
                this.deleteTarget = preset;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deleteTarget) return;
                try {
                    var resp = await spFetch('/api/v1/admin/metadata-presets/' + this.deleteTarget.slug + '/', {
                        method: 'DELETE'
                    });
                    if (!resp.ok) {
                        var err = await resp.json();
                        var msg = (err.errors && err.errors[0] && err.errors[0].message) || 'Delete failed';
                        window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: msg } }));
                        return;
                    }
                    var slug = this.deleteTarget.slug;
                    this.presets = this.presets.filter(function (p) { return p.slug !== slug; });
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'success', message: 'Preset deleted' } }));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: 'Network error: ' + e.message } }));
                } finally {
                    this.showDeleteModal = false;
                    this.deleteTarget = null;
                }
            }
        };
    });
});
