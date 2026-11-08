/* The "New User" form, shared by Admin > Users and the project Members tab.
 *
 * Both pages render the same modal (pages/admin/_user_create_modal.html) and
 * mix this object into their Alpine component, so the rules for what a new
 * account may look like live in one place. What happens after the account
 * exists differs: the host component defines `userCreated(user)`.
 *
 * Creating a user is admin-only on the server; a page offers this form only
 * to administrators.
 */

function emptyUser(displayName) {
    // must_change_password defaults on: an administrator typing somebody
    // else's password knows it, so it must not stay theirs.
    return {
        login: '',
        email: '',
        display_name: displayName || '',
        password: '',
        is_admin: false,
        is_service_account: false,
        must_change_password: true
    };
}

/**
 * State and methods for the create-user modal, to merge into an Alpine
 * component with Object.assign. `i18n` supplies `passwordRequired` and
 * `createFailed`.
 */
export function userCreateForm(i18n) {
    return {
        showCreate: false,
        creating: false,
        createError: '',
        newUser: emptyUser(),

        /** Open the form empty, or with a display name already typed elsewhere. */
        openCreateUser(displayName) {
            this.newUser = emptyUser(displayName);
            this.createError = '';
            this.showCreate = true;
        },

        generatePassword() {
            var chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%&*';
            var arr = new Uint8Array(16);
            crypto.getRandomValues(arr);
            return Array.from(arr, function (b) { return chars[b % chars.length]; }).join('');
        },

        async createUser() {
            this.creating = true;
            this.createError = '';
            var payload = Object.assign({}, this.newUser);
            // The role select binds a string; the API wants a boolean.
            payload.is_admin = payload.is_admin === true || payload.is_admin === 'true';
            // Service accounts don't need a password
            if (payload.is_service_account && !payload.password) {
                delete payload.password;
            }
            // A service account has no password to change and the server
            // refuses the combination outright, so never send it.
            if (payload.is_service_account) {
                payload.must_change_password = false;
            }
            if (!payload.password && !payload.is_service_account) {
                this.createError = i18n.passwordRequired;
                this.creating = false;
                return;
            }
            var res = await spFetch('/api/v1/admin/users/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                var user = await res.json();
                this.creating = false;
                this.showCreate = false;
                this.userCreated(user);
                return;
            }
            var err = await res.json().catch(function () { return {}; });
            if (err.errors && err.errors.length > 0) {
                this.createError = err.errors.map(function (e) {
                    var field = e.field ? e.field + ': ' : '';
                    return field + e.message;
                }).join('\n');
            } else {
                this.createError = err.detail || i18n.createFailed;
            }
            this.creating = false;
        }
    };
}
