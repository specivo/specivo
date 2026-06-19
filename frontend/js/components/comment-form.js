export function commentForm(initial) {
        return {
            notes: '',
            submitting: false,
            displayKey: initial.displayKey || '',

            get canSubmit() {
                return !this.submitting && this.notes.trim() !== '';
            },

            async submit() {
                this.submitting = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/journals/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({notes: this.notes})
                    });
                    if (res.ok) {
                        this.notes = '';
                        window.location.reload();
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.submitting = false;
            }
        };
    }
