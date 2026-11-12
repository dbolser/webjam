// In-place editing (docs/phase1b-shell.md §4.4): the playlist's title, description and
// visibility (one `set_meta`), each row's "Note & clip" (one `update`, with "Preview seam" to
// hear the previous track's last 10 s into this one while you try start points), swapping a row
// to another YouTube upload, removing rows, and deleting a playlist (confirmed; not undoable).

import { html, nothing } from '../lit.js';
import { state, update, isMine, forgetPlaylist } from '../store.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { openPopover, closePopover, popoverAnchor } from '../ui/popover.js';
import { confirm } from '../ui/confirm.js';
import { toast } from '../ui/toast.js';
import { navigate } from '../router.js';
import { reloadDock } from '../session.js';
import { mmss, plural, isVideoId, playlistUrl } from '../format.js';
import * as player from '../player/player.js';
import { itemsFromRows } from '../player/queue.js';
import { listRows } from '../components/track-row.js';
import { submit } from './ops.js';
import { selectedRows, rowsOfList, isEditableList, focusRow } from './selection.js';

// --- playlist details ---------------------------------------------------------------------------------

let metaEditing = null; // playlist id being edited
let metaSuggest = null; // a visibility to preselect (a fresh "Untitled playlist" suggests public)

/** Leaving the page drops an unsaved details form. */
export function resetMetaEditor() {
  metaEditing = null;
  metaSuggest = null;
}

export function isEditingMeta(pid) {
  return metaEditing === Number(pid);
}

const VIS = [
  ['public', 'Public', 'Anyone can find it, and it counts toward discovery.'],
  ['unlisted', 'Unlisted', 'Only people with the link can open it.'],
  ['private', 'Private', 'Only you can see it.'],
];

/** The header form that replaces the title block while editing (rendered by views/playlist.js). */
export function metaForm(pid) {
  const p = state.playlists.get(Number(pid));
  if (!p) return nothing;
  const m = p.meta;
  const vis = metaSuggest || m.visibility || 'public';
  return html`<form class="ed-meta" data-action="edit-meta-save" data-cancel-action="edit-meta-cancel" data-playlist-id=${pid}>
    <label class="ed-field">
      <span class="label">Title</span>
      <input class="input ed-meta-title" name="title" maxlength="200" required autocomplete="off" .value=${m.title || ''} />
    </label>
    <label class="ed-field">
      <span class="label">Description</span>
      <textarea class="input ed-meta-desc" name="description" rows="3" maxlength="5000" .value=${m.description || ''}></textarea>
    </label>
    <fieldset class="ed-field ed-vis">
      <legend class="label">Who can see it</legend>
      ${VIS.map(([v, label, help]) => html`<label class="ed-vis-opt">
        <input type="radio" name="visibility" value=${v} .checked=${vis === v} />
        <span class="ed-vis-name">${label}</span>
        <span class="ed-vis-help">${help}</span>
      </label>`)}
    </fieldset>
    <div class="ed-meta-actions">
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" class="btn btn-quiet" data-action="edit-meta-cancel">Cancel</button>
    </div>
  </form>`;
}

function rerenderView() {
  update(null, 'edit-ui');
}

export function openMetaEditor(pid, { focus = 'title', suggest = null } = {}) {
  if (!isMine(pid)) return;
  const p = state.playlists.get(Number(pid));
  if (!p || p.kind === 'crate') return;
  metaEditing = Number(pid);
  metaSuggest = suggest;
  rerenderView();
  requestAnimationFrame(() => {
    const el = document.querySelector(focus === 'description' ? '.ed-meta-desc' : '.ed-meta-title');
    if (el) {
      el.focus();
      if (el.select) el.select();
    }
  });
}

function closeMetaEditor() {
  metaEditing = null;
  metaSuggest = null;
  rerenderView();
  requestAnimationFrame(() => {
    const h1 = document.querySelector('#view h1');
    if (h1) h1.focus({ preventScroll: true });
  });
}

