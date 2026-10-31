/**
 * Project settings "Webhooks" tab: list, create, edit, enable/disable, delete,
 * send a test event, and read a webhook's delivery log.
 *
 * Secrets never reach the page except in the response that set them; that one
 * is shown once in a reveal panel. All user-visible strings come from the
 * template (`initial.i18n`, `initial.eventLabels`).
 */
export function projectWebhooks(initial) {
    var i18n = Object.assign({
        saveFailed: 'Failed to save webhook.',
        connectFailed: 'Unable to connect.',
        loadFailed: 'Could not load deliveries.',
        testQueued: 'Test event queued.',
        testFailed: 'Could not send the test event.',
        newWebhook: 'New webhook',
        editWebhook: 'Edit webhook',
        createWebhook: 'Create webhook',
        saveChanges: 'Save changes',
        copy: 'Copy',
        copied: 'Copied',
        statusPending: 'Pending',
        statusSucceeded: 'Succeeded',
        statusFailed: 'Failed'
    }, initial.i18n || {});
    var PAGE = 20;

    return {
        i18n: i18n,
        webhooks: initial.webhooks || [],
        events: initial.events || [],
        eventLabels: initial.eventLabels || {},
        projectKey: initial.projectKey || '',
        showModal: false,
        showDeleteModal: false,
        editing: null,
        deleting: null,
        saving: false,
        busy: false,
        error: '',
        notice: '',
        form: {url: '', events: [], include_private: false},
        reveal: null,
        copied: false,
        log: null,
        deliveries: [],
        total: 0,
        offset: 0,
        loadingLog: false,
        logError: '',

        get canSave() {
            return !this.saving && this.form.url.trim() !== '' && this.form.events.length > 0;
        },

        base() {
            return '/api/v1/projects/' + this.projectKey + '/webhooks/';
        },

        eventLabel(name) {
            return this.eventLabels[name] || name;
        },

        eventsText(w) {
            return w.events.map(this.eventLabel.bind(this)).join(', ');
        },

        statusLabel(s) {
            return {pending: i18n.statusPending, succeeded: i18n.statusSucceeded, failed: i18n.statusFailed}[s] || s;
        },

        statusClass(s) {
            return 'sp-webhook-status sp-webhook-status-' + s;
        },

        formatTime(iso) {
            return spFormatDate(iso, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
        },

        async _error(res, fallback) {
            var err = await res.json().catch(function () { return {}; });
            return (err.errors && err.errors[0] && err.errors[0].message) || fallback;
        },

        openCreate() {
            this.editing = null;
            this.error = '';
            this.form = {url: '', events: this.events.slice(0, 1), include_private: false};
            this.showModal = true;
        },

        openEdit(w) {
            this.editing = w;
            this.error = '';
            this.form = {url: w.url, events: w.events.slice(), include_private: w.include_private};
            this.showModal = true;
        },

        _store(saved) {
            var secret = saved.secret;
            delete saved.secret;
            var idx = this.webhooks.findIndex(function (w) { return w.id === saved.id; });
            if (idx === -1) {
                this.webhooks.unshift(saved);
            } else {
                this.webhooks[idx] = saved;
            }
            if (this.log && this.log.id === saved.id) this.log = saved;
            if (secret) {
                this.reveal = {url: saved.url, secret: secret};
                this.copied = false;
            }
        },

        async _send(url, method, payload) {
            return spFetch(url, {
                method: method,
                headers: {'Content-Type': 'application/json'},
                body: payload === undefined ? undefined : JSON.stringify(payload)
            });
        },

        async save() {
            if (!this.canSave) return;
            this.saving = true;
            this.error = '';
            var payload = {url: this.form.url.trim(), events: this.form.events, include_private: this.form.include_private};
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

        async patch(w, payload) {
            this.busy = true;
            this.notice = '';
            try {
                var res = await this._send(this.base() + w.id + '/', 'PATCH', payload);
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

        toggle(w) {
            return this.patch(w, {is_active: !w.is_active});
        },

        rotate(w) {
            return this.patch(w, {rotate_secret: true});
        },

        copySecret() {
            if (!this.reveal) return;
            navigator.clipboard.writeText(this.reveal.secret);
            this.copied = true;
            var self = this;
            setTimeout(function () { self.copied = false; }, 2000);
        },

        confirmDelete(w) {
            this.deleting = w;
            this.showDeleteModal = true;
        },

        async doDelete() {
            if (!this.deleting) return;
            var target = this.deleting;
            this.busy = true;
            try {
                var res = await spFetch(this.base() + target.id + '/', {method: 'DELETE'});
                if (res.ok || res.status === 204) {
                    this.webhooks = this.webhooks.filter(function (w) { return w.id !== target.id; });
                    if (this.log && this.log.id === target.id) this.log = null;
                    this.showDeleteModal = false;
                    this.deleting = null;
                }
            } catch (_e) {}
            this.busy = false;
        },

        async sendTest(w) {
            this.busy = true;
            this.notice = '';
            try {
                var res = await this._send(this.base() + w.id + '/test/', 'POST');
                this.notice = res.ok ? i18n.testQueued : await this._error(res, i18n.testFailed);
                if (res.ok && this.log && this.log.id === w.id) await this.loadLog(0);
            } catch (_e) {
                this.notice = i18n.connectFailed;
            }
            this.busy = false;
        },

        async openLog(w) {
            if (this.log && this.log.id === w.id) {
                this.log = null;
                return;
            }
            this.log = w;
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
