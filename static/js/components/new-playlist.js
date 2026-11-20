// The inline "New playlist" title field (home list and dock). Submitting runs the F action
// 'new-playlist-submit' (POST /api/playlists, then navigate to it); Esc or Cancel closes it.

import { html, nothing, ref } from '../lit.js';

const focused = new WeakSet();
function focusOnce(el) {
  if (el && !focused.has(el)) {
    focused.add(el);
    requestAnimationFrame(() => el.focus());
  }
}

export function newPlaylistForm(where) {
  const inDock = where === 'dock';
  return html`<form class=${inDock ? 'chip-form' : 'new-form'} data-action="new-playlist-submit"
      data-cancel-action="new-playlist-cancel" data-where=${where}>
    <input class="input" name="title" maxlength="200" required autocomplete="off"
      placeholder="Name the playlist" aria-label="New playlist title" ${ref(focusOnce)} />
    ${inDock ? nothing : html`
      <button class="btn btn-primary" type="submit">Create</button>
      <button class="btn btn-quiet" type="button" data-action="new-playlist-cancel">Cancel</button>`}
  </form>`;
}