function patchCards(pid, set) {
  // Keep the dock chip and my home cards in step with a rename, without refetching.
  if (state.dock && set.title != null) {
    const d = state.dock;
    for (const c of [d.crate, ...(d.pinned || []), ...(d.recent || [])]) if (c && Number(c.playlist_id) === pid) c.title = set.title;
    state.dock = { ...d };
  }
  if (state.home && Array.isArray(state.home.playlists)) {
    for (const card of state.home.playlists) if (Number(card.id) === pid) Object.assign(card, set);
    state.home = { ...state.home };
  }
}

function saveMeta(form) {
  const pid = Number(form.dataset.playlistId);
  const p = state.playlists.get(pid);
  if (!p) return;
  const title = form.elements.title.value.trim();
  const description = form.elements.description.value.trim();
  const checked = form.querySelector('input[name="visibility"]:checked');
  const visibility = checked ? checked.value : p.meta.visibility;
  if (!title) {
    form.elements.title.focus();
    return;
  }
  const set = {};
  if (title !== p.meta.title) set.title = title;
  if (description !== (p.meta.description || '')) set.description = description;
  if (visibility && visibility !== p.meta.visibility) set.visibility = visibility;
  metaEditing = null;
  metaSuggest = null;
  if (!Object.keys(set).length) {
    closeMetaEditor();
    return;
  }
  const old = { title: p.meta.title, description: p.meta.description, visibility: p.meta.visibility };
  const undoSet = Object.fromEntries(Object.keys(set).map((k) => [k, old[k]]));
  submit({ kind: 'set_meta', playlist_id: pid, set }, {
    apply: (s) => {
      const e = s.playlists.get(pid);
      if (e) e.meta = { ...e.meta, ...set };
      patchCards(pid, set);
    },
    revert: (s) => {
      const e = s.playlists.get(pid);
      if (e) e.meta = { ...e.meta, ...undoSet };
      patchCards(pid, undoSet);
    },
    toastText: set.visibility && Object.keys(set).length === 1 ? `Now ${set.visibility}` : 'Saved',
  }).then((res) => {
    if (res && set.title) {
      import('../router.js').then((r) => r.replaceUrl(playlistUrl(pid, set.title) + location.search)).catch(() => {});
    }
  });
  closeMetaEditor();
}

