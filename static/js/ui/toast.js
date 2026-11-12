// Toasts (§1.5): one line of text plus at most one action; 4 s, paused on hover; at most 2 stacked.
//
//   toast.show(text, { action: { label, href } | { label, run }, timeout, glyph }) -> id
//   toast.badge(badges)          the quiet unlock toast (§1.7), coalesced
//   toast.dismiss(id)

import { html, render, repeat } from '../lit.js';
import { registerActions } from '../actions.js';
import { localUrl, userUrl } from '../format.js';
import { state } from '../store.js';

const MAX = 2;
let items = [];
let seq = 0;
let paused = false;
let wired = false;

function host() {
  const el = document.getElementById('toasts');
  if (el && !wired) {
    wired = true;
    el.addEventListener('mouseenter', () => {
      paused = true;
      for (const t of items) clearTimeout(t.timer);
    });
    el.addEventListener('mouseleave', () => {
      paused = false;
      for (const t of items) arm(t);
    });
  }
  return el;
}

function arm(t) {
  clearTimeout(t.timer);
  if (!paused && t.timeout > 0) t.timer = setTimeout(() => dismiss(t.id), t.timeout);
}

function draw() {
  const el = host();
  if (!el) return;
  render(
    repeat(items, (t) => t.id, (t) => html`<div class="toast" data-toast-id=${t.id}>
      <span class="toast-text">${t.glyph ? html`<span class="glyph" aria-hidden="true">${t.glyph}</span>` : ''}${t.text}</span>
      ${t.action
        ? t.action.href
          ? html`<a class="btn btn-quiet" href=${localUrl(t.action.href)} data-action="toast-action" data-toast-id=${t.id}>${t.action.label}</a>`
          : html`<button type="button" class="btn btn-quiet" data-action="toast-action" data-toast-id=${t.id}>${t.action.label}</button>`
        : ''}
      <button type="button" class="icon-btn" data-action="toast-close" data-toast-id=${t.id} aria-label="Dismiss">
        <svg class="i"><use href="#i-close"></use></svg>
      </button>
    </div>`),
    el,
  );
}

function show(text, { action = null, timeout = 4000, glyph = '' } = {}) {
  const t = { id: ++seq, text: String(text || ''), action, timeout, glyph, timer: null };
  items.push(t);
  while (items.length > MAX) {
    const old = items.shift();
    clearTimeout(old.timer);
  }
  arm(t);
  draw();
  return t.id;
}

function dismiss(id) {
  const t = items.find((x) => x.id === Number(id));
  if (!t) return;
  clearTimeout(t.timer);
  items = items.filter((x) => x !== t);
  draw();
}

function badge(badges) {
  const list = (Array.isArray(badges) ? badges : [badges]).filter(Boolean);
  if (!list.length) return;
  const first = list[0];
  const me = state.auth.user && state.auth.user.username;
  const name = first.name || first.key || 'a badge';
  const text = list.length > 1 ? `Unlocked · ${name} and ${list.length - 1} more` : `Unlocked · ${name}`;
  show(text, {
    glyph: list.length > 1 ? '' : first.icon || '',
    timeout: 6000,
    action: me ? { label: 'View', href: `${userUrl(me)}#badges` } : null,
  });
}

export const toast = { show, badge, dismiss };

registerActions({
  'toast-action': (el, event) => {
    const t = items.find((x) => x.id === Number(el.dataset.toastId));
    if (t && t.action && typeof t.action.run === 'function') {
      event.preventDefault();
      t.action.run();
    }
    dismiss(el.dataset.toastId);
  },
  'toast-close': (el) => dismiss(el.dataset.toastId),
});
