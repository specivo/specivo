export function sprintEdit(initial) {
        // The day and week counts are computed here from the live date inputs,
        // so plural agreement cannot come from the template; translations use a
        // count-neutral form where a language needs more than two forms.
        var i18n = Object.assign({
            invalidRange: 'Invalid range',
            oneDay: '1 day',
            nDays: '%(count)s days',
            oneWeek: '1 week',
            nWeeks: '%(count)s weeks',
            durationWeeks: '%(days)s (%(weeks)s)',
            durationApprox: '%(days)s (~%(weeks)s)',
            saveFailed: 'Failed to save changes.'
        }, initial.i18n || {});

        function daysLabel(n) {
            return n === 1 ? i18n.oneDay : i18n.nDays.replace('%(count)s', n);
        }

        function weeksLabel(n) {
            return n === 1 ? i18n.oneWeek : i18n.nWeeks.replace('%(count)s', n);
        }

        return {
            startDate: initial.startDate || '',
            endDate: initial.endDate || '',
            saving: false,
            saved: false,
            error: '',

            get duration() {
                if (!this.startDate || !this.endDate) return '';
                var s = new Date(this.startDate);
                var e = new Date(this.endDate);
                var days = Math.round((e - s) / (1000 * 60 * 60 * 24));
                if (days < 0) return i18n.invalidRange;
                var weeks = Math.floor(days / 7);
                if (weeks > 0) {
                    var template = days % 7 === 0 ? i18n.durationWeeks : i18n.durationApprox;
                    return template.replace('%(days)s', daysLabel(days)).replace('%(weeks)s', weeksLabel(weeks));
                }
                return daysLabel(days);
            },

            onSaveResponse(event) {
                var self = this;
                this.saving = false;
                if (event.detail.successful) {
                    this.saved = true;
                    setTimeout(function () { self.saved = false; }, 3000);
                } else {
                    this.error = i18n.saveFailed;
                }
            }
        };
    }
