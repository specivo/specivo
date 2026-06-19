export function issueForm(initial) {
        return {
            submitting: false,
            subject: initial.subject || '',
            tracker_id: initial.tracker_id || 0,
            description: initial.description || '',
            status_id: initial.status_id || 0,
            priority_id: initial.priority_id || 0,
            assigned_to_id: initial.assigned_to_id || null,
            start_date: initial.start_date || '',
            due_date: initial.due_date || '',
            est_hours: initial.estimated_hours ? Math.floor(parseFloat(initial.estimated_hours)) : '',
            est_minutes: initial.estimated_hours ? Math.round((parseFloat(initial.estimated_hours) % 1) * 60) : '',
            done_ratio: initial.done_ratio || 0,
            is_private: initial.is_private || false,
            version_id: initial.version_id || '',
            mode: initial.mode || 'create',
            projectKey: initial.projectKey || '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,
            files: [],
            dragover: false,
            pendingRelations: [],
            showRelationForm: false,
            newRelationType: 'relates',
            newRelationKey: '',
            pendingTags: initial.tags || [],

            get canSubmit() {
                return !this.submitting && this.subject.trim() !== '';
            },

            fileSizeKB(f) {
                return (f.size / 1024).toFixed(1) + ' KB';
            },

            init() {
                var self = this;
                this.$el.addEventListener('relation-issue-selected', function (e) {
                    self.newRelationKey = e.detail.key;
                });
            },

            addFiles(fileList) {
                for (var i = 0; i < fileList.length; i++) {
                    this.files.push(fileList[i]);
                }
            },

            handleDrop(event) {
                this.dragover = false;
                if (event.dataTransfer && event.dataTransfer.files) {
                    this.addFiles(event.dataTransfer.files);
                }
            },

            addPendingRelation() {
                if (!this.newRelationKey) return;
                var typeLabels = {
                    relates: 'Relates to',
                    blocks: 'Blocks',
                    blocked: 'Blocked by',
                    duplicates: 'Duplicates',
                    duplicated: 'Duplicated by',
                    precedes: 'Precedes',
                    follows: 'Follows'
                };
                this.pendingRelations.push({
                    issue_key: this.newRelationKey,
                    relation_type: this.newRelationType,
                    type_label: typeLabels[this.newRelationType] || this.newRelationType
                });
                this.newRelationKey = '';
                this.newRelationType = 'relates';
                this.showRelationForm = false;
            },

            _getMetadataFromRenderer() {
                // Collect metadata from nested metadataFieldRenderer component if present
                var el = this.$el.querySelector('[x-data*="metadataFieldRenderer"]');
                if (el && el._x_dataStack) {
                    for (var i = 0; i < el._x_dataStack.length; i++) {
                        var data = el._x_dataStack[i];
                        if (typeof data.getMetadataForSubmit === 'function') {
                            return data.getMetadataForSubmit();
                        }
                    }
                }
                return null;
            },

            async submitForm(continueCreating) {
                this.submitting = true;
                var metadataVal = this._getMetadataFromRenderer();
                var payload = {
                    project_key: this.projectKey,
                    tracker_id: this.tracker_id,
                    subject: this.subject,
                    description: this.description || null,
                    status_id: this.status_id || null,
                    priority_id: this.priority_id || null,
                    assigned_to_id: this.assigned_to_id ? parseInt(this.assigned_to_id) : null,
                    start_date: this.start_date || null,
                    due_date: this.due_date || null,
                    estimated_hours: (this.est_hours || this.est_minutes) ? Math.round(((parseInt(this.est_hours) || 0) + (parseInt(this.est_minutes) || 0) / 60) * 100) / 100 : null,
                    done_ratio: this.done_ratio,
                    is_private: this.is_private,
                    version_id: this.version_id ? parseInt(this.version_id) : null
                };
                if (metadataVal !== null) {
                    payload.metadata = metadataVal;
                }

                var tagNames = (this.pendingTags || []).map(function (t) { return t.name; });

                try {
                    if (this.mode === 'edit') {
                        payload.lock_version = this.lockVersion;
                        var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        if (res.ok) {
                            try {
                                await spFetch('/api/v1/issues/' + this.displayKey + '/tags/', {
                                    method: 'PUT',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({names: tagNames})
                                });
                            } catch (_e) { /* best-effort */ }
                            window.location.href = '/projects/' + this.projectKey + '/issues/' + this.displayKey + '/';
                        }
                    } else {
                        var res = await spFetch('/api/v1/projects/' + this.projectKey + '/issues/', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                        var data = await res.json();
                        if (!data.key) {
                            this.submitting = false;
                            return;
                        }

                        /* Upload attachments */
                        for (var i = 0; i < this.files.length; i++) {
                            var formData = new FormData();
                            formData.append('file', this.files[i]);
                            formData.append('container_type', 'Issue');
                            formData.append('container_id', data.id);
                            try {
                                await spFetch('/api/v1/attachments/', {
                                    method: 'POST',
                                    body: formData
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        /* Apply pending tags */
                        if (tagNames.length) {
                            try {
                                await spFetch('/api/v1/issues/' + data.key + '/tags/', {
                                    method: 'PUT',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({names: tagNames})
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        /* Create pending relations */
                        for (var j = 0; j < this.pendingRelations.length; j++) {
                            var rel = this.pendingRelations[j];
                            try {
                                await spFetch('/api/v1/issues/' + data.key + '/relations/', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({
                                        issue_to_key: rel.issue_key,
                                        relation_type: rel.relation_type
                                    })
                                });
                            } catch (_e) { /* best-effort */ }
                        }

                        if (continueCreating) {
                            this.subject = '';
                            this.description = '';
                            this.files = [];
                            this.pendingRelations = [];
                            this.pendingTags = [];
                            this.newRelationKey = '';
                            this.showRelationForm = false;
                            this.submitting = false;
                            return;
                        }

                        window.location.href = '/projects/' + this.projectKey + '/issues/' + data.key + '/';
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.submitting = false;
            }
        };
    }
