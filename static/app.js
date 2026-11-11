function readAudioOnlyModePreference() {
  try {
    return localStorage.getItem('webjam_audio_only_mode') === '1';
  } catch (_) {
    return false;
  }
}

// Webjam Frontend Application
const state = {
  currentUserId: Number(localStorage.getItem('webjam_user_id')) || 1,
  users: [],
  currentUser: null,
  auth: { mode: 'demo', signed_in: false },
  badgeCatalog: [],
  feedData: null,
  currentRoute: { view: 'feed', param: null },
  // Player & Queue state
  ytPlayer: null,
  ytReady: false,
  pendingVideoLoad: null,
  clipMonitorTimer: null,
  clipAdvanceLock: false,
  queue: [], // Array of track objects
  queueIndex: -1,
  queuePlaylistId: null,
  queuePlaylistKind: 'playlist',
  queuePlaylistTitle: '',
  queuePlaylistOwner: '',
  queuePlaylistLiked: false,
  queuePlaylistMyLikes: 0,
  queuePlaylistLikeCount: 0,
  repeatTrack: false,
  audioOnlyMode: readAudioOnlyModePreference(),
  superloveSyncTimer: null,
  // Drag-and-Drop state (undo lives on the server: state.currentUser.undo)
  dragState: {
    playlistId: null,
    draggedTrackId: null,
    targetTrackId: null,
    insertBefore: true,
  },
  // Embed audit & Import Wizard state
  embedAuditByVideoId: {},
  importWizard: null,
};

