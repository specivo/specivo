/* Admin > Roles > New role.
 *
 * A name and a starting point: a blank role, or a copy of an existing one
 * (permissions, visibility and workflow transitions). The role is created
 * through the API and the editor opens on it, where the permissions are set.
 */
export function adminRoleNew(initial) {
    var i18n = Object.assign({
        createFailed: 'Failed to create the role.'
    }, initial.i18n || {});

    return {
        name: '',
        source: '',
        saving: false,
        error: '',
        nameError: '',

        get canSubmit() {
            return this.name.trim().length > 0;
        },

        get nameInvalid() {
            return this.nameError !== '';
        },

        async submit() {
            if (this.saving || !this.canSubmit) return;
            this.saving = true;
            this.error = '';
            this.nameError = '';
            var body = { name: this.name.trim() };
            if (this.source !== '') body.copy_from_role_id = parseInt(this.source, 10);
            try {
                var res = await spFetch('/api/v1/admin/roles/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (res.ok) {
                    var role = await res.json();
                    window.location.href = spUrl('/admin/roles/' + role.id + '/');
                    return;
                }
                var err = await res.json().catch(function () { return {}; });
                var first = err.errors && err.errors[0];
                var message = (first && first.message) || i18n.createFailed;
                if (first && first.field === 'name') {
                    this.nameError = message;
                } else {
                    this.error = message;
                }
            } catch (_e) {
                this.error = i18n.createFailed;
            }
            this.saving = false;
        }
    };
}
