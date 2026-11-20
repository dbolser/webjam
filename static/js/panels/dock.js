// The dock (left rail; a chip strip on phones). Signed-in only.
// Each chip carries data-drop-target ("crate:{id}" for slot 0, "playlist:{id}" otherwise) and
// data-slot for Lane A's drag-and-drop and digit keys.
//
//   renderDock()
//   chipForSlot(n) -> chip | null
//   openSlot(n)    navigate to slot n's playlist (slot 0 opens the crate panel)

import { html, render, nothing } from '../lit.js';
import { state, update } from '../store.js';
import { playlistUrl } from '../format.js';
import { newPlaylistForm } from '../components/new-playlist.js';
import * as slots from '../slots.js';

export function chips() {
  const d = state.dock;
  if (!d) return [];
  return [d.crate, ...(d.pinned || []), ...(d.recent || [])].filter(Boolean);
}

export function chipForSlot(n) {
  return chips().find((c) => c.slot === Number(n)) || null;
}

export async function openSlot(n) {
  const chip = chipForSlot(n);
  if (!chip) return;
  if (chip.kind === 'crate') {
    update((s) => { s.ui.cratePanel = true; }, 'crate-panel');
    return;
  }
  const router = await import('../router.js');
  router.navigate(playlistUrl(chip.playlist_id, chip.title));
}

function currentPlaylistId() {
  const r = state.route;
  if (r.name !== 'playlist') return null;
  const m = /^(\d+)/.exec(r.params.ref || '');
  return m ? Number(m[1]) : null;
}

function chipTpl(c, here) {
  const slot = c.slot != null ? c.slot : '';
  const label = `${c.slot != null ? `Slot ${c.slot}: ` : ''}${c.title}, ${c.track_count} tracks`;
  if (c.kind === 'crate') {
    return html`<li><button type="button" class="chip" data-action="open-crate"
        data-drop-target=${`crate:${c.playlist_id}`} data-slot="0" data-playlist-id=${c.playlist_id}
        aria-label=${label} aria-pressed=${state.ui.cratePanel ? 'true' : 'false'}>
      <span class="chip-slot">0</span><span class="chip-dot" aria-hidden="true"></span>
      <span class="chip-label">Crate</span><span class="chip-count">${c.track_count}</span>
    </button></li>`;
  }
  return html`<li><a class="chip" href=${playlistUrl(c.playlist_id, c.title)}
      data-drop-target=${`playlist:${c.playlist_id}`} data-slot=${slot} data-playlist-id=${c.playlist_id}
      aria-label=${label} aria-current=${here === c.playlist_id ? 'page' : nothing}>
    <span class="chip-slot">${slot}</span>
    <span class="chip-label">${c.title}</span><span class="chip-count">${c.track_count}</span>
  </a></li>`;
}

export function renderDock() {
  const el = document.getElementById('dock');
  const app = document.getElementById('app');
  if (!el) return;
  const show = !!(state.auth.signedIn && state.dock);
  el.hidden = !show;
  if (app) app.classList.toggle('no-dock', !show);
  document.body.classList.toggle('has-dock', show);
  if (!show) {
    render(nothing, el);
    return;
  }
  const d = state.dock;
  const here = currentPlaylistId();
  const recent = d.recent || [];
  render(html`
    <p class="label dock-label">Dock</p>
    <ul class="dock-list">
      ${chipTpl(d.crate, here)}
      ${(d.pinned || []).map((c) => chipTpl(c, here))}
      ${recent.length && (d.pinned || []).length ? html`<li class="dock-sep" role="presentation"></li>` : nothing}
      ${recent.map((c) => chipTpl(c, here))}
      <li>${state.ui.newPlaylist === 'dock'
        ? newPlaylistForm('dock')
        : html`<button type="button" class="chip chip-new" data-action="new-playlist" data-where="dock"
            data-drop-target="new">
            <span class="chip-slot"><svg class="i"><use href="#i-plus"></use></svg></span>
            <span class="chip-label">New playlist</span></button>`}</li>
      ${slots.has('import.open')
        ? html`<li class="dock-import"><button type="button" class="link-btn dock-import-btn" data-action="import-open"
            data-target="crate">Import from YouTube…</button></li>`
        : nothing}
    </ul>`, el);
}

