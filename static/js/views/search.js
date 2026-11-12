// Search (/search?q=, §2.4): Playlists, Tracks and People on webjam (GET /api/search), 10 each with
// "More" (up to 25). Signed-in visitors also get a quiet "On YouTube" section whose button calls
// /api/youtube/search only when pressed, because every search spends shared quota. Results are
// track rows with + (crate) and, in the ⋯ menu, "Send to…" (editing/index.js).

import { html, nothing, repeat } from '../lit.js';
import { state } from '../store.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { trackList } from '../components/track-row.js';
import { playlistCard } from '../components/playlist-card.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import { toast } from '../ui/toast.js';
import { userUrl, plural } from '../format.js';
import { redraw, askSignIn } from '../discovery/util.js';

const FIRST = 10;
const MAX = 25;

let s = null; // { q, playlists, tracks, people, full: Set, more: Set, error, yt: { state, results, error } }

function cleanQuery(q) {
  return String(q || '').replace(/\s+/g, ' ').trim();
}

export async function load(ctx) {
  const q = cleanQuery(ctx.query.q);
  const next = { q, playlists: [], tracks: [], people: [], full: new Set(), more: new Set(), error: '', yt: { state: 'idle', results: [], error: '' } };
  if (!q) {
    s = next;
    return;
  }
  if (q.length > 100) {
    next.error = 'That search is too long. Try fewer words.';
    s = next;
    return;
  }
  try {
    const res = await api.get('/api/search', { q, type: 'all', limit: FIRST });
    next.playlists = res.playlists || [];
    next.tracks = res.tracks || [];
    next.people = res.people || [];
  } catch (err) {
    next.error = err.status === 429 ? 'Searching a lot. Wait a moment and try again.' : err.detail || 'Search failed.';
  }
  s = next;
}

export function title(ctx) {
  const q = cleanQuery(ctx.query.q);
  return q ? `Search: ${q}` : 'Search';
}

function trackRows(list, prefix) {
  return list.map((t) => ({
    id: `${prefix}:${t.video_id}`,
    video_id: t.video_id,
    title: t.title,
    channel_title: t.channel_title,
    thumbnail_url: t.thumbnail_url,
    playlist_count: t.playlist_count,
  }));
}

function moreTpl(group, list) {
  if (s.full.has(group) || list.length < FIRST) return nothing;
  const busy = s.more.has(group);
  return html`<p class="more-row"><button type="button" class="btn btn-quiet" data-action="search-more" data-group=${group}
    ?disabled=${busy}>${busy ? 'Loading…' : 'More'}</button></p>`;
}

function youtubeTpl() {
  if (!state.auth.signedIn) return nothing;
  const yt = s.yt;
  return html`<section class="section dc-yt-section" aria-labelledby="dc-h-yt">
    <h2 class="label" id="dc-h-yt">On YouTube</h2>
    ${yt.state === 'done'
      ? yt.results.length
        ? trackList(trackRows(yt.results, 'yt'), { key: 'search:youtube', context: 'search', label: 'YouTube results' })
        : html`<p class="quiet-line">Nothing playable on YouTube for that.</p>`
      : html`<p class="dc-yt-ask">
          <button type="button" class="btn" data-action="search-youtube" ?disabled=${yt.state === 'loading'}>
            <svg class="i"><use href="#i-search"></use></svg>${yt.state === 'loading' ? 'Searching YouTube…' : `Search YouTube for ‘${s.q}’`}</button>
          ${yt.error ? html`<span class="quiet-line">${yt.error}</span>` : nothing}
        </p>`}
  </section>`;
}

export function render(ctx) {
  const q = cleanQuery(ctx.query.q);
  if (!s || s.q !== q) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  if (!q) {
    return html`<div class="page dc-search">
      <h1 tabindex="-1">Search</h1>
      <p class="intro">Type in the box above: playlists, tracks and people. Paste a YouTube link to collect it.</p>
    </div>`;
  }
  const none = !s.error && !s.playlists.length && !s.tracks.length && !s.people.length;
  return html`<div class="page dc-search">
    <h1 tabindex="-1">‘${q}’</h1>
    ${s.error ? html`<p class="quiet-line">${s.error}</p>` : nothing}
    ${none ? html`<p class="quiet-line">Nothing on webjam matches that yet.${state.auth.signedIn ? ' Try YouTube below.' : ''}</p>` : nothing}

    ${s.playlists.length
      ? html`<section class="section" aria-labelledby="dc-h-sp">
          <h2 class="label" id="dc-h-sp">Playlists</h2>
          <div class="cards">${repeat(s.playlists, (p) => p.id, (p) => playlistCard(p))}</div>
          ${moreTpl('playlists', s.playlists)}
        </section>`
      : nothing}

    ${s.tracks.length
      ? html`<section class="section" aria-labelledby="dc-h-st">
          <h2 class="label" id="dc-h-st">Tracks</h2>
          ${trackList(trackRows(s.tracks, 's'), { key: 'search:tracks', context: 'search', label: 'Tracks' })}
          ${moreTpl('tracks', s.tracks)}
        </section>`
      : nothing}

    ${s.people.length
      ? html`<section class="section" aria-labelledby="dc-h-su">
          <h2 class="label" id="dc-h-su">People</h2>
          <ul class="people">${s.people.map((p) => html`<li class="person">
            ${avatar(p.user, { small: true })}
            <a href=${userUrl(p.user.username)}>${p.user.display_name || `@${p.user.username}`}</a>
            <span class="count">@${p.user.username} · ${plural(p.playlist_count, 'playlist')}</span>
          </li>`)}</ul>
          ${moreTpl('people', s.people)}
        </section>`
      : nothing}

    ${s.error ? nothing : youtubeTpl()}
    ${!state.auth.signedIn && none ? empty('Sign in to search YouTube and collect what you find.') : nothing}
  </div>`;
}

registerActions({
  'search-more': async (el) => {
    const group = el.dataset.group;
    const cur = s;
    if (!cur || !['playlists', 'tracks', 'people'].includes(group) || cur.more.has(group)) return;
    cur.more.add(group);
    redraw('dc-search');
    try {
      const res = await api.get('/api/search', { q: cur.q, type: group, limit: MAX });
      if (s !== cur) return;
      cur[group] = res[group] || cur[group];
      cur.full.add(group);
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not load more.');
    } finally {
      cur.more.delete(group);
      redraw('dc-search');
    }
  },
  'search-youtube': async () => {
    const cur = s;
    if (!cur || !cur.q || cur.yt.state === 'loading') return;
    if (!state.auth.signedIn) {
      askSignIn({ action: 'search-youtube' }, 'search YouTube');
      return;
    }
    cur.yt = { state: 'loading', results: [], error: '' };
    redraw('dc-search');
    try {
      const res = await api.get('/api/youtube/search', { q: cur.q.slice(0, 200), limit: 8 });
      if (s !== cur) return;
      cur.yt = { state: 'done', results: (res.results || []).filter((r) => r && r.video_id && r.embeddable !== false), error: '' };
    } catch (err) {
      cur.yt = { state: 'idle', results: [], error: err.status === 503 || err.status === 429 ? err.detail : (err.detail || 'YouTube search failed.') };
    } finally {
      redraw('dc-search');
    }
  },
});