// --- Utilities ---
function esc(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Encode a value as a JS literal that is safe to drop into an inline on*="..."
// handler attribute. JSON.stringify produces a valid JS expression (quoting
// and backslash-escaping strings); esc() then HTML-attribute-escapes it so the
// HTML parser decodes it back to exactly that literal before the JS runs.
// Never put esc() output inside '...' in a handler: the browser decodes &#39;
// back to ' before the JS engine sees it.
function jsArg(value) {
  return esc(JSON.stringify(value === undefined ? null : value));
}

// Number-or-null for optional numeric fields (e.g. clip start/end seconds).
function numOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// Only allow #rgb / #rrggbb colours from data into style attributes.
function safeColor(value, fallback = '#8a8a8a') {
  const s = String(value ?? '').trim();
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(s) ? s : fallback;
}

function shortDate(iso) {
  if (!iso) return '';
  return String(iso).slice(0, 10);
}

function formatTimestamp(sec) {
  if (sec === null || sec === undefined || sec === '' || Number.isNaN(Number(sec))) return '';
  const total = Math.max(0, Math.floor(Number(sec)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  return `${m}:${String(s).padStart(2, '0')}`;
}

function formatClipRange(startSec, endSec) {
  const hasStart = startSec !== null && startSec !== undefined && startSec !== '';
  const hasEnd = endSec !== null && endSec !== undefined && endSec !== '';
  if (!hasStart && !hasEnd) return '';
  const startLabel = hasStart ? formatTimestamp(startSec) : '0:00';
  const endLabel = hasEnd ? formatTimestamp(endSec) : 'end';
  return `${startLabel} → ${endLabel}`;
}

function getLoveBtnClasses(myLikes) {
  const n = Number(myLikes || 0);
  if (n >= 5) return 'liked superloved';
  if (n >= 1) return 'liked';
  return '';
}

function getLoveBtnTitle(myLikes, itemLabel = 'track') {
  const n = Number(myLikes || 0);
  if (n >= 5) {
    return `Superloved ×${n} by you! Smack again for more love (Shift+Click to reset)`;
  }
  if (n >= 1) {
    return `Loved ×${n} by you — keep smacking to Superlove! (Shift+Click to reset)`;
  }
  return `Smack to love this ${itemLabel}! Click as many times as you like`;
}

function formatLoveBtnHtml(myLikes, totalLikes, mode = 'compact') {
  const my = Number(myLikes || 0);
  const total = Number(totalLikes || 0);
  const icon = my >= 5 ? '💖' : my >= 1 ? '♥' : '♡';

  if (mode === 'queue') {
    const totalPart = total > 0 ? ` ${total}` : '';
    const myPart = my > 1 ? ` ×${my}` : '';
    return `${icon}${totalPart}${myPart}`;
  }
  if (mode === 'deck-song') {
    const label =
      my >= 5
        ? `Superloved ×${my}`
        : my > 1
        ? `Loved ×${my}`
        : my === 1
        ? 'Loved'
        : 'Love Track';
    const totalPart = total > 0 ? ` (${total})` : '';
    return `${icon} ${label}${totalPart}`;
  }
  if (mode === 'deck-playlist') {
    const label =
      my >= 5
        ? `Mix ×${my}`
        : my > 1
        ? `Mix ×${my}`
        : my === 1
        ? 'Mix Loved'
        : 'Love Mix';
    const totalPart = total > 0 ? ` (${total})` : '';
    return `${icon} ${label}${totalPart}`;
  }
  if (mode === 'header-playlist') {
    const label = my >= 5 ? 'Superloved' : my >= 1 ? 'Loved' : 'Love';
    const myPill = my > 1 ? ` <span class="superlove-my-pill">×${my}</span>` : '';
    return `${icon} ${label}${myPill} (${total})`;
  }
  if (mode === 'header-song') {
    const label = my >= 5 ? 'Superloved' : my >= 1 ? 'Loved' : 'Love Song';
    const myPill = my > 1 ? ` <span class="superlove-my-pill">×${my}</span>` : '';
    return `${icon} ${label}${myPill} (${total})`;
  }
  const totalPart = total > 0 ? ` ${total}` : '';
  const myPill = my > 1 ? ` <span class="superlove-my-pill">×${my}</span>` : '';
  return `${icon}${totalPart}${myPill}`;
}

function formatCoSignersHtml(users) {
  if (!Array.isArray(users) || users.length === 0) return '';
  return users
    .map((u) => {
      const cnt = Number(u.count || 1);
      const badge =
        cnt >= 5
          ? ` <span class="superlove-my-pill" title="Superloved ×${cnt}">💖×${cnt}</span>`
          : cnt > 1
          ? ` <span class="superlove-my-pill" title="Loved ×${cnt}">×${cnt}</span>`
          : '';
      return `<a href="#user/${Number(u.id)}" onclick="navigate('user', ${Number(u.id)}); return false;">@${esc(u.username)}</a>${badge}`;
    })
    .join(', ');
}

function triggerSmackBounce(el) {
  if (!el) return;
  el.classList.remove('smack-pop');
  void el.offsetWidth;
  el.classList.add('smack-pop');
}

function spawnSuperloveParticle(event, anchorEl, myLikes, isReset = false) {
  let x = window.innerWidth / 2;
  let y = window.innerHeight / 2;

  if (event && typeof event.clientX === 'number' && event.clientX > 0) {
    x = event.clientX;
    y = event.clientY;
  } else if (anchorEl && typeof anchorEl.getBoundingClientRect === 'function') {
    const rect = anchorEl.getBoundingClientRect();
    x = rect.left + rect.width / 2;
    y = rect.top;
  }

  if (anchorEl) {
    triggerSmackBounce(anchorEl);
  }

  const particle = document.createElement('div');
  const my = Number(myLikes || 0);
  const isSuper = !isReset && my >= 5;
  particle.className = `superlove-float ${isSuper ? 'is-super' : ''}`;

  const dx = Math.round((Math.random() - 0.5) * 34);
  const rot = Math.round((Math.random() - 0.5) * 18);
  particle.style.left = `${x}px`;
  particle.style.top = `${y - 8}px`;
  particle.style.setProperty('--sl-dx', `${dx}px`);
  particle.style.setProperty('--sl-rot', `${rot}deg`);

  if (isReset) {
    particle.textContent = '💔 Reset';
  } else if (my === 10) {
    particle.textContent = '💖 SUPERLOVE ×10!';
  } else if (my === 5) {
    particle.textContent = '💖 SUPERLOVE ×5!';
  } else if (my >= 5) {
    particle.textContent = `💖 +1 (×${my}!)`;
  } else if (my > 1) {
    particle.textContent = `♥ +1 (×${my})`;
  } else {
    particle.textContent = '♥ +1';
  }

  document.body.appendChild(particle);
  setTimeout(() => particle.remove(), 800);
}

function scheduleDebouncedSuperloveSync() {
  if (state.superloveSyncTimer) {
    clearTimeout(state.superloveSyncTimer);
  }
  state.superloveSyncTimer = setTimeout(async () => {
    state.superloveSyncTimer = null;
    try {
      await refreshBootstrapSilently();
    } catch (_) {}
  }, 650);
}

function showToast(message, isBadge = false) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast ${isBadge ? 'badge-unlock' : ''}`;
  el.innerHTML = message;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.25s ease';
    setTimeout(() => el.remove(), 260);
  }, isBadge ? 5500 : 3200);
}

function notifyBadges(badges, ownerUsername = null) {
  if (!Array.isArray(badges) || badges.length === 0) return;
  for (const b of badges) {
    const who = ownerUsername ? `@${esc(ownerUsername)} unlocked` : 'Badge Unlocked!';
    showToast(
      `<div><strong>${esc(b.icon)} ${who}: ${esc(b.name)}</strong></div>` +
        `<div style="font-size:12px; margin-top:2px; opacity:0.92;">${esc(b.context_note || b.description)}</div>`,
      true
    );
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    // X-Webjam marks same-origin app requests; the backend requires it on
    // unsafe methods as a CSRF guard (custom headers force a CORS preflight).
    headers: { 'Content-Type': 'application/json', 'X-Webjam': '1', ...(options.headers || {}) },
  });
  if (!res.ok) {
    let errText = `Request failed (${res.status})`;
    let errCode = '';
    let errData = null;
    try {
      const data = await res.json();
      errData = data && typeof data === 'object' ? data : null;
      errCode = typeof data.code === 'string' ? data.code : '';
      // FastAPI validation errors (422) carry a list of {msg, ...}.
      if (Array.isArray(data.detail)) {
        errText = data.detail.map((d) => (d && d.msg) || String(d)).join('; ') || errText;
      } else if (data.detail) {
        errText = String(data.detail);
      }
    } catch (_) {}
    const err = new Error(errText);
    err.status = res.status;
    err.code = errCode;
    err.data = errData; // machine-readable extras (e.g. group_id on undo_conflict / stack_moved)
    err.retryAfter = Number(res.headers.get('Retry-After')) || 0;
    throw err;
  }
  return res.json();
}

// For loops that make many requests (auto-migrate): wait out a 429/503 and retry
// instead of silently skipping the rest.
async function apiWithRetry(path, options = {}, attempts = 4) {
  for (let i = 1; ; i++) {
    try {
      return await api(path, options);
    } catch (err) {
      if (i >= attempts || (err.status !== 429 && err.status !== 503)) throw err;
      const waitSec = Math.min(Math.max(err.retryAfter || 2, 1), 30);
      await new Promise((resolve) => setTimeout(resolve, waitSec * 1000));
    }
  }
}

// " (2 skipped: …)" for add/import results whose videos YouTube couldn't look up.
function skippedNote(skipped) {
  const list = Array.isArray(skipped) ? skipped : [];
  if (!list.length) return '';
  const unavailable = list.filter((s) => s && s.reason === 'youtube_unavailable').length;
  const missing = list.length - unavailable;
  const parts = [];
  if (missing) parts.push(`${missing} not found on YouTube`);
  if (unavailable) parts.push(`${unavailable} couldn't be checked right now, try again`);
  return ` (${list.length} skipped: ${parts.join(', ')})`;
}

// --- YouTube IFrame Player API Integration ---
window.onYouTubeIframeAPIReady = function () {
  state.ytReady = true;
  if (state.pendingVideoLoad) {
    const { videoId, startSeconds, endSeconds } = state.pendingVideoLoad;
    state.pendingVideoLoad = null;
    mountOrLoadYouTubeVideo(videoId, startSeconds, endSeconds);
  }
};

function startClipMonitor() {
  stopClipMonitor();
  state.clipMonitorTimer = setInterval(() => {
    if (!state.ytPlayer || typeof state.ytPlayer.getCurrentTime !== 'function') return;
    const track = state.queue[state.queueIndex];
    if (!track || track.end_seconds === null || track.end_seconds === undefined) return;
    const cur = state.ytPlayer.getCurrentTime();
    if (cur >= Number(track.end_seconds) && !state.clipAdvanceLock) {
      state.clipAdvanceLock = true;
      handleTrackEndedOrClipDone();
    }
  }, 350);
}

function stopClipMonitor() {
  if (state.clipMonitorTimer) {
    clearInterval(state.clipMonitorTimer);
    state.clipMonitorTimer = null;
  }
}

async function handleTrackEndedOrClipDone() {
  if (state.repeatTrack && state.queue.length > 0 && state.queueIndex >= 0) {
    const track = state.queue[state.queueIndex];
    if (track) {
      state.clipAdvanceLock = false;
      const startSec =
        track.start_seconds !== null && track.start_seconds !== undefined
          ? Number(track.start_seconds)
          : 0;
      if (state.ytPlayer && typeof state.ytPlayer.seekTo === 'function') {
        state.ytPlayer.seekTo(startSec, true);
        if (typeof state.ytPlayer.playVideo === 'function') {
          state.ytPlayer.playVideo();
        }
      } else {
        mountOrLoadYouTubeVideo(track.video_id, track.start_seconds, track.end_seconds);
      }
      if (state.queuePlaylistId) {
        try {
          const res = await api(`/api/playlists/${state.queuePlaylistId}/play`, {
            method: 'POST',
            body: JSON.stringify({ user_id: state.currentUserId, video_id: track.video_id, count: 1 }),
          });
          notifyBadges(res.new_badges, state.queuePlaylistOwner);
          await refreshBootstrapSilently();
        } catch (_) {}
      }
      return;
    }
  }
  playNextTrack();
}

function toggleRepeatTrack() {
  state.repeatTrack = !state.repeatTrack;
  const btn = document.getElementById('np-repeat-btn');
  if (btn) {
    btn.textContent = state.repeatTrack ? '🔁 Repeat: ON' : '🔁 Repeat: Off';
    btn.classList.toggle('liked', state.repeatTrack);
  }
  showToast(
    state.repeatTrack
      ? '🔁 Repeat Track ON — current track will loop endlessly!'
      : '🔁 Repeat Track Off — queue will advance normally.'
  );
}

const YT_QUALITY_LABELS = {
  tiny: '144p tiny',
  small: '240p low',
  medium: '360p',
  large: '480p',
  hd720: '720p',
  hd1080: '1080p',
  highres: 'HD+',
  default: 'auto',
};

function updateAudioModeQualityPill(qualityCode = null) {
  const pill = document.getElementById('audio-mode-quality-pill');
  if (!pill) return;
  let q = qualityCode;
  if (!q && state.ytPlayer && typeof state.ytPlayer.getPlaybackQuality === 'function') {
    try {
      q = state.ytPlayer.getPlaybackQuality();
    } catch (_) {
      q = null;
    }
  }
  const label = (q && YT_QUALITY_LABELS[q]) || 'compact · auto';
  pill.textContent = label;
}

function enforceAudioModePlayerConstraints() {
  if (!state.ytPlayer) return;
  try {
    if (typeof state.ytPlayer.setSize === 'function') {
      if (state.audioOnlyMode) {
        // YouTube's Required Minimum Functionality mandates at least 200x200 for embedded players.
        state.ytPlayer.setSize(200, 200);
      } else {
        const wrapper = document.getElementById('yt-viewport-wrapper');
        const w = wrapper && wrapper.clientWidth ? wrapper.clientWidth : 400;
        const h = Math.round((w * 9) / 16);
        state.ytPlayer.setSize(w, h);
      }
    }
  } catch (_) {}

  updateAudioModeQualityPill();
}

function applyAudioOnlyModeUI() {
  const btn = document.getElementById('audio-mode-toggle-btn');
  const wrapper = document.getElementById('yt-viewport-wrapper');
  const infoBox = document.getElementById('audio-mode-deck-info');

  if (btn) {
    btn.textContent = state.audioOnlyMode ? '🎧 Audio / Low-Data: ON' : '🎧 Audio / Low-Data: Off';
    btn.classList.toggle('active', state.audioOnlyMode);
  }
  if (wrapper) {
    wrapper.classList.toggle('audio-only-mode', state.audioOnlyMode);
  }
  if (infoBox) {
    infoBox.classList.toggle('hidden', !state.audioOnlyMode);
  }

  enforceAudioModePlayerConstraints();
}

function toggleAudioOnlyMode() {
  state.audioOnlyMode = !state.audioOnlyMode;
  let saved = true;
  try {
    localStorage.setItem('webjam_audio_only_mode', state.audioOnlyMode ? '1' : '0');
  } catch (_) {
    saved = false;
  }
  applyAudioOnlyModeUI();
  const storageNotice = saved ? '' : ' (preference not saved in browser storage)';
  showToast(
    state.audioOnlyMode
      ? `🎧 Low-Bandwidth Audio Mode ON — compact 200×200 viewport nudges YouTube adaptive streaming toward low video bitrates.${storageNotice}`
      : `🎬 Standard Video Mode restored (16:9 viewport).${storageNotice}`
  );
}

function mountOrLoadYouTubeVideo(videoId, startSeconds = null, endSeconds = null) {
  const placeholder = document.getElementById('yt-placeholder');
  if (placeholder) placeholder.classList.add('hidden');
  state.clipAdvanceLock = false;

  const hasStart = startSeconds !== null && startSeconds !== undefined && startSeconds !== '';
  const hasEnd = endSeconds !== null && endSeconds !== undefined && endSeconds !== '';

  if (!state.ytReady || !window.YT || !window.YT.Player) {
    state.pendingVideoLoad = { videoId, startSeconds, endSeconds };
    return;
  }

  if (state.ytPlayer && typeof state.ytPlayer.loadVideoById === 'function') {
    const loadSpec = { videoId };
    if (hasStart) loadSpec.startSeconds = Number(startSeconds);
    if (hasEnd) loadSpec.endSeconds = Number(endSeconds);
    state.ytPlayer.loadVideoById(loadSpec);
    enforceAudioModePlayerConstraints();
    return;
  }

  const playerVars = {
    autoplay: 1,
    rel: 0,
    modestbranding: 1,
    playsinline: 1,
    origin: window.location.origin,
  };
  if (hasStart) playerVars.start = Number(startSeconds);
  if (hasEnd) playerVars.end = Number(endSeconds);

  state.ytPlayer = new window.YT.Player('yt-player-mount', {
    width: state.audioOnlyMode ? '200' : '100%',
    height: state.audioOnlyMode ? '200' : '100%',
    videoId: videoId,
    playerVars,
    events: {
      onReady: () => {
        enforceAudioModePlayerConstraints();
      },
      onPlaybackQualityChange: (event) => {
        updateAudioModeQualityPill(event?.data);
      },
      onStateChange: (event) => {
        const dot = document.getElementById('player-status-dot');
        if (event.data === window.YT.PlayerState.PLAYING) {
          if (dot) dot.classList.add('live');
          if (state.audioOnlyMode) {
            enforceAudioModePlayerConstraints();
          }
          startClipMonitor();
        } else if (event.data === window.YT.PlayerState.ENDED) {
          if (dot) dot.classList.remove('live');
          stopClipMonitor();
          if (!state.clipAdvanceLock) {
            state.clipAdvanceLock = true;
            handleTrackEndedOrClipDone();
          }
        } else {
          stopClipMonitor();
        }
      },
      onError: (event) => {
        stopClipMonitor();
        showToast(
          `YouTube embed notice (code ${Number(event.data)}) — creator restricts external embedding or video unavailable. Advancing queue…`
        );
        setTimeout(() => {
          if (state.queue.length > 1 && state.queueIndex < state.queue.length - 1) {
            playNextTrack();
          }
        }, 1800);
      },
    },
  });
}

// --- Queue & Playback Controller ---
async function playPlaylistFromIndex(playlist, startIndex = 0) {
  if (!playlist || !Array.isArray(playlist.tracks) || playlist.tracks.length === 0) {
    showToast('This playlist has no tracks yet! Drop a YouTube link first.');
    return;
  }
  state.queue = playlist.tracks.map((t) => ({
    ...t,
    playlist_id: playlist.id,
    playlist_title: playlist.title,
    playlist_owner_id: playlist.user_id,
    playlist_owner_username: playlist.username,
    song_like_count: Number(t.song_like_count ?? 0),
    song_my_likes: Number(t.song_my_likes ?? (t.song_liked_by_me ? 1 : 0)),
    song_liked_by_me: Boolean(t.song_liked_by_me),
  }));
  state.queuePlaylistId = playlist.id;
  state.queuePlaylistKind = playlist.kind || 'playlist';
  state.queuePlaylistTitle = playlist.title;
  state.queuePlaylistOwner = playlist.username || '';
  state.queuePlaylistLiked = Boolean(playlist.liked_by_me);
  state.queuePlaylistMyLikes = Number(playlist.my_likes ?? (playlist.liked_by_me ? 1 : 0));
  state.queuePlaylistLikeCount = Number(playlist.like_count ?? 0);
  await playQueueIndex(startIndex, true);
}

async function playSingleSong(songObj, sourcePlaylistId = null, sourceUserId = null) {
  const myLikes = Number(songObj.song_my_likes ?? songObj.my_likes ?? (songObj.song_liked_by_me || songObj.liked_by_me ? 1 : 0));
  state.queue = [
    {
      video_id: songObj.video_id,
      title: songObj.title,
      channel_title: songObj.channel_title || 'YouTube',
      track_note: songObj.track_note || '',
      start_seconds: songObj.start_seconds ?? null,
      end_seconds: songObj.end_seconds ?? null,
      playlist_id: sourcePlaylistId,
      playlist_owner_id: sourceUserId,
      song_like_count: Number(songObj.song_like_count ?? songObj.like_count ?? 0),
      song_my_likes: myLikes,
      song_liked_by_me: myLikes > 0,
    },
  ];
  state.queuePlaylistId = sourcePlaylistId;
  state.queuePlaylistKind = (sourcePlaylistId && playlistCache[sourcePlaylistId] && playlistCache[sourcePlaylistId].kind) || 'playlist';
  state.queuePlaylistTitle = songObj.playlist_title || 'Single Track Spin';
  state.queuePlaylistOwner = songObj.username || songObj.finder_username || '';
  state.queuePlaylistLiked = false;
  state.queuePlaylistMyLikes = 0;
  state.queuePlaylistLikeCount = 0;
  await playQueueIndex(0, Boolean(sourcePlaylistId));
}

async function playQueueIndex(idx, recordPlaylistSpin = true) {
  if (idx < 0 || idx >= state.queue.length) return;
  state.queueIndex = idx;
  const track = state.queue[idx];

  mountOrLoadYouTubeVideo(track.video_id, track.start_seconds, track.end_seconds);
  updatePlayerDeckUI();
  highlightActiveTrackRows();

  if (recordPlaylistSpin && state.queuePlaylistId) {
    try {
      const res = await api(`/api/playlists/${state.queuePlaylistId}/play`, {
        method: 'POST',
        body: JSON.stringify({ user_id: state.currentUserId, video_id: track.video_id, count: 1 }),
      });
      notifyBadges(res.new_badges, state.queuePlaylistOwner);
      await refreshBootstrapSilently();
    } catch (_) {}
  }
}

function playPrevTrack() {
  if (state.queue.length === 0) return;
  const nextIdx = state.queueIndex > 0 ? state.queueIndex - 1 : 0;
  playQueueIndex(nextIdx, false);
}

function playNextTrack() {
  if (state.queue.length === 0) return;
  if (state.queueIndex + 1 < state.queue.length) {
    playQueueIndex(state.queueIndex + 1, false);
  } else {
    stopClipMonitor();
    if (state.ytPlayer && typeof state.ytPlayer.pauseVideo === 'function') {
      try {
        state.ytPlayer.pauseVideo();
      } catch (_) {}
    }
    showToast('Reached the end of the playlist.');
  }
}

async function simulateExtraSpin() {
  if (!state.queuePlaylistId) return;
  const currentTrack = state.queue[state.queueIndex];
  try {
    const res = await api(`/api/playlists/${state.queuePlaylistId}/play`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        video_id: currentTrack ? currentTrack.video_id : null,
        count: 1,
      }),
    });
    showToast(
      res.counted
        ? `Logged +1 spin on "${esc(state.queuePlaylistTitle)}" (Total: ${res.plays} plays)`
        : `Spin not counted: your own playlist, or already counted in the last 30 min (Total: ${res.plays} plays)`
    );
    notifyBadges(res.new_badges, state.queuePlaylistOwner);
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

function grabCurrentlyPlaying() {
  const track = state.queue[state.queueIndex];
  if (!track) return;
  openGrabModal({
    videoId: track.video_id,
    title: track.title,
    channelTitle: track.channel_title,
    sourcePlaylistId: track.playlist_id || null,
    sourcePlaylistTitle: state.queuePlaylistTitle || '',
    sourceUserId: track.playlist_owner_id || null,
    sourceUsername: state.queuePlaylistOwner || '',
    startSeconds: track.start_seconds ?? null,
    endSeconds: track.end_seconds ?? null,
  });
}

async function likeCurrentlyPlayingSong(event = null) {
  const track = state.queue[state.queueIndex];
  if (!track) return;
  await toggleLikeSong(track.video_id, event);
}

async function likeActiveQueuePlaylist(event = null) {
  if (!state.queuePlaylistId || state.queuePlaylistKind === 'crate') return;
  await toggleLikePlaylist(state.queuePlaylistId, event);
}

async function swapCurrentlyPlayingTrack() {
  const track = state.queue[state.queueIndex];
  if (!track || !state.queuePlaylistId || !track.id) return;
  await openMigrateSongModal(state.queuePlaylistId, track.id);
}

function updatePlayerDeckUI() {
  const track = state.queue[state.queueIndex];
  const headerTitle = document.getElementById('player-header-title');
  const songLink = document.getElementById('np-song-link');
  const metaRow = document.getElementById('np-meta-row');
  const linerEl = document.getElementById('np-liner-note');
  const likeSongBtn = document.getElementById('np-like-song-btn');
  const likePlaylistBtn = document.getElementById('np-like-playlist-btn');
  const swapBtn = document.getElementById('np-swap-btn');
  const grabBtn = document.getElementById('np-grab-btn');
  const spinBtn = document.getElementById('np-spin-btn');
  const ytExt = document.getElementById('np-yt-external');
  const qTitle = document.getElementById('queue-playlist-title');
  const qCount = document.getElementById('queue-count');
  const qList = document.getElementById('queue-list');

  if (!track) return;

  headerTitle.textContent = `Spinning [${state.queueIndex + 1}/${state.queue.length}]`;
  songLink.textContent = track.title;
  songLink.onclick = (e) => {
    e.preventDefault();
    navigate('song', track.video_id);
  };

  const clipLabel = formatClipRange(track.start_seconds, track.end_seconds);
  let metaHtml = `<span>${esc(track.channel_title || 'YouTube')}</span>`;
  if (track.migrated_from_video_id) {
    const origDesc = track.migrated_from_title
      ? `${track.migrated_from_title} (${track.migrated_from_video_id})`
      : track.migrated_from_video_id;
    metaHtml += ` · <span class="embed-pill-migrated" style="cursor:pointer;" onclick="swapCurrentlyPlayingTrack()" title="Migrated from ${esc(origDesc)} — click to swap version">⇄ Migrated</span>`;
  }
  if (clipLabel) {
    metaHtml += ` · <span class="pill pill-commons" title="Configured track clip window">⏱ ${esc(clipLabel)}</span>`;
  }
  if (state.queuePlaylistId) {
    metaHtml += ` · from <a href="#playlist/${Number(state.queuePlaylistId)}" onclick="navigate('playlist', ${Number(state.queuePlaylistId)}); return false;">${esc(state.queuePlaylistTitle)}</a>`;
    if (state.queuePlaylistOwner) {
      metaHtml += ` by <strong>@${esc(state.queuePlaylistOwner)}</strong>`;
    }
  }
  metaRow.innerHTML = metaHtml;

  if (track.track_note && track.track_note.trim()) {
    linerEl.textContent = `“${track.track_note}”`;
    linerEl.classList.remove('hidden');
  } else {
    linerEl.classList.add('hidden');
  }

  if (likeSongBtn) {
    const songMyLikes = Number(track.song_my_likes ?? (track.song_liked_by_me ? 1 : 0));
    const songLikeCount = Number(track.song_like_count ?? 0);
    likeSongBtn.disabled = false;
    likeSongBtn.setAttribute('data-song-like-btn', track.video_id);
    likeSongBtn.setAttribute('data-like-mode', 'deck-song');
    likeSongBtn.classList.toggle('liked', songMyLikes >= 1);
    likeSongBtn.classList.toggle('superloved', songMyLikes >= 5);
    likeSongBtn.title = getLoveBtnTitle(songMyLikes, 'track');
    likeSongBtn.innerHTML = formatLoveBtnHtml(songMyLikes, songLikeCount, 'deck-song');
  }

  if (likePlaylistBtn) {
    // A crate can't be liked (the server answers 400 not_likeable).
    if (state.queuePlaylistId && state.queuePlaylistKind !== 'crate') {
      const cachedPl = playlistCache[state.queuePlaylistId];
      const plMyLikes = cachedPl
        ? Number(cachedPl.my_likes ?? (cachedPl.liked_by_me ? 1 : 0))
        : Number(state.queuePlaylistMyLikes ?? (state.queuePlaylistLiked ? 1 : 0));
      const plLikeCount = cachedPl
        ? Number(cachedPl.like_count ?? 0)
        : Number(state.queuePlaylistLikeCount ?? 0);
      likePlaylistBtn.disabled = false;
      likePlaylistBtn.setAttribute('data-playlist-like-btn', String(state.queuePlaylistId));
      likePlaylistBtn.setAttribute('data-like-mode', 'deck-playlist');
      likePlaylistBtn.classList.toggle('liked', plMyLikes >= 1);
      likePlaylistBtn.classList.toggle('superloved', plMyLikes >= 5);
      likePlaylistBtn.title = getLoveBtnTitle(plMyLikes, 'mix');
      likePlaylistBtn.innerHTML = formatLoveBtnHtml(plMyLikes, plLikeCount, 'deck-playlist');
    } else {
      likePlaylistBtn.disabled = true;
      likePlaylistBtn.removeAttribute('data-playlist-like-btn');
      likePlaylistBtn.classList.remove('liked', 'superloved');
      likePlaylistBtn.textContent = '♡ Love Mix';
    }
  }

  if (swapBtn) {
    const canSwap = Boolean(
      state.queuePlaylistId &&
        track.id &&
        (!track.playlist_owner_id || Number(track.playlist_owner_id) === Number(state.currentUserId))
    );
    swapBtn.disabled = !canSwap;
    swapBtn.textContent = track.migrated_from_video_id ? '⇄ Swap Migrated' : '⇄ Swap Version';
    swapBtn.title = canSwap
      ? track.migrated_from_video_id
        ? `This track was migrated from "${track.migrated_from_title || track.migrated_from_video_id}" — click to find a higher-quality version`
        : 'Swap this track to a different YouTube version'
      : 'Play one of your own playlists to swap track versions';
  }

  grabBtn.disabled = false;
  spinBtn.disabled = !state.queuePlaylistId;
  const startParam = track.start_seconds ? `&t=${Number(track.start_seconds)}s` : '';
  ytExt.href = `https://www.youtube.com/watch?v=${encodeURIComponent(track.video_id)}${startParam}`;
  ytExt.classList.remove('hidden');

  qTitle.textContent = state.queuePlaylistTitle || 'Active Queue';
  qCount.textContent = `${state.queue.length} track${state.queue.length === 1 ? '' : 's'}`;

  qList.innerHTML = state.queue
    .map((item, idx) => {
      const qClip = formatClipRange(item.start_seconds, item.end_seconds);
      const itemMyLikes = Number(item.song_my_likes ?? (item.song_liked_by_me ? 1 : 0));
      const itemLikes = Number(item.song_like_count ?? 0);
      return `
      <li class="queue-item ${idx === state.queueIndex ? 'active' : ''}" onclick="playQueueIndex(${Number(idx)}, false)">
        <span style="min-width:0; overflow:hidden; text-overflow:ellipsis;">
          <span class="muted" style="font-family:var(--font-mono); margin-right:6px;">${String(idx + 1).padStart(2, '0')}</span>${esc(item.title)}
          ${item.migrated_from_video_id ? `<span class="embed-pill-migrated" style="margin-left:4px; padding:0 4px; font-size:9.5px;" title="Migrated from ${esc(item.migrated_from_title || item.migrated_from_video_id)}">⇄</span>` : ''}
          ${qClip ? `<span class="muted" style="font-size:10.5px; font-family:var(--font-mono); margin-left:4px;">[${esc(qClip)}]</span>` : ''}
        </span>
        <span style="display:flex; align-items:center; gap:6px; flex-shrink:0;">
          <button
            type="button"
            class="queue-like-btn ${getLoveBtnClasses(itemMyLikes)}"
            data-song-like-btn="${esc(item.video_id)}"
            data-like-mode="queue"
            onclick="toggleLikeSong(${jsArg(item.video_id)}, event)"
            title="${esc(getLoveBtnTitle(itemMyLikes, 'song'))}"
          >${formatLoveBtnHtml(itemMyLikes, itemLikes, 'queue')}</button>
          <span class="muted" style="font-size:11px;">${idx === state.queueIndex ? '▶' : ''}</span>
        </span>
      </li>
    `;
    })
    .join('');
}

function highlightActiveTrackRows() {
  const activeTrack = state.queue[state.queueIndex];
  document.querySelectorAll('.track-row').forEach((row) => {
    const vid = row.getAttribute('data-video-id');
    const pid = Number(row.getAttribute('data-playlist-id')) || null;
    const rowTrackId = Number(row.getAttribute('data-track-row-id')) || null;
    const isMatch =
      activeTrack &&
      (activeTrack.id && rowTrackId
        ? Number(activeTrack.id) === rowTrackId
        : vid === activeTrack.video_id && (!state.queuePlaylistId || pid === state.queuePlaylistId));
    if (isMatch) {
      row.classList.add('playing');
    } else {
      row.classList.remove('playing');
    }
  });
}

// --- Bootstrap & Persona Switching ---
async function refreshBootstrapSilently() {
  const data = await api(`/api/bootstrap?user_id=${state.currentUserId}`);
  state.users = data.users;
  state.currentUser = data.current_user;
  state.currentUserId = data.current_user.id;
  state.badgeCatalog = data.badge_catalog;
  state.auth = data.auth || { mode: 'demo', signed_in: false };
  if (state.auth.mode === 'demo') localStorage.setItem('webjam_user_id', String(state.currentUserId));
  renderPersonaBar();
  renderQuickDropSelector();
  renderSidebarUserCard();
}

async function switchPersona(userId) {
  state.currentUserId = Number(userId);
  localStorage.setItem('webjam_user_id', String(state.currentUserId));
  Object.keys(playlistCache).forEach((k) => delete playlistCache[k]);
  await refreshBootstrapSilently();
  if (state.queuePlaylistId) {
    await syncQueueWithPlaylistIfActive(state.queuePlaylistId);
  } else if (state.queue.length === 1 && state.queue[0]) {
    try {
      const s = await api(`/api/songs/${encodeURIComponent(state.queue[0].video_id)}?user_id=${state.currentUserId}`);
      state.queue[0].song_like_count = Number(s.like_count ?? 0);
      state.queue[0].song_my_likes = Number(s.my_likes ?? 0);
      state.queue[0].song_liked_by_me = Boolean(s.liked_by_me);
      updatePlayerDeckUI();
    } catch (_) {}
  }
  showToast(`Switched active curator to @${esc(state.currentUser.username)}`);
  renderCurrentRoute();
}

function isGoogleAuthMode() {
  return state.auth && state.auth.mode === 'google';
}

async function signOut() {
  try {
    await api('/auth/logout', { method: 'POST' });
  } catch (_) {}
  window.location.href = '/';
}

function renderPersonaBar() {
  const container = document.getElementById('persona-pills');
  if (!container) return;
  const newUserBtn = document.getElementById('new-user-btn');
  const label = document.querySelector('.persona-label');
  if (isGoogleAuthMode()) {
    if (newUserBtn) newUserBtn.classList.add('hidden');
    if (label) {
      label.textContent = state.auth.signed_in ? 'Signed in as:' : '';
      label.title = '';
    }
    const u = state.currentUser;
    container.innerHTML = state.auth.signed_in
      ? `<a href="#user/${Number(u.id)}" class="persona-pill active" onclick="navigate('user', ${Number(u.id)}); return false;">
           <span class="persona-dot" style="background:${esc(safeColor(u.avatar_color))}"></span>@${esc(u.username)}
         </a>
         <button type="button" class="btn-tiny btn-outline" onclick="signOut()">Sign out</button>`
      : `<a href="/auth/google/login" class="btn-tiny btn-primary" style="text-decoration:none;">Sign in with Google</a>`;
    return;
  }
  container.innerHTML = state.users
    .map(
      (u) => `
      <button
        type="button"
        class="persona-pill ${u.id === state.currentUserId ? 'active' : ''}"
        onclick="switchPersona(${Number(u.id)})"
        title="@${esc(u.username)} (${u.badge_count} badges, ${u.playlist_count} playlists)"
      >
        <span class="persona-dot" style="background:${esc(safeColor(u.avatar_color))}"></span>
        @${esc(u.username)}
      </button>
    `
    )
    .join('');
}

function renderQuickDropSelector() {
  const sel = document.getElementById('quick-drop-playlist-select');
  if (!sel || !state.currentUser) return;
  const pls = state.currentUser.playlists || [];
  if (pls.length === 0) {
    sel.innerHTML = `<option value="__new__">My Crate</option>`;
  } else {
    sel.innerHTML = pls
      .map(
        (p) =>
          `<option value="${Number(p.id)}">${p.kind === 'crate' ? 'My Crate' : esc(p.title)} (${Number(p.track_count)} tracks)</option>`
      )
      .join('');
  }
}

function renderSidebarUserCard() {
  const el = document.getElementById('sidebar-user-card');
  if (!el || !state.currentUser) return;
  const u = state.currentUser;
  const m = u.metrics || {};
  const badgesHtml =
    (u.badges || []).length > 0
      ? u.badges
          .map(
            (b) =>
              `<span class="badge-chip" title="${esc(b.name)}: ${esc(b.context_note || b.description)}">${esc(b.icon)} ${esc(b.name)}</span>`
          )
          .join(' ')
      : `<span class="muted" style="font-size:12px;">No badges yet — drop a link or grab a track!</span>`;

  el.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
      <div>
        <span class="user-avatar-dot" style="background:${esc(safeColor(u.avatar_color))}"></span>
        <strong><a href="#user/${Number(u.id)}" onclick="navigate('user', ${Number(u.id)}); return false;">@${esc(u.username)}</a></strong>
        <span class="muted" style="font-size:12px;">(${esc(u.display_name)})</span>
      </div>
      <a href="#badges" onclick="navigate('badges'); return false;" class="mono-badge">${(u.badges || []).length}/${state.badgeCatalog.length} badges</a>
    </div>
    <div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">
      <strong>${m.songs_discovered || 0}</strong> first finds ·
      <strong>${m.tracks_taken_from_me || 0}</strong> grabs by others ·
      <strong>${m.tracks_taken_by_me || 0}</strong> borrowed ·
      <strong>${m.likes_received || 0}</strong> likes
    </div>
    <div style="display:flex; flex-wrap:wrap; gap:5px;">
      ${badgesHtml}
    </div>
  `;
}

// --- Routing ---
function navigate(view, param = null) {
  state.currentRoute = { view, param };
  const hash = param !== null && param !== undefined ? `#${view}/${param}` : `#${view}`;
  if (window.location.hash !== hash) {
    history.pushState(null, '', hash);
  }
  updateNavHighlight(view);
  renderCurrentRoute();
}

function navigateToCurrentProfile() {
  navigate('user', state.currentUserId);
}

function updateNavHighlight(view) {
  document.querySelectorAll('.main-nav .nav-link').forEach((a) => a.classList.remove('active'));
  if (view === 'feed' || view === 'playlist') {
    document.getElementById('nav-feed')?.classList.add('active');
  } else if (view === 'songs' || view === 'song') {
    document.getElementById('nav-songs')?.classList.add('active');
  } else if (view === 'curators') {
    document.getElementById('nav-curators')?.classList.add('active');
  } else if (view === 'badges') {
    document.getElementById('nav-badges')?.classList.add('active');
  } else if (view === 'user') {
    document.getElementById('nav-profile')?.classList.add('active');
  }
}

function parseHashRoute() {
  const raw = (window.location.hash || '#feed').replace(/^#/, '');
  const [view, ...rest] = raw.split('/');
  const param = rest.length > 0 ? decodeURIComponent(rest.join('/')) : null;
  if (!view) return { view: 'feed', param: null };
  return { view, param };
}

window.addEventListener('popstate', () => {
  state.currentRoute = parseHashRoute();
  updateNavHighlight(state.currentRoute.view);
  renderCurrentRoute();
});

async function renderCurrentRoute() {
  const { view, param } = state.currentRoute;
  const container = document.getElementById('view-container');
  if (!container) return;

  try {
    if (view === 'feed') {
      await renderFeedView(container);
    } else if (view === 'playlist') {
      await renderPlaylistView(container, Number(param));
    } else if (view === 'song') {
      await renderSongView(container, param);
    } else if (view === 'user') {
      await renderUserView(container, Number(param || state.currentUserId));
    } else if (view === 'songs') {
      await renderSongsDirectoryView(container);
    } else if (view === 'curators') {
      await renderCuratorsView(container);
    } else if (view === 'badges') {
      await renderBadgesView(container);
    } else {
      await renderFeedView(container);
    }
    highlightActiveTrackRows();
  } catch (err) {
    container.innerHTML = `<div class="playlist-sheet" style="padding:16px; color:var(--accent-warm);">Error loading view: ${esc(err.message)}</div>`;
  }
}

// --- Shared Track Row Builder ---
function renderTrackRow(t, playlist, idx, isOwner = false, isDetailView = false) {
  const posStr = String(idx + 1).padStart(2, '0');
  const isFirstFinder = t.first_added_by_user_id === playlist.user_id;
  const hasSource = t.source_username && t.source_user_id !== playlist.user_id;
  const clipRange = formatClipRange(t.start_seconds, t.end_seconds);

  let provenancePills = '';
  if (clipRange) {
    provenancePills += `<span class="pill pill-commons" title="Configured playback window for this track">⏱ ${esc(clipRange)}</span>`;
  }
  if (isFirstFinder) {
    provenancePills += `<a href="#song/${encodeURIComponent(t.video_id)}" onclick="navigate('song', ${jsArg(t.video_id)}); return false;" class="pill pill-discovery" title="First added to Webjam by @${esc(t.first_finder_username)}">◈ First on Webjam</a>`;
  } else if (t.first_finder_username) {
    provenancePills += `<a href="#song/${encodeURIComponent(t.video_id)}" onclick="navigate('song', ${jsArg(t.video_id)}); return false;" class="pill pill-commons" title="First discovered on Webjam by @${esc(t.first_finder_username)}">◈ 1st: @${esc(t.first_finder_username)}</a>`;
  }

  if (hasSource) {
    const srcView = t.source_playlist_id ? 'playlist' : 'user';
    const srcId = Number(t.source_playlist_id || t.source_user_id);
    provenancePills += `<a href="#${esc(srcView)}/${Number(srcId)}" onclick="navigate(${jsArg(srcView)}, ${Number(srcId)}); return false;" class="pill pill-lineage" title="Grabbed from @${esc(t.source_username)}${t.source_playlist_title ? ` (${esc(t.source_playlist_title)})` : ''}">↳ via @${esc(t.source_username)}</a>`;
  }

  if (t.playlist_count && t.playlist_count > 1) {
    provenancePills += `<a href="#song/${encodeURIComponent(t.video_id)}" onclick="navigate('song', ${jsArg(t.video_id)}); return false;" class="pill pill-commons" title="See all ${t.playlist_count} playlists featuring this song">in ${t.playlist_count} playlists</a>`;
  }

  if (t.migrated_from_video_id) {
    const origDesc = t.migrated_from_title
      ? `${t.migrated_from_title} (${t.migrated_from_video_id})`
      : t.migrated_from_video_id;
    const clickAttr =
      isOwner && t.id
        ? `onclick="openMigrateSongModal(${Number(playlist.id)}, ${Number(t.id)})" style="cursor:pointer;"`
        : '';
    const titleHint =
      isOwner && t.id
        ? `Migrated from "${origDesc}" — click to swap for a different or higher-quality version`
        : `Migrated from "${origDesc}"`;
    provenancePills += `<span class="embed-pill-migrated" ${clickAttr} title="${esc(titleHint)}">⇄ Migrated</span>`;
  }

  const auditInfo = state.embedAuditByVideoId && state.embedAuditByVideoId[t.video_id];
  if (auditInfo) {
    if (auditInfo.embeddable) {
      provenancePills += `<span class="embed-pill-ok" title="Verified embeddable on YouTube">✓ Embed OK</span>`;
    } else {
      provenancePills += `<span class="embed-pill-warn" title="${esc(auditInfo.reason || 'Restricted from external embedding')}">⚠ Restricted Embed</span>`;
    }
  }

  let ownerControls = '';
  const canDrag = Boolean(isOwner && isDetailView && t.id);
  if (isOwner && t.id) {
    const clipBtn = `<button type="button" class="btn-tiny btn-secondary" onclick="openTrackClipModal(${Number(playlist.id)}, ${Number(t.id)})" title="Configure start/end position and liner note">⏱ Clip / Note</button>`;
    const swapTitle = t.migrated_from_video_id
      ? `Migrated from "${t.migrated_from_title || t.migrated_from_video_id}" — swap to a different or higher-quality YouTube version`
      : 'Swap this track to a different YouTube version';
    const migrateBtn = `<button type="button" class="btn-tiny btn-secondary" onclick="openMigrateSongModal(${Number(playlist.id)}, ${Number(t.id)})" title="${esc(swapTitle)}">⇄ Swap</button>`;
    if (isDetailView) {
      ownerControls = `
        ${migrateBtn}
        ${clipBtn}
        <button type="button" class="btn-tiny btn-secondary" onclick="moveTrack(${Number(playlist.id)}, ${Number(t.id)}, 'up')" title="Move track up">↑</button>
        <button type="button" class="btn-tiny btn-secondary" onclick="moveTrack(${Number(playlist.id)}, ${Number(t.id)}, 'down')" title="Move track down">↓</button>
        <button type="button" class="btn-tiny btn-secondary" onclick="removeTrack(${Number(playlist.id)}, ${Number(t.id)})" title="Remove track from playlist">×</button>
      `;
    } else {
      ownerControls = `${migrateBtn} ${clipBtn}`;
    }
  }

  const dragAttrs = canDrag
    ? `draggable="true" data-track-id="${t.id}" ondragstart="handleTrackDragStart(event, ${Number(playlist.id)}, ${Number(t.id)})" ondragover="handleTrackDragOver(event, ${Number(playlist.id)}, ${Number(t.id)})" ondragleave="handleTrackDragLeave(event)" ondrop="handleTrackDrop(event, ${Number(playlist.id)}, ${Number(t.id)})" ondragend="handleTrackDragEnd(event)"`
    : '';
  const dragHandleHtml = canDrag
    ? `<span class="track-drag-handle" title="Drag song into place">⠿</span>`
    : '';

  const trackMyLikes = Number(t.song_my_likes ?? (t.song_liked_by_me ? 1 : 0));
  const trackTotalLikes = Number(t.song_like_count ?? 0);

  return `
    <li class="track-row ${canDrag ? 'draggable-row' : ''}" data-track-row-id="${t.id || ''}" data-video-id="${esc(t.video_id)}" data-playlist-id="${playlist.id}" ${dragAttrs}>
      <div class="track-left">
        ${dragHandleHtml}
        <span class="track-num">${posStr}</span>
        <button
          type="button"
          class="btn-play-inline"
          onclick="playTrackFromPlaylistId(${Number(playlist.id)}, ${Number(idx)})"
          title="Play starting from this track"
        >▶</button>
        <div class="track-body">
          <div class="track-main-line">
            <a href="#song/${encodeURIComponent(t.video_id)}" class="track-title-link" onclick="navigate('song', ${jsArg(t.video_id)}); return false;">
              ${esc(t.title)}
            </a>
            <span class="track-channel">${esc(t.channel_title)}</span>
            ${provenancePills}
          </div>
          ${t.track_note ? `<div class="track-note-inline">“${esc(t.track_note)}”</div>` : ''}
        </div>
      </div>
      <div class="track-right">
        ${ownerControls}
        <button
          type="button"
          class="btn-tiny btn-secondary btn-like ${getLoveBtnClasses(trackMyLikes)}"
          data-song-like-btn="${esc(t.video_id)}"
          data-like-mode="compact"
          onclick="toggleLikeSong(${jsArg(t.video_id)}, event)"
          title="${esc(getLoveBtnTitle(trackMyLikes, 'song'))}"
        >${formatLoveBtnHtml(trackMyLikes, trackTotalLikes, 'compact')}</button>
        <button
          type="button"
          class="btn-tiny btn-secondary"
          onclick="openGrabModal({
            videoId: ${jsArg(t.video_id)},
            title: ${jsArg(t.title)},
            channelTitle: ${jsArg(t.channel_title)},
            sourcePlaylistId: ${jsArg(numOrNull(playlist.id))},
            sourcePlaylistTitle: ${jsArg(playlist.title)},
            sourceUserId: ${jsArg(numOrNull(playlist.user_id))},
            sourceUsername: ${jsArg(playlist.username || '')},
            startSeconds: ${jsArg(numOrNull(t.start_seconds))},
            endSeconds: ${jsArg(numOrNull(t.end_seconds))}
          })"
          title="Grab this song into one of your playlists"
        >+ Grab</button>
      </div>
    </li>
  `;
}

// Cache playlists in memory for instant click-to-play from feed
const playlistCache = {};

async function playTrackFromPlaylistId(playlistId, trackIdx) {
  let pl = playlistCache[playlistId];
  if (!pl) {
    pl = await api(`/api/playlists/${playlistId}?user_id=${state.currentUserId}`);
    playlistCache[playlistId] = pl;
  }
  await playPlaylistFromIndex(pl, trackIdx);
}

// --- 1. Feed View ---
async function renderFeedView(container) {
  const data = await api(`/api/feed?user_id=${state.currentUserId}`);
  state.feedData = data;
  data.playlists.forEach((p) => {
    playlistCache[p.id] = p;
  });

  const playlistsHtml = data.playlists
    .map((pl) => {
      const tracksHtml = (pl.tracks || [])
        .map((t, idx) => renderTrackRow(t, pl, idx, pl.user_id === state.currentUserId, false))
        .join('');
      const likedByNames = formatCoSignersHtml(pl.liked_by_users || []);
      const plMyLikes = Number(pl.my_likes ?? (pl.liked_by_me ? 1 : 0));

      return `
        <article class="playlist-sheet">
          <div class="playlist-sheet-header">
            <div class="playlist-title-bar">
              <div>
                <a href="#playlist/${Number(pl.id)}" class="playlist-title-link" onclick="navigate('playlist', ${Number(pl.id)}); return false;">
                  ${esc(pl.title)}
                </a>
                <div class="playlist-byline">
                  <span>curated by <a href="#user/${Number(pl.user_id)}" class="user-chip" onclick="navigate('user', ${Number(pl.user_id)}); return false;">
                    <span class="user-avatar-dot" style="background:${esc(safeColor(pl.avatar_color))}"></span>@${esc(pl.username)}
                  </a></span>
                  <span>· ${pl.track_count} tracks</span>
                  <span>· ${pl.plays} spins</span>
                  ${pl.times_grabbed_from > 0 ? `<span class="pill pill-lineage" title="Other users grabbed tracks from this playlist ${pl.times_grabbed_from} times">⚡ ${pl.times_grabbed_from} grabbed</span>` : ''}
                  <span>· <a href="#playlist/${Number(pl.id)}" onclick="navigate('playlist', ${Number(pl.id)}); return false;">💬 ${pl.comment_count || 0} comments</a></span>
                </div>
              </div>
              <div class="playlist-actions-cluster">
                <button class="btn-primary btn-tiny" onclick="playTrackFromPlaylistId(${Number(pl.id)}, 0)">▶ Play Mix</button>
                <button
                  class="btn-secondary btn-tiny btn-like ${getLoveBtnClasses(plMyLikes)}"
                  data-playlist-like-btn="${pl.id}"
                  data-like-mode="header-playlist"
                  onclick="toggleLikePlaylist(${Number(pl.id)}, event)"
                  title="${esc(getLoveBtnTitle(plMyLikes, 'playlist'))}"
                >${formatLoveBtnHtml(plMyLikes, pl.like_count, 'header-playlist')}</button>
                <a href="#playlist/${Number(pl.id)}" class="btn-secondary btn-tiny" onclick="navigate('playlist', ${Number(pl.id)}); return false;">Open Sheet →</a>
              </div>
            </div>
            ${pl.description ? `<p class="playlist-liner-notes">${esc(pl.description)}</p>` : ''}
            <div data-playlist-cosigners="${pl.id}" class="${likedByNames ? '' : 'hidden'}" style="font-size:11.5px; color:var(--text-muted); margin-top:6px;">
              ${likedByNames ? `♥ Co-signed by ${likedByNames}` : ''}
            </div>
          </div>
          <ol class="tracklist">
            ${tracksHtml || `<li class="queue-empty">No tracks yet — open playlist to drop YouTube links!</li>`}
          </ol>
        </article>
      `;
    })
    .join('');

  const discoveriesHtml = data.recent_songs
    .slice(0, 7)
    .map((s) => {
      const sMyLikes = Number(s.song_my_likes ?? (s.song_liked_by_me ? 1 : 0));
      return `
      <li class="activity-item">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
          <a href="#song/${encodeURIComponent(s.video_id)}" onclick="navigate('song', ${jsArg(s.video_id)}); return false;" style="font-weight:600;">
            ${esc(s.title)}
          </a>
          <button
            type="button"
            class="btn-tiny btn-secondary btn-like ${getLoveBtnClasses(sMyLikes)}"
            data-song-like-btn="${esc(s.video_id)}"
            data-like-mode="compact"
            onclick="toggleLikeSong(${jsArg(s.video_id)}, event)"
            title="${esc(getLoveBtnTitle(sMyLikes, 'song'))}"
          >${formatLoveBtnHtml(sMyLikes, s.song_like_count, 'compact')}</button>
        </div>
        <div class="muted" style="font-size:11.5px; margin-top:2px;">
          1st found by <a href="#user/${Number(s.first_added_by_user_id)}" onclick="navigate('user', ${Number(s.first_added_by_user_id)}); return false;">@${esc(s.finder_username)}</a>
          · in ${s.playlist_count} playlist${s.playlist_count === 1 ? '' : 's'}
        </div>
      </li>
    `;
    })
    .join('');

  const activityHtml = data.activity
    .slice(0, 10)
    .map((a) => {
      let targetLink = '';
      if (a.link_type === 'playlist' && a.link_id) {
        targetLink = `<a href="#playlist/${Number(a.link_id)}" onclick="navigate('playlist', ${Number(a.link_id)}); return false;">view →</a>`;
      } else if (a.link_type === 'song' && a.link_id) {
        targetLink = `<a href="#song/${encodeURIComponent(a.link_id)}" onclick="navigate('song', ${jsArg(a.link_id)}); return false;">song page →</a>`;
      } else if (a.link_type === 'profile' && a.link_id) {
        targetLink = `<a href="#user/${Number(a.link_id)}" onclick="navigate('user', ${Number(a.link_id)}); return false;">profile →</a>`;
      }
      return `
        <li class="activity-item">
          <a href="#user/${Number(a.actor_user_id)}" onclick="navigate('user', ${Number(a.actor_user_id)}); return false;" style="font-weight:600;">@${esc(a.username)}</a>
          ${esc(a.summary)} ${targetLink}
        </li>
      `;
    })
    .join('');

  container.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">Community Playlists &amp; Liner Notes</h1>
        <p class="page-subtitle">Handmade mixtapes powered by YouTube embeds. Click any track to spin, smack <strong>♡</strong> as many times as you like to Superlove a track or mix, or click <strong>+ Grab</strong> to weave it into your own playlist.</p>
      </div>
    </div>

    <div class="feed-subgrid">
      <div>
        ${playlistsHtml}
      </div>
      <aside>
        <div class="sidebox">
          <h3 class="sidebox-title">◈ Fresh Song Discoveries</h3>
          <ul class="activity-list">${discoveriesHtml}</ul>
          <div style="margin-top:8px; text-align:right;">
            <a href="#songs" onclick="navigate('songs'); return false;" style="font-size:12px;">All ${data.recent_songs.length} songs in index →</a>
          </div>
        </div>

        <div class="sidebox">
          <h3 class="sidebox-title">⚡ Community Wire</h3>
          <ul class="activity-list">${activityHtml}</ul>
        </div>
      </aside>
    </div>
  `;
}

// --- 2. Playlist Detail View ---
async function renderPlaylistView(container, playlistId) {
  const pl = await api(`/api/playlists/${playlistId}?user_id=${state.currentUserId}`);
  playlistCache[pl.id] = pl;
  const isOwner = pl.user_id === state.currentUserId;
  const isCrate = pl.kind === 'crate';
  if (isOwner && state.currentUser) {
    try {
      state.currentUser.undo = await api(`/api/me/undo?user_id=${state.currentUserId}`);
    } catch (_) {}
  }
  const undoInfo = isOwner && state.currentUser && state.currentUser.undo && state.currentUser.undo.can_undo
    ? state.currentUser.undo
    : null;
  const lastUndoForPl = undoInfo ? { actionLabel: undoInfo.undo_label || 'last change' } : null;

  const tracksHtml = (pl.tracks || [])
    .map((t, idx) => renderTrackRow(t, pl, idx, isOwner, true))
    .join('');

  const likedByNames = formatCoSignersHtml(pl.liked_by_users || []);
  const plMyLikes = Number(pl.my_likes ?? (pl.liked_by_me ? 1 : 0));

  const ownerDropBox = isOwner
    ? `
      <div class="quick-drop-bar" style="margin-top:12px; background:#f8fbf9;">
        <form onsubmit="handleDropIntoPlaylist(event, ${Number(pl.id)})">
          <div style="font-weight:600; font-size:12.5px; margin-bottom:6px; color:var(--accent-primary);">
            + Add YouTube Link(s) to "${esc(pl.title)}"
          </div>
          <div class="quick-drop-row">
            <input type="text" id="pl-drop-url" class="input-text flex-grow" placeholder="Paste one or multiple YouTube URLs (youtube.com/watch?v=... or youtu.be/...)" required />
            <button type="submit" class="btn-primary">+ Add Track(s)</button>
          </div>
          <div class="quick-drop-note-row" style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
            <input type="text" id="pl-drop-note" class="input-text input-subtle flex-grow" placeholder="Optional liner note / commentary for this track…" />
            <input type="text" id="pl-drop-start" class="input-text input-subtle mono-input" style="width:110px;" placeholder="Start (0:42)" title="Optional start position (mm:ss or seconds)" />
            <input type="text" id="pl-drop-end" class="input-text input-subtle mono-input" style="width:110px;" placeholder="End (4:15)" title="Optional end position (mm:ss or seconds)" />
          </div>
        </form>
      </div>
    `
    : '';

  const ownerEditBox = isOwner
    ? `
      <div id="pl-edit-box" class="hidden" style="margin-top:10px; padding:10px; background:#fff; border:1px solid var(--border-strong); border-radius:3px;">
        <form onsubmit="submitEditPlaylist(event, ${Number(pl.id)})">
          <label class="form-label">Playlist Title</label>
          <input type="text" id="edit-pl-title" class="input-text full-width" value="${esc(pl.title)}" required />
          <label class="form-label">Playlist Liner Notes / Description</label>
          <textarea id="edit-pl-desc" class="input-textarea full-width" rows="3">${esc(pl.description || '')}</textarea>
          <div style="margin-top:8px; display:flex; gap:6px; justify-content:flex-end;">
            <button type="button" class="btn-secondary btn-tiny" onclick="document.getElementById('pl-edit-box').classList.add('hidden')">Cancel</button>
            <button type="submit" class="btn-primary btn-tiny">Save Liner Notes</button>
          </div>
        </form>
      </div>
    `
    : '';

  const ownerDragBanner =
    isOwner && (pl.tracks || []).length > 1
      ? `<div style="padding:6px 14px; background:var(--bg-subtle); border-bottom:1px solid var(--border-color); font-size:11.5px; color:var(--text-secondary); display:flex; justify-content:space-between; align-items:center;">
          <span><strong>⠿ Drag &amp; Drop:</strong> Grab any track row to reorder your mixtape. Press <kbd style="font-family:var(--font-mono); background:#fff; border:1px solid var(--border-strong); border-radius:2px; padding:0 4px;">Ctrl+Z</kbd> to undo changes.</span>
          ${lastUndoForPl ? `<button type="button" class="btn-tiny btn-secondary" onclick="undoLastPlaylistChange()" title="Undo: ${esc(lastUndoForPl.actionLabel)}">↶ Undo (${esc(lastUndoForPl.actionLabel)})</button>` : ''}
        </div>`
      : '';

  container.innerHTML = `
    <div style="margin-bottom:10px;">
      <a href="#feed" onclick="navigate('feed'); return false;" style="font-size:12.5px;">← Back to all playlists</a>
    </div>

    <article class="playlist-sheet">
      <div class="playlist-sheet-header">
        <div class="playlist-title-bar">
          <div>
            <h1 class="page-title" style="font-size:22px;">${esc(pl.title)}</h1>
            <div class="playlist-byline">
              <span>curated by <a href="#user/${Number(pl.user_id)}" class="user-chip" onclick="navigate('user', ${Number(pl.user_id)}); return false;">
                <span class="user-avatar-dot" style="background:${esc(safeColor(pl.avatar_color))}"></span>@${esc(pl.username)} (${esc(pl.display_name)})
              </a></span>
              <span>· ${(pl.tracks || []).length} tracks</span>
              <span>· <strong>${pl.plays}</strong> spins</span>
              <span>· updated ${shortDate(pl.updated_at)}</span>
              ${pl.times_grabbed_from > 0 ? `<span class="pill pill-lineage">⚡ ${pl.times_grabbed_from} tracks grabbed by others</span>` : ''}
            </div>
          </div>
          <div class="playlist-actions-cluster">
            <button class="btn-primary" onclick="playTrackFromPlaylistId(${Number(pl.id)}, 0)">▶ Play Full Playlist</button>
            ${isCrate ? '' : `<button
              class="btn-secondary btn-like ${getLoveBtnClasses(plMyLikes)}"
              data-playlist-like-btn="${pl.id}"
              data-like-mode="header-playlist"
              onclick="toggleLikePlaylist(${Number(pl.id)}, event)"
              title="${esc(getLoveBtnTitle(plMyLikes, 'playlist'))}"
            >${formatLoveBtnHtml(plMyLikes, pl.like_count, 'header-playlist')}</button>`}
            <button class="btn-secondary" onclick="incrementPlaylistSpin(${Number(pl.id)})" title="Record a playlist spin (counts once per listener every 30 minutes; owners' own spins don't count)">+1 Spin</button>
            ${isOwner ? `<button class="btn-secondary" id="audit-embeds-btn-${pl.id}" onclick="auditPlaylistEmbeds(${Number(pl.id)})" title="Check all songs in this playlist for embed playback & migrate restricted tracks">🔍 Audit Embeds</button>` : ''}
            ${isOwner ? `<button class="btn-secondary" onclick="openImportPlaylistModal(${Number(pl.id)})" title="Import a YouTube playlist into this mix">📥 Import YT Playlist</button>` : ''}
            ${isOwner ? `<button class="btn-secondary" onclick="document.getElementById('pl-edit-box').classList.toggle('hidden')">✎ Edit Liner Notes</button>` : ''}
            ${isOwner && lastUndoForPl ? `<button class="btn-secondary" onclick="undoLastPlaylistChange()" title="Undo last change (Ctrl+Z): ${esc(lastUndoForPl.actionLabel)}">↶ Undo</button>` : ''}
          </div>
        </div>

        ${pl.description ? `<p class="playlist-liner-notes">${esc(pl.description)}</p>` : ''}
        <div data-playlist-cosigners="${pl.id}" class="${likedByNames ? '' : 'hidden'}" style="font-size:12px; color:var(--text-muted); margin-top:8px;">
          ${likedByNames ? `♥ Co-signed by: ${likedByNames}` : ''}
        </div>
        ${ownerEditBox}
        ${ownerDropBox}
        <div id="playlist-audit-banner-${pl.id}" class="hidden" style="margin-top:10px;"></div>
      </div>

      ${ownerDragBanner}
      <ol class="tracklist">
        ${tracksHtml || `<li class="queue-empty">This playlist has no tracks yet. Paste a YouTube link above!</li>`}
      </ol>
    </article>

    ${isCrate ? '' : renderCommentsSection('playlist', String(pl.id), pl.comments || [], `Comments & Liner Note Replies on "${pl.title}"`, pl.comments_has_more)}
  `;
}

// --- 3. Song Page View (First Finder + Full Provenance Tree + Comments) ---
async function renderSongView(container, videoId) {
  const song = await api(`/api/songs/${encodeURIComponent(videoId)}?user_id=${state.currentUserId}`);

  const songLikedByNames = formatCoSignersHtml(song.liked_by_users || []);
  const songMyLikes = Number(song.my_likes ?? (song.liked_by_me ? 1 : 0));

  const appearancesHtml = (song.appearances || [])
    .map((ap, idx) => {
      const isDebut =
        ap.user_id === song.first_added_by_user_id &&
        ap.playlist_id === song.first_added_playlist_id;
      const viaBadge =
        ap.source_username && ap.source_user_id !== ap.user_id
          ? `<span class="pill pill-lineage">↳ grabbed from @${esc(ap.source_username)}${ap.source_playlist_title ? ` ("${esc(ap.source_playlist_title)}")` : ''}</span>`
          : `<span class="pill pill-commons">direct YouTube link drop</span>`;
      const apClip = formatClipRange(ap.start_seconds, ap.end_seconds);

      return `
        <li class="track-row">
          <div class="track-left">
            <span class="track-num">#${idx + 1}</span>
            <div class="track-body">
              <div class="track-main-line">
                <a href="#playlist/${Number(ap.playlist_id)}" class="track-title-link" onclick="navigate('playlist', ${Number(ap.playlist_id)}); return false;">
                  ${esc(ap.playlist_title)}
                </a>
                <span>by <a href="#user/${Number(ap.user_id)}" class="user-chip" onclick="navigate('user', ${Number(ap.user_id)}); return false;">
                  <span class="user-avatar-dot" style="background:${esc(safeColor(ap.avatar_color))}"></span>@${esc(ap.username)}
                </a></span>
                <span class="muted" style="font-size:12px;">(Track #${ap.position} · ${ap.playlist_plays} spins · ${ap.playlist_likes} likes · ${shortDate(ap.created_at)})</span>
                ${apClip ? `<span class="pill pill-commons">⏱ ${esc(apClip)}</span>` : ''}
                ${isDebut ? `<span class="pill pill-discovery">◈ Original Webjam Debut</span>` : viaBadge}
              </div>
              ${ap.track_note ? `<div class="track-note-inline">“${esc(ap.track_note)}” — @${esc(ap.username)}</div>` : ''}
            </div>
          </div>
          <div class="track-right">
            <button class="btn-tiny btn-secondary" onclick="playTrackFromPlaylistId(${Number(ap.playlist_id)}, ${Number(Math.max(0, Number(ap.position) - 1))})">▶ Play in Mix</button>
            <button class="btn-tiny btn-secondary" onclick="openGrabModal({
              videoId: ${jsArg(song.video_id)},
              title: ${jsArg(song.title)},
              channelTitle: ${jsArg(song.channel_title)},
              sourcePlaylistId: ${jsArg(numOrNull(ap.playlist_id))},
              sourcePlaylistTitle: ${jsArg(ap.playlist_title)},
              sourceUserId: ${jsArg(numOrNull(ap.user_id))},
              sourceUsername: ${jsArg(ap.username)},
              startSeconds: ${jsArg(numOrNull(ap.start_seconds))},
              endSeconds: ${jsArg(numOrNull(ap.end_seconds))}
            })">+ Grab from @${esc(ap.username)}</button>
          </div>
        </li>
      `;
    })
    .join('');

  container.innerHTML = `
    <div style="margin-bottom:10px;">
      <a href="#songs" onclick="navigate('songs'); return false;" style="font-size:12.5px;">← Song Index</a>
    </div>

    <div class="playlist-sheet">
      <div class="playlist-sheet-header">
        <div class="playlist-title-bar">
          <div>
            <span class="pill pill-discovery" style="margin-bottom:4px;">SONG PAGE · YOUTUBE ID: ${esc(song.video_id)}</span>
            <h1 class="page-title" style="font-size:22px;">${esc(song.title)}</h1>
            <div class="playlist-byline">
              <span>Channel: <strong>${esc(song.channel_title)}</strong></span>
              <span>· <strong>${(song.appearances || []).length}</strong> playlists on Webjam</span>
              <span>· <strong>${song.total_plays}</strong> total track spins</span>
              <span>· <strong id="song-like-total-stat">${song.like_count || 0}</strong> song loves</span>
              <span>· <a href="https://www.youtube.com/watch?v=${encodeURIComponent(song.video_id)}" target="_blank" rel="noopener">Watch on YouTube ↗</a></span>
            </div>
          </div>
          <div class="playlist-actions-cluster">
            <button class="btn-primary" onclick="playSingleSong({
              video_id: ${jsArg(song.video_id)},
              title: ${jsArg(song.title)},
              channel_title: ${jsArg(song.channel_title)},
              finder_username: ${jsArg(song.finder_username)},
              song_like_count: ${Number(song.like_count || 0)},
              song_my_likes: ${Number(songMyLikes)},
              song_liked_by_me: ${jsArg(Boolean(song.liked_by_me))}
            })">▶ Spin Track Now</button>
            <button
              class="btn-secondary btn-like ${getLoveBtnClasses(songMyLikes)}"
              data-song-like-btn="${esc(song.video_id)}"
              data-like-mode="header-song"
              onclick="toggleLikeSong(${jsArg(song.video_id)}, event)"
              title="${esc(getLoveBtnTitle(songMyLikes, 'song'))}"
            >${formatLoveBtnHtml(songMyLikes, song.like_count || 0, 'header-song')}</button>
            <button class="btn-secondary" onclick="openGrabModal({
              videoId: ${jsArg(song.video_id)},
              title: ${jsArg(song.title)},
              channelTitle: ${jsArg(song.channel_title)},
              sourcePlaylistId: ${jsArg(numOrNull(song.first_added_playlist_id))},
              sourcePlaylistTitle: ${jsArg(song.debut_playlist_title || '')},
              sourceUserId: ${jsArg(numOrNull(song.first_added_by_user_id))},
              sourceUsername: ${jsArg(song.finder_username)}
            })">+ Grab to My Playlist</button>
          </div>
        </div>

        <div id="song-cosigners-line" class="${songLikedByNames ? '' : 'hidden'}" style="font-size:12px; color:var(--text-muted); margin-top:8px;">
          ${songLikedByNames ? `♥ Loved by: ${songLikedByNames}` : ''}
        </div>

        <!-- Trailblazer / First Discovery Attribution Banner -->
        <div style="margin-top:12px; padding:10px 12px; background:var(--accent-gold-bg); border:1px solid #e3c878; border-radius:3px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px;">
          <div>
            <strong>◈ First Discovered on Webjam by</strong>
            <a href="#user/${Number(song.first_added_by_user_id)}" class="user-chip" onclick="navigate('user', ${Number(song.first_added_by_user_id)}); return false;" style="margin-left:4px;">
              <span class="user-avatar-dot" style="background:${esc(safeColor(song.finder_avatar_color))}"></span>@${esc(song.finder_username)} (${esc(song.finder_display_name)})
            </a>
            ${
              song.first_added_playlist_id
                ? `in playlist <a href="#playlist/${Number(song.first_added_playlist_id)}" onclick="navigate('playlist', ${Number(song.first_added_playlist_id)}); return false;" style="font-weight:600;">“${esc(song.debut_playlist_title)}”</a>`
                : ''
            }
          </div>
          <span class="mono-badge">${shortDate(song.first_added_at)}</span>
        </div>
      </div>

      <div style="padding:10px 14px; background:var(--bg-subtle); border-bottom:1px solid var(--border-color); font-weight:600; font-size:12.5px;">
        Curated In ${(song.appearances || []).length} Webjam Playlist${(song.appearances || []).length === 1 ? '' : 's'} (Chronological Lineage)
      </div>
      <ol class="tracklist">
        ${appearancesHtml}
      </ol>
    </div>

    ${renderCommentsSection('song', song.video_id, song.comments || [], `Community Notes on "${song.title}"`, song.comments_has_more)}
  `;
}

// --- 4. User Profile View ---
async function renderUserView(container, profileUserId) {
  const data = await api(`/api/users/${profileUserId}?viewer_id=${state.currentUserId}`);
  const u = data.user;
  const m = data.metrics;
  const isMe = u.id === state.currentUserId;

  const earnedBadgesHtml =
    (data.earned_badges || []).length > 0
      ? `<div class="badge-grid">
          ${data.earned_badges
            .map(
              (b) => `
              <div class="badge-card earned">
                <div style="font-weight:700; font-size:14px;">
                  <span class="badge-icon-large">${esc(b.icon)}</span>${esc(b.name)}
                </div>
                <div style="font-size:12px; color:var(--text-secondary); margin-top:4px;">${esc(b.description)}</div>
                ${b.context_note ? `<div style="font-size:11.5px; color:var(--accent-gold); margin-top:6px; font-family:var(--font-mono);">★ ${esc(b.context_note)}</div>` : ''}
              </div>
            `
            )
            .join('')}
        </div>`
      : `<p class="muted">No badges unlocked yet.</p>`;

  const playlistsHtml = (data.playlists || [])
    .map((pl) => {
      const plMyLikes = Number(pl.my_likes ?? (pl.liked_by_me ? 1 : 0));
      return `
      <div class="playlist-sheet" style="margin-bottom:12px;">
        <div class="playlist-sheet-header">
          <div class="playlist-title-bar">
            <div>
              <a href="#playlist/${Number(pl.id)}" class="playlist-title-link" onclick="navigate('playlist', ${Number(pl.id)}); return false;">${esc(pl.title)}</a>
              <span class="muted" style="font-size:12px; margin-left:8px;">
                ${pl.track_count} tracks · ${pl.plays} spins · ${pl.like_count} loves
                ${pl.times_grabbed_from > 0 ? ` · ⚡ ${pl.times_grabbed_from} grabbed by others` : ''}
              </span>
            </div>
            <div class="playlist-actions-cluster">
              <button class="btn-primary btn-tiny" onclick="playTrackFromPlaylistId(${Number(pl.id)}, 0)">▶ Play</button>
              <button
                class="btn-secondary btn-tiny btn-like ${getLoveBtnClasses(plMyLikes)}"
                data-playlist-like-btn="${pl.id}"
                data-like-mode="header-playlist"
                onclick="toggleLikePlaylist(${Number(pl.id)}, event)"
                title="${esc(getLoveBtnTitle(plMyLikes, 'playlist'))}"
              >${formatLoveBtnHtml(plMyLikes, pl.like_count, 'header-playlist')}</button>
              <a href="#playlist/${Number(pl.id)}" class="btn-secondary btn-tiny" onclick="navigate('playlist', ${Number(pl.id)}); return false;">Open →</a>
            </div>
          </div>
          ${pl.description ? `<p class="playlist-liner-notes">${esc(pl.description)}</p>` : ''}
        </div>
      </div>
    `;
    })
    .join('');

  const discoveriesHtml = (data.discovered_songs || [])
    .map((s) => {
      const sMyLikes = Number(s.song_my_likes ?? (s.song_liked_by_me ? 1 : 0));
      return `
      <li class="track-row">
        <div class="track-left">
          <span class="pill pill-discovery">◈ 1st</span>
          <div class="track-body">
            <a href="#song/${encodeURIComponent(s.video_id)}" class="track-title-link" onclick="navigate('song', ${jsArg(s.video_id)}); return false;">${esc(s.title)}</a>
            <span class="track-channel">(${esc(s.channel_title)}) · in ${s.playlist_count} playlists</span>
          </div>
        </div>
        <div class="track-right">
          <button
            type="button"
            class="btn-tiny btn-secondary btn-like ${getLoveBtnClasses(sMyLikes)}"
            data-song-like-btn="${esc(s.video_id)}"
            data-like-mode="compact"
            onclick="toggleLikeSong(${jsArg(s.video_id)}, event)"
            title="${esc(getLoveBtnTitle(sMyLikes, 'song'))}"
          >${formatLoveBtnHtml(sMyLikes, s.song_like_count, 'compact')}</button>
          <button class="btn-tiny btn-secondary" onclick="openGrabModal({
            videoId: ${jsArg(s.video_id)},
            title: ${jsArg(s.title)},
            channelTitle: ${jsArg(s.channel_title)},
            sourcePlaylistId: ${jsArg(numOrNull(s.first_added_playlist_id))},
            sourcePlaylistTitle: ${jsArg(s.debut_playlist_title || '')},
            sourceUserId: ${jsArg(numOrNull(u.id))},
            sourceUsername: ${jsArg(u.username)}
          })">+ Grab</button>
        </div>
      </li>
    `;
    })
    .join('');

  const likedSongsHtml = (data.liked_songs || [])
    .map((s) => {
      const sMyLikes = Number(s.song_my_likes ?? (s.song_liked_by_me ? 1 : 0));
      const profileLoves = Number(s.profile_user_likes || 1);
      const profileLoveBadge =
        profileLoves >= 5
          ? ` <span class="pill pill-discovery" title="@${esc(u.username)} superloved this track ×${profileLoves}">💖 Superloved ×${profileLoves}</span>`
          : profileLoves > 1
          ? ` <span class="pill pill-commons" title="@${esc(u.username)} loved this track ×${profileLoves}">♥ ×${profileLoves}</span>`
          : '';
      return `
      <li class="track-row">
        <div class="track-left">
          <button class="btn-play-inline" onclick="playSingleSong({
            video_id: ${jsArg(s.video_id)},
            title: ${jsArg(s.title)},
            channel_title: ${jsArg(s.channel_title)},
            finder_username: ${jsArg(s.finder_username)},
            song_like_count: ${Number(s.song_like_count || 0)},
            song_my_likes: ${Number(sMyLikes)},
            song_liked_by_me: ${jsArg(Boolean(s.song_liked_by_me))}
          })">▶</button>
          <div class="track-body">
            <a href="#song/${encodeURIComponent(s.video_id)}" class="track-title-link" onclick="navigate('song', ${jsArg(s.video_id)}); return false;">${esc(s.title)}</a>
            <span class="track-channel">(${esc(s.channel_title)}) · 1st found by @${esc(s.finder_username)}</span>
            ${profileLoveBadge}
          </div>
        </div>
        <div class="track-right">
          <button
            type="button"
            class="btn-tiny btn-secondary btn-like ${getLoveBtnClasses(sMyLikes)}"
            data-song-like-btn="${esc(s.video_id)}"
            data-like-mode="compact"
            onclick="toggleLikeSong(${jsArg(s.video_id)}, event)"
            title="${esc(getLoveBtnTitle(sMyLikes, 'song'))}"
          >${formatLoveBtnHtml(sMyLikes, s.song_like_count, 'compact')}</button>
          <button class="btn-tiny btn-secondary" onclick="openGrabModal({
            videoId: ${jsArg(s.video_id)},
            title: ${jsArg(s.title)},
            channelTitle: ${jsArg(s.channel_title)},
            sourcePlaylistId: null,
            sourcePlaylistTitle: '',
            sourceUserId: ${jsArg(numOrNull(s.first_added_by_user_id))},
            sourceUsername: ${jsArg(s.finder_username)}
          })">+ Grab</button>
        </div>
      </li>
    `;
    })
    .join('');

  const likedPlaylistsHtml = (data.liked_playlists || [])
    .map((lp) => {
      const pLoves = Number(lp.profile_user_likes || 1);
      const pBadge = pLoves > 1 ? ` · <strong>×${pLoves}</strong>` : '';
      return `
      <li class="activity-item">
        <a href="#playlist/${Number(lp.id)}" onclick="navigate('playlist', ${Number(lp.id)}); return false;" style="font-weight:600;">${esc(lp.title)}</a>
        <span class="muted">by <a href="#user/${Number(lp.user_id)}" onclick="navigate('user', ${Number(lp.user_id)}); return false;">@${esc(lp.username)}</a> (${lp.track_count} tracks · ${lp.like_count} loves${pBadge})</span>
      </li>
    `;
    })
    .join('');

  const grabsFromHtml = (data.grabs_from_user || [])
    .map(
      (g) => `
      <li class="activity-item">
        <strong><a href="#user/${Number(g.taker_user_id)}" onclick="navigate('user', ${Number(g.taker_user_id)}); return false;">@${esc(g.taker_username)}</a></strong>
        grabbed <a href="#song/${encodeURIComponent(g.video_id)}" onclick="navigate('song', ${jsArg(g.video_id)}); return false;">${esc(g.song_title)}</a>
        into <a href="#playlist/${Number(g.target_playlist_id)}" onclick="navigate('playlist', ${Number(g.target_playlist_id)}); return false;">“${esc(g.target_playlist_title)}”</a>
        (${g.target_playlist_plays} spins)
      </li>
    `
    )
    .join('');

  const grabsByHtml = (data.grabs_by_user || [])
    .map(
      (g) => `
      <li class="activity-item">
        Grabbed <a href="#song/${encodeURIComponent(g.video_id)}" onclick="navigate('song', ${jsArg(g.video_id)}); return false;">${esc(g.song_title)}</a>
        from <strong><a href="#user/${Number(g.source_user_id)}" onclick="navigate('user', ${Number(g.source_user_id)}); return false;">@${esc(g.source_username)}</a></strong>
        into <a href="#playlist/${Number(g.target_playlist_id)}" onclick="navigate('playlist', ${Number(g.target_playlist_id)}); return false;">“${esc(g.target_playlist_title)}”</a>
        (${g.target_playlist_plays} spins)
      </li>
    `
    )
    .join('');

  container.innerHTML = `
    <div class="playlist-sheet">
      <div class="playlist-sheet-header">
        <div class="playlist-title-bar">
          <div>
            <span class="user-avatar-dot" style="background:${esc(safeColor(u.avatar_color))}; width:12px; height:12px;"></span>
            <h1 class="page-title" style="display:inline; margin-left:6px;">${esc(u.display_name)} (@${esc(u.username)})</h1>
            <p class="page-subtitle" style="margin-top:6px;">${esc(u.bio || 'No bio yet.')}</p>
          </div>
          <div class="playlist-actions-cluster">
            ${
              isMe
                ? `<button class="btn-secondary btn-tiny" onclick="document.getElementById('edit-bio-box').classList.toggle('hidden')">✎ Edit Profile</button>
                   <button class="btn-primary btn-tiny" onclick="openNewPlaylistModal()">+ New Playlist</button>`
                : `<button class="btn-secondary btn-tiny" onclick="switchPersona(${Number(u.id)})">Switch to @${esc(u.username)}</button>`
            }
          </div>
        </div>

        ${
          isMe
            ? `<div id="edit-bio-box" class="hidden" style="margin-top:10px; padding:10px; background:#fff; border:1px solid var(--border-strong); border-radius:3px;">
                <form onsubmit="submitEditProfile(event, ${Number(u.id)})">
                  <label class="form-label">Display Name</label>
                  <input type="text" id="edit-user-display" class="input-text full-width" value="${esc(u.display_name)}" required />
                  <label class="form-label">Bio / Liner Note Manifesto</label>
                  <textarea id="edit-user-bio" class="input-textarea full-width" rows="2">${esc(u.bio || '')}</textarea>
                  <div style="margin-top:8px; display:flex; justify-content:flex-end; gap:6px;">
                    <button type="submit" class="btn-primary btn-tiny">Save Profile</button>
                  </div>
                </form>
              </div>`
            : ''
        }
      </div>

      <div style="padding:14px;">
        <div class="stat-strip">
          <div class="stat-box"><span class="stat-num">${m.playlist_count}</span><span class="stat-label">Playlists</span></div>
          <div class="stat-box"><span class="stat-num">${m.songs_discovered}</span><span class="stat-label">1st Discoveries</span></div>
          <div class="stat-box"><span class="stat-num">${m.tracks_taken_from_me}</span><span class="stat-label">Tracks Grabbed by Others</span></div>
          <div class="stat-box"><span class="stat-num">${m.tracks_taken_by_me}</span><span class="stat-label">Tracks Borrowed</span></div>
          <div class="stat-box"><span class="stat-num">${m.total_playlist_plays}</span><span class="stat-label">Playlist Spins</span></div>
          <div class="stat-box"><span class="stat-num">${m.likes_received}</span><span class="stat-label">Loves Received</span></div>
          <div class="stat-box"><span class="stat-num">${(data.earned_badges || []).length}</span><span class="stat-label">Badges Earned</span></div>
        </div>

        <h3 style="margin:14px 0 6px; font-family:var(--font-serif);">Earned Badges (${(data.earned_badges || []).length}/${(data.badge_catalog || []).length})</h3>
        ${earnedBadgesHtml}

        <div class="feed-subgrid" style="margin-top:18px;">
          <div>
            <h3 style="margin:0 0 10px; font-family:var(--font-serif);">Playlists by @${esc(u.username)}</h3>
            ${playlistsHtml || `<p class="muted">No playlists published yet.</p>`}

            <h3 style="margin:18px 0 10px; font-family:var(--font-serif);">Songs First Brought to Webjam by @${esc(u.username)} (${(data.discovered_songs || []).length})</h3>
            <div class="playlist-sheet">
              <ol class="tracklist">
                ${discoveriesHtml || `<li class="queue-empty">No first discoveries yet. Drop a brand-new YouTube link to earn Crate Digger!</li>`}
              </ol>
            </div>

            <h3 style="margin:18px 0 10px; font-family:var(--font-serif);">💖 Songs Loved by @${esc(u.username)} (${(data.liked_songs || []).length})</h3>
            <div class="playlist-sheet">
              <ol class="tracklist">
                ${likedSongsHtml || `<li class="queue-empty">No loved songs yet — smack ♡ in the player or on any playlist track!</li>`}
              </ol>
            </div>
          </div>

          <aside>
            <div class="sidebox">
              <h3 class="sidebox-title">♥ Co-Signed Mixtapes (${(data.liked_playlists || []).length})</h3>
              <ul class="activity-list">
                ${likedPlaylistsHtml || `<li class="muted" style="font-size:12px;">No liked playlists yet.</li>`}
              </ul>
            </div>

            <div class="sidebox">
              <h3 class="sidebox-title">⚡ Tracks Grabbed From @${esc(u.username)}</h3>
              <ul class="activity-list">
                ${grabsFromHtml || `<li class="muted" style="font-size:12px;">No tracks grabbed by others yet.</li>`}
              </ul>
            </div>

            <div class="sidebox">
              <h3 class="sidebox-title">↳ Tracks @${esc(u.username)} Borrowed</h3>
              <ul class="activity-list">
                ${grabsByHtml || `<li class="muted" style="font-size:12px;">Hasn't grabbed tracks from others yet.</li>`}
              </ul>
            </div>
          </aside>
        </div>
      </div>
    </div>

    ${renderCommentsSection('profile', String(u.id), data.comments || [], `@${u.username}'s Guestbook / Profile Wall`, data.comments_has_more)}
  `;
}

// --- 5. Song Index Directory View ---
async function renderSongsDirectoryView(container) {
  const data = await api(`/api/feed?user_id=${state.currentUserId}`);
  const rowsHtml = (data.recent_songs || [])
    .map((s, idx) => {
      const sMyLikes = Number(s.song_my_likes ?? (s.song_liked_by_me ? 1 : 0));
      return `
      <li class="track-row">
        <div class="track-left">
          <span class="track-num">${String(idx + 1).padStart(2, '0')}</span>
          <button class="btn-play-inline" onclick="playSingleSong({
            video_id: ${jsArg(s.video_id)},
            title: ${jsArg(s.title)},
            channel_title: ${jsArg(s.channel_title)},
            finder_username: ${jsArg(s.finder_username)},
            song_like_count: ${Number(s.song_like_count || 0)},
            song_my_likes: ${Number(sMyLikes)},
            song_liked_by_me: ${jsArg(Boolean(s.song_liked_by_me))}
          })">▶</button>
          <div class="track-body">
            <div class="track-main-line">
              <a href="#song/${encodeURIComponent(s.video_id)}" class="track-title-link" onclick="navigate('song', ${jsArg(s.video_id)}); return false;">
                ${esc(s.title)}
              </a>
              <span class="track-channel">${esc(s.channel_title)}</span>
              <span class="pill pill-discovery">◈ First found by @${esc(s.finder_username)}</span>
              <span class="pill pill-commons">in ${s.playlist_count} playlist${s.playlist_count === 1 ? '' : 's'}</span>
              <span class="pill pill-commons">${s.total_plays} spins</span>
              <span class="pill pill-commons">💬 ${s.comment_count || 0}</span>
            </div>
          </div>
        </div>
        <div class="track-right">
          <button
            type="button"
            class="btn-tiny btn-secondary btn-like ${getLoveBtnClasses(sMyLikes)}"
            data-song-like-btn="${esc(s.video_id)}"
            data-like-mode="compact"
            onclick="toggleLikeSong(${jsArg(s.video_id)}, event)"
            title="${esc(getLoveBtnTitle(sMyLikes, 'song'))}"
          >${formatLoveBtnHtml(sMyLikes, s.song_like_count, 'compact')}</button>
          <a href="#song/${encodeURIComponent(s.video_id)}" class="btn-tiny btn-secondary" onclick="navigate('song', ${jsArg(s.video_id)}); return false;">Song Lineage →</a>
          <button class="btn-tiny btn-secondary" onclick="openGrabModal({
            videoId: ${jsArg(s.video_id)},
            title: ${jsArg(s.title)},
            channelTitle: ${jsArg(s.channel_title)},
            sourcePlaylistId: ${jsArg(numOrNull(s.first_added_playlist_id))},
            sourcePlaylistTitle: ${jsArg(s.debut_playlist_title || '')},
            sourceUserId: ${jsArg(numOrNull(s.first_added_by_user_id))},
            sourceUsername: ${jsArg(s.finder_username)}
          })">+ Grab</button>
        </div>
      </li>
    `;
    })
    .join('');

  container.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">Webjam Song Commons (${(data.recent_songs || []).length} Unique Tracks)</h1>
        <p class="page-subtitle">Every song added by any curator gets a dedicated page tracking who discovered it first and every playlist that includes it.</p>
      </div>
    </div>
    <div class="playlist-sheet">
      <ol class="tracklist">${rowsHtml}</ol>
    </div>
  `;
}

