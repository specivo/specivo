export function projectGeneralSettings(initial) {
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
                        this.message = 'Saved successfully.';
                    } else {
                        var data = await res.json();
                        this.message = 'Error: ' + ((data.errors && data.errors[0] && data.errors[0].message) || 'Failed to save');
                    }
                } catch (_e) {
                    this.message = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    }
