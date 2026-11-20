// Who is here: /api/bootstrap into state.auth, plus the dock and undo state for a signed-in user.
//
//   loadSession()        fetch bootstrap (+ dock); safe to call again (persona switch, sign-in)
//   setDemoUser(id)      demo mode only: act as another persona, then reload the session and view
//   reloadDock()         GET /api/me/dock into state.dock

import { state, update } from './store.js';
import * as api from './api.js';

export async function loadSession() {
  const boot = await api.get('/api/bootstrap');
  const mode = boot.auth && boot.auth.mode === 'demo' ? 'demo' : 'google';
  const user = boot.current_user && boot.current_user.id ? boot.current_user : null;
  const signedIn = mode === 'demo' ? !!user : !!(boot.auth && boot.auth.signed_in && user);
  update((s) => {
    s.auth = {
      mode,
      signedIn,
      user: signedIn ? user : null,
      crateId: signedIn ? user.crate_id || null : null,
      users: boot.users || [],
      youtubeConnected: !!(boot.auth && boot.auth.youtube_connected),
      badgeCatalog: boot.badge_catalog || [],
    };
    s.undo = signedIn ? user.undo || null : null;
    s.home = null;
    if (!signedIn) {
      s.dock = null;
      s.ui.cratePanel = false;
    }
  }, 'session');
  if (mode === 'demo' && user) api.storeDemoUserId(user.id);
  if (signedIn) await reloadDock().catch(() => {});
  return state.auth;
}

export async function reloadDock() {
  if (!state.auth.signedIn) return null;
  const dock = await api.get('/api/me/dock');
  update((s) => { s.dock = dock; }, 'dock');
  return dock;
}

export async function setDemoUser(id) {
  api.storeDemoUserId(id);
  update((s) => {
    s.auth = { ...s.auth, user: null };
    s.playlists.clear();
    s.tracks.clear();
    s.selection = { playlistId: null, ids: new Set(), anchorId: null, cursorId: null, insertAfterId: undefined };
  }, 'persona');
  await loadSession();
  const router = await import('./router.js');
  await router.refresh();
}
