export function projectComputedMetadata(initial) {
        var configured = initial.computedMetadata || {};
        var keys = Object.keys(configured);

        // The row editor stores values as text. A map that already holds a
        // non-string value (only reachable via the API) would be silently
        // coerced on save, so editing is disabled instead of losing data.
        var editable = true;
        var rows = [];
        for (var i = 0; i < keys.length; i++) {
            var value = configured[keys[i]];
            if (typeof value !== 'string') {
                editable = false;
                value = JSON.stringify(value);
            }
            rows.push({key: keys[i], value: value});
        }

        return {
            projectKey: initial.projectKey || '',
            rows: rows,
            editable: editable,
            saving: false,
            message: '',
            error: '',

            addRow() {
                this.rows.push({key: '', value: ''});
            },

            removeRow(index) {
                this.rows.splice(index, 1);
            },

            // Collect rows into the {key: value} map the API expects.
            // Returns null (and sets this.error) when the rows are invalid.
            buildPayload() {
                var map = {};
                for (var i = 0; i < this.rows.length; i++) {
                    var key = this.rows[i].key.trim();
                    if (!key) {
                        // A blank key with a value is a half-filled row, not an
                        // intentional deletion — refuse rather than drop it.
                        if (this.rows[i].value.trim()) {
                            this.error = 'Every value needs a field name.';
                            return null;
                        }
                        continue;
                    }
                    if (Object.prototype.hasOwnProperty.call(map, key)) {
                        this.error = 'Duplicate field name: ' + key;
                        return null;
                    }
                    map[key] = this.rows[i].value;
                }
                return map;
            },

            async save() {
                this.message = '';
                this.error = '';
                var payload = this.buildPayload();
                if (payload === null) {
                    return;
                }
                this.saving = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({computed_metadata: payload})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this.message = 'Saved successfully.';
                        // Re-seed from the response so the editor shows what
                        // the server actually stored.
                        var saved = data.computed_metadata || {};
                        var savedKeys = Object.keys(saved);
                        var next = [];
                        for (var i = 0; i < savedKeys.length; i++) {
                            next.push({key: savedKeys[i], value: saved[savedKeys[i]]});
                        }
                        this.rows = next;
                    } else {
                        var err = await res.json();
                        this.error = (err.errors && err.errors[0] && err.errors[0].message) || 'Failed to save';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.saving = false;
            }
        };
    }
