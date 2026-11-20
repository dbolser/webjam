// Lock-screen and headphone controls through the Media Session API (docs/phase1b-shell.md §12.2).
// The YouTube iframe is cross-origin and owns the actual <video>, so some browsers show YouTube's
// own session (or none) instead of ours. Everything here is set correctly and fails quietly.
//
// Pure helpers (metadataFor, positionFor) are tested in tests/js/player_helpers.mjs; the rest take
// `nav` so a fake navigator works too.

export const SEEK_STEP = 10;
const VID = /^[\w-]{11}$/;

/** { title, artist, album, artwork } for an item. Artist is the channel, else the curator. */
export function metadataFor(item, source = null, curator = '') {
  if (!item) return null;
  const vid = String(item.video_id || '');
  const artwork = VID.test(vid)
    ? [
      { src: `https://i.ytimg.com/vi/${vid}/mqdefault.jpg`, sizes: '320x180', type: 'image/jpeg' },
      { src: `https://i.ytimg.com/vi/${vid}/hqdefault.jpg`, sizes: '480x360', type: 'image/jpeg' },
    ]
    : [];
  const album = source && source.kind === 'crate' ? 'Your crate' : (source && source.title) || 'webjam';
  return {
    title: String(item.title || vid),
    artist: String(item.channel || (curator ? `@${curator}` : '')),
    album: String(album),
    artwork,
  };
}

/** setPositionState() argument across the clip (0 = clip start), or null when not yet valid. */
export function positionFor(item, time, duration) {
  if (!item) return null;
  const start = Number(item.start) || 0;
  const end = item.end != null ? Number(item.end) : Number(duration);
  if (!Number.isFinite(end) || !Number.isFinite(start)) return null;
  const span = end - start;
  if (!(span > 0)) return null;
  const t = Number(time);
  const position = Math.max(0, Math.min(span, (Number.isFinite(t) ? t : start) - start));
  return { duration: span, position, playbackRate: 1 };
}

export function supported(nav) {
  return !!(nav && nav.mediaSession);
}

export function setMetadata(nav, Ctor, meta) {
  if (!supported(nav)) return;
  try {
    nav.mediaSession.metadata = meta && typeof Ctor === 'function' ? new Ctor(meta) : null;
  } catch { /* ignore */ }
}

/** 'playing' | 'paused' | 'none' */
export function setPlaybackState(nav, s) {
  if (!supported(nav)) return;
  try { nav.mediaSession.playbackState = s; } catch { /* ignore */ }
}

export function setPosition(nav, pos) {
  if (!supported(nav) || typeof nav.mediaSession.setPositionState !== 'function') return;
  try {
    nav.mediaSession.setPositionState(pos || undefined); // undefined clears it
  } catch { /* invalid values or unsupported */ }
}

/** Wires the handlers once. `h` = { play, pause, prev, next, seekTo(clipSeconds), seekBy(delta) }. */
export function wireActions(nav, h) {
  if (!supported(nav)) return;
  const set = (name, fn) => {
    try { nav.mediaSession.setActionHandler(name, fn); } catch { /* unsupported action */ }
  };
  set('play', () => h.play());
  set('pause', () => h.pause());
  set('previoustrack', () => h.prev());
  set('nexttrack', () => h.next());
  set('seekto', (d) => {
    if (d && Number.isFinite(Number(d.seekTime))) h.seekTo(Number(d.seekTime));
  });
  set('seekbackward', (d) => h.seekBy(-((d && Number(d.seekOffset)) || SEEK_STEP)));
  set('seekforward', (d) => h.seekBy((d && Number(d.seekOffset)) || SEEK_STEP));
}
