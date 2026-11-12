// F's core actions and keys. Imported by main.js BEFORE the lane entry modules, so lanes can extend
// a key (later registrations run first) and add actions with their own prefixes.

import { state, update, rowsOf, loadPlaylist, optimistic, expectedFor } from './store.js';
import * as api from './api.js';
import * as slots from './slots.js';
import { registerActions } from './actions.js';
import { registerKeys } from './keys.js';
import { navigate } from './router.js';
import { toast } from './ui/toast.js';
import { openMenu } from './ui/menu.js';
import { listRows } from './components/track-row.js';
import { itemsFromRows, itemFromRow } from './player/queue.js';
import * as player from './player/player.js';
import { reloadDock } from './session.js';
import { openSlot } from './panels/dock.js';
import { playlistUrl, trackUrl, absoluteUrl, signInUrl } from './format.js';
import { cycleTheme } from './theme.js';

// --- helpers ------------------------------------------------------------------------------------

/** The row object behind a DOM row (from the list registry), or null. */
export function rowFor(ctx) {
  if (!ctx || !ctx.row) return null;
  const key = ctx.list && ctx.list.dataset.listKey;
  const { rows } = listRows(key);
  const idx = Number(ctx.row.dataset.index);
  const byIdx = Number.isInteger(idx) ? rows[idx] : null;
  if (byIdx && (ctx.trackId == null || Number(byIdx.id) === ctx.trackId)) return byIdx;
  return rows.find((r) => Number(r.id) === ctx.trackId) || byIdx || null;
}

/** Queue + index + source for "play this row" (a card preview plays its whole playlist). */
async function queueFor(ctx) {
  const key = ctx.list && ctx.list.dataset.listKey;
  const { rows, source } = listRows(key);
  let idx = ctx.row ? Number(ctx.row.dataset.index) : 0;
  if (source && source.preview && source.playlistId) {
    try {
      await loadPlaylist(source.playlistId);
      const full = rowsOf(source.playlistId);
      const at = full.findIndex((r) => Number(r.id) === ctx.trackId);
      const meta = state.playlists.get(Number(source.playlistId));
      const src = { playlistId: source.playlistId, title: meta ? meta.meta.title : source.title, kind: meta ? meta.kind : source.kind };
      if (full.length) return { items: itemsFromRows(full, src), index: at >= 0 ? at : 0, source: src };
    } catch {
      /* fall back to the preview rows */
    }
  }
  const items = itemsFromRows(rows, source);
  const live = rows.filter((r) => r && r.video_id && !(Number(r.id) < 0));
  const row = rows[idx];
  idx = Math.max(0, live.indexOf(row));
  return { items, index: idx, source };
}

async function playRow(ctx) {
  const q = await queueFor(ctx);
  if (!q.items.length) return;
  await player.playQueue(q.items, q.index, { source: q.source });
}

async function playPlaylist(pid, from = 0) {
  if (!pid) return;
  try {
    await loadPlaylist(pid);
  } catch (err) {
    toast.show(err.detail || 'That playlist could not load.');
    return;
  }
  const p = state.playlists.get(Number(pid));
  const src = { playlistId: Number(pid), title: p.kind === 'crate' ? 'Crate' : p.meta.title, kind: p.kind };
  await player.playQueue(itemsFromRows(rowsOf(pid), src), from, { source: src });
}

function requireSignIn(intent) {
  const handled = slots.call('auth.required', intent);
  if (handled === undefined) {
    toast.show(state.auth.mode === 'demo' ? 'Pick a persona to do that.' : 'Sign in to collect tracks.',
      state.auth.mode === 'demo' ? {} : { action: { label: 'Sign in', href: signInUrl(location.pathname + location.search) } });
  }
}

/** Add rows to the crate: one insert op, optimistic when the crate is loaded. */
export async function addToCrate(rows) {
  const list = (rows || []).filter((r) => r && r.video_id);
  if (!list.length) return null;
  if (!state.auth.signedIn) {
    requireSignIn({ action: 'crate-add', payload: { items: list.map((r) => ({ video_id: r.video_id, start_seconds: r.start_seconds ?? null, end_seconds: r.end_seconds ?? null })) }, path: location.pathname + location.search });
    return null;
  }
  const crateId = state.auth.crateId;
  const items = list.map((r) => {
    const it = { video_id: r.video_id };
    if (r.start_seconds != null) it.start_seconds = r.start_seconds;
    if (r.end_seconds != null) it.end_seconds = r.end_seconds;
    if (Number(r.id) > 0) it.ref = { track_id: Number(r.id) };
    else if (Number(r.playlist_id) > 0 && Number(r.playlist_id) !== crateId) it.ref = { playlist_id: Number(r.playlist_id) };
    return it;
  });
  const op = { kind: 'insert', playlist_id: crateId, items };
  const token = optimistic({ ...op, items: items.map((it, i) => ({ ...it, row: list[i] })) });
  try {
    const res = await api.ops.send([op], { token });
    const n = items.length;
    toast.show(n === 1 ? 'Added to your crate' : `Added ${n} tracks to your crate`, {
      action: { label: 'Open', run: () => update((s) => { s.ui.cratePanel = true; }, 'crate-panel') },
    });
    return res;
  } catch {
    return null; // api.ops already rolled back and toasted
  }
}

