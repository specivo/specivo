export function markdownEditor(initial) {
        initial = initial || {};
        return {
            value: initial.initial || '',
            editor: null,
            previewUrl: initial.previewUrl || '/api/v1/markdown/preview/',
            context: initial.context || 'wiki',
            onPasteDrop: initial.onPasteDrop || '',
            _previewTimer: null,
            _lastPreviewText: null,
            _lastPreviewHtml: '',

            init() {
                var self = this;
                if (typeof window.EasyMDE === 'undefined') {
                    /* EasyMDE asset not loaded — keep textarea functional. */
                    return;
                }
                var ta = this.$refs.textarea;
                if (!ta) return;
                ta.value = this.value;

                this.editor = new window.EasyMDE({
                    element: ta,
                    autoDownloadFontAwesome: false,
                    spellChecker: false,
                    /* Disable status bar items that imply spell-check or remote fetch. */
                    status: ['lines', 'words'],
                    tabSize: 2,
                    indentWithTabs: false,
                    forceSync: true,
                    minHeight: '200px',
                    placeholder: 'Write your content here (Markdown supported)...',
                    /* Use built-in toolbar shorthand strings — EasyMDE wires up the
                     * matching prototype methods (togglePreview, toggleSideBySide,
                     * toggleFullScreen) internally. Passing static method refs as
                     * `action` was redundant and risked breaking when the bundle's
                     * internal toolbar map was rebuilt against unbound prototype
                     * methods on click. */
                    toolbar: [
                        'bold', 'italic', 'code', '|',
                        'heading-1', 'heading-2', 'heading-3', '|',
                        'unordered-list', 'ordered-list', 'quote', 'horizontal-rule', '|',
                        'link', '|',
                        'preview'
                        /* 'side-by-side' and 'fullscreen' deliberately omitted —
                         * EasyMDE pins them with position:fixed and assumes the
                         * editor owns the viewport, which clobbers the page chrome
                         * (sidebar nav, header, metadata panel). Preview-only is
                         * the right affordance for an editor embedded in a card. */
                    ],
                    previewRender: function (plainText, previewElement) {
                        return self._renderPreview(plainText, previewElement);
                    }
                });

                /* Refresh CodeMirror on the first user interaction.
                 *
                 * After EasyMDE's gfm-overlay mode finishes async tokenization,
                 * the doc replaces the Line object for each tokenized line, but
                 * the cached display.view items still reference the previous
                 * Line objects. A subsequent prepareSelection -> Bn(viewItem,
                 * line, n) checks `viewItem.line === line`, the identity check
                 * fails, the function falls through to a non-rest branch and
                 * returns undefined, and the caller throws "Cannot read
                 * properties of undefined (reading 'map')" — the symptom
                 * users see as silent keystroke drops on every click into
                 * the editor body.
                 *
                 * Calling cm.refresh() rebuilds display.view from the current
                 * Doc, restoring view[i].line === doc line i. The catch is
                 * timing: a refresh scheduled from init (rAF, double-rAF,
                 * setTimeout) all fire BEFORE the gfm tokenizer has finished
                 * its own deferred work, so the view gets re-corrupted right
                 * after we rebuild it. Refreshing on the first mousedown /
                 * focus runs after every async setup is done, when the user
                 * actually wants to interact. A flag ensures we only do this
                 * once per editor lifetime; subsequent interactions go through
                 * a clean view. */
                var cm = this.editor.codemirror;
                var firstInteractionDone = false;
                var refreshOnFirstUse = function () {
                    if (firstInteractionDone) return;
                    firstInteractionDone = true;
                    if (!self.editor || self.editor.codemirror !== cm) return;
                    cm.refresh();
                };
                cm.on('mousedown', refreshOnFirstUse);
                cm.on('focus', refreshOnFirstUse);

                /* Keep Alpine `value` in sync with the editor. */
                cm.on('change', function () {
                    self.value = self.editor.value();
                });

                /* Seed the editor with the current bound value, then keep it in
                 * sync when the parent updates `value` (via x-modelable / x-model).
                 * This matters when the wrapper is rendered inside x-show: Alpine
                 * inits the component on page load when the parent's bound value
                 * may still be empty, so EasyMDE's initial snapshot is empty.
                 * Once the parent populates the value (e.g. issue Edit click sets
                 * draft = description), we push it into the editor view. Also
                 * covers Cancel reverting drafts back to the saved value.
                 *
                 * After every value swap we call codemirror.refresh() on the next
                 * tick. CodeMirror caches scroller/sizer dimensions at init time;
                 * if the wrapper was display:none at init (x-show="editing"), the
                 * cached metrics are wrong and content renders pushed to the
                 * bottom of the pane. refresh() recomputes them once the wrapper
                 * is visible and the value has settled. */
                var refreshCm = function () {
                    if (!self.editor || !self.editor.codemirror) return;
                    self.$nextTick(function () {
                        self.editor.codemirror.refresh();
                    });
                };
                if (this.value && this.editor.value() !== this.value) {
                    this.editor.value(this.value);
                    refreshCm();
                }
                this.$watch('value', function (newValue) {
                    if (!self.editor) return;
                    if (self.editor.value() === (newValue || '')) return;
                    self.editor.value(newValue || '');
                    refreshCm();
                });

                /* Expose paste/drop hooks for the parent component.
                 * Walk parent elements asking Alpine for their data scope until
                 * we find one that exposes the named method. Uses the public
                 * Alpine.$data(el) API which returns the merged data proxy. */
                if (this.onPasteDrop) {
                    var dispatch = function (event, kind) {
                        var node = self.$el.parentElement;
                        while (node) {
                            try {
                                var data = window.Alpine && window.Alpine.$data
                                    ? window.Alpine.$data(node)
                                    : null;
                                if (data && typeof data[self.onPasteDrop] === 'function') {
                                    data[self.onPasteDrop](event, kind, self);
                                    return;
                                }
                            } catch (_e) { /* node has no data scope */ }
                            node = node.parentElement;
                        }
                    };
                    this.editor.codemirror.on('paste', function (cm, event) {
                        dispatch(event, 'paste');
                    });
                    this.editor.codemirror.on('drop', function (cm, event) {
                        dispatch(event, 'drop');
                    });
                }

                /* Add a stable hook class so CSS overrides can target the wrapper. */
                var wrapper = ta.nextElementSibling;
                if (wrapper && wrapper.classList && wrapper.classList.contains('EasyMDEContainer')) {
                    wrapper.classList.add('sp-md-editor');
                }

                /* When the editor lives inside a hidden ancestor (x-show="editing"),
                 * CodeMirror caches zero-size metrics at init and renders content
                 * pushed to the bottom of the pane once the wrapper is shown. An
                 * IntersectionObserver covers every reveal — including the first
                 * Edit click and any subsequent show/hide cycle — by refreshing
                 * the editor once the container intersects the viewport.
                 *
                 * The observer is attached directly to the DOM element (not the
                 * Alpine reactive state) because Alpine's proxy strips non-plain
                 * objects on assignment. destroy() reads it back from the DOM. */
                if (wrapper && typeof window.IntersectionObserver === 'function') {
                    var io = new IntersectionObserver(function (entries) {
                        entries.forEach(function (entry) {
                            if (entry.isIntersecting && self.editor && self.editor.codemirror) {
                                self.editor.codemirror.refresh();
                            }
                        });
                    });
                    io.observe(wrapper);
                    wrapper._spVisibilityObserver = io;
                }
            },

            destroy() {
                if (this._previewTimer) {
                    clearTimeout(this._previewTimer);
                    this._previewTimer = null;
                }
                /* Disconnect the visibility observer attached to the wrapper. */
                var ta = this.$refs && this.$refs.textarea;
                var wrapper = ta && ta.nextElementSibling;
                if (wrapper && wrapper._spVisibilityObserver) {
                    try { wrapper._spVisibilityObserver.disconnect(); } catch (_e) { /* already gone */ }
                    wrapper._spVisibilityObserver = null;
                }
                if (this.editor) {
                    try {
                        this.editor.toTextArea();
                    } catch (_e) { /* already torn down */ }
                    this.editor = null;
                }
            },

            /* Replace the editor contents (used by parent when resetting drafts). */
            setValue(text) {
                this.value = text || '';
                if (this.editor) {
                    this.editor.value(this.value);
                }
            },

            /* Insert text at the current cursor position (used by paste/drop upload). */
            insertText(text) {
                if (!this.editor) return;
                var cm = this.editor.codemirror;
                cm.replaceSelection(text);
                cm.focus();
            },

            /* Server-side preview with debounce. */
            _renderPreview(plainText, previewElement) {
                var self = this;
                if (this._lastPreviewText === plainText) {
                    return this._lastPreviewHtml;
                }
                if (this._previewTimer) {
                    clearTimeout(this._previewTimer);
                }
                /* Loading placeholder — plain text only, no HTML injection. */
                previewElement.textContent = 'Rendering...';
                this._previewTimer = setTimeout(function () {
                    self._fetchPreview(plainText, previewElement);
                }, 250);
                return '';
            },

            async _fetchPreview(plainText, previewElement) {
                try {
                    var res = await spFetch(this.previewUrl, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({text: plainText, context: this.context})
                    });
                    if (res.ok) {
                        var data = await res.json();
                        this._lastPreviewText = plainText;
                        this._lastPreviewHtml = data.html || '';
                        /* Server returns sanitised HTML (markdown_service.preview).
                         * Same path as the saved-content rendering, so this matches
                         * what the user will see after Save. */
                        previewElement.innerHTML = this._lastPreviewHtml;  /* noqa: XSS */
                    } else {
                        previewElement.textContent = 'Preview unavailable.';
                    }
                } catch (_e) {
                    previewElement.textContent = 'Preview unavailable.';
                }
            }
        };
    }
