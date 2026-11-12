// "N playlists share these tracks" (§2.4, §3.5): the Webjay "shares these links" view.
// One --ink-3 line under the playlist header. Opening it shows the related playlists in the right
// rail on wide screens (rail.context) and inline under the header otherwise (CSS picks which).
// Each related playlist expands to show the shared tracks, playable.
//
// The panel only ever names LISTED playlists (the server guarantees it); it works for my private
// playlists too, because I can already see the source.

import { html, nothing, repeat } from '../lit.js';
import { state, onChange, rowsOf } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { compactList } from '../components/track-row.js';
import { playlistUrl, userUrl, plural } from '../format.js';
import { redraw } from './util.js';

const PAGE = 12;
const panels = new Map(); // playlistId -> { open, items, total, next, loading, more, error, expanded: Set }

function panel(id) {
  let p = panels.get(id);
  if (!p) {
    p = { open: false, items: null, total: 0, next: null, loading: false, more: false, error: '', expanded: new Set(), sig: null };
    panels.set(id, p);
  }
  return p;
}

async function fetchPage(id, offset) {
  return api.get(`/api/playlists/${Number(id)}/related`, { limit: PAGE, offset });
}

export async function openRelated(id) {
  const p = panel(id);
  p.open = true;
  if (p.items || p.loading) {
    redraw('dc-related');
    return;
  }
  p.loading = true;
  p.error = '';
  redraw('dc-related');
  p.sig = trackSig(id);
  try {
    const res = await fetchPage(id, 0);
    p.items = res.related || [];
    p.total = Number(res.total) || p.items.length;
    p.next = res.next_offset ?? null;
  } catch (err) {
    p.items = null;
    p.error = err.detail || 'Could not load related playlists.';
  } finally {
    p.loading = false;
    redraw('dc-related');
  }
}

function currentPlaylistId() {
  const r = state.route;
  if (!r || r.name !== 'playlist') return null;
  const m = /^(\d{1,18})/.exec((r.params && r.params.ref) || '');
  return m ? Number(m[1]) : null;
}

function itemTpl(pid, it) {
  const pl = it.playlist || {};
  const p = panel(pid);
  const open = p.expanded.has(pl.id);
  const shared = (it.shared || []).map((t) => ({ ...t, playlist_id: pl.id }));
  const more = Math.max(0, Number(it.shared_count) - shared.length);
  return html`<li class="dc-rel-item">
    <div class="dc-rel-head">
      <a class="dc-rel-title" href=${playlistUrl(pl.id, pl.title)}>${pl.title}</a>
      <button type="button" class="dc-rel-count" data-action="related-expand" data-playlist-id=${pid}
        data-related-id=${pl.id} aria-expanded=${open ? 'true' : 'false'}
        aria-label=${`${it.shared_count} shared tracks with ${pl.title}; ${open ? 'hide' : 'show'} them`}>
        ${it.shared_count} shared<svg class="i dc-chev"><use href="#i-chevron-down"></use></svg>
      </button>
    </div>
    <p class="dc-rel-meta">
      ${pl.user ? html`<a class="hop" href=${userUrl(pl.user.username)}>by @${pl.user.username}</a>` : nothing}
      <span>${plural(pl.track_count, 'track')}</span>
    </p>
    ${open && shared.length
      ? html`${compactList(shared, {
          key: `rel:${pid}:${pl.id}`,
          label: `Shared with ${pl.title}`,
          source: { playlistId: pl.id, title: pl.title, kind: 'playlist' },
        })}
        ${more ? html`<p class="dc-rel-more"><a class="hop" href=${playlistUrl(pl.id, pl.title)}>and ${more} more in ‘${pl.title}’</a></p>` : nothing}`
      : nothing}
  </li>`;
}

