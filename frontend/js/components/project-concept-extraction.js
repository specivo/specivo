// Project settings: concept extraction with the project's own AI provider.
// Managers switch it on or off, set the confidence threshold and the run limit
// (PATCH /api/v1/projects/{key}/) and queue a run; instance administrators also
// set or remove the provider (PUT/DELETE /api/v1/projects/{key}/ai-provider/).
// The API key is write-only: the page only ever knows whether one is stored.
export function projectConceptExtraction(initial) {
    var i18n = Object.assign({
        saved: 'Saved.',
        providerSaved: 'AI provider saved.',
        providerRemoved: 'AI provider removed. Concept extraction is off.',
        queued: 'Extraction queued. Changed issues and pages are sent to the provider.',
        notQueued: 'Could not queue the extraction. It runs again tonight.',
        confirmRemove: 'Remove the AI provider of this project? Concept extraction is switched off.',
        errorWithMessage: 'Error: %(message)s',
        saveFailed: 'Failed to save',
        connectFailed: 'Unable to connect.'
    }, initial.i18n || {});

    function errorText(data) {
        var detail = (data && data.errors && data.errors[0] && data.errors[0].message) || i18n.saveFailed;
        return i18n.errorWithMessage.replace('%(message)s', detail);
    }

    return {
        projectKey: initial.projectKey || '',
        enabled: initial.enabled === true,
        threshold: initial.threshold,
        runLimit: initial.runLimit,
        provider: initial.provider || null,
        baseUrl: (initial.provider && initial.provider.base_url) || '',
        model: (initial.provider && initial.provider.model) || '',
        providerName: (initial.provider && initial.provider.provider) || 'openai',
        apiKey: '',
        removeKey: false,
        saving: false,
        message: '',
        providerMessage: '',

        options(method, payload) {
            var options = {method: method, headers: {'Content-Type': 'application/json'}};
            if (payload !== undefined) {
                options.body = JSON.stringify(payload);
            }
            return options;
        },

        async save() {
            this.saving = true;
            this.message = '';
            try {
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/', this.options('PATCH', {
                    concept_extraction: this.enabled,
                    concept_confidence_threshold: Number(this.threshold),
                    concept_extraction_run_limit: parseInt(this.runLimit, 10)
                }));
                this.message = res.ok ? i18n.saved : errorText(await res.json());
                if (!res.ok) {
                    this.enabled = false;
                }
            } catch (_e) {
                this.message = i18n.connectFailed;
            }
            this.saving = false;
        },

        async runNow() {
            this.saving = true;
            this.message = '';
            try {
                var res = await spFetch(
                    '/api/v1/projects/' + this.projectKey + '/concept-extraction/run/', this.options('POST')
                );
                if (res.ok) {
                    var data = await res.json();
                    this.message = data.queued ? i18n.queued : i18n.notQueued;
                } else {
                    this.message = errorText(await res.json());
                }
            } catch (_e) {
                this.message = i18n.connectFailed;
            }
            this.saving = false;
        },

        async saveProvider() {
            this.saving = true;
            this.providerMessage = '';
            var payload = {base_url: this.baseUrl, model: this.model, provider: this.providerName || 'openai'};
            if (this.removeKey) {
                payload.api_key = '';
            } else if (this.apiKey) {
                payload.api_key = this.apiKey;
            }
            try {
                var res = await spFetch(
                    '/api/v1/projects/' + this.projectKey + '/ai-provider/', this.options('PUT', payload)
                );
                var data = await res.json();
                if (res.ok) {
                    this.provider = data;
                    this.apiKey = '';
                    this.removeKey = false;
                    this.providerMessage = i18n.providerSaved;
                } else {
                    this.providerMessage = errorText(data);
                }
            } catch (_e) {
                this.providerMessage = i18n.connectFailed;
            }
            this.saving = false;
        },

        async removeProvider() {
            if (!window.confirm(i18n.confirmRemove)) {
                return;
            }
            this.saving = true;
            this.providerMessage = '';
            try {
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/ai-provider/', this.options('DELETE'));
                if (res.ok) {
                    this.provider = null;
                    this.enabled = false;
                    this.providerMessage = i18n.providerRemoved;
                } else {
                    this.providerMessage = errorText(await res.json());
                }
            } catch (_e) {
                this.providerMessage = i18n.connectFailed;
            }
            this.saving = false;
        }
    };
}
