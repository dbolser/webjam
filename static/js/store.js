// The single source of truth. Views hold no copies of server data; they render from here.
// Mutate only through update(); listeners are notified once per microtask.

import * as api from './api.js';

export const state = {
  auth: { mode: null /* 'google' | 'demo' after bootstrap */, signedIn: false, user: null /* bootstrap current_user */, crateId: null, users: [] },
  route: { name: '', params: {}, query: {}, path: '/' },
  playlists: new Map(), // id -> { meta, version, order: [trackId], related_count, kind, ownerId, loadedAt }
  tracks: new Map(), // trackId -> row (GET /api/playlists/{id} row shape)
  dock: null, // GET /api/me/dock shape
  home: null, // GET /api/me/home shape
  undo: null, // { can_undo, undo_label, undo_group_id, can_redo, redo_label, redo_group_id }
  selection: { playlistId: null, ids: new Set(), anchorId: null, cursorId: null, insertAfterId: undefined },
  clipboard: { items: [], mode: 'copy', fromPlaylistId: null },
  player: { queue: [], index: -1, status: 'idle', time: 0, duration: 0, seam: null, source: null },
  ui: { theme: 'system', cratePanel: false, newPlaylist: null /* 'home' | 'dock' | null */, notice: null },
  landed: new Set(), // track ids that render with .is-new for --dur-land
  dead: new Set(), // video ids YouTube refused this session
};

const anyListeners = new Set();
const selectorListeners = new Set();
let scheduled = false;
let reasons = new Set();

function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.is(a[k], b[k]));
}

function flush() {
  scheduled = false;
  const rs = reasons;
  reasons = new Set();
  for (const fn of [...anyListeners]) {
    try { fn(rs); } catch (err) { console.error(err); }
  }
  for (const entry of [...selectorListeners]) {
    let next;
    try { next = entry.selector(state); } catch (err) { console.error(err); continue; }
    if (!shallowEqual(next, entry.prev)) {
      const prev = entry.prev;
      entry.prev = next;
      try { entry.cb(next, prev); } catch (err) { console.error(err); }
    }
  }
}

/** Mutate state in place, then notify once per microtask. `reason` is a short tag for debugging
 *  and for listeners that want to skip some updates (the player uses 'tick'). */
export function update(mutator, reason = '') {
  if (mutator) mutator(state);
  reasons.add(reason);
  if (!scheduled) {
    scheduled = true;
    queueMicrotask(flush);
  }
}

/** cb(selected, prev) whenever selector(state) changes (shallow, Object.is per key). Returns an unsubscribe. */
export function subscribe(selector, cb) {
  const entry = { selector, cb, prev: selector(state) };
  selectorListeners.add(entry);
  return () => selectorListeners.delete(entry);
}

/** cb(reasons: Set<string>) after every flush. Returns an unsubscribe. */
export function onChange(cb) {
  anyListeners.add(cb);
  return () => anyListeners.delete(cb);
}

// --- playlists and rows ---------------------------------------------------------------------

/** Rows of a loaded playlist in order (temp rows included). */
export function rowsOf(playlistId) {
  const p = state.playlists.get(Number(playlistId));
  if (!p) return [];
  return p.order.map((id) => state.tracks.get(id)).filter(Boolean);
}

export function isMine(playlistId) {
  const p = state.playlists.get(Number(playlistId));
  const me = state.auth.user && state.auth.user.id;
  return !!(p && me && p.ownerId === me);
}

/** Put a GET /api/playlists/{id} payload into the maps. */
export function ingestPlaylist(detail) {
  const id = Number(detail.id);
  const tracks = Array.isArray(detail.tracks) ? detail.tracks : [];
  const old = state.playlists.get(id);
  const meta = { ...detail };
  delete meta.tracks;
  const order = tracks.map((t) => Number(t.id));
  const keep = new Set(order);
  if (old) {
    for (const tid of old.order) if (!keep.has(tid)) state.tracks.delete(tid);
  }
  for (const t of tracks) state.tracks.set(Number(t.id), { ...t, playlist_id: id });
  state.playlists.set(id, {
    meta,
    version: detail.version,
    order,
    related_count: detail.related_count ?? (old && old.related_count) ?? null,
    kind: detail.kind || 'playlist',
    ownerId: detail.user_id,
    loadedAt: Date.now(),
  });
}

