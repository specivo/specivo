export function versionIssues(initial) {
        return {
            filter: 'all',
            issues: (initial && initial.issues) || [],

            assigneeInitials(issue) {
                if (!issue.assignee) return '';
                return issue.assignee.substring(0, 2).toUpperCase();
            },

            get filtered() {
                if (this.filter === 'open') return this.issues.filter(function (i) { return i.is_open; });
                if (this.filter === 'closed') return this.issues.filter(function (i) { return !i.is_open; });
                return this.issues;
            }
        };
    }
