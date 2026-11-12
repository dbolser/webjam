// The keymap (docs/phase1b-shell.md §5.1). F creates it; Lane A owns it afterwards.
//
//   registerKeys([{ combo: 'mod+c', when: 'list', label: 'Copy', group: 'Edit', run }])
//
// combo: '+'-joined modifiers (mod, ctrl, meta, alt, shift) and a key: a letter, a digit, '?', '/',
//        '#', 'space', 'enter', 'escape', 'delete', 'backspace', 'arrowup', 'arrowleft', ... .
//        'mod' is ⌘ on macOS and Ctrl elsewhere. A combo without 'shift' still matches a shifted
//        key whose character needs Shift ('?', '#').
// when:  'always' | 'list' (focus in a [role=listbox] list) | 'editable' (that list has
//        data-editable) | 'player' (focus in the player bar), or an array of them.
// label/group: shown in the ? sheet (omit label to keep a binding out of it); display: optional text
//        shown instead of the combo.
// run(event, ctx): return false to pass the key on to the next handler; anything else handles it
//        (preventDefault). ctx = { row, list, trackId, playlistId, videoId, index }.
// Later registrations run first, so a lane can extend a key (Esc) by registering it again.
// Keys are ignored in inputs/textareas/selects/contenteditable, during IME composition, and while
// a modal sheet is open (except Escape). e.repeat is ignored except for the arrow keys.

import { contextOf, runAction } from './actions.js';

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
const bindings = [];

function parse(combo) {
  const parts = String(combo).toLowerCase().split('+');
  let key = parts.pop();
  if (key === '' && parts.length && parts[parts.length - 1] === '') {
    parts.pop();
    key = '+';
  }
  const mods = new Set(parts);
  if (mods.has('mod')) {
    mods.delete('mod');
    mods.add(IS_MAC ? 'meta' : 'ctrl');
  }
  if (key === 'space') key = ' ';
  if (key === 'esc') key = 'escape';
  if (key === 'del') key = 'delete';
  return { key, ctrl: mods.has('ctrl'), meta: mods.has('meta'), alt: mods.has('alt'), shift: mods.has('shift') };
}

export function registerKeys(list) {
  for (const b of list) {
    if (!b || !b.combo || typeof b.run !== 'function') throw new TypeError('registerKeys: combo and run are required');
    bindings.unshift({ ...b, parsed: parse(b.combo), when: b.when || 'always' });
  }
}

/** Every registration, for the ? cheat sheet: [{ combo, label, group, when, display }].
 *  `display` (optional) replaces the combo's label in the sheet, e.g. '0–9' for ten digit bindings. */
export function listKeys() {
  return bindings.filter((b) => b.label).map(({ combo, label, group, when, display }) => ({ combo, label, group, when, display })).reverse();
}

export function comboLabel(combo) {
  return String(combo)
    .split('+')
    .map((p) => {
      const k = p.toLowerCase();
      if (k === 'mod') return IS_MAC ? '⌘' : 'Ctrl';
      if (k === 'shift') return IS_MAC ? '⇧' : 'Shift';
      if (k === 'alt') return IS_MAC ? '⌥' : 'Alt';
      if (k === 'arrowup') return '↑';
      if (k === 'arrowdown') return '↓';
      if (k === 'arrowleft') return '←';
      if (k === 'arrowright') return '→';
      if (k === 'escape') return 'Esc';
      if (k === 'space') return 'Space';
      return p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1);
    })
    .join(IS_MAC ? '' : '+');
}

function keyOf(e) {
  const k = e.key || '';
  return k.length === 1 ? k.toLowerCase() : k.toLowerCase();
}

function matches(p, e) {
  const k = keyOf(e);
  if (k !== p.key) {
    // Digits typed with Shift on some layouts, or e.code fallbacks for letters with Alt on macOS.
    const code = e.code || '';
    const fromCode = code.startsWith('Key') ? code.slice(3).toLowerCase() : code.startsWith('Digit') ? code.slice(5) : null;
    if (!(e.altKey && fromCode === p.key)) return false;
  }
  if (p.ctrl !== e.ctrlKey || p.meta !== e.metaKey || p.alt !== e.altKey) return false;
  const shiftedChar = p.key.length === 1 && !/[a-z0-9 ]/.test(p.key);
  if (!shiftedChar && p.shift !== e.shiftKey) return false;
  return true;
}

function inField(el) {
  return !!(el && el.closest && el.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
}

function scopeOf(target) {
  const el = target instanceof Element ? target : null;
  const list = el ? el.closest('[role="listbox"]') : null;
  return {
    list,
    editable: !!(list && list.hasAttribute('data-editable')),
    player: !!(el && el.closest('#player-bar')),
  };
}

function allowed(when, scope) {
  const ws = Array.isArray(when) ? when : [when];
  return ws.some((w) =>
    w === 'always' || (w === 'list' && scope.list) || (w === 'editable' && scope.editable) || (w === 'player' && scope.player));
}

function sheetOpen() {
  const d = document.getElementById('sheet');
  return !!(d && d.open);
}

let started = false;
export function startKeys() {
  if (started) return;
  started = true;
  document.addEventListener('keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    const key = keyOf(e);
    const target = e.target;
    if (key === 'escape' && inField(target)) {
      // Esc inside an inline field: run the form's cancel action, if it declares one.
      const form = target.closest('form[data-cancel-action]');
      if (form) {
        e.preventDefault();
        runAction(form.dataset.cancelAction, form, e);
      }
      return;
    }
    if (inField(target)) return;
    if (sheetOpen() && key !== 'escape') return;
    const arrow = key.startsWith('arrow');
    if (e.repeat && !arrow) return;
    const scope = scopeOf(target);
    const ctx = contextOf(target instanceof Element ? target : document.body);
    ctx.index = ctx.row ? Number(ctx.row.dataset.index) : null;
    for (const b of bindings) {
      if (!matches(b.parsed, e) || !allowed(b.when, scope)) continue;
      let out;
      try {
        out = b.run(e, ctx);
      } catch (err) {
        console.error(`key ${b.combo}`, err);
        out = true;
      }
      if (out === false) continue;
      e.preventDefault();
      return;
    }
  });
}
