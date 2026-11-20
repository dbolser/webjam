// Menus in a popover.
//
//   openMenu(anchor, items, { label })
//   items: [{ label, icon?, run?(), href?, danger?, kbd? } | 'sep']
// An item with href is a link (router-handled); an item with run is a button.

import { html } from '../lit.js';
import { registerActions } from '../actions.js';
import { localUrl, iconHref } from '../format.js';
import { openPopover, closePopover, popoverAnchor } from './popover.js';

let current = [];

function itemTpl(it, i) {
  if (it === 'sep') return html`<div class="menu-sep" role="separator"></div>`;
  const icon = it.icon ? html`<svg class="i"><use href=${iconHref(it.icon)}></use></svg>` : '';
  const kbd = it.kbd ? html`<kbd>${it.kbd}</kbd>` : '';
  if (it.href) {
    return html`<a class="menu-item ${it.danger ? 'is-danger' : ''}" role="menuitem" href=${localUrl(it.href)}
      data-action="menu-item" data-index=${i}>${icon}<span>${it.label}</span>${kbd}</a>`;
  }
  return html`<button type="button" class="menu-item ${it.danger ? 'is-danger' : ''}" role="menuitem"
    data-action="menu-item" data-index=${i}>${icon}<span>${it.label}</span>${kbd}</button>`;
}

export function openMenu(anchor, items, { label = 'Menu' } = {}) {
  if (popoverAnchor() === anchor) {
    closePopover();
    return;
  }
  current = (items || []).filter(Boolean);
  // drop leading, trailing and doubled separators
  current = current.filter((it, i, a) => !(it === 'sep' && (i === 0 || i === a.length - 1 || a[i - 1] === 'sep')));
  if (anchor && anchor.setAttribute) anchor.setAttribute('aria-expanded', 'true');
  openPopover(anchor, () => html`<div class="menu" role="menu">${current.map(itemTpl)}</div>`, {
    label,
    onClose: () => anchor && anchor.setAttribute && anchor.setAttribute('aria-expanded', 'false'),
  });
}

registerActions({
  'menu-item': (el, event) => {
    const it = current[Number(el.dataset.index)];
    if (!it) return;
    if (it.href) {
      closePopover();
      return; // the router follows the link
    }
    event.preventDefault();
    closePopover({ restoreFocus: true });
    if (typeof it.run === 'function') it.run();
  },
});
