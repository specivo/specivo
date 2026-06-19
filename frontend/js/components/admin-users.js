export function adminUsers(initial) {
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
    }
