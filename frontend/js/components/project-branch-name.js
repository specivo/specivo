/**
 * Project settings "Branch names" card: the format the issue page's
 * "Copy branch name" button uses, and the tracker → type mapping.
 *
 * The preview is only a hint; the server builds the real name
 * (specivo/services/branch_name.py) and validates the format on save.
 */
export function projectBranchName(initial) {
    var i18n = Object.assign({
        saved: 'Saved',
        saveFailed: 'Could not save the branch name settings.',
        connectFailed: 'Unable to connect.'
    }, initial.i18n || {});
    var settings = initial.settings || {};
    var types = settings.types || {};

    return {
        i18n: i18n,
        projectKey: initial.projectKey || '',
        format: settings.format || '{type}/{key}-{slug}',
        defaultType: settings.default_type || 'chore',
        trackers: (initial.trackers || []).map(function (t) {
            return {name: t.name, type: types[t.name.toLowerCase()] || ''};
        }),
        saving: false,
        message: '',
        error: '',

        get preview() {
            var tracker = this.trackers.find(function (t) { return t.type !== ''; });
            var type = tracker ? tracker.type : this.defaultType;
            return this.format
                .split('{type}').join(type)
                .split('{key}').join(this.projectKey + '-12')
                .split('{slug}').join('add-the-login-form');
        },

        async save() {
            this.saving = true;
            this.message = '';
            this.error = '';
            var mapped = {};
            this.trackers.forEach(function (t) {
                var value = t.type.trim();
                if (value !== '') mapped[t.name] = value;
            });
            try {
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        branch_name: {format: this.format.trim(), types: mapped, default_type: this.defaultType.trim()}
                    })
                });
                if (res.ok) {
                    this.message = i18n.saved;
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.error = (err.errors && err.errors[0] && err.errors[0].message) || i18n.saveFailed;
                }
            } catch (_e) {
                this.error = i18n.connectFailed;
            }
            this.saving = false;
        }
    };
}
