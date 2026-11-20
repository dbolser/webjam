// Lane C helpers shared by the share, import, crate and account modules.
//
//   here()                    the current path + query (for sign-in return and intents)
//   copyText(text)            clipboard write with a quiet fallback; resolves to true/false
//   undoToast(text, opts)     a toast whose action undoes the group just written
//   refreshUndo()             GET /api/me/undo into state.undo (after server-composed writes)

import { state, update, forgetPlaylist } from '../store.js';
import { reloadDock } from '../session.js';
import * as api from '../api.js';
import { toast } from '../ui/toast.js';

export function here() {
  return location.pathname + location.search;
}

export async function copyText(text) {
  const value = String(text || '');
  if (!value) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* fall through to the textarea fallback */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.className = 'sh-offscreen';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return !!ok;
  } catch {
    return false;
  }
}

/** Undo the latest group (optionally asserting its id) and say what happened. */
export async function undoLatest(groupId) {
  try {
    const res = await api.undo(groupId ? { groupId } : {});
    if (res && res.reason === 'nothing_to_undo') {
      toast.show('Nothing to undo.');
      return res;
    }
    const notes = (res && res.notes) || [];
    const empty = notes.find((n) => n && n.code === 'playlist_left_empty');
    if (empty && empty.playlist_id) {
      const pid = Number(empty.playlist_id);
      toast.show('Undone. That playlist is empty now.', {
        action: { label: 'Delete it', run: () => deleteEmpty(pid) },
        timeout: 8000,
      });
    } else {
      toast.show(res && res.label ? `Undone: ${res.label}` : 'Undone');
    }
    return res;
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not undo that.');
    return null;
  }
}

async function deleteEmpty(pid) {
  try {
    await api.del(`/api/playlists/${pid}`);
    forgetPlaylist(pid);
    reloadDock().catch(() => {});
    if (state.home) {
      update((s) => {
        s.home = { ...s.home, playlists: (s.home.playlists || []).filter((p) => Number(p.id) !== pid) };
      }, 'home-delete');
    }
    toast.show('Deleted the empty playlist.');
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not delete it.');
  }
}

export function undoToast(text, { groupId, timeout = 6000 } = {}) {
  return toast.show(text, {
    timeout,
    action: { label: 'Undo', run: () => undoLatest(groupId) },
  });
}

export async function refreshUndo() {
  if (!state.auth.signedIn) return;
  try {
    const u = await api.get('/api/me/undo', {}, { quiet: true });
    update((s) => { s.undo = u; }, 'undo');
  } catch {
    /* the next op response brings it */
  }
}

/** Rows currently selected in playlist `pid` (positive ids only), in list order. */
export function selectedIdsIn(pid, order) {
  const sel = state.selection;
  if (!sel || !sel.ids || !sel.ids.size || Number(sel.playlistId) !== Number(pid)) return [];
  return (order || []).filter((id) => id > 0 && sel.ids.has(id));
}

export function clearSelection() {
  update((s) => {
    s.selection = { playlistId: null, ids: new Set(), anchorId: null, cursorId: null, insertAfterId: undefined };
  }, 'select');
}

/** Run fn once no sheet is open. ui/sheet.js handles the dialog's async 'close' event after a
 *  replacement sheet has opened (and would close it), so lane C never opens a sheet over another. */
export function whenSheetClosed(fn) {
  const d = document.getElementById('sheet');
  if (!d || !d.open) {
    fn();
    return;
  }
  d.addEventListener('close', () => fn(), { once: true });
  d.close('replaced');
}
