// "Start timer" on the issue page. data-running says whether the user's timer
// runs nowhere ('none'), on this issue ('here') or elsewhere ('other'); in the
// last case starting first asks whether to log the running timer (the server
// logs it with the default activity before it starts the new one) or to
// discard it. Configured through data attributes so it works with the Alpine
// CSP build.
//
// The server reports what became of the running timer (``replaced``: logged,
// or discarded when the user may no longer log time where it ran). The page
// reloads after a start, so the notice is kept in sessionStorage for the
// reloaded page of the same issue to show once.

import { readI18n } from './header-timer';

var NOTICE_KEY = 'sp-timer-replaced';

export function replacedNotice(replaced, i18n) {
    if (!replaced) return '';
    var target = replaced.issue_key || replaced.project_key || '';
    if (replaced.outcome === 'logged') {
        return i18n.replacedLogged.replace('%(target)s', target).replace('%(hours)s', String(replaced.hours));
    }
    return i18n.replacedDiscarded.replace('%(target)s', target);
}

function storeNotice(issueId, text) {
    try {
        window.sessionStorage.setItem(NOTICE_KEY, JSON.stringify({issueId: issueId, text: text}));
    } catch (_e) { /* storage unavailable: the notice is skipped */ }
}

function takeNotice(issueId) {
    try {
        var raw = window.sessionStorage.getItem(NOTICE_KEY);
        if (!raw) return '';
        window.sessionStorage.removeItem(NOTICE_KEY);
        var data = JSON.parse(raw);
        return data && data.issueId === issueId ? String(data.text || '') : '';
    } catch (_e) {
        return '';
    }
}

export function timerStart() {
    return {
        i18n: {},
        projectId: 0,
        issueId: 0,
        running: 'none',
        confirming: false,
        busy: false,
        error: '',
        notice: '',

        init() {
            var data = this.$el.dataset;
            this.i18n = readI18n(this.$el, {
                startFailed: 'Could not start the timer.',
                connectFailed: 'Unable to connect.',
                replacedLogged: 'The timer on %(target)s was stopped and %(hours)s h were logged with the default activity.',
                replacedDiscarded: 'The timer on %(target)s was discarded without logging time: you may no longer log time there.'
            });
            this.projectId = parseInt(data.projectId, 10) || 0;
            this.issueId = parseInt(data.issueId, 10) || 0;
            this.running = data.running || 'none';
            this.notice = takeNotice(this.issueId);
        },

        get hasError() { return this.error !== ''; },
        get hasNotice() { return this.notice !== ''; },

        dismissNotice() { this.notice = ''; },

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
                    var started = await res.json().catch(function () { return {}; });
                    var text = replacedNotice(started.replaced, this.i18n);
                    if (text) storeNotice(this.issueId, text);
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
