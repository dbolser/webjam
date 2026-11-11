// Sheets (§1.5): a centred dialog on desktop, a bottom sheet on phones, using <dialog>.showModal().
// Esc closes; focus returns to the trigger.
//
//   openSheet({ title, body: () => tpl, foot?: () => tpl, onClose?, label? }) -> { close, rerender }
//   closeSheet(returnValue?)

import { html, render, nothing } from '../lit.js';
import { registerActions } from '../actions.js';

let open = null; // { opts, trigger }
let wired = false;

function dialog() {
  return document.getElementById('sheet');
}

function draw() {
  const d = dialog();
  if (!d) return;
  if (!open) {
    render(nothing, d);
    return;
  }
  const { title, body, foot } = open.opts;
  render(html`
    <div class="sheet-head">
      <h2 id="sheet-title">${title || ''}</h2>
      <button type="button" class="icon-btn" data-action="sheet-close" aria-label="Close">
        <svg class="i"><use href="#i-close"></use></svg>
      </button>
    </div>
    <div class="sheet-body">${body ? body() : ''}</div>
    ${foot ? html`<div class="sheet-foot">${foot()}</div>` : ''}
  `, d);
}

export function openSheet(opts) {
  const d = dialog();
  if (!d) return { close() {}, rerender() {} };
  if (!wired) {
    wired = true;
    d.addEventListener('close', () => {
      const was = open;
      open = null;
      draw();
      if (was && was.trigger && was.trigger.focus && document.contains(was.trigger)) {
        was.trigger.focus({ preventScroll: true });
      }
      if (was && typeof was.opts.onClose === 'function') {
        try { was.opts.onClose(d.returnValue); } catch (err) { console.error(err); }
      }
    });
  }
  if (open && d.open) d.close();
  open = { opts, trigger: document.activeElement };
  d.setAttribute('aria-labelledby', 'sheet-title');
  draw();
  if (!d.open) d.showModal();
  const first = d.querySelector('.sheet-body input, .sheet-body textarea, .sheet-body select, .sheet-foot .btn-primary');
  if (first) first.focus();
  return { close: closeSheet, rerender: draw };
}

export function closeSheet(value = '') {
  const d = dialog();
  if (d && d.open) d.close(value);
}

export function rerenderSheet() {
  draw();
}

registerActions({
  'sheet-close': () => closeSheet('cancel'),
});
