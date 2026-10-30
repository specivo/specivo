/**
 * Project settings "Repositories" tab: connect forge repositories (GitHub,
 * GitLab, Gitea, Forgejo), show the hook URL and secret once with setup hints,
 * rotate the secret, enable/disable, edit, delete, and read the delivery log.
 *
 * The secret reaches the page only in the response that set it (create and
 * rotate-secret) and is shown once in a reveal panel. All user-visible strings
 * come from the template (`initial.i18n`, `initial.providerLabels`).
 *
 * The form also holds what the repository may change on issues: commit
 * commands (fixing status, time logging) and merge automation with the role
 * `$git` holds. Picking a role other than the Automation role needs
 * `manage_members` (`initial.canPickRole`). `ignore_external_pull_requests`
 * (on unless the stored settings say false) skips pull requests from forks
 * and outsiders until they are merged.
 */
export function projectRepositories(initial) {
    var i18n = Object.assign({
        saveFailed: 'Failed to save the repository.',
        connectFailed: 'Unable to connect.',
        loadFailed: 'Could not load deliveries.',
        newRepository: 'Connect a repository',
        editRepository: 'Edit repository',
        createRepository: 'Connect repository',
        saveChanges: 'Save changes',
        copy: 'Copy',
        copied: 'Copied',
        neverReceived: 'Never received',
        statusAccepted: 'Accepted',
        statusDuplicate: 'Duplicate',
        statusIgnored: 'Ignored',
        statusProcessed: 'Processed',
        statusFailed: 'Failed'
    }, initial.i18n || {});
    var PAGE = 20;
    var defaultRoleId = initial.defaultRoleId || null;

    function blankForm(provider) {
        return {
            provider: provider,
            repo_full_name: '',
            repo_url: '',
            scope: 'project',
            commands_enabled: false,
            time_logging_enabled: false,
            fixing_status_id: '',
            merge_automation_enabled: false,
            merge_status_id: '',
            ignore_external_pull_requests: true,
            automation_role_id: defaultRoleId === null ? '' : String(defaultRoleId)
        };
    }

    function idOrNull(value) {
        return value === '' || value === null || value === undefined ? null : parseInt(value, 10);
    }

    return {
        i18n: i18n,
        repositories: initial.repositories || [],
        providers: initial.providers || [],
        providerLabels: initial.providerLabels || {},
        projectKey: initial.projectKey || '',
        // Option values are strings, as <select> gives them back.
        statuses: (initial.statuses || []).map(function (st) { return {id: st.id, value: String(st.id), name: st.name}; }),
        roles: (initial.roles || []).map(function (r) { return {id: r.id, value: String(r.id), name: r.name}; }),
        canPickRole: initial.canPickRole === true,
        showModal: false,
        showDeleteModal: false,
        editing: null,
        deleting: null,
        saving: false,
        busy: false,
        error: '',
        notice: '',
        form: blankForm('github'),
        reveal: null,
        copied: '',
        log: null,
        deliveries: [],
        total: 0,
        offset: 0,
        loadingLog: false,
        logError: '',

        get canSave() {
            if (this.saving || this.form.repo_full_name.trim() === '' || this.form.repo_url.trim() === '') return false;
            return !(this.form.merge_automation_enabled && this.form.merge_status_id === '');
        },

        statusName(id) {
            var found = this.statuses.find(function (s) { return s.id === id; });
            return found ? found.name : '';
        },

        roleName(id) {
            var found = this.roles.find(function (r) { return r.id === id; });
            return found ? found.name : '';
        },

        onCommandsChange() {
            if (!this.form.commands_enabled) this.form.time_logging_enabled = false;
        },

        base() {
            return '/api/v1/projects/' + this.projectKey + '/repositories/';
        },

        providerLabel(name) {
            return this.providerLabels[name] || name;
        },

        formatTime(iso) {
            return spFormatDate(iso, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
        },

        lastDelivery(r) {
            return r.last_delivery_at ? this.formatTime(r.last_delivery_at) : i18n.neverReceived;
        },

        statusLabel(s) {
            return {
                accepted: i18n.statusAccepted,
                duplicate: i18n.statusDuplicate,
                ignored: i18n.statusIgnored,
                processed: i18n.statusProcessed,
                failed: i18n.statusFailed
            }[s] || s;
        },

        statusClass(s) {
            return 'sp-repo-status sp-repo-status-' + s;
        },

        async _error(res, fallback) {
            var err = await res.json().catch(function () { return {}; });
            return (err.errors && err.errors[0] && err.errors[0].message) || fallback;
        },

        async _send(url, method, payload) {
            return spFetch(url, {
                method: method,
                headers: {'Content-Type': 'application/json'},
                body: payload === undefined ? undefined : JSON.stringify(payload)
            });
        },

        openCreate() {
            this.editing = null;
            this.error = '';
            this.form = blankForm(this.providers[0] || 'github');
            this.showModal = true;
        },

        openEdit(r) {
            this.editing = r;
            this.error = '';
            var settings = r.settings || {};
            var form = blankForm(r.provider);
            form.repo_full_name = r.repo_full_name;
            form.repo_url = r.repo_url;
            form.scope = r.scope;
            form.commands_enabled = settings.commands_enabled === true;
            form.time_logging_enabled = settings.time_logging_enabled === true;
            form.fixing_status_id = settings.fixing_status_id ? String(settings.fixing_status_id) : '';
            form.merge_automation_enabled = settings.merge_automation_enabled === true;
            form.merge_status_id = settings.merge_status_id ? String(settings.merge_status_id) : '';
            form.ignore_external_pull_requests = settings.ignore_external_pull_requests !== false;
            if (r.automation_role_id) form.automation_role_id = String(r.automation_role_id);
            this.form = form;
            this.showModal = true;
        },

        _store(saved) {
            var secret = saved.secret;
            delete saved.secret;
            var idx = this.repositories.findIndex(function (r) { return r.id === saved.id; });
            if (idx === -1) {
                this.repositories.unshift(saved);
            } else {
                this.repositories[idx] = saved;
            }
            // $git holds one role per project: every integration shows it.
            this.repositories.forEach(function (r) {
                if (r.settings && r.settings.merge_automation_enabled) r.automation_role_id = saved.automation_role_id;
            });
            if (this.log && this.log.id === saved.id) this.log = saved;
            if (secret) {
                this.reveal = {
                    provider: saved.provider,
                    repo: saved.repo_full_name,
                    hook_url: saved.hook_url,
                    secret: secret
                };
                this.copied = '';
            }
        },

        async save() {
            if (!this.canSave) return;
            this.saving = true;
            this.error = '';
            var f = this.form;
            var payload = {
                repo_full_name: f.repo_full_name.trim(),
                repo_url: f.repo_url.trim(),
                scope: f.scope,
                settings: {
                    commands_enabled: f.commands_enabled,
                    time_logging_enabled: f.commands_enabled && f.time_logging_enabled,
                    fixing_status_id: idOrNull(f.fixing_status_id),
                    merge_automation_enabled: f.merge_automation_enabled,
                    merge_status_id: idOrNull(f.merge_status_id),
                    ignore_external_pull_requests: f.ignore_external_pull_requests
                }
            };
            if (f.merge_automation_enabled && this.canPickRole && idOrNull(f.automation_role_id) !== null) {
                payload.automation_role_id = idOrNull(f.automation_role_id);
            }
            if (!this.editing) payload.provider = f.provider;
            var url = this.editing ? this.base() + this.editing.id + '/' : this.base();
            try {
                var res = await this._send(url, this.editing ? 'PATCH' : 'POST', payload);
                if (res.ok) {
                    this._store(await res.json());
                    this.showModal = false;
                } else {
                    this.error = await this._error(res, i18n.saveFailed);
                }
            } catch (_e) {
                this.error = i18n.connectFailed;
            }
            this.saving = false;
        },

        async _action(url, method, payload) {
            this.busy = true;
            this.notice = '';
            try {
                var res = await this._send(url, method, payload);
                if (res.ok) {
                    this._store(await res.json());
                } else {
                    this.notice = await this._error(res, i18n.saveFailed);
                }
            } catch (_e) {
                this.notice = i18n.connectFailed;
            }
            this.busy = false;
        },

        toggle(r) {
            return this._action(this.base() + r.id + '/', 'PATCH', {is_active: !r.is_active});
        },

        rotate(r) {
            return this._action(this.base() + r.id + '/rotate-secret/', 'POST');
        },

        copy(field) {
            if (!this.reveal) return;
            navigator.clipboard.writeText(this.reveal[field]);
            this.copied = field;
            var self = this;
            setTimeout(function () { if (self.copied === field) self.copied = ''; }, 2000);
        },

        confirmDelete(r) {
            this.deleting = r;
            this.showDeleteModal = true;
        },

        async doDelete() {
            if (!this.deleting) return;
            var target = this.deleting;
            this.busy = true;
            try {
                var res = await spFetch(this.base() + target.id + '/', {method: 'DELETE'});
                if (res.ok || res.status === 204) {
                    this.repositories = this.repositories.filter(function (r) { return r.id !== target.id; });
                    if (this.log && this.log.id === target.id) this.log = null;
                    if (this.reveal && this.reveal.hook_url === target.hook_url) this.reveal = null;
                    this.showDeleteModal = false;
                    this.deleting = null;
                }
            } catch (_e) {}
            this.busy = false;
        },

        async openLog(r) {
            if (this.log && this.log.id === r.id) {
                this.log = null;
                return;
            }
            this.log = r;
            await this.loadLog(0);
        },

        async loadLog(offset) {
            if (!this.log) return;
            this.loadingLog = true;
            this.logError = '';
            try {
                var res = await spFetch(this.base() + this.log.id + '/deliveries/?limit=' + PAGE + '&offset=' + offset);
                if (res.ok) {
                    var page = await res.json();
                    this.deliveries = page.items;
                    this.total = page.total_count;
                    this.offset = page.offset;
                } else {
                    this.logError = await this._error(res, i18n.loadFailed);
                }
            } catch (_e) {
                this.logError = i18n.connectFailed;
            }
            this.loadingLog = false;
        },

        get hasNewer() {
            return this.offset > 0;
        },

        get hasOlder() {
            return this.offset + PAGE < this.total;
        },

        newer() {
            return this.loadLog(Math.max(0, this.offset - PAGE));
        },

        older() {
            return this.loadLog(this.offset + PAGE);
        }
    };
}
