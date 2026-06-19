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

            saveEstimate() {
                var h = parseInt(this.$refs.estH.value) || 0;
                var m = Math.min(parseInt(this.$refs.estM.value) || 0, 59);
                var total = (h || m) ? Math.round((h + m / 60) * 100) / 100 : null;
                this.updateField('estimated_hours', total);
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
