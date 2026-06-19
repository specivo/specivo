export function metadataFieldRenderer(initial) {
        return {
            schemas: initial.schemas || [],
            trackerId: initial.trackerId || null,
            existingValues: initial.existingValues || {},
            currentFields: [],
            currentSchemaNames: [],
            metadata: { ...initial.existingValues },

            resolveFields() {
                var tid = parseInt(this.trackerId) || null;
                var matching = this.schemas.filter(function (s) {
                    return s.tracker_id === null || s.tracker_id === tid;
                });

                var fields = [];
                var names = [];
                for (var si = 0; si < matching.length; si++) {
                    var schema = matching[si];
                    names.push(schema.name);
                    var props = (schema.schema_definition && schema.schema_definition.properties) || {};
                    var keys = Object.keys(props);
                    for (var ki = 0; ki < keys.length; ki++) {
                        var key = keys[ki];
                        var prop = props[key];
                        var inputType = 'text', typeLabel = 'string', options = null, placeholder = '', min = null, max = null;
                        if (prop.enum) {
                            inputType = 'enum'; typeLabel = 'enum'; options = prop.enum;
                        } else if (prop.type === 'integer' || prop.type === 'number') {
                            inputType = 'number'; typeLabel = prop.type; min = prop.minimum; max = prop.maximum;
                        } else if (prop.type === 'boolean') {
                            inputType = 'boolean'; typeLabel = 'boolean';
                        } else if (prop.type === 'string' && prop.format === 'date') {
                            inputType = 'date'; typeLabel = 'date';
                        } else if (prop.type === 'array' && prop.items && prop.items.enum) {
                            inputType = 'multiselect'; typeLabel = 'multi'; options = prop.items.enum;
                        } else if (prop.type === 'array') {
                            inputType = 'tags'; typeLabel = 'array'; placeholder = 'Type and press Enter...';
                        } else {
                            placeholder = prop.description || '';
                        }

                        var label = key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
                        fields.push({ key: key, label: label, inputType: inputType, typeLabel: typeLabel, options: options, placeholder: placeholder, min: min, max: max });

                        // Initialize metadata value if not set
                        if (!(key in this.metadata)) {
                            if (inputType === 'tags' || inputType === 'multiselect') this.metadata[key] = [];
                            else if (inputType === 'boolean') this.metadata[key] = false;
                            else this.metadata[key] = '';
                        }
                    }
                }
                this.currentFields = fields;
                // Deduplicate schema names
                var unique = [];
                for (var i = 0; i < names.length; i++) {
                    if (unique.indexOf(names[i]) === -1) unique.push(names[i]);
                }
                this.currentSchemaNames = unique;
            },

            addTag(key, event) {
                var val = event.target.value.trim();
                if (!val) return;
                if (!this.metadata[key]) this.metadata[key] = [];
                if (this.metadata[key].indexOf(val) === -1) this.metadata[key].push(val);
                event.target.value = '';
            },

            removeLastTag(key, event) {
                if (event.target.value === '' && this.metadata[key] && this.metadata[key].length > 0) {
                    this.metadata[key].pop();
                }
            },

            hasMultiValue(key, opt) {
                return (this.metadata[key] || []).indexOf(opt) !== -1;
            },

            toggleMulti(key, opt) {
                if (!this.metadata[key]) this.metadata[key] = [];
                var idx = this.metadata[key].indexOf(opt);
                if (idx >= 0) this.metadata[key].splice(idx, 1);
                else this.metadata[key].push(opt);
            },

            schemaNameLabel() {
                return this.currentSchemaNames.join(' + ');
            },

            getMetadataForSubmit() {
                var result = {};
                for (var i = 0; i < this.currentFields.length; i++) {
                    var f = this.currentFields[i];
                    var v = this.metadata[f.key];
                    if (v === '' || v === null || v === undefined) continue;
                    if (Array.isArray(v) && v.length === 0) continue;
                    result[f.key] = v;
                }
                return result;
            }
        };
    }
