// The player (docs/phase1b-shell.md §6). One YT.Player in the video tile. Anonymous visitors can
// play everything; nothing here ever asks for sign-in.
//
//   playQueue(items, index, { source })   items from queue.js; source = { playlistId, title, kind }
//   toggle()  next()  prev()  seek(seconds)  jumpToEnd(lead = 10)  previewSeam(bIndex)
//   current() -> the item playing (or null)
//   renderBar()                           draws the player bar (main.js calls it on store changes)
//   toggleRepeat()  toggleSmallVideo()    ported from the classic UI at cutover: repeat one track,
//                                         and a small, low-data video tile (remembered per browser)
//   play()  pause()                       idempotent, for lock-screen and headphone buttons
//   toggleKeepAwake()  keepAwake()        screen wake lock while playing (§12.1, wake-lock.js)
//   resumeSaved()  dismissResume()        "Resume <title> at 2:31" (§12.3, resume.js)

import { html, render, nothing, ref } from '../lit.js';
import { state, update, loadPlaylist, rowsOf } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { toast } from '../ui/toast.js';
import { trackUrl, playlistUrl, mmss, iconHref } from '../format.js';
import { itemsFromRows } from './queue.js';
import { createWakeLock } from './wake-lock.js';
import * as ms from './media-session.js';
import {
  snapshot, readResume, writeResume, clearResume, resumeAt, resumeKey, findInQueue, itemFromRecord,
} from './resume.js';
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
let repeatOne = false;
let smallVideo = readPref('webjam.smallVideo');

const nav = typeof navigator !== 'undefined' ? navigator : null;
const RESUME_EVERY_MS = 5000;
let lastSaved = 0;
let offer = null; // { key, record }: the saved spot offered on load, until played or dismissed
let offerBar = false; // the bar is showing only because of the offer
let awayAt = null; // seconds: the OS paused us while the tab was hidden; offer one tap back
let playingOnHide = false;

const wake = createWakeLock({ nav, doc: typeof document !== 'undefined' ? document : null, storage: safeStorage(), touch: isTouch() });

function safeStorage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null; } catch { return null; }
}

function isTouch() {
  try { return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches; } catch { return false; }
}

function readPref(key) {
  try { return localStorage.getItem(key) === '1'; } catch { return false; }
}

function writePref(key, on) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch { /* private mode */ }
}

function applySmallVideo() {
  document.body.classList.toggle('video-small', smallVideo);
  if (yt && smallVideo) {
    try { yt.setPlaybackQuality('small'); } catch { /* not supported */ }
  }
}

export function isRepeating() {
  return repeatOne;
}

export function isSmallVideo() {
  return smallVideo;
}

export function toggleRepeat() {
  repeatOne = !repeatOne;
  update(null, 'player');
  toast.show(repeatOne ? 'Repeating this track' : 'Repeat off');
}

/** Below 1100px the video floats over the page. YouTube needs a visible player while playing, so
 *  hiding the video pauses, and playing again brings it back (docs/phase1b-shell.md §1.3). */
export function isVideoCollapsed() {
  return document.body.classList.contains('video-collapsed');
}

export function toggleVideo() {
  if (isVideoCollapsed()) {
    setBody('video-collapsed', false);
  } else {
    try { yt && yt.pauseVideo(); } catch { /* ignore */ }
    setBody('video-collapsed', true);
  }
  update(null, 'player');
}

