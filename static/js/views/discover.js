// Discover (F's minimal version; Lane B replaces this file with the tabbed view).
// Anonymous "/" lands here: listed playlist cards with three preview tracks, paged by 20.
// Uses /api/discover and falls back to /api/feed until the backend lane lands it.

import { html, nothing, repeat } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import { registerActions } from '../actions.js';
import { playlistCard, feedCard } from '../components/playlist-card.js';
import { empty } from '../components/empty.js';

let cards = [];
let nextOffset = null;
let loadingMore = false;
let rerender = () => {};

async function page(offset) {
  try {
    const d = await api.get('/api/discover', { tab: 'playlists', sort: 'fresh', offset, limit: 20 });
    return { playlists: d.playlists || [], next: d.next_offset ?? null };
  } catch (err) {
    if (!(err instanceof api.ApiError) || err.status !== 404) throw err;
    const feed = await api.get('/api/feed');
    const all = (feed.playlists || []).filter((p) => (p.track_count ?? (p.tracks || []).length) > 0).map(feedCard);
    const slice = all.slice(offset, offset + 20);
    return { playlists: slice, next: offset + 20 < all.length ? offset + 20 : null };
  }
}

export async function load(ctx) {
  rerender = ctx.rerender;
  const first = await page(0);
  cards = first.playlists;
  nextOffset = first.next;
}

export function title() {
  return 'Discover';
}

export function render() {
  const anon = !state.auth.signedIn;
  return html`<div class="page discover">
    ${state.ui.notice ? html`<p class="notice" role="status">${state.ui.notice}</p>` : nothing}
    <h1 tabindex="-1">Discover</h1>
    ${anon ? html`<p class="intro">Playlists of YouTube music, made by people. Press play; no account needed.</p>` : nothing}
    <section class="section" aria-label="Playlists">
      ${cards.length
        ? html`<div class="cards">${repeat(cards, (p) => p.id, (p) => playlistCard(p))}</div>`
        : empty('No public playlists yet.')}
      ${nextOffset != null
        ? html`<p class="more-row"><button type="button" class="btn" data-action="discover-more" ?disabled=${loadingMore}>More</button></p>`
        : nothing}
    </section>
  </div>`;
}

export function unmount() {
  if (state.ui.notice) update((s) => { s.ui.notice = null; }, 'notice');
}

registerActions({
  'discover-more': async () => {
    if (nextOffset == null || loadingMore) return;
    loadingMore = true;
    rerender();
    try {
      const more = await page(nextOffset);
      const seen = new Set(cards.map((c) => c.id));
      cards = cards.concat(more.playlists.filter((c) => !seen.has(c.id)));
      nextOffset = more.next;
    } finally {
      loadingMore = false;
      rerender();
    }
  },
});
