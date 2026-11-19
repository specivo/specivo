import { resolveSchemaFields } from '../lib/metadata-fields';

export function issueMetadataPanel(initial) {
        var i18n = Object.assign({
            tagsPlaceholder: 'Type and press Enter...',
            addMore: 'Add more...',
            yes: 'Yes',
            no: 'No',
            countFields: '%(filled)s/%(total)s fields',
            saving: 'Saving...',
            saveMetadata: 'Save Metadata',
            saveFailed: 'The metadata could not be saved.'
        }, initial.i18n || {});

        // {key: {id: name}}: the people and versions user and version fields hold, named
        // for this reader by the server; an id without a name is shown as stored.
        var valueNames = initial.names || {};
        var metadataObj = JSON.parse(JSON.stringify(initial.metadata || {}));
        var resolved = resolveSchemaFields(initial.schemas || [], metadataObj, i18n.tagsPlaceholder);

        return {
            i18n: i18n,
            expanded: false,
            dirty: false,
            saving: false,
            // The server's reason for refusing the last save; the panel stays editable.
            error: '',
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
            get fieldsLabel() {
                return i18n.countFields
                    .replace('%(filled)s', this.filledCount)
                    .replace('%(total)s', this.totalCount);
            },

            valueText(key, value) {
                var names = valueNames[key] || {};
                var name = names[String(value)];
                return name !== undefined ? name : String(value);
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
                            values.push({ text: this.valueText(f.key, v[j]), href: this.metadataSearchUrl(f.key, v[j], true) });
                        }
                    } else if (typeof v === 'boolean') {
                        values.push({ text: v ? i18n.yes : i18n.no, href: null });
                    } else if (f.inputType === 'enum') {
                        cls = 'sp-ms-enum';
                        values.push({ text: String(v), href: this.metadataSearchUrl(f.key, v, false) });
                    } else {
                        values.push({ text: this.valueText(f.key, v), href: this.metadataSearchUrl(f.key, v, false) });
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
                this.error = '';
            },

            async save() {
                this.saving = true;
                this.error = '';
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
                    } else {
                        // A refused value (schema, workflow rule, stale version):
                        // show why, and keep the edits so they can be corrected.
                        var body = null;
                        try { body = await res.json(); } catch (_parse) { /* not JSON */ }
                        var first = body && body.errors && body.errors[0];
                        this.error = (first && first.message) || i18n.saveFailed;
                    }
                } catch (_e) {
                    this.error = i18n.saveFailed;
                }
                this.saving = false;
            }
        };
    }
