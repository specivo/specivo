function spAttachmentsComponent(initial, defaultContainerType, idField) {
        var IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'];
        var EXT_MAP = {
            pdf: 'pdf', doc: 'doc', docx: 'doc',
            xls: 'xls', xlsx: 'xls', csv: 'xls',
            zip: 'zip', gz: 'zip', tar: 'zip', '7z': 'zip', rar: 'zip',
            txt: 'txt', md: 'txt', json: 'txt', yml: 'txt', yaml: 'txt',
            png: 'img', jpg: 'img', jpeg: 'img', gif: 'img', webp: 'img', svg: 'img',
            mp4: 'vid', mov: 'vid', avi: 'vid', mkv: 'vid',
            mp3: 'aud', wav: 'aud', ogg: 'aud', flac: 'aud'
        };

        function getExt(filename) {
            var parts = (filename || '').split('.');
            return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
        }

        var containerId = initial.containerId || initial.pageId || 0;
        var containerType = initial.containerType || defaultContainerType;

        return {
            containerId: containerId,
            containerType: containerType,
            attachments: initial.attachments || [],
            showUpload: false,
            isDragging: false,
            uploads: [],
            uploadError: '',
            lightbox: null,
            deleteTarget: null,
            deleting: false,

            get images() {
                return this.attachments.filter(function (a) {
                    return IMAGE_TYPES.indexOf(a.content_type) !== -1;
                });
            },

            get files() {
                return this.attachments.filter(function (a) {
                    return IMAGE_TYPES.indexOf(a.content_type) === -1;
                });
            },

            fileIconClass(att) {
                var ext = getExt(att.filename);
                return EXT_MAP[ext] || 'generic';
            },

            fileIconLabel(att) {
                var ext = getExt(att.filename);
                return ext ? ext.toUpperCase().substring(0, 4) : 'FILE';
            },

            formatSize(bytes) {
                if (bytes < 1024) return bytes + ' B';
                if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
                return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
            },

            formatDate(isoStr) {
                if (!isoStr) return '';
                var d = new Date(isoStr);
                var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                return months[d.getMonth()] + ' ' + d.getDate();
            },

            openLightbox(att) {
                this.lightbox = {
                    url: '/api/v1/attachments/' + att.id + '/download/',
                    name: att.filename,
                    size: this.formatSize(att.filesize)
                };
            },

            copyLink(att) {
                var isImage = IMAGE_TYPES.indexOf(att.content_type) !== -1;
                var url = '/api/v1/attachments/' + att.id + '/download/';
                var md = isImage
                    ? '![' + att.filename + '](' + url + ')'
                    : '[' + att.filename + '](' + url + ')';
                navigator.clipboard.writeText(md);
            },

            downloadFile(att) {
                window.open('/api/v1/attachments/' + att.id + '/download/', '_blank');
            },

            confirmDelete(att) {
                this.deleteTarget = att;
                this.deleting = false;
            },

            async doDelete() {
                if (!this.deleteTarget) return;
                this.deleting = true;
                try {
                    var res = await spFetch('/api/v1/attachments/' + this.deleteTarget.id + '/', {
                        method: 'DELETE'
                    });
                    if (res.ok || res.status === 204) {
                        var targetId = this.deleteTarget.id;
                        this.attachments = this.attachments.filter(function (a) {
                            return a.id !== targetId;
                        });
                        this.deleteTarget = null;
                    } else {
                        var data = {};
                        try { data = await res.json(); } catch (_e) {}
                        this.uploadError = (data.errors && data.errors[0] && data.errors[0].message) || 'Delete failed';
                        this.deleteTarget = null;
                    }
                } catch (_e) {
                    this.uploadError = 'Unable to connect.';
                    this.deleteTarget = null;
                }
                this.deleting = false;
            },

            handleDrop(event) {
                this.isDragging = false;
                var files = event.dataTransfer && event.dataTransfer.files;
                if (files && files.length) {
                    this.handleFiles(files);
                }
            },

            async handleFiles(fileList) {
                if (!fileList || !fileList.length) return;
                this.uploadError = '';
                for (var i = 0; i < fileList.length; i++) {
                    await this._uploadOne(fileList[i]);
                }
                // Reset file input so the same file can be re-selected
                if (this.$refs.fileInput) {
                    this.$refs.fileInput.value = '';
                }
            },

            async _uploadOne(file) {
                var entry = { name: file.name, progress: 0 };
                this.uploads.push(entry);
                try {
                    var formData = new FormData();
                    formData.append('file', file);
                    formData.append('container_type', this.containerType);
                    formData.append('container_id', this.containerId);

                    var res = await spFetch('/api/v1/attachments/', {
                        method: 'POST',
                        body: formData
                    });

                    entry.progress = 100;
                    if (res.ok) {
                        var data = await res.json();
                        this.attachments.push(data);
                    } else {
                        var errData = {};
                        try { errData = await res.json(); } catch (_e) {}
                        this.uploadError = (errData.errors && errData.errors[0] && errData.errors[0].message)
                            || errData.message || 'Upload failed';
                    }
                } catch (_e) {
                    this.uploadError = 'Unable to connect.';
                }
                // Remove progress entry after a short delay
                var self = this;
                setTimeout(function () {
                    var idx = self.uploads.indexOf(entry);
                    if (idx !== -1) self.uploads.splice(idx, 1);
                }, 1500);
            }
        };
    }

export function wikiAttachments(initial) {
        if (initial.dataElementId && !initial.attachments) {
            var el = document.getElementById(initial.dataElementId);
            initial.attachments = el ? JSON.parse(el.textContent) : [];
        }
        return spAttachmentsComponent(initial, 'WikiPage', 'pageId');
    }

export function issueAttachments(initial) {
        /* CSP-safe: parse attachments from a <script type=application/json> element */
        if (initial.dataElementId && !initial.attachments) {
            var el = document.getElementById(initial.dataElementId);
            initial.attachments = el ? JSON.parse(el.textContent) : [];
        }
        return spAttachmentsComponent(initial, 'Issue', 'containerId');
    }
