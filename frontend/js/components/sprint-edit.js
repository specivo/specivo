export function sprintEdit(initial) {
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
                if (days < 0) return 'Invalid range';
                var weeks = Math.floor(days / 7);
                if (weeks > 0 && days % 7 === 0) return days + ' days (' + weeks + ' week' + (weeks > 1 ? 's' : '') + ')';
                if (weeks > 0) return days + ' days (~' + weeks + ' week' + (weeks > 1 ? 's' : '') + ')';
                return days + ' day' + (days !== 1 ? 's' : '');
            },

            onSaveResponse(event) {
                var self = this;
                this.saving = false;
                if (event.detail.successful) {
                    this.saved = true;
                    setTimeout(function () { self.saved = false; }, 3000);
                } else {
                    this.error = 'Failed to save changes.';
                }
            }
        };
    }
