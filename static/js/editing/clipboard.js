// Copy, cut, paste and duplicate (docs/phase1-ops-api.md §5, docs/phase1b-shell.md §4.4).
//
// ⌘/Ctrl-C on a focused list (with no text selected) writes three formats through the `copy`
// event: application/x-webjam+json, text/html with data-webjam, and text/plain lines of Webjam
// URLs. Crate and private rows are written as /t/{vid}?s=&e= lines, never /p/{crate}?track=.
// V (alone) or ⌘V pastes after the insertion cursor, and the pasted rows become the selection, so
// V, V, V gives three copies in order: three requests, three undo groups.
// A cut becomes a `move` on paste while every row is still in a playlist I own; else an `insert`.

import { state, update, isMine } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { register as registerPaste } from '../paste-router.js';
import { toast } from '../ui/toast.js';
import { trackUrl, playlistUrl, absoluteUrl, clipLabel, isVideoId, plural } from '../format.js';
import { submit, real, rowKey } from './ops.js';
import { selectedRows, insertAfterId, rowsOfList, isEditableList, focusRow } from './selection.js';

const MAX_ITEMS = 1000;
const CHUNK = 50;

// --- building the payload --------------------------------------------------------------------------

function hiddenPlaylist(pid) {
  const p = pid ? state.playlists.get(Number(pid)) : null;
  return !p || p.kind === 'crate' || (p.meta && p.meta.visibility === 'private');
}

function itemOf(row) {
  const pid = Number(row.playlist_id) || null;
  const tid = real(row.id);
  const hidden = hiddenPlaylist(pid);
  const p = pid ? state.playlists.get(pid) : null;
  const ref = tid > 0 ? { track_id: tid } : null;
  if (ref && !hidden) {
    ref.playlist_id = pid;
    if (p && p.ownerId) ref.user_id = p.ownerId;
    if (p && p.meta && p.meta.username) ref.username = p.meta.username;
  }
  return {
    video_id: row.video_id,
    start_seconds: row.start_seconds ?? null,
    end_seconds: row.end_seconds ?? null,
    note: row.track_note || '',
    display_title: row.display_title || null,
    ref,
    title: row.title || row.video_id,
    channel_title: row.channel_title || '',
    thumbnail_url: row.thumbnail_url || '',
  };
}

