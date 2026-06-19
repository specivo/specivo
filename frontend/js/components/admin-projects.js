export function adminProjects(initial) {
        return {
            projects: (initial && initial.projects) || [],
            colors: (initial && initial.colors) || [],
            allProjects: (initial && initial.allProjects) || [],
            showModal: false,
            editMode: false,
            editHasIssues: false,
            saving: false,
            formError: '',
            form: { name: '', identifier: '', key: '', description: '', color: '', parentKey: '', moduleWiki: true, moduleTime: true },

            formatDate: function (iso) {
                if (!iso) return '-';
                var d = new Date(iso);
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                return months[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
            },

            get parentOptions() {
                var self = this;
                return this.allProjects.filter(function (p) {
                    return !self.editMode || p.key !== self.form.key;
                });
            },

            slugify: function (v) {
                return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 50);
            },

            keyify: function (v) {
                return v.toUpperCase().replace(/[^A-Z0-9]/g, '').substring(0, 8);
            },

            onNameInput: function () {
                if (!this.editMode) {
                    this.form.identifier = this.slugify(this.form.name);
                    this.form.key = this.keyify(this.form.name);
                }
            },

            openCreate: function () {
                this.editMode = false;
                this.editHasIssues = false;
                this.formError = '';
                this.form = { name: '', identifier: '', key: '', description: '', color: this.colors[0] || '#c49a3c', parentKey: '', moduleWiki: true, moduleTime: true };
                this.showModal = true;
            },

            openEdit: function (p) {
                this.editMode = true;
                this.editHasIssues = p.has_issues;
                this.formError = '';
                this.form = { name: p.name, identifier: p.identifier, key: p.key, description: p.description || '', color: p.color || '#c49a3c', parentKey: '', moduleWiki: true, moduleTime: true };
                this.showModal = true;
            },

            closeModal: function () {
                this.showModal = false;
            },

            async submitForm() {
                this.saving = true;
                this.formError = '';
                try {
                    if (this.editMode) {
                        var res = await spFetch('/api/v1/projects/' + this.form.key + '/', {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                name: this.form.name,
                                description: this.form.description || null,
                                color: this.form.color || null
                            })
                        });
                    } else {
                        var modules = ['issue_tracking'];
                        if (this.form.moduleWiki) modules.push('wiki');
                        if (this.form.moduleTime) modules.push('time_tracking');
                        var res = await spFetch('/api/v1/projects/', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                name: this.form.name,
                                identifier: this.form.identifier,
                                key: this.form.key,
                                description: this.form.description || null,
                                parent_key: this.form.parentKey || null,
                                color: this.form.color || null,
                                modules: modules
                            })
                        });
                    }
                    if (res.ok) {
                        location.reload();
                    } else {
                        var data = await res.json();
                        this.formError = (data.errors && data.errors[0] && data.errors[0].message)
                            || (data.detail && data.detail[0] && data.detail[0].msg)
                            || data.detail
                            || 'Failed to save';
                    }
                } catch (_e) {
                    this.formError = 'Unable to connect.';
                }
                this.saving = false;
            },

            async archiveProject(p) {
                if (!confirm('Archive ' + p.name + '?')) return;
                await spFetch('/api/v1/admin/projects/' + p.key + '/archive/', { method: 'POST' });
                location.reload();
            },

            async unarchiveProject(p) {
                await spFetch('/api/v1/admin/projects/' + p.key + '/unarchive/', { method: 'POST' });
                location.reload();
            }
        };
    }
