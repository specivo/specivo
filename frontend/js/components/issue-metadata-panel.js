export function issueMetadataPanel(initial) {
        function resolveFieldsFromSchemas(schemas, metadataObj) {
            var fields = [];
            var names = [];
            for (var si = 0; si < schemas.length; si++) {
                var schema = schemas[si];
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
                    if (!(key in metadataObj)) {
                        if (inputType === 'tags' || inputType === 'multiselect') metadataObj[key] = [];
                        else if (inputType === 'boolean') metadataObj[key] = false;
                        else metadataObj[key] = '';
                    }
                }
            }
            // Deduplicate
            var unique = [];
            for (var i = 0; i < names.length; i++) {
                if (unique.indexOf(names[i]) === -1) unique.push(names[i]);
            }
            return { fields: fields, schemaNames: unique };
        }

        var metadataObj = JSON.parse(JSON.stringify(initial.metadata || {}));
        var resolved = resolveFieldsFromSchemas(initial.schemas || [], metadataObj);

        return {
            expanded: false,
            dirty: false,
            saving: false,
            fields: resolved.fields,
            schemaNames: resolved.schemaNames,
            metadata: metadataObj,
            savedMetadata: JSON.parse(JSON.stringify(metadataObj)),
            issueRef: initial.issueRef || '',
            projectKey: initial.projectKey || '',
            lockVersion: initial.lockVersion || 0,

            get totalCount() { return this.fields.length; },
            get filledCount() {
                var count = 0;
                for (var i = 0; i < this.fields.length; i++) {
                    var v = this.metadata[this.fields[i].key];
                    if (Array.isArray(v)) { if (v.length > 0) count++; }
                    else if (typeof v === 'boolean') { count++; }
                    else if (v !== '' && v !== null && v !== undefined) { count++; }
                }
                return count;
            },

            metadataSearchUrl(slug, value, isArray) {
                return '/search/?scope=issues&mf=' + encodeURIComponent(slug) +
                    '&mv=' + encodeURIComponent(value) + '&ma=' + (isArray ? '1' : '0');
            },

            get summaryChips() {
                var chips = [];
                for (var i = 0; i < this.fields.length; i++) {
                    var f = this.fields[i];
                    var v = this.metadata[f.key];
                    if (v === '' || v === null || v === undefined) continue;
                    if (Array.isArray(v) && v.length === 0) continue;
                    var cls = '';
                    var values = [];
                    if (Array.isArray(v)) {
                        // Each array element renders as its own clickable tag-link.
                        cls = 'sp-ms-tag';
                        for (var j = 0; j < v.length; j++) {
                            values.push({ text: String(v[j]), href: this.metadataSearchUrl(f.key, v[j], true) });
                        }
                    } else if (typeof v === 'boolean') {
                        values.push({ text: v ? 'Yes' : 'No', href: null });
                    } else if (f.inputType === 'enum') {
                        cls = 'sp-ms-enum';
                        values.push({ text: String(v), href: this.metadataSearchUrl(f.key, v, false) });
                    } else {
                        values.push({ text: String(v), href: this.metadataSearchUrl(f.key, v, false) });
                    }
                    chips.push({ key: f.key, label: f.label, values: values, chipClass: cls });
                }
                return chips;
            },

            addTag(key, event) {
                var val = event.target.value.trim();
                if (!val) return;
                if (!this.metadata[key]) this.metadata[key] = [];
                if (this.metadata[key].indexOf(val) === -1) this.metadata[key].push(val);
                event.target.value = '';
                this.dirty = true;
            },

            removeLastTag(key, event) {
                if (event.target.value === '' && this.metadata[key] && this.metadata[key].length > 0) {
                    this.metadata[key].pop();
                    this.dirty = true;
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
                this.dirty = true;
            },

            revert() {
                this.metadata = JSON.parse(JSON.stringify(this.savedMetadata));
                this.dirty = false;
            },

            async save() {
                this.saving = true;
                try {
                    // Build clean metadata (only filled values)
                    var clean = {};
                    for (var i = 0; i < this.fields.length; i++) {
                        var f = this.fields[i];
                        var v = this.metadata[f.key];
                        if (v === '' || v === null || v === undefined) continue;
                        if (Array.isArray(v) && v.length === 0) continue;
                        clean[f.key] = v;
                    }
                    var res = await spFetch('/api/v1/issues/' + this.issueRef + '/', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ metadata: clean, lock_version: this.lockVersion })
                    });
                    if (res.ok) {
                        var data = await res.json();
                        if (data.lock_version) this.lockVersion = data.lock_version;
                        this.savedMetadata = JSON.parse(JSON.stringify(this.metadata));
                        this.dirty = false;
                    }
                } catch (_e) {
                    /* request failed */
                }
                this.saving = false;
            }
        };
    }
