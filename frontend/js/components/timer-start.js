// "Start timer" on the issue page. data-running says whether the user's timer
// runs nowhere ('none'), on this issue ('here') or elsewhere ('other'); in the
// last case starting first asks whether to log the running timer (the server
// logs it with the default activity before it starts the new one) or to
// discard it. Configured through data attributes so it works with the Alpine
// CSP build.

import { readI18n } from './header-timer';

export function timerStart() {
    return {
        i18n: {},
        projectId: 0,
        issueId: 0,
        running: 'none',
        confirming: false,
        busy: false,
        error: '',

        init() {
            var data = this.$el.dataset;
            this.i18n = readI18n(this.$el, {
                startFailed: 'Could not start the timer.',
                connectFailed: 'Unable to connect.'
            });
            this.projectId = parseInt(data.projectId, 10) || 0;
            this.issueId = parseInt(data.issueId, 10) || 0;
            this.running = data.running || 'none';
        },

        get hasError() { return this.error !== ''; },

        start() {
            this.error = '';
            if (this.running === 'other') {
                this.confirming = true;
                return;
            }
            return this.startNow();
        },

        cancel() {
            this.confirming = false;
            this.error = '';
        },

        logAndStart() {
            // Starting stops the running timer and logs it (default activity).
            return this.startNow();
        },

        async discardAndStart() {
            this.busy = true;
            this.error = '';
            try {
                var res = await spFetch('/api/v1/timer/discard/', {method: 'POST'});
                // 404: it was stopped meanwhile, so there is nothing left to discard.
                if (!res.ok && res.status !== 404) {
                    this.error = await this._message(res);
                    this.busy = false;
                    return;
                }
            } catch (_e) {
                this.error = this.i18n.connectFailed;
                this.busy = false;
                return;
            }
            await this.startNow();
        },

        async startNow() {
            this.busy = true;
            this.error = '';
            try {
                var res = await spFetch('/api/v1/timer/start/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({project_id: this.projectId, issue_id: this.issueId})
                });
                if (res.ok) {
                    window.location.reload();
                    return;
                }
                this.error = await this._message(res);
            } catch (_e) {
                this.error = this.i18n.connectFailed;
            }
            this.busy = false;
        },

        async _message(res) {
            try {
                var data = await res.json();
                return (data.errors && data.errors[0] && data.errors[0].message) || this.i18n.startFailed;
            } catch (_e) {
                return this.i18n.startFailed;
            }
        }
    };
}
