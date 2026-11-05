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
 *
 * Validation is shown on the fields: a field's message appears once it has
 * been left (blur/change) or after a submit attempt, never on a pristine
 * form. Save stays enabled; a click with errors marks every field, focuses
 * the first one that failed and sends nothing. A 422/409 from the server
 * that names a `field` is shown on that field the same way, until its value
 * changes.
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
        statusFailed: 'Failed',
        errRepoName: 'Enter the repository as owner/name.',
        errRepoUrl: "Enter the repository's http(s) address.",
        errMergeStatus: 'Choose a merge status, or turn merge automation off.'
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

    // Mirrors the server's checks in specivo/schemas/repository.py.
    var REPO_NAME = /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+$/;
    var REPO_URL = /^https?:\/\/[^\s\/?#]+/i;

    // Fields in the order they appear in the form; the first failed one gets focus.
    var FIELDS = [
        {name: 'repo_full_name', id: 'repository-name'},
        {name: 'repo_url', id: 'repository-url'},
        {name: 'merge_status_id', id: 'repository-merge-status'}
    ];
    // Server error `field` values and the form field they belong to.
    var SERVER_FIELDS = {
        repo_full_name: 'repo_full_name',
        repo_url: 'repo_url',
        'settings.merge_status_id': 'merge_status_id'
    };

    function validName(value) {
        var v = value.trim().replace(/^\/+|\/+$/g, '');
        return REPO_NAME.test(v) && v.split('/').every(function (p) { return p !== '.' && p !== '..'; });
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
        // Fields the person has left, a submit attempt, and the server's field errors.
        touched: {},
        submitted: false,
        serverErrors: {},
        reveal: null,
        copied: '',
        log: null,
        deliveries: [],
        total: 0,
        offset: 0,
        loadingLog: false,
        logError: '',

        /** What is wrong with the form now, field by field, whether shown yet or not. */
        get clientErrors() {
            var f = this.form;
            var errors = {};
            if (!validName(f.repo_full_name)) errors.repo_full_name = i18n.errRepoName;
            if (!REPO_URL.test(f.repo_url.trim())) errors.repo_url = i18n.errRepoUrl;
            if (f.merge_automation_enabled && f.merge_status_id === '') errors.merge_status_id = i18n.errMergeStatus;
            return errors;
        },

        /** The message shown under *field*, or '' while it is valid or still pristine. */
        errorFor(field) {
            var message = this.clientErrors[field];
            if (message && (this.touched[field] || this.submitted)) return message;
            var server = this.serverErrors[field];
            // A server error stands until the value it was about changes.
            if (server && String(this.form[field]) === server.value) return server.message;
            return '';
        },

        /** `aria-invalid` for *field*: "true" while its error shows, else no attribute. */
        invalid(field) {
            return this.errorFor(field) ? 'true' : null;
        },

        /** `aria-describedby` for *field*: its hint (if any) and its message while one shows. */
        describedBy(field, hintId) {
            var ids = hintId ? [hintId] : [];
            if (this.errorFor(field)) ids.push(this.fieldId(field) + '-error');
            return ids.length ? ids.join(' ') : null;
        },

        fieldId(field) {
            var found = FIELDS.find(function (x) { return x.name === field; });
            return found ? found.id : field;
        },

        touch(field) {
            this.touched[field] = true;
        },

        _resetValidation() {
            this.touched = {};
            this.submitted = false;
            this.serverErrors = {};
        },

        /** Move to the first field showing an error: scroll it into view and focus it. */
        _focusFirstError() {
            var self = this;
            this.$nextTick(function () {
                var first = FIELDS.find(function (x) { return self.errorFor(x.name); });
                var el = first ? document.getElementById(first.id) : null;
                if (!el) return;
                el.scrollIntoView({block: 'center'});
                el.focus({preventScroll: true});
            });
        },

        /** Put the field errors of a 422/409 body on their fields; true when at least one landed. */
        _serverFieldErrors(body) {
            var found = false;
            var self = this;
            (body.errors || []).forEach(function (err) {
                var field = SERVER_FIELDS[err.field];
                if (!field || !err.message) return;
                self.serverErrors[field] = {
                    message: String(err.message).replace(/^Value error, /, ''),
                    value: String(self.form[field])
                };
                found = true;
            });
            return found;
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
            this._resetValidation();
            this.form = blankForm(this.providers[0] || 'github');
            this.showModal = true;
        },

        openEdit(r) {
            this.editing = r;
            this.error = '';
            this._resetValidation();
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
            if (this.saving) return;
            this.submitted = true;
            this.serverErrors = {};
            if (Object.keys(this.clientErrors).length > 0) {
                this._focusFirstError();
                return;
            }
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
                    var body = await res.json().catch(function () { return {}; });
                    if (this._serverFieldErrors(body)) {
                        this._focusFirstError();
                    } else {
                        this.error = (body.errors && body.errors[0] && body.errors[0].message) || i18n.saveFailed;
                    }
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