const inflight = new Map();

/** GET /api/playlists/{id} into the maps, deduping concurrent loads. Resolves to the entry. */
export function loadPlaylist(id, { force = false } = {}) {
  const pid = Number(id);
  const have = state.playlists.get(pid);
  if (have && !force) return Promise.resolve(have);
  if (inflight.has(pid)) return inflight.get(pid);
  const p = api.get(`/api/playlists/${pid}`)
    .then((detail) => {
      update(() => ingestPlaylist(detail), 'load-playlist');
      return state.playlists.get(pid);
    })
    .finally(() => inflight.delete(pid));
  inflight.set(pid, p);
  return p;
}

export function forgetPlaylist(id) {
  const pid = Number(id);
  const p = state.playlists.get(pid);
  if (!p) return;
  update(() => {
    for (const tid of p.order) state.tracks.delete(tid);
    state.playlists.delete(pid);
  }, 'forget-playlist');
}

// --- "just landed" ----------------------------------------------------------------------------

const LAND_MS = 1600;
export function markLanded(ids) {
  const list = [...ids].map(Number).filter((n) => n > 0);
  if (!list.length) return;
  for (const id of list) state.landed.add(id);
  setTimeout(() => update(() => list.forEach((id) => state.landed.delete(id)), 'landed-clear'), LAND_MS);
}

// --- optimistic edits -------------------------------------------------------------------------

let nextTemp = -1;
export function tempId() {
  nextTemp -= 1;
  return nextTemp;
}

const tokens = new Map();
let nextToken = 1;

function placeIn(order, ids, { after_track_id, position } = {}) {
  if (after_track_id != null) {
    const at = order.indexOf(Number(after_track_id));
    if (at >= 0) {
      order.splice(at + 1, 0, ...ids);
      return;
    }
  }
  if (position != null) {
    const at = Math.max(0, Math.min(order.length, Number(position) - 1));
    order.splice(at, 0, ...ids);
    return;
  }
  order.push(...ids);
}

/** Apply a local preview of one op (insert, delete, move); returns a token for rollback().
 *  insert items may carry `row` (a display row) so the temp row renders with a title and thumbnail. */
export function optimistic(op) {
  const token = nextToken++;
  const snap = { orders: new Map(), temps: [], rows: new Map() };
  const touch = (pid) => {
    const p = state.playlists.get(Number(pid));
    if (p && !snap.orders.has(p)) snap.orders.set(p, p.order.slice());
    return p;
  };
  update(() => {
    if (op.kind === 'insert') {
      const p = touch(op.playlist_id);
      if (!p) return;
      const ids = (op.items || []).map((it) => {
        const id = tempId();
        snap.temps.push(id);
        state.tracks.set(id, {
          ...(it.row || {}),
          id,
          playlist_id: Number(op.playlist_id),
          video_id: it.video_id,
          start_seconds: it.start_seconds ?? null,
          end_seconds: it.end_seconds ?? null,
          track_note: it.note ?? (it.row && it.row.track_note) ?? '',
          pending: true,
        });
        return id;
      });
      placeIn(p.order, ids, op);
    } else if (op.kind === 'delete') {
      const p = touch(op.playlist_id);
      if (!p) return;
      const drop = new Set((op.track_ids || []).map(Number));
      p.order = p.order.filter((id) => !drop.has(id));
    } else if (op.kind === 'move') {
      const src = touch(op.playlist_id);
      const dstId = op.to_playlist_id ?? op.playlist_id;
      const dst = touch(dstId);
      const moving = new Set((op.track_ids || []).map(Number));
      const block = src ? src.order.filter((id) => moving.has(id)) : [...moving];
      if (src) src.order = src.order.filter((id) => !moving.has(id));
      if (dst) {
        if (dst !== src) {
          for (const id of block) {
            const row = state.tracks.get(id);
            if (row) {
              snap.rows.set(id, row);
              state.tracks.set(id, { ...row, playlist_id: Number(dstId) });
            }
          }
        }
        placeIn(dst.order, block, op);
      }
    }
  }, 'optimistic');
  tokens.set(token, snap);
  return token;
}

