export function adminSettings(initial, labels) {
        // User-facing strings are translated in the template and passed as the
        // second argument; the English defaults only cover a caller that omits them.
        var i18n = Object.assign({
            updated: 'Setting updated.',
            saveFailed: 'Failed to save.'
        }, labels || {});

        return {
            items: initial || {},

            get itemKeys() {
                return Object.keys(this.items);
            },

            get hasItems() {
                return Object.keys(this.items).length > 0;
            },

            editingKey: null,
            editValue: '',
            saving: false,
            message: '',
            messageError: false,

            startEdit(key) {
                this.editingKey = key;
                this.editValue = this.items[key] || '';
                this.message = '';
            },

            cancelEdit() {
                this.editingKey = null;
                this.editValue = '';
            },

            async save(key) {
                this.saving = true;
                this.message = '';
                var payload = {};
                payload[key] = this.editValue;
                var res = await spFetch('/api/v1/admin/settings/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    var data = await res.json();
                    this.items = data;
                    this.editingKey = null;
                    this.message = i18n.updated;
                    this.messageError = false;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.message = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || i18n.saveFailed;
                    this.messageError = true;
                }
                this.saving = false;
            }
        };
    }
