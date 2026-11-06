/* "Select all" checkboxes (CSP-safe): a checkbox with data-select-all="<name>"
   ticks or clears every checkbox named <name> of the same form. The form is the
   checkbox's form owner, so it works for checkboxes tied to a form by the
   form="" attribute (rows of a table outside the form). */
export function initSelectAll() {
    document.addEventListener('change', function (e) {
        var box = e.target;
        if (!box || !box.matches || !box.matches('input[type="checkbox"][data-select-all]')) return;
        var form = box.form;
        if (!form) return;
        var name = box.getAttribute('data-select-all');
        var items = form.elements.namedItem(name);
        if (!items) return;
        if (items instanceof Element) items = [items];
        for (var i = 0; i < items.length; i++) {
            if (items[i].type === 'checkbox' && !items[i].disabled) items[i].checked = box.checked;
        }
    });
}
