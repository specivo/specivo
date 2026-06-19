export function adminSettings(initial) {
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
                    this.message = 'Setting updated.';
                    this.messageError = false;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.message = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || 'Failed to save.';
                    this.messageError = true;
                }
                this.saving = false;
            }
        };
    }
