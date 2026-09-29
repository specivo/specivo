export function testEmailForm() {
        return {
            to: '',
            subject: '',
            body: '',
            sending: false,
            result: null,
            resultOk: false,
            msgSent: '',
            msgUnknownError: '',
            msgRequestFailed: '',

            init() {
                // Translated strings arrive as data-msg-* attributes rendered by
                // the template; the English defaults cover a template without them.
                var d = this.$el.dataset;
                this.subject = d.msgSubject || 'Specivo test email';
                this.body = d.msgBody || 'This is a test email from Specivo to verify SMTP configuration.\n\nIf you received this message, email delivery is working correctly.';
                this.msgSent = d.msgSent || 'Test email sent to %(email)s';
                this.msgUnknownError = d.msgUnknownError || 'Unknown error';
                this.msgRequestFailed = d.msgRequestFailed || 'Request failed: %(error)s';
            },

            async sendTest() {
                if (!this.to) return;
                this.sending = true;
                this.result = null;
                try {
                    var resp = await spFetch('/api/v1/admin/test-email/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({to: this.to, subject: this.subject, body: this.body})
                    });
                    var data = await resp.json();
                    if (data.ok) {
                        this.result = this.msgSent.replace('%(email)s', this.to);
                        this.resultOk = true;
                    } else {
                        this.result = data.error || this.msgUnknownError;
                        this.resultOk = false;
                    }
                } catch (e) {
                    this.result = this.msgRequestFailed.replace('%(error)s', e.message);
                    this.resultOk = false;
                } finally {
                    this.sending = false;
                }
            }
        };
    }
