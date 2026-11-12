// "Resume where you left off" (docs/phase1b-shell.md §12.3). Pure logic: no DOM, no globals except
// what the caller passes in, so tests/js/player_helpers.mjs can run it under node.
//
// One record per person per browser, in localStorage["webjam.resume.u<id>"] (signed in) or
// ["webjam.resume.anon"]. Keying by user means a second person signing in on the same browser
// never sees what the first was playing, and a crate never lands under the anonymous key.
//
//   resumeKey(userId)                          the storage key
//   snapshot({ item, index, source, time, duration, userId, now })  -> record | null
//   parse(raw, now)                            JSON string -> record | null (bad, old or expired)
//   resumeAt(record)                           where to start, inside the clip
//   readResume(storage, userId, now)  writeResume(storage, userId, record)  clearResume(storage, userId)

export const RESUME_VERSION = 1;
export const RESUME_TTL_MS = 7 * 24 * 60 * 60 * 1000;
/** Closer than this to the end of the clip counts as finished: nothing worth resuming. */
export const RESUME_TAIL = 3;

const VID = /^[\w-]{11}$/;
const MAX_TEXT = 200;

export function resumeKey(userId) {
  const id = Number(userId);
  return Number.isInteger(id) && id > 0 ? `webjam.resume.u${id}` : 'webjam.resume.anon';
}

const num = (v) => (v == null || v === '' ? null : Number.isFinite(Number(v)) ? Number(v) : null);
const text = (v) => (typeof v === 'string' ? v.slice(0, MAX_TEXT) : '');
const posInt = (v) => (Number.isInteger(Number(v)) && Number(v) > 0 ? Number(v) : null);

/** The record to store for what is playing now, or null when there is nothing worth resuming. */
export function snapshot({ item, index = 0, source = null, time = 0, duration = 0, userId = null, now = Date.now() } = {}) {
  if (!item || !VID.test(String(item.video_id || ''))) return null;
  const start = Math.max(0, num(item.start) || 0);
  const end = num(item.end);
  const stop = end != null ? end : num(duration) || null;
  let pos = num(time);
  if (pos == null) return null;
  pos = Math.max(start, pos);
  if (stop != null && pos >= stop - RESUME_TAIL) return null;
  let src = null;
  if (source && (source.kind || source.playlistId)) {
    const kind = source.kind === 'crate' ? 'crate' : 'playlist';
    // A crate is private: only ever stored under its owner's key.
    if (kind === 'crate' && !posInt(userId)) return null;
    const pid = posInt(source.playlistId);
    if (pid) src = { kind, playlistId: pid, title: kind === 'crate' ? 'Crate' : text(source.title) };
  }
  return {
    v: RESUME_VERSION,
    src,
    trackId: posInt(item.trackId),
    videoId: item.video_id,
    index: Math.max(0, Math.floor(num(index) || 0)),
    start: num(item.start),
    end,
    title: text(item.title) || item.video_id,
    channel: text(item.channel),
    pos: Math.round(pos * 10) / 10,
    at: Math.round(num(now) || 0),
  };
}

/** Validates a stored record. Returns null for junk, other versions and anything older than 7 days. */
export function parse(raw, now = Date.now()) {
  if (typeof raw !== 'string' || !raw) return null;
  let r;
  try { r = JSON.parse(raw); } catch { return null; }
  if (!r || typeof r !== 'object' || r.v !== RESUME_VERSION) return null;
  if (!VID.test(String(r.videoId || ''))) return null;
  const at = num(r.at);
  if (at == null || at > now + 60_000 || now - at > RESUME_TTL_MS) return null;
  const pos = num(r.pos);
  if (pos == null || pos < 0) return null;
  let src = null;
  if (r.src && typeof r.src === 'object') {
    const pid = posInt(r.src.playlistId);
    if (pid) src = { kind: r.src.kind === 'crate' ? 'crate' : 'playlist', playlistId: pid, title: text(r.src.title) };
  }
  return {
    v: RESUME_VERSION,
    src,
    trackId: posInt(r.trackId),
    videoId: r.videoId,
    index: Math.max(0, Math.floor(num(r.index) || 0)),
    start: num(r.start),
    end: num(r.end),
    title: text(r.title) || r.videoId,
    channel: text(r.channel),
    pos,
    at,
  };
}

/** Where to start playing: the saved position, kept inside the clip's start and end. */
export function resumeAt(record) {
  if (!record) return 0;
  const start = record.start || 0;
  let t = Math.max(start, record.pos || 0);
  if (record.end != null) t = Math.min(t, Math.max(start, record.end - RESUME_TAIL));
  return t;
}

/** The record as a queue item (queue.js shape), for a queue of one when the list can't load. */
export function itemFromRecord(record) {
  return {
    trackId: record.src ? record.trackId : null,
    playlistId: record.src ? record.src.playlistId : null,
    video_id: record.videoId,
    start: record.start,
    end: record.end,
    title: record.title,
    channel: record.channel,
    thumb: `https://i.ytimg.com/vi/${record.videoId}/mqdefault.jpg`,
  };
}

/** Index of the saved track in a freshly loaded queue: by track id, then by index, then by video. */
export function findInQueue(record, items) {
  if (!record || !Array.isArray(items)) return -1;
  if (record.trackId) {
    const i = items.findIndex((it) => it && it.trackId === record.trackId);
    if (i >= 0) return i;
  }
  const at = items[record.index];
  if (at && at.video_id === record.videoId) return record.index;
  return items.findIndex((it) => it && it.video_id === record.videoId);
}

export function readResume(storage, userId, now = Date.now()) {
  const key = resumeKey(userId);
  let raw = null;
  try { raw = storage.getItem(key); } catch { return null; }
  const r = parse(raw, now);
  if (!r && raw != null) clearResume(storage, userId);
  // Belt and braces: a crate under the anonymous key is never offered.
  if (r && r.src && r.src.kind === 'crate' && key.endsWith('.anon')) return null;
  return r;
}

export function writeResume(storage, userId, record) {
  if (!record) return false;
  if (record.src && record.src.kind === 'crate' && !posInt(userId)) return false;
  try {
    storage.setItem(resumeKey(userId), JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

export function clearResume(storage, userId) {
  try { storage.removeItem(resumeKey(userId)); } catch { /* blocked */ }
}
