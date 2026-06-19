export function watcherToggle(initial) {
        return {
            watching: initial.watching || false,
            issueKey: initial.issueKey || '',
            async toggle() {
                var method = this.watching ? 'DELETE' : 'POST';
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/watchers/', {
                    method: method,
                    headers: {'Content-Type': 'application/json'}
                });
                if (res.ok || res.status === 204) {
                    this.watching = !this.watching;
                    window.location.reload();
                }
            }
        };
    }
