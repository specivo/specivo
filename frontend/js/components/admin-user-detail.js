export function adminUserDetail(initial) {
        var i18n = Object.assign({
            createKeyFailed: 'Failed to create key',
            connectFailed: 'Unable to connect. Please try again.',
            confirmRevoke: 'Revoke this API key? This cannot be undone.',
            never: 'Never',
            justNow: 'Just now',
            minutesAgo: '%(count)s min ago',
            hoursAgo: '%(count)s hours ago',
            yesterday: 'Yesterday',
            daysAgo: '%(count)s days ago'
        }, initial.i18n || {});

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
                        this.createError = (errData.errors && errData.errors[0] && errData.errors[0].message) || errData.detail || i18n.createKeyFailed;
                    }
                } catch (_e) {
                    this.createError = i18n.connectFailed;
                }
                this.creating = false;
            },

            async revokeKey(id) {
                if (!confirm(i18n.confirmRevoke)) return;
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
                if (!iso) return i18n.never;
                var d = new Date(iso);
                var now = new Date();
                var diff = Math.floor((now - d) / 1000);
                if (diff < 60) return i18n.justNow;
                if (diff < 3600) return i18n.minutesAgo.replace('%(count)s', Math.floor(diff / 60));
                if (diff < 86400) return i18n.hoursAgo.replace('%(count)s', Math.floor(diff / 3600));
                if (diff < 172800) return i18n.yesterday;
                if (diff < 604800) return i18n.daysAgo.replace('%(count)s', Math.floor(diff / 86400));
                return d.toLocaleDateString();
            }
        };
    }