// --- 6. Curators Leaderboard View ---
async function renderCuratorsView(container) {
  const data = await api(`/api/feed?user_id=${state.currentUserId}`);
  const cardsHtml = (data.curators || [])
    .map(
      (u) => `
      <div class="playlist-sheet" style="padding:14px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
          <div>
            <span class="user-avatar-dot" style="background:${esc(safeColor(u.avatar_color))}; width:10px; height:10px;"></span>
            <a href="#user/${Number(u.id)}" onclick="navigate('user', ${Number(u.id)}); return false;" style="font-size:17px; font-weight:700; font-family:var(--font-serif);">
              ${esc(u.display_name)} (@${esc(u.username)})
            </a>
            <p class="muted" style="margin:4px 0 8px;">${esc(u.bio)}</p>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
              ${(u.badges || [])
                .map(
                  (b) =>
                    `<span class="badge-chip" title="${esc(b.description)}">${esc(b.icon)} ${esc(b.name)}</span>`
                )
                .join('')}
            </div>
          </div>
          <div style="text-align:right;">
            <div class="mono-badge" style="margin-bottom:6px;">
              ${u.playlist_count} playlists · ${u.songs_discovered} first finds · ⚡ ${u.times_grabbed} grabs by others
            </div>
            <div>
              <a href="#user/${Number(u.id)}" class="btn-secondary btn-tiny" onclick="navigate('user', ${Number(u.id)}); return false;">Visit Crate &amp; Guestbook →</a>
            </div>
          </div>
        </div>
      </div>
    `
    )
    .join('');

  container.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">Community Curators</h1>
        <p class="page-subtitle">Browse fellow selectors, inspect their song discoveries, grab tracks from their mixes, or sign their guestbook.</p>
      </div>
      <button class="btn-primary" onclick="openNewUserModal()">+ New Curator Account</button>
    </div>
    ${cardsHtml}
  `;
}

// --- 7. Badges Directory & Progress View ---
async function renderBadgesView(container) {
  await refreshBootstrapSilently();
  const u = state.currentUser;
  const catalog = u.badge_progress || [];

  const cardsHtml = catalog
    .map(
      (b) => `
      <div class="badge-card ${b.earned ? 'earned' : 'locked'}">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div style="font-weight:700; font-size:14.5px;">
            <span class="badge-icon-large">${esc(b.icon)}</span>${esc(b.name)}
          </div>
          <span class="mono-badge">${esc(b.category)}</span>
        </div>
        <div style="font-size:12.5px; color:var(--text-secondary); margin-top:5px;">
          ${esc(b.description)}
        </div>
        ${
          b.earned && b.context_note
            ? `<div style="font-size:11.5px; color:var(--accent-gold); margin-top:6px; font-family:var(--font-mono);">★ Unlocked: ${esc(b.context_note)}</div>`
            : `<div style="font-size:11.5px; color:var(--text-muted); margin-top:6px; font-family:var(--font-mono);">Progress: ${b.current_value} / ${b.threshold}</div>`
        }
        <div class="badge-progress-bar">
          <div class="badge-progress-fill" style="width:${Number(b.progress_pct || 0)}%"></div>
        </div>
      </div>
    `
    )
    .join('');

  container.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">Webjam Badges &amp; Provenance Achievements</h1>
        <p class="page-subtitle">
          Showing live badge progress for <strong>@${esc(u.username)}</strong>. Earn badges by finding new songs first, having curators grab your tracks, or borrowing tracks from X and hitting Y plays!
        </p>
      </div>
    </div>
    <div class="badge-grid">
      ${cardsHtml}
    </div>
  `;
}