export async function deletePlaylist(pid) {
  const p = state.playlists.get(Number(pid));
  if (!p || !isMine(pid) || p.kind === 'crate') return;
  const ok = await confirm({
    title: `Delete ‘${p.meta.title}’?`,
    body: `Its ${plural(p.order.length, 'track')} and comments go with it. This can't be undone.`,
    confirmLabel: 'Delete playlist',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/playlists/${Number(pid)}`);
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not delete the playlist.');
    return;
  }
  const title = p.meta.title;
  forgetPlaylist(pid);
  update((s) => {
    if (s.home && Array.isArray(s.home.playlists)) s.home = { ...s.home, playlists: s.home.playlists.filter((c) => Number(c.id) !== Number(pid)) };
  }, 'deleted');
  reloadDock().catch(() => {});
  api.get('/api/me/undo', {}, { quiet: true }).then((u) => update((s) => { s.undo = u; }, 'undo')).catch(() => {});
  await navigate('/');
  toast.show(`Deleted ‘${title}’`);
}

// --- removing rows ---------------------------------------------------------------------------------------

/** The selection when it belongs to this list; else the focused row in it (never another list's rows). */
function rowsHere(list) {
  const key = list.dataset.listKey;
  if (state.selection.listKey === key) return selectedRows();
  const a = document.activeElement;
  const rowEl = a && a.closest ? a.closest('[data-track-id]') : null;
  if (!rowEl || !list.contains(rowEl)) return [];
  const id = Number(rowEl.dataset.trackId);
  const r = rowsOfList(key).find((x) => x.id === id);
  return r ? [r] : [];
}

/** Remove the selection (or the given rows) from an editable list. */
export function removeRows(list, rows = null) {
  if (!list || !isEditableList(list)) return false;
  const pid = Number(list.dataset.playlistId);
  if (!pid || !isMine(pid)) return false;
  const key = list.dataset.listKey;
  const picked = rows || rowsHere(list);
  const ids = picked.map((r) => r.id).filter((id) => id > 0);
  if (!ids.length) return false;
  // The cursor moves to the row after the block (or before it at the end).
  const all = rowsOfList(key);
  const last = all.findIndex((r) => r.id === ids[ids.length - 1]);
  const drop = new Set(ids);
  const after = all.slice(last + 1).find((r) => !drop.has(r.id)) || all.slice(0, last).reverse().find((r) => !drop.has(r.id));
  submit({ kind: 'delete', playlist_id: pid, track_ids: ids }, {
    toastText: ids.length === 1 ? `Removed ‘${picked[0].title || picked[0].video_id}’` : `Removed ${plural(ids.length, 'track')}`,
  });
  if (after) {
    update((s) => {
      s.selection = { ...s.selection, listKey: key, playlistId: pid, ids: new Set([after.id]), anchorId: after.id, cursorId: after.id, insertAfterId: after.id };
    }, 'select');
    focusRow(key, after.id);
  }
  return true;
}

// --- note & clip ----------------------------------------------------------------------------------------

let clip = null; // { key, pid, trackId }

/** "1:02:03", "4:31", "271", "4m31s" -> seconds; "" -> null; anything else -> NaN. */
export function parseTime(v) {
  const s = String(v ?? '').trim();
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) return Math.floor(Number(s));
  let m = /^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.\d+)?$/.exec(s);
  if (m) return (Number(m[1] || 0) * 3600) + Number(m[2]) * 60 + Number(m[3]);
  m = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/i.exec(s);
  if (m && (m[1] || m[2] || m[3])) return Number(m[1] || 0) * 3600 + Number(m[2] || 0) * 60 + Number(m[3] || 0);
  return NaN;
}

function rowById(key, tid) {
  return rowsOfList(key).find((r) => r.id === Number(tid)) || null;
}

function clipTpl() {
  const c = clip;
  const row = c && rowById(c.key, c.trackId);
  if (!row) return html`<p class="ed-quiet">That track is gone.</p>`;
  const rows = rowsOfList(c.key);
  const i = rows.findIndex((r) => r.id === row.id);
  const prev = i > 0 ? rows[i - 1] : null;
  return html`<form class="ed-clip" data-action="edit-clip-save" data-cancel-action="edit-clip-cancel">
    <p class="ed-clip-title">${row.title || row.video_id}</p>
    <label class="ed-field">
      <span class="label">Note</span>
      <textarea class="input" name="note" rows="3" maxlength="5000" placeholder="Why this one, what to listen for…"
        .value=${row.track_note || ''}></textarea>
    </label>
    <div class="ed-clip-times">
      <label class="ed-field">
        <span class="label">Start</span>
        <span class="ed-time">
          <input class="input mono" name="start" inputmode="numeric" placeholder="0:00" autocomplete="off"
            .value=${row.start_seconds != null ? mmss(row.start_seconds) : ''} />
          <button type="button" class="btn btn-quiet ed-now" data-action="edit-clip-now" data-field="start"
            title="Set from the player">Now</button>
        </span>
      </label>
      <label class="ed-field">
        <span class="label">End</span>
        <span class="ed-time">
          <input class="input mono" name="end" inputmode="numeric" placeholder="end" autocomplete="off"
            .value=${row.end_seconds != null ? mmss(row.end_seconds) : ''} />
          <button type="button" class="btn btn-quiet ed-now" data-action="edit-clip-now" data-field="end"
            title="Set from the player">Now</button>
        </span>
      </label>
    </div>
    <p class="ed-clip-error" role="alert" hidden></p>
    <div class="ed-clip-listen">
      ${prev
        ? html`<button type="button" class="btn ed-seam-btn" data-action="edit-clip-seam"
            title="Play the last 10 s of ‘${prev.title || prev.video_id}’, then this from its start">
            <svg class="i"><use href="#i-seam"></use></svg>Preview seam</button>`
        : nothing}
      <button type="button" class="btn btn-quiet" data-action="edit-clip-play">
        <svg class="i"><use href="#i-play"></use></svg>From start</button>
    </div>
    <div class="ed-clip-actions">
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" class="btn btn-quiet" data-action="edit-clip-cancel">Cancel</button>
    </div>
  </form>`;
}

export function openClipEditor(list, rowEl) {
  if (!list || !rowEl || !isEditableList(list)) return false;
  const pid = Number(list.dataset.playlistId);
  if (!isMine(pid)) return false;
  const tid = Number(rowEl.dataset.trackId);
  if (!(tid > 0)) return false;
  clip = { key: list.dataset.listKey, pid, trackId: tid };
  openPopover(rowEl, clipTpl, { label: 'Note and clip', onClose: () => { clip = null; } });
  requestAnimationFrame(() => {
    const ta = document.querySelector('.ed-clip textarea');
    if (ta) ta.focus();
  });
  return true;
}

function readClip(form) {
  const err = form.querySelector('.ed-clip-error');
  const start = parseTime(form.elements.start.value);
  const end = parseTime(form.elements.end.value);
  let msg = '';
  if (Number.isNaN(start) || Number.isNaN(end)) msg = 'Times look like 1:23 or 1:02:03.';
  else if (start != null && end != null && end <= start) msg = 'The end has to come after the start.';
  else if ((start ?? 0) > 86400 || (end ?? 0) > 86400) msg = 'That is longer than a day.';
  if (err) {
    err.textContent = msg;
    err.hidden = !msg;
  }
  return msg ? null : { start, end, note: form.elements.note.value };
}

/** Queue for previewing with the (unsaved) clip values from the form. */
function previewQueue(form) {
  const c = clip;
  if (!c) return null;
  const v = readClip(form);
  if (!v) return null;
  const { rows, source } = listRows(c.key);
  const live = rows.filter((r) => r && r.video_id && !(Number(r.id) < 0));
  const items = itemsFromRows(live, source);
  const i = live.findIndex((r) => r.id === c.trackId);
  if (i < 0) return null;
  items[i] = { ...items[i], start: v.start, end: v.end };
  return { items, index: i, source };
}

function saveClip(form) {
  const c = clip;
  if (!c) return;
  const row = rowById(c.key, c.trackId);
  const v = readClip(form);
  if (!row || !v) return;
  const set = {};
  const note = v.note.replace(/\s+$/, '');
  if (note !== (row.track_note || '')) set.note = note;
  if ((v.start ?? null) !== (row.start_seconds ?? null)) set.start_seconds = v.start;
  if ((v.end ?? null) !== (row.end_seconds ?? null)) set.end_seconds = v.end;
  closePopover({ restoreFocus: true });
  if (!Object.keys(set).length) return;
  const old = { track_note: row.track_note, start_seconds: row.start_seconds, end_seconds: row.end_seconds };
  const local = {};
  if ('note' in set) local.track_note = set.note;
  if ('start_seconds' in set) local.start_seconds = set.start_seconds;
  if ('end_seconds' in set) local.end_seconds = set.end_seconds;
  const tid = row.id;
  submit({ kind: 'update', playlist_id: c.pid, track_id: tid, set }, {
    apply: (s) => {
      const r = s.tracks.get(tid);
      if (r) s.tracks.set(tid, { ...r, ...local });
      patchQueue(tid, local);
    },
    revert: (s) => {
      const r = s.tracks.get(tid);
      if (r) s.tracks.set(tid, { ...r, ...old });
      patchQueue(tid, old);
    },
    toastText: ('start_seconds' in set || 'end_seconds' in set) ? 'Clip saved' : 'Note saved',
  });
}

/** The player queue holds copies of rows; keep a playing row's clip in step with an edit. */
function patchQueue(tid, local) {
  const q = state.player.queue || [];
  for (const it of q) {
    if (it.trackId === tid) {
      if ('start_seconds' in local) it.start = local.start_seconds;
      if ('end_seconds' in local) it.end = local.end_seconds;
    }
  }
}

// --- swap to another upload -------------------------------------------------------------------------------

let swap = null; // { key, pid, trackId }

export function videoIdFrom(text) {
  const s = String(text || '').trim();
  if (isVideoId(s)) return s;
  const m = /(?:[?&]v=|youtu\.be\/|\/shorts\/|\/embed\/|\/live\/|\/v\/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])/.exec(s);
  return m ? m[1] : null;
}

export function openSwap(list, rowEl) {
  if (!list || !rowEl || !isEditableList(list)) return false;
  const pid = Number(list.dataset.playlistId);
  const tid = Number(rowEl.dataset.trackId);
  if (!isMine(pid) || !(tid > 0)) return false;
  swap = { key: list.dataset.listKey, pid, trackId: tid };
  openPopover(rowEl, () => html`<form class="ed-swap" data-action="edit-swap-save" data-cancel-action="edit-swap-cancel">
    <label class="ed-field">
      <span class="label">Swap to another upload</span>
      <input class="input" name="url" placeholder="Paste a YouTube link" autocomplete="off" spellcheck="false" required />
    </label>
    <p class="ed-quiet">The note and clip stay. Undo puts the old video back.</p>
    <div class="ed-clip-actions">
      <button type="submit" class="btn btn-primary">Swap</button>
      <button type="button" class="btn btn-quiet" data-action="edit-swap-cancel">Cancel</button>
    </div>
  </form>`, { label: 'Swap video', onClose: () => { swap = null; } });
  return true;
}

// --- actions -----------------------------------------------------------------------------------------------

registerActions({
  'edit-meta-open': (el, e, ctx) => openMetaEditor(Number(el.dataset.playlistId || ctx.playlistId)),
  'edit-meta-save': (form) => saveMeta(form),
  'edit-meta-cancel': () => closeMetaEditor(),
  'delete-playlist': (el, e, ctx) => deletePlaylist(Number(el.dataset.playlistId || ctx.playlistId)),

  'edit-clip-save': (form) => saveClip(form),
  'edit-clip-cancel': () => closePopover({ restoreFocus: true }),
  'edit-clip-now': (el) => {
    const form = el.closest('form');
    const cur = player.current();
    if (!form || !clip) return;
    if (!cur || cur.trackId !== clip.trackId) {
      toast.show('Play this track first, then press Now at the moment you want.');
      return;
    }
    form.elements[el.dataset.field].value = mmss(state.player.time);
  },
  'edit-clip-seam': (el) => {
    const q = previewQueue(el.closest('form'));
    if (!q) return;
    if (q.index < 1) {
      toast.show('Nothing before this track.');
      return;
    }
    player.previewSeam(q.index, { items: q.items, source: q.source });
  },
  'edit-clip-play': (el) => {
    const q = previewQueue(el.closest('form'));
    if (q) player.playQueue(q.items, q.index, { source: q.source });
  },

  'edit-swap-save': (form) => {
    const s = swap;
    const vid = videoIdFrom(form.elements.url.value);
    if (!s) return;
    if (!vid) {
      form.elements.url.setCustomValidity('That is not a YouTube video link.');
      form.elements.url.reportValidity();
      form.elements.url.setCustomValidity('');
      return;
    }
    closePopover({ restoreFocus: true });
    const row = rowById(s.key, s.trackId);
    if (!row || row.video_id === vid) return;
    submit({ kind: 'update', playlist_id: s.pid, track_id: row.id, set: { video_id: vid } }, { toastText: 'Swapped the video' });
  },
  'edit-swap-cancel': () => closePopover({ restoreFocus: true }),

  'edit-remove': (el, e, ctx) => {
    const list = ctx.list || (ctx.row && ctx.row.closest('[data-list-key]'));
    const rows = rowsOfList(list && list.dataset.listKey);
    const row = rows.find((r) => r.id === ctx.trackId);
    if (row) removeRows(list, [row]);
  },
});

/** Re-render the open clip editor (e.g. after the row's values changed underneath it). */
export function clipOpenFor(tid) {
  return !!(clip && clip.trackId === Number(tid) && popoverAnchor());
}
