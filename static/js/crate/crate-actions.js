// The crate's verbs (docs/phase1b-shell.md §4.4, docs/phase1-ops-api.md §3.3–3.5).
//
//   Unload ▸        a popover of my dock playlists. Default is a `move` (the rows leave the crate);
//                   "Keep a copy in the crate" sends an `insert` with ref.track_id instead.
//   Make it a playlist   POST /api/me/crate/promote (all, or only the selection), title + visibility.
//   Clear unloaded  GET /api/me/crate, then one `delete` of the rows whose also_in is non-empty.
//   Import          opens the import sheet aimed at the crate.
//
// Acts on the crate selection when there is one, otherwise on the whole crate. Rendered through the
// 'crate.actions' slot (crate panel and /crate page) and 'crate.row.menu' (per row "Move to …").
//
//   insertIntoCrate(items, { rows, groupId }) -> { added, groupId } | null
//       insert items (already in the /api/ops insert shape, extra display fields allowed) into my
//       crate in batches of 50 sharing one undo group, optimistic where the crate is loaded.

import { html, nothing } from '../lit.js';
import { state, update, optimistic, ingestPlaylist } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { navigate } from '../router.js';
import { reloadDock } from '../session.js';
import { chips } from '../panels/dock.js';
import { toast } from '../ui/toast.js';
import { openPopover, closePopover, rerenderPopover, popoverAnchor } from '../ui/popover.js';
import { openSheet, closeSheet, rerenderSheet } from '../ui/sheet.js';
import { playlistUrl, plural, isVideoId } from '../format.js';
import { undoToast, refreshUndo, selectedIdsIn, clearSelection, whenSheetClosed } from '../share/util.js';

const BATCH = 50;

function crateId() {
  return state.auth.crateId;
}

function crateOrder() {
  const p = state.playlists.get(Number(crateId()));
  return p ? p.order : [];
}

/** The ids an action applies to: the crate selection, else every real row. */
function targetIds() {
  const order = crateOrder();
  const sel = selectedIdsIn(crateId(), order);
  return sel.length ? { ids: sel, selection: true } : { ids: order.filter((id) => id > 0), selection: false };
}

function playlistChips() {
  return chips().filter((c) => c && c.kind !== 'crate');
}

function chunks(list, n = BATCH) {
  const out = [];
  for (let i = 0; i < list.length; i += n) out.push(list.slice(i, i + n));
  return out;
}

function cleanItem(it) {
  const o = { video_id: it.video_id };
  if (it.start_seconds != null && it.start_seconds !== '') o.start_seconds = it.start_seconds;
  if (it.end_seconds != null && it.end_seconds !== '') o.end_seconds = it.end_seconds;
  if (it.note) o.note = String(it.note).slice(0, 5000);
  if (it.display_title) o.display_title = String(it.display_title).slice(0, 200);
  const ref = it.ref || null;
  if (ref && Number(ref.track_id) > 0) o.ref = { track_id: Number(ref.track_id) };
  else if (ref && Number(ref.playlist_id) > 0) o.ref = { playlist_id: Number(ref.playlist_id) };
  return o;
}

function displayRow(it) {
  return {
    video_id: it.video_id,
    title: it.title || it.display_title || '',
    channel_title: it.channel_title || '',
    thumbnail_url: it.thumbnail_url || '',
    start_seconds: it.start_seconds ?? null,
    end_seconds: it.end_seconds ?? null,
    source_username: (it.ref && it.ref.username) || null,
    track_note: it.note || '',
  };
}

export async function insertIntoCrate(items, { groupId } = {}) {
  const cid = crateId();
  if (!cid) return null;
  const list = (items || []).filter((it) => it && isVideoId(it.video_id)).slice(0, 1000);
  if (!list.length) return null;
  const gid = groupId || api.newId();
  let added = 0;
  for (const part of chunks(list)) {
    const op = { kind: 'insert', playlist_id: cid, items: part.map(cleanItem) };
    const token = optimistic({ ...op, items: op.items.map((it, i) => ({ ...it, row: displayRow(part[i]) })) });
    try {
      const res = await api.ops.send([op], { token, groupId: gid });
      const r = (res.results || [])[0];
      added += r && Array.isArray(r.track_ids) ? r.track_ids.length : part.length;
    } catch {
      break; // rolled back and toasted by api.ops
    }
  }
  return added ? { added, groupId: gid } : null;
}

// --- unload -------------------------------------------------------------------------------------

let unload = { keep: false };

