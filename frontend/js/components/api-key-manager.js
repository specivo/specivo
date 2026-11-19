import { apiKeyProjectPicker } from '../lib/api-key-project-picker';

/* The component's messages: property (and data-* attribute) name, English default. */
var MESSAGES = [
    ['msgCreateFailed', 'Failed to create key'],
    ['msgConnectFailed', 'Unable to connect. Please try again.'],
    ['msgConfirmDelete', 'Are you sure you want to permanently delete this API key?'],
    ['msgCopied', 'Copied!'],
    ['msgCopy', 'Copy'],
    ['msgCreating', 'Creating...'],
    ['msgCreate', 'Create key'],
    ['msgActive', 'Active'],
    ['msgInactive', 'Inactive'],
    ['msgDeactivate', 'Deactivate'],
    ['msgActivate', 'Activate']
];

/* The project limit picker's messages from the data-* attributes *d*. */
function limitMessages(d) {
    return {
        allHint: d.msgLimitAllHint || 'Nothing ticked: the key reaches every project you can see.',
        all: d.msgLimitAll || 'All projects',
        limitedTo: d.msgLimitedTo || 'Limited to: %(projects)s',
        none: d.msgLimitNone || 'None: its projects were deleted'
    };
}

export function apiKeyManager() {
        var component = {
            keys: [],
            showCreate: false,
            newKeyName: '',
            newKey: null,
            loading: false,
            error: '',
            copied: false,
            msgCreateFailed: '',
            msgConnectFailed: '',
            msgConfirmDelete: '',
            msgCopied: '',
            msgCopy: '',
            msgCreating: '',
            msgCreate: '',
            msgActive: '',
            msgInactive: '',
            msgDeactivate: '',
            msgActivate: '',

            init() {
                // Translated strings arrive as data-msg-* attributes rendered by
                // the template; the English defaults cover a template without them.
                var d = this.$el.dataset;
                for (var i = 0; i < MESSAGES.length; i++) {
                    this[MESSAGES[i][0]] = d[MESSAGES[i][0]] || MESSAGES[i][1];
                }
                this.limitI18n = limitMessages(d);
                this.loadKeys();
            },

            async loadKeys() {
                var res = await spFetch('/api/v1/my/api-keys/');
                if (res.ok) {
                    this.keys = await res.json();
                }
            },

            toggleCreate() {
                this.showCreate = !this.showCreate;
                this.newKey = null;
                if (this.showCreate) this.loadLimitProjects();
            },

            async createKey() {
                if (!this.newKeyName.trim()) return;
                this.loading = true;
                this.error = '';
                var body = {name: this.newKeyName.trim()};
                var scopes = this.limitScopes();
                if (scopes) body.scopes = scopes;
                try {
                    var res = await spFetch('/api/v1/my/api-keys/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body)
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this.newKey = data.raw_key;
                        this.newKeyName = '';
                        this.resetLimit();
                        await this.loadKeys();
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message) || this.msgCreateFailed;
                    }
                } catch (_e) {
                    this.error = this.msgConnectFailed;
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
                if (!confirm(this.msgConfirmDelete)) return;
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
                return spFormatDate(iso) || '-';
            }
        };
        return Object.assign(component, apiKeyProjectPicker());
    }
