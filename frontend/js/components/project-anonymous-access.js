// Admin-only card on the project settings page: what visitors who are not
// signed in may read in this public project. Writes through the admin API.
export function projectAnonymousAccess(initial) {
        return {
            projectKey: initial.projectKey || '',
            permissions: initial.permissions || [],
            savedMessage: initial.savedMessage || '',
            errorMessage: initial.errorMessage || '',
            saving: false,
            message: '',

            async save() {
                this.saving = true;
                this.message = '';
                try {
                    var res = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/anonymous-permissions/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ anonymous_permissions: this.permissions })
                    });
                    var data = await res.json();
                    if (res.ok) {
                        this.permissions = data.anonymous_permissions;
                        this.message = this.savedMessage;
                    } else {
                        var detail = (data.errors && data.errors[0] && data.errors[0].message) || '';
                        this.message = detail ? this.errorMessage + ' ' + detail : this.errorMessage;
                    }
                } catch (_e) {
                    this.message = this.errorMessage;
                }
                this.saving = false;
            }
        };
    }
