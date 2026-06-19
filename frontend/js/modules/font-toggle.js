/* Font size toggle — bound via data-font attribute, no inline onclick */
export function initFontToggle() {
    var container = document.querySelector('.font-toggle');
    if (!container) return;
    container.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-font]');
        if (!btn) return;
        var cls = btn.getAttribute('data-font');
        document.documentElement.classList.remove('font-md', 'font-lg');
        if (cls) document.documentElement.classList.add(cls);
        container.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
    });
}