// --- Shared Comments Component ---
function renderCommentItem(c) {
  return `
          <div class="comment-item">
            <div class="comment-meta">
              <a href="#user/${Number(c.author_user_id)}" class="user-chip" onclick="navigate('user', ${Number(c.author_user_id)}); return false;">
                <span class="user-avatar-dot" style="background:${esc(safeColor(c.author_avatar_color))}"></span>@${esc(c.author_username)}
              </a>
              <span>(${esc(c.author_display_name)}) · ${esc(c.created_at)}</span>
            </div>
            <div class="comment-body">${esc(c.body)}</div>
          </div>
        `;
}

function renderCommentsSection(targetType, targetId, comments, heading, hasMore = false) {
  const listHtml =
    comments.length > 0
      ? comments.map(renderCommentItem).join('')
      : `<p class="muted" style="font-size:13px;">No comments yet. Be the first to leave a note!</p>`;
  const oldestId = comments.length > 0 ? Number(comments[0].id) : 0;
  const olderBtn = hasMore
    ? `<button type="button" class="btn-secondary btn-tiny" data-before-id="${oldestId}" onclick="loadOlderComments(this, ${jsArg(targetType)}, ${jsArg(targetId)})" style="margin-bottom:8px;">Show older comments</button>`
    : '';

  return `
    <section class="comments-box">
      <h3 style="margin:0 0 10px; font-family:var(--font-serif); font-size:16px;">💬 ${esc(heading)} (${comments.length}${hasMore ? '+' : ''})</h3>
      ${olderBtn}
      <div id="comments-list-${esc(targetType)}-${esc(targetId)}">
        ${listHtml}
      </div>
      <form onsubmit="submitComment(event, ${jsArg(targetType)}, ${jsArg(targetId)})" style="margin-top:12px;">
        <textarea
          id="comment-input-${esc(targetType)}-${esc(targetId)}"
          class="input-textarea full-width"
          maxlength="2000"
          rows="2"
          placeholder="Write a comment as @${esc(state.currentUser?.username || 'user')}…"
          required
        ></textarea>
        <div style="margin-top:6px; display:flex; justify-content:flex-end;">
          <button type="submit" class="btn-primary btn-tiny">Post Comment</button>
        </div>
      </form>
    </section>
  `;
}

