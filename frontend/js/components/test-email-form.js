export function testEmailForm() {
        return {
            to: '',
            subject: 'Specivo test email',
            body: 'This is a test email from Specivo to verify SMTP configuration.\n\nIf you received this message, email delivery is working correctly.',
            sending: false,
            result: null,
            resultOk: false,
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
                        this.result = 'Test email sent to ' + this.to;
                        this.resultOk = true;
                    } else {
                        this.result = data.error || 'Unknown error';
                        this.resultOk = false;
                    }
                } catch (e) {
                    this.result = 'Request failed: ' + e.message;
                    this.resultOk = false;
                } finally {
                    this.sending = false;
                }
            }
        };
    }
