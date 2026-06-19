export function replyForm(initial) {
        return {
            showReply: false,
            replyText: '',
            submitting: false,
            displayKey: initial.displayKey || '',
            journalId: initial.journalId || 0,

            get canSubmitReply() {
                return !this.submitting && this.replyText.trim() !== '';
            },

            async submitReply() {
                if (!this.replyText.trim()) return;
                this.submitting = true;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/journals/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({notes: this.replyText, reply_to_id: this.journalId})
                    });
                    if (res.ok) {
                        this.replyText = '';
                        this.showReply = false;
                        window.location.reload();
                    }
                } catch (_e) {}
                this.submitting = false;
            }
        };
    }
