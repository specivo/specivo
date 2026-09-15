export function descriptionEditor(initial) {
        var i18n = Object.assign({
            titleRequired: 'Title cannot be empty.',
            saveFailed: 'Could not save changes.',
            networkError: 'Network error. Please retry.'
        }, initial.i18n || {});

        return {
            subject: initial.subject || '',
            description: initial.description || '',
            subjectDraft: '',
            draft: '',
            editing: false,
            saving: false,
            error: '',
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            startEdit() {
                this.subjectDraft = this.subject;
                this.draft = this.description;
                this.error = '';
                this.editing = true;
                /* Focus the title input on next tick so Alpine has rendered it */
                this.$nextTick(function () {
                    if (this.$refs.subjectInput) {
                        this.$refs.subjectInput.focus();
                    }
                }.bind(this));
            },

            cancelEdit() {
                this.subjectDraft = '';
                this.draft = '';
                this.error = '';
                this.editing = false;
            },

            async save() {
                /* Client-side validation: title must contain non-whitespace */
                var trimmed = (this.subjectDraft || '').trim();
                if (!trimmed) {
                    this.error = i18n.titleRequired;
                    return;
                }
                this.error = '';
                this.saving = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            subject: trimmed,
                            description: this.draft,
                            lock_version: this.lockVersion
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var msg = '';
                        try {
                            var body = await res.json();
                            if (body && body.errors && body.errors.length) {
                                msg = body.errors[0].message || '';
                            }
                        } catch (_e) { /* ignore parse errors */ }
                        this.error = msg || i18n.saveFailed;
                    }
                } catch (_e) {
                    this.error = i18n.networkError;
                }
                this.saving = false;
            }
        };
    }
