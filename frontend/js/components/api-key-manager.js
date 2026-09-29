export function apiKeyManager() {
        return {
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

            init() {
                // Translated strings arrive as data-msg-* attributes rendered by
                // the template; the English defaults cover a template without them.
                var d = this.$el.dataset;
                this.msgCreateFailed = d.msgCreateFailed || 'Failed to create key';
                this.msgConnectFailed = d.msgConnectFailed || 'Unable to connect. Please try again.';
                this.msgConfirmDelete = d.msgConfirmDelete || 'Are you sure you want to permanently delete this API key?';
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
                if (!iso) return '-';
                var d = new Date(iso);
                return d.toLocaleDateString('en-US', {year: 'numeric', month: 'short', day: 'numeric'});
            }
        };
    }
