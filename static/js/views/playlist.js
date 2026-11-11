// A playlist page, read-only (F); Lane A owns it afterwards and adds editing.
// Also renders /crate (the viewer's own crate; anonymous visitors get a sign-in line).
//
// Slots: playlist.actions (♡, Share), playlist.menu (⋯ items), playlist.related (the related panel),
// playlist.footer (comments). The crate shows none of them.

import { html, nothing } from '../lit.js';
import { state, update, rowsOf, loadPlaylist, isMine } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { replaceUrl } from '../router.js';
import { trackList } from '../components/track-row.js';
import { empty } from '../components/empty.js';
import { playlistUrl, userUrl, relTime, isoTs, plural } from '../format.js';

const FRESH_MS = 30000;
const related = new Map(); // playlistId -> { open, items, error, loading } (F's interim list)
let rerender = () => {};

export function playlistIdFrom(ctx) {
  if (ctx.name === 'crate') return state.auth.crateId;
  const m = /^(\d{1,18})(?:-.*)?$/.exec(ctx.params.ref || '');
  return m ? Number(m[1]) : null;
}

export async function load(ctx) {
  rerender = ctx.rerender;
  if (ctx.name === 'crate' && !state.auth.signedIn) return;
  const id = playlistIdFrom(ctx);
  if (!id) throw new api.ApiError(404, 'playlist_not_found', 'Playlist not found');
  const have = state.playlists.get(id);
  await loadPlaylist(id, { force: !have || Date.now() - have.loadedAt > FRESH_MS });
  const p = state.playlists.get(id);
  if (ctx.name !== 'crate' && p && p.kind === 'crate') {
    replaceUrl('/crate');
  } else if (ctx.name !== 'crate' && p) {
    const want = playlistUrl(id, p.meta.title).split('?')[0];
    if (location.pathname !== want) replaceUrl(want + location.search + location.hash);
  }
  const tid = Number(ctx.query.track);
  if (tid && p && p.order.includes(tid)) {
    update((s) => {
      s.selection = { playlistId: id, ids: new Set([tid]), anchorId: tid, cursorId: tid, insertAfterId: tid };
    }, 'select');
    requestAnimationFrame(() => {
      const row = document.querySelector(`#view [data-track-id="${tid}"]`);
      if (row) {
        const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
        row.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
        row.focus({ preventScroll: true });
      }
    });
  }
  if (ctx.hash === 'related' && !slots.has('playlist.related')) openRelated(id);
}

export function title(ctx) {
  const id = playlistIdFrom(ctx);
  const p = id && state.playlists.get(id);
  if (!p) return ctx.name === 'crate' ? 'Crate' : 'Playlist';
  return p.kind === 'crate' ? 'Crate' : p.meta.title;
}

async function openRelated(id) {
  const cur = related.get(id) || {};
  related.set(id, { ...cur, open: true, loading: !cur.items });
  rerender();
  if (cur.items) return;
  try {
    const res = await api.get(`/api/playlists/${id}/related`, { limit: 12 });
    related.set(id, { open: true, items: res.related || [], loading: false });
  } catch (err) {
    related.set(id, { open: true, items: [], loading: false, error: err.status === 404 ? 'soon' : err.detail });
  }
  rerender();
}

function relatedInterim(id, count) {
  const r = related.get(id) || {};
  const label = `${plural(count, 'playlist')} share${count === 1 ? 's' : ''} these tracks`;
  return html`<div class="related-line" id="related">
    <button type="button" class="link-btn" data-action="pl-related-basic" data-playlist-id=${id}
      aria-expanded=${r.open ? 'true' : 'false'}>${label}</button>
    ${r.open
      ? r.loading
        ? html`<p class="loading">Loading…</p>`
        : r.error
          ? html`<p class="quiet-line">${r.error === 'soon' ? 'Related playlists are on their way.' : r.error}</p>`
          : html`<ul class="lines section">${(r.items || []).map((it) => html`<li class="line">
              <span class="line-text"><a href=${playlistUrl(it.playlist.id, it.playlist.title)}>${it.playlist.title}</a>
                ${it.playlist.user ? html`<span class="muted"> by </span><a class="hop" href=${userUrl(it.playlist.user.username)}>@${it.playlist.user.username}</a>` : nothing}</span>
              <span class="num muted">${it.shared_count} shared</span>
            </li>`)}</ul>`
      : nothing}
  </div>`;
}