export function toggleSmallVideo() {
  smallVideo = !smallVideo;
  writePref('webjam.smallVideo', smallVideo);
  applySmallVideo();
  update(null, 'player');
}

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
  setBody('has-video', true);
  ready = loadYouTube().then((YT) => new Promise((resolve) => {
    yt = new YT.Player('yt-player', {
      width: '100%',
      height: '100%',
      playerVars: { playsinline: 1, rel: 0, modestbranding: 1, origin: location.origin },
      events: {
        onReady: () => {
          applySmallVideo();
          resolve(yt);
        },
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
  if (state.player.status === 'playing' && now - lastSaved >= RESUME_EVERY_MS) {
    state.player.time = t;
    saveResume();
    mediaPosition();
  }
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

// --- resume where you left off (§12.3) -----------------------------------------------------------

function userId() {
  return state.auth && state.auth.user ? state.auth.user.id : null;
}

function saveResume() {
  const p = state.player;
  const item = current();
  const store = safeStorage();
  if (!item || !store || p.seam || (p.status !== 'playing' && p.status !== 'paused')) return;
  let t = p.time;
  try { if (yt) t = yt.getCurrentTime() || t; } catch { /* keep */ }
  const rec = snapshot({ item, index: p.index, source: p.source, time: t, duration: p.duration, userId: userId() });
  lastSaved = performance.now();
  if (rec) writeResume(store, userId(), rec);
}

function forgetResume() {
  const store = safeStorage();
  if (store) clearResume(store, userId());
}

/** The saved spot for whoever is here now (re-read when the person changes). */
function currentOffer() {
  if (!state.auth || !state.auth.mode) return null; // bootstrap not back yet
  const key = resumeKey(userId());
  if (!offer || offer.key !== key) {
    const store = safeStorage();
    offer = { key, record: store ? readResume(store, userId()) : null };
  }
  return offer.record;
}

/** Tap "Resume …": rebuild the queue (the playlist when it still loads), then play from the spot. */
export async function resumeSaved() {
  if (current() && awayAt != null) {
    const at = awayAt;
    awayAt = null;
    if (!yt || state.player.status === 'idle' || state.player.status === 'ended') return loadCurrent({ startAt: at });
    try {
      if (Math.abs((yt.getCurrentTime() || 0) - at) > 2) yt.seekTo(at, true);
      yt.playVideo();
    } catch { /* ignore */ }
    update(null, 'player');
    return undefined;
  }
  const r = currentOffer();
  if (!r) return undefined;
  offer = { key: offer.key, record: null };
  let items = null;
  let index = 0;
  let source = null;
  const src = r.src;
  if (src && (src.kind !== 'crate' || Number(state.auth.crateId) === src.playlistId)) {
    try {
      await loadPlaylist(src.playlistId);
      const pl = state.playlists.get(src.playlistId);
      source = { playlistId: src.playlistId, title: pl.kind === 'crate' ? 'Crate' : pl.meta.title, kind: pl.kind };
      const list = itemsFromRows(rowsOf(src.playlistId), source);
      const i = findInQueue(r, list);
      if (i >= 0) { items = list; index = i; }
    } catch { /* gone or private now: play just the track */ }
  }
  if (!items) {
    items = [itemFromRecord(r)];
    index = 0;
    source = null;
  }
  return playQueue(items, index, { source, startAt: resumeAt(r) });
}

export function dismissResume() {
  awayAt = null;
  if (!current()) {
    forgetResume();
    if (offer) offer = { key: offer.key, record: null };
  }
  update(null, 'player');
}

function onVisibility() {
  wake.visibility();
  if (document.visibilityState === 'hidden') {
    playingOnHide = state.player.status === 'playing';
    saveResume();
    return;
  }
  if (!playingOnHide) return;
  playingOnHide = false;
  // Give YouTube a moment to report the pause the OS made while we were away.
  setTimeout(() => {
    if (document.visibilityState === 'hidden' || !current()) return;
    if (state.player.status === 'paused') {
      awayAt = state.player.time;
      update(null, 'player');
    }
  }, 800);
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('visibilitychange', onVisibility);
}
if (typeof addEventListener === 'function') addEventListener('pagehide', () => saveResume());

// --- keep the screen awake (§12.1) --------------------------------------------------------------

export function keepAwakeSupported() {
  return wake.supported;
}

export function keepAwake() {
  return wake.supported && wake.enabled();
}

export function toggleKeepAwake() {
  if (!wake.supported) return;
  wake.setEnabled(!wake.enabled());
  update(null, 'player');
  toast.show(wake.enabled() ? 'Screen stays awake while playing' : 'Screen can sleep');
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
    setBody('video-collapsed', false);
    awayAt = null;
    lastSaved = performance.now();
    startMonitor();
    wake.setPlaying(true);
    ms.setPlaybackState(nav, 'playing');
    mediaPosition();
  } else if (code === YT_PAUSED) {
    stopMonitor();
    update((s) => { s.player.status = 'paused'; }, 'player');
    setBody('player-paused', true);
    wake.setPlaying(false);
    ms.setPlaybackState(nav, 'paused');
    mediaPosition();
    saveResume();
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
    wake.setPlaying(false);
    toast.show('Unavailable on YouTube; skipped');
    setTimeout(() => {
      if (token === loadToken) next({ auto: true });
    }, 1500);
  }
}

function handleEnd() {
  const item = current();
  if (item) countPlay(item);
  if (repeatOne && item && yt) {
    endedToken = -1;
    try {
      yt.seekTo(item.start || 0, true);
      yt.playVideo();
    } catch { /* ignore */ }
    return;
  }
  const p = state.player;
  if (p.index < p.queue.length - 1) {
    next({ auto: true });
  } else {
    stopMonitor();
    update((s) => { s.player.status = 'ended'; s.player.seam = null; }, 'player');
    setBody('player-paused', true);
    wake.setPlaying(false);
    ms.setPlaybackState(nav, 'paused');
    forgetResume(); // finished the list: nothing to come back to
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

async function loadCurrent({ tail = null, startAt = null } = {}) {
  const item = current();
  if (!item) return;
  const token = ++loadToken;
  playedMs = 0;
  pendingTail = tail;
  awayAt = null;
  const known = getDuration(item.video_id);
  let from = item.start || 0;
  if (startAt != null && Number.isFinite(Number(startAt))) {
    from = Math.max(from, Number(startAt));
    if (item.end != null) from = Math.min(from, Math.max(item.start || 0, item.end - 1));
  }
  update((s) => {
    s.player.status = 'loading';
    s.player.time = from;
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
  let startSeconds = from;
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

export function playQueue(items, index = 0, { source = null, startAt = null } = {}) {
  const list = (items || []).filter((it) => it && it.video_id);
  if (!list.length) return Promise.resolve();
  const i = Math.max(0, Math.min(list.length - 1, index));
  update((s) => {
    s.player.queue = list;
    s.player.index = i;
    s.player.source = source;
    s.player.seam = null;
  }, 'player');
  return loadCurrent({ startAt });
}

export function toggle() {
  const p = state.player;
  if (!current()) return;
  if (!yt || p.status === 'idle' || p.status === 'ended') return loadCurrent();
  if (p.status === 'playing') yt.pauseVideo();
  else yt.playVideo();
}

/** Idempotent play / pause, for the lock screen and headphones (toggle() would flip twice). */
export function play() {
  const s = state.player.status;
  if (s !== 'playing' && s !== 'loading') toggle();
}

export function pause() {
  if (yt && state.player.status === 'playing') {
    try { yt.pauseVideo(); } catch { /* ignore */ }
  }
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
  mediaPosition();
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

// --- Media Session (§12.2, media-session.js) ----------------------------------------------------

function mediaMeta(item) {
  const src = state.player.source;
  const pl = src && src.playlistId ? state.playlists.get(Number(src.playlistId)) : null;
  const curator = pl && pl.meta && src.kind !== 'crate' ? pl.meta.username || '' : '';
  ms.setMetadata(nav, typeof MediaMetadata === 'function' ? MediaMetadata : null, ms.metadataFor(item, src, curator));
  ms.setPosition(nav, null);
}

function mediaPosition() {
  ms.setPosition(nav, ms.positionFor(current(), state.player.time, state.player.duration));
}

let mediaWired = false;
function wireMediaSession() {
  if (mediaWired) return;
  mediaWired = true;
  ms.wireActions(nav, {
    play: () => play(),
    pause: () => pause(),
    prev: () => prev(),
    next: () => next(),
    seekTo: (clipSeconds) => {
      const item = current();
      if (item) seek((item.start || 0) + clipSeconds);
    },
    seekBy: (delta) => seek(state.player.time + delta),
  });
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
    renderOffer(bar);
    return;
  }
  offerBar = false;
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
      ${awayAt != null
        ? html`<button type="button" class="pbar-resume" data-action="player-resume">Resume at
            <span class="pbar-time">${mmss(awayAt - start)}</span></button>`
        : html`<div class="s">${item.channel}${src && src.playlistId && src.kind !== 'crate' && src.title
        ? html`<a class="hop" href=${playlistUrl(src.playlistId, src.title)}>from ‘${src.title}’</a>`
        : src && src.kind === 'crate' ? html`<a class="hop" href="/crate">from your crate</a>` : nothing}</div>`}
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
      <button type="button" class="icon-btn" data-action="player-crate" aria-label="Add this track to your crate"
        title="Add to crate"><svg class="i"><use href="#i-crate"></use></svg></button>
      <button type="button" class="icon-btn ${repeatOne ? 'is-on' : ''}" data-action="player-repeat"
        aria-pressed=${repeatOne ? 'true' : 'false'} aria-label="Repeat this track" title="Repeat this track">
        <svg class="i"><use href="#i-repeat"></use></svg></button>
      <button type="button" class="icon-btn ${smallVideo ? 'is-on' : ''}" data-action="player-small-video"
        aria-pressed=${smallVideo ? 'true' : 'false'} aria-label="Small video, low data" title="Small video, low data">
        <svg class="i"><use href="#i-small-video"></use></svg></button>
      ${wake.supported && isTouch()
        ? html`<button type="button" class="icon-btn ${keepAwake() ? 'is-on' : ''}" data-action="player-keep-awake"
            aria-pressed=${keepAwake() ? 'true' : 'false'} aria-label="Keep the screen awake while playing"
            title="Keep the screen awake while playing"><svg class="i"><use href="#i-awake"></use></svg></button>`
        : nothing}
      <button type="button" class="icon-btn" data-action="player-seam" aria-label="Preview the seam into the next track"
        title="Preview the seam (N)" ?disabled=${p.index >= p.queue.length - 1}>
        <svg class="i"><use href="#i-seam"></use></svg></button>
      <button type="button" class="icon-btn pbar-video ${isVideoCollapsed() ? 'is-on' : ''}" data-action="player-video"
        aria-pressed=${isVideoCollapsed() ? 'true' : 'false'}
        aria-label=${isVideoCollapsed() ? 'Show the video' : 'Hide the video (pauses)'}
        title=${isVideoCollapsed() ? 'Show the video' : 'Hide the video (pauses)'}>
        <svg class="i"><use href="#i-video-off"></use></svg></button>
      ${slots.render('player.like', { item })}
    </div>
    <button type="button" class="icon-btn pbar-more" data-action="player-more" aria-label="More player controls"
      aria-haspopup="menu" aria-expanded="false"><svg class="i"><use href="#i-more"></use></svg></button>
  </div>`, bar);
}

/** Nothing playing: a quiet "Resume <title> at 2:31" if there's a saved spot (never autoplays). */
function renderOffer(bar) {
  const r = currentOffer();
  if (!r) {
    render(nothing, bar);
    if (offerBar) {
      offerBar = false;
      bar.hidden = true;
      setBody('has-player', false);
    }
    return;
  }
  if (bar.hidden) {
    offerBar = true;
    showBar();
  }
  const at = mmss(resumeAt(r) - (r.start || 0));
  render(html`<div class="pbar pbar-offer">
    <button type="button" class="icon-btn play" data-action="player-resume" aria-label=${`Resume ${r.title} at ${at}`}>
      <svg class="i"><use href="#i-play"></use></svg></button>
    <button type="button" class="pbar-resume" data-action="player-resume">
      <span class="pbar-resume-label">Resume</span> <span class="t">${r.title}</span>
      <span class="pbar-resume-label">at</span> <span class="pbar-time">${at}</span></button>
    <button type="button" class="icon-btn" data-action="player-resume-dismiss" aria-label="Not now" title="Not now">
      <svg class="i"><use href="#i-close"></use></svg></button>
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
