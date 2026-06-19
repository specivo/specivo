/* ============================================================
   Application entry — non-Alpine (vanilla) modules.

   Bundled into app.min.js, loaded after Alpine. Each module
   exposes an initX() that wires its listeners; they run on
   DOMContentLoaded (the document is already parsed since the
   bundle is deferred), matching the old monolith's behavior.
   ============================================================ */
import './lib/globals';
import { initCsrf } from './modules/csrf';
import { initHtmxBehaviors } from './modules/htmx-behaviors';
import { initServiceWorker } from './modules/service-worker';
import { initWikiHistory } from './modules/wiki-history';
import { initPageSize } from './modules/page-size';
import { initFontToggle } from './modules/font-toggle';
import { initSidebar } from './modules/sidebar';
import { initCommandPalette } from './modules/command-palette';
import { initColorPicker } from './modules/color-picker';
import { initAvatarUpload } from './modules/avatar-upload';
import { initConfirmDialog } from './modules/confirm-dialog';
import { initModal } from './modules/modal';

document.addEventListener('DOMContentLoaded', function () {
    initCsrf();
    initHtmxBehaviors();
    initServiceWorker();
    initWikiHistory();
    initPageSize();
    initFontToggle();
    initSidebar();
    initCommandPalette();
    initColorPicker();
    initAvatarUpload();
    initConfirmDialog();
    initModal();
});
