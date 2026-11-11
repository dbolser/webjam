// Action-triggered sign-in (docs/phase1b-shell.md §3.9, §4.4).
//
// Registers the 'auth.required' slot. An anonymous visitor who tries to collect a track, like
// something or paste a link gets a quiet toast with a "Sign in" action. Taking it stores the
// intent { action, payload, path } in sessionStorage and goes to /auth/google/login?next=<path>,
// so the visitor comes back to the page they were on. After sign-in only 'crate-add' and 'like'
// intents are replayed. Listening never needs any of this.
//
// Intent shapes this module replays:
//   { action: 'crate-add', payload: { items: [{ video_id, start_seconds, end_seconds }] } }   (F core)
//   { action: 'crate-add', payload: { text } }                                              (paste)
//   { action: 'like', payload: { playlist_id } | { video_id } }                             (Lane B)
//
// It also turns the old #not-invited / #auth-error notices into a toast when the view does not
// already show them inline.

import { state, subscribe } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { toast } from '../ui/toast.js';
import { signInUrl, localUrl, isVideoId } from '../format.js';
import { addToCrate } from '../core.js';
import { here } from '../share/util.js';

const KEY = 'webjam.intent';
const REPLAY = new Set(['crate-add', 'like']);
const MAX_AGE_MS = 30 * 60 * 1000;
const MAX_TEXT = 20000;

function stash(intent) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ ...intent, at: Date.now() }));
  } catch {
    /* storage blocked: the visitor returns to the page, just without the replay */
  }
}

function take() {
  let raw = null;
  try {
    raw = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (!v || typeof v !== 'object' || !REPLAY.has(v.action)) return null;
    if (!(Date.now() - Number(v.at) < MAX_AGE_MS)) return null;
    return v;
  } catch {
    return null;
  }
}

/** The page to come back to: the intent's own path, unless the intent came from an API 401. */
function returnPath(intent) {
  if (intent && !intent.error && typeof intent.path === 'string' && !intent.path.startsWith('/api/')) {
    return localUrl(intent.path);
  }
  return localUrl(here());
}

function slimPayload(intent) {
  const p = intent.payload || {};
  if (intent.action === 'crate-add') {
    if (Array.isArray(p.items)) {
      return {
        items: p.items.slice(0, 50).filter((it) => it && isVideoId(it.video_id)).map((it) => ({
          video_id: it.video_id,
          start_seconds: it.start_seconds ?? null,
          end_seconds: it.end_seconds ?? null,
        })),
      };
    }
    if (typeof p.text === 'string') return { text: p.text.slice(0, MAX_TEXT) };
    return null;
  }
  if (intent.action === 'like') {
    if (Number(p.playlist_id) > 0) return { playlist_id: Number(p.playlist_id) };
    if (isVideoId(p.video_id)) return { video_id: p.video_id };
  }
  return null;
}

/** Go to Google sign-in, remembering a replayable intent. */
export function goSignIn(intent = {}) {
  const path = returnPath(intent);
  const payload = REPLAY.has(intent.action) ? slimPayload(intent) : null;
  if (payload) stash({ action: intent.action, payload, path });
  location.assign(signInUrl(path));
}

function promptText(intent) {
  switch (intent && intent.action) {
    case 'crate-add': return 'Sign in to collect tracks in your crate.';
    case 'like': return 'Sign in to like this.';
    case 'fork': return 'Sign in to copy this playlist.';
    case 'import': return 'Sign in to import from YouTube.';
    case 'new-playlist': return 'Sign in to make a playlist.';
    case 'open-crate': return 'Sign in to start a crate.';
    default: return 'Sign in to do that.';
  }
}

slots.register('auth.required', (intent = {}) => {
  if (state.auth.mode === 'demo') {
    toast.show('Pick a persona from the account menu to do that.');
    return true;
  }
  toast.show(promptText(intent), {
    timeout: 6000,
    action: { label: 'Sign in', run: () => goSignIn(intent) },
  });
  return true;
});

// --- replay after sign-in -----------------------------------------------------------------------

async function replay(intent) {
  const p = intent.payload || {};
  if (intent.action === 'crate-add') {
    if (Array.isArray(p.items) && p.items.length) {
      await addToCrate(p.items); // toasts "Added to your crate"
    } else if (typeof p.text === 'string' && p.text.trim()) {
      const { pasteIntoCrate } = await import('../import/paste-to-crate.js');
      await pasteIntoCrate({ text: p.text, html: '', json: null });
    }
    return;
  }
  if (intent.action === 'like') {
    try {
      if (p.playlist_id) await api.post(`/api/playlists/${Number(p.playlist_id)}/like`, { count: 1 });
      else if (p.video_id) await api.post(`/api/songs/${encodeURIComponent(p.video_id)}/like`, { count: 1 });
      toast.show('Liked');
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not like that.');
    }
  }
}

let replayed = false;
subscribe((s) => s.auth.signedIn && !!s.auth.crateId, (ready) => {
  if (!ready || replayed) return;
  replayed = true;
  const intent = take();
  if (intent) replay(intent).catch((err) => console.error('intent replay', err));
});

// --- sign-in notices (#not-invited, #auth-error) ------------------------------------------------

subscribe((s) => s.ui.notice, (notice) => {
  if (!notice) return;
  // The home and discover views show the notice inline; toast only when nothing on screen does.
  requestAnimationFrame(() => {
    if (document.querySelector('#view .notice')) return;
    const retry = /didn't complete/i.test(notice) && state.auth.mode === 'google';
    toast.show(notice, {
      timeout: 8000,
      action: retry ? { label: 'Try again', href: '/auth/google/login' } : null,
    });
  });
});