async function loadOlderComments(btn, targetType, targetId) {
  const beforeId = Number(btn.dataset.beforeId || 0);
  const list = document.getElementById(`comments-list-${targetType}-${targetId}`);
  if (!beforeId || !list) return;
  btn.disabled = true;
  try {
    const params = new URLSearchParams({ target_type: targetType, target_id: String(targetId), before_id: String(beforeId) });
    const data = await api(`/api/comments?${params.toString()}`);
    const older = data.comments || [];
    list.insertAdjacentHTML('afterbegin', older.map(renderCommentItem).join(''));
    if (data.has_more && older.length) {
      btn.dataset.beforeId = String(Number(older[0].id));
      btn.disabled = false;
    } else {
      btn.remove();
    }
  } catch (err) {
    btn.disabled = false;
    showToast(esc(err.message));
  }
}

// --- Interactive Actions & Form Handlers ---

async function handleQuickDrop(e) {
  e.preventDefault();
  const urlInput = document.getElementById('quick-drop-url');
  const noteInput = document.getElementById('quick-drop-note');
  const startInput = document.getElementById('quick-drop-start');
  const endInput = document.getElementById('quick-drop-end');
  const sel = document.getElementById('quick-drop-playlist-select');
  const submitBtn = document.getElementById('quick-drop-submit');

  const urls = urlInput.value.trim();
  const note = noteInput.value.trim();
  const startTime = startInput ? startInput.value.trim() : '';
  const endTime = endInput ? endInput.value.trim() : '';
  if (!urls) return;

  submitBtn.disabled = true;
  submitBtn.textContent = 'Adding…';

  try {
    let targetPlaylistId = sel.value;
    if (targetPlaylistId === '__new__' || !targetPlaylistId) {
      // Quick drop with no playlist picked goes to your crate (never a fake "X's Crate" playlist).
      targetPlaylistId = state.currentUser && state.currentUser.crate_id;
      if (!targetPlaylistId) throw new Error('Sign in to drop tracks.');
    }

    const res = await api(`/api/playlists/${targetPlaylistId}/tracks`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        urls_or_ids: urls,
        track_note: note,
        start_time: startTime || null,
        end_time: endTime || null,
      }),
    });

    urlInput.value = '';
    noteInput.value = '';
    if (startInput) startInput.value = '';
    if (endInput) endInput.value = '';

    const addedCount = res.results.filter((r) => !r.already_in_playlist).length;
    const newDiscoveries = res.results.filter((r) => r.is_new_discovery).length;
    showToast(
      `Dropped ${addedCount} track(s) into playlist!${esc(skippedNote(res.skipped))} ${newDiscoveries > 0 ? `(◈ ${newDiscoveries} brand-new Webjam discovery!)` : ''}`
    );
    notifyBadges(res.new_badges);

    delete playlistCache[targetPlaylistId];
    await refreshBootstrapSilently();
    navigate('playlist', Number(targetPlaylistId));
  } catch (err) {
    showToast(esc(err.message));
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '+ Drop Track';
  }
}

async function handleDropIntoPlaylist(e, playlistId) {
  e.preventDefault();
  const urlInput = document.getElementById('pl-drop-url');
  const noteInput = document.getElementById('pl-drop-note');
  const startInput = document.getElementById('pl-drop-start');
  const endInput = document.getElementById('pl-drop-end');
  const urls = urlInput.value.trim();
  const note = noteInput.value.trim();
  const startTime = startInput ? startInput.value.trim() : '';
  const endTime = endInput ? endInput.value.trim() : '';
  if (!urls) return;

  try {
    const res = await api(`/api/playlists/${playlistId}/tracks`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        urls_or_ids: urls,
        track_note: note,
        start_time: startTime || null,
        end_time: endTime || null,
      }),
    });
    urlInput.value = '';
    noteInput.value = '';
    if (startInput) startInput.value = '';
    if (endInput) endInput.value = '';
    const newDiscoveries = res.results.filter((r) => r.is_new_discovery).length;
    const maxOcc = Math.max(0, ...res.results.map((r) => Number(r.occurrences_in_playlist || 1)));
    const occNote = maxOcc > 1 ? ` (×${maxOcc} in this mix!)` : '';
    showToast(
      `Added ${res.results.length} track(s)${occNote}!${esc(skippedNote(res.skipped))} ${newDiscoveries > 0 ? '◈ You were the FIRST to add this song to Webjam!' : ''} <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
    );
    notifyBadges(res.new_badges);
    delete playlistCache[playlistId];
    await syncQueueWithPlaylistIfActive(playlistId);
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

// --- Grab Track Modal ---
function openGrabModal({
  videoId,
  title,
  channelTitle,
  sourcePlaylistId,
  sourcePlaylistTitle,
  sourceUserId,
  sourceUsername,
  startSeconds = null,
  endSeconds = null,
}) {
  document.getElementById('grab-video-id').value = videoId;
  document.getElementById('grab-source-playlist-id').value = sourcePlaylistId || '';
  document.getElementById('grab-source-user-id').value = sourceUserId || '';

  const startEl = document.getElementById('grab-start-time');
  const endEl = document.getElementById('grab-end-time');
  if (startEl) startEl.value = startSeconds !== null && startSeconds !== undefined ? formatTimestamp(startSeconds) : '';
  if (endEl) endEl.value = endSeconds !== null && endSeconds !== undefined ? formatTimestamp(endSeconds) : '';

  const summary = document.getElementById('grab-track-summary');
  summary.innerHTML = `
    <div style="font-weight:700;">${esc(title)}</div>
    <div class="muted" style="font-size:12px;">
      ${esc(channelTitle)}
      ${
        sourceUsername
          ? ` · grabbing via <strong>@${esc(sourceUsername)}</strong>${sourcePlaylistTitle ? ` ("${esc(sourcePlaylistTitle)}")` : ''}`
          : ''
      }
    </div>
  `;

  const sel = document.getElementById('grab-target-playlist');
  const myPlaylists = (state.currentUser && state.currentUser.playlists) || [];
  sel.innerHTML =
    myPlaylists
      .map((p) => `<option value="${p.id}">${esc(p.title)} (${p.track_count} tracks)</option>`)
      .join('') + `<option value="__new__">+ Create a New Playlist…</option>`;

  toggleGrabNewPlaylistInput();
  document.getElementById('grab-track-note').value =
    sourceUsername && Number(sourceUserId) !== state.currentUserId
      ? `Hat tip to @${sourceUsername} for this track!`
      : '';

  document.getElementById('grab-modal').classList.remove('hidden');
}

function toggleGrabNewPlaylistInput() {
  const sel = document.getElementById('grab-target-playlist');
  const newFields = document.getElementById('grab-new-playlist-fields');
  if (sel.value === '__new__') {
    newFields.classList.remove('hidden');
  } else {
    newFields.classList.add('hidden');
  }
}

async function submitGrabTrack(e) {
  e.preventDefault();
  const videoId = document.getElementById('grab-video-id').value;
  const sourcePlaylistId = Number(document.getElementById('grab-source-playlist-id').value) || null;
  const sourceUserId = Number(document.getElementById('grab-source-user-id').value) || null;
  const targetVal = document.getElementById('grab-target-playlist').value;
  const note = document.getElementById('grab-track-note').value.trim();
  const startTime = document.getElementById('grab-start-time')?.value.trim() || null;
  const endTime = document.getElementById('grab-end-time')?.value.trim() || null;

  try {
    let targetPlaylistId = Number(targetVal);
    if (targetVal === '__new__' || !targetPlaylistId) {
      const newTitle =
        document.getElementById('grab-new-playlist-title').value.trim() ||
        `${state.currentUser.display_name}'s Mix`;
      const created = await api('/api/playlists', {
        method: 'POST',
        body: JSON.stringify({
          user_id: state.currentUserId,
          title: newTitle,
          description: 'Curated on Webjam.',
        }),
      });
      targetPlaylistId = created.playlist_id;
    }

    const res = await api(`/api/playlists/${targetPlaylistId}/tracks`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        urls_or_ids: videoId,
        track_note: note,
        source_playlist_id: sourcePlaylistId,
        source_user_id: sourceUserId,
        start_time: startTime,
        end_time: endTime,
      }),
    });

    closeModal('grab-modal');
    const r0 = res.results[0];
    const occ = Number(r0?.occurrences_in_playlist || 1);
    const repeatSuffix = occ > 1 ? ` (×${occ} in this mix!)` : '';
    showToast(`Grabbed "${esc(r0 ? r0.title : videoId)}" into your playlist${repeatSuffix}! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`);
    notifyBadges(res.new_badges);
    delete playlistCache[targetPlaylistId];
    await syncQueueWithPlaylistIfActive(targetPlaylistId);
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

// --- Undo (Ctrl+Z, server-side op log) & Drag-and-Drop Reordering ---
async function syncQueueWithPlaylistIfActive(playlistId) {
  const pid = Number(playlistId);
  if (state.queuePlaylistId !== pid) return;
  try {
    const currentTrack = state.queue[state.queueIndex];
    const currentTrackId = currentTrack ? currentTrack.id : null;
    const currentVideoId = currentTrack ? currentTrack.video_id : null;
    const pl = await api(`/api/playlists/${pid}?user_id=${state.currentUserId}`);
    playlistCache[pid] = pl;
    state.queue = (pl.tracks || []).map((t) => ({
      ...t,
      playlist_id: pl.id,
      playlist_title: pl.title,
      playlist_owner_id: pl.user_id,
      playlist_owner_username: pl.username,
      song_like_count: Number(t.song_like_count ?? 0),
      song_my_likes: Number(t.song_my_likes ?? (t.song_liked_by_me ? 1 : 0)),
      song_liked_by_me: Boolean(t.song_liked_by_me),
    }));
    state.queuePlaylistTitle = pl.title;
    state.queuePlaylistKind = pl.kind || 'playlist';
    state.queuePlaylistLiked = Boolean(pl.liked_by_me);
    state.queuePlaylistMyLikes = Number(pl.my_likes ?? (pl.liked_by_me ? 1 : 0));
    state.queuePlaylistLikeCount = Number(pl.like_count ?? 0);
    if (currentTrackId) {
      const idIdx = state.queue.findIndex((t) => Number(t.id) === Number(currentTrackId));
      if (idIdx !== -1) {
        state.queueIndex = idIdx;
      } else if (currentVideoId) {
        const vidIdx = state.queue.findIndex((t) => t.video_id === currentVideoId);
        if (vidIdx !== -1) state.queueIndex = vidIdx;
      }
    } else if (currentVideoId) {
      const newIdx = state.queue.findIndex((t) => t.video_id === currentVideoId);
      if (newIdx !== -1) state.queueIndex = newIdx;
    }
    updatePlayerDeckUI();
  } catch (_) {}
}

let undoInFlight = false;

// One undo per press: the request names the group the UI showed (409 stack_moved if the stack
// moved meanwhile), and presses while a request is in flight are dropped.
async function undoLastPlaylistChange(skip = false, groupId = undefined) {
  if (!state.currentUserId) {
    showToast('Sign in to undo.');
    return;
  }
  if (undoInFlight) return;
  undoInFlight = true;
  try {
    await runUndo(skip, groupId);
  } finally {
    undoInFlight = false;
  }
}

async function runUndo(skip, groupId) {
  const shown = state.currentUser && state.currentUser.undo ? state.currentUser.undo.undo_group_id : null;
  const expect = groupId !== undefined ? groupId : shown;
  let res;
  try {
    res = await api('/api/ops/undo', {
      method: 'POST',
      body: JSON.stringify({ user_id: state.currentUserId, skip: Boolean(skip), group_id: expect || null }),
    });
  } catch (err) {
    const conflictGroup = err.data && typeof err.data.group_id === 'string' ? err.data.group_id : null;
    if (err.status === 409 && err.code === 'undo_conflict' && !skip && conflictGroup) {
      if (window.confirm("Can't undo that: the playlist was changed afterwards. Skip it and undo the step before?")) {
        // Skip exactly the group that conflicted, then undo the one before it.
        let skipped;
        try {
          skipped = await api('/api/ops/undo', {
            method: 'POST',
            body: JSON.stringify({ user_id: state.currentUserId, skip: true, group_id: conflictGroup }),
          });
        } catch (skipErr) {
          showToast(`Undo failed: ${esc(skipErr.message)}`);
          await refreshBootstrapSilently();
          return;
        }
        if (state.currentUser && skipped.undo) state.currentUser.undo = skipped.undo;
        const next = skipped.undo && skipped.undo.can_undo ? skipped.undo.undo_group_id : null;
        if (!next) {
          showToast(`Skipped: ${esc(skipped.label || 'last change')}. Nothing earlier to undo.`);
          await renderCurrentRoute();
          return;
        }
        await runUndo(false, next);
      }
      return;
    }
    if (err.status === 409 && err.code === 'stack_moved') {
      try {
        await refreshBootstrapSilently();
        await renderCurrentRoute();
      } catch (_) {}
      const latest = err.data && typeof err.data.label === 'string' ? ` (${esc(err.data.label)})` : '';
      showToast(`Nothing was undone: your undo history changed. Press Ctrl+Z again to undo the latest step${latest}.`);
      return;
    }
    showToast(`Undo failed: ${esc(err.message)}`);
    return;
  }
  if (state.currentUser && res.undo) state.currentUser.undo = res.undo;
  if (!res.group_id) {
    showToast('Nothing to undo.');
    return;
  }
  const touched = Object.keys(res.order || {}).map(Number);
  touched.forEach((pid) => delete playlistCache[pid]);
  for (const pid of touched) {
    await syncQueueWithPlaylistIfActive(pid);
  }
  await refreshBootstrapSilently();
  await renderCurrentRoute();
  const emptied = (res.notes || []).some((n) => n && n.code === 'playlist_left_empty');
  const what = res.skipped ? 'Skipped' : 'Undid';
  showToast(`↶ ${what}: ${esc(res.label || 'last change')}${emptied ? ' (the new playlist is empty now)' : ''}`);
}

function clearDragIndicators() {
  document.querySelectorAll('.track-row.dragging, .track-row.drag-over-top, .track-row.drag-over-bottom').forEach((el) => {
    el.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
  });
}

function handleTrackDragStart(e, playlistId, trackId) {
  state.dragState = {
    playlistId: Number(playlistId),
    draggedTrackId: Number(trackId),
    targetTrackId: null,
    insertBefore: true,
  };
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', String(trackId));
  }
  const row = e.currentTarget;
  setTimeout(() => {
    if (row) row.classList.add('dragging');
  }, 0);
}

