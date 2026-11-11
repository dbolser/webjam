// The crate panel shell: a slide-over on the right (a bottom sheet on phones) listing the
// signed-in user's private crate. F builds the shell and remove-row; Lane C owns it afterwards
// (unload, promote, clear unloaded) through the 'crate.actions' and 'crate.row.menu' slots.
// Crate rows never appear in any public view.

import { html, render, nothing } from '../lit.js';
import { state, rowsOf, loadPlaylist } from '../store.js';
import * as slots from '../slots.js';
import { trackList } from '../components/track-row.js';
import { empty } from '../components/empty.js';

let loading = false;

export function crateRows() {
  return state.auth.crateId ? rowsOf(state.auth.crateId) : [];
}

export function renderCratePanel() {
  const el = document.getElementById('crate-panel');
  if (!el) return;
  const open = !!(state.ui.cratePanel && state.auth.signedIn && state.auth.crateId);
  el.hidden = !open;
  if (!open) {
    render(nothing, el);
    return;
  }
  const cid = state.auth.crateId;
  const entry = state.playlists.get(cid);
  if (!entry && !loading) {
    loading = true;
    loadPlaylist(cid).catch(() => {}).finally(() => { loading = false; });
  }
  const rows = entry ? rowsOf(cid) : [];
  const ctx = { crateId: cid, rows };
  render(html`
    <header class="crate-head">
      <span class="chip-dot" aria-hidden="true"></span>
      <h2>Crate</h2>
      <span class="count">${entry ? rows.length : ''}</span>
      <a class="btn btn-quiet" href="/crate">Open page</a>
      <button type="button" class="icon-btn" data-action="close-crate" aria-label="Close crate">
        <svg class="i"><use href="#i-close"></use></svg>
      </button>
    </header>
    <div class="crate-actions">${slots.render('crate.actions', ctx)}</div>
    <div class="crate-body">
      ${!entry
        ? html`<p class="loading">Loading…</p>`
        : rows.length
          ? trackList(rows, { key: 'crate', context: 'crate', editable: true, playlistId: cid, label: 'Crate',
            source: { playlistId: cid, title: 'Crate', kind: 'crate' } })
          : empty('Your crate is empty. Paste a YouTube link anywhere, or press + on any track.')}
    </div>`, el);
}
