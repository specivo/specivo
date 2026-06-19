export function forgotPasswordForm() {
        return {
            email: '',
            error: '',
            loading: false,
            sent: false,
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Sending...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Send reset link';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.loading = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/auth/forgot-password/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({email: this.email})
                    });
                    if (res.ok || res.status === 202) {
                        this.sent = true;
                    } else if (res.status === 429) {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Too many requests. Please wait.';
                    } else {
                        /* Always show success to prevent enumeration */
                        this.sent = true;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    }
