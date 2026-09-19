export function loginForm() {
        return {
            login: '',
            password: '',
            remember: false,
            error: '',
            loading: false,
            next: '',
            msgInvalid: '',
            msgError: '',
            msgLoading: '',
            msgSubmit: '',

            init() {
                this.next = this.safeNext(this.$el.dataset.next);
                this.msgInvalid = this.$el.dataset.msgInvalid || 'Invalid credentials';
                this.msgError = this.$el.dataset.msgError || 'Unable to connect. Please try again.';
                this.msgLoading = this.$el.dataset.msgLoading || 'Signing in...';
                this.msgSubmit = this.$el.dataset.msgSubmit || 'Sign in';
            },

            get errorClass() {
                return this.error ? 'show' : '';
            },

            // Where to go once signed in. The server already validated the
            // ``next`` it rendered, but this runs the same check again rather
            // than trusting an attribute: a single leading slash and nothing
            // that a browser would resolve as another origin.
            safeNext(value) {
                if (!value || value.charAt(0) !== '/') {
                    return '';
                }
                if (value.charAt(1) === '/' || value.charAt(1) === '\\') {
                    return '';
                }
                return value;
            },

            get buttonText() {
                return this.loading ? this.msgLoading : this.msgSubmit;
            },

            async submit() {
                this.loading = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/auth/login/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({login: this.login, password: this.password, remember: this.remember})
                    });
                    if (res.ok) {
                        window.location.href = this.next || '/';
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || this.msgInvalid;
                    }
                } catch (_e) {
                    this.error = this.msgError;
                }
                this.loading = false;
            }
        };
    }
