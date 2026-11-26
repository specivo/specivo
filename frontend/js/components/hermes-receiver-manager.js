/* Hermes event receivers of one account (pages/my/_hermes_receivers_panel.html).
 * The JSON API base comes from data-api: /api/v1/my/hermes-receivers/ on the
 * person's own page, /api/v1/admin/users/{id}/hermes-receivers/ on Admin > Users.
 * A new receiver's URL, secret and hooks.outbound block, and a rotated secret,
 * are kept only in memory until dismissed: the server never answers them again. */

/* The component's messages: property (and data-* attribute) name, English default. */
var MESSAGES = [
    ['msgCreateFailed', 'Failed to create the receiver'],
    ['msgConnectFailed', 'Unable to connect. Please try again.'],
    ['msgConfirmDelete', 'Delete this receiver? Hermes events sent to it will be refused.'],
    ['msgConfirmRotate', 'Rotate the secret? The current secret stops working at once.'],
    ['msgCopied', 'Copied!'],
    ['msgCopy', 'Copy'],
    ['msgCreating', 'Creating...'],
    ['msgCreate', 'Create receiver'],
    ['msgActive', 'Active'],
    ['msgInactive', 'Inactive'],
    ['msgDeactivate', 'Deactivate'],
    ['msgActivate', 'Activate'],
    ['msgNever', 'Never']
];

var JSON_HEADERS = {'Content-Type': 'application/json'};

function errorMessage(data, fallback) {
    return (data && data.errors && data.errors[0] && data.errors[0].message) || fallback;
}

export function hermesReceiverManager() {
    var component = {
        api: '',
        receivers: [],
        newName: '',
        revealed: null,
        copiedField: '',
        loading: false,
        error: '',

        init() {
            var d = this.$el.dataset;
            for (var i = 0; i < MESSAGES.length; i++) {
                this[MESSAGES[i][0]] = d[MESSAGES[i][0]] || MESSAGES[i][1];
            }
            this.api = d.api || spUrl('/api/v1/my/hermes-receivers/');
            this.load();
        },

        async load() {
            var res = await spFetch(this.api);
            if (res.ok) this.receivers = await res.json();
        },

        async create() {
            var name = this.newName.trim();
            if (!name) return;
            this.loading = true;
            this.error = '';
            try {
                var res = await spFetch(this.api, {
                    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({name: name})
                });
                var data = await res.json();
                if (res.ok) {
                    this.revealed = {
                        url: data.url, secret: data.secret, secretEnv: data.secret_env, hooksConfig: data.hooks_config
                    };
                    this.newName = '';
                    await this.load();
                } else {
                    this.error = errorMessage(data, this.msgCreateFailed);
                }
            } catch (_e) {
                this.error = this.msgConnectFailed;
            }
            this.loading = false;
        },

        async patch(receiver, body) {
            this.error = '';
            var res = await spFetch(this.api + receiver.id + '/', {
                method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(body)
            });
            var data = await res.json();
            if (!res.ok) {
                this.error = errorMessage(data, this.msgConnectFailed);
                return null;
            }
            await this.load();
            return data;
        },

        async toggle(receiver) {
            await this.patch(receiver, {is_active: !receiver.is_active});
        },

        async rotate(receiver) {
            if (!confirm(this.msgConfirmRotate)) return;
            var data = await this.patch(receiver, {rotate_secret: true});
            if (data && data.secret) {
                this.revealed = {url: '', secret: data.secret, secretEnv: data.secret_env, hooksConfig: ''};
            }
        },

        async remove(receiver) {
            if (!confirm(this.msgConfirmDelete)) return;
            var res = await spFetch(this.api + receiver.id + '/', {method: 'DELETE', headers: JSON_HEADERS});
            if (res.ok || res.status === 204) await this.load();
        },

        copy(field) {
            if (!this.revealed || !this.revealed[field]) return;
            navigator.clipboard.writeText(this.revealed[field]);
            this.copiedField = field;
            var self = this;
            setTimeout(function () { self.copiedField = ''; }, 2000);
        },

        dismiss() {
            this.revealed = null;
        },

        formatDate(iso) {
            return spFormatDate(iso) || '-';
        }
    };
    for (var i = 0; i < MESSAGES.length; i++) component[MESSAGES[i][0]] = '';
    return component;
}
