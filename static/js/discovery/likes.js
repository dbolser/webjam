// Likes (§4.4 B): a quiet ♡ with a mono count. A plain click toggles (count: 1, or reset when I
// already like it); Shift-click adds one more and shows "×n". No particles, no animation.
// Registered into playlist.actions (never for crates), player.like, and used by the track header.
//
//   likeButton(kind, id, { mine, count, label })   kind: 'playlist' | 'song'
//   seedLike(kind, id, mine, count)                 server-fresh values from a page load
//   toggleLike(kind, id, { add })

import { html, nothing } from '../lit.js';
import { state } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { registerKeys } from '../keys.js';
import { toast } from '../ui/toast.js';
import * as player from '../player/player.js';
import { isVideoId, iconHref } from '../format.js';
import { redraw, askSignIn } from './util.js';

const likes = new Map(); // "playlist:12" | "song:abcdefghijk" -> { mine, count }
const chains = new Map(); // key -> promise (one request at a time per item)
const seenMeta = new WeakSet();

const keyOf = (kind, id) => `${kind}:${id}`;

export function seedLike(kind, id, mine, count) {
  if (id == null) return;
  likes.set(keyOf(kind, id), { mine: Number(mine) || 0, count: Number(count) || 0 });
}

export function likeState(kind, id, fallback = {}) {
  return likes.get(keyOf(kind, id)) || { mine: Number(fallback.mine) || 0, count: Number(fallback.count) || 0 };
}

function path(kind, id) {
  return kind === 'playlist' ? `/api/playlists/${Number(id)}/like` : `/api/songs/${encodeURIComponent(id)}/like`;
}

/** add: Shift-click (one more). Otherwise a toggle: like once, or take my likes back. */
export function toggleLike(kind, id, { add = false, fallback } = {}) {
  if (!state.auth.signedIn) {
    askSignIn({ action: 'like', payload: kind === 'playlist' ? { playlist_id: Number(id) } : { video_id: id } }, kind === 'playlist' ? 'like playlists' : 'like tracks');
    return Promise.resolve(null);
  }
  if (kind === 'song' && !isVideoId(id)) return Promise.resolve(null);
  if (kind === 'playlist' && !(Number(id) > 0)) return Promise.resolve(null);
  const k = keyOf(kind, id);
  const before = likeState(kind, id, fallback);
  const reset = !add && before.mine >= 1;
  // Optimistic: the heart changes at once; the server's numbers replace it.
  const guess = reset
    ? { mine: 0, count: Math.max(0, before.count - before.mine) }
    : { mine: before.mine + 1, count: before.count + 1 };
  likes.set(k, guess);
  redraw('dc-like');
  const body = reset ? { reset: true, count: 0 } : { count: 1 };
  const prev = chains.get(k) || Promise.resolve();
  const run = prev.catch(() => {}).then(() => api.queued(() => api.post(path(kind, id), body)))
    .then((res) => {
      if (res) likes.set(k, { mine: Number(res.my_likes) || 0, count: Number(res.like_count) || 0 });
      redraw('dc-like');
      return res;
    })
    .catch((err) => {
      likes.set(k, before);
      redraw('dc-like');
      if (!err.handled) toast.show(err.detail || 'That like did not save.');
      return null;
    });
  chains.set(k, run);
  run.finally(() => { if (chains.get(k) === run) chains.delete(k); });
  return run;
}

/** The ♡ control. `mine`/`count` are the page's values, used until this session knows better. */
export function likeButton(kind, id, { mine = 0, count = 0, label = '' } = {}) {
  if (id == null || id === '') return nothing;
  const s = likeState(kind, id, { mine, count });
  const on = s.mine > 0;
  const what = label || (kind === 'playlist' ? 'this playlist' : 'this track');
  const aria = on
    ? `Unlike ${what}${s.mine > 1 ? ` (you liked it ${s.mine} times)` : ''}`
    : `Like ${what}`;
  return html`<button type="button" class="dc-like ${on ? 'is-on' : ''}" data-action="like-toggle"
      data-like-kind=${kind} data-like-id=${String(id)} data-like-mine=${s.mine} data-like-count=${s.count}
      aria-pressed=${on ? 'true' : 'false'} aria-label=${aria}
      title=${on ? 'Click to unlike · Shift-click to like once more' : 'Like · Shift-click to like more than once'}>
    <svg class="i"><use href=${iconHref(on ? 'heart-fill' : 'heart')}></use></svg>
    ${s.count > 0 ? html`<span class="dc-like-n">${s.count}</span>` : nothing}
    ${s.mine > 1 ? html`<span class="dc-like-x">×${s.mine}</span>` : nothing}
  </button>`;
}

// --- registrations ------------------------------------------------------------------------------

registerActions({
  'like-toggle': (el, event) => {
    const kind = el.dataset.likeKind === 'playlist' ? 'playlist' : 'song';
    const id = kind === 'playlist' ? Number(el.dataset.likeId) : el.dataset.likeId;
    return toggleLike(kind, id, {
      add: !!event.shiftKey,
      fallback: { mine: el.dataset.likeMine, count: el.dataset.likeCount },
    });
  },
});

// ♡ beside Play on a playlist page. The crate never gets one (the view does not render this slot
// for crates; the check here is belt and braces).
slots.register('playlist.actions', (ctx) => {
  if (!ctx || ctx.crate || !ctx.playlist || ctx.playlist.kind === 'crate') return null;
  const meta = ctx.playlist;
  // A freshly loaded meta object is server truth: let it replace what this session guessed.
  if (!seenMeta.has(meta) && !chains.has(keyOf('playlist', ctx.playlistId))) {
    seenMeta.add(meta);
    seedLike('playlist', ctx.playlistId, meta.my_likes, meta.like_count);
  }
  return likeButton('playlist', ctx.playlistId, { mine: meta.my_likes, count: meta.like_count, label: meta.title });
}, { order: 10 });

function songFallback(item) {
  const row = item && item.trackId ? state.tracks.get(Number(item.trackId)) : null;
  return row ? { mine: row.song_my_likes, count: row.song_like_count } : {};
}

// ♡ for the track that is playing.
slots.register('player.like', ({ item } = {}) => {
  if (!item || !isVideoId(item.video_id)) return null;
  const fb = songFallback(item);
  return likeButton('song', item.video_id, { ...fb, label: item.title });
});

registerKeys([
  { combo: 'l', when: ['list', 'player'], label: 'Like the focused or playing track', group: 'Player', run: (e, ctx) => {
    let vid = ctx && ctx.row ? ctx.row.dataset.videoId : null;
    let fb = {};
    if (vid && ctx.trackId) {
      const row = state.tracks.get(Number(ctx.trackId));
      if (row) fb = { mine: row.song_my_likes, count: row.song_like_count };
    }
    if (!vid) {
      const cur = player.current();
      if (!cur) return false;
      vid = cur.video_id;
      fb = songFallback(cur);
    }
    if (!isVideoId(vid)) return false;
    toggleLike('song', vid, { fallback: fb }).then((res) => {
      if (res) toast.show(res.my_likes > 0 ? 'Liked' : 'Like removed');
    });
  } },
  { combo: 'shift+l', when: ['list', 'player'], label: 'Like once more', group: 'Player', run: (e, ctx) => {
    const vid = ctx && ctx.row ? ctx.row.dataset.videoId : (player.current() || {}).video_id;
    if (!isVideoId(vid)) return false;
    toggleLike('song', vid, { add: true });
  } },
]);
