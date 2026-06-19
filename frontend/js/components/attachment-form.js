export function attachmentForm(initial) {
        return {
            showForm: false,
            file: null,
            description: '',
            submitting: false,
            error: '',
            issueId: initial.issueId || 0,

            selectFile(event) {
                this.file = event.target.files[0] || null;
            },

            async submit() {
                if (!this.file) return;
                this.submitting = true;
                this.error = '';
                try {
                    var formData = new FormData();
                    formData.append('file', this.file);
                    formData.append('container_type', 'Issue');
                    formData.append('container_id', this.issueId);
                    if (this.description) {
                        formData.append('description', this.description);
                    }
                    var res = await spFetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Upload failed';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    }
