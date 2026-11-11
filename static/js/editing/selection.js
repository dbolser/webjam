// Selection, the roving cursor and the insertion line (docs/phase1b-shell.md §5.2).
//
// state.selection = { listKey, playlistId, ids: Set, anchorId, cursorId, insertAfterId }
// Selection belongs to one rendered list at a time (data-list-key); selecting in another list
// clears the first. Arrow keys move the cursor and the selection follows it (select-follows-focus),
// so "the insertion cursor sits after the last selected row" is always where V will paste.

import { state, update, onChange } from '../store.js';
import { listRows } from '../components/track-row.js';
import { real } from './ops.js';

const EMPTY = () => ({ listKey: null, playlistId: null, ids: new Set(), anchorId: null, cursorId: null, insertAfterId: undefined });

export function listEl(key) {
  if (!key) return null;
  return document.querySelector(`[data-list-key="${CSS.escape(String(key))}"]`);
}

export function rowsOfList(key) {
  return listRows(key).rows || [];
}

function listPlaylistId(list) {
  const v = list && list.dataset.playlistId;
  return v ? Number(v) : null;
}

export function isEditableList(list) {
  return !!(list && list.matches('[role="listbox"][data-editable]'));
}

/** The rows that are selected, in list order; with nothing selected, the focused/cursor row. */
export function selectedRows({ fallbackRow = null } = {}) {
  const sel = state.selection;
  const rows = rowsOfList(sel.listKey);
  const ids = sel.ids || new Set();
  const out = rows.filter((r) => ids.has(r.id));
  if (out.length) return out;
  if (fallbackRow) return [fallbackRow];
  const cur = rows.find((r) => r.id === sel.cursorId);
  return cur ? [cur] : [];
}

export function selectionCount() {
  const sel = state.selection;
  return sel && sel.ids ? sel.ids.size : 0;
}

/** Where V pastes in `key`: after the last selected row, else after the cursor row, else at the start (null). */
export function insertAfterId(key) {
  const sel = state.selection;
  const rows = rowsOfList(key);
  if (sel.listKey === key) {
    const ids = sel.ids || new Set();
    for (let i = rows.length - 1; i >= 0; i--) if (ids.has(rows[i].id)) return rows[i].id;
    if (sel.cursorId != null && rows.some((r) => r.id === sel.cursorId)) return sel.cursorId;
  }
  return null;
}

export function setSelection(next) {
  update((s) => {
    s.selection = { ...EMPTY(), ...next, ids: new Set(next.ids || []) };
    s.selection.insertAfterId = s.selection.cursorId;
  }, 'select');
}

export function clearSelection() {
  if (!selectionCount() && state.selection.cursorId == null) return false;
  const keep = state.selection;
  update((s) => {
    s.selection = { ...EMPTY(), listKey: keep.listKey, playlistId: keep.playlistId, cursorId: keep.cursorId };
  }, 'select');
  return true;
}

/** Click on a row: plain selects one, Shift extends from the anchor, Mod toggles. */
export function clickSelect(rowEl, { shift = false, toggle = false } = {}) {
  const list = rowEl.closest('[data-list-key]');
  if (!list) return;
  const key = list.dataset.listKey;
  const id = Number(rowEl.dataset.trackId);
  const rows = rowsOfList(key);
  const sel = state.selection;
  const same = sel.listKey === key;
  const pid = listPlaylistId(list);
  if (shift && same && sel.anchorId != null) {
    const a = rows.findIndex((r) => r.id === sel.anchorId);
    const b = rows.findIndex((r) => r.id === id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      setSelection({ listKey: key, playlistId: pid, ids: rows.slice(lo, hi + 1).map((r) => r.id), anchorId: sel.anchorId, cursorId: id });
      return;
    }
  }
  if (toggle && same) {
    const ids = new Set(sel.ids);
    if (ids.has(id)) ids.delete(id);
    else ids.add(id);
    setSelection({ listKey: key, playlistId: pid, ids: [...ids], anchorId: id, cursorId: id });
    return;
  }
  setSelection({ listKey: key, playlistId: pid, ids: [id], anchorId: id, cursorId: id });
}

