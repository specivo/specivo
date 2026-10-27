/* Admin > Single sign-on > provider form.
 *
 * One component for both "new" (provider === null) and "edit". It talks to
 * the admin JSON API under /api/v1/admin/sso/providers/. The client secret
 * is write-only: the page never receives it, an empty secret field means
 * "keep the stored one", and the status badge comes from the API
 * (client_secret_status).
 */

var LIST_FIELDS = ['allowed_email_domains', 'required_groups', 'admin_groups'];
var CLAIM_FIELDS = ['username_claim', 'email_claim', 'name_claim', 'groups_claim', 'locale_claim'];

function linesToList(text) {
    return (text || '').split('\n').map(function (s) { return s.trim(); }).filter(function (s) { return s; });
}

function formFromProvider(p, extraKeys) {
    var claims = (p && p.claim_config) || {};
    var form = {
        slug: p ? p.slug : '',
        display_name: p ? p.display_name : '',
        enabled: p ? p.enabled : false,
        position: p ? p.position : 0,
        issuer: p ? p.issuer : '',
        discovery_url: (p && p.discovery_url) || '',
        client_id: p ? p.client_id : '',
        token_endpoint_auth_method: p ? p.token_endpoint_auth_method : 'client_secret_basic',
        scopes: p ? p.scopes : 'openid profile email',
        use_userinfo: !!claims.use_userinfo,
        allow_jit_provisioning: p ? p.allow_jit_provisioning : false,
        link_policy: p ? p.link_policy : 'manual',
        trust_email_verified: p ? p.trust_email_verified : false,
        sync_admin_flag: p ? p.sync_admin_flag : false,
        sync_groups: p ? p.sync_groups : false,
        deactivate_on_access_loss: p ? p.deactivate_on_access_loss : false,
        rp_logout_enabled: p ? p.rp_logout_enabled : true,
        backchannel_logout_enabled: p ? p.backchannel_logout_enabled : false,
        session_max_age_hours: p ? p.session_max_age_hours : 24,
        extra: {}
    };
    CLAIM_FIELDS.forEach(function (f) { form[f] = claims[f] || ''; });
    LIST_FIELDS.forEach(function (f) { form[f] = ((p && p[f]) || []).join('\n'); });
    (extraKeys || []).forEach(function (k) {
        form.extra[k] = (p && p.extra_auth_params && p.extra_auth_params[k]) || '';
    });
    return form;
}

