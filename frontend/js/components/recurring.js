function _blankRecurringForm() {
        return {
            name: '',
            enabled: true,
            freq: 'weekly',
            rrule_interval: 1,
            byday: [],
            bymonthday: '',
            bysetpos: '',
            anchor_mode: 'fixed',
            base_date_strategy: 'scheduled',
            timezone: 'UTC',
            creation_lead_time_days: 30,
            dtstart: '',
            template_tracker_id: null,
            template_status_id: null,
            template_priority_id: null,
            template_assigned_to_id: null,
            template_subject: '',
            template_description: '',
            carry_over: {description: true, assignee: true, metadata: true, estimated_hours: true},
            reset_checklist: true,
            rotation_user_ids: [],
            lock_version: 0
        };
    }

function _patternToForm(p) {
        var f = _blankRecurringForm();
        f.name = p.name || '';
        f.enabled = p.enabled !== false;
        f.freq = p.freq || 'weekly';
        f.rrule_interval = p.rrule_interval || 1;
        f.byday = (p.byday || []).slice();
        f.bymonthday = (p.bymonthday || []).join(', ');
        f.bysetpos = (p.bysetpos || []).join(', ');
        f.anchor_mode = p.anchor_mode || 'fixed';
        f.base_date_strategy = p.base_date_strategy || 'scheduled';
        f.timezone = p.timezone || 'UTC';
        f.creation_lead_time_days = p.creation_lead_time_days || 30;
        f.dtstart = p.dtstart ? p.dtstart.slice(0, 16) : '';
        f.template_tracker_id = p.template_tracker_id || null;
        f.template_status_id = p.template_status_id || null;
        f.template_priority_id = p.template_priority_id || null;
        f.template_assigned_to_id = p.template_assigned_to_id || null;
        f.template_subject = p.template_subject || '';
        f.template_description = p.template_description || '';
        f.carry_over = Object.assign(f.carry_over, p.carry_over || {});
        f.reset_checklist = p.reset_checklist !== false;
        f.rotation_user_ids = (p.assignee_rotation && p.assignee_rotation.user_ids) ? p.assignee_rotation.user_ids.slice() : [];
        f.lock_version = (typeof p.lock_version === 'number') ? p.lock_version : 0;
        return f;
    }

function _parseIntList(raw) {
        if (!raw) return null;
        var parts = String(raw).split(',');
        var out = [];
        for (var i = 0; i < parts.length; i++) {
            var n = parseInt(parts[i].trim(), 10);
            if (!isNaN(n)) out.push(n);
        }
        return out.length > 0 ? out : null;
    }

function _formToPayload(form, includeLock) {
        var dtstart = form.dtstart;
        if (dtstart && dtstart.length === 16) {
            dtstart = dtstart + ':00';
        }
        var rotation = null;
        if (form.rotation_user_ids && form.rotation_user_ids.length > 0) {
            rotation = {user_ids: form.rotation_user_ids, strategy: 'round_robin'};
        }
        var payload = {
            name: form.name.trim(),
            enabled: form.enabled,
            freq: form.freq,
            rrule_interval: form.rrule_interval || 1,
            // byday applies to weekly schedules and to monthly/yearly
            // nth-weekday rules (combined with bysetpos, e.g. "2nd Tuesday").
            byday: (form.freq !== 'daily' && form.byday.length > 0) ? form.byday : null,
            bymonthday: form.freq === 'monthly' ? _parseIntList(form.bymonthday) : null,
            bysetpos: (form.freq === 'monthly' || form.freq === 'yearly') ? _parseIntList(form.bysetpos) : null,
            anchor_mode: form.anchor_mode,
            base_date_strategy: form.base_date_strategy,
            timezone: form.timezone || 'UTC',
            creation_lead_time_days: form.creation_lead_time_days || 30,
            dtstart: dtstart,
            template_tracker_id: form.template_tracker_id,
            template_status_id: form.template_status_id || null,
            template_priority_id: form.template_priority_id || null,
            template_assigned_to_id: form.template_assigned_to_id || null,
            template_subject: form.template_subject.trim(),
            template_description: form.template_description || null,
            carry_over: form.carry_over,
            reset_checklist: form.reset_checklist,
            assignee_rotation: rotation
        };
        if (includeLock) {
            payload.lock_version = form.lock_version;
        }
        return payload;
    }