/** ↑/↓ (delta ±1), with Shift to extend. Returns false when there is no list to act on. */
export function moveCursor(list, delta, { extend = false } = {}) {
  if (!list) return false;
  const key = list.dataset.listKey;
  const rows = rowsOfList(key);
  if (!rows.length) return false;
  const sel = state.selection;
  const same = sel.listKey === key;
  let at = same ? rows.findIndex((r) => r.id === sel.cursorId) : -1;
  if (at < 0) {
    const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest('[data-track-id]');
    at = focused ? rows.findIndex((r) => r.id === Number(focused.dataset.trackId)) : -1;
  }
  const next = at < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, at + delta));
  const id = rows[next].id;
  const pid = listPlaylistId(list);
  if (extend && same && sel.anchorId != null) {
    const a = rows.findIndex((r) => r.id === sel.anchorId);
    const [lo, hi] = a < next ? [a, next] : [next, a];
    setSelection({ listKey: key, playlistId: pid, ids: rows.slice(Math.max(0, lo), hi + 1).map((r) => r.id), anchorId: sel.anchorId, cursorId: id });
  } else {
    setSelection({ listKey: key, playlistId: pid, ids: [id], anchorId: id, cursorId: id });
  }
  focusRow(key, id, { scroll: true });
  return true;
}

export function selectAll(list) {
  if (!list) return false;
  const key = list.dataset.listKey;
  const rows = rowsOfList(key);
  if (!rows.length) return false;
  const cur = state.selection.listKey === key ? state.selection.cursorId : null;
  setSelection({ listKey: key, playlistId: listPlaylistId(list), ids: rows.map((r) => r.id), anchorId: rows[0].id, cursorId: cur ?? rows[rows.length - 1].id });
  return true;
}

/** Focus a row after the next render (the row may not exist yet). */
export function focusRow(key, id, { scroll = false } = {}) {
  requestAnimationFrame(() => {
    const list = listEl(key);
    const el = list && list.querySelector(`[data-track-id="${real(id)}"]`);
    if (!el) return;
    el.focus({ preventScroll: true });
    if (scroll) el.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  });
}

// --- the insertion line ---------------------------------------------------------------------------

/** Draw the list's insertion line after row `afterId` (null = at the top). `hint` = the quieter
 *  "V pastes here" look; pass afterId === undefined to hide it. */
export function drawInsertLine(list, afterId, { hint = false } = {}) {
  const line = list && list.querySelector(':scope > .insert-line');
  if (!line) return;
  if (afterId === undefined) {
    line.hidden = true;
    return;
  }
  let y = 0;
  if (afterId != null) {
    const row = list.querySelector(`:scope > [data-track-id="${afterId}"]`);
    if (!row) {
      line.hidden = true;
      return;
    }
    y = row.offsetTop + row.offsetHeight;
  } else {
    const first = list.querySelector(':scope > [data-track-id]');
    y = first ? first.offsetTop : 0;
  }
  line.style.setProperty('--y', `${y}px`);
  line.classList.toggle('ed-hint', !!hint);
  line.hidden = false;
}

export function hideAllInsertLines() {
  for (const line of document.querySelectorAll('.tracks > .insert-line')) line.hidden = true;
}

let dragging = false;
export function setDragging(on) {
  dragging = on;
}

function refreshHint() {
  if (dragging) return;
  hideAllInsertLines();
  const active = document.activeElement;
  const list = active && active.closest ? active.closest('[role="listbox"][data-list-key]') : null;
  if (!list || !isEditableList(list)) return;
  if (!(state.clipboard && state.clipboard.items && state.clipboard.items.length)) return;
  drawInsertLine(list, insertAfterId(list.dataset.listKey), { hint: true });
}

// --- keep focus on the cursor row across re-renders ------------------------------------------------
// A pasted row is first a temp row (negative id) and is replaced by the real one when the response
// lands; lit's keyed repeat swaps the <li>, which would drop focus to <body> and stop V, V, V.

let pending = false;
function afterRender() {
  pending = false;
  const active = document.activeElement;
  const sel = state.selection;
  if ((!active || active === document.body) && lastFocusedKey && sel.cursorId != null && sel.listKey === lastFocusedKey) {
    const list = listEl(sel.listKey);
    const el = list && list.querySelector(`[data-track-id="${real(sel.cursorId)}"]`);
    if (el) el.focus({ preventScroll: true });
  }
  refreshHint();
}

let lastFocusedKey = null;
let started = false;

export function startSelection() {
  if (started) return;
  started = true;
  onChange((reasons) => {
    if (reasons.size === 1 && reasons.has('tick')) return;
    const active = document.activeElement;
    const list = active && active.closest ? active.closest('[role="listbox"][data-list-key]') : null;
    lastFocusedKey = list ? list.dataset.listKey : null;
    if (!pending) {
      pending = true;
      requestAnimationFrame(afterRender);
    }
  });
  document.addEventListener('focusin', () => refreshHint());
  document.addEventListener('focusout', () => requestAnimationFrame(() => {
    if (!pending) refreshHint();
  }));
}

/** Forget the selection when the list it belonged to is gone (navigation). */
export function pruneSelection() {
  const sel = state.selection;
  if (!sel.listKey) return;
  if (!listEl(sel.listKey)) update((s) => { s.selection = EMPTY(); }, 'select');
}
