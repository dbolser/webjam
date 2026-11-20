// Sending tracks somewhere else: the digit keys (dock slots), drops on dock chips, the
// "+ New playlist" chip, and the touch "Send 2 to…" strip. One op per send, one undo group.
//
// Mode rules (docs/phase1b-shell.md §5.3):
//   my playlist -> same playlist         move   (Alt: copy)
//   my crate    -> any of my playlists   move   (Alt: copy, keeps a copy in the crate)
//   my playlist -> another mine / crate  copy   (Alt: move)
//   anything else -> mine                copy   (credited through the ref)

import { state, update, isMine, loadPlaylist } from '../store.js';
import * as api from '../api.js';
import { toast } from '../ui/toast.js';
import { navigate } from '../router.js';
import { reloadDock } from '../session.js';
import { chipForSlot } from '../panels/dock.js';
import { playlistUrl, plural } from '../format.js';
import { submit, settled, real } from './ops.js';
import { clearSelection } from './selection.js';

export function isCrate(pid) {
  return Number(pid) > 0 && Number(pid) === Number(state.auth.crateId);
}

export function titleOf(pid) {
  if (isCrate(pid)) return 'your crate';
  const p = state.playlists.get(Number(pid));
  if (p && p.meta && p.meta.title) return `‘${p.meta.title}’`;
  const d = state.dock;
  const chip = d && [...(d.pinned || []), ...(d.recent || [])].find((c) => Number(c.playlist_id) === Number(pid));
  return chip ? `‘${chip.title}’` : 'that playlist';
}

/** 'move' | 'copy' for rows from srcPid dropped on dstPid, with Alt toggling where allowed. */
export function modeFor(srcPid, dstPid, alt = false) {
  const src = Number(srcPid) || null;
  if (!src || !isMine(src)) return 'copy';
  let d;
  if (src === Number(dstPid)) d = 'move';
  else if (isCrate(src)) d = 'move';
  else d = 'copy';
  if (!alt) return d;
  return d === 'move' ? 'copy' : 'move';
}

function itemFrom(r) {
  const it = { video_id: r.video_id };
  if (r.start_seconds != null) it.start_seconds = r.start_seconds;
  if (r.end_seconds != null) it.end_seconds = r.end_seconds;
  if (r.track_note) it.note = r.track_note;
  if (r.display_title) it.display_title = r.display_title;
  const tid = real(r.id);
  if (tid > 0) it.ref = { track_id: tid };
  else if (Number(r.playlist_id) > 0 && !isCrate(r.playlist_id)) it.ref = { playlist_id: Number(r.playlist_id) };
  return it;
}

function placementOf(after) {
  if (after === undefined) return {}; // append
  return after == null ? { position: 1 } : { after_track_id: after };
}

/** Flash "+n" on the dock chip for pid. */
export function flashChip(pid, n) {
  const sel = isCrate(pid) ? `[data-drop-target="crate:${Number(pid)}"]` : `[data-drop-target="playlist:${Number(pid)}"]`;
  for (const chip of document.querySelectorAll(sel)) {
    chip.dataset.edPlus = `+${n}`;
    chip.classList.remove('ed-plus');
    void chip.offsetWidth; // restart the fade
    chip.classList.add('ed-plus');
    setTimeout(() => {
      chip.classList.remove('ed-plus');
      delete chip.dataset.edPlus;
    }, 1600);
  }
}

/**
 * Copy or move `rows` (from srcPid, which may be null for rows outside any playlist I can edit)
 * into dstPid. `after`: undefined appends, null puts them first, a track id places after it.
 */
export function transfer(rows, srcPid, dstPid, mode, { after, select = null, quietToast = false } = {}) {
  const list = (rows || []).filter((r) => r && r.video_id);
  const dst = Number(dstPid);
  if (!list.length || !dst) return Promise.resolve(null);
  const n = list.length;
  const where = titleOf(dst);
  if (mode === 'move' && srcPid && isMine(srcPid)) {
    const ids = list.map((r) => real(r.id)).filter((id) => id > 0);
    if (!ids.length) return Promise.resolve(null);
    const op = { kind: 'move', playlist_id: Number(srcPid), track_ids: ids, ...placementOf(after) };
    if (Number(srcPid) !== dst) op.to_playlist_id = dst;
    const within = Number(srcPid) === dst;
    if (!within) clearSelection();
    return submit(op, {
      select: within ? select : null,
      landed: within ? ids : null,
      toastText: quietToast ? null : within ? `Moved ${plural(n, 'track')}` : `Moved ${plural(n, 'track')} to ${where}`,
    });
  }
  const op = { kind: 'insert', playlist_id: dst, ...placementOf(after), items: list.map(itemFrom) };
  return submit(op, {
    rows: list,
    select,
    toastText: quietToast ? null : `${n === 1 ? `Copied ‘${list[0].title || list[0].video_id}’` : `Copied ${plural(n, 'track')}`} to ${where}`,
  });
}

/** Digits 0–9: send to dock slot n (0 = crate). Copies; from the crate it moves. */
export function sendToSlot(n, rows, srcPid) {
  const chip = chipForSlot(n);
  if (!chip) {
    toast.show(n === 0 ? 'Sign in to use your crate.' : `Nothing in dock slot ${n} yet.`);
    return;
  }
  sendToChip(chip.kind === 'crate' ? `crate:${chip.playlist_id}` : `playlist:${chip.playlist_id}`, rows, srcPid);
}

/** A dock chip's data-drop-target value ("crate:5", "playlist:9", "new"). */
export function sendToChip(value, rows, srcPid, { alt = false, after } = {}) {
  if (!state.auth.signedIn) return;
  if (value === 'new') return toNewPlaylist(rows, srcPid, { alt });
  const m = /^(crate|playlist):(\d+)$/.exec(String(value || ''));
  if (!m) return;
  const dst = Number(m[2]);
  if (Number(srcPid) === dst && after === undefined) {
    toast.show('Those tracks are already there.');
    return;
  }
  const mode = modeFor(srcPid, dst, alt);
  flashChip(dst, rows.length);
  return transfer(rows, srcPid, dst, mode, { after });
}

/** The "+ New playlist" chip: create "Untitled playlist", put the tracks in, open it with the title ready to type. */
export async function toNewPlaylist(rows, srcPid, { alt = false } = {}) {
  if (!rows || !rows.length) return;
  let res;
  try {
    res = await api.post('/api/playlists', { title: 'Untitled playlist', visibility: 'unlisted' });
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not create a playlist.');
    return;
  }
  if (res.new_badges && res.new_badges.length) toast.badge(res.new_badges);
  const pid = Number(res.playlist_id);
  try {
    await loadPlaylist(pid, { force: true });
  } catch {
    /* the insert still works without a local copy */
  }
  const mode = srcPid && isCrate(srcPid) ? (alt ? 'copy' : 'move') : (alt && srcPid && isMine(srcPid) ? 'move' : 'copy');
  transfer(rows, srcPid, pid, mode, { quietToast: true });
  await settled().catch(() => {});
  reloadDock().catch(() => {});
  await navigate(playlistUrl(pid, 'Untitled playlist'));
  const { openMetaEditor } = await import('./inline-edit.js');
  openMetaEditor(pid, { suggest: 'public' });
  update(null, 'edit-ui');
}