/** Undo an optimistic preview (after a failed request). */
export function rollback(token) {
  const snap = tokens.get(token);
  if (!snap) return;
  tokens.delete(token);
  update(() => {
    if (snap.temps.length && !snap.rows.size) {
      // An insert: take out only its own temp rows, so earlier edits reconciled since stay put.
      const drop = new Set(snap.temps);
      for (const [p] of snap.orders) p.order = p.order.filter((id) => !drop.has(id));
    } else {
      for (const [p, order] of snap.orders) p.order = order;
    }
    for (const id of snap.temps) state.tracks.delete(id);
    for (const [id, row] of snap.rows) state.tracks.set(id, row);
  }, 'rollback');
}

// temp id -> real id for the insert that just came back; read by the reconcile() that follows, so
// rows still queued behind it (V, V, V faster than the round trip) can keep their place.
let justResolved = new Map();

/** Forget a token without restoring (the server response replaced the preview). With the response,
 *  remember which real ids replaced the token's temp rows for the reconcile that follows. */
export function settle(token, res) {
  const snap = tokens.get(token);
  tokens.delete(token);
  if (!snap || !res || !snap.temps.length) return;
  const r0 = (res.results || [])[0];
  if (r0 && Array.isArray(r0.track_ids)) {
    snap.temps.forEach((t, i) => { if (r0.track_ids[i] != null) justResolved.set(t, Number(r0.track_ids[i])); });
  }
}

/** Temp ids of edits that are still queued or in flight (their previews must survive a reconcile). */
function pendingTemps() {
  const out = new Set();
  for (const snap of tokens.values()) for (const id of snap.temps) out.add(id);
  return out;
}

/** Put still-pending temp rows back into a server order, each after the row it followed. */
export function keepPending(oldOrder, serverOrder, pending, resolved = new Map()) {
  const out = serverOrder.slice();
  for (let i = 0; i < oldOrder.length; i++) {
    const id = oldOrder[i];
    if (!(id < 0) || !pending.has(id)) continue;
    let at = 0;
    for (let j = i - 1; j >= 0; j--) {
      const prev = resolved.has(oldOrder[j]) ? resolved.get(oldOrder[j]) : oldOrder[j];
      const k = out.indexOf(prev);
      if (k >= 0) {
        at = k + 1;
        break;
      }
    }
    out.splice(at, 0, id);
  }
  return out;
}

// --- reconcile --------------------------------------------------------------------------------

function landedFromResults(results) {
  const ids = [];
  for (const r of results || []) {
    if (Array.isArray(r.track_ids) && (r.kind === 'insert' || r.kind === 'restore')) ids.push(...r.track_ids);
    if (Array.isArray(r.moved)) ids.push(...r.moved.map((m) => m.track_id));
  }
  return ids;
}

/** Apply a /api/ops, /api/ops/undo|redo or promote response: versions, order (wholesale), rows,
 *  undo state, dock and home counts, and the "just landed" marks. Never refetches. */
export function reconcile(res) {
  if (!res || typeof res !== 'object') return;
  update(() => {
    const versions = res.versions || {};
    const orders = res.order || {};
    const rows = res.rows || {};
    const landed = new Set(landedFromResults(res.results));

    for (const [pidStr, row] of Object.entries(rows)) {
      const id = Number(pidStr);
      const prev = state.tracks.get(id) || {};
      state.tracks.set(id, { ...prev, ...row, id, pending: false });
    }

    for (const [pidStr, order] of Object.entries(orders)) {
      const pid = Number(pidStr);
      const p = state.playlists.get(pid);
      const ids = (order || []).map(Number);
      if (p) {
        const before = new Set(p.order.filter((id) => id > 0));
        const pending = pendingTemps();
        for (const id of p.order) if (id < 0 && !pending.has(id)) state.tracks.delete(id);
        const keep = new Set(ids);
        for (const id of before) {
          if (!keep.has(id)) {
            const row = state.tracks.get(id);
            if (row && Number(row.playlist_id) === pid) state.tracks.delete(id);
          }
        }
        if (before.size || p.order.length) for (const id of ids) if (!before.has(id)) landed.add(id);
        p.order = keepPending(p.order, ids, pending, justResolved);
        if (versions[pidStr] != null) p.version = versions[pidStr];
        if (p.meta) p.meta.version = p.version;
      }
      touchCounts(pid, ids, versions[pidStr], rows);
    }
    for (const [pidStr, v] of Object.entries(versions)) {
      const p = state.playlists.get(Number(pidStr));
      if (p && !(pidStr in orders)) {
        p.version = v;
        if (p.meta) p.meta.version = v;
      }
    }
    if (res.undo) state.undo = res.undo;
    markLanded(landed);
  }, 'reconcile');
  justResolved = new Map();
}

