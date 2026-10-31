/**
 * "Copy branch name" on the issue page.
 *
 * The server computes the name and renders it into a read-only input; this
 * copies it with the Clipboard API and says "Copied". Where the clipboard is
 * not available (an insecure origin, a denied permission) it selects the
 * text instead and asks the user to copy it themselves.
 */
export function copyBranchName(initial) {
    var i18n = Object.assign({
        copied: 'Copied',
        pressToCopy: 'Press Ctrl+C to copy'
    }, initial.i18n || {});

    return {
        i18n: i18n,
        name: initial.name || '',
        message: '',
        hint: false,
        _timer: null,

        async copy() {
            var done = false;
            if (navigator.clipboard && window.isSecureContext) {
                try {
                    await navigator.clipboard.writeText(this.name);
                    done = true;
                } catch (e) {
                    done = false;
                }
            }
            if (done) {
                this.show(this.i18n.copied, false);
            } else {
                this.selectName();
                this.show(this.i18n.pressToCopy, true);
            }
        },

        selectName() {
            var input = this.$refs.input;
            if (!input) return;
            input.focus();
            input.select();
            input.setSelectionRange(0, input.value.length);
        },

        show(text, hint) {
            var self = this;
            this.message = text;
            this.hint = hint;
            clearTimeout(this._timer);
            this._timer = setTimeout(function () { self.message = ''; }, 3000);
        }
    };
}
