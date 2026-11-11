// The share popover (docs/phase1b-shell.md §4.4).
//
//   openShare(anchor, target)
//     target: { kind: 'playlist', playlistId }
//           | { kind: 'track', videoId, start, end, trackId?, playlistId?, title? }
//
// Playlist: copies the link as it opens and says "Link copied". Options: start at the selected
// track (/p/{id}?track={tid}), copy as a text list, the system share sheet when there is one.
// Private playlists and the crate say "Only you can open this" and offer no link. Unlisted says
// "Anyone with the link can open it".
// Track: the /t/{vid} link (with the clip), "Start at m:ss" while that track is playing, and,
// for a row in a public or unlisted playlist, the credited /p/{id}?track={tid} link instead.
//
// Wiring: the Share button in 'playlist.actions', the 'share.open' slot (Lane B's track page),
// the data-action "share-open" (data-share="track" + data-video-id for a track), "Share…" in the
// row menu, and the S key in a list.

import { html, nothing } from '../lit.js';
import { state, rowsOf } from '../store.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { registerKeys } from '../keys.js';
import { toast } from '../ui/toast.js';
import { openPopover, rerenderPopover, popoverAnchor, closePopover } from '../ui/popover.js';
import { playlistUrl, trackUrl, absoluteUrl, clipLabel, mmss, plural, isVideoId } from '../format.js';
import { copyText } from './util.js';

let cur = null; // { target, copied, atTrack, atTime, inPlaylist }

function nativeShare() {
  return typeof navigator !== 'undefined' && typeof navigator.share === 'function';
}

function playlistInfo(pid) {
  const p = state.playlists.get(Number(pid));
  if (!p) return null;
  const crate = p.kind === 'crate';
  const vis = crate ? 'private' : p.meta.visibility || 'public';
  return { p, crate, vis, title: crate ? 'Crate' : p.meta.title };
}

function selectedTrackIn(pid) {
  const sel = state.selection;
  if (!sel || Number(sel.playlistId) !== Number(pid) || !sel.ids || sel.ids.size !== 1) return null;
  const tid = [...sel.ids][0];
  return tid > 0 ? state.tracks.get(tid) || null : null;
}

function playingSeconds(videoId) {
  const pl = state.player;
  const item = pl.index >= 0 ? pl.queue[pl.index] : null;
  if (!item || item.video_id !== videoId) return 0;
  return Math.floor(Number(pl.time) || 0);
}

/** The link the popover currently shows, or '' when there is none to give. */
function currentUrl() {
  const t = cur.target;
  if (t.kind === 'playlist') {
    const info = playlistInfo(t.playlistId);
    if (!info || info.vis === 'private') return '';
    const row = cur.atTrack ? selectedTrackIn(t.playlistId) : null;
    return absoluteUrl(playlistUrl(t.playlistId, info.title, { track: row ? row.id : null }));
  }
  if (cur.inPlaylist && t.trackId > 0 && t.playlistId) {
    const info = playlistInfo(t.playlistId);
    if (info) return absoluteUrl(playlistUrl(t.playlistId, info.title, { track: t.trackId }));
  }
  const at = cur.atTime ? playingSeconds(t.videoId) : 0;
  return absoluteUrl(trackUrl(t.videoId, at > 0 ? at : t.start, t.end));
}

function textList(pid) {
  const info = playlistInfo(pid);
  if (!info) return '';
  const rows = rowsOf(pid).filter((r) => r.id > 0);
  const linkable = info.vis !== 'private';
  const head = linkable ? `${info.title}\n${absoluteUrl(playlistUrl(pid, info.title))}\n` : `${info.title}\n`;
  const lines = rows.map((r) => {
    const url = linkable
      ? absoluteUrl(playlistUrl(pid, null, { track: r.id }))
      : absoluteUrl(trackUrl(r.video_id, r.start_seconds, r.end_seconds));
    const clip = clipLabel(r.start_seconds, r.end_seconds);
    return `${url}  ${r.title || r.video_id}${clip ? ` (${clip})` : ''}`;
  });
  return `${head}\n${lines.join('\n')}\n`;
}

async function copyCurrent() {
  const url = currentUrl();
  if (!url) return;
  const ok = await copyText(url);
  if (!cur) return;
  cur.copied = ok ? 'Link copied' : 'Copy the link below';
  rerenderPopover();
}

