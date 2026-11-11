// Boot: theme, listeners, session, shell, router.
//
// Import order matters: F's core actions/keys and routes register first, then each lane's entry
// module (one line per lane; each lane uncomments its own). Lanes extend through registries only:
// registerActions, registerKeys, slots.register, pasteRouter.register, router.register.

import './core.js';
import './routes.js';
// import './editing/index.js';
// import './discovery/index.js';
// import './share/index.js';

import { state, onChange } from './store.js';
import * as api from './api.js';
import { startActions } from './actions.js';
import { startKeys } from './keys.js';
import { startPasteRouter } from './paste-router.js';
import { start as startRouter } from './router.js';
import { loadSession } from './session.js';
import { initTheme } from './theme.js';
import { renderShell, renderTick } from './shell.js';
import { toast } from './ui/toast.js';

async function boot() {
  initTheme();
  startActions(); // before the router's link listener, so data-action links can preventDefault
  startKeys();
  startPasteRouter();
  try {
    await loadSession();
  } catch (err) {
    console.error('bootstrap', err);
    toast.show('Could not reach the server. Showing what we can.');
  }
  renderShell();
  onChange((reasons) => {
    if (reasons.size === 1 && reasons.has('tick')) renderTick();
    else renderShell();
  });
  const me = state.auth.user;
  await startRouter({
    me: me && me.username,
    getUser: (id) => api.get(`/api/users/${Number(id)}`),
  });
}

boot();
