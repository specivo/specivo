export function issueAutocomplete(initial) {
        return {
            query: initial.value || '',
            results: [],
            showResults: false,
            selectedKey: initial.value || '',
            loading: false,
            debounceTimer: null,

            search() {
                clearTimeout(this.debounceTimer);
                this.selectedKey = '';
                if (this.query.length < 1) {
                    this.results = [];
                    this.showResults = false;
                    return;
                }
                var self = this;
                this.debounceTimer = setTimeout(async function () {
                    self.loading = true;
                    try {
                        var res = await spFetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(self.query) + '&limit=8');
                        if (res.ok) {
                            self.results = await res.json();
                            self.showResults = self.results.length > 0;
                        }
                    } catch (_e) {}
                    self.loading = false;
                }, 250);
            },

            select(item) {
                this.query = item.key;
                this.selectedKey = item.key;
                this.showResults = false;
                this.$dispatch('issue-selected', {key: item.key, subject: item.subject});
            }
        };
    }
