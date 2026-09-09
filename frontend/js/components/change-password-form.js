// Self-service password change. Shared by the Security section of the
// preferences page and by the standalone page a user is sent to when the
// account is required to set a new password.
// Posts to /api/v1/auth/change-password/; the server re-checks everything,
// the client-side checks below only save a round trip.
//
// `redirectTo` is what makes the forced case work: on the standalone page the
// success message is not the end of the story, the user is trying to get into
// the app, so the browser is sent on once the flag has been cleared. Left
// empty (the preferences page) the form just reports success and stays put.
export function changePasswordForm(initial) {
    return {
        minLength: initial.minLength || 8,
        messages: initial.messages || {},
        redirectTo: initial.redirectTo || '',
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
                    if (this.redirectTo) {
                        // Keep the button disabled while the browser navigates,
                        // so a second submit cannot race the redirect.
                        window.location.href = this.redirectTo;
                        return;
                    }
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
