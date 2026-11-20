// Choose your @handle (docs/phase1b-shell.md §3.8): a quiet card at the top of my page while
// needs_handle is true (Google mode only), and the same form in a sheet from the account menu.
// "Keep @x" saves the current handle unchanged (that is how it gets confirmed); "Save" saves a new
// one. PUT /api/me/handle; errors (taken, reserved, bad shape) show inline.

import { html, nothing } from '../lit.js';
import { state, update } from '../store.js';
import * as api from '../api.js';
import * as slots from '../slots.js';
import { registerActions } from '../actions.js';
import { replaceUrl } from '../router.js';
import { toast } from '../ui/toast.js';
import { openSheet, closeSheet, rerenderSheet } from '../ui/sheet.js';
import { whenSheetClosed } from '../share/util.js';

const HANDLE = /^[a-z0-9_]{3,20}$/;
const err = { card: '', sheet: '' };
let busy = false;

export function cleanHandle(raw) {
  return String(raw || '').trim().replace(/^@+/, '').toLowerCase();
}

export function handleProblem(h) {
  if (!h) return 'Choose a handle.';
  if (!HANDLE.test(h)) return 'Handles are 3–20 characters: lowercase letters, digits and underscores.';
  return '';
}

function formTpl(where, username) {
  const id = `sh-handle-${where}`;
  const msg = err[where];
  return html`<form id=${id} class="sh-handle-form" data-action="handle-save" data-where=${where} novalidate>
    <span class="sh-at" aria-hidden="true">@</span>
    <input class="input" name="handle" value=${username || ''} maxlength="20" autocomplete="off" autocapitalize="none"
      spellcheck="false" aria-label="Handle" aria-invalid=${msg ? 'true' : 'false'} aria-describedby=${`${id}-err`} />
    ${where === 'card'
      ? html`<button type="submit" class="btn btn-primary" ?disabled=${busy}>Save</button>
          <button type="button" class="btn btn-quiet" data-action="handle-keep" ?disabled=${busy}>Keep @${username}</button>`
      : nothing}
    <p class="sh-err" id=${`${id}-err`} role="alert">${msg}</p>
  </form>`;
}

function me() {
  return state.auth.user;
}

slots.register('home.top', (ctx = {}) => {
  const home = ctx.home || state.home;
  const u = (home && home.user) || me();
  if (state.auth.mode !== 'google' || !u || !u.needs_handle) return null;
  return html`<section class="sh-handle" aria-labelledby="sh-handle-h">
    <h2 class="label" id="sh-handle-h">Your handle</h2>
    <p class="sh-handle-text">Your page lives at <span class="mono">/u/${u.username}</span>. Keep this handle or pick another; you can change it later.</p>
    ${formTpl('card', u.username)}
  </section>`;
}, { order: -10 });

async function saveHandle(handle, where, form) {
  const h = cleanHandle(handle);
  const problem = handleProblem(h);
  if (problem) {
    err[where] = problem;
    rerender(where);
    if (form) form.querySelector('input[name="handle"]').focus();
    return;
  }
  if (busy) return;
  busy = true;
  err[where] = '';
  rerender(where);
  try {
    const res = await api.put('/api/me/handle', { handle: h });
    const name = res.username || h;
    const old = me() && me().username;
    update((s) => {
      if (s.auth.user) s.auth.user = { ...s.auth.user, username: name, needs_handle: false };
      if (s.home && s.home.user) s.home = { ...s.home, user: { ...s.home.user, username: name, needs_handle: false } };
    }, 'handle');
    if (where === 'sheet') closeSheet('done');
    if (old && old !== name && location.pathname.toLowerCase() === `/u/${old}`) replaceUrl(`/u/${name}`);
    toast.show(old === name ? `Keeping @${name}` : `You are @${name} now`);
  } catch (e) {
    err[where] = e.detail || 'Could not save that handle.';
  } finally {
    busy = false;
    rerender(where);
  }
}

function rerender(where) {
  if (where === 'sheet') rerenderSheet();
  else update(() => {}, 'handle-form');
}

export function openHandleSheet() {
  const u = me();
  if (!u || state.auth.mode !== 'google') return;
  whenSheetClosed(() => {
    err.sheet = '';
    openSheet({
      title: 'Your handle',
      body: () => html`<p class="quiet-line sh-gap">People find you at /u/your-handle. Old links keep working.</p>
        ${formTpl('sheet', me() ? me().username : '')}`,
      foot: () => html`
        <button type="button" class="btn btn-quiet" data-action="sheet-close">Cancel</button>
        <button type="submit" class="btn btn-primary" form="sh-handle-sheet" ?disabled=${busy}>Save</button>`,
    });
  });
}

registerActions({
  'handle-save': (form) => {
    const input = form.querySelector('input[name="handle"]');
    saveHandle(input ? input.value : '', form.dataset.where === 'sheet' ? 'sheet' : 'card', form);
  },
  'handle-keep': () => {
    const u = me();
    if (u) saveHandle(u.username, 'card', null);
  },
});
