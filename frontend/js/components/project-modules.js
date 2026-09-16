export function projectModules(initial) {
        var i18n = Object.assign({
            updated: 'Module updated.',
            connectFailed: 'Unable to connect.'
        }, initial.i18n || {});

        return {
            modules: initial.modules || {},
            projectKey: initial.projectKey || '',
            saving: false,
            message: '',

            async toggleModule(name) {
                this.saving = true;
                this.message = '';
                var payload = {};
                payload[name] = !this.modules[name];
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/modules/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({modules: payload})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this.modules = data.modules;
                        this.message = i18n.updated;
                    }
                } catch (_e) {
                    this.message = i18n.connectFailed;
                }
                this.saving = false;
            }
        };
    }
