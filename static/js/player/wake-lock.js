// Keep the screen awake while playing (docs/phase1b-shell.md §12.1). Phones can't play a YouTube
// embed in the background, so the next best thing is not letting the screen sleep mid-mix.
//
// A small state machine over the Screen Wake Lock API, with navigator, document and storage passed
// in so tests/js/player_helpers.mjs can drive it with fakes:
//   want the lock  <=>  supported && enabled && playing && the page is visible
// Browsers drop the lock when the tab is hidden; visibility() asks again on return if still wanted.
// Every request() rejection (low battery, no user activation, policy) is swallowed.
//
//   const w = createWakeLock({ nav, doc, storage, touch })
//   w.supported  w.enabled()  w.held()  w.setEnabled(on)  w.setPlaying(on)  w.visibility()

export const WAKE_KEY = 'webjam.keepAwake';

export function createWakeLock({ nav, doc, storage, touch = false } = {}) {
  const supported = !!(nav && nav.wakeLock && typeof nav.wakeLock.request === 'function');
  let enabled = readPref(storage, touch);
  let playing = false;
  let sentinel = null;
  let pending = null;

  const visible = () => !doc || doc.visibilityState !== 'hidden';
  const wanted = () => supported && enabled && playing && visible();

  function sync() {
    if (wanted()) {
      if (sentinel || pending) return pending || Promise.resolve();
      let p;
      try {
        p = Promise.resolve(nav.wakeLock.request('screen'));
      } catch {
        return Promise.resolve();
      }
      pending = p.then((s) => {
        pending = null;
        if (!s) return;
        if (!wanted()) {
          // Paused or hidden while the request was in flight.
          try { Promise.resolve(s.release()).catch(() => {}); } catch { /* ignore */ }
          return;
        }
        sentinel = s;
        try {
          s.addEventListener('release', () => { if (sentinel === s) sentinel = null; });
        } catch { /* old sentinel without events */ }
      }, () => { pending = null; });
      return pending;
    }
    if (sentinel) {
      const s = sentinel;
      sentinel = null;
      try { Promise.resolve(s.release()).catch(() => {}); } catch { /* already released */ }
    }
    return pending || Promise.resolve();
  }

  return {
    supported,
    enabled: () => enabled,
    held: () => !!sentinel,
    setEnabled(on) {
      enabled = !!on;
      try { storage && storage.setItem(WAKE_KEY, enabled ? '1' : '0'); } catch { /* private mode */ }
      return sync();
    },
    setPlaying(on) {
      playing = !!on;
      return sync();
    },
    /** Call on visibilitychange: re-acquires on return (the browser released it on hide). */
    visibility() {
      return sync();
    },
  };
}

/** Remembered per browser; default on for touch devices, off elsewhere. */
function readPref(storage, touch) {
  try {
    const v = storage ? storage.getItem(WAKE_KEY) : null;
    if (v === '1') return true;
    if (v === '0') return false;
  } catch { /* blocked */ }
  return !!touch;
}
