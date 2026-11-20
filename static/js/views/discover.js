// Discover (/discover, and "/" when anonymous; §2.4). Tabs Playlists · Tracks · People
// (underline style). Playlists sort fresh | popular. "More" pages by 20 (next_offset).
// Recent activity sits in the right rail on wide screens and at the foot of the page otherwise.
// When signed in, a quiet "Shares tracks with yours" section lists listed playlists that share
// tracks with mine and my taste neighbours (fetched once in the background). No badges here.

import { html, nothing, repeat } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { playlistCard } from '../components/playlist-card.js';
import { trackList } from '../components/track-row.js';
import { avatar } from '../components/avatar.js';
import { empty } from '../components/empty.js';
import { toast } from '../ui/toast.js';
import { userUrl, playlistUrl, localUrl, plural, relTime, isoTs } from '../format.js';
import { activityLine, moreButton, redraw } from '../discovery/util.js';

const TABS = ['playlists', 'tracks', 'people'];
const SORTS = ['fresh', 'popular'];

let view = null; // { tab, sort, items, next, loadingMore, activity }
// For a signed-in viewer: taste neighbours and listed playlists that share tracks with mine.
let mine = null; // { userId, neighbours, related: [{ playlist, shared_count, via }], loading }
let loadSeq = 0;

function params(ctx) {
  const tab = TABS.includes(ctx.query.tab) ? ctx.query.tab : 'playlists';
  const sort = SORTS.includes(ctx.query.sort) ? ctx.query.sort : 'fresh';
  return { tab, sort };
}

function itemsOf(d, tab) {
  return (tab === 'tracks' ? d.tracks : tab === 'people' ? d.people : d.playlists) || [];
}

async function fetchPage(tab, sort, offset) {
  const q = { tab, offset, limit: 20 };
  if (tab === 'playlists') q.sort = sort;
  return api.get('/api/discover', q);
}

export async function load(ctx) {
  const { tab, sort } = params(ctx);
  const seq = ++loadSeq;
  const jobs = [fetchPage(tab, sort, 0)];
  // Activity only comes with the first page of the playlists tab; other tabs fetch it once.
  const needActivity = tab !== 'playlists' && !(view && view.activity && view.activity.length);
  if (needActivity) jobs.push(fetchPage('playlists', 'fresh', 0).catch(() => null));
  if (state.auth.signedIn && (!mine || mine.userId !== state.auth.user.id)) loadMine();
  const [d, act] = await Promise.all(jobs);
  if (seq !== loadSeq) return;
  const activity = tab === 'playlists' ? d.activity : act ? act.activity : view && view.activity;
  view = { tab, sort, items: itemsOf(d, tab), next: d.next_offset ?? null, loadingMore: false, activity: activity || [] };
}

export function title(ctx) {
  return ctx.name === 'home' ? '' : 'Discover';
}

function tabHref(tab, sort) {
  if (tab === 'playlists') return sort === 'popular' ? '/discover?sort=popular' : '/discover';
  return `/discover?tab=${tab}`;
}

function tabsTpl(tab) {
  const label = { playlists: 'Playlists', tracks: 'Tracks', people: 'People' };
  return html`<nav class="dc-tabs" aria-label="Discover">
    ${TABS.map((t) => html`<a class="dc-tab" href=${localUrl(tabHref(t, 'fresh'))} aria-current=${t === tab ? 'page' : nothing}>${label[t]}</a>`)}
  </nav>`;
}

function sortTpl(sort) {
  return html`<p class="dc-sort" aria-label="Sort">
    <a class=${sort === 'fresh' ? 'is-on' : ''} href=${localUrl(tabHref('playlists', 'fresh'))} aria-current=${sort === 'fresh' ? 'true' : nothing}>Fresh</a>
    <a class=${sort === 'popular' ? 'is-on' : ''} href=${localUrl(tabHref('playlists', 'popular'))} aria-current=${sort === 'popular' ? 'true' : nothing}>Popular</a>
  </p>`;
}

function trackRows(items) {
  return items.map((t) => ({
    id: `d:${t.video_id}`,
    video_id: t.video_id,
    title: t.title,
    channel_title: t.channel_title,
    thumbnail_url: t.thumbnail_url,
    playlist_count: t.playlist_count,
    found_by: t.finder ? t.finder.username : null,
  }));
}

function peopleTpl(items) {
  return html`<ul class="dc-people">${repeat(items, (p) => p.user.id, (p) => html`<li class="dc-person">
    ${avatar(p.user)}
    <div class="dc-person-main">
      <p><a class="dc-person-name" href=${userUrl(p.user.username)}>${p.user.display_name || `@${p.user.username}`}</a>
        <a class="hop" href=${userUrl(p.user.username)}>@${p.user.username}</a></p>
      ${p.bio ? html`<p class="dc-person-bio">${p.bio}</p>` : nothing}
    </div>
    <p class="dc-person-meta">${plural(p.playlist_count, 'playlist')} · ${plural(p.track_count, 'track')}${p.last_active_at
      ? html` · <time datetime=${isoTs(p.last_active_at)}>${relTime(p.last_active_at)}</time>` : nothing}</p>
  </li>`)}</ul>`;
}

function activityTpl(where) {
  const lines = (view && view.activity) || [];
  if (!lines.length) return nothing;
  return html`<section class="section dc-activity dc-activity-${where}" aria-labelledby=${`dc-h-act-${where}`}>
    <h2 class="label" id=${`dc-h-act-${where}`}>Recently</h2>
    <ul class="lines">${lines.slice(0, 15).map(activityLine)}</ul>
  </section>`;
}

/** Background: my neighbours (from /api/me/home) and, for up to three of my playlists that have
 *  related ones, the listed playlists sharing their tracks (merged, mine excluded). */
