/* Avatar upload: auto-submit on file select */
export function initAvatarUpload() {
    var input = document.querySelector('[data-avatar-upload]');
    if (input) input.addEventListener('change', function () { input.closest('form').submit(); });
}
