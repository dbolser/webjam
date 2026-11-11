// A small yes/no sheet.  await confirm({ title, body, confirmLabel, danger }) -> boolean

import { html } from '../lit.js';
import { registerActions } from '../actions.js';
import { openSheet, closeSheet } from './sheet.js';

let resolver = null;

export function confirm({ title = 'Are you sure?', body = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false } = {}) {
  if (resolver) resolver(false);
  return new Promise((resolve) => {
    resolver = resolve;
    openSheet({
      title,
      body: () => html`<p class="ink-2">${body}</p>`,
      foot: () => html`
        <button type="button" class="btn btn-quiet" data-action="confirm-no">${cancelLabel}</button>
        <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-action="confirm-yes">${confirmLabel}</button>`,
      onClose: (value) => {
        const r = resolver;
        resolver = null;
        if (r) r(value === 'yes');
      },
    });
  });
}

registerActions({
  'confirm-yes': () => closeSheet('yes'),
  'confirm-no': () => closeSheet('no'),
});
