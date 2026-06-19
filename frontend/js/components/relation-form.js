export function relationForm(initial) {
        var _searchTimer = null;
        return {
            showForm: false,
            issueToKey: '',
            relationType: 'relates',
            submitting: false,
            error: '',
            issueKey: initial.issueKey || '',

            // Autocomplete state
            suggestions: [],
            showSuggestions: false,
            highlightIndex: -1,
            searching: false,

            init() {
                this.$watch('issueToKey', function (val) {
                    clearTimeout(_searchTimer);
                    var query = (val || '').trim();
                    if (query.length < 2) {
                        this.suggestions = [];
                        this.showSuggestions = false;
                        return;
                    }
                    var self = this;
                    _searchTimer = setTimeout(function () { self.searchIssues(query); }, 300);
                }.bind(this));
            },

            get canSubmit() {
                return !this.submitting && this.issueToKey.trim() !== '';
            },

            async searchIssues(query) {
                this.searching = true;
                try {
                    var res = await spFetch('/api/v1/issues/autocomplete/?q=' + encodeURIComponent(query) + '&limit=8');
                    if (res.ok) {
                        var data = await res.json();
                        this.suggestions = data
                            .filter(function (r) { return r.key !== initial.issueKey; });
                        this.showSuggestions = this.suggestions.length > 0;
                        this.highlightIndex = -1;
                    }
                } catch (_e) {
                    /* search failed silently */
                }
                this.searching = false;
            },

            selectSuggestion(s) {
                this.issueToKey = s.key;
                this.showSuggestions = false;
                this.suggestions = [];
            },

            onKeydown(event) {
                if (!this.showSuggestions) return;
                if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    this.highlightIndex = Math.min(this.highlightIndex + 1, this.suggestions.length - 1);
                } else if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    this.highlightIndex = Math.max(this.highlightIndex - 1, 0);
                } else if (event.key === 'Enter' && this.highlightIndex >= 0) {
                    event.preventDefault();
                    this.selectSuggestion(this.suggestions[this.highlightIndex]);
                } else if (event.key === 'Escape') {
                    this.showSuggestions = false;
                }
            },

            closeSuggestions() {
                // Delay to allow click on suggestion to fire first
                var self = this;
                setTimeout(function () { self.showSuggestions = false; }, 200);
            },

            async submit() {
                this.submitting = true;
                this.error = '';
                try {
                    var res = await spFetch('/api/v1/issues/' + this.issueKey + '/relations/', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            issue_to_key: this.issueToKey,
                            relation_type: this.relationType
                        })
                    });
                    if (res.ok) {
                        window.location.reload();
                    } else {
                        var data = await res.json();
                        this.error = (data.errors && data.errors[0] && data.errors[0].message) || 'Failed to add relation';
                    }
                } catch (_e) {
                    this.error = 'Unable to connect.';
                }
                this.submitting = false;
            }
        };
    }