async function unloadTo(pid, ids, { keep = false } = {}) {
  const cid = crateId();
  const chip = playlistChips().find((c) => Number(c.playlist_id) === Number(pid));
  const title = chip ? chip.title : (state.playlists.get(Number(pid)) || { meta: {} }).meta.title || 'that playlist';
  const real = ids.filter((id) => id > 0);
  if (!cid || !real.length) return;
  const gid = api.newId();
  let n = 0;
  for (const part of chunks(real)) {
    let op;
    let token;
    if (keep) {
      const rows = part.map((id) => state.tracks.get(id)).filter(Boolean);
      const items = rows.map((r) => cleanItem({ ...r, note: r.track_note, ref: { track_id: r.id } }));
      op = { kind: 'insert', playlist_id: Number(pid), items };
      token = optimistic({ ...op, items: items.map((it, i) => ({ ...it, row: rows[i] })) });
    } else {
      op = { kind: 'move', playlist_id: cid, track_ids: part, to_playlist_id: Number(pid) };
      token = optimistic(op);
    }
    try {
      await api.ops.send([op], { token, groupId: gid });
      n += part.length;
    } catch {
      break;
    }
  }
  if (!n) return;
  if (!keep) clearSelection();
  undoToast(`${keep ? 'Copied' : 'Moved'} ${plural(n, 'track')} to ‘${title}’`, { groupId: gid });
}

function unloadTpl() {
  const { ids, selection } = targetIds();
  const targets = playlistChips();
  const n = ids.length;
  return html`<div class="sh-unload">
    <p class="label sh-pop-label">${selection ? `Unload ${n} selected to` : `Unload all ${n} to`}</p>
    ${targets.length
      ? html`<div class="menu" role="menu">${targets.map((c) => html`<button type="button" class="menu-item"
            role="menuitem" data-action="crate-unload-to" data-target-id=${c.playlist_id}>
            <span class="chip-slot">${c.slot ?? ''}</span><span class="sh-grow">${c.title}</span>
            <span class="chip-count">${c.track_count}</span></button>`)}</div>`
      : html`<p class="quiet-line sh-pop-line">No playlists yet. Make the crate into one instead.</p>`}
    <label class="sh-check">
      <input type="checkbox" data-action="crate-keep-copy" .checked=${unload.keep} />
      Keep a copy in the crate
    </label>
  </div>`;
}

// --- promote ------------------------------------------------------------------------------------

let promote = null; // { error, busy, scope }

function visibilityFields(name, value) {
  const opts = [
    ['public', 'Public', 'Anyone can find it'],
    ['unlisted', 'Unlisted', 'Only people with the link'],
    ['private', 'Private', 'Only you'],
  ];
  return html`<fieldset class="sh-radios">
    <legend class="label">Who can see it</legend>
    ${opts.map(([v, label, hint]) => html`<label class="sh-radio">
      <input type="radio" name=${name} value=${v} .checked=${value === v} />
      <span>${label}</span><span class="sh-hint">${hint}</span>
    </label>`)}
  </fieldset>`;
}

export { visibilityFields };

function openPromote() {
  const { ids, selection } = targetIds();
  if (!ids.length) {
    toast.show('Your crate is empty.');
    return;
  }
  promote = { error: '', busy: false, scope: selection ? 'selected' : 'all', selected: selection ? ids : null, total: crateOrder().filter((id) => id > 0).length };
  openSheet({
    title: 'Make it a playlist',
    body: () => html`<form id="sh-promote" class="sh-form" data-action="crate-promote-submit" novalidate>
      <label class="sh-field">
        <span class="label">Title</span>
        <input class="input" name="title" maxlength="200" required autocomplete="off" placeholder="Name the playlist" />
      </label>
      ${promote.selected
        ? html`<fieldset class="sh-radios">
            <legend class="label">Tracks</legend>
            <label class="sh-radio"><input type="radio" name="scope" value="selected" .checked=${promote.scope === 'selected'} />
              <span>The ${promote.selected.length} selected</span><span class="sh-hint">the rest stay in the crate</span></label>
            <label class="sh-radio"><input type="radio" name="scope" value="all" .checked=${promote.scope === 'all'} />
              <span>All ${promote.total}</span><span class="sh-hint">the crate is emptied</span></label>
          </fieldset>`
        : html`<p class="quiet-line">All ${plural(promote.total, 'track')} move into the new playlist; the crate starts empty.</p>`}
      ${visibilityFields('visibility', 'public')}
      ${promote.error ? html`<p class="sh-err" role="alert">${promote.error}</p>` : nothing}
    </form>`,
    foot: () => html`
      <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
      <button type="submit" class="btn btn-primary" form="sh-promote" ?disabled=${promote && promote.busy}>Make playlist</button>`,
    onClose: () => { promote = null; },
  });
}

