export function timeLogForm(initial) {
        return {
            showForm: false,
            hours: '',
            minutes: '',
            activityId: '',
            spentOn: new Date().toISOString().split('T')[0],
            comments: '',
            submitting: false,
            error: '',
            projectKey: initial.projectKey || '',
            issueId: initial.issueId || 0,

            get totalHours() {
                var h = parseInt(this.hours) || 0;
                var m = parseInt(this.minutes) || 0;
                return h + m / 60;
            },

            async submit() {
                this.submitting = true;
                this.error = '';
                try {
                    var total = this.totalHours;
                    if (total <= 0) {
                        this.error = 'Enter at least 1 minute';
                        this.submitting = false;
                        return;
                    }
                    var res = await spFetch('/api/v1/projects/' + this.projectKey + '/time-entries/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            issue_id: this.issueId,
                            activity_id: parseInt(this.activityId),
                            hours: Math.round(total * 100) / 100,
                            spent_on: this.spentOn,
                            comments: this.comments || null
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Failed to log time';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    }