function panelTpl(pid, where) {
  const p = panel(pid);
  if (!p.open) return nothing;
  return html`<div class="dc-related dc-related-${where}" aria-label="Playlists that share these tracks">
    ${where === 'rail'
      ? html`<div class="dc-rail-head">
          <p class="label">Shares these tracks</p>
          <button type="button" class="icon-btn" data-action="related-toggle" data-playlist-id=${pid}
            aria-label="Close related playlists"><svg class="i"><use href="#i-close"></use></svg></button>
        </div>`
      : nothing}
    ${p.loading
      ? html`<p class="quiet-line">Loading…</p>`
      : p.error
        ? html`<p class="quiet-line">${p.error}</p>`
        : html`<ul class="dc-rel-list">${repeat(p.items || [], (it) => it.playlist.id, (it) => itemTpl(pid, it))}</ul>
          ${p.next != null
            ? html`<p class="more-row"><button type="button" class="btn btn-quiet" data-action="related-more"
                data-playlist-id=${pid} ?disabled=${p.more}>${p.more ? 'Loading…' : 'More'}</button></p>`
            : nothing}`}
  </div>`;
}

// The header line + the inline panel (narrow screens) on a playlist page.
slots.register('playlist.related', (ctx) => {
  const pid = Number(ctx.playlistId);
  const count = Number(ctx.relatedCount) || 0;
  if (!pid || !count || ctx.crate) return null;
  const p = panel(pid);
  if (ctx.hash === 'related' && !p.open && !p.autoOpened) {
    p.autoOpened = true;
    queueMicrotask(() => openRelated(pid));
  }
  const label = `${plural(count, 'playlist')} share${count === 1 ? 's' : ''} these tracks`;
  return html`<div class="related-line dc-related-line" id="related">
    <button type="button" class="link-btn" data-action="related-toggle" data-playlist-id=${pid}
      aria-expanded=${p.open ? 'true' : 'false'}>${label}</button>
    ${panelTpl(pid, 'inline')}
  </div>`;
});

// The same panel in the right rail (only visible at ≥ 1100px).
slots.register('rail.context', () => {
  const pid = currentPlaylistId();
  if (!pid || !panels.has(pid) || !panels.get(pid).open) return null;
  return panelTpl(pid, 'rail');
});

registerActions({
  'related-toggle': (el, e, ctx) => {
    const pid = Number(el.dataset.playlistId || ctx.playlistId);
    if (!pid) return null;
    const p = panel(pid);
    if (p.open) {
      p.open = false;
      redraw('dc-related');
      return null;
    }
    return openRelated(pid);
  },
  'related-expand': (el) => {
    const p = panel(Number(el.dataset.playlistId));
    const rid = Number(el.dataset.relatedId);
    if (p.expanded.has(rid)) p.expanded.delete(rid);
    else p.expanded.add(rid);
    redraw('dc-related');
  },
  'related-more': async (el) => {
    const pid = Number(el.dataset.playlistId);
    const p = panel(pid);
    if (p.more || p.next == null) return;
    p.more = true;
    redraw('dc-related');
    try {
      const res = await fetchPage(pid, p.next);
      const seen = new Set((p.items || []).map((it) => it.playlist.id));
      p.items = (p.items || []).concat((res.related || []).filter((it) => !seen.has(it.playlist.id)));
      p.next = res.next_offset ?? null;
    } catch {
      p.next = null;
    } finally {
      p.more = false;
      redraw('dc-related');
    }
  },
});

/** Forget a playlist's panel (e.g. after its tracks changed enough to matter). */
export function resetRelated(id) {
  panels.delete(Number(id));
}

/** The playlist's set of videos as this tab knows it (temp rows excluded); null when not loaded. */
function trackSig(id) {
  const pid = Number(id);
  if (!state.playlists.has(pid)) return null;
  const vids = new Set(rowsOf(pid).filter((t) => t.id > 0).map((t) => t.video_id));
  return [...vids].sort().join(',');
}

// After an edit is reconciled (or a playlist reloaded), a panel computed from the old track set is
// stale: drop it, and refetch at once if it is open, so overlap and shared counts stay true.
onChange((reasons) => {
  if (!(reasons.has('reconcile') || reasons.has('load-playlist'))) return;
  for (const [id, p] of [...panels]) {
    if (p.loading || p.sig == null) continue;
    const now = trackSig(id);
    if (now == null || now === p.sig) continue;
    const wasOpen = p.open;
    resetRelated(id);
    if (wasOpen) openRelated(id);
  }
});
