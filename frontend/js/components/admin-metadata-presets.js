export function adminMetadataPresets(initialPresets, initialLabels) {
        var _knownIcons = ['code', 'bug', 'megaphone', 'sprint', 'book'];
        return {
            presets: initialPresets || [],
            labels: initialLabels || {},

            isKnownIcon(icon) {
                return _knownIcons.indexOf(icon) !== -1;
            },

            schemaFields(obj) {
                if (!obj || !obj.properties) return [];
                return Object.keys(obj.properties);
            },

            fieldCountLabel(obj) {
                var count = this.schemaFields(obj).length;
                return count + ' fields';
            },

            get builtinPresets() {
                return this.presets.filter(function (p) { return p.is_builtin; });
            },
            get customPresets() {
                return this.presets.filter(function (p) { return !p.is_builtin; });
            },

            showModal: false,
            showDeleteModal: false,
            editingPreset: null,
            deleteTarget: null,
            saving: false,
            schemaError: '',
            nameError: '',
            slugError: '',
            form: {
                slug: '',
                name: '',
                description: '',
                icon: 'default',
                schema_definition_raw: ''
            },

            openCreate: function () {
                this.editingPreset = null;
                this.schemaError = '';
                this.nameError = '';
                this.slugError = '';
                this.form = {
                    slug: '',
                    name: '',
                    description: '',
                    icon: 'default',
                    schema_definition_raw: JSON.stringify({ type: 'object', properties: { field_name: { type: 'string' } } }, null, 2)
                };
                this.showModal = true;
            },

            openEdit: function (preset) {
                this.editingPreset = preset;
                this.schemaError = '';
                this.nameError = '';
                this.slugError = '';
                this.form = {
                    slug: preset.slug,
                    name: preset.name,
                    description: preset.description || '',
                    icon: preset.icon,
                    schema_definition_raw: JSON.stringify(preset.schema_definition, null, 2)
                };
                this.showModal = true;
            },

            validateSchema: function () {
                if (!this.form.schema_definition_raw.trim()) {
                    this.schemaError = 'Schema definition is required.';
                    return false;
                }
                try {
                    var parsed = JSON.parse(this.form.schema_definition_raw);
                    if (parsed.type !== 'object') {
                        this.schemaError = 'Root schema type must be "object".';
                        return false;
                    }
                    this.schemaError = '';
                    return true;
                } catch (e) {
                    this.schemaError = 'Invalid JSON: ' + e.message;
                    return false;
                }
            },

            normalizeSlug: function (raw) {
                return (raw || '').trim().toLowerCase().replace(/\s+/g, '-');
            },

            validateForm: function () {
                var ok = true;
                this.nameError = '';
                this.slugError = '';
                if (!this.form.name || !this.form.name.trim()) {
                    this.nameError = this.labels.nameRequired || 'Name is required.';
                    ok = false;
                }
                // Slug is read-only for built-in presets, so only validate when editable.
                if (!(this.editingPreset && this.editingPreset.is_builtin)) {
                    var slug = this.normalizeSlug(this.form.slug);
                    if (!slug) {
                        this.slugError = this.labels.slugRequired || 'Identifier is required.';
                        ok = false;
                    } else if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
                        this.slugError = this.labels.slugInvalid || 'Use lowercase letters, numbers and dashes only.';
                        ok = false;
                    }
                }
                if (!this.validateSchema()) ok = false;
                return ok;
            },

            save: async function () {
                if (!this.validateForm()) return;
                this.saving = true;
                try {
                    var parsed = JSON.parse(this.form.schema_definition_raw);
                    var payload = {
                        name: this.form.name.trim(),
                        description: this.form.description || null,
                        icon: this.form.icon,
                        schema_definition: parsed
                    };
                    var resp;
                    if (this.editingPreset) {
                        if (!this.editingPreset.is_builtin) {
                            payload.slug = this.normalizeSlug(this.form.slug);
                        }
                        resp = await spFetch('/api/v1/admin/metadata-presets/' + this.editingPreset.slug + '/', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                    } else {
                        payload.slug = this.normalizeSlug(this.form.slug);
                        resp = await spFetch('/api/v1/admin/metadata-presets/', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });
                    }
                    if (!resp.ok) {
                        var err = await resp.json();
                        var msg = (err.errors && err.errors[0] && err.errors[0].message) || 'Save failed';
                        window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: msg } }));
                        return;
                    }
                    var updated = await resp.json();
                    if (this.editingPreset) {
                        var idx = this.presets.findIndex(function (p) { return p.slug === this.editingPreset.slug; }.bind(this));
                        if (idx !== -1) {
                            this.presets[idx] = updated;
                        }
                    } else {
                        this.presets.push(updated);
                    }
                    this.showModal = false;
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'success', message: this.editingPreset ? 'Preset updated' : 'Preset created' } }));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: 'Network error: ' + e.message } }));
                } finally {
                    this.saving = false;
                }
            },

            confirmDelete: function (preset) {
                this.deleteTarget = preset;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deleteTarget) return;
                try {
                    var resp = await spFetch('/api/v1/admin/metadata-presets/' + this.deleteTarget.slug + '/', {
                        method: 'DELETE'
                    });
                    if (!resp.ok) {
                        var err = await resp.json();
                        var msg = (err.errors && err.errors[0] && err.errors[0].message) || 'Delete failed';
                        window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: msg } }));
                        return;
                    }
                    var slug = this.deleteTarget.slug;
                    this.presets = this.presets.filter(function (p) { return p.slug !== slug; });
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'success', message: 'Preset deleted' } }));
                } catch (e) {
                    window.dispatchEvent(new CustomEvent('toast', { detail: { type: 'error', message: 'Network error: ' + e.message } }));
                } finally {
                    this.showDeleteModal = false;
                    this.deleteTarget = null;
                }
            }
        };
    }
