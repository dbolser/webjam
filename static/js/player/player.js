// The player (docs/phase1b-shell.md §6). One YT.Player in the video tile. Anonymous visitors can
// play everything; nothing here ever asks for sign-in.
//
//   playQueue(items, index, { source })   items from queue.js; source = { playlistId, title, kind }
//   toggle()  next()  prev()  seek(seconds)  jumpToEnd(lead = 10)  previewSeam(bIndex)
//   current() -> the item playing (or null)
//   renderBar()                           draws the player bar (main.js calls it on store changes)

import { html, render, nothing, ref } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { toast } from '../ui/toast.js';
import { trackUrl, playlistUrl, mmss, iconHref } from '../format.js';
import { loadYouTube } from './youtube.js';
import { getDuration, setDuration } from './durations.js';
import { SEAM_LEAD, tailStart, seamProgress } from './seam.js';

const YT_PLAYING = 1;
const YT_PAUSED = 2;
const YT_ENDED = 0;
const DEAD_CODES = new Set([100, 101, 150, 2, 5]);

let yt = null;
let ready = null;
let monitor = null;
let loadToken = 0;
let endedToken = -1;
let pendingTail = null; // lead seconds: seek to (end ?? duration) - lead once the duration is known
let playedMs = 0;
let lastTick = 0;
const counted = new Set();

export function current() {
  const p = state.player;
  return p.index >= 0 ? p.queue[p.index] || null : null;
}

function setBody(cls, on) {
  document.body.classList.toggle(cls, !!on);
}

function ensurePlayer() {
  if (ready) return ready;
  const tile = document.getElementById('video-tile');
  if (tile) tile.hidden = false;
  ready = loadYouTube().then((YT) => new Promise((resolve) => {
    yt = new YT.Player('yt-player', {
      width: '100%',
      height: '100%',
      playerVars: { playsinline: 1, rel: 0, modestbranding: 1, origin: location.origin },
      events: {
        onReady: () => resolve(yt),
        onStateChange: (e) => onState(e.data),
        onError: (e) => onError(e.data),
      },
    });
  })).catch((err) => {
    ready = null;
    toast.show(err.message || 'The YouTube player could not load.');
    update((s) => { s.player.status = 'idle'; }, 'player');
    throw err;
  });
  return ready;
}

function tick() {
  const now = performance.now();
  const item = current();
  if (!yt || !item) return;
  let t = 0;
  try { t = yt.getCurrentTime() || 0; } catch { t = 0; }
  if (state.player.status === 'playing') playedMs += lastTick ? now - lastTick : 0;
  lastTick = now;
  state.player.time = t;
  if (playedMs >= 30000) countPlay(item);
  const seam = state.player.seam;
  if (seam && seam.phase === 'b' && t - (seam.bFrom || 0) >= SEAM_LEAD) {
    update((s) => { s.player.seam = null; }, 'player');
  }
  if (item.end != null && t >= item.end - 0.25 && endedToken !== loadToken) {
    endedToken = loadToken;
    handleEnd();
    return;
  }
  update(null, 'tick');
}

function startMonitor() {
  stopMonitor();
  lastTick = performance.now();
  monitor = setInterval(tick, 250);
}

function stopMonitor() {
  if (monitor) clearInterval(monitor);
  monitor = null;
  lastTick = 0;
}

function onState(code) {
  const item = current();
  if (code === YT_PLAYING) {
    let d = 0;
    try { d = yt.getDuration() || 0; } catch { d = 0; }
    if (item && d > 0 && setDuration(item.video_id, d)) update(null, 'durations');
    if (pendingTail != null && item && d > 0) {
      const at = tailStart(item, d, pendingTail);
      pendingTail = null;
      if (at != null && at > (yt.getCurrentTime() || 0) + 1) yt.seekTo(at, true);
    }
    update((s) => {
      s.player.status = 'playing';
      s.player.duration = d || s.player.duration;
    }, 'player');
    setBody('player-paused', false);
    startMonitor();
    mediaState('playing');
  } else if (code === YT_PAUSED) {
    stopMonitor();
    update((s) => { s.player.status = 'paused'; }, 'player');
    setBody('player-paused', true);
    mediaState('paused');
  } else if (code === YT_ENDED) {
    stopMonitor();
    if (endedToken !== loadToken) {
      endedToken = loadToken;
      handleEnd();
    }
  }
}

function onError(code) {
  const item = current();
  if (!item) return;
  if (DEAD_CODES.has(Number(code))) {
    const token = loadToken;
    update((s) => { s.dead.add(item.video_id); s.player.status = 'paused'; }, 'player');
    toast.show('Unavailable on YouTube; skipped');
    setTimeout(() => {
      if (token === loadToken) next({ auto: true });
    }, 1500);
  }
}

