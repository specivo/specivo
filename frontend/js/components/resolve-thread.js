export function resolveThread(initial) {
        return {
            showModal: false,
            summary: '',
            issueKey: initial.issueKey || '',
            journalId: initial.journalId || 0,

            async resolve() {
                var res = await spFetch('/api/v1/issues/' + this.issueKey + '/journals/' + this.journalId + '/resolve/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({summary: this.summary})
                });
                if (res.ok) {
                    this.showModal = false;
                    window.location.reload();
                }
            }
        };
    }