function _scheduleLabel(p) {
        var n = p.rrule_interval || 1;
        var plural = {daily: 'days', weekly: 'weeks', monthly: 'months', yearly: 'years'};
        var single = {daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', yearly: 'Yearly'};
        if (n > 1) return 'Every ' + n + ' ' + (plural[p.freq] || p.freq);
        return single[p.freq] || p.freq;
    }

function _fmtOccurrence(iso) {
        if (!iso) return '';
        return iso.replace('T', ' ').slice(0, 16);
    }

export function recurringPatternForm(initial) {
        var labels = initial.labels || {};
        var isEdit = initial.mode === 'edit';
        var form;
        if (isEdit && initial.pattern) {
            form = _patternToForm(initial.pattern);
        } else {
            form = _blankRecurringForm();
            if (initial.trackers && initial.trackers.length > 0) {
                form.template_tracker_id = initial.trackers[0].id;
            }
            // New patterns default to the timezone resolved from the user's
            // profile / instance settings (server-provided), fallback UTC.
            form.timezone = initial.defaultTimezone || 'UTC';
        }
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            members: initial.members || [],
            trackers: initial.trackers || [],
            statuses: initial.statuses || [],
            priorities: initial.priorities || [],
            timezones: initial.timezones || [],
            labels: labels,
            editing: isEdit,
            patternId: (isEdit && initial.pattern) ? initial.pattern.id : null,
            saving: false,
            form: form,
            formError: '',
            previewForm: [],
            previewLoading: false,
            previewError: false,

            // Timezone combobox state.
            tzOpen: false,
            tzQuery: '',
            tzActiveIndex: 0,

            init: function () {
                if (this.editing) this.refreshFormPreview();
            },

            /**
             * Case-insensitive filter over the IANA timezone list. Matches on the
             * zone name (e.g. "Europe/London"); the full name already carries the
             * region/city offset hint, so a plain substring match is enough.
             */
            /** Display label for a timezone: IANA id with underscores shown as
             *  spaces (e.g. "America/El_Aaiun" -> "America/El Aaiun"). The stored
             *  value (form.timezone) always keeps the canonical underscore form. */
            tzLabel: function (tz) {
                return (tz || '').replace(/_/g, ' ');
            },

            tzFiltered: function () {
                // Match underscore-insensitively so typing "El Aaiun" finds
                // "America/El_Aaiun".
                var q = this.tzQuery.trim().toLowerCase().replace(/_/g, ' ');
                if (!q) return this.timezones;
                var out = [];
                for (var i = 0; i < this.timezones.length; i++) {
                    var hay = this.timezones[i].toLowerCase().replace(/_/g, ' ');
                    if (hay.indexOf(q) !== -1) {
                        out.push(this.timezones[i]);
                    }
                }
                return out;
            },

            tzToggle: function () {
                if (this.tzOpen) {
                    this.tzClose();
                } else {
                    this.tzOpenPanel();
                }
            },

            tzOpenPanel: function () {
                this.tzOpen = true;
                this.tzQuery = '';
                // Position the active highlight on the current selection.
                var list = this.tzFiltered();
                var sel = list.indexOf(this.form.timezone);
                this.tzActiveIndex = sel === -1 ? 0 : sel;
                var self = this;
                this.$nextTick(function () {
                    if (self.$refs.tzSearch) self.$refs.tzSearch.focus();
                });
            },

            tzClose: function () {
                if (!this.tzOpen) return;
                this.tzOpen = false;
                // Return focus to the trigger for keyboard users.
                var trigger = this.$el.querySelector('.sp-tz-trigger');
                if (trigger) trigger.focus();
            },

            tzSelect: function (tz) {
                this.form.timezone = tz;
                this.refreshFormPreview();
                this.tzClose();
            },

            /** Keyboard on the trigger button: open on arrow/enter/space. */
            tzTriggerKeydown: function (e) {
                if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.tzOpenPanel();
                }
            },

            /** Keyboard inside the search input: navigate and choose options. */
            tzSearchKeydown: function (e) {
                var list = this.tzFiltered();
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    if (list.length) this.tzActiveIndex = (this.tzActiveIndex + 1) % list.length;
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    if (list.length) this.tzActiveIndex = (this.tzActiveIndex - 1 + list.length) % list.length;
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (list.length) this.tzSelect(list[this.tzActiveIndex]);
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    this.tzClose();
                }
                this.$nextTick(this._tzScrollActive.bind(this));
            },

            /** Keep the active option in view while arrow-navigating. */
            _tzScrollActive: function () {
                var opt = this.$el.querySelector('#sp-tz-opt-' + this.tzActiveIndex);
                if (opt && opt.scrollIntoView) opt.scrollIntoView({ block: 'nearest' });
            },

            get canSaveForm() {
                return !this.saving
                    && this.form.name.trim() !== ''
                    && this.form.template_subject.trim() !== ''
                    && !!this.form.template_tracker_id
                    && !!this.form.dtstart;
            },

            /** Full weekday name for the weekday-button aria-label. */
            weekdayName: function (code) {
                var names = this.labels.weekdays || {};
                return names[code] || code;
            },

            toggleByday: function (day) {
                var idx = this.form.byday.indexOf(day);
                if (idx === -1) {
                    this.form.byday.push(day);
                } else {
                    this.form.byday.splice(idx, 1);
                }
                this.refreshFormPreview();
            },

            /**
             * Live preview: the occurrences endpoint requires a persisted
             * pattern, so we query it only when editing. For a brand-new pattern
             * the live count appears after the first save.
             */
            refreshFormPreview: async function () {
                if (!this.editing || !this.patternId) {
                    this.previewForm = [];
                    this.previewError = false;
                    return;
                }
                this.previewLoading = true;
                this.previewError = false;
                try {
                    var days = this.form.creation_lead_time_days || 30;
                    var url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.patternId + '/occurrences/?days=' + days;
                    var res = await spFetch(url);
                    if (res.ok) {
                        var data = await res.json();
                        this.previewForm = (data.occurrences || []).slice(0, 5).map(_fmtOccurrence);
                    } else {
                        this.previewError = true;
                    }
                } catch (_e) {
                    this.previewError = true;
                }
                this.previewLoading = false;
            },

            saveForm: async function () {
                // Double-submit guard: bail if a request is already in flight.
                if (this.saving || !this.canSaveForm) return;
                this.saving = true;
                this.formError = '';
                try {
                    var url, method;
                    if (this.editing) {
                        url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.patternId + '/';
                        method = 'PATCH';
                    } else {
                        url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/';
                        method = 'POST';
                    }
                    var payload = _formToPayload(this.form, this.editing);
                    var res = await spFetch(url, {
                        method: method,
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        // Navigate to the saved pattern's detail page. Stay in the
                        // saving state so the button cannot be re-submitted while
                        // the browser navigates away.
                        var saved = await res.json();
                        var id = saved.id || this.patternId;
                        window.location = '/projects/' + this.projectKey + '/recurring-patterns/' + id + '/';
                        return;
                    }
                    if (res.status === 409) {
                        this.formError = this.labels.conflict || 'This pattern was changed by someone else. Reload and try again.';
                    } else {
                        var err = await res.json().catch(function () { return {}; });
                        this.formError = (err.errors && err.errors[0] && err.errors[0].message) || err.detail || this.labels.saveFailed || 'Failed to save pattern.';
                    }
                } catch (_e) {
                    this.formError = this.labels.connectFailed || 'Unable to connect.';
                }
                // Re-enable only on error (on success we navigate away).
                this.saving = false;
            },

            scheduleLabel: function (p) { return _scheduleLabel(p); }
        };
    }

