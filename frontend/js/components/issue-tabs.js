/**
 * The tabs under an issue's description: Activity, Development, Time,
 * Relations, Attachments.
 *
 * A tab can be opened by link: ``#development`` (or any other tab's name)
 * opens it on load and on a hash change, and opening a tab puts its name in
 * the address so the link can be copied. Activity is the default and keeps a
 * bare address. A hash that names no tab here (``#journal-12``, or a tab this
 * reader does not have) is left alone. Opening a tab dispatches
 * ``sp-show-tab-<name>``, which the lazily loaded tabs fetch on.
 */
export function issueTabs() {
    return {
        tab: 'activity',

        init() {
            this._fromHash(false);
            var self = this;
            window.addEventListener('hashchange', function () { self._fromHash(true); });
        },

        /* Open *name* and record it in the address. */
        show(name) {
            this._open(name, true);
            var url = window.location.pathname + window.location.search + (name === 'activity' ? '' : '#' + name);
            history.replaceState(history.state, '', url);
        },

        _has(name) {
            var links = this.$el.querySelectorAll('[data-tab]');
            for (var i = 0; i < links.length; i++) {
                if (links[i].getAttribute('data-tab') === name) return true;
            }
            return false;
        },

        _fromHash(ready) {
            var name;
            try {
                name = decodeURIComponent(window.location.hash.slice(1));
            } catch (e) {
                return;
            }
            if (!this._has(name)) return;
            this._open(name, ready);
            this.$el.scrollIntoView({ block: 'start' });
        },

        _open(name, ready) {
            this.tab = name;
            var el = this.$el;
            var fire = function () {
                el.dispatchEvent(new CustomEvent('sp-show-tab-' + name, { bubbles: true }));
            };
            // On load htmx binds its triggers on DOMContentLoaded, after Alpine starts.
            if (ready) {
                fire();
            } else if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', function () { setTimeout(fire, 0); }, { once: true });
            } else {
                setTimeout(fire, 0);
            }
        }
    };
}
