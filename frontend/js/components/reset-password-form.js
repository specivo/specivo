export function resetPasswordForm() {
        return {
            password: '',
            confirm: '',
            error: '',
            loading: false,
            token: '',
            msgMismatch: '',
            msgShort: '',
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.token = this.$el.dataset.token || '';
                this.msgMismatch = this.$el.dataset.msgMismatch || 'Passwords do not match';
                this.msgShort = this.$el.dataset.msgShort || 'Password must be at least 8 characters';
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Resetting...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Reset Password';
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.error = '';
                if (this.password.length < 8) {
                    this.error = this.msgShort;
                    return;
                }
                if (this.password !== this.confirm) {
                    this.error = this.msgMismatch;
                    return;
                }
                this.loading = true;
                try {
                    var res = await spFetch('/api/v1/auth/reset-password/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({token: this.token, new_password: this.password})
                    });
                    if (res.ok) {
                        window.location.href = '/login/?reset=ok';
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || this.msgError;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    }