export function recurringPatterns(initial) {
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            patterns: initial.patterns || [],
            message: '',
            messageType: 'success',
            showDeleteModal: false,
            deleting: null,
            deletingBusy: false,

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type || 'success';
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            scheduleLabel: function (p) { return _scheduleLabel(p); },

            toggleEnabled: async function (p) {
                if (!this.canManage) return;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + p.id + '/', {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({enabled: !p.enabled, lock_version: p.lock_version})
                    });
                    if (res.ok) {
                        // Refresh enabled + lock_version from the response so a
                        // subsequent toggle does not hit a stale-version 409.
                        var updated = await res.json();
                        p.enabled = updated.enabled;
                        p.lock_version = updated.lock_version;
                        this.flash('Pattern ' + (p.enabled ? 'enabled' : 'disabled') + '.', 'success');
                    } else {
                        this.flash('Failed to update pattern.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
            },

            confirmDelete: function (p) {
                if (!this.canManage) return;
                this.deleting = p;
                this.showDeleteModal = true;
            },

            doDelete: async function () {
                if (!this.deleting) return;
                this.deletingBusy = true;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.deleting.id + '/', {
                        method: 'DELETE',
                        headers: {'Content-Type': 'application/json'}
                    });
                    if (res.ok || res.status === 204) {
                        var id = this.deleting.id;
                        this.patterns = this.patterns.filter(function (x) { return x.id !== id; });
                        this.showDeleteModal = false;
                        this.deleting = null;
                        this.flash('Pattern deleted.', 'success');
                    } else {
                        this.flash('Failed to delete pattern.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
                this.deletingBusy = false;
            }
        };
    }

export function recurringPatternDetail(initial) {
        return {
            projectKey: initial.projectKey || '',
            canManage: initial.canManage !== false,
            pattern: initial.pattern || {},
            preview: [],
            previewLoading: false,
            previewError: false,
            message: '',
            messageType: 'success',

            init: function () {
                this.loadPreview();
            },

            flash: function (msg, type) {
                this.message = msg;
                this.messageType = type || 'success';
                setTimeout(function () { this.message = ''; }.bind(this), 4000);
            },

            loadPreview: async function () {
                this.previewLoading = true;
                this.previewError = false;
                try {
                    var days = this.pattern.creation_lead_time_days || 30;
                    var url = '/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.pattern.id + '/occurrences/?days=' + days;
                    var res = await spFetch(url);
                    if (res.ok) {
                        var data = await res.json();
                        this.preview = (data.occurrences || []).slice(0, 5).map(_fmtOccurrence);
                    } else {
                        this.previewError = true;
                    }
                } catch (_e) {
                    this.previewError = true;
                }
                this.previewLoading = false;
            },

            skipOccurrence: async function (occurrenceAt) {
                if (!this.canManage) return;
                try {
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/recurring-patterns/' + this.pattern.id + '/skip/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({occurrence_at: occurrenceAt})
                    });
                    if (res.ok) {
                        this.flash('Occurrence skipped.', 'success');
                        window.location.reload();
                    } else {
                        this.flash('Failed to skip occurrence.', 'error');
                    }
                } catch (_e) {
                    this.flash('Unable to connect.', 'error');
                }
            }
        };
    }
