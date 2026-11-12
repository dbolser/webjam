// Formatting and URL builders. Every href bound from data goes through trackUrl, playlistUrl,
// userUrl or localUrl, so a data field can never become a script (or off-site) URL.

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const HANDLE = /^[a-z0-9_]{3,20}$/;

export function isVideoId(v) {
  return typeof v === 'string' && VIDEO_ID.test(v);
}

function posInt(n) {
  const v = Number(n);
  return Number.isSafeInteger(v) && v > 0 ? v : null;
}

/** "Basement Velvet & Motorik" -> "basement-velvet-motorik" (≤ 60 chars). */
export function slug(title) {
  return String(title || '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
    .replace(/-+$/g, '');
}

/** /p/{id}-{slug}[?track=tid][#hash]. Falls back to "/" for a bad id. */
export function playlistUrl(id, title, { track, hash } = {}) {
  const pid = posInt(id);
  if (!pid) return '/';
  const s = title ? slug(title) : '';
  const tid = posInt(track);
  const h = hash && /^[a-z0-9-]{1,40}$/.test(hash) ? `#${hash}` : '';
  return `/p/${pid}${s ? `-${s}` : ''}${tid ? `?track=${tid}` : ''}${h}`;
}

/** A YouTube thumbnail URL: the stored https one, else derived from the video id. */
export function thumbSrc(thumbnailUrl, videoId) {
  const u = String(thumbnailUrl || '');
  if (/^https:\/\/[a-z0-9.-]*ytimg\.com\//i.test(u)) return u;
  return isVideoId(videoId) ? `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg` : '';
}

/** /t/{video_id}[?s=&e=]. Falls back to "/" for a bad id. */
export function trackUrl(videoId, start, end) {
  if (!isVideoId(videoId)) return '/';
  const q = new URLSearchParams();
  const s = Number(start);
  const e = Number(end);
  if (start != null && Number.isFinite(s) && s > 0) q.set('s', String(Math.floor(s)));
  if (end != null && Number.isFinite(e) && e > 0) q.set('e', String(Math.floor(e)));
  const qs = q.toString();
  return `/t/${videoId}${qs ? `?${qs}` : ''}`;
}

/** /u/{handle}. Falls back to "/" for a handle the router would not accept. */
export function userUrl(handle) {
  const h = String(handle || '').toLowerCase();
  return HANDLE.test(h) ? `/u/${h}` : '/';
}

/** A same-origin path (starts with one "/", no scheme, no "//"); anything else becomes "/". */
/** /auth/google/login, with ?next=<current path> for an action-triggered sign-in (§3.9). The top-bar
 *  "Sign in" link stays bare so a deliberate sign-in lands on my page. */
export function signInUrl(next) {
  const n = next == null ? '' : localUrl(next);
  if (!n || n === '/' || /^\/(auth|api)\//.test(n) || n.length > 512) return '/auth/google/login';
  return `/auth/google/login?next=${encodeURIComponent(n)}`;
}

export function localUrl(path) {
  const p = String(path || '');
  if (!p.startsWith('/') || p.startsWith('//') || p.startsWith('/\\')) return '/';
  if (/[\u0000-\u001f\s]/.test(p)) return '/';
  return p;
}

/** 0 -> "0:00", 312 -> "5:12", 3725 -> "1:02:05". */
export function mmss(seconds) {
  const v = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  const s = v % 60;
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** Clip chip text: "5:12–8:18", "5:12–end", "0:00–8:18"; "" when unclipped. */
export function clipLabel(start, end) {
  const hasS = start != null && start !== '';
  const hasE = end != null && end !== '';
  if (!hasS && !hasE) return '';
  return `${mmss(hasS ? start : 0)}–${hasE ? mmss(end) : 'end'}`;
}

export function plural(n, one, many) {
  const v = Number(n) || 0;
  return `${v} ${v === 1 ? one : many || `${one}s`}`;
}

/** Server timestamps are "YYYY-MM-DD HH:MM:SS" in UTC. */
export function parseTs(ts) {
  if (!ts) return null;
  const d = new Date(String(ts).replace(' ', 'T') + (String(ts).endsWith('Z') ? '' : 'Z'));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "now", "4m", "3h", "2d", "5w", "Nov 2026". */
export function relTime(ts, now = Date.now()) {
  const d = parseTs(ts);
  if (!d) return '';
  const s = Math.max(0, (now - d.getTime()) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}d`;
  if (s < 86400 * 60) return `${Math.floor(s / (86400 * 7))}w`;
  return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

export function isoTs(ts) {
  const d = parseTs(ts);
  return d ? d.toISOString() : '';
}

/** Two-letter initials for avatars. */
export function initials(user) {
  const name = String((user && (user.display_name || user.username)) || '?').trim();
  const parts = name.split(/\s+/).filter(Boolean);
  const out = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : name.slice(0, 2);
  return out.toUpperCase();
}

/** An absolute URL for sharing (copy link). */
export function absoluteUrl(path) {
  return new URL(localUrl(path), location.origin).toString();
}

const ICONS = new Set(['play', 'pause', 'prev', 'next', 'plus', 'crate', 'more', 'share', 'heart', 'heart-fill',
  'undo', 'redo', 'close', 'search', 'chevron-down', 'grip', 'link', 'copy', 'seam', 'check', 'sun', 'moon', 'system']);

/** "#i-play" for <use href>, from the sprite in index.html; unknown names fall back to "#i-more". */
export function iconHref(name) {
  return ICONS.has(name) ? `#i-${name}` : '#i-more';
}