export async function removeFromCrate(trackId) {
  const cid = state.auth.crateId;
  if (!cid || !(trackId > 0)) return;
  const op = { kind: 'delete', playlist_id: cid, track_ids: [trackId] };
  const token = optimistic(op);
  try {
    await api.ops.send([op], { token, expectedVersions: () => expectedFor([cid]) });
    toast.show('Removed from your crate', state.undo && state.undo.can_undo
      ? { action: { label: 'Undo', run: () => api.undo().catch((err) => toast.show(err.detail)) } } : {});
  } catch {
    /* rolled back and toasted */
  }
}

function selectedRows(ctx) {
  const sel = state.selection;
  if (sel && sel.ids && sel.ids.size && sel.playlistId != null) {
    const rows = rowsOf(sel.playlistId).filter((r) => sel.ids.has(r.id));
    if (rows.length) return rows;
  }
  const r = rowFor(ctx);
  return r ? [r] : [];
}

async function copyText(text, done = 'Link copied') {
  try {
    await navigator.clipboard.writeText(text);
    toast.show(done);
  } catch {
    toast.show('Copy failed; your browser blocked the clipboard.');
  }
}

/** Add the playing track to my crate (the player bar's crate button and the phone ⋯ menu). */
function playingToCrate() {
  const it = player.current();
  if (!it) return null;
  const fromCrate = state.player.source && state.player.source.kind === 'crate';
  return addToCrate([{
    id: fromCrate ? null : it.trackId,
    playlist_id: fromCrate ? null : it.playlistId,
    video_id: it.video_id,
    start_seconds: it.start ?? null,
    end_seconds: it.end ?? null,
    title: it.title,
    channel_title: it.channel,
    thumbnail_url: it.thumb,
  }]);
}

// --- actions ------------------------------------------------------------------------------------

