export function projectCreateModal(initial) {
        return {
            name: '',
            identifier: '',
            key: '',
            description: '',
            parentKey: (initial && initial.parentKey) || '',
            color: (initial && initial.colors && initial.colors[0]) || '#c49a3c',
            colors: (initial && initial.colors) || [],
            allProjects: (initial && initial.allProjects) || [],
            moduleWiki: true,
            moduleTime: true,
            saving: false,
            error: '',

            slugify: function (v) {
                return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 50);
            },

            keyify: function (v) {
                return v.toUpperCase().replace(/[^A-Z0-9]/g, '').substring(0, 8);
            },

            onNameInput: function () {
                this.identifier = this.slugify(this.name);
                this.key = this.keyify(this.name);
            },

            async submit() {
                this.saving = true;
                this.error = '';
                var modules = ['issue_tracking'];
                if (this.moduleWiki) modules.push('wiki');
                if (this.moduleTime) modules.push('time_tracking');
                try {
                    var res = await spFetch('/api/v1/projects/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            name: this.name,
                            identifier: this.identifier,
                            key: this.key,
                            description: this.description || null,
                            parent_key: this.parentKey || null,
                            color: this.color || null,
                            modules: modules
                        })
                    });
                    if (res.ok) {
                        var data = await res.json();
                        var url = '/projects/' + data.key + '/';
                        for (var i = 0; i < 5; i++) {
                            var check = await spFetch(url, {method: 'HEAD'});
                            if (check.ok) break;
                            await new Promise(function (r) { setTimeout(r, 300); });
                        }
                        window.location.href = url;
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message)
                            || (errData.detail && errData.detail[0] && errData.detail[0].msg)
                            || errData.detail
                            || 'Failed to create project';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    }
