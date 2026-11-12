// The track page (/t/{video_id}, §2.4): what this track is, which playlists hold it and who
// curates them, what it is often played alongside, and comments. Every item is a one-click hop:
// track -> playlists -> curators -> their playlists. GET /api/songs/{video_id}.
// ?s=&e= sets the clip when it is played from this page.

import { html, nothing, repeat } from '../lit.js';
import { state } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { trackList } from '../components/track-row.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import * as player from '../player/player.js';
import { addToCrate, playPlaylist } from '../core.js';
import {
  playlistUrl, userUrl, plural, thumbSrc, clipLabel, isVideoId, relTime, isoTs,
} from '../format.js';
import { likeButton, seedLike } from '../discovery/likes.js';
import { commentsSection } from '../discovery/comments.js';

const cache = new Map(); // video_id -> song payload (a few, newest last)
const CAP = 12;

function remember(vid, song) {
  cache.delete(vid);
  cache.set(vid, song);
  while (cache.size > CAP) cache.delete(cache.keys().next().value);
}

function clipFrom(query) {
  const num = (v) => {
    if (v == null || v === '') return null;
    const n = Math.floor(Number(v));
    return Number.isFinite(n) && n >= 0 && n <= 86400 ? n : null;
  };
  const s = num(query.s);
  let e = num(query.e);
  if (s != null && e != null && e <= s) e = null;
  return { start: s, end: e };
}

export async function load(ctx) {
  const vid = ctx.params.vid;
  if (!isVideoId(vid)) throw new api.ApiError(404, 'song_not_found', 'Track not found');
  const song = await api.get(`/api/songs/${encodeURIComponent(vid)}`);
  seedLike('song', vid, song.my_likes, song.like_count);
  remember(vid, song);
}

export function title(ctx) {
  const s = cache.get(ctx.params.vid);
  return s ? s.title : 'Track';
}

/** The first placement of this video in each playlist the viewer can see: playlist id -> appearance. */
function firstPlacements(song) {
  const out = new Map();
  for (const a of song.appearances || []) {
    const have = out.get(a.playlist_id);
    if (!have || Number(a.position) < Number(have.position)) out.set(a.playlist_id, a);
  }
  return out;
}

/** The placement "+ Crate" credits: the debut when visible, else the first listed placement. */
function creditPlacement(song, firsts) {
  if (song.first_added_playlist_id && firsts.has(song.first_added_playlist_id)) return firsts.get(song.first_added_playlist_id);
  for (const p of song.playlists || []) {
    if (p.playlist && p.playlist.visibility === 'public' && firsts.has(p.playlist.id)) return firsts.get(p.playlist.id);
  }
  return null;
}

function lovers(song) {
  const users = (song.liked_by_users || []).filter((u) => u && u.username);
  if (!users.length) return nothing;
  const shown = users.slice(0, 4);
  const rest = users.length - shown.length;
  return html`<p class="dc-lovers">liked by ${shown.map((u, i) => html`${i ? ', ' : ''}<a class="hop" href=${userUrl(u.username)}>@${u.username}</a>${u.count > 1 ? html`<span class="num"> ×${u.count}</span>` : nothing}`)}${rest > 0 ? html` and ${plural(rest, 'other')}` : nothing}</p>`;
}

function inPlaylistTpl(song, entry, firsts) {
  const pl = entry.playlist || {};
  const first = firsts.get(pl.id);
  const pos = Number(entry.first_position) || (first && Number(first.position)) || 1;
  const mine = state.auth.user && pl.user && pl.user.id === state.auth.user.id;
  return html`<li class="dc-in" data-playlist-id=${pl.id}>
    <div class="dc-in-main">
      <a class="dc-in-title" href=${playlistUrl(pl.id, pl.title, { track: first && first.track_entry_id })}>${pl.title}</a>
      <p class="dc-in-meta">
        ${pl.user ? html`<a class="hop" href=${userUrl(pl.user.username)}>by @${pl.user.username}</a>` : nothing}
        <span class="num">#${pos}</span>
        ${Number(entry.occurrences) > 1 ? html`<span class="num">×${entry.occurrences}</span>` : nothing}
        <span>${plural(pl.track_count, 'track')}</span>
        ${mine && pl.visibility && pl.visibility !== 'public' ? html`<span class="tag">${pl.visibility}</span>` : nothing}
      </p>
      ${entry.note ? html`<p class="dc-in-note">${entry.note}</p>` : nothing}
    </div>
    <span class="dc-in-actions">
      <button type="button" class="icon-btn" data-action="dc-play-in" data-playlist-id=${pl.id} data-position=${pos}
        aria-label=${`Play ‘${pl.title}’ from this track`} title="Play the playlist from here">
        <svg class="i"><use href="#i-play"></use></svg></button>
      ${first
        ? html`<button type="button" class="icon-btn" data-action="dc-track-crate" data-entry-id=${first.track_entry_id}
            aria-label=${pl.user ? `Add to crate, credited to @${pl.user.username}` : 'Add to crate'}
            title=${pl.user ? `Add to crate · via @${pl.user.username}` : 'Add to crate'}>
            <svg class="i"><use href="#i-plus"></use></svg></button>`
        : nothing}
    </span>
  </li>`;
}