registerActions({
  'play-track': (el, e, ctx) => playRow(ctx),
  'play-from': (el, e, ctx) => playRow(ctx),
  'play-playlist': (el, e, ctx) => playPlaylist(Number(el.dataset.playlistId || ctx.playlistId)),

  'crate-add': (el, e, ctx) => {
    const row = rowFor(ctx);
    if (row) return addToCrate([row]);
    if (el.dataset.videoId) return addToCrate([{ video_id: el.dataset.videoId }]);
    return null;
  },
  'crate-remove': (el, e, ctx) => removeFromCrate(ctx.trackId),
  'open-crate': () => {
    if (!state.auth.signedIn) {
      requireSignIn({ action: 'open-crate', path: '/crate' });
      return;
    }
    update((s) => { s.ui.cratePanel = !s.ui.cratePanel; }, 'crate-panel');
  },
  'close-crate': () => update((s) => { s.ui.cratePanel = false; }, 'crate-panel'),
  'dock-open': (el) => openSlot(Number(el.dataset.slot)),

  'new-playlist': (el) => {
    if (!state.auth.signedIn) {
      requireSignIn({ action: 'new-playlist', path: '/' });
      return;
    }
    update((s) => { s.ui.newPlaylist = el.dataset.where === 'dock' ? 'dock' : 'home'; }, 'new-playlist');
  },
  'new-playlist-cancel': () => update((s) => { s.ui.newPlaylist = null; }, 'new-playlist'),
  'new-playlist-submit': async (form) => {
    const input = form.querySelector('input[name="title"]');
    const title = (input && input.value.trim()) || '';
    if (!title) {
      if (input) input.focus();
      return;
    }
    if (form.dataset.busy) return;
    form.dataset.busy = '1';
    try {
      const res = await api.post('/api/playlists', { title, visibility: 'public' });
      if (res.new_badges && res.new_badges.length) toast.badge(res.new_badges);
      update((s) => { s.ui.newPlaylist = null; }, 'new-playlist');
      reloadDock().catch(() => {});
      await navigate(playlistUrl(res.playlist_id, title));
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not create the playlist.');
    } finally {
      delete form.dataset.busy;
    }
  },

  'theme-cycle': () => cycleTheme(),

  'omnibox-submit': (form) => {
    const q = (form.querySelector('input[name="q"]').value || '').trim();
    if (!q) return;
    // A YouTube link typed (not pasted) into the omnibox opens the import sheet instead of a search.
    if (/^(?:https?:\/\/)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)\//i.test(q) && slots.has('import.open')) {
      slots.call('import.open', { source: q, target: 'crate', autoInspect: true });
      return;
    }
    navigate(`/search?q=${encodeURIComponent(q)}`);
  },

  'player-toggle': () => player.toggle(),
  'player-prev': () => player.prev(),
  'player-next': () => player.next(),
  'player-seam': () => player.previewSeam(state.player.index + 1),
  'player-repeat': () => player.toggleRepeat(),
  'player-small-video': () => player.toggleSmallVideo(),
  'player-video': () => player.toggleVideo(),
  'player-keep-awake': () => player.toggleKeepAwake(),
  'player-resume': () => player.resumeSaved(),
  'player-resume-dismiss': () => player.dismissResume(),
  'player-crate': () => playingToCrate(),
  // Phones: the extra controls (like, crate, seam, repeat, video, keep awake) live behind one quiet ⋯.
  'player-more': (el) => {
    const p = state.player;
    const item = player.current();
    if (!item) return;
    const items = [
      ...slots.render('player.more', { item }),
      { label: 'Add to crate', icon: 'crate', run: () => playingToCrate() },
      p.index < p.queue.length - 1
        ? { label: 'Preview the seam into the next track', icon: 'seam', kbd: 'N', run: () => player.previewSeam(p.index + 1) }
        : null,
      'sep',
      { label: player.isRepeating() ? 'Repeat this track: on' : 'Repeat this track', icon: 'repeat', run: () => player.toggleRepeat() },
      { label: player.isSmallVideo() ? 'Small video: on' : 'Small video, low data', icon: 'small-video', run: () => player.toggleSmallVideo() },
      { label: player.isVideoCollapsed() ? 'Show the video' : 'Hide the video (pauses)', icon: 'video-off', run: () => player.toggleVideo() },
      player.keepAwakeSupported()
        ? { label: player.keepAwake() ? 'Keep screen awake: on' : 'Keep screen awake while playing', icon: 'awake', run: () => player.toggleKeepAwake() }
        : null,
    ];
    openMenu(el, items, { label: 'Player' });
  },
  'player-seek': (el, e) => {
    if (e.type === 'change' || e.type === 'input') player.seekFraction(Number(el.value) / 1000);
  },

  'menu-open': (el, e, ctx) => {
    const kind = el.dataset.menu;
    if (kind === 'row' || kind === 'crate-row') {
      const row = rowFor(ctx);
      if (!row) return;
      const mctx = { ...ctx, row, rowEl: ctx.row, crate: kind === 'crate-row' };
      const at = focusedIndexIn(ctx);
      const count = listRows(ctx.list && ctx.list.dataset.listKey).rows.length;
      const items = [
        { label: 'Play', icon: 'play', run: () => playRow(ctx) },
        at >= 1 ? { label: 'Preview the seam into this', icon: 'seam', kbd: 'B', run: () => seamAt(ctx, 'into') } : null,
        at >= 0 && at < count - 1 ? { label: 'Preview the seam out of this', icon: 'seam', kbd: 'N', run: () => seamAt(ctx, 'out') } : null,
        kind === 'crate-row'
          ? { label: 'Remove from crate', icon: 'close', run: () => removeFromCrate(Number(row.id)) }
          : { label: 'Add to crate', icon: 'plus', kbd: 'C', run: () => addToCrate([row]) },
        'sep',
        ...slots.render(kind === 'crate-row' ? 'crate.row.menu' : 'row.menu', mctx),
        'sep',
        { label: 'Open track page', icon: 'link', href: trackUrl(row.video_id, row.start_seconds, row.end_seconds) },
        { label: 'Copy link', icon: 'copy', run: () => copyText(absoluteUrl(trackUrl(row.video_id, row.start_seconds, row.end_seconds))) },
      ];
      openMenu(el, items, { label: 'Track actions' });
    } else if (kind === 'playlist') {
      const pid = Number(el.dataset.playlistId || ctx.playlistId);
      const p = state.playlists.get(pid);
      if (!p) return;
      const crate = p.kind === 'crate';
      const items = [{ label: 'Play from the start', icon: 'play', run: () => playPlaylist(pid) }];
      if (!crate && p.meta.visibility !== 'private') {
        items.push({ label: 'Copy link', icon: 'link', run: () => copyText(absoluteUrl(playlistUrl(pid, p.meta.title))) });
      }
      const extra = slots.render('playlist.menu', { playlistId: pid, playlist: p.meta, crate, mine: p.ownerId === (state.auth.user && state.auth.user.id) });
      if (extra.length) items.push('sep', ...extra);
      openMenu(el, items, { label: 'Playlist actions' });
    }
  },
});

