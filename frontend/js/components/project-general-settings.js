export function projectGeneralSettings(initial) {
        var i18n = Object.assign({
            saved: 'Saved successfully.',
            errorWithMessage: 'Error: %(message)s',
            saveFailed: 'Failed to save',
            connectFailed: 'Unable to connect.'
        }, initial.i18n || {});

        return {
            name: initial.name || '',
            description: initial.description || '',
            projectKey: initial.projectKey || '',
            parentId: initial.parentId !== undefined ? initial.parentId : null,
            availableParents: initial.availableParents || [],
            saving: false,
            message: '',

            async save() {
                this.saving = true;
                this.message = '';
                try {
                    var payload = {
                        name: this.name,
                        description: this.description,
                        parent_id: this.parentId
                    };
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        this.message = i18n.saved;
                    } else {
                        var data = await res.json();
                        var detail = (data.errors && data.errors[0] && data.errors[0].message) || i18n.saveFailed;
                        this.message = i18n.errorWithMessage.replace('%(message)s', detail);
                    }
                } catch (_e) {
                    this.message = i18n.connectFailed;
                }
                this.saving = false;
            }
        };
    }