function touchCounts(pid, ids, version, rows) {
  const dock = state.dock;
  if (dock) {
    const chips = [dock.crate, ...(dock.pinned || []), ...(dock.recent || [])].filter(Boolean);
    let changed = false;
    for (const chip of chips) {
      if (Number(chip.playlist_id) === pid) {
        chip.track_count = ids.length;
        if (version != null) chip.version = version;
        changed = true;
      }
    }
    if (changed) state.dock = { ...dock };
  }
  const home = state.home;
  if (home) {
    for (const card of home.playlists || []) {
      if (Number(card.id) === pid) {
        card.track_count = ids.length;
        if (version != null) card.version = version;
      }
    }
    if (home.crate && Number(home.crate.id) === pid) {
      const known = new Map();
      for (const t of home.crate.recent || []) known.set(Number(t.id), t);
      for (const [id, row] of Object.entries(rows || {})) known.set(Number(id), row);
      for (const id of ids) {
        if (!known.has(id) && state.tracks.has(id)) known.set(id, state.tracks.get(id));
      }
      const live = ids.map((id) => known.get(id)).filter(Boolean);
      live.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || b.id - a.id);
      home.crate = { ...home.crate, track_count: ids.length, version: version ?? home.crate.version, recent: live.slice(0, 5) };
    }
    state.home = { ...home };
  }
}

/** A queued op failed with stale_version / anchor_missing: refetch the touched playlists. */
export async function refreshPlaylists(ids) {
  await Promise.all(
    [...new Set(ids.map(Number))].filter((id) => state.playlists.has(id)).map((id) =>
      loadPlaylist(id, { force: true }).catch(() => forgetPlaylist(id))),
  );
}

/** A 409 carried the server's current versions ({ "17": 2 }): adopt them for the playlists we
 *  only know through the dock or home (no loaded copy to refetch), so the next send can succeed. */
export function adoptVersions(versions) {
  if (!versions || typeof versions !== 'object') return;
  const entries = Object.entries(versions)
    .map(([k, v]) => [Number(k), Number(v)])
    .filter(([k, v]) => k > 0 && Number.isFinite(v));
  if (!entries.length) return;
  update(() => {
    for (const [pid, v] of entries) {
      if (state.playlists.has(pid)) continue; // refreshPlaylists refetches these
      const dock = state.dock;
      if (dock) {
        for (const chip of [dock.crate, ...(dock.pinned || []), ...(dock.recent || [])]) {
          if (chip && Number(chip.playlist_id) === pid) chip.version = v;
        }
        state.dock = { ...dock };
      }
      const home = state.home;
      if (home) {
        for (const card of home.playlists || []) if (Number(card.id) === pid) card.version = v;
        if (home.crate && Number(home.crate.id) === pid) home.crate = { ...home.crate, version: v };
        state.home = { ...home };
      }
    }
  }, 'versions');
}

/** expected_versions for `pids` from what this tab knows (a loaded playlist, else its dock chip).
 *  Pass `() => expectedFor([...])` to api.ops.send so it is read at send time, not when queued. */
export function expectedFor(pids) {
  const out = {};
  for (const raw of pids || []) {
    const id = Number(raw);
    if (!(id > 0)) continue;
    const p = state.playlists.get(id);
    let v = p && p.version != null ? p.version : null;
    const d = state.dock;
    if (v == null && d) {
      for (const c of [d.crate, ...(d.pinned || []), ...(d.recent || [])]) {
        if (c && Number(c.playlist_id) === id && c.version != null) v = c.version;
      }
    }
    if (v != null) out[String(id)] = v;
  }
  return out;
}
