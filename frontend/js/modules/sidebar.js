/* Mobile sidebar toggle */
export function initSidebar() {
    var hamburger = document.querySelector('.hamburger');
    var overlay = document.querySelector('.sidebar-overlay');
    if (hamburger) {
        hamburger.addEventListener('click', function () {
            document.body.classList.add('sidebar-open');
        });
    }
    if (overlay) {
        overlay.addEventListener('click', function () {
            document.body.classList.remove('sidebar-open');
        });
    }
}
