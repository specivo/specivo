export function projectMetadataSettings(initial) {
        var _knownIcons = ['code', 'bug', 'megaphone', 'sprint', 'book'];
        return {
            presets: initial.presets || [],
            enabledSlugs: initial.enabledSlugs || [],
            schemas: initial.schemas || [],
            trackers: initial.trackers || [],
            projectKey: initial.projectKey,
            enableScope: {},

            isKnownIcon(icon) {
                return _knownIcons.indexOf(icon) !== -1;
            },

            schemaFields(obj) {
                if (!obj || !obj.properties) return [];
                return Object.keys(obj.properties);
            },

            trackerName(trackerId) {
                for (var i = 0; i < this.trackers.length; i++) {
                    if (this.trackers[i].id === trackerId) return this.trackers[i].name;
                }
                return 'Tracker #' + trackerId;
            },

            showSchemaModal: false,
            showDeleteModal: false,
            showDisableWarning: false,
            editingSchema: null,
            deleteTarget: null,
            schemaError: '',
            disableWarningMsg: '',

            schemaForm: {name: '', tracker_id: '', schema_definition_raw: ''},

            isEnabled: function (slug) {
                return this.enabledSlugs.includes(slug);
            },

            getEnabledScope: function (slug) {
                var schema = this.schemas.find(function (s) { return s.preset_slug === slug; });
                if (!schema) return '';
                if (schema.tracker_id) {
                    var tracker = this.trackers.find(function (t) { return t.id === schema.tracker_id; });
                    return '(' + (tracker ? tracker.name : 'Tracker #' + schema.tracker_id) + ' only)';
                }
                return '(all trackers)';
            },

            enablePreset: async function (slug) {
                var trackerId = this.enableScope[slug] || null;
                var body = trackerId ? {tracker_id: parseInt(trackerId)} : {};
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-presets/' + slug + '/enable/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                if (resp.ok) {
                    var schema = await resp.json();
                    this.enabledSlugs.push(slug);
                    schema.usage_count = 0;
                    this.schemas.push(schema);
                }
            },

            disablePreset: async function (slug) {
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-presets/' + slug + '/disable/', {
                    method: 'DELETE'
                });
                if (resp.ok) {
                    this.enabledSlugs = this.enabledSlugs.filter(function (s) { return s !== slug; });
                    this.schemas = this.schemas.filter(function (s) { return s.preset_slug !== slug; });
                } else if (resp.status === 409) {
                    var data = await resp.json();
                    this.disableWarningMsg = (data.errors && data.errors[0] && data.errors[0].message) || 'Cannot disable: issues have data from this preset.';
                    this.showDisableWarning = true;
                }
            },

            openCreateSchema: function () {
                this.editingSchema = null;
                this.schemaError = '';
                this.schemaForm = {
                    name: '',
                    tracker_id: '',
                    schema_definition_raw: JSON.stringify({type: 'object', properties: {field_name: {type: 'string'}}}, null, 2)
                };
                this.showSchemaModal = true;
            },

            openEditSchema: function (schema) {
                this.editingSchema = schema;
                this.schemaError = '';
                this.schemaForm = {
                    name: schema.name,
                    tracker_id: schema.tracker_id || '',
                    schema_definition_raw: JSON.stringify(schema.schema_definition, null, 2)
                };
                this.showSchemaModal = true;
            },

            validateSchema: function () {
                if (!this.schemaForm.schema_definition_raw.trim()) {
                    this.schemaError = 'Schema definition is required.';
                    return false;
                }
                try {
                    var parsed = JSON.parse(this.schemaForm.schema_definition_raw);
                    if (parsed.type !== 'object') {
                        this.schemaError = 'Root type must be "object".';
                        return false;
                    }
                    this.schemaError = '';
                    return true;
                } catch (e) {
                    this.schemaError = 'Invalid JSON: ' + e.message;
                    return false;
                }
            },

            saveSchema: async function () {
                if (!this.validateSchema()) return;
                var parsed = JSON.parse(this.schemaForm.schema_definition_raw);
                var trackerId = this.schemaForm.tracker_id ? parseInt(this.schemaForm.tracker_id) : null;

                if (this.editingSchema) {
                    var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + this.editingSchema.id + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.schemaForm.name, tracker_id: trackerId, schema_definition: parsed})
                    });
                    if (resp.ok) {
                        var updated = await resp.json();
                        updated.usage_count = this.editingSchema.usage_count;
                        var idx = this.schemas.findIndex(function (s) { return s.id === this.editingSchema.id; }.bind(this));
                        if (idx !== -1) this.schemas[idx] = updated;
                    }
                } else {
                    var resp2 = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: this.schemaForm.name, tracker_id: trackerId, schema_definition: parsed})
                    });
                    if (resp2.ok) {
                        var created = await resp2.json();
                        created.usage_count = 0;
                        this.schemas.push(created);
                    }
                }
                this.showSchemaModal = false;
            },

            confirmDeleteSchema: async function (schema) {
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + schema.id + '/usage/');
                if (resp.ok) {
                    var data = await resp.json();
                    if (data.usage_count > 0) {
                        this.disableWarningMsg = 'Cannot delete: ' + data.usage_count + ' issue(s) use this schema.';
                        this.showDisableWarning = true;
                        return;
                    }
                }
                this.deleteTarget = schema;
                this.showDeleteModal = true;
            },

            doDeleteSchema: async function () {
                if (!this.deleteTarget) return;
                var resp = await spFetch('/api/v1/admin/projects/' + this.projectKey + '/metadata-schemas/' + this.deleteTarget.id + '/', {
                    method: 'DELETE'
                });
                if (resp.ok) {
                    this.schemas = this.schemas.filter(function (s) { return s.id !== this.deleteTarget.id; }.bind(this));
                }
                this.showDeleteModal = false;
                this.deleteTarget = null;
            }
        };
    }
