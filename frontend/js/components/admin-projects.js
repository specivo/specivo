import { apiErrorMessage } from '../lib/api-error';
import { projectCreateBody } from '../lib/project-form';

export function adminProjects(initial) {
        var i18n = Object.assign({
            saveFailed: 'Failed to save',
            connectFailed: 'Unable to connect.',
            confirmArchive: 'Archive %(name)s?',
            statusActive: 'Active',
            statusArchived: 'Archived',
            statusClosed: 'Closed',
            editProject: 'Edit Project',
            newProject: 'New Project',
            save: 'Save',
            createProject: 'Create Project'
        }, (initial && initial.i18n) || {});

        return {
            i18n: i18n,
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
                return spFormatDate(iso) || '-';
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

            /* The save request: a PATCH of the editable fields, or the create request. */
            _saveRequest() {
                if (this.editMode) {
                    return spFetch('/api/v1/projects/' + this.form.key + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            name: this.form.name,
                            description: this.form.description || null,
                            color: this.form.color || null
                        })
                    });
                }
                return spFetch('/api/v1/projects/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(projectCreateBody(this.form))
                });
            },

            async submitForm() {
                this.saving = true;
                this.formError = '';
                try {
                    var res = await this._saveRequest();
                    if (res.ok) {
                        location.reload();
                    } else {
                        this.formError = apiErrorMessage(await res.json(), i18n.saveFailed);
                    }
                } catch (_e) {
                    this.formError = i18n.connectFailed;
                }
                this.saving = false;
            },

            async archiveProject(p) {
                if (!confirm(i18n.confirmArchive.replace('%(name)s', p.name))) return;
                await spFetch('/api/v1/admin/projects/' + p.key + '/archive/', { method: 'POST' });
                location.reload();
            },

            async unarchiveProject(p) {
                await spFetch('/api/v1/admin/projects/' + p.key + '/unarchive/', { method: 'POST' });
                location.reload();
            }
        };
    }