function signedOutCrate() {
  return html`<div class="page">
    <h1 tabindex="-1">Crate</h1>
    <p class="intro">Your crate is a private place to collect tracks while you browse.
      <a href="/auth/google/login">Sign in</a> to start one.</p>
  </div>`;
}

export function render(ctx) {
  if (ctx.name === 'crate' && !state.auth.signedIn) return signedOutCrate();
  const id = playlistIdFrom(ctx);
  const p = id && state.playlists.get(id);
  if (!p) return html`<div class="page"><p class="loading">Loading…</p></div>`;
  const meta = p.meta;
  const crate = p.kind === 'crate';
  const mine = isMine(id);
  const rows = rowsOf(id);
  const sctx = { ...ctx, playlistId: id, playlist: meta, mine, crate, rows };
  const relatedCount = Number(p.related_count ?? meta.related_count) || 0;
  const vis = meta.visibility;
  return html`<div class="page playlist" data-playlist-id=${id}>
    <header>
      <h1 tabindex="-1">${crate ? 'Crate' : meta.title}</h1>
      <p class="byline">
        ${crate
          ? html`<span>Private · only you can see it</span>`
          : html`by <a class="hop" href=${userUrl(meta.username)}>@${meta.username}</a>`}
        <span class="dot-sep">${plural(rows.length, 'track')}</span>
        ${meta.updated_at ? html`<span class="dot-sep"><time datetime=${isoTs(meta.updated_at)}>updated ${relTime(meta.updated_at)}</time></span>` : nothing}
        ${mine && !crate && vis && vis !== 'public' ? html`<span class="dot-sep"><span class="tag">${vis}</span></span>` : nothing}
      </p>
      ${meta.forked_from
        ? html`<p class="byline">copied from <a class="hop" href=${playlistUrl(meta.forked_from.playlist_id, meta.forked_from.title)}>‘${meta.forked_from.title}’</a>
            by <a class="hop" href=${userUrl(meta.forked_from.username)}>@${meta.forked_from.username}</a></p>`
        : nothing}
      ${meta.description && !crate ? html`<p class="desc">${meta.description}</p>` : nothing}
      <div class="page-actions">
        <button type="button" class="btn btn-primary" data-action="play-playlist" data-playlist-id=${id} ?disabled=${!rows.length}>
          <svg class="i"><use href="#i-play"></use></svg>Play</button>
        ${crate ? slots.render('crate.actions', { crateId: id, rows }) : slots.render('playlist.actions', sctx)}
        <button type="button" class="icon-btn" data-action="menu-open" data-menu="playlist" data-playlist-id=${id}
          aria-label="More playlist actions" aria-haspopup="menu" aria-expanded="false">
          <svg class="i"><use href="#i-more"></use></svg></button>
      </div>
      ${!crate && relatedCount > 0
        ? slots.has('playlist.related') ? slots.render('playlist.related', { ...sctx, relatedCount }) : relatedInterim(id, relatedCount)
        : nothing}
    </header>
    <section class="section" aria-label="Tracks">
      ${rows.length
        ? trackList(rows, {
          key: crate ? 'crate-page' : `pl:${id}`,
          context: crate ? 'crate' : 'playlist',
          editable: mine,
          playlistId: id,
          label: crate ? 'Crate' : meta.title,
          source: { playlistId: id, title: crate ? 'Crate' : meta.title, kind: p.kind },
        })
        : empty(crate ? 'Your crate is empty. Paste a YouTube link anywhere, or press + on any track.' : 'No tracks yet.')}
    </section>
    ${crate ? nothing : html`<div class="section">${slots.render('playlist.footer', sctx)}</div>`}
  </div>`;
}

export function unmount() {
  related.clear();
}

registerActions({
  'pl-related-basic': (el, e, ctx) => {
    const id = Number(el.dataset.playlistId || ctx.playlistId);
    const r = related.get(id);
    if (r && r.open) {
      related.set(id, { ...r, open: false });
      rerender();
    } else openRelated(id);
  },
});