function playlistTpl() {
  const t = cur.target;
  const info = playlistInfo(t.playlistId);
  if (!info) return html`<p class="quiet-line">Loading…</p>`;
  const url = currentUrl();
  const sel = selectedTrackIn(t.playlistId);
  return html`<div class="sh-share">
    ${info.vis === 'private'
      ? html`<p class="sh-share-status">Only you can open this.</p>
          <p class="sh-hint">${info.crate ? 'Your crate is private.' : 'Make it public or unlisted in settings to share a link.'}</p>`
      : html`<p class="sh-share-status" role="status">${cur.copied || 'Link'}</p>
          <input class="input sh-share-url" type="text" readonly .value=${url} aria-label="Link to share" />
          ${info.vis === 'unlisted' ? html`<p class="sh-hint">Anyone with the link can open it.</p>` : nothing}
          ${sel
            ? html`<label class="sh-check"><input type="checkbox" data-action="share-at-track" .checked=${!!cur.atTrack} />
                <span>Start at ‘${sel.title || sel.video_id}’</span></label>`
            : nothing}`}
    <div class="sh-share-actions">
      ${url ? html`<button type="button" class="btn" data-action="share-copy"><svg class="i"><use href="#i-link"></use></svg>Copy link</button>` : nothing}
      <button type="button" class="btn btn-quiet" data-action="share-text"><svg class="i"><use href="#i-copy"></use></svg>Copy as text</button>
      ${url && nativeShare() ? html`<button type="button" class="btn btn-quiet" data-action="share-native">
          <svg class="i"><use href="#i-share"></use></svg>Share…</button>` : nothing}
    </div>
  </div>`;
}

function trackTpl() {
  const t = cur.target;
  const url = currentUrl();
  const at = playingSeconds(t.videoId);
  const info = t.playlistId && t.trackId > 0 ? playlistInfo(t.playlistId) : null;
  const credited = !!(info && info.vis !== 'private');
  return html`<div class="sh-share">
    <p class="sh-share-status" role="status">${cur.copied || 'Link'}</p>
    <input class="input sh-share-url" type="text" readonly .value=${url} aria-label="Link to share" />
    ${credited
      ? html`<label class="sh-check"><input type="checkbox" data-action="share-in-playlist" .checked=${!!cur.inPlaylist} />
          <span>Link to this spot in ‘${info.title}’</span></label>`
      : nothing}
    ${at > 4 && !cur.inPlaylist
      ? html`<label class="sh-check"><input type="checkbox" data-action="share-at-time" .checked=${!!cur.atTime} />
          <span>Start at <span class="mono">${mmss(at)}</span></span></label>`
      : nothing}
    <div class="sh-share-actions">
      <button type="button" class="btn" data-action="share-copy"><svg class="i"><use href="#i-link"></use></svg>Copy link</button>
      ${nativeShare() ? html`<button type="button" class="btn btn-quiet" data-action="share-native">
          <svg class="i"><use href="#i-share"></use></svg>Share…</button>` : nothing}
    </div>
  </div>`;
}

export function openShare(anchor, target) {
  if (!target) return;
  if (anchor && popoverAnchor() === anchor) {
    closePopover();
    return;
  }
  if (target.kind === 'track' && !isVideoId(target.videoId)) return;
  cur = { target, copied: '', atTrack: false, atTime: false, inPlaylist: false };
  if (target.kind === 'playlist') {
    // "Start at the selected track" is on by default when exactly one row is selected.
    cur.atTrack = !!selectedTrackIn(target.playlistId);
  } else if (target.trackId > 0 && target.playlistId) {
    const info = playlistInfo(target.playlistId);
    cur.inPlaylist = !!(info && info.vis !== 'private');
  }
  const label = target.kind === 'playlist' ? 'Share playlist' : 'Share track';
  openPopover(anchor, () => (cur ? (cur.target.kind === 'playlist' ? playlistTpl() : trackTpl()) : nothing), {
    label,
    onClose: () => { cur = null; },
  });
  // Copy straight away (this runs inside the click that opened it, so the clipboard allows it).
  if (currentUrl()) copyCurrent();
}

function targetFromEl(el, ctx) {
  const d = el.dataset;
  const vid = d.videoId || (d.share === 'track' ? ctx.videoId : null);
  if (d.share === 'track' || (vid && !d.playlistId)) {
    return {
      kind: 'track',
      videoId: vid,
      start: d.start ? Number(d.start) : null,
      end: d.end ? Number(d.end) : null,
      trackId: ctx.trackId || null,
      playlistId: ctx.playlistId || null,
    };
  }
  const pid = Number(d.playlistId || ctx.playlistId);
  return pid ? { kind: 'playlist', playlistId: pid } : null;
}

