// The only place a track row is built (docs/phase1b-shell.md §1.4).
//
//   trackList(rows, { key, context, editable, playlistId, label, source })
//   trackRow(row, { index, context, editable, selected, cursor, playing, isNew, cut, dead, tabindex })
//   compactList(rows, { key, source, label })     32px preview rows for cards and home lists
//   listRows(key) -> { rows, source }              what play-track uses to build a queue
//
// context: 'playlist' | 'crate' | 'track-page' | 'search' | 'card' | 'home'
// key: unique per rendered list (e.g. `pl:12`, `crate`, `home:fresh`); written to data-list-key.
// source: { playlistId, title, kind } attached to the player queue (play counting, "from" link).
// Rows are TrackMini or GET-playlist rows: { id, playlist_id?, video_id, title, channel_title,
// thumbnail_url, start_seconds, end_seconds, track_note?, source_username?, source_playlist_id?,
// source_playlist_title?, playlist_count? }. Temp rows (negative ids) render as pending.

import { html, nothing, repeat, classMap } from '../lit.js';
import { state } from '../store.js';
import { trackUrl, playlistUrl, userUrl, clipLabel, mmss, plural, thumbSrc } from '../format.js';
import { displayLength } from '../player/durations.js';

const lists = new Map();

export function registerList(key, rows, source) {
  lists.set(key, { rows, source: source || null });
}

export function listRows(key) {
  return lists.get(key) || { rows: [], source: null };
}

/** Is this row the one the player is on? */
export function isPlaying(row) {
  const p = state.player;
  const cur = p.index >= 0 ? p.queue[p.index] : null;
  if (!cur || !row) return false;
  if (cur.trackId && row.id) return Number(cur.trackId) === Number(row.id);
  return !cur.trackId && cur.video_id === row.video_id;
}

function selectedIn(row, playlistId) {
  const sel = state.selection;
  return !!(sel && sel.ids && sel.ids.has(row.id) && (sel.playlistId == null || sel.playlistId === playlistId));
}

function metaLine(row, context) {
  const parts = [];
  if (row.channel_title) parts.push(html`<span>${row.channel_title}</span>`);
  if (row.source_username && context !== 'crate') {
    parts.push(html`<a class="hop" href=${userUrl(row.source_username)}>via @${row.source_username}</a>`);
  }
  if (context === 'crate' || context === 'home') {
    if (row.source_playlist_id && row.source_playlist_title) {
      parts.push(html`<a class="hop" href=${playlistUrl(row.source_playlist_id, row.source_playlist_title)}>from ‘${row.source_playlist_title}’</a>`);
    } else if (row.source_username && context === 'crate') {
      parts.push(html`<a class="hop" href=${userUrl(row.source_username)}>via @${row.source_username}</a>`);
    }
  }
  const min = context === 'playlist' ? 2 : 1;
  const n = Number(row.playlist_count) || 0;
  if (n >= min) parts.push(html`<a class="hop" href=${trackUrl(row.video_id)}>in ${plural(n, 'playlist')}</a>`);
  if (state.dead.has(row.video_id)) parts.push(html`<span class="sep dead-label">unavailable</span>`);
  const len = displayLength(row);
  if (len) parts.push(html`<span class="sep dur-inline mono">${mmss(len)}</span>`);
  return parts;
}