async function submitPromote(form) {
  if (!promote || promote.busy) return;
  const fd = new FormData(form);
  const title = String(fd.get('title') || '').trim();
  if (!title) {
    promote.error = 'Give it a title.';
    rerenderSheet();
    form.querySelector('input[name="title"]').focus();
    return;
  }
  const visibility = String(fd.get('visibility') || 'public');
  const scope = String(fd.get('scope') || 'all');
  const ids = scope === 'selected' && promote.selected ? promote.selected : null;
  promote.busy = true;
  promote.error = '';
  rerenderSheet();
  const cid = crateId();
  try {
    const res = await api.queued(() => api.post('/api/me/crate/promote', { title, description: '', visibility, track_ids: ids }));
    const moved = new Set(ids || crateOrder());
    update((s) => {
      const p = s.playlists.get(Number(cid));
      if (p) {
        p.order = p.order.filter((id) => !moved.has(id));
        for (const id of moved) s.tracks.delete(id);
        if (res.crate && res.crate.version != null) {
          p.version = res.crate.version;
          if (p.meta) p.meta.version = p.version;
        }
      }
      if (s.home && s.home.crate) {
        const left = (s.home.crate.recent || []).filter((t) => !moved.has(Number(t.id)));
        s.home = { ...s.home, crate: { ...s.home.crate, ...(res.crate || {}), recent: ids ? left : [] } };
      }
    }, 'promote');
    clearSelection();
    refreshUndo();
    reloadDock().catch(() => {});
    closeSheet('done');
    update((s) => { s.ui.cratePanel = false; }, 'crate-panel');
    await navigate(playlistUrl(res.playlist_id, title));
    undoToast(`Made ‘${title}’ from ${plural(res.moved, 'track')}`, { groupId: res.group_id });
  } catch (err) {
    if (promote) {
      promote.busy = false;
      promote.error = err.detail || 'Could not make the playlist.';
      rerenderSheet();
    }
  }
}

// --- clear unloaded -----------------------------------------------------------------------------

async function clearUnloaded() {
  let detail;
  try {
    detail = await api.get('/api/me/crate');
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not read the crate.');
    return;
  }
  update(() => ingestPlaylist(detail), 'load-playlist');
  const ids = (detail.tracks || []).filter((t) => Array.isArray(t.also_in) && t.also_in.length).map((t) => Number(t.id));
  if (!ids.length) {
    toast.show('Nothing to clear: no crate track is in one of your playlists yet.');
    return;
  }
  const gid = api.newId();
  let n = 0;
  for (const part of chunks(ids)) {
    const op = { kind: 'delete', playlist_id: Number(detail.id), track_ids: part };
    const token = optimistic(op);
    try {
      await api.ops.send([op], { token, groupId: gid });
      n += part.length;
    } catch {
      break;
    }
  }
  if (n) {
    clearSelection();
    undoToast(`Cleared ${plural(n, 'track')} already in your playlists`, { groupId: gid });
  }
}

// --- slots and actions --------------------------------------------------------------------------

slots.register('crate.actions', (ctx = {}) => {
  if (!state.auth.signedIn) return null;
  const { ids, selection } = targetIds();
  const n = ids.length;
  const importBtn = html`<button type="button" class="btn btn-quiet" data-action="import-open" data-target="crate">Import</button>`;
  if (!n && !(ctx.rows && ctx.rows.length)) return importBtn;
  return html`
    <button type="button" class="btn" data-action="crate-unload" aria-haspopup="dialog" aria-expanded="false">
      ${selection ? `Unload ${n}` : 'Unload'}<svg class="i"><use href="#i-chevron-down"></use></svg></button>
    <button type="button" class="btn btn-quiet" data-action="crate-promote">Make it a playlist</button>
    <button type="button" class="btn btn-quiet" data-action="crate-clear-unloaded"
      title="Remove crate tracks you have already put in a playlist">Clear unloaded</button>
    ${importBtn}`;
});

slots.register('crate.row.menu', (mctx = {}) => {
  const row = mctx.row;
  if (!row || !(Number(row.id) > 0)) return null;
  const targets = playlistChips().slice(0, 5);
  if (!targets.length) return null;
  return targets.map((c) => ({
    label: `Move to ‘${c.title}’`,
    run: () => unloadTo(c.playlist_id, [Number(row.id)]),
  }));
});

registerActions({
  'crate-unload': (el) => {
    if (popoverAnchor() === el) {
      closePopover();
      return;
    }
    if (!targetIds().ids.length) return;
    openPopover(el, unloadTpl, {
      label: 'Unload the crate',
      onClose: () => el.setAttribute('aria-expanded', 'false'),
    });
    el.setAttribute('aria-expanded', 'true');
  },
  'crate-unload-to': (el) => {
    const pid = Number(el.dataset.targetId);
    const { ids } = targetIds();
    const keep = unload.keep;
    closePopover({ restoreFocus: true });
    unloadTo(pid, ids, { keep });
  },
  'crate-keep-copy': (el, e) => {
    if (e.type !== 'change') return;
    unload = { ...unload, keep: !!el.checked };
    rerenderPopover();
  },
  'crate-promote': () => whenSheetClosed(openPromote),
  'crate-promote-submit': (form) => submitPromote(form),
  'crate-clear-unloaded': () => clearUnloaded(),
});

export { unloadTo };
