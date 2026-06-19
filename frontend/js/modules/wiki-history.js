/* Wiki history: compare two versions */
export function initWikiHistory() {
    var btn = document.getElementById('btn-compare');
    if (!btn) return;
    var checks = document.querySelectorAll('.version-check');
    function update() {
        var selected = document.querySelectorAll('.version-check:checked');
        if (selected.length === 2) {
            btn.classList.add('enabled');
        } else {
            btn.classList.remove('enabled');
        }
        // Limit to max 2 selections
        if (selected.length >= 2) {
            checks.forEach(function (c) {
                if (!c.checked) c.disabled = true;
            });
        } else {
            checks.forEach(function (c) { c.disabled = false; });
        }
    }
    checks.forEach(function (c) { c.addEventListener('change', update); });
    btn.addEventListener('click', function () {
        var selected = document.querySelectorAll('.version-check:checked');
        if (selected.length !== 2) return;
        var v1 = selected[0].value;
        var v2 = selected[1].value;
        // Sort so older version is first
        var from = Math.min(v1, v2);
        var to = Math.max(v1, v2);
        var path = window.location.pathname.replace('/history/', '/diff/');
        window.location.href = path + '?from_version=' + from + '&to_version=' + to;
    });
}
