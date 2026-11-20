// One popover at a time (§1.5), anchored to its trigger. Closes on Esc, outside click and route change.
//
//   openPopover(anchor, () => TemplateResult, { onClose, label }) -> { close, rerender }
//   closePopover({ restoreFocus })
//   rerenderPopover()
//   popoverAnchor()       the element the open popover belongs to (or null)

import { html, render, nothing } from '../lit.js';

let open = null; // { anchor, tpl, onClose, label }
let wired = false;

function layer() {
  return document.getElementById('popover-layer');
}

function wire() {
  if (wired) return;
  wired = true;
  document.addEventListener('pointerdown', (e) => {
    if (!open) return;
    const t = e.target;
    const pop = layer() && layer().querySelector('.popover');
    if (pop && pop.contains(t)) return;
    if (open.anchor && open.anchor.contains && open.anchor.contains(t)) return;
    closePopover();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (open && e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closePopover({ restoreFocus: true });
    }
  }, true);
  window.addEventListener('resize', () => position());
  window.addEventListener('scroll', () => position(), { passive: true, capture: true });
}

function draw() {
  const el = layer();
  if (!el) return;
  render(
    open
      ? html`<div class="popover" role="dialog" aria-label=${open.label || 'Menu'}>${open.tpl()}</div>`
      : nothing,
    el,
  );
}

function position() {
  if (!open) return;
  const pop = layer() && layer().querySelector('.popover');
  if (!pop || !open.anchor || !open.anchor.getBoundingClientRect) return;
  const a = open.anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  const h = pop.offsetHeight;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  let left = Math.min(a.left, vw - w - 16);
  if (a.right - w > 16 && a.left + w > vw - 16) left = a.right - w;
  left = Math.max(16, left);
  let top = a.bottom + 4;
  if (top + h > vh - 8 && a.top - h - 4 > 8) top = a.top - h - 4;
  pop.style.setProperty('--left', `${Math.round(left)}px`);
  pop.style.setProperty('--top', `${Math.round(top)}px`);
}

export function openPopover(anchor, tpl, { onClose, label } = {}) {
  wire();
  if (open) closePopover();
  open = { anchor, tpl, onClose, label };
  draw();
  position();
  const pop = layer() && layer().querySelector('.popover');
  const first = pop && pop.querySelector('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (first) first.focus({ preventScroll: true });
  return { close: closePopover, rerender: rerenderPopover };
}

export function closePopover({ restoreFocus = false } = {}) {
  if (!open) return;
  const was = open;
  open = null;
  draw();
  if (restoreFocus && was.anchor && was.anchor.focus) was.anchor.focus({ preventScroll: true });
  if (typeof was.onClose === 'function') {
    try { was.onClose(); } catch (err) { console.error(err); }
  }
}

export function rerenderPopover() {
  draw();
  position();
}

export function popoverAnchor() {
  return open ? open.anchor : null;
}