function handleEnd() {
  const item = current();
  if (item) countPlay(item);
  const p = state.player;
  if (p.index < p.queue.length - 1) {
    next({ auto: true });
  } else {
    stopMonitor();
    update((s) => { s.player.status = 'ended'; s.player.seam = null; }, 'player');
    setBody('player-paused', true);
    try { yt && yt.pauseVideo(); } catch { /* ignore */ }
  }
}

function countPlay(item) {
  const src = state.player.source;
  if (!item || !item.playlistId || !item.trackId) return;
  if (!src || src.kind === 'crate' || state.player.queue.length < 2) return;
  const key = `${item.playlistId}:${item.trackId}`;
  if (counted.has(key)) return;
  counted.add(key);
  api.post(`/api/playlists/${Number(item.playlistId)}/play`, { video_id: item.video_id }, { quiet: true }).catch(() => {});
}

async function loadCurrent({ tail = null } = {}) {
  const item = current();
  if (!item) return;
  const token = ++loadToken;
  playedMs = 0;
  pendingTail = tail;
  const known = getDuration(item.video_id);
  update((s) => {
    s.player.status = 'loading';
    s.player.time = item.start || 0;
    s.player.duration = known || 0;
  }, 'player');
  showBar();
  setBody('player-paused', false);
  mediaMeta(item);
  let p;
  try {
    p = await ensurePlayer();
  } catch {
    return;
  }
  if (token !== loadToken) return;
  let startSeconds = item.start || 0;
  if (tail != null) {
    const at = tailStart(item, known, tail);
    if (at != null) {
      startSeconds = at;
      pendingTail = null;
    }
  }
  const opts = { videoId: item.video_id, startSeconds };
  if (item.end != null) opts.endSeconds = item.end;
  p.loadVideoById(opts);
}

function showBar() {
  const bar = document.getElementById('player-bar');
  if (bar && bar.hidden) {
    bar.hidden = false;
    setBody('has-player', true);
  }
}

// --- public API ---------------------------------------------------------------------------------

export function playQueue(items, index = 0, { source = null } = {}) {
  const list = (items || []).filter((it) => it && it.video_id);
  if (!list.length) return Promise.resolve();
  const i = Math.max(0, Math.min(list.length - 1, index));
  update((s) => {
    s.player.queue = list;
    s.player.index = i;
    s.player.source = source;
    s.player.seam = null;
  }, 'player');
  return loadCurrent();
}

export function toggle() {
  const p = state.player;
  if (!current()) return;
  if (!yt || p.status === 'idle' || p.status === 'ended') return loadCurrent();
  if (p.status === 'playing') yt.pauseVideo();
  else yt.playVideo();
}

export function next({ auto = false } = {}) {
  const p = state.player;
  if (p.index >= p.queue.length - 1) return;
  const seam = p.seam;
  update((s) => {
    s.player.index += 1;
    if (auto && seam && seam.phase === 'a' && s.player.index === seam.bIndex) {
      const it = s.player.queue[s.player.index];
      s.player.seam = { ...seam, phase: 'b', bFrom: (it && it.start) || 0 };
    } else {
      s.player.seam = null;
    }
  }, 'player');
  loadCurrent();
}

export function prev() {
  const p = state.player;
  const item = current();
  if (!item) return;
  if (p.time - (item.start || 0) > 3 || p.index === 0) {
    seek(item.start || 0);
    return;
  }
  update((s) => {
    s.player.index -= 1;
    s.player.seam = null;
  }, 'player');
  loadCurrent();
}

export function seek(seconds) {
  const item = current();
  if (!yt || !item) return;
  let t = Math.max(item.start || 0, Number(seconds) || 0);
  if (item.end != null) t = Math.min(t, item.end - 0.5);
  yt.seekTo(t, true);
  state.player.time = t;
  endedToken = -1;
  update(null, 'tick');
}

/** Seek to 10 s before the end (clip end if set). */
export function jumpToEnd(lead = SEAM_LEAD) {
  const item = current();
  if (!item) return;
  if (!yt || state.player.status === 'loading') {
    pendingTail = lead;
    return;
  }
  let d = state.player.duration;
  try { d = yt.getDuration() || d; } catch { /* keep */ }
  const at = tailStart(item, d || null, lead);
  if (at == null) pendingTail = lead;
  else seek(at);
}

/** Play the seam INTO queue[bIndex]: queue[bIndex-1]'s last 10 s, then queue[bIndex].
 *  With { items, source } the queue is replaced first (B / N on a list that isn't playing). */
export function previewSeam(bIndex, { items = null, source = null } = {}) {
  const queue = items ? items.filter((it) => it && it.video_id) : state.player.queue;
  const b = Number(bIndex);
  if (!(b >= 1 && b < queue.length)) {
    toast.show(b >= 1 ? 'Nothing after this track.' : 'Nothing before this track.');
    return;
  }
  update((s) => {
    if (items) {
      s.player.queue = queue;
      s.player.source = source;
    }
    s.player.index = b - 1;
    s.player.seam = { bIndex: b, phase: 'a', bFrom: 0 };
  }, 'player');
  loadCurrent({ tail: SEAM_LEAD });
}

