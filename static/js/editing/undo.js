// Undo / redo through the server's op log (docs/phase1-ops-api.md §3.2). The stack is per user,
// spans all playlists and survives a reload. ⌘Z / ⌘⇧Z / Ctrl-Y; held keys never repeat (keys.js
// drops e.repeat), one request is in flight at a time, and every request names the group it
// expects (group_id), so a double press gets a harmless 409 stack_moved instead of undoing twice.

import { html } from '../lit.js';
import { state, update, loadPlaylist, forgetPlaylist } from '../store.js';
import { iconHref } from '../format.js';
import * as api from '../api.js';
import { toast } from '../ui/toast.js';
import { confirm } from '../ui/confirm.js';
import { reloadDock } from '../session.js';
import { navigate } from '../router.js';
import { setUndoHook, settled } from './ops.js';

let inFlight = false;

function refreshMeta(res) {
  // set_meta isn't carried in rows/order; refetch only the playlists whose details changed.
  const ids = new Set();
  for (const r of res.results || []) if (r && r.kind === 'set_meta' && r.playlist_id) ids.add(Number(r.playlist_id));
  for (const id of ids) if (state.playlists.has(id)) loadPlaylist(id, { force: true }).catch(() => {});
  if (ids.size) reloadDock().catch(() => {});
}

async function refreshUndoState() {
  try {
    const u = await api.get('/api/me/undo', {}, { quiet: true });
    update((s) => { s.undo = u; }, 'undo');
  } catch {
    /* keep what we have */
  }
}

async function offerDeleteEmpty(notes) {
  const n = (notes || []).find((x) => x && x.code === 'playlist_left_empty');
  if (!n) return;
  const pid = Number(n.playlist_id);
  toast.show('That playlist is empty now.', {
    action: {
      label: 'Delete it',
      run: async () => {
        const ok = await confirm({ title: 'Delete the empty playlist?', body: "This can't be undone.", confirmLabel: 'Delete', danger: true });
        if (!ok) return;
        try {
          await api.del(`/api/playlists/${pid}`);
          forgetPlaylist(pid);
          reloadDock().catch(() => {});
          if (state.route.name === 'playlist' && String(state.route.params.ref || '').startsWith(String(pid))) navigate('/');
          toast.show('Deleted the empty playlist.');
        } catch (err) {
          if (!err.handled) toast.show(err.detail);
        }
      },
    },
  });
}

export async function doUndo({ skip = false, groupId } = {}) {
  if (!state.auth.signedIn) {
    toast.show('Sign in to undo.');
    return;
  }
  if (inFlight) return;
  inFlight = true;
  try {
    await settled().catch(() => {});
    const res = await api.undo({ skip, groupId });
    if (!res || !res.group_id) {
      toast.show(res && res.reason === 'nothing_to_undo' ? 'Nothing to undo.' : 'Nothing to undo.');
      return;
    }
    refreshMeta(res);
    toast.show(`Undid: ${res.label || 'last change'}`, state.undo && state.undo.can_redo
      ? { action: { label: 'Redo', run: () => doRedo() } } : {});
    offerDeleteEmpty(res.notes);
  } catch (err) {
    await handleError(err, 'undo');
  } finally {
    inFlight = false;
  }
}

export async function doRedo() {
  if (!state.auth.signedIn) return;
  if (inFlight) return;
  inFlight = true;
  try {
    await settled().catch(() => {});
    const res = await api.redo();
    if (!res || !res.group_id) {
      toast.show('Nothing to redo.');
      return;
    }
    refreshMeta(res);
    toast.show(`Redid: ${res.label || 'last change'}`, { action: { label: 'Undo', run: () => doUndo() } });
  } catch (err) {
    await handleError(err, 'redo');
  } finally {
    inFlight = false;
  }
}

async function handleError(err, which) {
  const data = (err && err.data) || {};
  if (err && err.code === 'undo_conflict' && typeof data.group_id === 'string') {
    const gid = data.group_id;
    toast.show("Can't undo that: the playlist was changed afterwards.", {
      timeout: 8000,
      action: {
        label: 'Skip it',
        run: async () => {
          if (inFlight) return;
          inFlight = true;
          try {
            const skipped = await api.undo({ skip: true, groupId: gid });
            const next = skipped && skipped.undo && skipped.undo.can_undo ? skipped.undo.undo_group_id : null;
            if (!next) {
              toast.show(`Skipped: ${skipped.label || 'that change'}. Nothing earlier to undo.`);
              return;
            }
          } catch (e2) {
            if (!e2.handled) toast.show(e2.detail || 'Skip failed.');
            return;
          } finally {
            inFlight = false;
          }
          doUndo();
        },
      },
    });
    return;
  }
  if (err && err.code === 'stack_moved') {
    await refreshUndoState();
    toast.show(`Nothing was ${which === 'undo' ? 'undone' : 'redone'}: your history changed. Press again to ${which} the latest step.`);
    return;
  }
  if (err && !err.handled) toast.show(err.detail || `Could not ${which}.`);
}

/** Quiet undo / redo buttons (views/playlist.js renders them for a signed-in viewer). */
export function undoButtons() {
  const u = state.undo;
  if (!state.auth.signedIn || !u) return '';
  return html`<span class="ed-undo">
    <button type="button" class="icon-btn" data-action="undo" ?disabled=${!u.can_undo}
      aria-label=${u.can_undo ? `Undo: ${u.undo_label || 'last change'}` : 'Nothing to undo'}
      title=${u.can_undo ? `Undo: ${u.undo_label || 'last change'}` : 'Nothing to undo'}>
      <svg class="i"><use href=${iconHref('undo')}></use></svg></button>
    <button type="button" class="icon-btn" data-action="redo" ?disabled=${!u.can_redo}
      aria-label=${u.can_redo ? `Redo: ${u.redo_label || 'change'}` : 'Nothing to redo'}
      title=${u.can_redo ? `Redo: ${u.redo_label || 'change'}` : 'Nothing to redo'}>
      <svg class="i"><use href=${iconHref('redo')}></use></svg></button>
  </span>`;
}


setUndoHook(() => doUndo());
