export function adminVersions(initial) {
        return {
            allVersions: (initial && initial.versions) || [],
            projects: (initial && initial.projects) || [],
            filterProject: '',
            filterStatus: '',
            selected: [],

            countByStatus(status) {
                var count = 0;
                for (var i = 0; i < this.allVersions.length; i++) {
                    if (this.allVersions[i].status === status) count++;
                }
                return count;
            },

            get filtered() {
                var self = this;
                return this.allVersions.filter(function (v) {
                    if (self.filterProject && v.project_key !== self.filterProject) return false;
                    if (self.filterStatus && v.status !== self.filterStatus) return false;
                    return true;
                });
            },

            toggleAll: function (e) {
                if (e.target.checked) {
                    this.selected = this.filtered.map(function (v) { return v.id; });
                } else {
                    this.selected = [];
                }
            },

            async bulkAction(newStatus) {
                var self = this;
                var ids = this.selected.slice();
                var promises = [];
                for (var i = 0; i < ids.length; i++) {
                    var ver = this.allVersions.find(function (v) { return v.id === ids[i]; });
                    if (!ver) continue;
                    promises.push(
                        spFetch('/api/v1/projects/' + ver.project_key + '/versions/' + ver.id + '/', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ status: newStatus })
                        })
                    );
                }
                await Promise.all(promises);
                // Update local state
                for (var j = 0; j < ids.length; j++) {
                    var v = this.allVersions.find(function (item) { return item.id === ids[j]; });
                    if (v) v.status = newStatus;
                }
                this.selected = [];
            },

            async deleteVersion(v) {
                if (!confirm('Delete version "' + v.name + '"?')) return;
                var res = await spFetch('/api/v1/projects/' + v.project_key + '/versions/' + v.id + '/', {
                    method: 'DELETE'
                });
                if (res.ok) {
                    this.allVersions = this.allVersions.filter(function (item) { return item.id !== v.id; });
                    this.selected = this.selected.filter(function (id) { return id !== v.id; });
                }
            }
        };
    }
