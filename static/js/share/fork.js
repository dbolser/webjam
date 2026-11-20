// "Copy all" (docs/phase1b-shell.md §4.4, docs/phase1-ops-api.md §3.9): POST /api/playlists/{id}/fork
// with { title: null, visibility: null } (the source's title and visibility), then open the copy.
// Rows are credited to the source's curator ("via @maya"); the copy is not on the undo stack.

import { state, isMine } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { navigate } from '../router.js';
import { reloadDock } from '../session.js';
import { toast } from '../ui/toast.js';
import { playlistUrl, plural } from '../format.js';
import { here } from './util.js';

let busy = false;

export async function forkPlaylist(pid) {
  const id = Number(pid);
  const entry = state.playlists.get(id);
  if (!entry || entry.kind === 'crate') return;
  if (!state.auth.signedIn) {
    slots.call('auth.required', { action: 'fork', path: here() });
    return;
  }
  if (busy) return;
  busy = true;
  const meta = entry.meta || {};
  const mine = isMine(id);
  try {
    const res = await api.post(`/api/playlists/${id}/fork`, { title: null, visibility: null });
    if (res.new_badges && res.new_badges.length) toast.badge(res.new_badges);
    reloadDock().catch(() => {});
    await navigate(playlistUrl(res.playlist_id, meta.title));
    const via = !mine && meta.username ? ` · via @${meta.username}` : '';
    toast.show(`Copied ${plural(res.track_count, 'track')}${via}`);
  } catch (err) {
    if (!err.handled) toast.show(err.detail || 'Could not copy that playlist.');
  } finally {
    busy = false;
  }
}

slots.register('playlist.menu', (ctx = {}) => {
  if (ctx.crate || !ctx.playlistId) return null;
  const mine = !!ctx.mine;
  return [{
    label: mine ? 'Duplicate playlist' : 'Copy all to a new playlist',
    icon: 'copy',
    run: () => forkPlaylist(ctx.playlistId),
  }];
}, { order: 10 });
