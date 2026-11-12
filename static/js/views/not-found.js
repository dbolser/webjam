import { html } from '../lit.js';

export function title() {
  return 'Not found';
}

export function render() {
  return html`<div class="page not-found">
    <h1 tabindex="-1">Nothing here</h1>
    <p>This page doesn't exist, or it isn't shared with you.</p>
    <p><a href="/">Go to the front page</a> or <a href="/discover">discover playlists</a>.</p>
  </div>`;
}
