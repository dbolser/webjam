// A playlist page. Everyone can select, copy, drag out and add to the crate; the owner edits in
// place (Lane A): click / Shift / ⌘ selection, ⌘C then V V V, drag to reorder or onto the dock,
// G to move to a position, T for note and clip, Delete to remove, ⌘Z to undo, and "Edit details"
// for title, description and visibility. Also renders /crate (the viewer's own crate; anonymous
// visitors get a sign-in line).
//
// Slots: playlist.actions (♡, Share), playlist.menu (⋯ items), playlist.related (the related panel),
// playlist.footer (comments). The crate shows none of them.

import { html, nothing } from '../lit.js';
import { state, update, rowsOf, loadPlaylist, isMine } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { replaceUrl } from '../router.js';
import { trackList, registerList } from '../components/track-row.js';
import { isEditingMeta, metaForm, resetMetaEditor } from '../editing/inline-edit.js';
import { undoButtons } from '../editing/undo.js';
import { empty } from '../components/empty.js';
import { playlistUrl, userUrl, relTime, isoTs, plural, signInUrl, localUrl } from '../format.js';

const FRESH_MS = 30000;

export function playlistIdFrom(ctx) {
  if (ctx.name === 'crate') return state.auth.crateId;
  const m = /^(\d{1,18})(?:-.*)?$/.exec(ctx.params.ref || '');
  return m ? Number(m[1]) : null;
}

export async function load(ctx) {
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
}

export function title(ctx) {
  const id = playlistIdFrom(ctx);
  const p = id && state.playlists.get(id);
  if (!p) return ctx.name === 'crate' ? 'Crate' : 'Playlist';
  return p.kind === 'crate' ? 'Crate' : p.meta.title;
}

function signedOutCrate() {
  return html`<div class="page">
    <h1 tabindex="-1">Crate</h1>
    <p class="intro">Your crate is a private place to collect tracks while you browse.
      <a href=${localUrl(signInUrl('/crate'))} rel="external">Sign in</a> to start one.</p>
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
  const editingMeta = mine && !crate && isEditingMeta(id);
  const listKey = crate ? 'crate-page' : `pl:${id}`;
  return html`<div class="page playlist" data-playlist-id=${id}>
    <header>
      ${editingMeta ? metaForm(id) : html`<h1 tabindex="-1" class=${mine && !crate ? 'ed-title' : ''}
        data-action=${mine && !crate ? 'edit-meta-open' : nothing} data-playlist-id=${mine && !crate ? id : nothing}
        title=${mine && !crate ? 'Edit details' : nothing}>${crate ? 'Crate' : meta.title}</h1>`}
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
      ${meta.description && !crate && !editingMeta ? html`<p class="desc">${meta.description}</p>` : nothing}
      <div class="page-actions">
        <button type="button" class="btn btn-primary" data-action="play-playlist" data-playlist-id=${id} ?disabled=${!rows.length}>
          <svg class="i"><use href="#i-play"></use></svg>Play</button>
        ${crate ? slots.render('crate.actions', { crateId: id, rows }) : slots.render('playlist.actions', sctx)}
        <button type="button" class="icon-btn" data-action="menu-open" data-menu="playlist" data-playlist-id=${id}
          aria-label="More playlist actions" aria-haspopup="menu" aria-expanded="false">
          <svg class="i"><use href="#i-more"></use></svg></button>
        ${mine ? undoButtons() : nothing}
      </div>
      ${!crate && relatedCount > 0 ? slots.render('playlist.related', { ...sctx, relatedCount }) : nothing}
    </header>
    <section class="section" aria-label="Tracks">
      ${rows.length
        ? trackList(rows, {
          key: listKey,
          context: crate ? 'crate' : 'playlist',
          editable: mine,
          playlistId: id,
          label: crate ? 'Crate' : meta.title,
          source: { playlistId: id, title: crate ? 'Crate' : meta.title, kind: p.kind },
        })
        : mine
          ? emptyEditable(id, listKey, crate, meta.title)
          : empty('No tracks yet.')}
    </section>
    ${crate ? nothing : html`<div class="section">${slots.render('playlist.footer', sctx)}</div>`}
  </div>`;
}

/** An empty playlist of mine is still a focusable, editable list, so ⌘V / V and drops land in it. */
function emptyEditable(id, key, crate, title) {
  registerList(key, [], { playlistId: id, title: crate ? 'Crate' : title, kind: crate ? 'crate' : 'playlist' });
  return html`<ol class="tracks ed-empty-list" role="listbox" aria-label=${crate ? 'Crate' : title} tabindex="0"
      data-list-key=${key} data-context=${crate ? 'crate' : 'playlist'} data-playlist-id=${id} data-editable>
    <li class="ed-empty" role="presentation">${crate
      ? 'Your crate is empty. Paste a YouTube link anywhere, or press + on any track.'
      : 'No tracks yet. Paste links here, drag tracks in, or send them from any list with a dock number.'}</li>
    <li class="insert-line" role="presentation" aria-hidden="true" hidden></li>
  </ol>`;
}

export function unmount() {
  resetMetaEditor();
}
