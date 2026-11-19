import { resolveSchemaFields } from '../lib/metadata-fields';

export function metadataFieldRenderer(initial) {
        var i18n = Object.assign({
            tagsPlaceholder: 'Type and press Enter...',
            addMore: 'Add more...',
            yes: 'Yes',
            no: 'No'
        }, initial.i18n || {});

        return {
            i18n: i18n,
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

                var resolved = resolveSchemaFields(matching, this.metadata, i18n.tagsPlaceholder);
                this.currentFields = resolved.fields;
                this.currentSchemaNames = resolved.schemaNames;
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