async function loadMine() {
  const uid = state.auth.user.id;
  mine = { userId: uid, neighbours: [], related: [], loading: true };
  const m = mine;
  try {
    const home = await api.get('/api/me/home', {}, { quiet: true });
    m.neighbours = (home.neighbours || []).slice(0, 6);
    const sources = (home.playlists || []).filter((p) => Number(p.related_count) > 0).slice(0, 3);
    const pages = await Promise.all(sources.map((p) =>
      api.get(`/api/playlists/${p.id}/related`, { limit: 6 }, { quiet: true }).then((r) => ({ via: p, r })).catch(() => null)));
    const merged = new Map();
    for (const page of pages) {
      if (!page) continue;
      for (const it of page.r.related || []) {
        const pl = it.playlist;
        if (!pl || (pl.user && pl.user.id === uid)) continue;
        const have = merged.get(pl.id);
        if (!have) merged.set(pl.id, { playlist: pl, shared_count: Number(it.shared_count) || 0, via: page.via });
        else if (Number(it.shared_count) > have.shared_count) Object.assign(have, { shared_count: Number(it.shared_count), via: page.via });
      }
    }
    m.related = [...merged.values()].sort((a, b) => b.shared_count - a.shared_count).slice(0, 6);
  } catch {
    /* quiet: this section is a bonus */
  } finally {
    m.loading = false;
    if (mine === m) redraw('dc-discover');
  }
}

function mineTpl() {
  if (!state.auth.signedIn || !mine || mine.userId !== state.auth.user.id) return nothing;
  const { related, neighbours } = mine;
  if (!related.length && !neighbours.length) return nothing;
  return html`<section class="section dc-mine" aria-labelledby="dc-h-mine">
    <h2 class="label" id="dc-h-mine">Shares tracks with yours</h2>
    ${related.length
      ? html`<ul class="lines">${related.map((it) => html`<li class="line"><span class="line-text">
          <a href=${playlistUrl(it.playlist.id, it.playlist.title)}>${it.playlist.title}</a>
          ${it.playlist.user ? html` <a class="hop" href=${userUrl(it.playlist.user.username)}>by @${it.playlist.user.username}</a>` : nothing}
          <span class="muted"> · with </span><a class="hop" href=${playlistUrl(it.via.id, it.via.title, { hash: 'related' })}>‘${it.via.title}’</a></span>
          <span class="num muted">${it.shared_count} shared</span></li>`)}</ul>`
      : nothing}
    ${neighbours.length
      ? html`<ul class="people dc-mine-people">${neighbours.map((n) => html`<li class="person">
          ${avatar(n.user, { small: true })}
          <a href=${userUrl(n.user.username)}>@${n.user.username}</a>
          <span class="count">${n.shared_count} shared</span>
        </li>`)}</ul>`
      : nothing}
  </section>`;
}

export function render(ctx) {
  const anon = !state.auth.signedIn;
  const { tab, sort } = params(ctx);
  const v = view && view.tab === tab && (tab !== 'playlists' || view.sort === sort) ? view : null;
  let body;
  if (!v) body = html`<p class="loading">Loading…</p>`;
  else if (tab === 'tracks') {
    body = v.items.length
      ? trackList(trackRows(v.items), { key: 'discover:tracks', context: 'search', label: 'Recent discoveries' })
      : empty('No tracks yet.');
  } else if (tab === 'people') {
    body = v.items.length ? peopleTpl(v.items) : empty('Nobody has shared a playlist yet.');
  } else {
    body = v.items.length
      ? html`<div class="cards">${repeat(v.items, (p) => p.id, (p) => playlistCard(p))}</div>`
      : empty('No public playlists yet.');
  }
  return html`<div class="page discover dc-discover">
    ${state.ui.notice ? html`<p class="notice" role="status">${state.ui.notice}</p>` : nothing}
    <h1 tabindex="-1">Discover</h1>
    ${anon ? html`<p class="intro">Playlists of YouTube music, made by people. Press play; no account needed.</p>` : nothing}
    ${tab === 'playlists' ? mineTpl() : nothing}
    <div class="dc-tabbar">
      ${tabsTpl(tab)}
      ${tab === 'playlists' ? sortTpl(sort) : nothing}
    </div>
    <section class="dc-tabpanel" aria-label=${tab}>
      ${body}
      ${v && v.next != null ? moreButton('discover-more', { busy: v.loadingMore }) : nothing}
    </section>
    ${activityTpl('inline')}
  </div>`;
}

export function unmount() {
  if (state.ui.notice) update((s) => { s.ui.notice = null; }, 'notice');
}

// Recent activity in the right rail (≥ 1100px) while Discover is on screen.
slots.register('rail.context', ({ route } = {}) => {
  const r = route || state.route;
  const here = r && (r.name === 'discover' || (r.name === 'home' && !state.auth.signedIn));
  if (!here) return null;
  return activityTpl('rail');
});

registerActions({
  'discover-more': async () => {
    const v = view;
    if (!v || v.next == null || v.loadingMore) return;
    v.loadingMore = true;
    redraw('dc-discover');
    try {
      const d = await fetchPage(v.tab, v.sort, v.next);
      if (view !== v) return;
      const idOf = v.tab === 'tracks' ? (x) => x.video_id : v.tab === 'people' ? (x) => x.user.id : (x) => x.id;
      const seen = new Set(v.items.map(idOf));
      v.items = v.items.concat(itemsOf(d, v.tab).filter((x) => !seen.has(idOf(x))));
      v.next = d.next_offset ?? null;
    } catch (err) {
      if (!err.handled) toast.show(err.detail || 'Could not load more.');
    } finally {
      v.loadingMore = false;
      redraw('dc-discover');
    }
  },
});
