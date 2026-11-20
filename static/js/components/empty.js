// A quiet empty state: one --ink-3 line, optionally followed by buttons.

import { html } from '../lit.js';

export function empty(text, actions = null) {
  return html`<div class="empty">
    <p>${text}</p>
    ${actions ? html`<div class="empty-actions">${actions}</div>` : ''}
  </div>`;
}
