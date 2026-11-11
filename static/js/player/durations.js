// video_id -> seconds, learned when the player first loads a video (Phase 2 adds songs.duration_seconds).
// Kept in localStorage["webjam.durations"] (try/catch), capped at 2000 entries.

const KEY = 'webjam.durations';
const CAP = 2000;
let cache = null;

function load() {
  if (cache) return cache;
  cache = new Map();
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]');
    if (Array.isArray(raw)) for (const [k, v] of raw) if (typeof k === 'string' && v > 0) cache.set(k, Number(v));
  } catch {
    /* storage blocked or corrupt: start empty */
  }
  return cache;
}

export function getDuration(videoId) {
  return load().get(videoId) || null;
}

/** Returns true when the value is new (so the caller can re-render rows). */
export function setDuration(videoId, seconds) {
  const s = Math.round(Number(seconds) || 0);
  if (!videoId || s <= 0) return false;
  const m = load();
  if (m.get(videoId) === s) return false;
  m.delete(videoId);
  m.set(videoId, s);
  while (m.size > CAP) m.delete(m.keys().next().value);
  try {
    localStorage.setItem(KEY, JSON.stringify([...m.entries()]));
  } catch {
    /* quota or blocked: keep it in memory */
  }
  return true;
}

/** Length shown in a row: the clip length when clipped, else the full duration; null when unknown. */
export function displayLength(row) {
  const full = getDuration(row.video_id);
  const s = row.start_seconds != null ? Number(row.start_seconds) : 0;
  const e = row.end_seconds != null ? Number(row.end_seconds) : full;
  if (e == null) return null;
  const len = e - s;
  return len > 0 ? len : null;
}
