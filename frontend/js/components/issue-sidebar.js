export function issueSidebar(initial) {
        return {
            updating: false,
            displayKey: initial.displayKey || '',
            lockVersion: initial.lockVersion || 0,

            /* CSP-safe event handlers for select/input changes */
            onSelectInt(fieldName, event) {
                this.updateField(fieldName, parseInt(event.target.value) || null);
            },
            onSelectIntRequired(fieldName, event) {
                this.updateField(fieldName, parseInt(event.target.value));
            },
            onInputChange(fieldName, event) {
                this.updateField(fieldName, event.target.value || null);
            },

            /* Save an hours field from its "<ref>H" and "<ref>M" inputs; the button
               names both in data-field and data-ref. Blank inputs send null
               (no change), so a zero is only sent when one was typed. */
            saveHours(event) {
                var field = event.currentTarget.dataset.field;
                var ref = event.currentTarget.dataset.ref;
                var hInput = this.$refs[ref + 'H'].value;
                var mInput = this.$refs[ref + 'M'].value;
                var h = parseInt(hInput) || 0;
                var m = Math.min(parseInt(mInput) || 0, 59);
                var typed = hInput !== '' || mInput !== '';
                var total = (field === 'estimated_hours' ? (h || m) : typed) ? Math.round((h + m / 60) * 100) / 100 : null;
                this.updateField(field, total);
            },

            async updateField(fieldName, value) {
                this.updating = true;
                var payload = {lock_version: this.lockVersion};
                payload[fieldName] = value;
                try {
                    var res = await spFetch('/api/v1/issues/' + this.displayKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        window.location.reload();
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.updating = false;
            }
        };
    }
