export function adminUserDetail(initial) {
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
    }