function handleTrackDragOver(e, playlistId, targetTrackId) {
  if (state.dragState.playlistId !== Number(playlistId)) return;
  if (!state.dragState.draggedTrackId || state.dragState.draggedTrackId === Number(targetTrackId)) return;
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';

  const row = e.currentTarget;
  const rect = row.getBoundingClientRect();
  const insertBefore = e.clientY < rect.top + rect.height / 2;

  state.dragState.targetTrackId = Number(targetTrackId);
  state.dragState.insertBefore = insertBefore;

  document.querySelectorAll('.track-row.drag-over-top, .track-row.drag-over-bottom').forEach((el) => {
    if (el !== row) el.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  row.classList.toggle('drag-over-top', insertBefore);
  row.classList.toggle('drag-over-bottom', !insertBefore);
}

function handleTrackDragLeave(e) {
  const row = e.currentTarget;
  if (row) row.classList.remove('drag-over-top', 'drag-over-bottom');
}

async function handleTrackDrop(e, playlistId, targetTrackId) {
  e.preventDefault();
  const pid = Number(playlistId);
  const draggedId = state.dragState.draggedTrackId;
  const dropTargetId = Number(targetTrackId);
  const insertBefore = state.dragState.insertBefore;

  clearDragIndicators();
  if (!draggedId || draggedId === dropTargetId || state.dragState.playlistId !== pid) return;

  const pl = playlistCache[pid];
  if (!pl || !Array.isArray(pl.tracks)) return;

  const currentIds = pl.tracks.map((t) => Number(t.id));
  const fromIdx = currentIds.indexOf(draggedId);
  if (fromIdx === -1) return;

  const draggedTrack = pl.tracks[fromIdx];
  const remaining = currentIds.filter((id) => id !== draggedId);
  const targetIdx = remaining.indexOf(dropTargetId);
  if (targetIdx === -1) return;

  const insertIdx = insertBefore ? targetIdx : targetIdx + 1;
  remaining.splice(insertIdx, 0, draggedId);

  // If the order didn't actually change, do nothing
  if (remaining.every((id, i) => id === currentIds[i])) return;

  try {
    await api(`/api/playlists/${pid}/reorder`, {
      method: 'PUT',
      body: JSON.stringify({
        user_id: state.currentUserId,
        track_ids: remaining,
      }),
    });
    delete playlistCache[pid];
    await syncQueueWithPlaylistIfActive(pid);
    await renderCurrentRoute();
    showToast(
      `Moved "${esc(draggedTrack ? draggedTrack.title : 'track')}" to #${insertIdx + 1} <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
    );
  } catch (err) {
    showToast(esc(err.message));
  }
}

function handleTrackDragEnd() {
  clearDragIndicators();
  state.dragState = {
    playlistId: null,
    draggedTrackId: null,
    targetTrackId: null,
    insertBefore: true,
  };
}

window.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'z') {
    const active = document.activeElement;
    const tag = active ? active.tagName : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || active?.isContentEditable) {
      return;
    }
    if (state.currentUserId) {
      e.preventDefault();
      if (e.repeat) return; // holding the keys must not undo several groups
      undoLastPlaylistChange();
    }
  }
});

// --- Playlist Editing, Likes, Spins, Track Notes & Clip Positions ---
function updatePlaylistLikeButtonsInDOM(playlistId, myLikes, totalLikes, likedByUsers = null) {
  const pidStr = String(playlistId);
  document.querySelectorAll(`[data-playlist-like-btn="${pidStr}"]`).forEach((btn) => {
    const mode = btn.getAttribute('data-like-mode') || 'header-playlist';
    btn.classList.toggle('liked', myLikes >= 1);
    btn.classList.toggle('superloved', myLikes >= 5);
    btn.title = getLoveBtnTitle(myLikes, 'playlist');
    btn.innerHTML = formatLoveBtnHtml(myLikes, totalLikes, mode);
  });

  if (Array.isArray(likedByUsers)) {
    const cosignHtml = formatCoSignersHtml(likedByUsers);
    document.querySelectorAll(`[data-playlist-cosigners="${pidStr}"]`).forEach((el) => {
      if (cosignHtml) {
        el.innerHTML = `♥ Co-signed by ${cosignHtml}`;
        el.classList.remove('hidden');
      } else {
        el.innerHTML = '';
        el.classList.add('hidden');
      }
    });
  }
}

function updateSongLikeButtonsInDOM(videoId, myLikes, totalLikes, likedByUsers = null) {
  document.querySelectorAll('[data-song-like-btn]').forEach((btn) => {
    if (btn.getAttribute('data-song-like-btn') !== videoId) return;
    const mode = btn.getAttribute('data-like-mode') || 'compact';
    btn.classList.toggle('liked', myLikes >= 1);
    btn.classList.toggle('superloved', myLikes >= 5);
    btn.title = getLoveBtnTitle(myLikes, 'song');
    btn.innerHTML = formatLoveBtnHtml(myLikes, totalLikes, mode);
  });

  if (state.currentRoute.view === 'song' && state.currentRoute.param === videoId) {
    const totalStat = document.getElementById('song-like-total-stat');
    if (totalStat) totalStat.textContent = String(totalLikes);
    if (Array.isArray(likedByUsers)) {
      const cosignEl = document.getElementById('song-cosigners-line');
      const cosignHtml = formatCoSignersHtml(likedByUsers);
      if (cosignEl) {
        if (cosignHtml) {
          cosignEl.innerHTML = `♥ Loved by: ${cosignHtml}`;
          cosignEl.classList.remove('hidden');
        } else {
          cosignEl.innerHTML = '';
          cosignEl.classList.add('hidden');
        }
      }
    }
  }
}

async function toggleLikePlaylist(playlistId, event = null) {
  if (event && typeof event.stopPropagation === 'function') {
    event.stopPropagation();
  }
  const pid = Number(playlistId);
  const isReset = Boolean(event && event.shiftKey);
  const clickedBtn =
    (event && event.currentTarget) ||
    document.querySelector(`[data-playlist-like-btn="${pid}"]`);

  try {
    const res = await api(`/api/playlists/${pid}/like`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        count: 1,
        reset: isReset,
      }),
    });

    const myLikes = Number(res.my_likes ?? (res.liked ? 1 : 0));
    const totalLikes = Number(res.like_count ?? 0);

    if (Number(state.queuePlaylistId) === pid) {
      state.queuePlaylistLiked = Boolean(res.liked);
      state.queuePlaylistMyLikes = myLikes;
      state.queuePlaylistLikeCount = totalLikes;
    }

    if (playlistCache[pid]) {
      playlistCache[pid].liked_by_me = Boolean(res.liked);
      playlistCache[pid].my_likes = myLikes;
      playlistCache[pid].like_count = totalLikes;
      if (Array.isArray(res.liked_by_users)) {
        playlistCache[pid].liked_by_users = res.liked_by_users;
      }
    }

    spawnSuperloveParticle(event, clickedBtn, myLikes, isReset);
    updatePlaylistLikeButtonsInDOM(pid, myLikes, totalLikes, res.liked_by_users);

    if (isReset) {
      showToast('Reset your loves on this mixtape.');
    } else if (myLikes === 1) {
      showToast('♥ Loved & co-signed mixtape! Keep smacking to Superlove!');
    } else if (myLikes === 5) {
      showToast('💖 SUPERLOVE ×5 on this mixtape! Keep smacking!');
    } else if (myLikes === 10) {
      showToast('💖🔥 SUPERLOVE ×10! Maximum crate devotion!');
    }

    notifyBadges(res.new_badges);
    notifyBadges(res.owner_new_badges);
    scheduleDebouncedSuperloveSync();

    if (
      state.currentRoute.view === 'badges' ||
      (Array.isArray(res.new_badges) && res.new_badges.length > 0 && state.currentRoute.view === 'user')
    ) {
      renderCurrentRoute();
    }
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function toggleLikeSong(videoId, event = null) {
  if (event && typeof event.stopPropagation === 'function') {
    event.stopPropagation();
  }
  if (!videoId) return;

  const isReset = Boolean(event && event.shiftKey);
  const clickedBtn =
    (event && event.currentTarget) ||
    document.querySelector(`[data-song-like-btn="${CSS.escape ? CSS.escape(videoId) : videoId}"]`);

  try {
    const res = await api(`/api/songs/${encodeURIComponent(videoId)}/like`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        count: 1,
        reset: isReset,
      }),
    });

    const myLikes = Number(res.my_likes ?? (res.liked ? 1 : 0));
    const totalLikes = Number(res.like_count ?? 0);

    // Update any matching tracks in the active player queue
    state.queue.forEach((qt) => {
      if (qt.video_id === videoId) {
        qt.song_liked_by_me = Boolean(res.liked);
        qt.song_my_likes = myLikes;
        qt.song_like_count = totalLikes;
      }
    });

    // Update cached playlists in place so playing or inspecting uses fresh counts
    Object.values(playlistCache).forEach((pl) => {
      if (pl && Array.isArray(pl.tracks)) {
        pl.tracks.forEach((t) => {
          if (t.video_id === videoId) {
            t.song_liked_by_me = Boolean(res.liked);
            t.song_my_likes = myLikes;
            t.song_like_count = totalLikes;
          }
        });
      }
    });

    spawnSuperloveParticle(event, clickedBtn, myLikes, isReset);
    updateSongLikeButtonsInDOM(videoId, myLikes, totalLikes, res.liked_by_users);

    if (isReset) {
      showToast(`Reset your loves on "${esc(res.title || videoId)}".`);
    } else if (myLikes === 1) {
      showToast(`♥ Loved "${esc(res.title || videoId)}"! Smack again to Superlove!`);
    } else if (myLikes === 5) {
      showToast(`💖 Superloved "${esc(res.title || videoId)}" ×5! Keep smacking!`);
    } else if (myLikes === 10) {
      showToast(`💖🔥 SUPERLOVE ×10 on "${esc(res.title || videoId)}"!`);
    }

    notifyBadges(res.new_badges);
    notifyBadges(res.owner_new_badges);
    scheduleDebouncedSuperloveSync();

    if (
      state.currentRoute.view === 'badges' ||
      (Array.isArray(res.new_badges) && res.new_badges.length > 0 && state.currentRoute.view === 'user')
    ) {
      renderCurrentRoute();
    }
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function incrementPlaylistSpin(playlistId) {
  try {
    const res = await api(`/api/playlists/${playlistId}/play`, {
      method: 'POST',
      body: JSON.stringify({ user_id: state.currentUserId, count: 1 }),
    });
    showToast(
      res.counted
        ? `+1 Spin recorded (Now at ${res.plays} spins)`
        : `Spin not counted: your own playlist, or already counted in the last 30 min (${res.plays} spins)`
    );
    notifyBadges(res.new_badges);
    delete playlistCache[playlistId];
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function submitEditPlaylist(e, playlistId) {
  e.preventDefault();
  const title = document.getElementById('edit-pl-title').value.trim();
  const description = document.getElementById('edit-pl-desc').value.trim();
  try {
    const res = await api(`/api/playlists/${playlistId}`, {
      method: 'PUT',
      body: JSON.stringify({
        user_id: state.currentUserId,
        title,
        description,
      }),
    });
    showToast('Updated playlist liner notes! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>');
    notifyBadges(res.new_badges);
    delete playlistCache[playlistId];
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function openTrackClipModal(playlistId, trackId) {
  let pl = playlistCache[playlistId];
  if (!pl) {
    pl = await api(`/api/playlists/${playlistId}?user_id=${state.currentUserId}`);
    playlistCache[playlistId] = pl;
  }
  const track = (pl.tracks || []).find((t) => Number(t.id) === Number(trackId));
  if (!track) return;

  document.getElementById('clip-playlist-id').value = playlistId;
  document.getElementById('clip-track-id').value = trackId;
  document.getElementById('clip-start-time').value =
    track.start_seconds !== null && track.start_seconds !== undefined
      ? formatTimestamp(track.start_seconds)
      : '';
  document.getElementById('clip-end-time').value =
    track.end_seconds !== null && track.end_seconds !== undefined
      ? formatTimestamp(track.end_seconds)
      : '';
  document.getElementById('clip-track-note').value = track.track_note || '';

  const summary = document.getElementById('clip-track-summary');
  if (summary) {
    summary.innerHTML = `
      <div style="font-weight:700;">${esc(track.title)}</div>
      <div class="muted" style="font-size:12px;">${esc(track.channel_title)} · in “${esc(pl.title)}”</div>
    `;
  }

  document.getElementById('track-clip-modal').classList.remove('hidden');
}

function setClipFieldFromPlayer(fieldId) {
  const input = document.getElementById(fieldId);
  if (!input) return;
  if (!state.ytPlayer || typeof state.ytPlayer.getCurrentTime !== 'function') {
    showToast('Start playing the track in the deck first to grab its live timestamp.');
    return;
  }
  const sec = Math.floor(state.ytPlayer.getCurrentTime() || 0);
  input.value = formatTimestamp(sec);
}

function clearClipFields() {
  const s = document.getElementById('clip-start-time');
  const e = document.getElementById('clip-end-time');
  if (s) s.value = '';
  if (e) e.value = '';
}

async function submitTrackClipForm(e) {
  e.preventDefault();
  const playlistId = Number(document.getElementById('clip-playlist-id').value);
  const trackId = Number(document.getElementById('clip-track-id').value);
  const startTime = document.getElementById('clip-start-time').value.trim();
  const endTime = document.getElementById('clip-end-time').value.trim();
  const trackNote = document.getElementById('clip-track-note').value;

  try {
    const res = await api(`/api/playlists/${playlistId}/tracks/${trackId}`, {
      method: 'PUT',
      body: JSON.stringify({
        user_id: state.currentUserId,
        track_note: trackNote,
        start_time: startTime,
        end_time: endTime,
      }),
    });

    closeModal('track-clip-modal');
    const clipDesc = formatClipRange(res.start_seconds, res.end_seconds);
    showToast(
      clipDesc
        ? `Saved track clip window (⏱ ${esc(clipDesc)})! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
        : 'Saved track settings! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>'
    );
    notifyBadges(res.new_badges);

    // Update active queue if this playlist is currently loaded
    if (state.queuePlaylistId === playlistId) {
      state.queue.forEach((qt) => {
        if (Number(qt.id) === trackId) {
          qt.start_seconds = res.start_seconds;
          qt.end_seconds = res.end_seconds;
          qt.track_note = trackNote.trim();
        }
      });
      updatePlayerDeckUI();
    }

    delete playlistCache[playlistId];
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function promptEditTrackNote(playlistId, trackId) {
  await openTrackClipModal(playlistId, trackId);
}

async function moveTrack(playlistId, trackId, direction) {
  try {
    await api(`/api/playlists/${playlistId}/tracks/${trackId}`, {
      method: 'PUT',
      body: JSON.stringify({
        user_id: state.currentUserId,
        direction,
      }),
    });
    delete playlistCache[playlistId];
    await syncQueueWithPlaylistIfActive(playlistId);
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function removeTrack(playlistId, trackId) {
  try {
    const pl = playlistCache[playlistId];
    const removedTrack = pl && pl.tracks ? pl.tracks.find((t) => Number(t.id) === Number(trackId)) : null;
    await api(`/api/playlists/${playlistId}/tracks/${trackId}?user_id=${state.currentUserId}`, {
      method: 'DELETE',
    });
    showToast('Removed track from playlist. <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>');
    delete playlistCache[playlistId];
    await syncQueueWithPlaylistIfActive(playlistId);
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

// --- New Playlist & New User Modals ---
function openNewPlaylistModal() {
  document.getElementById('np-form-title').value = '';
  document.getElementById('np-form-desc').value = '';
  document.getElementById('np-form-urls').value = '';
  document.getElementById('new-playlist-modal').classList.remove('hidden');
}

async function submitNewPlaylist(e) {
  e.preventDefault();
  const title = document.getElementById('np-form-title').value.trim();
  const description = document.getElementById('np-form-desc').value.trim();
  const initial_urls = document.getElementById('np-form-urls').value.trim();
  if (!title) return;

  try {
    const res = await api('/api/playlists', {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        title,
        description,
        initial_urls,
      }),
    });
    closeModal('new-playlist-modal');
    showToast(`Published playlist "${esc(title)}"!`);
    notifyBadges(res.new_badges);
    await refreshBootstrapSilently();
    navigate('playlist', res.playlist_id);
  } catch (err) {
    showToast(esc(err.message));
  }
}

function openNewUserModal() {
  document.getElementById('nu-username').value = '';
  document.getElementById('nu-display-name').value = '';
  document.getElementById('nu-bio').value = '';
  document.getElementById('new-user-modal').classList.remove('hidden');
}

async function submitNewUser(e) {
  e.preventDefault();
  const username = document.getElementById('nu-username').value.trim();
  const display_name = document.getElementById('nu-display-name').value.trim();
  const bio = document.getElementById('nu-bio').value.trim();
  if (!username) return;

  try {
    const res = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, display_name, bio }),
    });
    closeModal('new-user-modal');
    await switchPersona(res.user_id);
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function submitEditProfile(e, userId) {
  e.preventDefault();
  const display_name = document.getElementById('edit-user-display').value.trim();
  const bio = document.getElementById('edit-user-bio').value.trim();
  try {
    await api(`/api/users/${userId}`, {
      method: 'PUT',
      body: JSON.stringify({ display_name, bio }),
    });
    showToast('Profile updated!');
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

async function submitComment(e, targetType, targetId) {
  e.preventDefault();
  const input = document.getElementById(`comment-input-${targetType}-${targetId}`);
  if (!input) return;
  const body = input.value.trim();
  if (!body) return;

  try {
    const res = await api('/api/comments', {
      method: 'POST',
      body: JSON.stringify({
        author_user_id: state.currentUserId,
        target_type: targetType,
        target_id: String(targetId),
        body,
      }),
    });
    input.value = '';
    showToast('Comment posted!');
    notifyBadges(res.new_badges);
    await refreshBootstrapSilently();
    renderCurrentRoute();
  } catch (err) {
    showToast(esc(err.message));
  }
}

function closeModal(id) {
  document.getElementById(id)?.classList.add('hidden');
}

function closeModalOnBackdrop(e, id) {
  if (e.target && e.target.id === id) {
    closeModal(id);
  }
}

// --- Embeddable YouTube Song Search ---
function toggleEmbedSearchDrawer() {
  const drawer = document.getElementById('embed-search-drawer');
  if (!drawer) return;
  drawer.classList.toggle('hidden');
  if (!drawer.classList.contains('hidden')) {
    document.getElementById('embed-search-input')?.focus();
  }
}

function runQuickEmbedSearch(query) {
  const drawer = document.getElementById('embed-search-drawer');
  if (drawer && drawer.classList.contains('hidden')) {
    drawer.classList.remove('hidden');
  }
  const input = document.getElementById('embed-search-input');
  if (input) input.value = query;
  executeEmbedSearch(query);
}

async function handleEmbedSearch(e) {
  e.preventDefault();
  const input = document.getElementById('embed-search-input');
  if (!input || !input.value.trim()) return;
  await executeEmbedSearch(input.value.trim());
}

async function executeEmbedSearch(query) {
  const resultsEl = document.getElementById('embed-search-results');
  const btn = document.getElementById('embed-search-btn');
  if (!resultsEl) return;

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Checking Embeds…';
  }
  resultsEl.innerHTML = `<div class="muted" style="font-size:12px; padding:6px 0;">Searching YouTube &amp; verifying embed playability for <strong>"${esc(query)}"</strong>…</div>`;

  try {
    const data = await api(`/api/youtube/search?q=${encodeURIComponent(query)}&limit=6`);
    const items = data.results || [];
    if (items.length === 0) {
      resultsEl.innerHTML = `<div class="muted" style="font-size:12px; padding:6px 0;">No verified embeddable tracks found for "${esc(query)}". Try adding "live", "KEXP", or "audio".</div>`;
      return;
    }

    const rows = items
      .map(
        (item) => `
        <li class="track-row" style="padding:6px 10px;">
          <div class="track-left">
            <span class="pill pill-discovery" title="Verified previewPlayabilityStatus == OK in YouTube IFrame Embed">✓ Embed OK</span>
            <div class="track-body">
              <div class="track-main-line">
                <strong style="font-size:13px;">${esc(item.title)}</strong>
                <span class="track-channel">${esc(item.channel_title)}</span>
                <span class="mono-badge">${esc(item.video_id)}</span>
              </div>
            </div>
          </div>
          <div class="track-right">
            <button
              type="button"
              class="btn-tiny btn-secondary"
              onclick="playSingleSong({
                video_id: ${jsArg(item.video_id)},
                title: ${jsArg(item.title)},
                channel_title: ${jsArg(item.channel_title)}
              })"
            >▶ Preview</button>
            <button
              type="button"
              class="btn-tiny btn-primary"
              onclick="dropSearchedVideo(${jsArg(item.video_id)}, ${jsArg(item.title)})"
            >+ Drop into Selected Playlist</button>
          </div>
        </li>
      `
      )
      .join('');

    resultsEl.innerHTML = `
      <div class="playlist-sheet" style="margin-bottom:4px;">
        <ol class="tracklist">${rows}</ol>
      </div>
    `;
  } catch (err) {
    resultsEl.innerHTML = `<div style="color:var(--accent-warm); font-size:12px;">Search error: ${esc(err.message)}</div>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Search Verified Embeds';
    }
  }
}

async function dropSearchedVideo(videoId, title) {
  const urlInput = document.getElementById('quick-drop-url');
  if (urlInput) {
    urlInput.value = `https://www.youtube.com/watch?v=${videoId}`;
  }
  const form = document.getElementById('quick-drop-form');
  if (form) {
    await handleQuickDrop(new Event('submit', { cancelable: true }));
  } else {
    showToast(`Copied ${esc(title)} (${esc(videoId)}) into link bar!`);
  }
}

// --- Import YouTube Playlist & Migrate Song Wizard ---
function openImportPlaylistModal(defaultTargetPlaylistId = null) {
  resetImportWizardToSource();
  state.importWizard = {
    defaultTargetPlaylistId: defaultTargetPlaylistId ? Number(defaultTargetPlaylistId) : null,
    report: null,
    tracks: [],
    openDrawerIdx: null,
    candidatesByIdx: {},
  };
  document.getElementById('import-playlist-modal')?.classList.remove('hidden');
  document.getElementById('import-source-input')?.focus();
  loadMyYouTubePlaylists();
}

async function loadMyYouTubePlaylists() {
  const wrap = document.getElementById('import-my-youtube');
  const sel = document.getElementById('import-my-youtube-select');
  if (!wrap || !sel) return;
  // Hidden until YouTube access can be connected (incremental consent, Phase 4).
  const show = isGoogleAuthMode() && state.auth.signed_in && Boolean(state.auth.youtube_connected);
  wrap.classList.toggle('hidden', !show);
  if (!show) return;
  sel.innerHTML = '<option value="">Loading your playlists…</option>';
  try {
    const data = await api('/api/youtube/my-playlists');
    sel.innerHTML = data.playlists.length
      ? data.playlists
          .map((p) => `<option value="${esc(p.id)}">${esc(p.title)} (${Number(p.item_count)} videos${p.privacy ? ', ' + esc(p.privacy) : ''})</option>`)
          .join('')
      : '<option value="">No playlists on your YouTube account</option>';
  } catch (err) {
    sel.innerHTML = `<option value="">${esc(err.message)}</option>`;
  }
}

async function useMyYouTubePlaylist() {
  const sel = document.getElementById('import-my-youtube-select');
  const input = document.getElementById('import-source-input');
  if (!sel || !sel.value || !input) return;
  const maxTracks = Number(document.getElementById('import-max-tracks')?.value || 25);
  try {
    const data = await api(`/api/youtube/my-playlists/${encodeURIComponent(sel.value)}/videos?limit=${maxTracks}`);
    if (!data.video_ids.length) {
      showToast('That playlist has no videos.');
      return;
    }
    input.value = data.video_ids.map((id) => `https://www.youtube.com/watch?v=${id}`).join('\n');
    const titleEl = document.getElementById('import-playlist-title');
    if (titleEl && !titleEl.value) titleEl.value = sel.options[sel.selectedIndex].text.replace(/ \(\d+ videos.*\)$/, '');
  } catch (err) {
    showToast(esc(err.message));
  }
}

function resetImportWizardToSource() {
  document.getElementById('import-step-source')?.classList.remove('hidden');
  document.getElementById('import-step-wizard')?.classList.add('hidden');
}

function fillImportSample(preset) {
  const input = document.getElementById('import-source-input');
  if (!input) return;
  if (preset === 'mixed') {
    input.value = [
      'https://www.youtube.com/watch?v=Z5NoQg8LdDk',
      'https://www.youtube.com/watch?v=0PsOJ_fRckg',
      'https://www.youtube.com/watch?v=q4xKvHANqjk',
      'https://www.youtube.com/watch?v=TNsSBhl_2LI',
    ].join('\n');
  } else if (preset === 'kexp') {
    input.value = 'https://www.youtube.com/playlist?list=PL15B1E77BB5708555';
  }
}

function populateImportDestinationSelect() {
  const sel = document.getElementById('import-target-playlist');
  if (!sel) return;
  const myPlaylists = (state.currentUser && state.currentUser.playlists) || [];
  const defaultTarget = state.importWizard?.defaultTargetPlaylistId;

  let html = `<option value="__new__">+ Create a New Webjam Playlist</option>`;
  myPlaylists.forEach((p) => {
    const selected = defaultTarget && Number(p.id) === Number(defaultTarget) ? 'selected' : '';
    html += `<option value="${p.id}" ${selected}>Append to "${esc(p.title)}" (${p.track_count} tracks)</option>`;
  });
  sel.innerHTML = html;
  toggleImportDestinationFields();
}

function toggleImportDestinationFields() {
  const sel = document.getElementById('import-target-playlist');
  const isNew = !sel || sel.value === '__new__';
  document.getElementById('import-new-title-wrap')?.classList.toggle('hidden', !isNew);
  document.getElementById('import-new-desc-wrap')?.classList.toggle('hidden', !isNew);
}

async function handleInspectPlaylistImport(e) {
  e.preventDefault();
  const sourceInput = document.getElementById('import-source-input')?.value.trim() || '';
  const maxTracks = Number(document.getElementById('import-max-tracks')?.value || 25);
  const btn = document.getElementById('import-inspect-btn');
  if (!sourceInput) return;

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Inspecting Playlist & Verifying Embeds…';
  }

  try {
    const report = await api('/api/playlists/import/inspect', {
      method: 'POST',
      body: JSON.stringify({
        source_input: sourceInput,
        max_tracks: maxTracks,
      }),
    });

    const wizardTracks = (report.tracks || []).map((t) => ({
      ...t,
      included: true,
      original_video_id: t.video_id,
      original_title: t.title,
      original_channel_title: t.channel_title,
      original_embeddable: Boolean(t.embeddable),
      original_reason: t.reason,
      migrated: false,
    }));

    // Also cache embed audit info
    wizardTracks.forEach((t) => {
      state.embedAuditByVideoId[t.video_id] = {
        embeddable: Boolean(t.embeddable),
        status: t.status,
        reason: t.reason,
        suggested_query: t.suggested_query,
      };
    });

    // Automatically open the first non-embeddable track's migration drawer if any exist
    const firstRestrictedIdx = wizardTracks.findIndex((t) => !t.embeddable);

    state.importWizard = {
      ...(state.importWizard || {}),
      report,
      tracks: wizardTracks,
      openDrawerIdx: firstRestrictedIdx !== -1 ? firstRestrictedIdx : null,
      candidatesByIdx: {},
    };

    populateImportDestinationSelect();
    const titleEl = document.getElementById('import-playlist-title');
    const descEl = document.getElementById('import-playlist-desc');
    if (titleEl) titleEl.value = report.title || 'Imported YouTube Playlist';
    if (descEl) descEl.value = report.description || 'Imported from YouTube into Webjam.';

    document.getElementById('import-step-source')?.classList.add('hidden');
    document.getElementById('import-step-wizard')?.classList.remove('hidden');

    renderImportWizardStep();
    if (firstRestrictedIdx !== -1) {
      searchImportTrackCandidates(firstRestrictedIdx);
    }
  } catch (err) {
    showToast(esc(err.message));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔍 Inspect Playlist & Check Embeds →';
    }
  }
}

function renderImportWizardStep() {
  const wiz = state.importWizard;
  if (!wiz) return;

  const tracks = wiz.tracks || [];
  const includedTracks = tracks.filter((t) => t.included);
  const restrictedCount = tracks.filter((t) => t.included && !t.embeddable).length;
  const migratedCount = tracks.filter((t) => t.migrated).length;
  const embedOkCount = tracks.filter((t) => t.included && t.embeddable).length;

  const bannerEl = document.getElementById('import-health-banner');
  if (bannerEl) {
    bannerEl.innerHTML = `
      <div>
        <strong>${tracks.length} track${tracks.length === 1 ? '' : 's'} inspected:</strong>
        <span class="embed-pill-ok" style="margin-left:6px;">✓ ${embedOkCount} Embeddable</span>
        ${
          restrictedCount > 0
            ? `<span class="embed-pill-warn" style="margin-left:6px;">⚠ ${restrictedCount} Non-Embeddable (Needs Migration)</span>`
            : `<span class="muted" style="margin-left:6px; font-size:12px;">All selected songs are ready for Webjam embed playback!</span>`
        }
        ${
          migratedCount > 0
            ? `<span class="embed-pill-migrated" style="margin-left:6px;">✨ ${migratedCount} Migrated</span>`
            : ''
        }
      </div>
      <div>
        ${
          restrictedCount > 0
            ? `<button type="button" class="btn-primary btn-tiny" id="import-auto-migrate-btn" onclick="autoMigrateAllImportTracks()">🪄 Auto-Migrate All Restricted Songs (${restrictedCount})</button>`
            : ''
        }
      </div>
    `;
  }

  const commitBtn = document.getElementById('import-commit-btn');
  if (commitBtn) {
    commitBtn.textContent = `✓ Import ${includedTracks.length} Track${includedTracks.length === 1 ? '' : 's'} into Webjam`;
    commitBtn.disabled = includedTracks.length === 0;
  }

  const listEl = document.getElementById('import-wizard-tracks');
  if (!listEl) return;

  listEl.innerHTML = tracks
    .map((t, idx) => {
      const rowClass = t.migrated ? 'migrated' : !t.embeddable ? 'restricted' : '';
      let statusBadge = '';
      if (t.migrated) {
        statusBadge = `<span class="embed-pill-migrated" title="Migrated from ${esc(t.original_video_id)} to verified embeddable version">✨ Migrated → Embed OK</span>`;
      } else if (t.embeddable) {
        statusBadge = `<span class="embed-pill-ok">✓ Embed OK</span>`;
      } else {
        statusBadge = `<span class="embed-pill-warn" title="${esc(t.reason || 'Uploader blocked external embeds')}">⚠ Non-Embeddable</span>`;
      }

      const isDrawerOpen = wiz.openDrawerIdx === idx;
      const candidates = wiz.candidatesByIdx[idx];

      let candidatesHtml = '';
      if (isDrawerOpen) {
        if (candidates === 'loading') {
          candidatesHtml = `<div class="muted" style="font-size:12px; padding:6px 0;">Searching YouTube &amp; verifying embed playability for <strong>"${esc(t.suggested_query)}"</strong>…</div>`;
        } else if (Array.isArray(candidates) && candidates.length === 0) {
          candidatesHtml = `<div class="muted" style="font-size:12px; padding:6px 0;">No verified embeddable matches found yet. Try adjusting the search query above (e.g. add "live", "audio", or "topic").</div>`;
        } else if (Array.isArray(candidates)) {
          candidatesHtml = `
            <ol class="tracklist" style="margin-top:6px; border:1px solid var(--border-color); border-radius:3px; background:#fff;">
              ${candidates
                .map(
                  (c, cIdx) => `
                  <li class="track-row" style="padding:5px 8px;">
                    <div class="track-left">
                      <span class="embed-pill-ok">✓ Embed OK</span>
                      <div class="track-body">
                        <div class="track-main-line">
                          <strong style="font-size:12.5px;">${esc(c.title)}</strong>
                          <span class="track-channel">${esc(c.channel_title)}</span>
                          <span class="mono-badge">${esc(c.video_id)}</span>
                        </div>
                      </div>
                    </div>
                    <div class="track-right">
                      <button type="button" class="btn-tiny btn-secondary" onclick="playSingleSong({ video_id: ${jsArg(c.video_id)}, title: ${jsArg(c.title)}, channel_title: ${jsArg(c.channel_title)} })">▶ Preview</button>
                      <button type="button" class="btn-tiny btn-primary" onclick="selectImportTrackMigration(${Number(idx)}, ${Number(cIdx)})">✓ Use This Version</button>
                    </div>
                  </li>
                `
                )
                .join('')}
            </ol>
          `;
        }
      }

      const drawerHtml = isDrawerOpen
        ? `
          <div class="import-migrate-drawer">
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
              <span style="font-size:11.5px; font-weight:700; color:var(--accent-primary);">🪄 Find Embeddable Version:</span>
              <input
                type="text"
                id="import-migrate-q-${idx}"
                class="input-text flex-grow"
                style="font-size:12px; padding:3px 7px;"
                value="${esc(t.suggested_query || t.title)}"
                onkeydown="if(event.key==='Enter'){event.preventDefault(); updateImportTrackQueryAndSearch(${Number(idx)});}"
              />
              <button type="button" class="btn-tiny btn-primary" onclick="updateImportTrackQueryAndSearch(${Number(idx)})">🔍 Search Verified Embeds</button>
              <button type="button" class="btn-tiny btn-secondary" onclick="toggleImportTrackMigrator(${Number(idx)})">Close</button>
            </div>
            ${candidatesHtml}
            <div style="margin-top:6px; padding-top:6px; border-top:1px dashed #dcd6c6; display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
              <span class="muted" style="font-size:11.5px;">Or paste custom YouTube URL/ID:</span>
              <input type="text" id="import-custom-vid-${idx}" class="input-text mono-input flex-grow" style="font-size:11.5px; padding:2px 6px;" placeholder="https://www.youtube.com/watch?v=..." />
              <button type="button" class="btn-tiny btn-secondary" onclick="applyImportCustomVideo(${Number(idx)})">Swap to URL</button>
            </div>
          </div>
        `
        : '';

      return `
        <div class="import-track-row ${rowClass}">
          <div class="import-track-main">
            <div style="display:flex; align-items:center; gap:8px; min-width:0;">
              <input
                type="checkbox"
                ${t.included ? 'checked' : ''}
                onchange="toggleImportTrackIncluded(${Number(idx)}, this.checked)"
                title="Include this track in import"
              />
              <span class="track-num">${String(idx + 1).padStart(2, '0')}</span>
              <div style="min-width:0;">
                <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                  <strong style="font-size:13px;">${esc(t.title)}</strong>
                  <span class="track-channel">${esc(t.channel_title)}</span>
                  <span class="mono-badge">${esc(t.video_id)}</span>
                  ${statusBadge}
                </div>
                ${
                  t.migrated
                    ? `<div class="muted" style="font-size:11px; margin-top:2px;">Migrated from restricted track <code>${esc(t.original_video_id)}</code> (${esc(t.original_title)})</div>`
                    : !t.embeddable
                      ? `<div style="font-size:11px; color:#b45309; margin-top:2px;">${esc(t.reason || 'This video is restricted from playing in third-party embeds')} — click <strong>🪄 Migrate Song</strong> to swap to an embeddable upload.</div>`
                      : ''
                }
              </div>
            </div>
            <div style="display:flex; align-items:center; gap:5px; flex-shrink:0;">
              <button
                type="button"
                class="btn-tiny btn-secondary"
                onclick="playSingleSong({ video_id: ${jsArg(t.video_id)}, title: ${jsArg(t.title)}, channel_title: ${jsArg(t.channel_title)} })"
              >▶ Preview</button>
              <button
                type="button"
                class="btn-tiny ${!t.embeddable ? 'btn-primary' : 'btn-secondary'}"
                onclick="toggleImportTrackMigrator(${Number(idx)})"
              >🪄 ${t.migrated ? 'Change Version' : 'Migrate Song'}</button>
              ${
                t.migrated
                  ? `<button type="button" class="btn-tiny btn-secondary" onclick="revertImportTrackMigration(${Number(idx)})" title="Revert to original video ID">↺ Revert</button>`
                  : ''
              }
            </div>
          </div>
          ${drawerHtml}
        </div>
      `;
    })
    .join('');
}

function toggleImportTrackIncluded(idx, checked) {
  if (!state.importWizard || !state.importWizard.tracks[idx]) return;
  state.importWizard.tracks[idx].included = Boolean(checked);
  renderImportWizardStep();
}

function toggleImportTrackMigrator(idx) {
  const wiz = state.importWizard;
  if (!wiz) return;
  if (wiz.openDrawerIdx === idx) {
    wiz.openDrawerIdx = null;
    renderImportWizardStep();
    return;
  }
  wiz.openDrawerIdx = idx;
  renderImportWizardStep();
  if (!wiz.candidatesByIdx[idx]) {
    searchImportTrackCandidates(idx);
  }
}

function updateImportTrackQueryAndSearch(idx) {
  const wiz = state.importWizard;
  if (!wiz || !wiz.tracks[idx]) return;
  const qEl = document.getElementById(`import-migrate-q-${idx}`);
  if (qEl && qEl.value.trim()) {
    wiz.tracks[idx].suggested_query = qEl.value.trim();
  }
  searchImportTrackCandidates(idx);
}

async function searchImportTrackCandidates(idx) {
  const wiz = state.importWizard;
  if (!wiz || !wiz.tracks[idx]) return;
  const query = (wiz.tracks[idx].suggested_query || wiz.tracks[idx].title || '').trim();
  if (!query) return;

  wiz.candidatesByIdx[idx] = 'loading';
  renderImportWizardStep();

  try {
    const data = await api(`/api/youtube/search?q=${encodeURIComponent(query)}&limit=5`);
    const results = (data.results || []).filter(
      (r) => r.video_id !== wiz.tracks[idx].original_video_id
    );
    wiz.candidatesByIdx[idx] = results;
  } catch (_) {
    wiz.candidatesByIdx[idx] = [];
  }
  renderImportWizardStep();
}

function selectImportTrackMigration(idx, candidateIdx) {
  const wiz = state.importWizard;
  if (!wiz || !wiz.tracks[idx]) return;
  const candidates = wiz.candidatesByIdx[idx];
  if (!Array.isArray(candidates) || !candidates[candidateIdx]) return;
  const chosen = candidates[candidateIdx];

  const t = wiz.tracks[idx];
  t.video_id = chosen.video_id;
  t.title = chosen.title;
  t.channel_title = chosen.channel_title;
  t.embeddable = true;
  t.status = 'OK';
  t.reason = null;
  t.migrated = true;
  state.embedAuditByVideoId[chosen.video_id] = { embeddable: true, status: 'OK', reason: null };

  wiz.openDrawerIdx = null;
  renderImportWizardStep();
  showToast(`✨ Migrated to embeddable version: "${esc(chosen.title)}"`);
}

async function applyImportCustomVideo(idx) {
  const wiz = state.importWizard;
  if (!wiz || !wiz.tracks[idx]) return;
  const input = document.getElementById(`import-custom-vid-${idx}`);
  const raw = input ? input.value.trim() : '';
  if (!raw) return;

  try {
    const report = await api('/api/playlists/import/inspect', {
      method: 'POST',
      body: JSON.stringify({ source_input: raw, max_tracks: 1 }),
    });
    const first = report.tracks && report.tracks[0];
    if (!first) {
      showToast('Could not parse a valid YouTube video ID from that URL.');
      return;
    }
    const t = wiz.tracks[idx];
    t.video_id = first.video_id;
    t.title = first.title;
    t.channel_title = first.channel_title;
    t.embeddable = Boolean(first.embeddable);
    t.status = first.status;
    t.reason = first.reason;
    t.migrated = true;
    wiz.openDrawerIdx = null;
    renderImportWizardStep();
    showToast(`Swapped track to "${esc(first.title)}"`);
  } catch (err) {
    showToast(esc(err.message));
  }
}

function revertImportTrackMigration(idx) {
  const wiz = state.importWizard;
  if (!wiz || !wiz.tracks[idx]) return;
  const t = wiz.tracks[idx];
  t.video_id = t.original_video_id;
  t.title = t.original_title;
  t.channel_title = t.original_channel_title;
  t.embeddable = t.original_embeddable;
  t.reason = t.original_reason;
  t.migrated = false;
  renderImportWizardStep();
}

async function autoMigrateAllImportTracks() {
  const wiz = state.importWizard;
  if (!wiz) return;
  const btn = document.getElementById('import-auto-migrate-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🪄 Finding Verified Embeddable Replacements…';
  }

  let migratedNow = 0;
  let failedNow = 0;
  for (let idx = 0; idx < wiz.tracks.length; idx++) {
    const t = wiz.tracks[idx];
    if (!t.included || t.embeddable) continue;
    const query = (t.suggested_query || t.title || '').trim();
    if (!query) continue;
    try {
      const data = await apiWithRetry(`/api/youtube/search?q=${encodeURIComponent(query)}&limit=4`);
      const match = (data.results || []).find((r) => r.video_id !== t.original_video_id && r.embeddable);
      if (match) {
        wiz.candidatesByIdx[idx] = data.results;
        t.video_id = match.video_id;
        t.title = match.title;
        t.channel_title = match.channel_title;
        t.embeddable = true;
        t.status = 'OK';
        t.reason = null;
        t.migrated = true;
        state.embedAuditByVideoId[match.video_id] = { embeddable: true, status: 'OK', reason: null };
        migratedNow++;
      }
    } catch (_) {
      failedNow++;
    }
  }

  wiz.openDrawerIdx = null;
  renderImportWizardStep();
  const failNote = failedNow > 0 ? ` ${failedNow} search${failedNow === 1 ? '' : 'es'} failed (try again in a minute).` : '';
  if (migratedNow > 0) {
    showToast(`✨ Auto-migrated ${migratedNow} restricted song${migratedNow === 1 ? '' : 's'} to verified-embeddable versions!${failNote}`);
  } else {
    showToast(`Could not auto-find replacements for all tracks — click "🪄 Migrate Song" on a track to customize the search.${failNote}`);
  }
}

async function submitCommitPlaylistImport() {
  const wiz = state.importWizard;
  if (!wiz) return;
  const included = (wiz.tracks || []).filter((t) => t.included);
  if (included.length === 0) {
    showToast('Select at least one track to import.');
    return;
  }

  const destVal = document.getElementById('import-target-playlist')?.value || '__new__';
  const targetPlaylistId = destVal === '__new__' ? null : Number(destVal);
  const title = document.getElementById('import-playlist-title')?.value.trim() || 'Imported YouTube Playlist';
  const description = document.getElementById('import-playlist-desc')?.value.trim() || '';
  const commitBtn = document.getElementById('import-commit-btn');

  if (commitBtn) {
    commitBtn.disabled = true;
    commitBtn.textContent = 'Importing into Webjam…';
  }

  try {
    const res = await api('/api/playlists/import/commit', {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        title,
        description,
        target_playlist_id: targetPlaylistId,
        tracks: included.map((t) => ({
          video_id: t.video_id,
          custom_title: t.title,
          track_note: t.migrated
            ? `Migrated to embeddable version from ${t.original_video_id}`
            : '',
          start_seconds: t.start_seconds ?? null,
          end_seconds: t.end_seconds ?? null,
          migrated_from_video_id: t.migrated ? t.original_video_id : null,
          migrated_from_title: t.migrated ? t.original_title : null,
        })),
      }),
    });

    closeModal('import-playlist-modal');
    showToast(
      `Imported ${res.added_tracks.length} track(s) into "${esc(res.title)}"!${esc(skippedNote(res.skipped))}`
    );
    notifyBadges(res.new_badges);
    delete playlistCache[res.playlist_id];
    await refreshBootstrapSilently();
    navigate('playlist', Number(res.playlist_id));
  } catch (err) {
    showToast(esc(err.message));
  } finally {
    if (commitBtn) {
      commitBtn.disabled = false;
    }
  }
}

// --- Existing Playlist Embed Audit & Single-Track Swap / Migrate Modal ---
async function auditPlaylistEmbeds(playlistId) {
  const pid = Number(playlistId);
  const btn = document.getElementById(`audit-embeds-btn-${pid}`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🔍 Checking Embeds…';
  }

  try {
    const audit = await api(`/api/playlists/${Number(pid)}/audit-embeds?user_id=${Number(state.currentUserId)}`);
    (audit.tracks || []).forEach((t) => {
      state.embedAuditByVideoId[t.video_id] = {
        embeddable: Boolean(t.embeddable),
        status: t.status,
        reason: t.reason,
        suggested_query: t.suggested_query,
      };
    });

    await renderCurrentRoute();

    const banner = document.getElementById(`playlist-audit-banner-${pid}`);
    if (banner) {
      banner.classList.remove('hidden');
      const isOwner = playlistCache[pid]?.user_id === state.currentUserId;
      banner.innerHTML = `
        <div class="import-health-banner">
          <div>
            <strong>Embed Audit Complete:</strong>
            <span class="embed-pill-ok" style="margin-left:6px;">✓ ${audit.embeddable_count} Embeddable</span>
            ${
              audit.non_embeddable_count > 0
                ? `<span class="embed-pill-warn" style="margin-left:6px;">⚠ ${audit.non_embeddable_count} Restricted / Non-Embeddable</span>`
                : `<span class="muted" style="margin-left:6px;">Every track in this playlist is verified embeddable!</span>`
            }
          </div>
          <div>
            ${
              audit.non_embeddable_count > 0 && isOwner
                ? `<button type="button" class="btn-primary btn-tiny" id="pl-auto-migrate-btn-${pid}" onclick="autoMigrateAuditedPlaylist(${Number(pid)})">🪄 Auto-Migrate ${audit.non_embeddable_count} Restricted Song${audit.non_embeddable_count === 1 ? '' : 's'}</button>`
                : ''
            }
          </div>
        </div>
      `;
    }
  } catch (err) {
    showToast(esc(err.message));
  } finally {
    const freshBtn = document.getElementById(`audit-embeds-btn-${pid}`);
    if (freshBtn) {
      freshBtn.disabled = false;
      freshBtn.textContent = '🔍 Audit Embeds';
    }
  }
}

async function autoMigrateAuditedPlaylist(playlistId) {
  const pid = Number(playlistId);
  const pl = playlistCache[pid];
  if (!pl || !Array.isArray(pl.tracks)) return;

  const btn = document.getElementById(`pl-auto-migrate-btn-${pid}`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🪄 Migrating Restricted Songs…';
  }

  let migratedCount = 0;
  let failedCount = 0;
  // All swaps of one run share an undo group, so one Ctrl+Z reverts the whole migration.
  const groupId = `mig-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

  for (const t of pl.tracks) {
    const audit = state.embedAuditByVideoId[t.video_id];
    if (!audit || audit.embeddable) continue;
    const query = (audit.suggested_query || `${t.channel_title || ''} ${t.title || ''}`).trim();
    if (!query) continue;
    try {
      const searchRes = await apiWithRetry(`/api/youtube/search?q=${encodeURIComponent(query)}&limit=4`);
      const match = (searchRes.results || []).find((r) => r.video_id !== t.video_id && r.embeddable);
      if (match) {
        await apiWithRetry(`/api/playlists/${pid}/tracks/${t.id}`, {
          method: 'PUT',
          body: JSON.stringify({
            user_id: state.currentUserId,
            new_video_id: match.video_id,
            custom_title: match.title,
            group_id: groupId,
          }),
        });
        state.embedAuditByVideoId[match.video_id] = { embeddable: true, status: 'OK', reason: null };
        migratedCount++;
      }
    } catch (_) {
      failedCount++;
    }
  }

  delete playlistCache[pid];
  await syncQueueWithPlaylistIfActive(pid);
  await refreshBootstrapSilently();
  await renderCurrentRoute();
  const failNote = failedCount > 0 ? ` ${failedCount} track(s) failed (try again in a minute).` : '';
  showToast(
    migratedCount > 0
      ? `✨ Auto-migrated ${migratedCount} track(s) to verified-embeddable versions!${failNote} <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
      : `Could not auto-find replacements — click "⇄ Swap" on individual tracks to search.${failNote}`
  );
}