// --- Media Session ----------------------------------------------------------------------------

function mediaMeta(item) {
  if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title || '',
      artist: item.channel || '',
      album: (state.player.source && state.player.source.title) || 'webjam',
      artwork: item.thumb ? [{ src: item.thumb, sizes: '480x360', type: 'image/jpeg' }] : [],
    });
  } catch {
    /* ignore */
  }
}

function mediaState(s) {
  if ('mediaSession' in navigator) {
    try { navigator.mediaSession.playbackState = s; } catch { /* ignore */ }
  }
}

let mediaWired = false;
function wireMediaSession() {
  if (mediaWired || !('mediaSession' in navigator)) return;
  mediaWired = true;
  const set = (name, fn) => {
    try { navigator.mediaSession.setActionHandler(name, fn); } catch { /* unsupported action */ }
  };
  set('play', () => toggle());
  set('pause', () => toggle());
  set('previoustrack', () => prev());
  set('nexttrack', () => next());
  set('seekto', (d) => d && d.seekTime != null && seek(d.seekTime));
}

// --- the bar ------------------------------------------------------------------------------------

function bounds(item) {
  const start = item.start || 0;
  const end = item.end != null ? item.end : state.player.duration || 0;
  return { start, end };
}

function setProgress(el, item) {
  if (!el || !item) return;
  const { start, end } = bounds(item);
  const span = Math.max(1, end - start);
  const t = state.player.time;
  const p = Math.max(0, Math.min(1, (t - start) / span));
  if (document.activeElement !== el) el.value = String(Math.round(p * 1000));
  el.style.setProperty('--p', String(p));
}

function setSeam(el, item) {
  if (!el) return;
  const p = seamProgress(state.player.seam, state.player.time, item, state.player.duration);
  el.style.setProperty('--p', String(p));
}

export function renderBar() {
  const bar = document.getElementById('player-bar');
  if (!bar) return;
  wireMediaSession();
  const item = current();
  if (!item) {
    render(nothing, bar);
    return;
  }
  const p = state.player;
  const src = p.source;
  const playing = p.status === 'playing' || p.status === 'loading';
  const { start, end } = bounds(item);
  const elapsed = Math.max(0, p.time - start);
  const total = end > start ? end - start : 0;
  render(html`<div class="pbar">
    <div class="pbar-controls">
      <button type="button" class="icon-btn" data-action="player-prev" aria-label="Previous">
        <svg class="i"><use href="#i-prev"></use></svg></button>
      <button type="button" class="icon-btn play" data-action="player-toggle" aria-label=${playing ? 'Pause' : 'Play'}>
        <svg class="i"><use href=${iconHref(playing ? 'pause' : 'play')}></use></svg></button>
      <button type="button" class="icon-btn" data-action="player-next" aria-label="Next"
        ?disabled=${p.index >= p.queue.length - 1}>
        <svg class="i"><use href="#i-next"></use></svg></button>
    </div>
    <div class="pbar-now">
      <a class="t" href=${trackUrl(item.video_id, item.start, item.end)}>${item.title}</a>
      <div class="s">${item.channel}${src && src.playlistId && src.kind !== 'crate' && src.title
        ? html`<a class="hop" href=${playlistUrl(src.playlistId, src.title)}>from ‘${src.title}’</a>`
        : src && src.kind === 'crate' ? html`<a class="hop" href="/crate">from your crate</a>` : nothing}</div>
    </div>
    <div class="pbar-progress">
      <span class="pbar-time">${mmss(elapsed)}</span>
      <input class="range" type="range" min="0" max="1000" step="1" aria-label="Seek"
        data-action="player-seek" ${ref((el) => setProgress(el, item))} />
      <span class="pbar-time">${total ? mmss(total) : '–:––'}</span>
      ${p.seam
        ? html`<span class="seam" role="img" aria-label="Previewing the seam" title="Previewing the seam"
            ${ref((el) => setSeam(el, item))}><span>A</span><span>B</span></span>`
        : nothing}
    </div>
    <div class="pbar-extra">
      <button type="button" class="icon-btn" data-action="player-seam" aria-label="Preview the seam into the next track"
        title="Preview the seam (N)" ?disabled=${p.index >= p.queue.length - 1}>
        <svg class="i"><use href="#i-seam"></use></svg></button>
      ${slots.render('player.like', { item })}
    </div>
  </div>`, bar);
}

/** Seek from the range input (0–1000 across the clip). */
export function seekFraction(f) {
  const item = current();
  if (!item) return;
  const { start, end } = bounds(item);
  if (end <= start) return;
  seek(start + (end - start) * Math.max(0, Math.min(1, f)));
}
