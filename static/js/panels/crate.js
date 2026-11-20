// The crate panel: a slide-over on the right (a bottom sheet on phones) listing the signed-in
// user's private crate. F built the shell and remove-row; Lane C owns it: unload, promote, clear
// unloaded and import come in through the 'crate.actions' slot (crate/crate-actions.js), per-row
// "Move to …" through 'crate.row.menu'. On phones the sheet folds down to a one-line strip
// (header only) so you can keep browsing and pasting with the crate in view.
// Crate rows never appear in any public view.

import { html, render, nothing } from '../lit.js';
import { state, rowsOf, loadPlaylist } from '../store.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { trackList } from '../components/track-row.js';
import { empty } from '../components/empty.js';
import { plural } from '../format.js';

let loading = false;
let folded = false;

export function crateRows() {
  return state.auth.crateId ? rowsOf(state.auth.crateId) : [];
}

export function renderCratePanel() {
  const el = document.getElementById('crate-panel');
  if (!el) return;
  const open = !!(state.ui.cratePanel && state.auth.signedIn && state.auth.crateId);
  el.hidden = !open;
  if (!open) {
    folded = false;
    el.classList.remove('sh-folded');
    render(nothing, el);
    return;
  }
  el.classList.toggle('sh-folded', folded);
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
      <button type="button" class="sh-fold" data-action="crate-fold" aria-expanded=${folded ? 'false' : 'true'}
        aria-label=${folded ? 'Show the crate' : 'Fold the crate to a strip'}>
        <svg class="i" aria-hidden="true"><use href="#i-chevron-down"></use></svg>
      </button>
      <span class="chip-dot" aria-hidden="true"></span>
      <h2>Crate</h2>
      <span class="count" aria-label=${entry ? plural(rows.length, 'track') : ''}>${entry ? rows.length : ''}</span>
      <a class="btn btn-quiet sh-crate-page" href="/crate">Open page</a>
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

registerActions({
  'crate-fold': () => {
    folded = !folded;
    renderCratePanel();
  },
});