// --- keys ---------------------------------------------------------------------------------------

function focusedIndexIn(ctx) {
  return ctx.row ? Number(ctx.row.dataset.index) : -1;
}

async function listQueue(ctx) {
  const key = ctx.list && ctx.list.closest('[data-list-key]') ? ctx.list.closest('[data-list-key]').dataset.listKey : null;
  const { rows, source } = listRows(key);
  return { items: itemsFromRows(rows, source), source, rows };
}

/** Preview the seam into ('into': the row before -> this row) or out of ('out': this row -> the next)
 *  the row in ctx: the B / N keys and the row menu share this. */
async function seamAt(ctx, dir) {
  const q = await listQueue(ctx);
  const i = focusedIndexIn(ctx);
  if (dir === 'into') {
    if (i < 1) {
      toast.show('Nothing before this track.');
      return;
    }
    player.previewSeam(i, { items: q.items, source: q.source });
    return;
  }
  if (i < 0 || i >= q.items.length - 1) {
    toast.show('Nothing after this track.');
    return;
  }
  player.previewSeam(i + 1, { items: q.items, source: q.source });
}

function isCurrentRow(ctx) {
  const cur = player.current();
  return !!(cur && ctx.trackId && cur.trackId === ctx.trackId);
}

registerKeys([
  { combo: '/', when: 'always', label: 'Search', group: 'General', run: () => {
    const box = document.getElementById('omnibox');
    if (!box) return false;
    box.focus();
    box.select();
    return true;
  } },
  { combo: 'escape', when: 'always', label: 'Close or cancel', group: 'General', run: () => {
    if (state.ui.cratePanel) {
      update((s) => { s.ui.cratePanel = false; }, 'crate-panel');
      return true;
    }
    if (state.ui.newPlaylist) {
      update((s) => { s.ui.newPlaylist = null; }, 'new-playlist');
      return true;
    }
    return false;
  } },
  { combo: 'space', when: ['list', 'player'], label: 'Play / pause', group: 'Player', run: (e) => {
    if (e.target.closest && e.target.closest('button, a[href]')) return false;
    player.toggle();
  } },
  { combo: 'k', when: ['list', 'player'], label: 'Play / pause', group: 'Player', run: () => player.toggle() },
  { combo: 'shift+arrowleft', when: ['list', 'player'], label: 'Previous track', group: 'Player', run: () => player.prev() },
  { combo: 'shift+arrowright', when: ['list', 'player'], label: 'Next track', group: 'Player', run: () => player.next() },
  { combo: 'arrowleft', when: 'player', label: 'Back 5 s', group: 'Player', run: (e) => {
    if (e.target.matches && e.target.matches('input[type=range]')) return false;
    player.seek(state.player.time - 5);
  } },
  { combo: 'arrowright', when: 'player', label: 'Forward 5 s', group: 'Player', run: (e) => {
    if (e.target.matches && e.target.matches('input[type=range]')) return false;
    player.seek(state.player.time + 5);
  } },
  { combo: 'enter', when: 'list', label: 'Play the focused track', group: 'Player', run: (e, ctx) => {
    if (!ctx.row || e.target !== ctx.row) return false;
    playRow(ctx);
  } },
  { combo: 'e', when: ['list', 'player'], label: 'Jump to 10 s before the end', group: 'Player', run: async (e, ctx) => {
    if (ctx.row && !isCurrentRow(ctx)) {
      await playRow(ctx);
    }
    player.jumpToEnd(10);
  } },
  { combo: 'b', when: 'list', label: 'Preview the seam before this track', group: 'Player', run: (e, ctx) => {
    if (!ctx.row) return false;
    return seamAt(ctx, 'into');
  } },
  { combo: 'n', when: 'list', label: 'Preview the seam after this track', group: 'Player', run: (e, ctx) => {
    if (!ctx.row) return false;
    return seamAt(ctx, 'out');
  } },
  { combo: 'c', when: 'list', label: 'Add to crate', group: 'Edit', run: (e, ctx) => {
    const rows = selectedRows(ctx);
    if (!rows.length) return false;
    addToCrate(rows);
  } },
]);

export { playPlaylist, playRow, itemFromRow };
