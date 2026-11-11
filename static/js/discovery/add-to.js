// "Add to playlist…" in the ⋯ menu of read-only discovery lists (search results, YouTube results,
// the track page's "often alongside", discover tracks, profile lists): a second menu of my dock
// playlists, then one insert op (optimistic when that playlist is loaded). Playlists and the crate
// have their own menus (Lane A's "Send to…", Lane C's crate actions), so this only shows for
// lists rendered with context 'search'.

import { state, optimistic } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { openMenu } from '../ui/menu.js';
import { toast } from '../ui/toast.js';
import { chips } from '../panels/dock.js';
import { playlistUrl } from '../format.js';

export async function addToPlaylist(chip, row) {
  if (!chip || !row) return;
  const item = { video_id: row.video_id };
  if (row.start_seconds != null) item.start_seconds = row.start_seconds;
  if (row.end_seconds != null) item.end_seconds = row.end_seconds;
  const op = { kind: 'insert', playlist_id: Number(chip.playlist_id), items: [item] };
  const token = optimistic({ ...op, items: [{ ...item, row }] });
  try {
    await api.ops.send([op], { token });
    toast.show(`Added to ‘${chip.title}’`, { action: { label: 'Open', href: playlistUrl(chip.playlist_id, chip.title) } });
  } catch {
    /* rolled back and toasted by the ops queue */
  }
}

slots.register('row.menu', (mctx) => {
  const list = mctx && mctx.list;
  if (!list || list.dataset.context !== 'search' || !mctx.row) return null;
  if (!state.auth.signedIn) return null;
  const targets = chips().filter((c) => c.kind !== 'crate');
  if (!targets.length) return null;
  const anchor = mctx.rowEl ? mctx.rowEl.querySelector('[data-action="menu-open"]') : null;
  return [{
    label: 'Add to playlist…',
    icon: 'plus',
    run: () => openMenu(anchor, targets.slice(0, 10).map((c) => ({
      label: c.title,
      run: () => addToPlaylist(c, mctx.row),
    })), { label: 'Add to playlist' }),
  }];
}, { order: 20 });
