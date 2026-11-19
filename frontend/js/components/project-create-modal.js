import { apiErrorMessage } from '../lib/api-error';
import { projectCreateBody } from '../lib/project-form';

export function projectCreateModal(initial) {
        initial = initial || {};
        var i18n = Object.assign({
            createFailed: 'Failed to create project',
            connectFailed: 'Unable to connect.'
        }, initial.i18n || {});

        return {
            name: '',
            identifier: '',
            key: '',
            description: '',
            parentKey: initial.parentKey || '',
            color: (initial.colors && initial.colors[0]) || '#c49a3c',
            colors: initial.colors || [],
            allProjects: initial.allProjects || [],
            moduleWiki: true,
            moduleTime: true,
            saving: false,
            error: '',

            slugify: function (v) {
                return v.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').substring(0, 50);
            },

            keyify: function (v) {
                return v.toUpperCase().replace(/[^A-Z0-9]/g, '').substring(0, 8);
            },

            onNameInput: function () {
                this.identifier = this.slugify(this.name);
                this.key = this.keyify(this.name);
            },

            /* Wait (at most about 1.5 s) until the new project's page is served, so the redirect never lands on a 404. */
            async _waitUntilServed(url) {
                for (var i = 0; i < 5; i++) {
                    var check = await spFetch(url, {method: 'HEAD'});
                    if (check.ok) break;
                    await new Promise(function (r) { setTimeout(r, 300); });
                }
            },

            async submit() {
                this.saving = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/projects/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(projectCreateBody(this))
                    });
                    if (res.ok) {
                        var data = await res.json();
                        var url = '/projects/' + data.key + '/';
                        await this._waitUntilServed(url);
                        window.location.href = url;
                    } else {
                        this.error = apiErrorMessage(await res.json(), i18n.createFailed);
                    }
                } catch (_e) {
                    this.error = i18n.connectFailed;
                }
                this.saving = false;
            }
        };
    }
