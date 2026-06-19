export function projectVersions(initial) {
        return {
            versions: initial.versions || [],
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            showModal: false,
            showDeleteModal: false,
            editingVersion: null,
            deletingVersion: null,
            saving: false,
            deleting: false,
            message: '',
            messageType: 'success',
            form: {name: '', description: '', status: 'open', due_date: ''},

            get canSaveVersion() {
                return !this.saving && this.form.name.trim() !== '';
            },

            openCreate: function () {
                if (!this.canManage) return;
                this.editingVersion = null;
                this.form = {name: '', description: '', status: 'open', due_date: ''};
                this.showModal = true;
            },

            openEdit: function (v) {
                if (!this.canManage) return;
                this.editingVersion = v;
                this.form = {
                    name: v.name,
                    description: v.description || '',
                    status: v.status,
                    due_date: v.due_date || ''
                };
                this.showModal = true;
            },

            saveVersion: async function () {
                if (!this.form.name.trim()) return;
                this.saving = true;
                try {
                    var payload = {
                        name: this.form.name.trim(),
                        description: this.form.description || null,
                        status: this.form.status,
                        effective_date: this.form.due_date || null
                    };
                    var url, method;
                    if (this.editingVersion) {
                        url = '/api/v1/projects/' + this.projectKey + '/versions/' + this.editingVersion.id + '/';
                        method = 'PATCH';
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/versions/';
                        method = 'POST';
                    }
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        // Reload versions with roadmap data to get fresh counts
                        await this._reloadVersions();
                        var action = this.editingVersion ? 'updated' : 'created';
                        this.flash('Version "' + this.form.name + '" ' + action + '.', 'success');
                        this.showModal = false;
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.flash((err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to save version.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.saving = false;
            },

            confirmDelete: function (v) {
                if (!this.canManage) return;
                this.deletingVersion = v;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deletingVersion) return;
                this.deleting = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/versions/' + this.deletingVersion.id + '/', {
                        method: 'DELETE',
                        headers: {'Content-Type': 'application/json'}
                    });
                    if (res.ok || res.status === 204) {
                        var name = this.deletingVersion.name;
                        this.versions = this.versions.filter(function (v) { return v.id !== this.deletingVersion.id; }.bind(this));
                        this.showDeleteModal = false;
                        this.deletingVersion = null;
                        this.flash('Version "' + name + '" deleted.', 'success');
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.flash((err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to delete version.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.deleting = false;
            },

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type;
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            _reloadVersions: async function () {
                try {
                    var roadmapRes = await spFetch('/api/v1/projects/' + this.projectKey + '/roadmap/');
                    if (roadmapRes.ok) {
                        var entries = await roadmapRes.json();
                        var today = new Date().toISOString().slice(0, 10);
                        this.versions = entries.map(function (e) {
                            var dueDate = e.version.effective_date || '';
                            return {
                                id: e.version.id,
                                name: e.version.name,
                                description: e.version.description,
                                status: e.version.status,
                                due_date: dueDate,
                                progress: e.progress_percent,
                                open_count: e.open_count,
                                closed_count: e.closed_count,
                                overdue: dueDate !== '' && dueDate < today && e.version.status !== 'closed'
                            };
                        });
                    }
                } catch (_e) {
                    // Silently fail — page already has stale data
                }
            }
        };
    }
