export function projectTagsSettings(initial) {
        return {
            tags: initial.tags || [],
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            showModal: false,
            showDeleteModal: false,
            editingTag: null,
            deletingTag: null,
            saving: false,
            deleting: false,
            error: '',
            form: {name: '', color: ''},

            get canSaveTag() {
                return !this.saving && this.form.name.trim() !== '';
            },

            openCreate() {
                if (!this.canManage) return;
                this.editingTag = null;
                this.error = '';
                this.form = {name: '', color: ''};
                this.showModal = true;
            },

            openEdit(t) {
                if (!this.canManage) return;
                this.editingTag = t;
                this.error = '';
                this.form = {name: t.name, color: t.color || ''};
                this.showModal = true;
            },

            async saveTag() {
                if (!this.form.name.trim()) return;
                this.saving = true;
                this.error = '';
                var payload = {name: this.form.name.trim(), color: this.form.color || null};
                var url, method;
                if (this.editingTag) {
                    url = '/api/v1/projects/' + this.projectKey + '/tags/' + this.editingTag.id + '/';
                    method = 'PATCH';
                } else {
                    url = '/api/v1/projects/' + this.projectKey + '/tags/';
                    method = 'POST';
                }
                try {
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        var saved = await res.json();
                        if (this.editingTag) {
                            var idx = this.tags.findIndex(function (t) { return t.id === saved.id; });
                            if (idx !== -1) {
                                saved.issue_count = this.tags[idx].issue_count || 0;
                                saved.wiki_count = this.tags[idx].wiki_count || 0;
                                this.tags[idx] = saved;
                            }
                        } else {
                            saved.issue_count = 0;
                            saved.wiki_count = 0;
                            this.tags.push(saved);
                            this.tags.sort(function (a, b) {
                                return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
                            });
                        }
                        this.showModal = false;
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.error = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to save tag.';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.saving = false;
            },

            confirmDelete(t) {
                if (!this.canManage) return;
                this.deletingTag = t;
                this.showDeleteModal = true;
            },

            async doDelete() {
                if (!this.deletingTag) return;
                this.deleting = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/tags/' + this.deletingTag.id + '/', {
                        method: 'DELETE'
                    });
                    if (res.ok || res.status === 204) {
                        this.tags = this.tags.filter(function (t) { return t.id !== this.deletingTag.id; }.bind(this));
                        this.showDeleteModal = false;
                        this.deletingTag = null;
                    }
                } catch (_e) {}
                this.deleting = false;
            }
        };
    }
