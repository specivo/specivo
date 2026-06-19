export function wikiForm(initial) {
        return {
            title: initial.title || '',
            text: initial.text || '',
            comments: '',
            parentSlug: initial.parentSlug || '',
            parentTitle: initial.parentTitle || '',
            submitting: false,
            error: '',
            mode: initial.mode || 'create',
            projectKey: initial.projectKey || '',
            slug: initial.slug || '',
            pageId: initial.pageId || 0,
            lockVersion: initial.lockVersion || 0,
            preview: false,
            uploading: false,

            /**
             * Paste/drop handler invoked by the markdownEditor wrapper.
             * On image paste or drop, upload the file as a wiki attachment
             * and insert markdown referring to it at the cursor.
             *
             * Works only in edit mode when a pageId is available; for new
             * pages there is no container yet, so we let the default
             * editor behaviour (raw paste of text) run.
             */
            async handleEditorPasteDrop(event, kind, mdEditor) {
                if (!this.pageId) return; /* no container yet (create mode) */
                var files = [];
                if (kind === 'paste') {
                    var items = event.clipboardData && event.clipboardData.items;
                    if (!items) return;
                    for (var i = 0; i < items.length; i++) {
                        if (items[i].kind === 'file') {
                            var f = items[i].getAsFile();
                            if (f) files.push(f);
                        }
                    }
                } else if (kind === 'drop') {
                    var dt = event.dataTransfer;
                    if (!dt || !dt.files || !dt.files.length) return;
                    for (var j = 0; j < dt.files.length; j++) {
                        files.push(dt.files[j]);
                    }
                }
                if (!files.length) return;
                event.preventDefault();
                this.uploading = true;
                for (var k = 0; k < files.length; k++) {
                    await this._uploadAndInsert(files[k], mdEditor);
                }
                this.uploading = false;
            },

            async _uploadAndInsert(file, mdEditor) {
                try {
                    var formData = new FormData();
                    formData.append('file', file);
                    formData.append('container_type', 'WikiPage');
                    formData.append('container_id', this.pageId);
                    var res = await spFetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });
                    if (!res.ok) return;
                    var data = await res.json();
                    var url = '/api/v1/attachments/' + data.id + '/download/';
                    var isImage = (file.type || '').indexOf('image/') === 0;
                    var snippet = isImage
                        ? '![' + (data.filename || file.name) + '](' + url + ')'
                        : '[' + (data.filename || file.name) + '](' + url + ')';
                    mdEditor.insertText(snippet + '\n');
                } catch (_e) { /* upload failed silently — user can retry */ }
            },

            get canSubmit() {
                if (this.submitting) return false;
                if (this.mode === 'create' && !this.title.trim()) return false;
                return true;
            },

            async submitForm() {
                this.submitting = true;
                this.error = '';
                try {
                    var url, method, payload;
                    if (this.mode === 'edit') {
                        url = '/api/v1/projects/' + this.projectKey + '/wiki/' + this.slug + '/';
                        method = 'PATCH';
                        payload = {
                            title: this.title,
                            text: this.text,
                            lock_version: this.lockVersion,
                            comments: this.comments || null,
                            parent_slug: this.parentSlug || null
                        };
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/wiki/';
                        method = 'POST';
                        payload = {
                            title: this.title,
                            text: this.text,
                            parent_slug: this.parentSlug || null,
                            comments: this.comments || null
                        };
                    }
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        var data = await res.json();
                        var slug = data.slug || this.slug;
                        window.location.href = '/projects/' + this.projectKey + '/wiki/' + slug + '/';
                    } else {
                        var errData = await res.json();
                        this.error = (errData.errors && errData.errors[0] && errData.errors[0].message) || 'Failed to save';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect. Please try again.';
                }
                this.submitting = false;
            }
        };
    }