export function render(ctx) {
  const vid = ctx.params.vid;
  const song = cache.get(vid);
  if (!song) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  const clip = clipFrom(ctx.query);
  const clipText = clipLabel(clip.start, clip.end);
  const firsts = firstPlacements(song);
  const playlists = song.playlists || [];
  const curators = song.curators || [];
  const along = (song.alongside || []).map((a) => ({ ...a, id: `a:${a.video_id}` }));
  const count = Number(song.playlist_count) || 0;
  const debutVisible = song.first_added_playlist_id && song.debut_playlist_title;
  return html`<div class="page dc-track" data-video-id=${vid}>
    <header class="dc-track-head">
      <img class="thumb" src=${thumbSrc(song.thumbnail_url, vid)} alt="" decoding="async" />
      <div class="dc-track-id">
        <h1 tabindex="-1">${song.title}</h1>
        <p class="byline">
          <span>${song.channel_title}</span>
          ${song.finder_username
            ? html`<span class="dot-sep">first found by <a class="hop" href=${userUrl(song.finder_username)}>@${song.finder_username}</a>${debutVisible
                ? html` in <a class="hop" href=${playlistUrl(song.first_added_playlist_id, song.debut_playlist_title)}>‘${song.debut_playlist_title}’</a>`
                : nothing}${song.first_added_at ? html` <time datetime=${isoTs(song.first_added_at)}>${relTime(song.first_added_at)}</time>` : nothing}</span>`
            : nothing}
          ${clipText ? html`<span class="dot-sep"><span class="clip" title="Clip">${clipText}</span></span>` : nothing}
        </p>
      </div>
    </header>
    <div class="page-actions">
      <button type="button" class="btn btn-primary" data-action="dc-track-play">
        <svg class="i"><use href="#i-play"></use></svg>Play</button>
      <button type="button" class="btn" data-action="dc-track-crate">
        <svg class="i"><use href="#i-plus"></use></svg>Crate</button>
      ${likeButton('song', vid, { mine: song.my_likes, count: song.like_count, label: song.title })}
      ${slots.render('track.actions', { videoId: vid, song, start: clip.start, end: clip.end })}
      <a class="btn btn-quiet dc-yt" href="https://www.youtube.com/watch?v=${vid}" target="_blank" rel="external noopener noreferrer">YouTube</a>
    </div>
    ${lovers(song)}

    <section class="section" aria-labelledby="dc-h-in">
      <h2 class="label" id="dc-h-in">In ${plural(playlists.length || count, 'playlist')}</h2>
      ${playlists.length
        ? html`<ul class="dc-in-list">${repeat(playlists, (e) => e.playlist.id, (e) => inPlaylistTpl(song, e, firsts))}</ul>`
        : empty('Not in any public playlist yet.')}
    </section>

    ${curators.length
      ? html`<section class="section" aria-labelledby="dc-h-cur">
          <h2 class="label" id="dc-h-cur">Curators</h2>
          <ul class="people">${curators.map((c) => html`<li class="person">
            ${avatar(c.user, { small: true })}
            <a href=${userUrl(c.user.username)}>@${c.user.username}</a>
            ${Number(c.playlist_count) > 1 ? html`<span class="count">${plural(c.playlist_count, 'playlist')}</span>` : nothing}
          </li>`)}</ul>
        </section>`
      : nothing}

    ${along.length
      ? html`<section class="section" aria-labelledby="dc-h-along">
          <h2 class="label" id="dc-h-along">Often alongside</h2>
          ${trackList(along, { key: `along:${vid}`, context: 'search', label: 'Often alongside' })}
        </section>`
      : nothing}

    ${commentsSection({ type: 'song', id: vid, comments: song.comments, hasMore: song.comments_has_more })}
  </div>`;
}

function currentSong() {
  const r = state.route;
  const vid = r && r.name === 'track' ? r.params.vid : null;
  return vid ? { vid, song: cache.get(vid), clip: clipFrom(r.query || {}) } : null;
}

registerActions({
  'dc-track-play': () => {
    const cur = currentSong();
    if (!cur || !cur.song) return;
    const s = cur.song;
    player.playQueue([{
      trackId: null, playlistId: null, video_id: cur.vid, start: cur.clip.start, end: cur.clip.end,
      title: s.title, channel: s.channel_title || '', thumb: s.thumbnail_url || '',
    }], 0, { source: null });
  },
  // "+ Crate" in the header credits the debut (or first listed) placement; the one on a playlist
  // line credits that curator's placement.
  'dc-track-crate': (el) => {
    const cur = currentSong();
    if (!cur || !cur.song) return null;
    const s = cur.song;
    const firsts = firstPlacements(s);
    const entryId = Number(el.dataset.entryId);
    let at = null;
    if (entryId > 0) at = (s.appearances || []).find((a) => Number(a.track_entry_id) === entryId) || null;
    else at = creditPlacement(s, firsts);
    const useOwnClip = !(entryId > 0) && (cur.clip.start != null || cur.clip.end != null);
    return addToCrate([{
      id: at ? Number(at.track_entry_id) : undefined,
      playlist_id: at ? Number(at.playlist_id) : undefined,
      video_id: cur.vid,
      title: s.title,
      channel_title: s.channel_title,
      thumbnail_url: s.thumbnail_url,
      start_seconds: useOwnClip ? cur.clip.start : at ? at.start_seconds : null,
      end_seconds: useOwnClip ? cur.clip.end : at ? at.end_seconds : null,
    }]);
  },
  'dc-play-in': (el) => {
    const pid = Number(el.dataset.playlistId);
    const pos = Math.max(1, Number(el.dataset.position) || 1);
    if (pid) return playPlaylist(pid, pos - 1);
    return null;
  },
});
