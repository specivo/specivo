/* Scripts written without spaces between words (Han, Hiragana, Katakana,
   Thai, Lao, Myanmar, Khmer): a few characters already make a title. */
var UNSEGMENTED_SCRIPT = /[\u0E00-\u0EFF\u1000-\u109F\u1780-\u17FF\u19E0-\u19FF\u3040-\u30FF\u31F0-\u31FF\u3400-\u4DBF\u4E00-\u9FFF\uA9E0-\uA9FF\uAA60-\uAA7F\uF900-\uFAFF\uFF66-\uFF9F\u{20000}-\u{3134F}]/u;
var SIMILAR_DEBOUNCE_MS = 1000;
var SIMILAR_MIN_LENGTH = 10;
var SIMILAR_MIN_LENGTH_UNSEGMENTED = 4;
var SIMILAR_MAX_QUERY = 255;

/* Whole hours and minutes of a stored hours value ("1.75" -> 1 and 45); '' when unset. */
function hoursPart(value) {
    return (value === null || value === undefined || value === '') ? '' : Math.floor(parseFloat(value));
}
function minutesPart(value) {
    return (value === null || value === undefined || value === '') ? '' : Math.round((parseFloat(value) % 1) * 60);
}

/* Hours from the hour and minute inputs, rounded to 0.01; null when both are blank. */
function toHours(hours, minutes) {
    if ((hours === '' || hours === null) && (minutes === '' || minutes === null)) {
        return null;
    }
    return Math.round(((parseInt(hours) || 0) + Math.min(parseInt(minutes) || 0, 59) / 60) * 100) / 100;
}

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
            orig_hours: hoursPart(initial.original_estimate),
            orig_minutes: minutesPart(initial.original_estimate),
            rem_hours: hoursPart(initial.remaining_estimate),
            rem_minutes: minutesPart(initial.remaining_estimate),
            done_ratio: initial.done_ratio || 0,
            is_private: initial.is_private || false,
            canSetPrivate: initial.canSetPrivate || false,
            trackerPrivateDefaults: initial.trackerPrivateDefaults || {},
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
            similarEnabled: initial.similarEnabled === true,
            canManageRelations: initial.canManageRelations === true,
            similarIssues: [],
            _similarTimer: null,
            _similarController: null,
            _similarLastSent: null,

            get canSubmit() {
                return !this.submitting && this.subject.trim() !== '';
            },

            // A new issue starts with the chosen tracker's private-by-default
            // setting; editing an issue never changes its privacy behind the user's back.
            applyTrackerPrivacyDefault() {
                if (this.mode !== 'create') return;
                this.is_private = this.trackerPrivateDefaults[String(this.tracker_id)] === true;
            },

            fileSizeKB(f) {
                return (f.size / 1024).toFixed(1) + ' KB';
            },

            init() {
                var self = this;
                this.$el.addEventListener('relation-issue-selected', function (e) {
                    self.newRelationKey = e.detail.key;
                });
                if (this.mode === 'create' && this.similarEnabled) {
                    this.$watch('subject', function () { self._subjectChanged(); });
                }
            },

            destroy() {
                clearTimeout(this._similarTimer);
                if (this._similarController) this._similarController.abort();
            },

            /* Similar issues: ask once the subject has rested for a second.
               Nothing here may get in the way of filing the issue: a failure
               only empties the list. */
            _similarQuery() {
                return this.subject.trim().slice(0, SIMILAR_MAX_QUERY);
            },

            _longEnough(q) {
                var min = UNSEGMENTED_SCRIPT.test(q) ? SIMILAR_MIN_LENGTH_UNSEGMENTED : SIMILAR_MIN_LENGTH;
                return Array.from(q).length >= min;
            },

            _subjectChanged() {
                var self = this;
                clearTimeout(this._similarTimer);
                if (!this._longEnough(this._similarQuery())) {
                    this._clearSimilar();
                    return;
                }
                this._similarTimer = setTimeout(function () { self._fetchSimilar(); }, SIMILAR_DEBOUNCE_MS);
            },

            _clearSimilar() {
                if (this._similarController) this._similarController.abort();
                this._similarController = null;
                this._similarLastSent = null;
                this.similarIssues = [];
            },

            async _fetchSimilar() {
                var q = this._similarQuery();
                if (!this._longEnough(q) || q === this._similarLastSent) return;
                if (this._similarController) this._similarController.abort();
                var controller = new AbortController();
                this._similarController = controller;
                this._similarLastSent = q;
                try {
                    var res = await spFetch(
                        '/api/v1/projects/' + encodeURIComponent(this.projectKey) +
                        '/issues/similar/?q=' + encodeURIComponent(q),
                        {signal: controller.signal}
                    );
                    if (!res.ok) throw new Error('similar issues: ' + res.status);
                    var data = await res.json();
                    if (controller !== this._similarController) return;
                    this.similarIssues = Array.isArray(data) ? data : [];
                } catch (_e) {
                    /* A newer request replaced this one: it owns the list. */
                    if (controller !== this._similarController) return;
                    this.similarIssues = [];
                    this._similarLastSent = null;
                } finally {
                    if (controller === this._similarController) this._similarController = null;
                }
            },

            markDuplicate(item) {
                var already = this.pendingRelations.some(function (r) {
                    return r.issue_key === item.key && r.relation_type === 'duplicates';
                });
                if (already) return;
                this.pendingRelations.push({
                    issue_key: item.key,
                    relation_type: 'duplicates',
                    type_label: this._relationLabel('duplicates')
                });
            },

            _relationLabel(type) {
                // Labels are translated in the template (i18n.relationTypes);
                // the English defaults only cover a caller that omits them.
                var typeLabels = Object.assign({
                    relates: 'Relates to',
                    blocks: 'Blocks',
                    blocked: 'Blocked by',
                    duplicates: 'Duplicates',
                    duplicated: 'Duplicated by',
                    precedes: 'Precedes',
                    follows: 'Follows'
                }, (initial.i18n && initial.i18n.relationTypes) || {});
                return typeLabels[type] || type;
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
                this.pendingRelations.push({
                    issue_key: this.newRelationKey,
                    relation_type: this.newRelationType,
                    type_label: this._relationLabel(this.newRelationType)
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

            /* Upload the chosen files to issue *issueId*, recorded in one history
               entry: *journalId* (the edit they came with) or the first file's. */
            async _uploadFiles(issueId, journalId) {
                var uploadJournalId = journalId || null;
                for (var i = 0; i < this.files.length; i++) {
                    var formData = new FormData();
                    formData.append('file', this.files[i]);
                    formData.append('container_type', 'Issue');
                    formData.append('container_id', issueId);
                    if (uploadJournalId) {
                        formData.append('journal_id', uploadJournalId);
                    }
                    try {
                        var upRes = await spFetch('/api/v1/attachments/', {
                            method: 'POST',
                            body: formData
                        });
                        if (upRes.ok) {
                            var upData = await upRes.json();
                            if (upData.journal_id) uploadJournalId = upData.journal_id;
                        }
                    } catch (_e) { /* best-effort */ }
                }
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
                    original_estimate: toHours(this.orig_hours, this.orig_minutes),
                    remaining_estimate: toHours(this.rem_hours, this.rem_minutes),
                    done_ratio: this.done_ratio,
                    // Without set_issues_private the server applies the tracker's
                    // default on create and keeps the current value on edit.
                    is_private: this.canSetPrivate ? this.is_private : null,
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
                            /* Files chosen in the form are recorded with the edit */
                            var updated = await res.json();
                            await this._uploadFiles(updated.id, updated.journal_id);
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

                        /* Upload attachments, recorded in one history entry */
                        await this._uploadFiles(data.id, null);

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