function urlOf(row) {
  const pid = Number(row.playlist_id) || null;
  const tid = real(row.id);
  if (pid && tid > 0 && !hiddenPlaylist(pid)) return absoluteUrl(playlistUrl(pid, null, { track: tid }));
  return absoluteUrl(trackUrl(row.video_id, row.start_seconds, row.end_seconds));
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** { json, html, text, payload } for the clipboard. Exported for tests and the share lane. */
export function buildPayload(rows, mode = 'copy') {
  const list = rows.filter((r) => r && isVideoId(r.video_id)).slice(0, MAX_ITEMS);
  const payload = {
    webjam: 1,
    origin: location.origin,
    copied_at: new Date().toISOString(),
    mode,
    items: list.map(itemOf),
  };
  const json = JSON.stringify(payload);
  const lines = list.map((r) => {
    const clip = clipLabel(r.start_seconds, r.end_seconds);
    return `${urlOf(r)}  ${r.title || r.video_id}${clip ? ` (${clip})` : ''}`;
  });
  const html = `<ul data-webjam='${esc(json)}'>${list.map((r) => `<li><a href="${esc(urlOf(r))}">${esc(r.title || r.video_id)}</a></li>`).join('')}</ul>`;
  return { json, html, text: lines.join('\n'), payload };
}

// --- copy / cut -------------------------------------------------------------------------------------

let pendingWrite = null;

function textSelected() {
  const s = window.getSelection && window.getSelection();
  return !!(s && !s.isCollapsed && String(s).trim());
}

function focusedList() {
  const a = document.activeElement;
  return a && a.closest ? a.closest('[role="listbox"][data-list-key]') : null;
}

function focusedRowObj(list) {
  const a = document.activeElement;
  const rowEl = a && a.closest ? a.closest('[data-track-id]') : null;
  if (!list || !rowEl) return null;
  const id = rowKey(rowEl.dataset.trackId);
  return rowsOfList(list.dataset.listKey).find((r) => r.id === id) || null;
}

/** Rows to copy: the selection in the focused list, else the focused row. */
function rowsToCopy(list) {
  const sel = state.selection;
  if (list && sel.listKey === list.dataset.listKey) {
    const rows = selectedRows({ fallbackRow: focusedRowObj(list) });
    if (rows.length) return rows;
  }
  const one = focusedRowObj(list);
  return one ? [one] : [];
}

function remember(rows, mode, built) {
  const list = focusedList();
  update((s) => {
    s.clipboard = {
      items: built.payload.items.map((it, i) => ({ ...it, track_id: real(rows[i].id), row: rows[i] })),
      mode,
      fromPlaylistId: list && list.dataset.playlistId ? Number(list.dataset.playlistId) : null,
      copiedAt: built.payload.copied_at,
    };
  }, 'clipboard');
}

/** ⌘C / ⌘X. Returns false when the key should fall through (text is selected, no list focused). */
export function copySelection({ cut = false } = {}) {
  if (textSelected()) return false;
  const list = focusedList();
  if (!list) return false;
  const rows = rowsToCopy(list);
  if (!rows.length) return false;
  const mode = cut && isEditableList(list) ? 'cut' : 'copy';
  const built = buildPayload(rows, mode);
  remember(rows, mode, built);
  pendingWrite = built;
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  if (pendingWrite) {
    // The copy event never ran (or execCommand is gone): fall back to plain text.
    pendingWrite = null;
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(built.text).catch(() => {});
    ok = true;
  }
  const n = rows.length;
  const what = n === 1 ? `‘${rows[0].title || rows[0].video_id}’` : plural(n, 'track');
  toast.show(mode === 'cut' ? `Cut ${what}. Paste to move ${n === 1 ? 'it' : 'them'}.` : `Copied ${what}`);
  return ok;
}

function onCopyEvent(e) {
  let p = pendingWrite;
  pendingWrite = null;
  if (!p) {
    // A copy from the browser's Edit menu while a list has focus.
    if (textSelected()) return;
    const list = focusedList();
    if (!list) return;
    const rows = rowsToCopy(list);
    if (!rows.length) return;
    const mode = e.type === 'cut' && isEditableList(list) ? 'cut' : 'copy';
    p = buildPayload(rows, mode);
    remember(rows, mode, p);
  }
  if (!e.clipboardData) return;
  e.clipboardData.setData('text/plain', p.text);
  e.clipboardData.setData('text/html', p.html);
  try {
    e.clipboardData.setData('application/x-webjam+json', p.json);
  } catch {
    /* custom types are Chromium-only; the html and text carry the same data */
  }
  e.preventDefault();
}

export function cancelCut() {
  if (state.clipboard.mode !== 'cut' || !state.clipboard.items.length) return false;
  update((s) => { s.clipboard = { ...s.clipboard, mode: 'copy' }; }, 'clipboard');
  toast.show('Cut cancelled; the tracks stay where they were.');
  return true;
}

// --- paste ------------------------------------------------------------------------------------------

function displayRow(it) {
  return {
    title: it.title || it.video_id,
    channel_title: it.channel_title || '',
    thumbnail_url: it.thumbnail_url || '',
    track_note: it.note || '',
    source_username: it.ref && it.ref.username && !(it.ref.user_id && state.auth.user && it.ref.user_id === state.auth.user.id) ? it.ref.username : null,
  };
}

function anchorIn(list) {
  const key = list.dataset.listKey;
  if (state.selection.listKey === key) return insertAfterId(key);
  const a = document.activeElement;
  const rowEl = a && a.closest ? a.closest('[data-track-id]') : null;
  return rowEl && list.contains(rowEl) ? rowKey(rowEl.dataset.trackId) : null;
}

function placement(after) {
  if (after === undefined) return {}; // the anchor is gone (a reconcile raced the key): append
  return after != null ? { after_track_id: after } : { position: 1 };
}

/** Is a cut still a move? Every row exists, in one playlist, and I own it. */
function cutSource(items) {
  const ids = items.map((it) => real(it.track_id ?? (it.ref && it.ref.track_id))).filter((id) => id > 0);
  if (ids.length !== items.length) return null;
  const rows = ids.map((id) => state.tracks.get(id));
  if (!rows.every(Boolean)) return null;
  const pids = new Set(rows.map((r) => Number(r.playlist_id)));
  if (pids.size !== 1) return null;
  const src = [...pids][0];
  if (!isMine(src)) return null;
  const p = state.playlists.get(src);
  if (!p || !ids.every((id) => p.order.includes(id))) return null;
  return { src, ids };
}

/** Paste `items` (clipboard items) into an editable list after its insertion cursor. */
export function pasteItems(list, items, { mode = 'copy', toastText = null } = {}) {
  const key = list.dataset.listKey;
  const pid = Number(list.dataset.playlistId);
  if (!pid || !isMine(pid)) return false;
  const good = (items || []).filter((it) => it && isVideoId(it.video_id)).slice(0, MAX_ITEMS);
  if (!good.length) return false;
  let after = anchorIn(list);
  const select = { listKey: key, playlistId: pid };

  if (mode === 'cut') {
    const cut = cutSource(good);
    if (cut) {
      if (after != null && cut.ids.includes(real(after)) && cut.src === pid) {
        // Pasting a cut right after itself: anchor on the nearest row above that isn't moving.
        const order = state.playlists.get(pid).order;
        let i = order.indexOf(real(after));
        while (i >= 0 && cut.ids.includes(order[i])) i -= 1;
        after = i >= 0 ? order[i] : null;
      }
      const n = cut.ids.length;
      const op = { kind: 'move', playlist_id: cut.src, track_ids: cut.ids, ...placement(after) };
      if (cut.src !== pid) op.to_playlist_id = pid;
      submit(op, {
        select,
        landed: cut.src === pid ? cut.ids : null,
        toastText: `Moved ${plural(n, 'track')} here`,
      });
      // Later pastes of the same clipboard are copies of the moved rows.
      update((s) => { s.clipboard = { ...s.clipboard, mode: 'copy' }; }, 'clipboard');
      focusRow(key, cut.ids[cut.ids.length - 1]);
      return true;
    }
  }

  const groupId = good.length > CHUNK ? api.newId() : null;
  for (let i = 0; i < good.length; i += CHUNK) {
    const part = good.slice(i, i + CHUNK);
    const op = { kind: 'insert', playlist_id: pid, ...placement(after), items: part.map((it) => ({
      video_id: it.video_id,
      start_seconds: it.start_seconds ?? null,
      end_seconds: it.end_seconds ?? null,
      note: it.note || '',
      display_title: it.display_title || null,
      ref: it.ref ? { ...it.ref, track_id: it.track_id ?? it.ref.track_id } : (it.track_id ? { track_id: it.track_id } : null),
    })) };
    const last = i + CHUNK >= good.length;
    submit(op, {
      rows: part.map((it) => it.row || displayRow(it)),
      select,
      groupId,
      toastText: last ? toastText || `Pasted ${good.length === 1 ? `‘${good[0].title || good[0].video_id}’` : plural(good.length, 'track')}` : null,
    });
    after = state.selection.cursorId; // the last temp row of this chunk
  }
  if (after != null) focusRow(key, after);
  return true;
}

/** V: paste the in-page clipboard into the focused editable list. */
export function pasteInternal(list) {
  if (!list || !isEditableList(list)) return false;
  const c = state.clipboard;
  if (!c.items || !c.items.length) {
    toast.show('Nothing copied yet. Select tracks and press ⌘C first.');
    return true;
  }
  return pasteItems(list, c.items, { mode: c.mode });
}

/** ⌘D: duplicate the selection right after itself. */
export function duplicateSelection(list) {
  if (!list || !isEditableList(list)) return false;
  const rows = rowsToCopy(list).filter((r) => r.id > 0 || real(r.id) > 0);
  if (!rows.length) return false;
  const built = buildPayload(rows, 'copy');
  const items = built.payload.items.map((it, i) => ({ ...it, track_id: real(rows[i].id), row: rows[i] }));
  return pasteItems(list, items, { mode: 'copy', toastText: `Duplicated ${plural(rows.length, 'track')}` });
}

function jsonFromHtml(html) {
  const m = /data-webjam='([^']*)'/.exec(html || '') || /data-webjam="([^"]*)"/.exec(html || '');
  if (!m) return null;
  const raw = m[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

const PASTEABLE = /https?:\/\/\S+|(?:^|\s)\/(?:p|t)\/\S+|^[\s,]*[A-Za-z0-9_-]{11}(?:[\s,]+[A-Za-z0-9_-]{11})*[\s,]*$/;

async function resolveAndPaste(list, text, html) {
  let res;
  try {
    res = await api.post('/api/resolve-paste', { text: String(text || '').slice(0, 100000), html: html ? String(html).slice(0, 250000) : null });
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'That paste could not be read.');
    return;
  }
  const items = (res.items || []).map((it) => ({ ...it, track_id: it.ref && it.ref.track_id }));
  const pointer = (res.pointers || []).find((p) => p.type === 'youtube_playlist');
  if (items.length) pasteItems(list, items, { mode: 'copy' });
  if (pointer) {
    toast.show('That link is a YouTube playlist.', slots.has('import.open')
      ? { action: { label: 'Import it', run: () => slots.call('import.open', { source: pointer.source_input, pointer }) } } : {});
  } else if (!items.length) {
    toast.show('Nothing in that paste could be added.');
  }
  const missing = (res.unresolved || []).length;
  if (missing && items.length) toast.show(`${plural(missing, 'link')} could not be added.`);
}