export function adminSsoProvider(initial) {
    var i18n = initial.i18n || {};
    var extraKeys = initial.extraParamKeys || [];

    return {
        provider: initial.provider || null,
        userGroups: initial.userGroups || [],
        form: formFromProvider(initial.provider, extraKeys),
        extraKeys: extraKeys,

        secretMode: initial.provider && initial.provider.client_secret_env ? 'env' : 'stored',
        secretInput: '',
        envInput: (initial.provider && initial.provider.client_secret_env) || '',
        clearSecret: false,

        saving: false,
        saveError: '',
        saveMessage: '',

        get isNew() { return !this.provider; },
        get isPublicClient() { return this.form.token_endpoint_auth_method === 'none'; },

        get redirectUri() {
            return initial.callbackTemplate.replace('__slug__', this.form.slug || '{slug}');
        },
        get backchannelUri() {
            return initial.backchannelTemplate.replace('__slug__', this.form.slug || '{slug}');
        },

        get secretStatusLabel() {
            if (!this.provider) return '';
            return (i18n.secretStatus || {})[this.provider.client_secret_status] || this.provider.client_secret_status;
        },
        get secretStatusOk() {
            if (!this.provider) return false;
            var s = this.provider.client_secret_status;
            return s === 'configured' || s === 'env' || s === 'not_required';
        },

        copied: '',
        copy(which) {
            var text = which === 'backchannel' ? this.backchannelUri : this.redirectUri;
            if (navigator.clipboard) navigator.clipboard.writeText(text);
            this.copied = which;
            var self = this;
            setTimeout(function () { self.copied = ''; }, 2000);
        },

        _readError(err, fallback) {
            var e = err.errors && err.errors[0];
            if (!e) return err.detail || fallback;
            return e.field ? e.field + ': ' + e.message : e.message;
        },

        _payload() {
            var f = this.form;
            var body = {
                slug: f.slug.trim(),
                display_name: f.display_name.trim(),
                enabled: !!f.enabled,
                position: parseInt(f.position, 10) || 0,
                issuer: f.issuer.trim(),
                discovery_url: f.discovery_url.trim() || null,
                client_id: f.client_id.trim(),
                token_endpoint_auth_method: f.token_endpoint_auth_method,
                scopes: f.scopes.trim(),
                claim_config: { use_userinfo: !!f.use_userinfo },
                allow_jit_provisioning: !!f.allow_jit_provisioning,
                link_policy: f.link_policy,
                trust_email_verified: !!f.trust_email_verified,
                sync_admin_flag: !!f.sync_admin_flag,
                sync_groups: !!f.sync_groups,
                deactivate_on_access_loss: !!f.deactivate_on_access_loss,
                rp_logout_enabled: !!f.rp_logout_enabled,
                backchannel_logout_enabled: !!f.backchannel_logout_enabled,
                session_max_age_hours: parseInt(f.session_max_age_hours, 10) || 24,
                extra_auth_params: {}
            };
            CLAIM_FIELDS.forEach(function (k) {
                var v = (f[k] || '').trim();
                if (v) body.claim_config[k] = v;
            });
            LIST_FIELDS.forEach(function (k) { body[k] = linesToList(f[k]); });
            this.extraKeys.forEach(function (k) {
                var v = (f.extra[k] || '').trim();
                if (v) body.extra_auth_params[k] = v;
            });

            // Secret: only sent when the admin typed or changed something.
            if (this.isPublicClient) {
                if (!this.isNew && this.provider.client_secret_configured) body.clear_client_secret = true;
            } else if (this.clearSecret) {
                body.clear_client_secret = true;
            } else if (this.secretMode === 'stored') {
                if (this.secretInput) body.client_secret = this.secretInput;
            } else {
                var env = this.envInput.trim();
                var current = this.provider ? this.provider.client_secret_env || '' : '';
                if (env && env !== current) body.client_secret_env = env;
            }
            return body;
        },

        async save() {
            this.saving = true;
            this.saveError = '';
            this.saveMessage = '';
            var url = '/api/v1/admin/sso/providers/' + (this.isNew ? '' : this.provider.id + '/');
            var res = await spFetch(url, {
                method: this.isNew ? 'POST' : 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this._payload())
            });
            if (res.ok) {
                var saved = await res.json();
                this.secretInput = '';
                this.clearSecret = false;
                if (this.isNew) {
                    window.location.href = '/admin/sso/' + saved.id + '/';
                    return;
                }
                this.provider = saved;
                this.envInput = saved.client_secret_env || '';
                // Disabling the provider ends its SSO sessions: say how many.
                var revoked = parseInt(res.headers.get('X-Revoked-Sessions') || '0', 10);
                this.saveMessage = revoked > 0
                    ? (i18n.savedSessionsEnded || 'Saved. Sessions ended: {count}.').replace('{count}', String(revoked))
                    : (i18n.saved || 'Saved.');
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.saveError = this._readError(err, i18n.saveFailed || 'Failed to save.');
            }
            this.saving = false;
        },

        // ---------------------------------------------------------------
        // Self-test
        // ---------------------------------------------------------------

        testing: false,
        testResult: null,
        testError: '',

        async runTest() {
            if (this.isNew) return;
            this.testing = true;
            this.testError = '';
            this.testResult = null;
            var res = await spFetch('/api/v1/admin/sso/providers/' + this.provider.id + '/test/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            if (res.ok) {
                this.testResult = await res.json();
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.testError = this._readError(err, i18n.testFailed || 'The test could not run.');
            }
            this.testing = false;
        },

        get testSummary() {
            var r = this.testResult;
            if (!r) return '';
            if (r.ok) return i18n.testOk || 'Connection works.';
            var label = (i18n.testErrors || {})[r.error_code] || r.error_code;
            return label + ' (' + r.failed_step + ': ' + r.error_reason + ')';
        },

        yesNo(value) {
            if (value === null || value === undefined) return '-';
            return value ? (i18n.yes || 'Yes') : (i18n.no || 'No');
        },

        warningText(code) {
            return (i18n.warnings || {})[code] || code;
        },

        listText(values) {
            return (values || []).join(', ') || '-';
        },

        // ---------------------------------------------------------------
        // Group mappings
        // ---------------------------------------------------------------

        mappings: (initial.mappings || []).map(function (m) {
            return { idp_group: m.idp_group, user_group_id: String(m.user_group_id) };
        }),
        mappingsSaving: false,
        mappingsError: '',
        mappingsMessage: '',

        addMapping() {
            var first = this.userGroups.length ? String(this.userGroups[0].id) : '';
            this.mappings.push({ idp_group: '', user_group_id: first });
        },

        removeMapping(index) {
            this.mappings.splice(index, 1);
        },

        async saveMappings() {
            this.mappingsSaving = true;
            this.mappingsError = '';
            this.mappingsMessage = '';
            var items = this.mappings
                .filter(function (m) { return m.idp_group.trim() && m.user_group_id; })
                .map(function (m) { return { idp_group: m.idp_group.trim(), user_group_id: parseInt(m.user_group_id, 10) }; });
            var res = await spFetch('/api/v1/admin/sso/providers/' + this.provider.id + '/group-mappings/', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(items)
            });
            if (res.ok) {
                var saved = await res.json();
                this.mappings = saved.map(function (m) {
                    return { idp_group: m.idp_group, user_group_id: String(m.user_group_id) };
                });
                this.mappingsMessage = i18n.saved || 'Saved.';
            } else {
                var err = await res.json().catch(function () { return {}; });
                this.mappingsError = this._readError(err, i18n.saveFailed || 'Failed to save.');
            }
            this.mappingsSaving = false;
        },

        // ---------------------------------------------------------------
        // Delete
        // ---------------------------------------------------------------

        showDelete: false,
        deleting: false,
        deleteError: '',
        forceDelete: false,

        get hasIdentities() {
            return !!(this.provider && this.provider.identity_count > 0);
        },

        openDelete() {
            this.deleteError = '';
            this.forceDelete = false;
            this.showDelete = true;
        },

        async confirmDelete() {
            this.deleting = true;
            this.deleteError = '';
            var url = '/api/v1/admin/sso/providers/' + this.provider.id + '/' + (this.forceDelete ? '?force=true' : '');
            var res = await spFetch(url, { method: 'DELETE', headers: { 'Content-Type': 'application/json' } });
            if (res.ok || res.status === 204) {
                window.location.href = '/admin/sso/';
                return;
            }
            var err = await res.json().catch(function () { return {}; });
            this.deleteError = this._readError(err, i18n.deleteFailed || 'Failed to delete.');
            this.deleting = false;
        }
    };
}