function cleanTrackTitleForSearch(rawTitle, channelTitle = '') {
  const cleaned = String(rawTitle || '')
    .replace(/\((?:official|music|lyric|audio|video|hd|4k|remastered|visualizer)[^)]*\)/gi, '')
    .replace(/\[(?:official|music|lyric|audio|video|hd|4k|remastered|visualizer)[^\]]*\]/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  const ch = String(channelTitle || '')
    .replace(/\s*-\s*Topic$/i, '')
    .replace(/VEVO$/i, '')
    .trim();
  if (ch && ch !== 'YouTube' && ch !== 'YouTube Artist' && !cleaned.toLowerCase().includes(ch.toLowerCase())) {
    return `${ch} - ${cleaned}`.trim();
  }
  return cleaned || String(rawTitle || '').trim();
}

async function openMigrateSongModal(playlistId, trackId) {
  const pid = Number(playlistId);
  const tid = Number(trackId);
  let pl = playlistCache[pid];
  if (!pl) {
    pl = await api(`/api/playlists/${pid}?user_id=${state.currentUserId}`);
    playlistCache[pid] = pl;
  }
  const track = (pl.tracks || []).find((t) => Number(t.id) === tid);
  if (!track) return;

  document.getElementById('migrate-modal-playlist-id').value = pid;
  document.getElementById('migrate-modal-track-id').value = tid;
  document.getElementById('migrate-modal-custom-url').value = '';

  const auditionBar = document.getElementById('migrate-modal-audition-bar');
  if (auditionBar) {
    auditionBar.classList.add('hidden');
    auditionBar.innerHTML = '';
  }

  const audit = state.embedAuditByVideoId[track.video_id];
  const baseTitleSource = track.migrated_from_title || track.title;
  const baseQuery =
    (audit && audit.suggested_query) ||
    cleanTrackTitleForSearch(baseTitleSource, track.migrated_from_title ? '' : track.channel_title);

  state.migrateModalContext = {
    playlistId: pid,
    trackId: tid,
    track,
    baseQuery,
    auditioningIdx: null,
  };

  const modalTitleEl = document.getElementById('migrate-modal-title');
  if (modalTitleEl) {
    modalTitleEl.textContent = track.migrated_from_video_id
      ? '⇄ Swap Migrated Track for a Different Version'
      : '⇄ Swap Track / Find Different Version';
  }

  document.getElementById('migrate-modal-query').value = baseQuery;

  const migratedOriginHtml = track.migrated_from_video_id
    ? `
      <div style="margin-top:6px; padding-top:6px; border-top:1px dashed var(--border-color); display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; font-size:12px;">
        <div>
          <span class="embed-pill-migrated">⇄ Migrated Track</span>
          <span class="muted" style="margin-left:4px;">Originally migrated from: <strong>${esc(track.migrated_from_title || track.migrated_from_video_id)}</strong> (<code>${esc(track.migrated_from_video_id)}</code>)</span>
        </div>
        <button
          type="button"
          class="btn-tiny btn-secondary"
          onclick="revertMigratedTrackToOriginal()"
          title="Restore original YouTube video ID (${esc(track.migrated_from_video_id)})"
        >↺ Revert to Original (${esc(track.migrated_from_video_id)})</button>
      </div>
    `
    : '';

  document.getElementById('migrate-modal-current').innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
      <div>
        <div style="font-weight:700;">Current Version: ${esc(track.title)}</div>
        <div class="muted" style="font-size:12px;">${esc(track.channel_title)} · Video ID: <code>${esc(track.video_id)}</code></div>
      </div>
      <div style="display:flex; gap:6px; align-items:center;">
        <button
          type="button"
          class="btn-tiny btn-secondary"
          onclick="auditionCurrentTrackInModal()"
          title="Play the current version in the player deck to compare"
        >▶ Hear Current</button>
        ${
          audit && !audit.embeddable
            ? `<span class="embed-pill-warn">⚠ Restricted Embed</span>`
            : `<span class="mono-badge">Find Better Version</span>`
        }
      </div>
    </div>
    ${migratedOriginHtml}
  `;

  const chipsEl = document.getElementById('migrate-modal-chips');
  if (chipsEl) {
    const currentClean = cleanTrackTitleForSearch(track.title, track.channel_title);
    const showCurrentChip =
      track.migrated_from_title &&
      currentClean.toLowerCase() !== baseQuery.toLowerCase();

    chipsEl.innerHTML = `
      <span class="muted">Quick version filters:</span>
      <button type="button" class="btn-tiny btn-secondary" onclick="runMigrateChipSearch('')">🎯 Base Match</button>
      <button type="button" class="btn-tiny btn-secondary" onclick="runMigrateChipSearch('official audio topic')">💿 Official Audio / Topic</button>
      <button type="button" class="btn-tiny btn-secondary" onclick="runMigrateChipSearch('remastered high quality audio')">✨ HQ / Remastered</button>
      <button type="button" class="btn-tiny btn-secondary" onclick="runMigrateChipSearch('live session KEXP Tiny Desk')">🎤 Live Session</button>
      <button type="button" class="btn-tiny btn-secondary" onclick="runMigrateChipSearch('full album studio version')">🎚 Studio Version</button>
      ${
        showCurrentChip
          ? `<button type="button" class="btn-tiny btn-secondary" onclick="runMigrateDirectQuery(${jsArg(currentClean)})">🔎 Search Current Title</button>`
          : ''
      }
    `;
  }

  document.getElementById('migrate-song-modal').classList.remove('hidden');
  await executeMigrateModalSearch(baseQuery, track.video_id);
}

function runMigrateChipSearch(suffix) {
  const ctx = state.migrateModalContext;
  const base = (ctx && ctx.baseQuery) || document.getElementById('migrate-modal-query')?.value.trim() || '';
  if (!base) return;
  const q = suffix ? `${base} ${suffix}`.trim() : base;
  const input = document.getElementById('migrate-modal-query');
  if (input) input.value = q;
  executeMigrateModalSearch(q, ctx?.track?.video_id || null);
}

function runMigrateDirectQuery(query) {
  if (!query) return;
  const input = document.getElementById('migrate-modal-query');
  if (input) input.value = query;
  const ctx = state.migrateModalContext;
  executeMigrateModalSearch(query, ctx?.track?.video_id || null);
}

async function revertMigratedTrackToOriginal() {
  const ctx = state.migrateModalContext;
  if (!ctx || !ctx.track || !ctx.track.migrated_from_video_id) return;
  await performPlaylistTrackMigration(
    ctx.track.migrated_from_video_id,
    ctx.track.migrated_from_title || null
  );
}

function auditionCurrentTrackInModal() {
  const ctx = state.migrateModalContext;
  if (!ctx || !ctx.track) return;
  ctx.auditioningIdx = null;
  ctx.auditionedCandidate = null;
  mountOrLoadYouTubeVideo(ctx.track.video_id, ctx.track.start_seconds, ctx.track.end_seconds);
  const auditionBar = document.getElementById('migrate-modal-audition-bar');
  if (auditionBar) {
    auditionBar.classList.remove('hidden');
    auditionBar.innerHTML = `
      <div>🎧 <strong>Playing current version:</strong> ${esc(ctx.track.title)} (<code>${esc(ctx.track.video_id)}</code>)</div>
      <span class="muted">Click <strong>▶ Audition</strong> on any candidate below to compare quality</span>
    `;
  }
}

function auditionMigrateCandidate(candidateIdx) {
  const items = state.migrateModalCandidates || [];
  const chosen = items[candidateIdx];
  if (!chosen) return;
  if (state.migrateModalContext) {
    state.migrateModalContext.auditioningIdx = candidateIdx;
    state.migrateModalContext.auditionedCandidate = { ...chosen };
  }
  mountOrLoadYouTubeVideo(chosen.video_id);
  const auditionBar = document.getElementById('migrate-modal-audition-bar');
  if (auditionBar) {
    auditionBar.classList.remove('hidden');
    auditionBar.innerHTML = `
      <div>
        🎧 <strong>Auditioning candidate:</strong> ${esc(chosen.title)} <span class="muted">(${esc(chosen.channel_title)} · <code>${esc(chosen.video_id)}</code>)</span>
      </div>
      <div style="display:flex; gap:6px; align-items:center; flex-shrink:0;">
        <button type="button" class="btn-tiny btn-secondary" onclick="auditionCurrentTrackInModal()">↺ Hear Current</button>
        <button type="button" class="btn-tiny btn-secondary" id="migrate-audition-add-btn" onclick="addAuditionedMigrateCandidateToPlaylist()" title="Add this version as a new track in the current playlist">+ Add to Playlist</button>
        <button type="button" class="btn-tiny btn-primary" onclick="applyAuditionedMigrateCandidate()">✓ Swap to This Version</button>
      </div>
    `;
  }
}

async function addAuditionedMigrateCandidateToPlaylist() {
  const chosen = state.migrateModalContext?.auditionedCandidate;
  if (!chosen) return;
  const btn = document.getElementById('migrate-audition-add-btn');
  await performAddTrackFromMigrateModal(chosen.video_id, chosen.title, btn, true);
}

async function applyAuditionedMigrateCandidate() {
  const chosen = state.migrateModalContext?.auditionedCandidate;
  if (!chosen) return;
  await performPlaylistTrackMigration(chosen.video_id, chosen.title);
}

async function handleMigrateModalSearch(e) {
  e.preventDefault();
  const query = document.getElementById('migrate-modal-query')?.value.trim();
  if (!query) return;
  const ctx = state.migrateModalContext;
  await executeMigrateModalSearch(query, ctx?.track?.video_id || null);
}

async function executeMigrateModalSearch(query, excludeVideoId = null) {
  const resultsEl = document.getElementById('migrate-modal-results');
  const btn = document.getElementById('migrate-modal-search-btn');
  if (!resultsEl) return;

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Checking Embeds…';
  }
  resultsEl.innerHTML = `<div class="muted" style="font-size:12px; padding:8px 0;">Searching YouTube &amp; verifying embed playability for <strong>"${esc(query)}"</strong>…</div>`;

  try {
    const data = await api(`/api/youtube/search?q=${encodeURIComponent(query)}&limit=8`);
    const items = (data.results || []).filter((item) => !excludeVideoId || item.video_id !== excludeVideoId);
    state.migrateModalCandidates = items;

    if (items.length === 0) {
      resultsEl.innerHTML = `<div class="muted" style="font-size:12px; padding:8px 0;">No verified embeddable tracks found for "${esc(query)}". Try clicking one of the quick version filters above or adjusting the search terms.</div>`;
      return;
    }

    const origVid = state.migrateModalContext?.track?.migrated_from_video_id;

    resultsEl.innerHTML = `
      <ol class="tracklist" style="border:1px solid var(--border-color); border-radius:3px;">
        ${items
          .map(
            (item, cIdx) => `
            <li class="track-row" style="padding:6px 10px;">
              <div class="track-left">
                <span class="embed-pill-ok">✓ Embed OK</span>
                <div class="track-body">
                  <div class="track-main-line">
                    <strong style="font-size:13px;">${esc(item.title)}</strong>
                    <span class="track-channel">${esc(item.channel_title)}</span>
                    <span class="mono-badge">${esc(item.video_id)}</span>
                    ${origVid && item.video_id === origVid ? `<span class="pill pill-discovery">Original Track</span>` : ''}
                  </div>
                </div>
              </div>
              <div class="track-right">
                <button
                  type="button"
                  class="btn-tiny btn-secondary"
                  onclick="auditionMigrateCandidate(${Number(cIdx)})"
                  title="Audition this version in the YouTube player without losing your queue"
                >▶ Audition</button>
                <button
                  type="button"
                  class="btn-tiny btn-secondary"
                  id="migrate-add-btn-${cIdx}"
                  onclick="addMigrateModalCandidateToPlaylist(${Number(cIdx)})"
                  title="Add this track to the current playlist (keeps your existing track)"
                >+ Add to Playlist</button>
                <button
                  type="button"
                  class="btn-tiny btn-primary"
                  onclick="applyMigrateModalCandidate(${Number(cIdx)})"
                  title="Replace the current track with this version"
                >✓ Swap to This Version</button>
              </div>
            </li>
          `
          )
          .join('')}
      </ol>
    `;
  } catch (err) {
    resultsEl.innerHTML = `<div style="color:var(--accent-warm); font-size:12px;">Search error: ${esc(err.message)}</div>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔍 Search Versions';
    }
  }
}

