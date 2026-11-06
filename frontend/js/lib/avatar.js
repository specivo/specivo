/* Avatars of the built-in system accounts (``$`` logins). Mirrors
   SYSTEM_AVATAR_ICONS in specivo/core/constants.py and the avatar macro. */

const SYSTEM_AVATAR_ICONS = { $git: 'git' };

/* The CSS classes of a system account's bundled icon avatar, or '' for anyone else. */
export function avatarIconClass(login) {
    const icon = Object.prototype.hasOwnProperty.call(SYSTEM_AVATAR_ICONS, login) ? SYSTEM_AVATAR_ICONS[login] : '';
    return icon ? 'sp-avatar-icon sp-avatar-icon-' + icon : '';
}

/* Up to two initials of *name*, never a ``$``: system accounts are named by their display name. */
export function avatarInitials(name) {
    const words = String(name || '').replace(/\$/g, ' ').split(/\s+/).filter(Boolean);
    const initials = words.map(function (w) { return w[0]; }).join('').substring(0, 2).toUpperCase();
    return initials || '?';
}