let fallbackTimer = null;
/** ⌘V: browsers that never fire `paste` on a non-editable element get the in-page clipboard. */
export function armPasteFallback(list) {
  clearTimeout(fallbackTimer);
  fallbackTimer = setTimeout(() => {
    fallbackTimer = null;
    pasteInternal(list);
  }, 150);
}

function cancelFallback() {
  clearTimeout(fallbackTimer);
  fallbackTimer = null;
}

let started = false;
export function startClipboard() {
  if (started) return;
  started = true;
  document.addEventListener('copy', onCopyEvent);
  document.addEventListener('cut', onCopyEvent);
  registerPaste({
    name: 'playlist-edit',
    priority: 10,
    handle(payload) {
      const list = payload.target && !payload.inOmnibox ? payload.target.closest('[role="listbox"][data-editable]') : null;
      if (!list || !isMine(Number(list.dataset.playlistId))) return false;
      cancelFallback();
      const json = payload.json || jsonFromHtml(payload.html);
      if (json && json.webjam === 1 && Array.isArray(json.items)) {
        const own = state.clipboard;
        if (own.copiedAt && json.copied_at === own.copiedAt) pasteItems(list, own.items, { mode: own.mode });
        else pasteItems(list, json.items.map((it) => ({ ...it, track_id: it.ref && it.ref.track_id })), { mode: 'copy' });
        return true;
      }
      if (!PASTEABLE.test(payload.text || '')) return false;
      return resolveAndPaste(list, payload.text, payload.html);
    },
  });
}