export function trackRow(row, o = {}) {
  const {
    index = 0, context = 'playlist', selected = false, cursor = false,
    playing = false, isNew = false, cut = false, tabindex = -1,
  } = o;
  const dead = state.dead.has(row.video_id);
  const clip = clipLabel(row.start_seconds, row.end_seconds);
  const len = displayLength(row);
  const inCrate = context === 'crate';
  const cls = classMap({
    trow: true,
    'is-selected': selected,
    'is-cursor': cursor && !selected,
    'is-new': isNew,
    'is-playing': playing,
    'is-cut': cut,
    'is-dead': dead,
    'is-pending': !!row.pending || row.id < 0,
  });
  return html`<li class=${cls}
      data-track-id=${row.id}
      data-playlist-id=${row.playlist_id ?? nothing}
      data-video-id=${row.video_id}
      data-index=${index}
      role="option"
      aria-selected=${selected ? 'true' : 'false'}
      tabindex=${tabindex}>
    <span class="trow-pos">${playing
      ? html`<svg class="i" aria-label="Playing"><use href="#i-play"></use></svg>`
      : index + 1}</span>
    <img class="thumb" src=${thumbSrc(row.thumbnail_url, row.video_id)} alt="" loading="lazy" decoding="async" draggable="false" />
    <div class="trow-main">
      <a class="trow-title" href=${trackUrl(row.video_id, row.start_seconds, row.end_seconds)} title=${row.title || ''}>${row.title || row.video_id}</a>
      <div class="trow-meta">${metaLine(row, context)}</div>
      ${row.track_note ? html`<div class="trow-note">${row.track_note}</div>` : nothing}
    </div>
    ${clip ? html`<span class="clip" title="Clip">${clip}</span>` : html`<span class="clip-none"></span>`}
    <span class="trow-dur">${len ? mmss(len) : ''}</span>
    <span class="trow-actions">
      <button type="button" class="icon-btn" data-action="play-track" aria-label="Play ${row.title || 'track'}">
        <svg class="i"><use href="#i-play"></use></svg>
      </button>
      ${inCrate
        ? html`<button type="button" class="icon-btn" data-action="crate-remove" aria-label="Remove from crate">
            <svg class="i"><use href="#i-close"></use></svg></button>`
        : html`<button type="button" class="icon-btn" data-action="crate-add" aria-label="Add to crate">
            <svg class="i"><use href="#i-plus"></use></svg></button>`}
      <button type="button" class="icon-btn" data-action="menu-open" data-menu=${inCrate ? 'crate-row' : 'row'}
        aria-label="More" aria-haspopup="menu" aria-expanded="false">
        <svg class="i"><use href="#i-more"></use></svg>
      </button>
    </span>
  </li>`;
}

export function trackList(rows, opts = {}) {
  const { key, context = 'playlist', editable = false, playlistId = null, label = 'Tracks', source = null } = opts;
  if (!key) throw new Error('trackList: key is required');
  registerList(key, rows, source || (playlistId ? { playlistId, kind: context === 'crate' ? 'crate' : 'playlist' } : null));
  const sel = state.selection || {};
  const cursorId = sel.cursorId != null && rows.some((r) => r.id === sel.cursorId) ? sel.cursorId : null;
  const rovingId = cursorId ?? (rows[0] && rows[0].id);
  const cutIds = state.clipboard && state.clipboard.mode === 'cut'
    ? new Set((state.clipboard.items || []).map((it) => it.track_id ?? it.id)) : null;
  return html`<ol class="tracks" role="listbox" aria-label=${label} aria-multiselectable="true"
      data-list-key=${key}
      data-context=${context}
      data-playlist-id=${playlistId ?? nothing}
      ?data-editable=${editable}>
    ${repeat(rows, (r) => r.id, (r, i) => trackRow(r, {
      index: i,
      context,
      editable,
      selected: selectedIn(r, playlistId),
      cursor: r.id === cursorId,
      playing: isPlaying(r),
      isNew: state.landed.has(r.id),
      cut: !!(cutIds && cutIds.has(r.id)),
      tabindex: r.id === rovingId ? 0 : -1,
    }))}
    <li class="insert-line" role="presentation" aria-hidden="true" hidden></li>
  </ol>`;
}

export function compactRow(t, { index = 0 } = {}) {
  const playing = isPlaying(t);
  return html`<li class="crow ${playing ? 'is-playing' : ''}" data-track-id=${t.id ?? nothing}
      data-playlist-id=${t.playlist_id ?? nothing} data-video-id=${t.video_id} data-index=${index}>
    <img class="thumb" src=${thumbSrc(t.thumbnail_url, t.video_id)} alt="" loading="lazy" decoding="async" draggable="false" />
    <span class="crow-text">
      <a href=${trackUrl(t.video_id, t.start_seconds, t.end_seconds)}>${t.title || t.video_id}</a>${t.channel_title
        ? html`<span class="ch">${t.channel_title}</span>` : nothing}
    </span>
    <button type="button" class="icon-btn" data-action="play-track" aria-label="Play ${t.title || 'track'}">
      <svg class="i"><use href="#i-play"></use></svg>
    </button>
  </li>`;
}

export function compactList(rows, { key, source = null, label = 'Preview' } = {}) {
  if (!key) throw new Error('compactList: key is required');
  registerList(key, rows, source);
  return html`<ol class="card-preview" aria-label=${label} data-list-key=${key}>
    ${repeat(rows, (r) => r.id ?? r.video_id, (r, i) => compactRow(r, { index: i }))}
  </ol>`;
}
