import { avatarIconClass, avatarInitials } from '../lib/avatar';
import { userCreateForm } from '../lib/user-create-form';

export function adminUsers(initial) {
        var i18n = Object.assign({
            passwordRequired: 'Password: required for regular users.',
            createFailed: 'Failed to create user.',
            passwordTooShort: 'Password must be at least 10 characters.',
            passwordReset: 'Password reset successfully.',
            resetFailed: 'Failed to reset password.',
            confirmLock: 'Lock user %(login)s?',
            confirmUnlock: 'Unlock user %(login)s?',
            lockFailed: 'Failed to lock user.',
            unlockFailed: 'Failed to unlock user.',
            never: 'Never',
            justNow: 'Just now',
            minutesAgo: '%(count)s min ago',
            hoursAgo: '%(count)s hours ago',
            yesterday: 'Yesterday',
            daysAgo: '%(count)s days ago',
            roleAdmin: 'Administrator',
            roleService: 'Service account',
            roleMember: 'Member'
        }, initial.i18n || {});
        var statusLabels = initial.statusLabels || {};

        var component = {
            i18n: i18n,
            users: initial.users || [],
            roles: initial.roles || [],

            initials(name) {
                return avatarInitials(name);
            },

            /* A bundled icon for a built-in system account (the Git account), else no extra class. */
            avatarIconClass(login) {
                return avatarIconClass(login);
            },

            capitalize(s) {
                return s.charAt(0).toUpperCase() + s.slice(1);
            },

            /* Translated label for a stored account status; capitalised raw value as a fallback. */
            statusLabel(s) {
                return statusLabels[s] || this.capitalize(s || '');
            },

            // The create-user modal comes from userCreateForm (merged below);
            // here a new account just reloads the list.
            userCreated() {
                window.location.reload();
            },

            // Reset password
            showReset: false,
            resetUser: null,
            resetPassword: '',
            resetMustChange: true,
            resetting: false,
            resetError: '',
            resetSuccess: '',

            openResetPassword(u) {
                this.resetUser = u;
                this.resetPassword = '';
                // Same default as creation, and off for a service account,
                // which the server refuses to flag.
                this.resetMustChange = !u.is_service_account;
                this.resetError = '';
                this.resetSuccess = '';
                this.showReset = true;
            },

            async doResetPassword() {
                if (!this.resetUser || this.resetPassword.length < 10) {
                    this.resetError = i18n.passwordTooShort;
                    return;
                }
                this.resetting = true;
                this.resetError = '';
                this.resetSuccess = '';
                var res = await spFetch('/api/v1/admin/users/' + this.resetUser.id + '/reset-password/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        password: this.resetPassword,
                        must_change_password: this.resetUser.is_service_account ? false : this.resetMustChange
                    })
                });
                if (res.ok) {
                    this.resetSuccess = i18n.passwordReset;
                    this.resetPassword = '';
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.resetError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.resetFailed;
                }
                this.resetting = false;
            },

            async toggleLock(u) {
                var action = u.status === 'locked' ? 'unlock' : 'lock';
                var question = action === 'lock' ? i18n.confirmLock : i18n.confirmUnlock;
                if (!confirm(question.replace('%(login)s', u.login))) return;
                var res = await spFetch('/api/v1/admin/users/' + u.id + '/' + action + '/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok) {
                    var updated = await res.json();
                    u.status = updated.status;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    var fallback = action === 'lock' ? i18n.lockFailed : i18n.unlockFailed;
                    alert((err.errors && err.errors[0] && err.errors[0].message) || err.detail || fallback);
                }
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
                return spFormatDate(d);
            }
        };
        return Object.assign(component, userCreateForm(i18n));
    }