registerActions({
  'share-open': (el, e, ctx) => openShare(el, targetFromEl(el, ctx)),
  'share-copy': () => copyCurrent(),
  'share-at-track': (el, e) => {
    if (!cur || e.type !== 'change') return;
    cur.atTrack = el.checked;
    copyCurrent();
  },
  'share-at-time': (el, e) => {
    if (!cur || e.type !== 'change') return;
    cur.atTime = el.checked;
    copyCurrent();
  },
  'share-in-playlist': (el, e) => {
    if (!cur || e.type !== 'change') return;
    cur.inPlaylist = el.checked;
    copyCurrent();
  },
  'share-text': async () => {
    if (!cur || cur.target.kind !== 'playlist') return;
    const pid = cur.target.playlistId;
    const text = textList(pid);
    const n = rowsOf(pid).filter((r) => r.id > 0).length;
    const ok = await copyText(text);
    closePopover({ restoreFocus: true });
    toast.show(ok ? `Copied ${plural(n, 'track')} as text` : 'Copy failed; your browser blocked the clipboard.');
  },
  'share-native': async () => {
    if (!cur || !nativeShare()) return;
    const url = currentUrl();
    if (!url) return;
    const t = cur.target;
    let title = 'webjam';
    if (t.kind === 'playlist') {
      const info = playlistInfo(t.playlistId);
      if (info) title = info.title;
    } else {
      const row = t.trackId ? state.tracks.get(Number(t.trackId)) : null;
      title = t.title || (row && row.title) || 'A track on webjam';
    }
    closePopover({ restoreFocus: true });
    try {
      await navigator.share({ title, url });
    } catch {
      /* dismissed */
    }
  },
});

slots.register('playlist.actions', (ctx = {}) => {
  if (ctx.crate || !ctx.playlistId) return null;
  return html`<button type="button" class="btn btn-quiet" data-action="share-open" data-playlist-id=${ctx.playlistId}
    aria-haspopup="dialog"><svg class="i"><use href="#i-share"></use></svg>Share</button>`;
}, { order: 50 });

// 'share.open' for other lanes: slots.call('share.open', anchorEl, target).
slots.register('share.open', (anchor, target) => {
  openShare(anchor, target);
  return true;
});

slots.register('row.menu', (mctx = {}) => {
  const row = mctx.row;
  if (!row || !isVideoId(row.video_id)) return null;
  return [{
    label: 'Share…',
    icon: 'share',
    run: () => {
      const anchor = (mctx.rowEl && mctx.rowEl.querySelector('[data-menu]')) || mctx.rowEl || null;
      openShare(anchor, {
        kind: 'track',
        videoId: row.video_id,
        start: row.start_seconds,
        end: row.end_seconds,
        trackId: Number(row.id) > 0 ? Number(row.id) : null,
        playlistId: Number(row.playlist_id) > 0 ? Number(row.playlist_id) : null,
        title: row.title,
      });
    },
  }];
}, { order: 50 });

registerKeys([
  { combo: 's', when: 'list', label: 'Share the playlist (or the selected track)', group: 'Share', run: (e, ctx) => {
    const pid = Number(ctx.list && ctx.list.dataset.playlistId) || null;
    const row = pid ? selectedTrackIn(pid) : null;
    if (row) {
      const rowEl = document.querySelector(`#view [data-track-id="${Number(row.id)}"], #crate-panel [data-track-id="${Number(row.id)}"]`);
      openShare((rowEl && rowEl.querySelector('[data-menu]')) || rowEl, {
        kind: 'track', videoId: row.video_id, start: row.start_seconds, end: row.end_seconds,
        trackId: Number(row.id), playlistId: pid, title: row.title,
      });
      return true;
    }
    if (pid && state.playlists.has(pid)) {
      const btn = document.querySelector(`#view [data-action="share-open"][data-playlist-id="${pid}"]`)
        || document.querySelector(`#view [data-menu="playlist"]`) || ctx.list;
      openShare(btn, { kind: 'playlist', playlistId: pid });
      return true;
    }
    if (ctx.row && ctx.videoId) {
      openShare(ctx.row, { kind: 'track', videoId: ctx.videoId, trackId: ctx.trackId, playlistId: ctx.playlistId });
      return true;
    }
    return false;
  } },
]);