async function addMigrateModalCandidateToPlaylist(candidateIdx) {
  const items = state.migrateModalCandidates || [];
  const chosen = items[candidateIdx];
  if (!chosen) return;
  const btn = document.getElementById(`migrate-add-btn-${candidateIdx}`);
  await performAddTrackFromMigrateModal(chosen.video_id, chosen.title, btn, true);
}

async function addCustomMigrateModalUrlToPlaylist() {
  const customInput = document.getElementById('migrate-modal-custom-url');
  const customUrl = customInput?.value.trim();
  if (!customUrl) return;
  const ok = await performAddTrackFromMigrateModal(customUrl, null, null, false);
  if (ok && customInput) {
    customInput.value = '';
  }
}

async function performAddTrackFromMigrateModal(
  videoUrlOrId,
  customTitle = null,
  buttonEl = null,
  isVerifiedEmbed = false
) {
  const pid = Number(document.getElementById('migrate-modal-playlist-id')?.value);
  if (!pid) return false;

  if (buttonEl) {
    buttonEl.disabled = true;
    buttonEl.textContent = 'Adding…';
  }

  let res;
  try {
    res = await api(`/api/playlists/${pid}/tracks`, {
      method: 'POST',
      body: JSON.stringify({
        user_id: state.currentUserId,
        urls_or_ids: videoUrlOrId,
        custom_title: customTitle,
      }),
    });
  } catch (err) {
    showToast(esc(err.message));
    if (buttonEl) {
      buttonEl.disabled = false;
      buttonEl.textContent = '+ Add to Playlist';
    }
    return false;
  }

  const added = (res.results && res.results[0]) || null;
  if (isVerifiedEmbed && added && added.video_id) {
    state.embedAuditByVideoId[added.video_id] = { embeddable: true, status: 'OK', reason: null };
  }

  delete playlistCache[pid];
  notifyBadges(res.new_badges);

  const trackTitle = (added && added.title) || customTitle || videoUrlOrId;
  showToast(
    `+ Added "${esc(trackTitle)}" to playlist! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
  );

  if (buttonEl) {
    buttonEl.textContent = '✓ Added!';
    setTimeout(() => {
      if (buttonEl) {
        buttonEl.disabled = false;
        buttonEl.textContent = '+ Add to Playlist';
      }
    }, 1600);
  }

  try {
    await syncQueueWithPlaylistIfActive(pid);
    await refreshBootstrapSilently();
    await renderCurrentRoute();
  } catch (_) {}

  return true;
}

async function applyMigrateModalCandidate(candidateIdx) {
  const items = state.migrateModalCandidates || [];
  const chosen = items[candidateIdx];
  if (!chosen) return;
  await performPlaylistTrackMigration(chosen.video_id, chosen.title);
}

async function applyCustomMigrateModalUrl() {
  const customUrl = document.getElementById('migrate-modal-custom-url')?.value.trim();
  if (!customUrl) return;
  await performPlaylistTrackMigration(customUrl, null);
}

async function performPlaylistTrackMigration(newVideoUrlOrId, customTitle = null) {
  const pid = Number(document.getElementById('migrate-modal-playlist-id')?.value);
  const tid = Number(document.getElementById('migrate-modal-track-id')?.value);
  if (!pid || !tid) return;

  const activeQueueTrack = state.queue[state.queueIndex];
  const wasPlayingThisTrack =
    Number(state.queuePlaylistId) === pid &&
    activeQueueTrack &&
    Number(activeQueueTrack.id) === tid;

  try {
    const res = await api(`/api/playlists/${pid}/tracks/${tid}`, {
      method: 'PUT',
      body: JSON.stringify({
        user_id: state.currentUserId,
        new_video_id: newVideoUrlOrId,
        custom_title: customTitle,
      }),
    });
    if (res.video_id) {
      state.embedAuditByVideoId[res.video_id] = { embeddable: true, status: 'OK', reason: null };
    }
    closeModal('migrate-song-modal');
    delete playlistCache[pid];
    await syncQueueWithPlaylistIfActive(pid);
    if (wasPlayingThisTrack && res.video_id) {
      const updatedTrack = state.queue[state.queueIndex];
      mountOrLoadYouTubeVideo(
        res.video_id,
        updatedTrack ? updatedTrack.start_seconds : null,
        updatedTrack ? updatedTrack.end_seconds : null
      );
      highlightActiveTrackRows();
    }
    await refreshBootstrapSilently();
    await renderCurrentRoute();
    showToast(
      `⇄ Swapped track to "${esc(res.title || res.video_id)}"! <span class="muted" style="font-size:11px;">(Ctrl+Z to undo)</span>`
    );
  } catch (err) {
    showToast(esc(err.message));
  }
}

// --- Initialization ---
// Plain-text clipboard links are real paths (/p/2-slug?track=4, /t/VIDEO_ID?s=&e=); the SPA routes on hashes.
function mapPathRouteToHash() {
  const path = window.location.pathname || '/';
  let hash = '';
  const p = path.match(/^\/p\/(\d{1,18})(?:-[A-Za-z0-9_-]*)?\/?$/);
  const t = path.match(/^\/t\/([A-Za-z0-9_-]{11})\/?$/);
  if (p) hash = `#playlist/${p[1]}`;
  else if (t) hash = `#song/${t[1]}`;
  if (hash) history.replaceState(null, '', `/${hash}`);
}

async function initApp() {
  // YouTube's embed validator blocks many music partner videos when Referer/Origin is a raw IP (127.0.0.1)
  // with Error 150, but allows them when accessed via hostname (localhost).
  if (window.location.hostname === '127.0.0.1') {
    const targetUrl = window.location.href.replace('://127.0.0.1', '://localhost');
    window.location.replace(targetUrl);
    return;
  }

  applyAudioOnlyModeUI();
  mapPathRouteToHash();
  state.currentRoute = parseHashRoute();
  updateNavHighlight(state.currentRoute.view);
  await refreshBootstrapSilently();
  if (window.location.hash === '#not-invited') {
    history.replaceState(null, '', '#feed');
    state.currentRoute = parseHashRoute();
    showToast('That Google account is not on the Webjam invite list yet. Ask a friend to invite you.');
  } else if (window.location.hash === '#auth-error') {
    history.replaceState(null, '', '#feed');
    state.currentRoute = parseHashRoute();
    showToast('Google sign-in was cancelled or failed. Please try again.');
  }
  await renderCurrentRoute();
}

document.addEventListener('DOMContentLoaded', initApp);


