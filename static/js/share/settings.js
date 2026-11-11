// Playlist settings for the owner: title, description, visibility (a `set_meta` op through the ops
// queue, so it is undoable) and delete (DELETE /api/playlists/{id}, confirmed, not undoable).
// Reached from the playlist ⋯ menu ('playlist.menu' slot). Lane A's inline editing writes the same
// op; this sheet is the plain-form path (and the only one on phones).

import { html, nothing } from '../lit.js';
import { state, update, forgetPlaylist, loadPlaylist, isMine } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { navigate, replaceUrl } from '../router.js';
import { reloadDock } from '../session.js';
import { toast } from '../ui/toast.js';
import { openSheet, closeSheet, rerenderSheet } from '../ui/sheet.js';
import { confirm } from '../ui/confirm.js';
import { playlistUrl } from '../format.js';
import { visibilityFields } from '../crate/crate-actions.js';
import { undoLatest, whenSheetClosed } from './util.js';

let st = null; // { pid, error, busy }

function entry(pid) {
  return state.playlists.get(Number(pid)) || null;
}

function bodyTpl() {
  const p = st && entry(st.pid);
  if (!p) return nothing;
  const m = p.meta;
  return html`<form id="sh-settings" class="sh-form" data-action="settings-save" novalidate>
    <label class="sh-field">
      <span class="label">Title</span>
      <input class="input" name="title" maxlength="200" required autocomplete="off" .value=${m.title || ''} />
    </label>
    <label class="sh-field">
      <span class="label">Description</span>
      <textarea class="input sh-textarea" name="description" rows="3" maxlength="5000" .value=${m.description || ''}></textarea>
    </label>
    ${visibilityFields('visibility', m.visibility || 'public')}
    ${st.error ? html`<p class="sh-err" role="alert">${st.error}</p>` : nothing}
    <p class="sh-danger-row">
      <button type="button" class="link-btn sh-danger" data-action="settings-delete">Delete this playlist…</button>
    </p>
  </form>`;
}

function footTpl() {
  return html`
    <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
    <button type="submit" class="btn btn-primary" form="sh-settings" ?disabled=${!!(st && st.busy)}>Save</button>`;
}

export function openSettings(pid) {
  const p = entry(pid);
  if (!p || p.kind === 'crate' || !isMine(pid)) return;
  whenSheetClosed(() => {
    st = { pid: Number(pid), error: '', busy: false };
    openSheet({ title: 'Playlist settings', body: bodyTpl, foot: footTpl, onClose: () => { st = null; } });
  });
}

/** Mirror a meta change into the store (playlist entry, dock chips, home cards). */
function applyMeta(pid, set) {
  update((s) => {
    const p = s.playlists.get(pid);
    if (p) p.meta = { ...p.meta, ...set };
    if (s.home && Array.isArray(s.home.playlists)) {
      s.home = { ...s.home, playlists: s.home.playlists.map((c) => (Number(c.id) === pid ? { ...c, ...set } : c)) };
    }
  }, 'meta');
}

async function save(form) {
  if (!st || st.busy) return;
  const pid = st.pid;
  const p = entry(pid);
  if (!p) return;
  const fd = new FormData(form);
  const title = String(fd.get('title') || '').trim();
  const description = String(fd.get('description') || '').trim();
  const visibility = String(fd.get('visibility') || p.meta.visibility || 'public');
  if (!title) {
    st.error = 'A playlist needs a title.';
    rerenderSheet();
    return;
  }
  const set = {};
  if (title !== p.meta.title) set.title = title;
  if (description !== (p.meta.description || '')) set.description = description;
  if (visibility !== p.meta.visibility) set.visibility = visibility;
  if (!Object.keys(set).length) {
    closeSheet('done');
    return;
  }
  st.busy = true;
  st.error = '';
  rerenderSheet();
  try {
    await api.ops.send([{ kind: 'set_meta', playlist_id: pid, set }], { quiet: true });
    applyMeta(pid, set);
    reloadDock().catch(() => {});
    closeSheet('done');
    if (set.title && state.route.name === 'playlist') replaceUrl(playlistUrl(pid, set.title) + location.search + location.hash);
    toast.show(set.visibility && Object.keys(set).length === 1 ? `Now ${set.visibility}` : 'Saved', {
      action: {
        label: 'Undo',
        run: async () => {
          const res = await undoLatest();
          if (res) {
            await loadPlaylist(pid, { force: true }).catch(() => {});
            reloadDock().catch(() => {});
          }
        },
      },
    });
  } catch (err) {
    if (st) {
      st.busy = false;
      st.error = err.detail || 'Could not save.';
      rerenderSheet();
    }
  }
}

export async function deletePlaylist(pid) {
  const p = entry(pid);
  if (!p || p.kind === 'crate') return;
  const title = p.meta.title;
  const yes = await confirm({
    title: `Delete ‘${title}’?`,
    body: "This can't be undone. Its tracks, likes and comments go with it.",
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!yes) return;
  try {
    await api.del(`/api/playlists/${Number(pid)}`);
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not delete it.');
    return;
  }
  const here = state.route.name === 'playlist' && document.querySelector(`#view [data-playlist-id="${Number(pid)}"]`);
  forgetPlaylist(pid);
  update((s) => {
    if (s.home && Array.isArray(s.home.playlists)) {
      s.home = { ...s.home, playlists: s.home.playlists.filter((c) => Number(c.id) !== Number(pid)) };
    }
  }, 'home-delete');
  reloadDock().catch(() => {});
  if (here) await navigate('/');
  toast.show(`Deleted ‘${title}’`);
}

registerActions({
  'settings-save': (form) => save(form),
  'settings-delete': () => {
    if (!st) return;
    const pid = st.pid;
    whenSheetClosed(() => deletePlaylist(pid));
  },
});

slots.register('playlist.menu', (ctx = {}) => {
  if (ctx.crate || !ctx.mine || !ctx.playlistId) return null;
  return [
    'sep',
    { label: 'Settings…', icon: 'more', run: () => openSettings(ctx.playlistId) },
    { label: 'Delete…', icon: 'close', danger: true, run: () => deletePlaylist(ctx.playlistId) },
  ];
}, { order: 90 });
