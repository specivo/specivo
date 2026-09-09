// Self-service password change on the preferences page.
// Posts to /api/v1/auth/change-password/; the server re-checks everything,
// the client-side checks below only save a round trip.
export function changePasswordForm(initial) {
    return {
        minLength: initial.minLength || 8,
        messages: initial.messages || {},
        current: '',
        next: '',
        confirm: '',
        error: '',
        success: '',
        saving: false,

        get buttonText() {
            return this.saving ? this.messages.saving : this.messages.submit;
        },

        reset() {
            this.current = '';
            this.next = '';
            this.confirm = '';
        },

        async submit() {
            this.error = '';
            this.success = '';
            if (this.next.length < this.minLength) {
                this.error = this.messages.tooShort;
                return;
            }
            if (this.next !== this.confirm) {
                this.error = this.messages.mismatch;
                return;
            }
            if (this.next === this.current) {
                this.error = this.messages.unchanged;
                return;
            }
            this.saving = true;
            try {
                var res = await spFetch('/api/v1/auth/change-password/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        current_password: this.current,
                        new_password: this.next
                    })
                });
                if (res.ok) {
                    var body = await res.json();
                    this.success = body.revoked_sessions > 0
                        ? this.messages.successOtherSessions
                        : this.messages.success;
                    this.reset();
                } else {
                    var data = await res.json();
                    this.error = (data.errors && data.errors[0] && data.errors[0].message) || this.messages.failed;
                }
            } catch (_e) {
                this.error = this.messages.connectionError;
            }
            this.saving = false;
        }
    };
}
