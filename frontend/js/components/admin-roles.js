/* Admin > Roles: the list.
 *
 * Only the order changes here. The up/down arrows call the move endpoint,
 * which answers with every custom role in its new order; the rows are put in
 * that order rather than swapped locally, so the page always shows what the
 * server stored (imported roles can share a position until the first move
 * renumbers them).
 */
export function adminRoles(initial) {
    var i18n = Object.assign({
        moveFailed: 'Failed to move the role.',
        moveUp: 'Move %s up',
        moveDown: 'Move %s down'
    }, initial.i18n || {});

    return {
        roles: initial.roles || [],
        moving: false,
        moveError: '',

        upLabel(r) {
            return i18n.moveUp.replace('%s', r.name);
        },

        downLabel(r) {
            return i18n.moveDown.replace('%s', r.name);
        },

        async move(r, direction) {
            if (this.moving) return;
            this.moving = true;
            this.moveError = '';
            try {
                var res = await spFetch('/api/v1/admin/roles/' + r.id + '/move/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ direction: direction })
                });
                if (res.ok) {
                    this.applyOrder(await res.json());
                } else {
                    var err = await res.json().catch(function () { return {}; });
                    this.moveError = (err.errors && err.errors[0] && err.errors[0].message) || i18n.moveFailed;
                }
            } catch (_e) {
                this.moveError = i18n.moveFailed;
            }
            this.moving = false;
        },

        // Reorder the rows by the ids the server returned; rows it did not
        // mention (none expected) keep their place at the end.
        applyOrder(ordered) {
            var byId = {};
            this.roles.forEach(function (row) { byId[row.id] = row; });
            var next = [];
            ordered.forEach(function (o) {
                if (byId[o.id]) {
                    next.push(byId[o.id]);
                    delete byId[o.id];
                }
            });
            this.roles.forEach(function (row) {
                if (byId[row.id]) next.push(row);
            });
            this.roles = next;
        }
    };
}
