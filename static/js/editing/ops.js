// Lane A's edit pipeline: every track edit goes through submit() so that
//   - the change shows at once (store.optimistic) and is reconciled from the /api/ops response;
//   - edits are sent one after another, each with expected_versions read at SEND time (so quick
//     V, V, V or Alt-↓ ×3 never trip over their own version bumps);
//   - anchors and ids that were still temporary (negative) when the key was pressed are mapped to
//     the real ids the earlier response created, so V, V, V chains after each previous paste;
//   - the new rows become the selection, and every edit toast carries "Undo".
// Nothing here refetches a page; a 409 refetches only the touched playlist (api.ops does that).

import { state, update, optimistic, rollback, markLanded } from '../store.js';
import * as api from '../api.js';
import { toast } from '../ui/toast.js';

const realOf = new Map(); // temp or moved-away id -> the id that replaced it
let chain = Promise.resolve();
let lastToast = null;
let undoHook = null;

/** undo.js registers its undo runner here so edit toasts can offer "Undo" without a cycle. */
export function setUndoHook(fn) {
  undoHook = fn;
}

/** A row id as rendered in data-track-id: numeric ids (playlist rows, temp rows) become numbers;
 *  synthetic ids of rows outside any playlist (search `s:VID`, discover `d:VID`, profile, the track
 *  page's `a:VID`) stay strings, so they still match the row objects they came from. */
export function rowKey(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number') return v;
  const s = String(v);
  return /^-?\d+$/.test(s) ? Number(s) : s;
}

/** Follow temp -> real (and moved-from -> moved-to) mappings. Synthetic string ids pass through. */
export function real(id) {
  if (typeof id === 'string' && !/^-?\d+$/.test(id)) return id;
  let v = Number(id);
  for (let i = 0; i < 8 && realOf.has(v); i++) v = realOf.get(v);
  return v;
}

export function versionOf(pid) {
  const id = Number(pid);
  const p = state.playlists.get(id);
  if (p && p.version != null) return p.version;
  const d = state.dock;
  if (d) {
    for (const c of [d.crate, ...(d.pinned || []), ...(d.recent || [])]) {
      if (c && Number(c.playlist_id) === id && c.version != null) return c.version;
    }
  }
  return null;
}

function touched(op) {
  const ids = [Number(op.playlist_id)];
  if (op.to_playlist_id != null) ids.push(Number(op.to_playlist_id));
  return [...new Set(ids)];
}

function orderSet(pid) {
  const p = state.playlists.get(Number(pid));
  return new Set(p ? p.order : []);
}

function newTemps(pid, before) {
  const p = state.playlists.get(Number(pid));
  return p ? p.order.filter((id) => id < 0 && !before.has(id)) : [];
}

/** Resolve temp ids in an op just before it is sent. Returns null when nothing is left to do. */
function resolveOp(op) {
  const out = { ...op };
  if (out.after_track_id != null) {
    const a = real(out.after_track_id);
    if (a > 0) out.after_track_id = a;
    else delete out.after_track_id; // the anchor never landed: append instead
  }
  if (Array.isArray(out.track_ids)) {
    out.track_ids = out.track_ids.map(real).filter((id) => id > 0);
    if (!out.track_ids.length) return null;
  }
  if (out.track_id != null) {
    out.track_id = real(out.track_id);
    if (!(out.track_id > 0)) return null;
  }
  if (Array.isArray(out.items)) out.items = out.items.map(cleanItem);
  return out;
}

/** An insert item with only the fields /api/ops accepts (it forbids extras). */
export function cleanItem(it) {
  const o = { video_id: it.video_id };
  if (it.start_seconds != null && it.start_seconds !== '') o.start_seconds = it.start_seconds;
  if (it.end_seconds != null && it.end_seconds !== '') o.end_seconds = it.end_seconds;
  if (it.note) o.note = String(it.note).slice(0, 5000);
  if (it.display_title) o.display_title = String(it.display_title).slice(0, 200);
  const r = it.ref;
  if (r && (r.track_id || r.playlist_id)) {
    const tid = real(r.track_id);
    if (tid > 0) o.ref = { track_id: tid };
    else if (Number(r.playlist_id) > 0) o.ref = { playlist_id: Number(r.playlist_id) };
  }
  return o;
}

function setSelectionTo(ids, listKey, playlistId) {
  if (!ids.length) return;
  update((s) => {
    s.selection = {
      ...s.selection,
      listKey: listKey ?? s.selection.listKey,
      playlistId: playlistId ?? s.selection.playlistId,
      ids: new Set(ids),
      anchorId: ids[0],
      cursorId: ids[ids.length - 1],
      insertAfterId: ids[ids.length - 1],
    };
  }, 'select');
}

