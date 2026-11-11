// Lane C entry (Import / Share / Crate / Account). main.js imports this once, after F's core
// registrations. Everything here wires itself in through registries: slots, actions, keys and the
// paste router. Styles: styles/share-import.css (.sh-*) and styles/mobile.css.

import '../account/signin-intent.js';
import '../account/account-pill.js';
import '../account/handle-prompt.js';
import '../account/personas.js';
import '../crate/crate-actions.js';
import '../import/import-sheet.js';
import '../import/paste-to-crate.js';
import './share-popover.js';
import './fork.js';
import './settings.js';
