export function backlogPage(initial) {
        return {
            showCreateModal: false,
            name: '',
            goal: '',
            start_date: '',
            end_date: '',
            projectKey: initial.projectKey || '',

            async createSprint() {
                var res = await spFetch('/api/v1/projects/' + this.projectKey + '/sprints/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: this.name,
                        goal: this.goal || null,
                        start_date: this.start_date || null,
                        end_date: this.end_date || null
                    })
                });
                if (res.ok) window.location.reload();
            }
        };
    }