function remapSelection() {
  const sel = state.selection;
  if (!sel || !sel.ids) return;
  const ids = new Set([...sel.ids].map(real));
  const cursorId = sel.cursorId != null ? real(sel.cursorId) : sel.cursorId;
  const anchorId = sel.anchorId != null ? real(sel.anchorId) : sel.anchorId;
  update((s) => {
    s.selection = { ...s.selection, ids, cursorId, anchorId, insertAfterId: cursorId };
  }, 'select');
}

function dropFromSelection(ids) {
  const drop = new Set(ids);
  const sel = state.selection;
  if (!sel || !sel.ids || ![...sel.ids].some((id) => drop.has(id))) return;
  update((s) => {
    const left = [...s.selection.ids].filter((id) => !drop.has(id));
    s.selection = { ...s.selection, ids: new Set(left), cursorId: drop.has(s.selection.cursorId) ? left[left.length - 1] ?? null : s.selection.cursorId };
  }, 'select');
}

/** Show an edit toast; consecutive edit toasts replace each other so V, V, V stays calm. */
export function editToast(text, { undo = true } = {}) {
  if (lastToast != null) toast.dismiss(lastToast);
  const action = undo && undoHook ? { label: 'Undo', run: () => undoHook() } : null;
  lastToast = toast.show(text, action ? { action } : {});
  return lastToast;
}

/**
 * submit(op, opts) -> Promise<response | null>
 *   op       one public op (insert | delete | move | update | set_meta)
 *   rows     display rows for insert items (same order), so temp rows render with title + thumb
 *   select   { listKey, playlistId }: make the new rows (insert, or the moved block) the selection
 *   toastText  string or (res) => string; shown with "Undo" on success
 *   groupId  share one undo group (a big paste sent in chunks)
 *   landed   ids to mark "just landed" on success (a move within a playlist)
 *   apply / revert  local (non-order) optimistic changes, e.g. a row's note or a title
 */
export function submit(op, opts = {}) {
  const { rows = null, select = null, toastText = null, groupId = null, landed = null, apply = null, revert = null } = opts;
  const dst = op.kind === 'move' ? op.to_playlist_id ?? op.playlist_id : op.playlist_id;
  const before = orderSet(dst);
  let token = null;
  if (op.kind === 'insert' || op.kind === 'delete' || op.kind === 'move') {
    const preview = op.kind === 'insert' && rows
      ? { ...op, items: op.items.map((it, i) => ({ ...it, row: rows[i] })) }
      : op;
    token = optimistic(preview);
  }
  if (apply) update(apply, 'optimistic');
  const temps = op.kind === 'insert' ? newTemps(dst, before) : [];
  if (select && op.kind === 'insert') setSelectionTo(temps, select.listKey, select.playlistId);
  if (select && op.kind === 'move') setSelectionTo(op.track_ids.map(Number), select.listKey, select.playlistId);
  if (op.kind === 'delete') dropFromSelection(op.track_ids.map(Number));

  const run = chain.then(async () => {
    const sendOp = resolveOp(op);
    if (!sendOp) {
      if (token != null) rollback(token);
      return null;
    }
    const expectedVersions = {};
    for (const pid of touched(sendOp)) {
      const v = versionOf(pid);
      if (v != null) expectedVersions[String(pid)] = v;
    }
    let res;
    try {
      res = await api.ops.send([sendOp], { token, groupId, expectedVersions });
    } catch (err) {
      if (revert) update(revert, 'rollback');
      dropFromSelection(temps);
      throw err;
    }
    const r0 = (res.results || [])[0] || {};
    if (op.kind === 'insert' && Array.isArray(r0.track_ids)) {
      temps.forEach((t, i) => { if (r0.track_ids[i] != null) realOf.set(t, Number(r0.track_ids[i])); });
    }
    if (op.kind === 'move' && Array.isArray(r0.moved)) {
      for (const m of r0.moved) if (m.from_track_id !== m.track_id) realOf.set(Number(m.from_track_id), Number(m.track_id));
    }
    remapSelection();
    if (landed) markLanded(landed.map(real));
    if (toastText) editToast(typeof toastText === 'function' ? toastText(res) : toastText);
    return res;
  });
  chain = run.catch(() => {});
  return run.catch((err) => {
    if (err && !err.handled) toast.show(err.detail || err.message || 'That change was not saved.');
    return null;
  });
}

/** Wait for every queued edit (used before navigating to a freshly created playlist). */
export function settled() {
  return chain;
}
